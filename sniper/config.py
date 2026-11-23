"""Every tunable number for Project SNIPER lives here."""

# --- TCGCSV feed -----------------------------------------------------------
CATEGORY_ID = 3  # Pokémon (English) on TCGplayer/TCGCSV
BASE_API = "https://tcgcsv.com/tcgplayer"
ARCHIVE_URL_TEMPLATE = "https://tcgcsv.com/archive/tcgplayer/prices-{date}.ppmd.7z"
# When TCGCSV last refreshed its data (one ISO timestamp, ~20:00 UTC daily).
# Live fetches are dated by this, not by the clock: a fetch at 04:02 UTC on
# 2026-11-21 returned exactly the 2026-11-20 archive's prices.
LAST_UPDATED_URL = "https://tcgcsv.com/last-updated.txt"
USER_AGENT = "PokemonSniper/1.0 (paper-trading bot; github.com/Burgos0/pokemon-sniper)"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 1
RATE_LIMIT_RETRY_DELAY = 5
TCGCSV_REQUEST_INTERVAL = 0.75  # seconds between live API requests
MAX_REQUEST_DELAY = 8.0

# --- Paths -----------------------------------------------------------------
PRICES_DB_PATH = "data/prices.db"
ARCHIVE_CACHE_DIR = "data/archive_cache"  # git-ignored; every archive kept here

# --- Storage ---------------------------------------------------------------
# Only price rows with market_price >= this are stored (per row, per day, so
# no lookahead). Storing every Pokémon row would make prices.db ~3-4 GB, too
# big for the daily snapshot cycle; this keeps it ~0.6 GB. It is 25% of the
# tradeable floor, so every row the trading rules can look at is kept. The
# raw archives stay in ARCHIVE_CACHE_DIR, so this can be lowered later by
# reloading from the cache with no re-downloads.
STORE_MIN_MARKET_PRICE = 5.00

# --- Daily row-count floors ------------------------------------------------
# Validated against ALL category-3 rows in the source (before the storage
# floor). The first live fetch (observation date 2026-11-21 UTC) returned
# 45,809 rows across 222 sets; this is 85% of that.
MIN_EXPECTED_DAILY_ROWS = 38_937
# Backfill uses a date-aware floor: 85% of the expected count, linearly
# interpolated between these observed archive counts.
BACKFILL_ROW_ANCHORS = (
    ("2024-02-08", 37_234),
    ("2025-05-01", 40_834),
    ("2026-11-19", 45_793),
)
BACKFILL_FLOOR_RATIO = 0.85

# --- Backfill --------------------------------------------------------------
BACKFILL_START_DATE = "2024-02-08"  # first date TCGCSV's FAQ documents
# Seconds between archive downloads. 3s triggered HTTP 429 after ~66
# downloads on 2026-11-20, so this is deliberately slow (~3 h for 1,000 days).
ARCHIVE_DOWNLOAD_INTERVAL = 10.0
# On HTTP 429: wait Retry-After if given, else back off from the first value,
# doubling up to the cap, then retry the same date. Stop the run after this
# many rate limits in a row.
RATE_LIMIT_BACKOFF_START = 120.0
RATE_LIMIT_BACKOFF_MAX = 1800.0
RATE_LIMIT_MAX_CONSECUTIVE = 6
# A 404 for a date older than this many days is recorded as missing and not
# re-requested unless --retry-missing is passed. Newer dates may simply not
# be published yet.
ARCHIVE_MISSING_GRACE_DAYS = 3

# --- Spike confirmation (copied from the Yu-Gi-Oh detect_spike) ------------
SPIKE_LATEST_VS_MEDIAN_MAX = 1.5  # latest raw > 1.5x recent 3-day median = spike
SPIKE_ELEVATED_RATIO = 1.10       # "elevated" = >= 10% above baseline
REVERSION_TOLERANCE = 1.10        # latest price within +10% of baseline = move already reverted
FLOOR_RISE_MIN = 1.15             # cheapest listing must rise >=15%...
FLOOR_SUPPORT_RATIO = 0.80        # ...or already sit within 20% of the new market price
MIN_PRICE_CHANGES = 2             # market price must move on 2+ separate days (not one sale)
MAX_LISTING_TO_MARKET = 2.0       # cheapest listing > 2x market = thin market, price is stale/erratic

# --- Paper trading (milestone 4) -------------------------------------------
SNIPER_DB_PATH = "data/sniper.db"      # live ledger
BACKTEST_DB_PATH = "data/backtest.db"  # backtest ledgers; never the live one
STARTING_BANKROLL = 1000.00
MAX_POSITION_FRACTION = 0.20  # of the starting bankroll per position ($200)
MAX_OPEN_POSITIONS = 10
MAX_POSITIONS_PER_SET = 2
# One copy per card. A "card" is a (product_id, printing) identity, the same
# identity used everywhere else, so a Holofoil and a Reverse Holofoil of one
# product are two cards.

# --- Calendar windows ------------------------------------------------------
# A window of N calendar days is valid only if prices exist on at least this
# share of its days (rounded up). A rule with an invalid window does not fire.
WINDOW_MIN_COVERAGE = 0.80
# Valuation only (open positions and benchmark): holdings are valued at their
# last known market price; one older than this many days is flagged stale.
# Thin cards can go weeks without a price, so stale is never valued at $0.
VALUATION_CARRY_DAYS = 7

# --- Universe --------------------------------------------------------------
UNIVERSE_MIN_PRICE = 20.00
UNIVERSE_MAX_PRICE = 200.00
UNIVERSE_MIN_SET_AGE_DAYS = 90
UNIVERSE_RECENT_DAYS = 12        # priced on at least...
UNIVERSE_RECENT_MIN_PRICED = 9   # ...9 of the last 12 days (the owner's rule, not 80%)
UNIVERSE_CHANGE_DAYS = 30        # market price changed on at least...
UNIVERSE_MIN_CHANGES = 2         # ...2 days in the last 30

# --- Rule B: sealed lead ---------------------------------------------------
B_BOX_LOOKBACK_DAYS = 30
B_BOX_MIN_RISE = 0.10
B_TOP_SINGLES = 5
B_MEDIAN_DAYS = 3          # 3-day median of the single...
B_MEDIAN_OFFSET_DAYS = 7   # ...above its 3-day median from 7 days earlier
# Box choice per set: sealed products with live metadata whose name contains
# the first phrase and none of the exclusions; else the fallback. Shortest
# name wins (so "X Booster Box" beats "X Half Booster Box"), then lowest
# product_id. "Code Card" products are digital codes, not boxes.
B_BOX_NAME = "Booster Box"
B_BOX_EXCLUDE = ("Case", "Code Card")
B_FALLBACK_NAME = "Elite Trainer Box"
B_FALLBACK_EXCLUDE = ("Pokemon Center", "Pokémon Center", "Code Card")

# --- Rule C: bottoming -----------------------------------------------------
C_HIGH_DAYS = 60          # market price is >= 20% below its 60-day high...
C_MIN_DRAWDOWN = 0.20
C_FLAT_DAYS = 14          # ...stayed within +/-5% of its median for 14 days...
C_FLAT_BAND = 0.05
C_RISE_DAYS = 3           # ...then rose >= 5% over the next 3 days
C_MIN_RISE = 0.05

# When signals exceed cash or slots: B, then C, then higher market price.
# Rule A (floor squeeze) was removed on 2026-11-22: it only "won" by buying at
# a market price below every listing, which no real buyer could get.
RULE_PRIORITY = ("B", "C")

# --- Exits -----------------------------------------------------------------
TAKE_PROFIT = 0.40
TRAIL_ACTIVATE = 0.20   # once the peak is +20% over the buy price...
TRAIL_DROP = 0.15       # ...sell if the price falls 15% from the peak
HARD_STOP = -0.20
TIME_STOP_DAYS = 90

# --- Benchmark -------------------------------------------------------------
BENCHMARK_SIZE = 20  # equal-weight, fractional shares, never traded

# --- Backtest (milestone 5) ------------------------------------------------
# Trading starts this many days into the price history, so the longest rule
# window (60 days) can be valid on the first trading day.
BACKTEST_WARMUP_DAYS = 60

# --- Dashboard (milestone 6) -----------------------------------------------
SITE_DIR = "site"
# Backtest results for the frozen rules, committed so the daily build can show
# them without re-running the backtest.
BACKTEST_FROZEN_PATH = "data/backtest_frozen.json"
REPO_URL = "https://github.com/Burgos0/pokemon-sniper"
TCGPLAYER_PRODUCT_URL = "https://www.tcgplayer.com/product/{product_id}"
