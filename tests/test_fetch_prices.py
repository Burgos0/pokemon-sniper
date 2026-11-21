import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import scripts.fetch_prices as fetch_prices
from sniper import config, db


GROUPS = [{"groupId": 10, "name": "Test Set", "publishedOn": "2020-01-01T00:00:00"}]
PRODUCTS = [{"productId": 100, "groupId": 10, "name": "Test Card",
             "extendedData": [{"name": "Number", "value": "1/100"},
                              {"name": "Rarity", "value": "Rare"}]}]
PRICES = [{
    "productId": 100,
    "subTypeName": "Holofoil",
    "lowPrice": 10.0,
    "midPrice": 20.0,
    "highPrice": 300.0,
    "marketPrice": 25.0,
    "directLowPrice": None,
}, {
    "productId": 100,
    "subTypeName": "Reverse Holofoil",
    "lowPrice": 8.0,
    "midPrice": 18.0,
    "highPrice": 280.0,
    "marketPrice": 23.0,
    "directLowPrice": None,
}, {
    "productId": 101,
    "subTypeName": "Normal",
    "lowPrice": 0.1,
    "midPrice": 0.2,
    "highPrice": 3.0,
    "marketPrice": 0.25,  # below the storage floor: counted, not stored
    "directLowPrice": None,
}]


class CategoryTests(unittest.TestCase):
    def test_category_is_pokemon(self):
        self.assertEqual(config.CATEGORY_ID, 3)
        self.assertNotIn("yu-gi-oh", config.USER_AGENT.lower())
        self.assertNotIn("yugioh", config.USER_AGENT.lower())

    def test_no_hardcoded_yugioh_category_in_sources(self):
        root = Path(__file__).resolve().parent.parent
        for path in list((root / "sniper").glob("*.py")) + list((root / "scripts").glob("*.py")):
            text = path.read_text()
            self.assertNotIn("tcgplayer/2/", text, path)
            self.assertNotIn("CATEGORY_ID = 2", text, path)
            self.assertNotIn("category_id=2", text, path)


class LiveFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")

    def test_default_target_date_is_yesterday_utc(self):
        with patch.object(fetch_prices, "datetime") as date_mock:
            date_mock.now.return_value = datetime(2026, 9, 24, tzinfo=timezone.utc)
            self.assertEqual(fetch_prices.parse_target_date(), "2026-09-23")

    def test_explicit_target_date_remains_exact(self):
        self.assertEqual(fetch_prices.parse_target_date("2026-01-02"), "2026-01-02")

    def run_main(self, min_rows=1, live=None):
        live = live or (lambda *a, **k: ({"10": PRICES}, ["10"], []))
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", min_rows), \
                patch.object(fetch_prices, "current_utc_date", return_value="2026-09-24"), \
                patch.object(fetch_prices, "fetch_groups", return_value=GROUPS), \
                patch.object(fetch_prices, "fetch_group_products", return_value=PRODUCTS), \
                patch.object(fetch_prices, "fetch_live_group_prices", side_effect=live), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            return fetch_prices.main()

    def test_live_prices_are_written_by_printing_with_floor_and_log(self):
        self.assertEqual(self.run_main(), 0)
        with sqlite3.connect(self.db_path) as conn:
            printings = conn.execute(
                "SELECT date, product_id, printing, market_price FROM printing_prices "
                "ORDER BY printing").fetchall()
            log = conn.execute(
                "SELECT date, source, status, rows_total, rows_stored FROM fetch_log").fetchall()
            seen = conn.execute("SELECT product_id FROM archive_products ORDER BY 1").fetchall()
            product = conn.execute(
                "SELECT name, card_number, rarity, is_sealed FROM products").fetchall()
            columns = [r[1] for r in conn.execute("PRAGMA table_info(printing_prices)")]
        self.assertEqual(printings, [("2026-09-24", 100, "Holofoil", 25.0),
                                     ("2026-09-24", 100, "Reverse Holofoil", 23.0)])
        self.assertEqual(log, [("2026-09-24", "live", "ok", 3, 2)])
        self.assertEqual(seen, [(100,), (101,)])
        self.assertEqual(product, [("Test Card", "1/100", "Rare", 0)])
        self.assertNotIn("high_price", columns)

    def test_http_error_is_not_retried_for_non_transient_status(self):
        response = Mock(status_code=403)
        session = MagicMock()
        session.get.return_value = response
        with patch.object(fetch_prices.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_prices.TCGCSVClient(session).fetch_json("https://example.invalid/groups")
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_called()

    def test_tcgcsv_requests_are_paced(self):
        session = MagicMock()
        session.get.side_effect = [Mock(status_code=200, json=lambda: GROUPS),
                                    Mock(status_code=200, json=lambda: GROUPS)]
        client = fetch_prices.TCGCSVClient(session)
        with patch.object(fetch_prices.time, "monotonic", side_effect=[100.0, 100.1]), \
                patch.object(fetch_prices.time, "sleep") as sleep:
            client.fetch_json("https://tcgcsv.com/tcgplayer/3/groups")
            client.fetch_json("https://tcgcsv.com/tcgplayer/3/groups")

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.65)

    def test_401_retries_once_with_short_pause(self):
        session = MagicMock()
        session.get.side_effect = [Mock(status_code=401),
                                    Mock(status_code=200, json=lambda: GROUPS)]
        client = fetch_prices.TCGCSVClient(session)
        with patch.object(fetch_prices.time, "sleep") as sleep:
            self.assertEqual(client.fetch_json("https://tcgcsv.com/tcgplayer/3/groups"), GROUPS)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(sleep.call_args_list[0].args[0], 5)
        self.assertEqual(client.request_delay, 1.5)

    def test_repeated_401_is_limited_to_one_retry(self):
        session = MagicMock()
        session.get.side_effect = [Mock(status_code=401), Mock(status_code=401)]
        with patch.object(fetch_prices.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                fetch_prices.TCGCSVClient(session).fetch_json("https://tcgcsv.com/tcgplayer/3/groups")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(sleep.call_args_list[0].args[0], 5)

    def test_each_group_prices_endpoint_is_called_once_and_cached(self):
        session = MagicMock()
        session.get.return_value = Mock(status_code=200, json=lambda: PRICES)
        client = fetch_prices.TCGCSVClient(session)

        parsed, successful, failed = fetch_prices.fetch_live_group_prices(["10", "11"], client)
        client.fetch_group_prices("10")

        self.assertEqual(set(parsed), {"10", "11"})
        self.assertEqual(successful, ["10", "11"])
        self.assertEqual(failed, [])
        self.assertEqual(
            [call.args[0] for call in session.get.call_args_list],
            ["https://tcgcsv.com/tcgplayer/3/10/prices",
             "https://tcgcsv.com/tcgplayer/3/11/prices"],
        )

    def test_observation_date_is_captured_once(self):
        with patch.object(fetch_prices, "current_utc_date", return_value="2026-09-24") as today:
            self.assertEqual(fetch_prices.parse_observation_date(), "2026-09-24")
        today.assert_called_once_with()

    def test_too_few_rows_never_creates_database(self):
        self.assertEqual(self.run_main(min_rows=4), 1)
        self.assertFalse(Path(self.db_path).exists())

    def test_live_group_failure_stops_before_database_write(self):
        def boom(*a, **k):
            raise RuntimeError("HTTP 503")
        self.assertEqual(self.run_main(live=boom), 1)
        self.assertFalse(Path(self.db_path).exists())

    def test_failed_groups_are_logged_for_data_health(self):
        self.assertEqual(self.run_main(live=lambda *a, **k: ({"10": PRICES}, ["10"], ["11"])), 0)
        with sqlite3.connect(self.db_path) as conn:
            failed = conn.execute("SELECT failed_groups FROM fetch_log").fetchone()[0]
        self.assertEqual(json.loads(failed), ["11"])


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")

    def rows(self, date="2026-09-17"):
        return db.build_price_rows({"10": PRICES}, date)

    def test_below_floor_raises_and_writes_nothing(self):
        rows, total, seen = self.rows()
        with self.assertRaisesRegex(RuntimeError, "expected at least"):
            db.write_day_atomically(self.db_path, "2026-09-17", rows, total, seen, 99, "archive")
        self.assertFalse(Path(self.db_path).exists())

    def test_same_day_rerun_replaces_rows(self):
        with db.init_db(self.db_path) as conn:
            conn.execute("INSERT INTO printing_prices (date, product_id, printing, market_price) "
                         "VALUES ('2026-09-17', 555, 'Normal', 9.0)")
        rows, total, seen = self.rows()
        rows_before, stored = db.write_day_atomically(
            self.db_path, "2026-09-17", rows, total, seen, 1, "archive")
        with sqlite3.connect(self.db_path) as conn:
            ids = conn.execute("SELECT DISTINCT product_id FROM printing_prices").fetchall()
        self.assertEqual((rows_before, stored), (1, 2))
        self.assertEqual(ids, [(100,)])

    def test_exception_mid_transaction_restores_prior_rows(self):
        with db.init_db(self.db_path) as conn:
            conn.execute("INSERT INTO printing_prices (date, product_id, printing, market_price) "
                         "VALUES ('2026-09-17', 555, 'Normal', 9.0)")
        rows, total, seen = self.rows()
        with patch.object(db, "_log_fetch", side_effect=sqlite3.OperationalError("forced")):
            with self.assertRaises(sqlite3.OperationalError):
                db.write_day_atomically(self.db_path, "2026-09-17", rows, total, seen, 1, "archive")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT product_id FROM printing_prices").fetchall(), [(555,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM archive_products").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0], 0)

    def test_failure_log_never_overwrites_ok_date(self):
        rows, total, seen = self.rows()
        db.write_day_atomically(self.db_path, "2026-09-17", rows, total, seen, 1, "archive")
        db.log_fetch_failure(self.db_path, "2026-09-17", "archive", db.FETCH_STATUS_ERROR, "x")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(db.fetch_status(conn, "2026-09-17"), db.FETCH_STATUS_OK)

    def test_archive_products_tracks_first_and_last_seen(self):
        for date in ("2026-09-18", "2026-09-16", "2026-09-17"):
            rows, total, seen = self.rows(date)
            db.write_day_atomically(self.db_path, date, rows, total, seen, 1, "archive")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT product_id, first_seen, last_seen FROM archive_products ORDER BY 1"
            ).fetchall(), [(100, "2026-09-16", "2026-09-18"), (101, "2026-09-16", "2026-09-18")])


class ArchiveParseTests(unittest.TestCase):
    def test_archive_parser_loads_group_prices_and_reports_bad_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            category_dir = Path(tmp) / "3"
            (category_dir / "10").mkdir(parents=True)
            (category_dir / "11").mkdir()
            (category_dir / "12").mkdir()
            with (category_dir / "10" / "prices").open("w") as price_file:
                json.dump({"results": PRICES}, price_file)
            (category_dir / "12" / "prices").write_text("{not json")

            parsed, failed = fetch_prices.parse_archive_group_prices(str(category_dir))

        self.assertEqual(parsed, {"10": PRICES})
        self.assertEqual(failed, ["11", "12"])

    def test_find_category_dir_uses_category_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "2026-11-19" / "2" / "1").mkdir(parents=True)
            (Path(tmp) / "2026-11-19" / "3" / "1494").mkdir(parents=True)
            self.assertTrue(fetch_prices.find_category_dir(tmp).endswith("2026-11-19/3"))

    def test_429_raises_rate_limited_without_retry(self):
        response = Mock(status_code=429, headers={"Retry-After": "30"})
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_prices.requests, "get", return_value=response) as get:
            with self.assertRaises(fetch_prices.ArchiveRateLimited) as ctx:
                fetch_prices.download_archive("2025-01-01", str(Path(tmp) / "a.7z"))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(ctx.exception.retry_after, 30.0)

    def test_404_raises_archive_not_found_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_prices.requests, "get", return_value=Mock(status_code=404)) as get:
            with self.assertRaises(fetch_prices.ArchiveNotFound):
                fetch_prices.download_archive("2025-01-01", str(Path(tmp) / "a.7z"))
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
