"""
Load the full Pokémon price history from TCGCSV's daily archives.

Resumable: stop it any time and re-run. Dates already 'ok' in the DB are
skipped, every downloaded archive is kept in the cache folder and never
downloaded again, downloads go to a .part file first so an interrupted
download never leaves a half file in the cache, and 404s for old dates are
remembered so they are not re-requested.

Usage:
    python -m scripts.backfill_archive                # 2024-02-08 .. yesterday
    python -m scripts.backfill_archive --end 2024-03-01
    python -m scripts.backfill_archive --report-only  # just print the report
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import fetch_prices
from sniper import config, db

MAX_CONSECUTIVE_DOWNLOAD_ERRORS = 5


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def backfill_floor(date_str, anchors=config.BACKFILL_ROW_ANCHORS,
                   ratio=config.BACKFILL_FLOOR_RATIO):
    """85% of the expected row count, linearly interpolated between observed anchors."""
    points = [(parse_date(d).toordinal(), count) for d, count in anchors]
    x = parse_date(date_str).toordinal()
    if x <= points[0][0]:
        expected = points[0][1]
    elif x >= points[-1][0]:
        expected = points[-1][1]
    else:
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= x <= x1:
                expected = y0 + (y1 - y0) * (x - x0) / (x1 - x0)
                break
    return int(expected * ratio)


def archive_path(cache_dir, date_str):
    return os.path.join(cache_dir, f"prices-{date_str}.ppmd.7z")


def missing_marker(cache_dir, date_str):
    return os.path.join(cache_dir, f"prices-{date_str}.missing")


class Downloader:
    """Downloads at most one archive per ARCHIVE_DOWNLOAD_INTERVAL seconds."""

    def __init__(self, interval=config.ARCHIVE_DOWNLOAD_INTERVAL):
        self.interval = interval
        self.last = None
        self.count = 0

    def fetch(self, date_str, dest):
        if self.last is not None:
            wait = self.interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
        part = dest + ".part"
        try:
            fetch_prices.download_archive(date_str, part)
        finally:
            self.last = time.monotonic()
        os.replace(part, dest)
        self.count += 1


def ensure_archive(date_str, cache_dir, downloader, today, retry_missing=False):
    """Return the cached archive path, downloading it once if needed, or None if missing."""
    path = archive_path(cache_dir, date_str)
    if os.path.exists(path):
        return path
    marker = missing_marker(cache_dir, date_str)
    if os.path.exists(marker) and not retry_missing:
        return None
    try:
        downloader.fetch(date_str, path)
    except fetch_prices.ArchiveNotFound:
        if (today - parse_date(date_str)).days > config.ARCHIVE_MISSING_GRACE_DAYS:
            with open(marker, "w") as f:
                f.write(db.utc_now_iso() + "\n")
        return None
    if os.path.exists(marker):
        os.remove(marker)
    return path


def load_archive_date(db_path, date_str, path):
    """Extract category 3 from one cached archive and write it. Returns (status, detail)."""
    min_expected = backfill_floor(date_str)
    with tempfile.TemporaryDirectory(prefix="sniper_extract_") as extract_dir:
        fetch_prices.extract_archive(path, extract_dir,
                                     include=f"{date_str}/{config.CATEGORY_ID}/*")
        cat_dir = fetch_prices.find_category_dir(extract_dir)
        parsed, failed_groups = fetch_prices.parse_archive_group_prices(cat_dir)
    rows, rows_total, seen = db.build_price_rows(parsed, date_str)
    if rows_total < min_expected:
        note = f"{rows_total:,} rows < floor {min_expected:,}"
        db.log_fetch_failure(db_path, date_str, "archive", db.FETCH_STATUS_REJECTED, note,
                             rows_total=rows_total, min_expected=min_expected)
        return db.FETCH_STATUS_REJECTED, note
    db.write_day_atomically(db_path, date_str, rows, rows_total, seen, min_expected,
                            source="archive", groups=len(parsed), failed_groups=failed_groups)
    return db.FETCH_STATUS_OK, f"{rows_total:,} rows, {len(rows):,} stored, {len(parsed)} sets"


def run_backfill(db_path, cache_dir, start, end, retry_missing=False, max_downloads=None,
                 downloader=None, today=None):
    os.makedirs(cache_dir, exist_ok=True)
    db.init_db(db_path).close()
    downloader = downloader or Downloader()
    today = today or datetime.now(timezone.utc).date()
    consecutive_errors = 0

    for day in date_range(start, end):
        date_str = day.isoformat()
        with sqlite3.connect(db_path) as conn:
            if db.fetch_status(conn, date_str) == db.FETCH_STATUS_OK:
                continue
        if max_downloads is not None and downloader.count >= max_downloads \
                and not os.path.exists(archive_path(cache_dir, date_str)):
            print(f"Reached --max-downloads {max_downloads}; stopping.")
            break
        try:
            path = ensure_archive(date_str, cache_dir, downloader, today, retry_missing)
        except Exception as error:
            consecutive_errors += 1
            print(f"{date_str}: download error: {error}")
            db.log_fetch_failure(db_path, date_str, "archive", db.FETCH_STATUS_ERROR,
                                 f"download: {error}")
            if consecutive_errors >= MAX_CONSECUTIVE_DOWNLOAD_ERRORS:
                print(f"{consecutive_errors} download errors in a row; stopping to be polite. "
                      "Re-run later to resume.")
                break
            continue
        consecutive_errors = 0
        if path is None:
            print(f"{date_str}: no archive (missing)")
            db.log_fetch_failure(db_path, date_str, "archive", db.FETCH_STATUS_MISSING,
                                 "archive not found")
            continue
        try:
            status, detail = load_archive_date(db_path, date_str, path)
        except Exception as error:
            status, detail = db.FETCH_STATUS_ERROR, f"load: {error}"
            db.log_fetch_failure(db_path, date_str, "archive", status, detail)
        print(f"{date_str}: {status} ({detail})")
    return downloader.count


def build_report(db_path, start, end):
    conn = sqlite3.connect(db_path)
    try:
        log = {row[0]: row[1:] for row in conn.execute(
            "SELECT date, status, source, rows_total, rows_stored, groups, failed_groups, note "
            "FROM fetch_log WHERE date BETWEEN ? AND ? ORDER BY date",
            (start.isoformat(), end.isoformat()))}
        days = [d.isoformat() for d in date_range(start, end)]
        ok_days = [d for d in days if log.get(d, (None,))[0] == db.FETCH_STATUS_OK]
        gaps = [{"date": d, "status": log[d][0] if d in log else "not_attempted",
                 "note": log[d][6] if d in log else None}
                for d in days if d not in ok_days]
        per_day = [{"date": d, "source": log[d][1], "rows_total": log[d][2],
                    "rows_stored": log[d][3], "sets": log[d][4],
                    "failed_sets": json.loads(log[d][5] or "[]")} for d in ok_days]

        archived = conn.execute("SELECT COUNT(*) FROM archive_products").fetchone()[0]
        no_meta = conn.execute(
            "SELECT COUNT(*) FROM archive_products a LEFT JOIN products p USING (product_id) "
            "WHERE p.product_id IS NULL").fetchone()[0]
        stored_ids = conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM printing_prices").fetchone()[0]
        stored_no_meta = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT product_id FROM printing_prices) s "
            "LEFT JOIN products p USING (product_id) WHERE p.product_id IS NULL").fetchone()[0]
        products = conn.execute(
            "SELECT COUNT(*), SUM(is_sealed), SUM(rarity IS NOT NULL AND card_number IS NULL) "
            "FROM products").fetchone()
        groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        total_rows = conn.execute("SELECT COUNT(*) FROM printing_prices").fetchone()[0]
    finally:
        conn.close()

    totals = [d["rows_total"] for d in per_day]
    stored = [d["rows_stored"] for d in per_day]
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "calendar_days": len(days)},
        "loaded": {
            "ok_days": len(ok_days),
            "first_ok": ok_days[0] if ok_days else None,
            "last_ok": ok_days[-1] if ok_days else None,
            "rows_total_min": min(totals) if totals else None,
            "rows_total_max": max(totals) if totals else None,
            "rows_stored_min": min(stored) if stored else None,
            "rows_stored_max": max(stored) if stored else None,
            "printing_prices_rows": total_rows,
            "db_bytes": os.path.getsize(db_path),
        },
        "gaps": gaps,
        "metadata": {
            "groups": groups,
            "products": products[0],
            "sealed_products": products[1],
            "products_with_rarity_but_no_number": products[2],
            "archived_product_ids": archived,
            "archived_ids_without_metadata": no_meta,
            "stored_product_ids": stored_ids,
            "stored_ids_without_metadata": stored_no_meta,
        },
        "per_day": per_day,
    }


def print_report(report):
    r, loaded, meta = report["range"], report["loaded"], report["metadata"]
    print("\n" + "=" * 60)
    print("BACKFILL REPORT")
    print("=" * 60)
    print(f"Range:              {r['start']} .. {r['end']} ({r['calendar_days']} days)")
    print(f"Days loaded (ok):   {loaded['ok_days']} ({loaded['first_ok']} .. {loaded['last_ok']})")
    print(f"Feed rows/day:      {loaded['rows_total_min']} .. {loaded['rows_total_max']}")
    print(f"Stored rows/day:    {loaded['rows_stored_min']} .. {loaded['rows_stored_max']}")
    print(f"printing_prices:    {loaded['printing_prices_rows']:,} rows, "
          f"DB {loaded['db_bytes'] / 1e6:,.0f} MB")
    print(f"Gaps:               {len(report['gaps'])}")
    for gap in report["gaps"]:
        print(f"  {gap['date']}  {gap['status']}  {gap['note'] or ''}")
    print(f"Groups / products:  {meta['groups']} / {meta['products']} "
          f"({meta['sealed_products']} sealed)")
    print(f"Archived IDs without live metadata: {meta['archived_ids_without_metadata']:,} "
          f"of {meta['archived_product_ids']:,}")
    print(f"Stored IDs without live metadata:   {meta['stored_ids_without_metadata']:,} "
          f"of {meta['stored_product_ids']:,}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    parser.add_argument("--start", type=parse_date, default=parse_date(config.BACKFILL_START_DATE))
    parser.add_argument("--end", type=parse_date, default=yesterday)
    parser.add_argument("--db", default=config.PRICES_DB_PATH)
    parser.add_argument("--cache-dir", default=config.ARCHIVE_CACHE_DIR)
    parser.add_argument("--retry-missing", action="store_true",
                        help="Re-request dates previously recorded as missing (404).")
    parser.add_argument("--max-downloads", type=int, default=None,
                        help="Stop after this many new downloads (cached dates still load).")
    parser.add_argument("--skip-metadata", action="store_true",
                        help="Do not refresh groups/products from the live API.")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--report-json", default="data/backfill_report.json")
    args = parser.parse_args()

    if args.start < parse_date(config.BACKFILL_START_DATE):
        parser.error(f"--start must be on or after {config.BACKFILL_START_DATE} "
                     "(earlier dates are not probed)")

    if not args.report_only:
        fetch_prices.check_7z_available()
        if not args.skip_metadata:
            groups, products, failed = fetch_prices.refresh_metadata(args.db)
            print(f"Metadata: {groups} groups, {products} products, {len(failed)} failed groups")
        downloads = run_backfill(args.db, args.cache_dir, args.start, args.end,
                                 args.retry_missing, args.max_downloads)
        print(f"New archive downloads this run: {downloads}")

    report = build_report(args.db, args.start, args.end)
    with open(args.report_json, "w") as f:
        json.dump(report, f, indent=2)
    print_report(report)
    print(f"Full report (incl. rows per day): {args.report_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
