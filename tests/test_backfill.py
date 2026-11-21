import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import backfill_archive as backfill
from scripts import fetch_prices
from sniper import db

ROWS = [{"productId": 100 + i, "subTypeName": "Holofoil", "lowPrice": 20.0,
         "midPrice": 25.0, "highPrice": 900.0, "marketPrice": 24.0 + i,
         "directLowPrice": None} for i in range(5)]


class FakeDownloader:
    """Writes a placeholder archive instead of hitting the network."""

    def __init__(self, missing=()):
        self.count = 0
        self.requested = []
        self.missing = set(missing)

    def fetch(self, date_str, dest):
        self.requested.append(date_str)
        if date_str in self.missing:
            raise fetch_prices.ArchiveNotFound(date_str)
        Path(dest).write_text("fake archive")
        self.count += 1


def fake_extract(archive_path, extract_dir, include=None):
    date_str = Path(archive_path).name[len("prices-"):-len(".ppmd.7z")]
    group = Path(extract_dir) / date_str / "3" / "1494"
    group.mkdir(parents=True)
    (group / "prices").write_text(json.dumps({"results": ROWS}))


class BackfillFloorTests(unittest.TestCase):
    def test_floor_matches_anchors_and_interpolates(self):
        self.assertEqual(backfill.backfill_floor("2024-02-08"), int(37_234 * 0.85))
        self.assertEqual(backfill.backfill_floor("2026-11-19"), int(45_793 * 0.85))
        mid = backfill.backfill_floor("2024-09-20")
        self.assertGreater(mid, backfill.backfill_floor("2024-02-08"))
        self.assertLess(mid, backfill.backfill_floor("2025-05-01"))
        self.assertEqual(backfill.backfill_floor("2027-06-01"), int(45_793 * 0.85))


class BackfillRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "prices.db")
        self.cache = os.path.join(self.tmp.name, "cache")
        patcher_extract = patch.object(fetch_prices, "extract_archive", side_effect=fake_extract)
        patcher_floor = patch.object(backfill, "backfill_floor", return_value=1)
        patcher_extract.start()
        patcher_floor.start()
        self.addCleanup(patcher_extract.stop)
        self.addCleanup(patcher_floor.stop)

    def run_range(self, start, end, downloader, **kwargs):
        return backfill.run_backfill(self.db_path, self.cache, date.fromisoformat(start),
                                     date.fromisoformat(end), downloader=downloader,
                                     today=date(2026, 11, 20), **kwargs)

    def statuses(self):
        with sqlite3.connect(self.db_path) as conn:
            return dict(conn.execute("SELECT date, status FROM fetch_log"))

    def test_loads_each_date_and_rerun_downloads_nothing(self):
        first = FakeDownloader()
        self.run_range("2025-01-01", "2025-01-03", first)
        self.assertEqual(first.requested, ["2025-01-01", "2025-01-02", "2025-01-03"])
        self.assertEqual(set(self.statuses().values()), {"ok"})

        second = FakeDownloader()
        self.run_range("2025-01-01", "2025-01-03", second)
        self.assertEqual(second.requested, [])

    def test_cached_archive_is_loaded_without_download(self):
        os.makedirs(self.cache)
        Path(backfill.archive_path(self.cache, "2025-01-01")).write_text("cached")
        downloader = FakeDownloader()
        self.run_range("2025-01-01", "2025-01-01", downloader)
        self.assertEqual(downloader.requested, [])
        self.assertEqual(self.statuses(), {"2025-01-01": "ok"})

    def test_missing_old_date_is_remembered_and_not_requested_again(self):
        downloader = FakeDownloader(missing={"2025-01-02"})
        self.run_range("2025-01-01", "2025-01-03", downloader)
        self.assertEqual(self.statuses()["2025-01-02"], "missing")
        self.assertTrue(os.path.exists(backfill.missing_marker(self.cache, "2025-01-02")))

        again = FakeDownloader()
        self.run_range("2025-01-01", "2025-01-03", again)
        self.assertEqual(again.requested, [])

        retry = FakeDownloader()
        self.run_range("2025-01-01", "2025-01-03", retry, retry_missing=True)
        self.assertEqual(retry.requested, ["2025-01-02"])
        self.assertEqual(self.statuses()["2025-01-02"], "ok")

    def test_recent_404_is_not_marked_missing(self):
        downloader = FakeDownloader(missing={"2026-11-19"})
        self.run_range("2026-11-19", "2026-11-19", downloader)
        self.assertFalse(os.path.exists(backfill.missing_marker(self.cache, "2026-11-19")))

    def test_too_few_rows_is_rejected_not_loaded(self):
        with patch.object(backfill, "backfill_floor", return_value=99):
            self.run_range("2025-01-01", "2025-01-01", FakeDownloader())
        self.assertEqual(self.statuses(), {"2025-01-01": "rejected"})
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM printing_prices").fetchone()[0], 0)

    def test_interrupted_download_leaves_no_cached_file(self):
        downloader = backfill.Downloader(interval=0)
        with patch.object(fetch_prices, "download_archive",
                          side_effect=lambda d, part: (Path(part).write_text("half"),
                                                       (_ for _ in ()).throw(KeyboardInterrupt))):
            with self.assertRaises(KeyboardInterrupt):
                self.run_range("2025-01-01", "2025-01-01", downloader)
        self.assertFalse(os.path.exists(backfill.archive_path(self.cache, "2025-01-01")))

    def test_rate_limit_backs_off_and_retries_same_date(self):
        waits = []
        calls = []

        def download(date_str, part):
            calls.append(date_str)
            if len(calls) <= 2:
                raise fetch_prices.ArchiveRateLimited("429", retry_after=None if len(calls) == 1 else 7)
            Path(part).write_text("ok")

        downloader = backfill.Downloader(interval=0, backoff_start=120, sleep=waits.append)
        with patch.object(fetch_prices, "download_archive", side_effect=download):
            self.run_range("2025-01-01", "2025-01-01", downloader)
        self.assertEqual(calls, ["2025-01-01"] * 3)
        self.assertEqual(waits, [120, 7])  # backoff first, then the server's Retry-After
        self.assertEqual(self.statuses(), {"2025-01-01": "ok"})

    def test_persistent_rate_limit_stops_run_without_marking_dates(self):
        def download(date_str, part):
            raise fetch_prices.ArchiveRateLimited("429")

        downloader = backfill.Downloader(interval=0, max_consecutive=3, sleep=lambda s: None)
        with patch.object(fetch_prices, "download_archive", side_effect=download) as dl:
            self.run_range("2025-01-01", "2025-01-05", downloader)
        self.assertEqual(dl.call_count, 3)  # stopped at the first date, did not hammer the rest
        self.assertEqual(self.statuses(), {})

    def test_report_lists_gaps_and_metadata_coverage(self):
        self.run_range("2025-01-01", "2025-01-03", FakeDownloader(missing={"2025-01-02"}))
        with db.init_db(self.db_path) as conn:
            conn.execute("INSERT INTO products (product_id, is_sealed) VALUES (100, 0)")
        report = backfill.build_report(self.db_path, date(2025, 1, 1), date(2025, 1, 4))
        self.assertEqual(report["loaded"]["ok_days"], 2)
        self.assertEqual([(g["date"], g["status"]) for g in report["gaps"]],
                         [("2025-01-02", "missing"), ("2025-01-04", "not_attempted")])
        self.assertEqual(report["metadata"]["archived_product_ids"], 5)
        self.assertEqual(report["metadata"]["archived_ids_without_metadata"], 4)


if __name__ == "__main__":
    unittest.main()
