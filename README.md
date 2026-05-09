# TwitchTracker Subscribers → StreamerPlus

Scrapes the top 100 channels by **active** subscribers AND **all-time** subscribers from TwitchTracker, once a day. Renders a styled, interactive leaderboard on streamerplus.com via a WordPress shortcode.

## What it does

- Scrapes both `/subscribers` and `/subscribers/all-time` daily via GitHub Actions
- Captures full column data per channel: rank, rank-change, name, slug, avatar, active subs, paid, prime, gifted, tier 1, tier 2, tier 3
- Validates each scrape before saving (refuses to overwrite good data with bad)
- Filters out injected ad rows
- Handles rank arrows (▲ 5, ▲▲▲ huge jumps, ▼ 2)
- Shows "—" for unavailable values (when channels show "?")
- WordPress plugin includes: tab toggle (Active / All-time), live search, responsive layout, top-3 medals

## Setup

See `setup-guide.docx` for the full step-by-step. Short version:

1. Create a free GitHub account, create a public repo
2. Upload these files preserving folder structure:
   ```
   scraper/scrape.py
   scraper/requirements.txt
   .github/workflows/scrape.yml
   ```
3. Run the workflow once manually from the Actions tab
4. Verify `data/subscribers.json` appears in your repo
5. Edit `wordpress-plugin/sp-top-subscribers.php` and replace `YOURUSER/YOURREPO` with your raw JSON URL
6. Zip and upload the plugin to WordPress
7. Drop `[sp_top_subscribers]` on any page

## Shortcode options

```
[sp_top_subscribers]                       Default: top 20, with hero
[sp_top_subscribers limit="50"]            Show top 50
[sp_top_subscribers limit="100" hero="false"]   Top 100, no hero block
```

## Local testing

```bash
cd scraper
pip install -r requirements.txt
python -m playwright install chromium
python scrape.py
```

Output: `../data/subscribers.json`. If selectors break, you'll get `debug_active.png` / `debug_all_time.png`.

## Reliability features

- **Validation gate**: scraper requires >= 50 rows per list and >= 1000 subs on the top channel before writing. If validation fails, the previous good JSON is left untouched.
- **WordPress fallback**: shows a clean "data unavailable" message rather than a broken page if the JSON ever can't be reached.
- **Defensive parsing**: all numeric fields are nullable and render as "—" when missing.
- **Cache**: WP plugin caches the JSON for 1 hour via transients to keep page loads fast.

## Notes

- TwitchTracker's ToS doesn't formally permit scraping. Once-a-day frequency is low impact, but you're doing this at your own risk.
- If TwitchTracker changes their HTML, the validation gate prevents bad data going live. The scraper saves a debug screenshot to make selector fixes quick.
