# Project SNIPER — build spec (v1)

A Pokémon card paper-trading bot. It trades a virtual bankroll on real daily
TCGplayer prices, never buys anything real, and publishes everything to one
dashboard page. It is for entertainment/content, so keep it simple and
explainable: fixed rules, no ML.

Work through the milestones in order. Stop and report at each **STOP** before
continuing.

---

## 0. Ground rules

- New public repo: `pokemon-sniper`. Do NOT modify `Yugioh-Prices-Fetcher-2`
  or the live tcgtrending.com site in any way. Copy files out of it only.
- Python 3.11, `requests`, `pandas`, SQLite. No new heavy dependencies.
- Every tunable number lives in one file, `sniper/config.py`.
- Never use a price dated after the day being simulated (no lookahead). Every
  function that reads prices takes an `as_of` date, same pattern as
  `calculate_early_mover_candidates` in the Yu-Gi-Oh repo.

## 1. Copy from `Burgos0/Yugioh-Prices-Fetcher-2`

Copy and adapt:

- `scripts/fetch_prices.py` (TCGCSV client, archive download/extract, DB init)
- `scripts/snapshot_storage.py`, `restore_snapshot.py`, `publish_snapshot.py`,
  `bootstrap_snapshot_storage.py` (DB stored as GitHub Release assets)
- `app/subtype_policy.py`
- From `app/analysis.py`: `_select_weekly_mover_windows`, `detect_spike`, the
  `printing_prices` loaders, `evaluate_horizon_outcome`
- `.github/workflows/daily.yml` as the starting workflow
- The matching tests

Do NOT copy: `meta_watch`, `tournament_adoption`, `evidence`,
`candidate_score`, `candidate_evaluation`, anything YGOPRODeck/TopDeck/Konami,
`social/`, the Flask app and templates.

## 2. Pokémon changes

- `CATEGORY_ID = 3` (Pokémon, English). Remove every hardcoded `2`, including
  the default in `fetch_group_release_dates`.
- Rename user agent and release notes text (no "Yu-Gi-Oh").
- `MIN_EXPECTED_DAILY_ROWS`: run one live fetch, then set it to ~85% of the
  observed row count (the 2026-11-19 archive had 45,793 Pokémon rows). The
  backfill needs a lower, date-aware floor: early 2024 days have ~37,000.
- **Sealed detection must change.** The old rule (`printing == "Normal"` means
  sealed) is wrong for Pokémon, where Normal is a regular singles printing.
  Use the TCGCSV products endpoint: a product whose `extendedData` has a card
  `Number` is a single; otherwise treat it as sealed. Store `is_sealed`,
  `card_number`, and `rarity` in a `products` table. Verify the field names
  against real data before relying on them.
- `PREFERRED_SUBTYPE = "1st Edition"` is irrelevant. Use `printing_prices`
  with `(product_id, printing)` as the identity everywhere. Because of this,
  the legacy one-printing-per-product `prices` table and
  `app/subtype_policy.py` (which only chose that one printing) were not
  carried over.

### STOP 1 — verify the price feed

Fetch product `94168` (M Gengar EX, XY – Phantom Forces, Holofoil) from TCGCSV
and print `lowPrice`, `midPrice`, `marketPrice` for each subtype.

On 2026-11-20 the TCGplayer page showed: Near Mint market price $167.12,
Damaged listings from $36.35. (This is the owner's own reading of the page;
it was not verified by tooling.)

- If `marketPrice` is near $167: good, continue.
- If `marketPrice` is near $36: the feed's market price is not near-mint
  based. Stop and report; the trading rules need rethinking.

**Already checked on 2026-11-20 against the 2026-11-19 archive: PASSED.**
Product 94168 Holofoil: low 94.59, mid 143.82, market 167.12 (verified from
the archive). That market price matches the owner's reading of the page's
Near Mint figure. Still do this step, but
only to confirm the product's name and set from the products endpoint, then
continue without waiting.

## 3. History backfill

- `scripts/backfill_archive.py` loads the FULL history, from 2024-02-08 (the
  first date TCGCSV's FAQ documents; not verified as the earliest, and
  earlier dates must not be probed) to yesterday, using the existing
  `download_archive` / `extract_archive` functions, keeping only category 3.
- Verified on 2026-11-20: archives for 2024-02-08, 2025-05-01 and 2026-11-19
  all exist (2 to 4 MB each) and unpack to `<date>/3/<groupId>/prices`, with
  37,234 / 40,834 / 45,793 Pokémon price rows.
- TCGCSV asks that each archive file be downloaded once only. Keep every
  downloaded file in a local cache folder (git-ignored) and never re-download
  a date that is cached or already in the DB. Pace the downloads politely.
- The backfill is resumable: it can be stopped at any point and re-run (for
  example overnight), picking up where it left off.
- Report every date in the range that has no archive or no Pokémon rows.
- Archives hold prices only (IDs, no names). Card names, set names, release
  dates, rarity and `is_sealed` come from the live groups/products endpoints.
  Report how many archived product IDs have no live metadata.
- Live fetches are dated by TCGCSV's last-updated timestamp
  (`https://tcgcsv.com/last-updated.txt`, UTC date), not the clock. The
  first live fetch, run at 04:02 UTC on 2026-11-21, matched the 2026-11-20
  archive row for row, so clock dating was one day ahead. A fetch that sees
  the timestamp change mid-run writes nothing.
- Never use `highPrice` for anything: it has extreme outliers. It is not
  stored in the DB.
- Storage floor (decided 2026-11-20): only price rows with
  `market_price >= $5` (`STORE_MIN_MARKET_PRICE`) are stored, per row and per
  day. Storing every row would make `prices.db` about 3-4 GB, too big for the
  daily snapshot cycle (GitHub release assets cap at 2 GB each). With the
  floor it is about 0.6 GB raw, 0.2 GB gzipped. $5 is 25% of the $20
  tradeable floor, so every row the rules look at is kept. The cached
  archives keep full-fidelity data, so the floor can be lowered later by
  reloading from the cache with no new downloads. Row-count floors check
  all category-3 rows in the source, before this filter.
- Run locally, then publish the DB with the snapshot scripts.

### STOP 2 — report row counts per day and the date range loaded.

## 4. Paper-trading engine (`sniper/`)

### Money and execution

- Starting bankroll: **$1,000** (config).
- Buys and sells both execute at that day's **market price**. No fees, tax, or
  shipping in v1.
- Log every trade raw (date, product_id, printing, card, set, side, price,
  rule, reason) so fees can be applied retroactively later.
- Limits: 1 copy per card, max 20% of starting bankroll ($200) per position,
  max 10 open positions, cash can never go negative.
- One trading step per date, idempotent: re-running a date changes nothing.

### Calendar and missing days

- All windows (7, 12, 14, 30, 60, 90 days, etc.) are calendar days.
- A window is valid only if at least 80% of its days have a price. A rule
  whose window is invalid does not fire.
- No trades on a day with no fetch.
- For valuing open positions and the benchmark only, carry the last price
  forward up to 7 days. Never carry prices forward for signals.

### Tables (`data/sniper.db`)

`trades`, `positions`, `equity_history` (date, cash, positions_value, total,
benchmark_total), `signals` (every signal fired, whether it was bought, and
why not if skipped).

### Universe filter

A card is tradeable only if all are true:

- Has live metadata (a row in `products`). Product IDs that appear only in
  archives, with no live name, set or sealed flag, are excluded.
- Single, not sealed
- Market price between $20 and $200
- Its set released ≥ 90 days ago
- Prices on at least 9 of the last 12 calendar days up to `as_of`
- Market price changed on at least 2 days in the last 30 (not a dead listing)

### Low price

`low_price` can come from worse-condition copies (the Gengar's low was 94.59
against a 167.12 market price). It is never an execution price: every trade
executes at that day's market price.

### Entry rules (each trade records which rule fired)

- **A. Floor squeeze:** `low_price >= market_price` for 2 consecutive
  days, and market price moved less than 10% over the last 7 days.
- **B. Sealed lead:** the set's box market price is up ≥ 10% over 30
  days. Then its 5 most expensive singles are eligible; buy one when its
  3-day median is above its median from 7 days earlier.
  - The set's box is the sealed product whose name contains "Booster Box"
    (excluding names containing "Case"). If the set has none, fall back to
    the sealed product whose name contains "Elite Trainer Box" (excluding
    "Pokemon Center"). If neither exists, rule B skips that set.
    Only products with live metadata are candidates (the name is needed).
  - Log which product was chosen for each set.
- **C. Bottoming:** market price is ≥ 20% below its 60-day high, stayed within
  ±5% for 14 days, then rose ≥ 5% over 3 days.

If more signals fire than cash allows, rank A, then B, then C, then by
higher price.

### Exit rules

- Take profit at +40%
- Trailing stop: once up 20%, sell if it falls 15% from its peak
- Hard stop at −20%
- Time stop: sell after 90 days

### Benchmark

Buy-and-hold equal-weight index: on the first day of the window, split
$1,000 equally across the 20 most expensive cards in the tradeable universe
($20 to $200), with fractional shares allowed, and never trade. Bot and
benchmark draw from the same pool. Track its value daily.

Every evaluation window (first half, second half, each calendar year, and
live) starts fresh on its own first day with $1,000 for both the bot and the
benchmark.

## 5. Backtest

- `scripts/backtest.py` replays the engine day by day over the backfilled
  history using `as_of`.
- Report per rule: trades, win rate, average and median return, average hold,
  and total return vs the benchmark.
- Tune thresholds only on the first half of the history, then run once on the
  second half and report both. Do not re-tune on the second half.
- The whole market rose sharply in this period (median market price up about
  1.78x from May 2025 to Nov 2026). Judge every rule by its return RELATIVE
  to the buy-and-hold benchmark, never by its raw return, and report results
  separately for 2024, 2025 and 2026.

### STOP 3 — show the backtest report and wait for go-ahead.

Backtest trades never enter the live ledger. The live track record starts at
$1,000 on go-live day with the rules frozen.

## 6. Dashboard (the one page with everything)

`scripts/build_dashboard.py` writes `site/index.html` plus
`site/dashboard.json`. Static, no server, deployed to GitHub Pages by the
daily workflow. Must read well on a phone. Chart.js from a CDN is fine.

Sections, top to bottom:

1. **Header:** total value, cash, total return %, benchmark return %, days
   live, last update date. Visible label: "Paper trading. Returns before
   fees."
2. **Equity curve:** bot vs benchmark, daily.
3. **Open positions:** card, set, printing, buy date, buy price, current
   price, % change, days held, entry rule, link to
   `https://www.tcgplayer.com/product/{product_id}`.
4. **Closed trades:** same columns plus sell date, sell price, exit reason.
5. **Today's signals:** every signal, bought or skipped, with the reason.
6. **Rule scoreboard:** per rule A/B/C: trades, win rate, average return.
7. **Data health:** last fetch date, rows fetched, any failed sets.

## 7. Daily workflow

restore DB snapshot → fetch prices → run trading step for today → publish DB
snapshot → commit `dashboard.json` → build and deploy the Pages site.

Keep the original safeguards: serialized runs, publish the snapshot even if
the trading step fails, never deploy a stale page, fail the run loudly if any
step fails.

## 8. Tests (minimum)

- No lookahead: results for date D are identical whether or not later dates
  exist in the DB.
- Trading step is idempotent.
- Cash never negative; position limits enforced.
- Every trade executes at that day's market price (never low, mid or high).
- Sealed products never enter the singles universe.

## 9. Out of scope for v1

eBay/Mercari, fees and shipping, tournament data, ML, Japanese prices
(TCGCSV category 85), social post generation, anything Yu-Gi-Oh.
