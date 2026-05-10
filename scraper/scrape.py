"""
TwitchTracker subscribers scraper — paginated to fetch 100 channels per list.
 
Scrapes:
  - https://twitchtracker.com/subscribers              page 1..5  (active, last 30 days)
  - https://twitchtracker.com/subscribers/all-time     page 1..5  (all-time)
 
The two pages have DIFFERENT column layouts:
 
  Active (11 columns):
    [0] rank   [1] diff   [2] avatar (linked)   [3] name+link
    [4] active subs   [5] paid   [6] prime   [7] gifted
    [8] tier 1   [9] tier 2   [10] tier 3
 
  All-time (8 columns):
    [0] rank   [1] diff   [2] avatar (NO link)   [3] name+link
    [4] all-time subs   [5] peak month/year   [6] prime
    [7] active/inactive flag
 
We detect the page type by:
  - URL ending in "/all-time", or
  - Cell count + structure (avatar cell has <a> on active, none on all-time)
 
Defensive features:
  - Filters out ad rows
  - Handles "?" cells as null
  - Decodes "▲▲▲" multi-arrow as diff = 999
  - Polite delays between pages and lists
  - Validates total scrape (>= 50 rows per list, top channel has subs > 0)
    before overwriting subscribers.json
"""
 
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
 
from playwright.sync_api import Page, sync_playwright
 
PAGES_PER_LIST = 5
DELAY_BETWEEN_PAGES_MS = 3000
DELAY_BETWEEN_LISTS_MS = 8000
 
LISTS = {
    "active":   "https://twitchtracker.com/subscribers",
    "all_time": "https://twitchtracker.com/subscribers/all-time",
}
 
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"
 
MIN_ROWS_FOR_VALID = 50
MIN_TOP_SUBS_FOR_VALID = 1000
 
 
# ---------- helpers ----------
 
def parse_int(text):
    if text is None:
        return None
    text = text.strip().replace(",", "").replace("\u00a0", "")
    if not text or text in ("?", "-", "—"):
        return None
    try:
        return int(text)
    except ValueError:
        return None
 
 
def parse_diff_from_cell(cell):
    if cell is None:
        return 0
    span = cell.query_selector("span.diff")
    if not span:
        return 0
    classes = span.get_attribute("class") or ""
    text = span.inner_text().strip()
    arrows = span.query_selector_all("i.fas.fa-caret-up, i.fas.fa-caret-down")
    if len(arrows) >= 2 and not re.search(r"\d", text):
        return 999 if "text-success" in classes else -999
    num_match = re.search(r"\d+", text)
    if not num_match:
        return 0
    n = int(num_match.group(0))
    return -n if "text-danger" in classes else n
 
 
def parse_rank(text):
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None
 
 
def slug_from_href(href):
    if not href:
        return None
    parts = [p for p in href.strip("/").split("/") if p]
    return parts[0] if parts else None
 
 
def page_url(base_url, page_num):
    if page_num <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page_num}"
 
 
def get_avatar_url(cell):
    """Find the <img> in this cell and return its src. Works whether or not
    the img is wrapped in an <a>."""
    if cell is None:
        return None
    img = cell.query_selector("img")
    if not img:
        return None
    return img.get_attribute("src") or img.get_attribute("data-src")
 
 
# ---------- per-row parsers ----------
 
def parse_row_active(cells):
    """Active page row: 11 cells. Avatar is wrapped in a link."""
    if len(cells) < 11:
        return None
    rank = parse_rank(cells[0].inner_text())
    if not rank:
        return None
 
    diff = parse_diff_from_cell(cells[1])
 
    # Avatar: linked
    avatar_url = get_avatar_url(cells[2])
    slug = None
    avatar_link = cells[2].query_selector("a")
    if avatar_link:
        slug = slug_from_href(avatar_link.get_attribute("href"))
 
    # Name
    name_link = cells[3].query_selector("a")
    name = name_link.inner_text().strip() if name_link else cells[3].inner_text().strip()
    if not slug and name_link:
        slug = slug_from_href(name_link.get_attribute("href"))
    if not name:
        return None
 
    return {
        "rank":   rank,
        "diff":   diff,
        "name":   name,
        "slug":   slug,
        "url":    f"https://twitchtracker.com/{slug}/subscribers" if slug else None,
        "avatar": avatar_url,
        "subs":   parse_int(cells[4].inner_text()),
        "paid":   parse_int(cells[5].inner_text()),
        "prime":  parse_int(cells[6].inner_text()),
        "gifted": parse_int(cells[7].inner_text()),
        "t1":     parse_int(cells[8].inner_text()),
        "t2":     parse_int(cells[9].inner_text()),
        "t3":     parse_int(cells[10].inner_text()),
    }
 
 
def parse_row_all_time(cells):
    """
    All-time page row: 8 cells. Avatar has NO link. Different columns.
      [0] rank   [1] diff   [2] avatar   [3] name+link
      [4] all-time subs   [5] peak month   [6] prime   [7] active flag
    """
    if len(cells) < 5:
        return None
    rank = parse_rank(cells[0].inner_text())
    if not rank:
        return None
 
    diff = parse_diff_from_cell(cells[1])
 
    # Avatar: bare img, no link wrapper
    avatar_url = get_avatar_url(cells[2])
 
    # Name + slug from cell[3]
    name_link = cells[3].query_selector("a")
    name = name_link.inner_text().strip() if name_link else cells[3].inner_text().strip()
    slug = slug_from_href(name_link.get_attribute("href")) if name_link else None
    if not name:
        return None
 
    subs  = parse_int(cells[4].inner_text())
    # cell[5] is peak month (e.g. "September\n2025") — keep as raw text
    peak_text = cells[5].inner_text().strip().replace("\n", " ") if len(cells) >= 6 else None
    prime = parse_int(cells[6].inner_text()) if len(cells) >= 7 else None
    # cell[7] has an icon for active/inactive; we don't store it but you could
 
    return {
        "rank":   rank,
        "diff":   diff,
        "name":   name,
        "slug":   slug,
        "url":    f"https://twitchtracker.com/{slug}/subscribers" if slug else None,
        "avatar": avatar_url,
        "subs":   subs,
        "paid":   None,            # not present on all-time page
        "prime":  prime,
        "gifted": None,            # not present on all-time page
        "t1":     None,
        "t2":     None,
        "t3":     None,
        "peak":   peak_text,       # bonus field — only on all-time
    }
 
 
def parse_row(cells, list_kind):
    """Dispatch to the right parser based on which list we're on."""
    # Skip ad rows (single colspan cell)
    if len(cells) <= 1:
        return None
    first_colspan = cells[0].get_attribute("colspan")
    if first_colspan and int(first_colspan) > 1:
        return None
 
    if list_kind == "all_time":
        return parse_row_all_time(cells)
    return parse_row_active(cells)
 
 
# ---------- page-level ----------
 
def diagnose_table(page: Page, label: str, page_num: int):
    try:
        rows = page.query_selector_all("table#channels tbody tr")
        print(f"   [diag] total tbody rows: {len(rows)}", flush=True)
        for i, row in enumerate(rows[:2]):
            cells = row.query_selector_all("td")
            print(f"   [diag] row {i} cell count: {len(cells)}", flush=True)
            for j, cell in enumerate(cells[:12]):
                txt = (cell.inner_text() or '').strip()[:40]
                print(f"     td[{j}] {txt!r}", flush=True)
        debug = Path(__file__).resolve().parent / f"debug_{label}_p{page_num}.png"
        page.screenshot(path=str(debug), full_page=True)
        print(f"   [diag] screenshot: {debug}", flush=True)
    except Exception as e:
        print(f"   [diag] failed: {e}", flush=True)
 
 
def scrape_one_page(page: Page, url: str, list_kind: str, page_num: int) -> list[dict]:
    print(f"[{list_kind}] page {page_num}: {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("table#channels tbody tr", timeout=30_000)
    except Exception:
        print(f"[{list_kind}] page {page_num}: ! No #channels table found.", flush=True)
        diagnose_table(page, list_kind, page_num)
        return []
 
    rows_el = page.query_selector_all("table#channels tbody tr")
    parsed = []
    for row in rows_el:
        cells = row.query_selector_all("td")
        result = parse_row(cells, list_kind)
        if result is not None:
            parsed.append(result)
 
    print(f"[{list_kind}] page {page_num}: parsed {len(parsed)} rows", flush=True)
    if not parsed:
        diagnose_table(page, list_kind, page_num)
    return parsed
 
 
def scrape_list(page: Page, base_url: str, list_kind: str) -> list[dict]:
    all_rows = []
    seen_ranks = set()
 
    for page_num in range(1, PAGES_PER_LIST + 1):
        url = page_url(base_url, page_num)
        rows = scrape_one_page(page, url, list_kind, page_num)
        if not rows and page_num == 1:
            print(f"[{list_kind}] page 1 empty; waiting 10s and retrying once...", flush=True)
            page.wait_for_timeout(10000)
            rows = scrape_one_page(page, url, list_kind, page_num)
 
        if not rows:
            print(f"[{list_kind}] page {page_num} empty; stopping pagination.", flush=True)
            break
 
        for r in rows:
            if r["rank"] in seen_ranks:
                continue
            seen_ranks.add(r["rank"])
            all_rows.append(r)
 
        if page_num < PAGES_PER_LIST:
            page.wait_for_timeout(DELAY_BETWEEN_PAGES_MS)
 
    print(f"[{list_kind}] TOTAL: {len(all_rows)} unique channels", flush=True)
    return all_rows
 
 
def is_valid(channels):
    if len(channels) < MIN_ROWS_FOR_VALID:
        return False, f"only {len(channels)} rows (need >= {MIN_ROWS_FOR_VALID})"
    top_subs = channels[0].get("subs") or 0
    if top_subs < MIN_TOP_SUBS_FOR_VALID:
        return False, f"top channel has {top_subs} subs (need >= {MIN_TOP_SUBS_FOR_VALID})"
    return True, "ok"
 
 
def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
 
    fresh = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": "twitchtracker.com",
        "lists": {},
    }
 
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
 
        list_items = list(LISTS.items())
        for i, (label, base_url) in enumerate(list_items):
            try:
                fresh["lists"][label] = scrape_list(page, base_url, label)
            except Exception as e:
                print(f"[{label}] ! exception: {e}", flush=True)
                fresh["lists"][label] = []
 
            if i < len(list_items) - 1:
                print(f"--- Pausing {DELAY_BETWEEN_LISTS_MS/1000:.0f}s before next list ---", flush=True)
                page.wait_for_timeout(DELAY_BETWEEN_LISTS_MS)
 
        browser.close()
 
    failures = []
    for label, channels in fresh["lists"].items():
        ok, reason = is_valid(channels)
        if not ok:
            failures.append(f"{label}: {reason}")
 
    if failures:
        print("\n! Validation failed:", flush=True)
        for f in failures:
            print(f"   - {f}", flush=True)
        if OUTPUT_PATH.exists():
            print("Keeping previous subscribers.json untouched.", flush=True)
            return 1
        else:
            print("No previous file exists; writing what we have anyway.", flush=True)
 
    OUTPUT_PATH.write_text(json.dumps(fresh, indent=2, ensure_ascii=False))
    total = sum(len(v) for v in fresh["lists"].values())
    print(
        f"\nDone. Wrote {total} total rows ("
        f"{len(fresh['lists'].get('active', []))} active + "
        f"{len(fresh['lists'].get('all_time', []))} all-time) to {OUTPUT_PATH}",
        flush=True,
    )
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
