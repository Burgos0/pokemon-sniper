"""Pokémon daily price fetcher using the TCGCSV live API.

Adapted from Burgos0/Yugioh-Prices-Fetcher-2 scripts/fetch_prices.py.
"""
import sys
import os
import shutil
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
import requests

# Allow `python scripts/fetch_prices.py` (direct script, no repo root on
# sys.path) as well as `python -m scripts.fetch_prices`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper import config, db, metadata
from sniper.config import (
    ARCHIVE_URL_TEMPLATE,
    BASE_API,
    CATEGORY_ID,
    MAX_REQUEST_DELAY,
    MAX_RETRIES,
    RATE_LIMIT_RETRY_DELAY,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
    TCGCSV_REQUEST_INTERVAL,
    USER_AGENT,
)

DB_PATH = config.PRICES_DB_PATH
MIN_EXPECTED_DAILY_ROWS = config.MIN_EXPECTED_DAILY_ROWS


class ArchiveNotFound(RuntimeError):
    """The archive for a date does not exist (HTTP 404/403)."""


def current_utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_target_date(date_arg=None):
    """Return an explicit date or yesterday's UTC date for archive fetching."""
    if date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
        return parsed.strftime("%Y-%m-%d")
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def parse_observation_date(date_arg=None):
    """Capture the live observation date once, at the beginning of a run."""
    if date_arg:
        try:
            return datetime.strptime(date_arg, "%Y-%m-%d").date().strftime("%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
    return current_utc_date()


class TCGCSVClient:
    """Per-run client with pacing, bounded retries, and group-price caching."""

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.request_delay = TCGCSV_REQUEST_INTERVAL
        self._last_request_at = None
        self._group_price_cache = {}

    def _pace(self):
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_time = self.request_delay - (now - self._last_request_at)
            if wait_time > 0:
                time.sleep(wait_time)
                now += wait_time
        self._last_request_at = now

    def _increase_delay(self):
        self.request_delay = min(MAX_REQUEST_DELAY, max(
            self.request_delay * 2, TCGCSV_REQUEST_INTERVAL))

    def fetch_json(self, url):
        """Fetch JSON with bounded rate-limit and transient-error retries."""
        last_error = None
        rate_limit_retry_used = False

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as error:
                last_error = error
                retry_delay = RETRY_DELAY * (2 ** attempt)
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                        return data["results"] if isinstance(data, dict) and "results" in data else data
                    except ValueError as error:
                        last_error = error
                        retry_delay = RETRY_DELAY * (2 ** attempt)
                elif response.status_code in (401, 429):
                    last_error = requests.HTTPError(f"HTTP {response.status_code} (rate limited)")
                    self._increase_delay()
                    if rate_limit_retry_used:
                        break
                    rate_limit_retry_used = True
                    retry_delay = RATE_LIMIT_RETRY_DELAY
                elif response.status_code >= 500:
                    last_error = requests.HTTPError(f"HTTP {response.status_code} (retryable)")
                    retry_delay = RETRY_DELAY * (2 ** attempt)
                else:
                    last_error = requests.HTTPError(f"HTTP {response.status_code}")
                    break

            if attempt >= MAX_RETRIES - 1:
                break
            print(f"  Request failed ({last_error}). Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

        raise RuntimeError(f"Failed to fetch {url}: {last_error}")

    def fetch_group_prices(self, group_id):
        """Fetch one group's prices at most once during this run."""
        group_id = str(group_id)
        if group_id not in self._group_price_cache:
            data = self.fetch_json(f"{BASE_API}/{CATEGORY_ID}/{group_id}/prices")
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected prices response for group {group_id}")
            self._group_price_cache[group_id] = data
        return self._group_price_cache[group_id]


def fetch_json(url, client=None):
    """Compatibility wrapper for callers that fetch one TCGCSV JSON endpoint."""
    return (client or TCGCSVClient()).fetch_json(url)


def check_7z_available():
    if shutil.which("7z") is None:
        raise RuntimeError("The `7z` command is not available in this environment")


def download_archive(target_date_str, dest_path):
    """Download exactly one fixed-date archive, without retrying HTTP 401/403/404."""
    url = ARCHIVE_URL_TEMPLATE.format(date=target_date_str)
    print(f"Downloading daily archive from: {url}")
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT, stream=True)
            if response.status_code == 200:
                with open(dest_path, "wb") as archive_file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        archive_file.write(chunk)
                print(f"Archive downloaded successfully ({os.path.getsize(dest_path):,} bytes).")
                return
            last_error = f"HTTP {response.status_code}"
            if response.status_code in (403, 404):
                raise ArchiveNotFound(f"No archive for {target_date_str}: {last_error}")
            if response.status_code == 401:
                break
            if response.status_code != 429 and response.status_code < 500:
                break
        except requests.exceptions.RequestException as error:
            last_error = str(error)
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAY * (2 ** attempt)
            print(f"  Archive download failed ({last_error}). Retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"Failed to download archive for {target_date_str}: {last_error}")


def extract_archive(archive_path, extract_dir, include=None):
    """Extract an archive; `include` (e.g. "2024-02-08/3/*") limits what is unpacked."""
    cmd = ["7z", "x", archive_path, f"-o{extract_dir}", "-y"]
    if include:
        cmd += [include, "-r"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip extraction failed: {result.stderr or result.stdout}")


def find_category_dir(extract_dir, category_id=CATEGORY_ID):
    category = str(category_id)
    for root, dirs, files in os.walk(extract_dir):
        parts = os.path.relpath(root, extract_dir).split(os.sep)
        if parts == [category] or (len(parts) == 2 and parts[1] == category):
            return root
    raise RuntimeError(f"Category {category_id} directory not found in extracted archive")


def parse_archive_group_prices(cat_dir):
    """Parse every group's price file under an extracted category directory.

    Returns ({group_id: [price dicts]}, [group_ids whose file failed to parse]).
    """
    parsed_group_prices = {}
    failed_groups = []
    for gid in sorted(os.listdir(cat_dir)):
        gpath = os.path.join(cat_dir, gid)
        if not os.path.isdir(gpath):
            continue
        pfile = os.path.join(gpath, "prices")
        if not os.path.isfile(pfile):
            pfile = os.path.join(gpath, "prices.json")
        if not os.path.isfile(pfile):
            failed_groups.append(gid)
            continue
        try:
            with open(pfile, "r") as f:
                data = json.load(f)
            items = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("results is not a list")
            parsed_group_prices[gid] = items
        except Exception as e:
            failed_groups.append(gid)
            print(f"Warning: Failed to parse price file in group {gid}: {e}")
    return parsed_group_prices, failed_groups


def fetch_groups(client=None):
    """Fetch all Pokémon groups (sets) from the live API."""
    return fetch_json(f"{BASE_API}/{CATEGORY_ID}/groups", client)


def fetch_group_products(group_id, client=None):
    return fetch_json(f"{BASE_API}/{CATEGORY_ID}/{group_id}/products", client)


def refresh_metadata(db_path, client=None, group_ids=None):
    """
    Upsert all groups, then products for `group_ids` (all groups if None).
    Returns (groups_count, products_count, failed_group_ids).
    """
    client = client or TCGCSVClient()
    fetched_at = db.utc_now_iso()
    group_records = [metadata.parse_group(g, fetched_at)
                     for g in fetch_groups(client) if "groupId" in g]
    conn = db.init_db(db_path)
    try:
        db.upsert_groups(conn, group_records)
        conn.commit()
        targets = [g["group_id"] for g in group_records] if group_ids is None else list(group_ids)
        failed = []
        product_count = 0
        for group_id in targets:
            try:
                products = fetch_group_products(group_id, client)
            except Exception as error:
                failed.append(group_id)
                print(f"Warning: Could not fetch products for group {group_id}: {error}")
                continue
            records = [metadata.parse_product(p, fetched_at)
                       for p in products if p.get("productId") is not None]
            db.upsert_products(conn, records)
            conn.commit()
            product_count += len(records)
        return len(group_records), product_count, failed
    finally:
        conn.close()


def fetch_live_group_prices(group_ids, client=None):
    """Fetch each group's prices once."""
    client = client or TCGCSVClient()
    parsed_group_prices = {}
    successful_groups = []
    failed_groups = []
    for group_id in group_ids:
        group_id = str(group_id)
        try:
            parsed_group_prices[group_id] = client.fetch_group_prices(group_id)
            successful_groups.append(group_id)
        except Exception as error:
            failed_groups.append(group_id)
            print(f"Warning: Failed to fetch prices for group {group_id}: {error}")
    return parsed_group_prices, successful_groups, failed_groups


def main():
    start_time = time.time()
    target_date_str = parse_observation_date(sys.argv[1] if len(sys.argv) > 1 else None)
    print("=" * 60)
    print("Pokémon Price Fetcher (Live API Mode)")
    print(f"Observation Date: {target_date_str}")
    print("=" * 60)

    try:
        client = TCGCSVClient()
        print(f"Request delay: {client.request_delay:.2f} seconds")
        groups = fetch_groups(client)
        group_ids = [str(g["groupId"]) for g in groups if "groupId" in g]
        print(f"Loaded {len(group_ids):,} groups from API.")

        parsed_group_prices, successful_groups, failed_groups = fetch_live_group_prices(
            group_ids, client)
        rows, rows_total, seen = db.build_price_rows(parsed_group_prices, target_date_str)

        print(f"Groups processed: {len(successful_groups):,}")
        print(f"Groups failed: {len(failed_groups):,}")
        print(f"Rows in feed: {rows_total:,}")
        print(f"Rows stored (market >= ${config.STORE_MIN_MARKET_PRICE:.2f}): {len(rows):,}")

        # Validation: check minimum expected rows before touching the DB.
        if rows_total < MIN_EXPECTED_DAILY_ROWS:
            print(f"Final validation: FAILED (requires at least "
                  f"{MIN_EXPECTED_DAILY_ROWS:,} rows in the feed).")
            print("Refusing to commit incomplete dataset to database.")
            return 1

        rows_before, rows_stored = db.write_day_atomically(
            DB_PATH, target_date_str, rows, rows_total, seen, MIN_EXPECTED_DAILY_ROWS,
            source="live", groups=len(successful_groups), failed_groups=failed_groups)

        # Metadata: refresh groups, and products for any group that has a
        # product we have never seen. Best effort; prices are already saved.
        with sqlite3.connect(DB_PATH) as conn:
            known = db.known_product_ids(conn)
        unknown_groups = sorted({gid for pid, gid in seen.items() if pid not in known})
        try:
            _, products_added, meta_failed = refresh_metadata(DB_PATH, client, unknown_groups)
            print(f"Metadata: refreshed {len(unknown_groups)} group(s) with new products "
                  f"({products_added} products, {len(meta_failed)} failed).")
        except Exception as error:
            print(f"Warning: metadata refresh failed: {error}")

        elapsed = time.time() - start_time
        print("\n" + "=" * 60)
        print("DAILY FETCH SUMMARY")
        print("=" * 60)
        print(f"Date:                {target_date_str}")
        print(f"Groups processed:     {len(successful_groups):,}")
        print(f"Groups failed:        {len(failed_groups):,}")
        print(f"Rows in feed:         {rows_total:,}")
        print(f"Rows before update:   {rows_before:,}")
        print(f"Rows stored:          {rows_stored:,}")
        print(f"Request delay:        {client.request_delay:.2f} seconds")
        print(f"Runtime:              {elapsed:.2f} seconds")
        print(f"Final validation:     PASSED (>= {MIN_EXPECTED_DAILY_ROWS:,} rows)")
        print("Status:               SUCCESS")
        print("=" * 60)
        return 0
    except Exception as e:
        print(f"\nERROR: Live API price fetch failed: {e}")
        print("No price observations were written.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
