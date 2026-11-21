"""Every tunable number for Project SNIPER lives here."""

# --- TCGCSV feed -----------------------------------------------------------
CATEGORY_ID = 3  # Pokémon (English) on TCGplayer/TCGCSV
BASE_API = "https://tcgcsv.com/tcgplayer"
ARCHIVE_URL_TEMPLATE = "https://tcgcsv.com/archive/tcgplayer/prices-{date}.ppmd.7z"
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
ARCHIVE_DOWNLOAD_INTERVAL = 3.0  # seconds between archive downloads
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
