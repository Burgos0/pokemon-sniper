import os
import tempfile
import unittest

from scripts import run_trading_step as step
from sniper import engine

DATES = ["2026-11-20", "2026-11-21", "2026-11-23", "2026-11-24"]


class DatesToTradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = engine.init_ledger(os.path.join(self.tmp.name, "sniper.db"))
        self.addCleanup(self.conn.close)

    def test_new_ledger_starts_on_latest_fetched_date_only(self):
        self.assertEqual(step.dates_to_trade(self.conn, DATES), ["2026-11-24"])

    def test_catches_up_on_every_fetched_date_after_last_traded(self):
        engine.open_account(self.conn, "live", "2026-11-20")
        with self.conn:
            self.conn.execute("UPDATE accounts SET last_date = '2026-11-20'")
        self.assertEqual(step.dates_to_trade(self.conn, DATES),
                         ["2026-11-21", "2026-11-23", "2026-11-24"])
        self.assertEqual(step.dates_to_trade(self.conn, DATES, until="2026-11-21"), ["2026-11-21"])

    def test_nothing_new_means_nothing_to_trade(self):
        engine.open_account(self.conn, "live", "2026-11-20")
        with self.conn:
            self.conn.execute("UPDATE accounts SET last_date = '2026-11-24'")
        self.assertEqual(step.dates_to_trade(self.conn, DATES), [])


if __name__ == "__main__":
    unittest.main()
