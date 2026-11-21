"""Copied analysis helpers: weekly windows, spike confirmation, as_of loading, horizons."""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from sniper import db
from sniper.analysis import (
    _select_weekly_mover_windows,
    detect_spike,
    evaluate_horizon_outcome,
    load_printing_prices,
)

DATES = pd.to_datetime([f"2026-09-{d:02d}" for d in range(20, 30)])
BASELINE, RECENT = list(DATES[1:4]), list(DATES[-3:])


def history(markets, lows):
    return pd.DataFrame({"product_id": 1, "printing": "Holofoil", "date": DATES,
                         "market_price": markets, "low_price": lows})


def status(markets, lows, baseline=10.0, current=None):
    df = history(markets, lows)
    current = current if current is not None else pd.Series(markets[-3:]).median()
    return detect_spike(1, baseline, current, df, DATES[-1], RECENT, "Holofoil", BASELINE)


class SpikeConfirmationRules(unittest.TestCase):
    def test_real_trend_is_confirmed(self):
        markets = [10, 10, 10, 10, 11, 13, 15, 17, 18, 18.5]
        lows = [9, 9, 9, 9, 10, 12, 14, 16, 17, 17]
        self.assertEqual(status(markets, lows), "CONFIRMED")

    def test_single_sale_step_is_unconfirmed(self):
        markets = [10] * 7 + [30, 30, 30]
        lows = [9] * 4 + [12, 15, 20, 26, 27, 28]
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_reverted_price_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 11, 12, 13, 40, 40, 10.5]
        lows = [9, 9, 9, 9, 12, 20, 30, 35, 36, 36]
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_flat_listing_floor_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        lows = [9] * 10
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_thin_market_baseline_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        lows = [40] * 10
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_missing_low_prices_skip_floor_rule(self):
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        self.assertEqual(status(markets, [None] * 10), "CONFIRMED")

    def test_printing_identity_is_respected(self):
        df = pd.concat([history([10] * 7 + [30, 30, 30], [9] * 10),
                        history([10, 10, 10, 10, 11, 13, 15, 17, 18, 18.5],
                                [9, 9, 9, 9, 10, 12, 14, 16, 17, 17]).assign(printing="Normal")])
        self.assertEqual(detect_spike(1, 10.0, 18.0, df, DATES[-1], RECENT, "Normal", BASELINE),
                         "CONFIRMED")
        self.assertEqual(detect_spike(1, 10.0, 30.0, df, DATES[-1], RECENT, "Holofoil", BASELINE),
                         "UNCONFIRMED")


class WeeklyWindowTests(unittest.TestCase):
    def test_windows_use_actual_snapshot_dates(self):
        dates = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-04", "2026-09-05",
                                "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-12",
                                "2026-09-15"])
        recent, baseline = _select_weekly_mover_windows(pd.DataFrame({"date": dates}))
        self.assertEqual(list(recent), list(dates[-3:]))
        self.assertEqual(list(baseline), list(dates[:3]))

    def test_fewer_than_nine_dates_returns_none(self):
        dates = pd.to_datetime([f"2026-09-0{d}" for d in range(1, 9)])
        self.assertIsNone(_select_weekly_mover_windows(pd.DataFrame({"date": dates})))


class AsOfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")
        with db.init_db(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO printing_prices (date, product_id, printing, market_price) "
                "VALUES (?, ?, ?, ?)",
                [("2026-09-01", 1, "Holofoil", 10.0), ("2026-09-02", 1, "Holofoil", 11.0),
                 ("2026-09-03", 1, "Holofoil", 50.0), ("2026-09-03", 1, "Normal", 7.0)])

    def test_loader_never_returns_rows_after_as_of(self):
        df = load_printing_prices(self.db_path, "2026-09-02")
        self.assertEqual(df["date"].max(), pd.Timestamp("2026-09-02"))
        self.assertEqual(len(df), 2)

    def test_loader_requires_as_of(self):
        with self.assertRaises(ValueError):
            load_printing_prices(self.db_path, None)

    def test_loader_result_unchanged_by_later_rows(self):
        before = load_printing_prices(self.db_path, "2026-09-02")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO printing_prices (date, product_id, printing, market_price) "
                         "VALUES ('2026-09-10', 1, 'Holofoil', 999.0)")
        after = load_printing_prices(self.db_path, "2026-09-02")
        pd.testing.assert_frame_equal(before, after)

    def test_horizon_outcome_uses_printing(self):
        with sqlite3.connect(self.db_path) as conn:
            result = evaluate_horizon_outcome(
                conn, 1, "Normal", datetime(2026, 9, 1), 5.0, 2,
                datetime(2026, 9, 3), jump_percent=20, jump_dollars=1)
        self.assertEqual(result[0], "ok")
        self.assertEqual(result[1], 7.0)
        self.assertAlmostEqual(result[2], 40.0)


if __name__ == "__main__":
    unittest.main()
