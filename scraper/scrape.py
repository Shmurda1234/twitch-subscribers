"""
TwitchTracker subscribers scraper for StreamerPlus.
 
Scrapes:
  - https://twitchtracker.com/subscribers          (active subs, last 30 days)
  - https://twitchtracker.com/subscribers/all-time (all-time totals)
 
Pulls top 100 channels from each page with full column breakdown:
  rank, diff, name, slug, avatar, subs, paid, prime, gifted, t1, t2, t3.
 
Defensive design:
  - Filters out ad rows injected into the tbody
  - Handles "?" as null (data unavailable)
  - Decodes the magic "huge jump" arrow stack as diff = 999
  - Validates the scrape (>= 50 rows, top channel has subs > 0) before
    overwriting subscribers.json. If validation fails, the previous good
    file is left in place so the WordPress site never goes blank.
"""
 
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
 
from playwright.sync_api import Page, sync_playwright
 
# Both pages, output keyed by these labels
PAGES = {
    "active":   "https://twitchtracker.com/subscribers",
    "all_time": "https://twitchtracker.com/subscribers/all-time",
}
 
TOP_N = 100  # how many rows to keep per page
 
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"
 
# Validation thresholds — if a fresh scrape fails these, we keep the old file
MIN_ROWS_FOR_VALID = 50          # at least this many channels per list
MIN_TOP_SUBS_FOR_VALID = 1000    # top channel must have at least this many subs
 
 
def parse_int(text):
    """'12,345' -> 12345. '?', '', '—' -> None."""
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
    """
    The diff cell can be:
      - empty (no change, often rank #1 has nothing)              -> 0
      - <span class="diff text-success">▲ 5</span>                -> +5
      - <span class="diff text-danger">▼ 2</span>                 -> -2
      - <span class="diff text-success">▲ ▲ ▲</span>              -> 999 (huge jump)
    """
    span = cell.query_selector("span.diff")
    if not span:
        return 0
 
    classes = span.get_attribute("class") or ""
    text = span.inner_text().strip()
 
    # Multi-arrow magic value (matches design's `diff: 999`)
    arrows = span.query_selector_all("i.fas.fa-caret-up, i.fas.fa-caret-down")
    if len(arrows) >= 2 and not re.search(r"\d", text):
        return 999 if "text-success" in classes else -999
 
    # Pull the number out of the text
    num_match = re.search(r"\d+", text)
    if not num_match:
        return 0
    n = int(num_match.group(0))
 
    if "text-danger" in classes:
        return -n
    return n
 
 
def parse_rank(text):
    """'#1' -> 1, '#42' -> 42."""
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None
 
 
def slug_from_href(href):
    """'/jynxzi/subscribers' -> 'jynxzi'."""
    if not href:
        return None
    parts = [p for p in href.strip("/").split("/") if p]
    return parts[0] if parts else None
 
 
def scrape_page(page: Page, url: str, label: str) -> list[dict]:
    print(f"[{label}] Loading {url} ...", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
 
    # Wait for the table to actually have rows. TwitchTracker is mostly server-rendered,
    # so this is usually instant — but Cloudflare may add a small interstitial.
    try:
        page.wait_for_selector("table#channels tbody tr", timeout=30_000)
    except Exception:
        debug = Path(__file__).resolve().parent / f"debug_{label}.png"
        page.screenshot(path=str(debug), full_page=True)
        print(f"[{label}] ! Could not find table. Screenshot: {debug}", flush=True)
        return []
 
    rows = page.query_selector_all("table#channels tbody tr")
    print(f"[{label}] Found {len(rows)} <tr> elements (may include ad rows)", flush=True)
 
    results = []
    for row in rows:
        cells = row.query_selector_all("td")
 
        # Filter out ad rows: <tr><td colspan="11">...ads...</td></tr>
        if len(cells) <= 1:
            continue
        first_colspan = cells[0].get_attribute("colspan")
        if first_colspan and int(first_colspan) > 1:
            continue
        # Real data rows have 11 cells exactly
        if len(cells) < 11:
            continue
 
        # td[0] rank, td[1] diff, td[2] avatar+link, td[3] name+link,
        # td[4] active subs, td[5] paid, td[6] prime, td[7] gifted,
        # td[8] tier1, td[9] tier2, td[10] tier3
        rank = parse_rank(cells[0].inner_text())
        diff = parse_diff(cells[1])
 
        # Avatar + slug
        avatar_url = None
        slug = None
        avatar_link = cells[2].query_selector("a")
        if avatar_link:
            slug = slug_from_href(avatar_link.get_attribute("href"))
            img = avatar_link.query_selector("img")
            if img:
                avatar_url = img.get_attribute("src")
 
        # Name (display name, e.g. "KaiCenat" not "kaicenat")
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
 
        # Skip rows with no rank / no name (very defensive)
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
 
        if len(results) >= TOP_N:
            break
 
    print(f"[{label}] Parsed {len(results)} channel rows", flush=True)
    return results
 
 
def is_valid(channels: list[dict]) -> tuple[bool, str]:
    """Return (ok, reason). Used to decide whether to overwrite the JSON."""
    if len(channels) < MIN_ROWS_FOR_VALID:
        return False, f"only {len(channels)} rows (need >= {MIN_ROWS_FOR_VALID})"
    top_subs = channels[0].get("subs") or 0
    if top_subs < MIN_TOP_SUBS_FOR_VALID:
        return False, f"top channel has {top_subs} subs (need >= {MIN_TOP_SUBS_FOR_VALID})"
    return True, "ok"
 
 
def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
 
    fresh = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": "twitchtracker.com",
        "lists": {},  # active / all_time -> [channels]
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
 
        for label, url in PAGES.items():
            try:
                fresh["lists"][label] = scrape_page(page, url, label)
            except Exception as e:
                print(f"[{label}] ! Exception: {e}", flush=True)
                fresh["lists"][label] = []
 
        browser.close()
 
    # ---- Validation: refuse to overwrite a good JSON with a bad one ----
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
    print(f"\nDone. Wrote {total} total rows ({len(fresh['lists']['active'])} active + "
          f"{len(fresh['lists']['all_time'])} all-time) to {OUTPUT_PATH}", flush=True)
 
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
