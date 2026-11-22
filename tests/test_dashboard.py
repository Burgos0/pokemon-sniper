import json
import os
import tempfile
import unittest

from scripts import build_dashboard as dash
from sniper import engine

BACKTEST = {
    "frozen_on": "2026-11-22",
    "windows": {"full": {
        "window": ["2024-04-07", "2026-11-20"], "bot_return": 1.989, "benchmark_return": 1.293,
        "excess_return": 0.696, "bot_max_drawdown": -0.142, "benchmark_max_drawdown": -0.10,
        "rules": {"B": {"closed_trades": 102, "win_rate": 0.72, "avg_return": 0.137,
                        "median_return": 0.125, "avg_hold_days": 79,
                        "avg_return_vs_benchmark": 0.043, "signals_fired": 23438, "bought": 107},
                  "C": {"closed_trades": 0, "win_rate": None, "avg_return": None,
                        "median_return": None, "avg_hold_days": None,
                        "avg_return_vs_benchmark": None, "signals_fired": 5, "bought": 0}}}},
}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = lambda name: os.path.join(self.tmp.name, name)
        with open(self.path("bt.json"), "w") as f:
            json.dump(BACKTEST, f)

    def build(self, ledger="missing.db"):
        out = self.path("site")
        dash.main(["--ledger-db", self.path(ledger), "--prices-db", self.path("none.db"),
                   "--backtest", self.path("bt.json"), "--out", out])
        with open(os.path.join(out, "index.html")) as f:
            page = f.read()
        with open(os.path.join(out, "dashboard.json")) as f:
            return page, json.load(f)

    def test_empty_live_ledger_shows_1000_and_backtest_is_separate(self):
        page, data = self.build()
        self.assertFalse(data["live"]["started"])
        self.assertEqual(data["live"]["total"], 1000.0)
        self.assertIn("$1,000.00", page)
        self.assertIn("Paper trading. Returns before fees.", page)
        live_part, backtest_part = page.split("Backtest, not live results", 1)
        self.assertNotIn("+198.9%", live_part)
        self.assertIn("+198.9%", backtest_part)
        self.assertIn("Rule B", backtest_part)
        self.assertFalse(os.path.exists(self.path("missing.db")))  # never creates the ledger

    def test_live_positions_trades_and_signals_render_escaped(self):
        conn = engine.init_ledger(self.path("ledger.db"))
        engine.open_account(conn, "live", "2026-11-20")
        with conn:
            conn.execute("INSERT INTO positions VALUES ('live', 5, 'Holofoil', '2026-11-10', 40, 44, "
                         "'B', 44, '2026-11-20')")
            conn.execute("INSERT INTO trades (account, date, product_id, printing, card, set_name, "
                         "side, price, rule, reason) VALUES ('live', '2026-11-10', 5, 'Holofoil', "
                         "'<b>Pikachu</b>', 'Base', 'buy', 40, 'B', 'x')")
            conn.execute("INSERT INTO trades (account, date, product_id, printing, card, set_name, "
                         "side, price, rule, reason, buy_date, buy_price, return_pct, hold_days) "
                         "VALUES ('live', '2026-11-20', 6, 'Normal', 'Eevee', 'Base', 'sell', 56, "
                         "'C', 'take_profit: +40%', '2026-11-01', 40, 0.4, 19)")
            conn.execute("INSERT INTO signals VALUES ('live', '2026-11-20', 7, 'Holofoil', 'B', 30, 0, "
                         "'max_per_set', 'box up')")
            conn.execute("INSERT INTO equity_history VALUES ('live', '2026-11-20', 1, 960, 44, 1004, "
                         "1010, 1, 0, 0)")
            conn.execute("UPDATE accounts SET cash = 960, last_date = '2026-11-20'")
        conn.close()
        page, data = self.build("ledger.db")
        self.assertEqual(data["live"]["days_live"], 1)
        self.assertIn("&lt;b&gt;Pikachu&lt;/b&gt;", page)
        self.assertNotIn("<b>Pikachu</b>", page.split('id="dashboard-data"')[0])
        self.assertIn("https://www.tcgplayer.com/product/5", page)
        self.assertIn("take profit", page)
        self.assertIn("2 already held from this set", page)
        self.assertIn("+10.0%", page)  # 40 -> 44


if __name__ == "__main__":
    unittest.main()
