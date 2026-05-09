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
  - Polite delay between pages (avoid hammering)
  - Validates total scrape (>= 50 rows per list, top channel has subs > 0)
    before overwriting subscribers.json
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# Each list is paginated; we fetch pages 1 through PAGES_PER_LIST.
PAGES_PER_LIST = 5  # 5 pages × 20 channels = 100 per list
DELAY_BETWEEN_PAGES_MS = 2000  # 2s polite delay so we don't look like a bot

# Base URLs (without page suffix)
LISTS = {
    "active":   "https://twitchtracker.com/subscribers",
    "all_time": "https://twitchtracker.com/subscribers/all-time",
}

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"

# Validation thresholds
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
    """page 1 has no suffix; pages 2..N use ?page=N"""
    if page_num <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page_num}"


def scrape_one_page(page: Page, url: str, label: str, page_num: int) -> list[dict]:
    """Fetch and parse a single TwitchTracker page (~20 rows)."""
    print(f"[{label}] page {page_num}: {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("table#channels tbody tr", timeout=30_000)
    except Exception:
        debug = Path(__file__).resolve().parent / f"debug_{label}_p{page_num}.png"
        page.screenshot(path=str(debug), full_page=True)
        print(f"[{label}] page {page_num}: ! No table found. Screenshot: {debug}", flush=True)
        return []

    rows = page.query_selector_all("table#channels tbody tr")
    results = []
    for row in rows:
        cells = row.query_selector_all("td")
        # Skip ad rows / invalid rows
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
    return results


def scrape_list(page: Page, base_url: str, label: str) -> list[dict]:
    """Loop pages 1..PAGES_PER_LIST and concatenate results."""
    all_rows = []
    seen_ranks = set()  # de-dup safety net in case TwitchTracker repeats rows on the last page

    for page_num in range(1, PAGES_PER_LIST + 1):
        url = page_url(base_url, page_num)
        try:
            rows = scrape_one_page(page, url, label, page_num)
        except Exception as e:
            print(f"[{label}] page {page_num}: ! exception: {e}", flush=True)
            rows = []

        if not rows:
            # Empty page — likely we've gone past the end. Stop early.
            print(f"[{label}] page {page_num} returned 0 rows; stopping early.", flush=True)
            break

        for r in rows:
            if r["rank"] in seen_ranks:
                continue
            seen_ranks.add(r["rank"])
            all_rows.append(r)

        # Polite delay before next page
        if page_num < PAGES_PER_LIST:
            page.wait_for_timeout(DELAY_BETWEEN_PAGES_MS)

    print(f"[{label}] TOTAL: {len(all_rows)} unique channels across all pages", flush=True)
    return all_rows


def is_valid(channels: list[dict]) -> tuple[bool, str]:
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

        for label, base_url in LISTS.items():
            try:
                fresh["lists"][label] = scrape_list(page, base_url, label)
            except Exception as e:
                print(f"[{label}] ! exception: {e}", flush=True)
                fresh["lists"][label] = []

        browser.close()

    # Validate
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
