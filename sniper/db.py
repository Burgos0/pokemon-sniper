"""prices.db schema and the atomic per-day write used by live fetch and backfill.

Identity is always (product_id, printing). Names, sets, release dates and
sealed status live in `products` / `groups`, never in the price rows.
`high_price` is deliberately not stored: it has extreme outliers and the
spec forbids using it.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from sniper import config

FETCH_STATUS_OK = "ok"
FETCH_STATUS_REJECTED = "rejected"  # fetched, but too few rows to trust
FETCH_STATUS_MISSING = "missing"    # archive does not exist (HTTP 404)
FETCH_STATUS_ERROR = "error"        # download/extract/parse failed


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path=config.PRICES_DB_PATH):
    """Create the database directory and every table if missing."""
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS printing_prices (
        date TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        printing TEXT NOT NULL,
        low_price REAL,
        mid_price REAL,
        market_price REAL,
        direct_low_price REAL,
        PRIMARY KEY (date, product_id, printing)
    ) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS groups (
        group_id INTEGER PRIMARY KEY,
        name TEXT,
        abbreviation TEXT,
        release_date TEXT,
        is_supplemental INTEGER,
        modified_on TEXT,
        fetched_at TEXT
    );

    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY,
        group_id INTEGER,
        name TEXT,
        clean_name TEXT,
        card_number TEXT,
        rarity TEXT,
        is_sealed INTEGER NOT NULL,
        url TEXT,
        fetched_at TEXT
    );

    -- Every product ID seen in any price source, including rows below the
    -- storage floor, so metadata coverage can be reported.
    CREATE TABLE IF NOT EXISTS archive_products (
        product_id INTEGER PRIMARY KEY,
        group_id INTEGER,
        first_seen TEXT,
        last_seen TEXT
    );

    -- One row per date that was attempted. Only status 'ok' dates have
    -- prices; no trading happens on any other date.
    CREATE TABLE IF NOT EXISTS fetch_log (
        date TEXT PRIMARY KEY,
        source TEXT,
        status TEXT NOT NULL,
        rows_total INTEGER,
        rows_stored INTEGER,
        groups INTEGER,
        failed_groups TEXT,
        min_expected INTEGER,
        note TEXT,
        fetched_at TEXT
    );
    """)
    conn.commit()
    return conn


def build_price_rows(parsed_group_prices, date_str, min_market=config.STORE_MIN_MARKET_PRICE):
    """
    Turn {group_id: [raw TCGCSV price dicts]} into stored rows.

    Returns (rows, rows_total, seen) where rows_total counts every
    category-3 row in the source (what the row-count floors check), rows are
    only those with market_price >= min_market, and seen maps every
    product_id to its group_id.
    """
    rows = {}
    rows_total = 0
    seen = {}
    for group_id, items in parsed_group_prices.items():
        for item in items:
            product_id = item.get("productId")
            if product_id is None:
                continue
            rows_total += 1
            seen[product_id] = int(group_id)
            market = item.get("marketPrice")
            if market is None or market < min_market:
                continue
            printing = item.get("subTypeName") or "Unspecified"
            rows[(product_id, printing)] = (
                date_str, product_id, printing,
                item.get("lowPrice"), item.get("midPrice"), market,
                item.get("directLowPrice"),
            )
    return list(rows.values()), rows_total, seen


def write_day_atomically(db_path, date_str, rows, rows_total, seen, min_expected,
                         source, groups=0, failed_groups=()):
    """
    Replace one date's prices and log the fetch in a single transaction.
    Refuses (raises, writes nothing) if rows_total < min_expected.
    """
    if rows_total < min_expected:
        raise RuntimeError(
            f"Row count for {date_str} is {rows_total:,}; expected at least {min_expected:,}")
    conn = init_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        rows_before = cur.execute(
            "SELECT COUNT(*) FROM printing_prices WHERE date = ?", (date_str,)).fetchone()[0]
        cur.execute("DELETE FROM printing_prices WHERE date = ?", (date_str,))
        cur.executemany(
            "INSERT INTO printing_prices (date, product_id, printing, low_price, mid_price, "
            "market_price, direct_low_price) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        cur.executemany(
            """
            INSERT INTO archive_products (product_id, group_id, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                group_id = excluded.group_id,
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)
            """,
            [(pid, gid, date_str, date_str) for pid, gid in seen.items()])
        _log_fetch(cur, date_str, source, FETCH_STATUS_OK, rows_total, len(rows),
                   groups, failed_groups, min_expected, None)
        conn.commit()
        return rows_before, len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_fetch_failure(db_path, date_str, source, status, note, rows_total=None,
                      min_expected=None):
    """Record a non-ok attempt. Never overwrites an existing 'ok' date."""
    conn = init_db(db_path)
    try:
        existing = conn.execute(
            "SELECT status FROM fetch_log WHERE date = ?", (date_str,)).fetchone()
        if existing and existing[0] == FETCH_STATUS_OK:
            return
        _log_fetch(conn, date_str, source, status, rows_total, 0, 0, (), min_expected, note)
        conn.commit()
    finally:
        conn.close()


def _log_fetch(cur, date_str, source, status, rows_total, rows_stored, groups,
               failed_groups, min_expected, note):
    cur.execute(
        """
        INSERT OR REPLACE INTO fetch_log (date, source, status, rows_total, rows_stored,
            groups, failed_groups, min_expected, note, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (date_str, source, status, rows_total, rows_stored, groups,
         json.dumps(sorted(str(g) for g in failed_groups)), min_expected, note, utc_now_iso()))


def fetch_status(conn, date_str):
    row = conn.execute("SELECT status FROM fetch_log WHERE date = ?", (date_str,)).fetchone()
    return row[0] if row else None


def upsert_groups(conn, group_records):
    conn.executemany(
        """
        INSERT OR REPLACE INTO groups (group_id, name, abbreviation, release_date,
            is_supplemental, modified_on, fetched_at)
        VALUES (:group_id, :name, :abbreviation, :release_date, :is_supplemental,
            :modified_on, :fetched_at)
        """, group_records)


def upsert_products(conn, product_records):
    conn.executemany(
        """
        INSERT OR REPLACE INTO products (product_id, group_id, name, clean_name,
            card_number, rarity, is_sealed, url, fetched_at)
        VALUES (:product_id, :group_id, :name, :clean_name, :card_number, :rarity,
            :is_sealed, :url, :fetched_at)
        """, product_records)


def known_product_ids(conn):
    return {row[0] for row in conn.execute("SELECT product_id FROM products")}
