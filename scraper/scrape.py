"""
TwitchTracker subscribers scraper — paginated to fetch 100 channels per list.
 
Scrapes:
  - https://twitchtracker.com/subscribers              page 1..5  (active, last 30 days)
  - https://twitchtracker.com/subscribers/all-time     page 1..5  (all-time)
 
Each TwitchTracker page shows 20 channels. We hit pages 1 through 5 to get
the top 100 per list.
 
Defensive features:
  - Filters out ad rows
  - Handles "?" cells as null
  - Decodes "▲▲▲" multi-arrow as diff = 999
  - Polite delays between pages and a longer pause between lists
  - Validates total scrape (>= 50 rows per list, top channel has subs > 0)
    before overwriting subscribers.json
  - Prints diagnostic info if a page returns 0 rows
"""
 
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
 
from playwright.sync_api import Page, sync_playwright
 
PAGES_PER_LIST = 5
DELAY_BETWEEN_PAGES_MS = 3000   # 3s between pages within a list
DELAY_BETWEEN_LISTS_MS = 8000   # 8s between switching from active -> all-time
 
LISTS = {
    "active":   "https://twitchtracker.com/subscribers",
    "all_time": "https://twitchtracker.com/subscribers/all-time",
}
 
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"
 
MIN_ROWS_FOR_VALID = 50
MIN_TOP_SUBS_FOR_VALID = 1000
 
 
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
 
 
def parse_diff(cell):
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
 
 
def diagnose_empty_page(page: Page, label: str, page_num: int):
    """When a page returns 0 rows, capture useful info to figure out why."""
    try:
        title = page.title()
        url   = page.url
        body_text = page.evaluate("document.body.innerText")[:300] if page.evaluate("!!document.body") else "(no body)"
        # Count tables and rows on the page
        n_tables = len(page.query_selector_all("table"))
        n_channels_table = len(page.query_selector_all("table#channels"))
        n_any_rows = len(page.query_selector_all("tbody tr"))
        # Check for Cloudflare interstitial
        has_cf = "cloudflare" in body_text.lower() or "challenge" in body_text.lower() or "checking your browser" in body_text.lower()
        print(f"   [diag] title={title!r}", flush=True)
        print(f"   [diag] url={url!r}", flush=True)
        print(f"   [diag] n_tables={n_tables}, n_channels_table={n_channels_table}, n_any_rows={n_any_rows}", flush=True)
        print(f"   [diag] cloudflare_indicators={has_cf}", flush=True)
        print(f"   [diag] body_preview: {body_text!r}", flush=True)
 
        # Save screenshot
        debug = Path(__file__).resolve().parent / f"debug_{label}_p{page_num}.png"
        page.screenshot(path=str(debug), full_page=True)
        print(f"   [diag] screenshot saved: {debug}", flush=True)
    except Exception as e:
        print(f"   [diag] diagnostic itself failed: {e}", flush=True)
 
 
def scrape_one_page(page: Page, url: str, label: str, page_num: int) -> list[dict]:
    print(f"[{label}] page {page_num}: {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("table#channels tbody tr", timeout=30_000)
    except Exception:
        print(f"[{label}] page {page_num}: ! No #channels table found (timeout).", flush=True)
        diagnose_empty_page(page, label, page_num)
        return []
 
    rows = page.query_selector_all("table#channels tbody tr")
    results = []
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) <= 1:
            continue
        first_colspan = cells[0].get_attribute("colspan")
        if first_colspan and int(first_colspan) > 1:
            continue
        if len(cells) < 11:
            continue
 
        rank = parse_rank(cells[0].inner_text())
        diff = parse_diff(cells[1])
 
        avatar_url = None
        slug = None
        avatar_link = cells[2].query_selector("a")
        if avatar_link:
            slug = slug_from_href(avatar_link.get_attribute("href"))
            img = avatar_link.query_selector("img")
            if img:
                avatar_url = img.get_attribute("src")
 
        name_link = cells[3].query_selector("a")
        name = name_link.inner_text().strip() if name_link else cells[3].inner_text().strip()
        if not slug and name_link:
            slug = slug_from_href(name_link.get_attribute("href"))
 
        subs   = parse_int(cells[4].inner_text())
        paid   = parse_int(cells[5].inner_text())
        prime  = parse_int(cells[6].inner_text())
        gifted = parse_int(cells[7].inner_text())
        t1     = parse_int(cells[8].inner_text())
        t2     = parse_int(cells[9].inner_text())
        t3     = parse_int(cells[10].inner_text())
 
        if not rank or not name:
            continue
 
        results.append({
            "rank":   rank,
            "diff":   diff,
            "name":   name,
            "slug":   slug,
            "url":    f"https://twitchtracker.com/{slug}/subscribers" if slug else None,
            "avatar": avatar_url,
            "subs":   subs,
            "paid":   paid,
            "prime":  prime,
            "gifted": gifted,
            "t1":     t1,
            "t2":     t2,
            "t3":     t3,
        })
 
    print(f"[{label}] page {page_num}: parsed {len(results)} rows", flush=True)
    if not results:
        diagnose_empty_page(page, label, page_num)
    return results
 
 
def scrape_list(page: Page, base_url: str, label: str) -> list[dict]:
    all_rows = []
    seen_ranks = set()
 
    for page_num in range(1, PAGES_PER_LIST + 1):
        url = page_url(base_url, page_num)
        # Retry once if a page returns 0 rows — could be a transient block
        rows = scrape_one_page(page, url, label, page_num)
        if not rows and page_num == 1:
            print(f"[{label}] page 1 returned 0 rows; waiting 10s and retrying once...", flush=True)
            page.wait_for_timeout(10000)
            rows = scrape_one_page(page, url, label, page_num)
 
        if not rows:
            print(f"[{label}] page {page_num} empty; stopping pagination for this list.", flush=True)
            break
 
        for r in rows:
            if r["rank"] in seen_ranks:
                continue
            seen_ranks.add(r["rank"])
            all_rows.append(r)
 
        if page_num < PAGES_PER_LIST:
            page.wait_for_timeout(DELAY_BETWEEN_PAGES_MS)
 
    print(f"[{label}] TOTAL: {len(all_rows)} unique channels", flush=True)
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
 
            # Longer pause before switching to next list (avoid rate-limit)
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
