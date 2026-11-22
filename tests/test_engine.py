import os
import shutil
import sqlite3
import tempfile
import unittest

import numpy as np

from scripts import backtest
from sniper import config, db, engine, rules
from sniper import panel as panel_mod

START = "2025-01-01"
OLD_SET = "2020-01-01T00:00:00"


class World:
    """A small prices.db built by hand: groups, products, prices, fetch log."""

    def __init__(self, path):
        self.path = path
        self.prices = {}   # (date, pid, printing) -> (low, market)
        self.products = {}
        self.groups = {1: ("Old Set", OLD_SET)}

    def product(self, pid, name=None, group=1, sealed=False):
        self.products[pid] = (name or f"Card {pid}", group, sealed)

    def series(self, pid, start, markets, lows=None, printing="Holofoil"):
        for i, m in enumerate(markets):
            if m is None:
                continue
            low = lows[i] if lows else m * 0.9
            self.prices[(rules.shift(start, i), pid, printing)] = (low, m)

    def save(self):
        conn = db.init_db(self.path)
        with conn:
            conn.executemany("INSERT INTO groups (group_id, name, release_date) VALUES (?, ?, ?)",
                             [(g, n, r) for g, (n, r) in self.groups.items()])
            conn.executemany(
                "INSERT INTO products (product_id, name, group_id, is_sealed) VALUES (?, ?, ?, ?)",
                [(p, n, g, int(s)) for p, (n, g, s) in self.products.items()])
            conn.executemany(
                "INSERT INTO printing_prices (date, product_id, printing, low_price, mid_price, "
                "market_price) VALUES (?, ?, ?, ?, ?, ?)",
                [(d, p, s, low, m * 1.1, m) for (d, p, s), (low, m) in self.prices.items()])
            dates = sorted({d for d, _, _ in self.prices})
            conn.executemany("INSERT INTO fetch_log (date, source, status) VALUES (?, 'test', 'ok')",
                             [(d,) for d in dates])
        conn.close()


def wiggle(base, n, step=0.01):
    """A price that changes every day but stays flat overall."""
    return [round(base * (1 + step * (i % 2)), 2) for i in range(n)]


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices_db = os.path.join(self.tmp.name, "prices.db")
        self.ledger_db = os.path.join(self.tmp.name, "ledger.db")
        self.world = World(self.prices_db)

    def replay(self, first, last, prices_db=None, ledger_db=None, account="t"):
        prices_db = prices_db or self.prices_db
        ledger_db = ledger_db or self.ledger_db
        meta = panel_mod.Metadata.load(prices_db)
        boxes = rules.choose_boxes(meta)
        panel = panel_mod.load_panel(prices_db, last, START,
                                     product_ids=[b["product_id"] for b in boxes.values()])
        ctx = rules.build_context(panel, meta)
        conn = engine.init_ledger(ledger_db)
        engine.open_account(conn, account, first)
        for day in panel_mod.calendar_days(first, last):
            engine.run_step(conn, account, panel.upto(day), ctx, meta)
        return conn

    def rows(self, conn, table):
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()


def squeeze_card(world, pid, base, n, squeeze_from):
    """A card whose low rises to its market price from day `squeeze_from` on."""
    markets = wiggle(base, n)
    lows = [m * (1.02 if i >= squeeze_from else 0.8) for i, m in enumerate(markets)]
    world.product(pid)
    world.series(pid, START, markets, lows)


class UniverseTests(EngineTestBase):
    def universe_ids(self, day):
        meta = panel_mod.Metadata.load(self.prices_db)
        panel = panel_mod.load_panel(self.prices_db, day, START)
        ctx = rules.build_context(panel, meta)
        mask = rules.universe_mask(panel, ctx)
        return {panel.ids[j][0] for j in np.flatnonzero(mask)}

    def test_sealed_products_never_enter_singles_universe(self):
        self.world.product(1, "Card")
        self.world.product(2, "Some Booster Pack", sealed=True)
        self.world.series(1, START, wiggle(50, 40))
        self.world.series(2, START, wiggle(50, 40), printing="Normal")
        self.world.save()
        self.assertEqual(self.universe_ids(rules.shift(START, 39)), {1})

    def test_price_range_set_age_history_and_metadata_filters(self):
        self.world.groups[2] = ("New Set", rules.shift(START, 10))
        self.world.product(1)                  # ok
        self.world.product(2)                  # too cheap
        self.world.product(3)                  # too expensive
        self.world.product(4, group=2)         # set too new
        self.world.product(6)                  # dead listing: never changes
        self.world.series(1, START, wiggle(50, 40))
        self.world.series(2, START, wiggle(15, 40))
        self.world.series(3, START, wiggle(250, 40))
        self.world.series(4, START, wiggle(50, 40))
        self.world.series(5, START, wiggle(50, 40))  # no live metadata
        self.world.series(6, START, [50.0] * 40)
        self.world.series(7, START, wiggle(50, 37) + [None, None, 50.0])  # 2 of last 3 missing: still 10/12
        self.world.product(7)
        self.world.series(8, START, wiggle(50, 36) + [None, None, None, 50.0])  # 9/12 ok
        self.world.product(8)
        self.world.series(9, START, wiggle(50, 35) + [None] * 4 + [50.0])  # 8/12: out
        self.world.product(9)
        self.world.save()
        self.assertEqual(self.universe_ids(rules.shift(START, 39)), {1, 7, 8})

    def test_window_needs_80_percent_coverage(self):
        self.assertEqual(rules.required_days(30), 24)
        self.assertEqual(rules.required_days(60), 48)
        self.assertEqual(rules.required_days(3), 3)


class RuleTests(EngineTestBase):
    def signals_on(self, day):
        meta = panel_mod.Metadata.load(self.prices_db)
        boxes = rules.choose_boxes(meta)
        panel = panel_mod.load_panel(self.prices_db, day, START,
                                     product_ids=[b["product_id"] for b in boxes.values()])
        ctx = rules.build_context(panel, meta)
        return [(s.rule, s.product_id) for s in rules.compute_signals(panel, ctx)]

    def test_rule_a_needs_two_squeeze_days_and_a_calm_week(self):
        squeeze_card(self.world, 1, 50, 40, squeeze_from=39)   # only 1 day
        squeeze_card(self.world, 2, 50, 40, squeeze_from=38)   # 2 days: fires
        markets = wiggle(50, 33) + [56, 57, 58, 59, 60, 61, 62]  # +24% in a week
        self.world.product(3)
        self.world.series(3, START, markets, [m * 1.02 for m in markets])
        self.world.save()
        self.assertEqual(self.signals_on(rules.shift(START, 39)), [("A", 2)])

    def test_rule_b_box_rise_buys_rising_top_singles_of_that_set(self):
        self.world.product(100, "Old Set Booster Box", sealed=True)
        self.world.series(100, START, [100.0] * 20 + [100 + 2 * i for i in range(1, 21)],
                          printing="Normal")
        rising = wiggle(50, 30) + [52, 53, 54, 55, 56, 57, 58, 59, 60, 61]
        for pid in range(1, 7):  # 6 rising singles; only the top 5 by price qualify
            self.world.product(pid)
            self.world.series(pid, START, [round(m + pid, 2) for m in rising])
        self.world.save()
        fired = self.signals_on(rules.shift(START, 39))
        self.assertEqual(fired, [("B", 6), ("B", 5), ("B", 4), ("B", 3), ("B", 2)])

    def test_rule_b_box_choice(self):
        products = {1: ("X Booster Box", True), 2: ("X Half Booster Box", True),
                    3: ("Code Card - X Booster Box", True), 4: ("X Booster Box Case", True),
                    5: ("Y Elite Trainer Box", True), 6: ("Y Pokemon Center Elite Trainer Box", True),
                    7: ("Z Booster Box", False)}
        groups = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2, 7: 3}
        meta = panel_mod.Metadata(
            {pid: {"name": n, "group_id": groups[pid], "is_sealed": s}
             for pid, (n, s) in products.items()}, {})
        boxes = rules.choose_boxes(meta)
        self.assertEqual({g: (b["product_id"], b["kind"]) for g, b in boxes.items()},
                         {1: (1, "booster_box"), 2: (5, "etb")})

    def test_rule_c_bottoming(self):
        markets = [100.0] * 30 + wiggle(70, 14) + [72.0, 73.0, 75.0]  # high 100, flat ~70, +6%
        self.world.product(1)
        self.world.series(1, rules.shift(START, 13), markets)  # 60-day window covers 47 of 60 days
        self.world.product(2)
        self.world.series(2, START, [100.0] * 43 + wiggle(70, 14) + [72.0, 73.0, 75.0])
        self.world.save()
        self.assertEqual(self.signals_on(rules.shift(START, 59)), [("C", 2)])


class StepTests(EngineTestBase):
    def test_every_trade_executes_at_market_price(self):
        squeeze_card(self.world, 1, 50, 60, squeeze_from=30)
        self.world.series(1, START, wiggle(50, 45) + [75.0] * 15,
                          [50.0] * 30 + [80.0] * 30)  # buy ~day 31, take profit at 75
        self.world.save()
        conn = self.replay(rules.shift(START, 30), rules.shift(START, 59))
        trades = conn.execute("SELECT date, product_id, printing, side, price FROM trades").fetchall()
        self.assertEqual([t[3] for t in trades][:2], ["buy", "sell"])
        with sqlite3.connect(self.prices_db) as prices:
            for day, pid, printing, _, price in trades:
                low, mid, market = prices.execute(
                    "SELECT low_price, mid_price, market_price FROM printing_prices WHERE date = ? "
                    "AND product_id = ? AND printing = ?", (day, pid, printing)).fetchone()
                self.assertEqual(price, market)
                self.assertNotEqual(price, low)
                self.assertNotEqual(price, mid)

    def test_cash_never_negative(self):
        for pid in range(1, 6):  # 5 squeezes near $190 use up almost all the cash
            squeeze_card(self.world, pid, 190 + pid / 10, 40, squeeze_from=30)
        for pid in range(21, 36):  # so most of these $35 cards cannot be afforded
            squeeze_card(self.world, pid, 35 + pid / 10, 40, squeeze_from=30)
        self.world.save()
        conn = self.replay(rules.shift(START, 31), rules.shift(START, 39))
        cash = [c for (c,) in conn.execute("SELECT cash FROM equity_history")]
        self.assertTrue(all(c >= 0 for c in cash))
        skips = {r for (r,) in conn.execute("SELECT DISTINCT skip_reason FROM signals")}
        self.assertIn("insufficient_cash", skips)
        self.assertLess(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 10)

    def test_max_positions_and_one_copy_per_card(self):
        for pid in range(1, 16):  # 15 cheap squeezes: only 10 slots
            squeeze_card(self.world, pid, 25 + pid / 10, 40, squeeze_from=30)
        self.world.save()
        conn = self.replay(rules.shift(START, 31), rules.shift(START, 39))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
                         config.MAX_OPEN_POSITIONS)
        skips = {r for (r,) in conn.execute("SELECT DISTINCT skip_reason FROM signals")}
        self.assertTrue({"max_positions", "already_held"} <= skips)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades WHERE side = 'buy'")
                         .fetchone()[0], config.MAX_OPEN_POSITIONS)

    def test_position_limit_is_20_percent_of_bankroll(self):
        conn = engine.init_ledger(self.ledger_db)
        engine.open_account(conn, "t", START, starting_cash=500.0)  # limit $100
        squeeze_card(self.world, 1, 150, 40, squeeze_from=30)
        self.world.save()
        meta = panel_mod.Metadata.load(self.prices_db)
        panel = panel_mod.load_panel(self.prices_db, rules.shift(START, 39), START)
        ctx = rules.build_context(panel, meta)
        engine.run_step(conn, "t", panel, ctx, meta)
        self.assertEqual(conn.execute("SELECT skip_reason FROM signals").fetchone()[0],
                         "over_position_limit")

    def test_step_is_idempotent(self):
        squeeze_card(self.world, 1, 50, 40, squeeze_from=30)
        self.world.save()
        conn = self.replay(rules.shift(START, 31), rules.shift(START, 39))
        before = {t: self.rows(conn, t) for t in ("trades", "positions", "equity_history", "signals",
                                                 "accounts", "benchmark_holdings")}
        meta = panel_mod.Metadata.load(self.prices_db)
        panel = panel_mod.load_panel(self.prices_db, rules.shift(START, 39), START)
        ctx = rules.build_context(panel, meta)
        for day in (rules.shift(START, 39), rules.shift(START, 35)):
            result = engine.run_step(conn, "t", panel.upto(day), ctx, meta)
            self.assertEqual(result["status"], "already_done")
        after = {t: self.rows(conn, t) for t in before}
        self.assertEqual(before, after)

    def test_no_lookahead(self):
        for pid in range(1, 4):
            squeeze_card(self.world, pid, 40 + pid * 10, 70, squeeze_from=30 + pid)
        self.world.product(9)
        self.world.series(9, START, [100.0] * 43 + wiggle(70, 14) + [72.0, 73.0, 74.0] +
                          [90.0] * 10)
        self.world.save()
        cut = rules.shift(START, 59)
        truncated = os.path.join(self.tmp.name, "truncated.db")
        shutil.copy(self.prices_db, truncated)
        with sqlite3.connect(truncated) as conn:
            conn.execute("DELETE FROM printing_prices WHERE date > ?", (cut,))
            conn.execute("DELETE FROM fetch_log WHERE date > ?", (cut,))
        full = self.replay(rules.shift(START, 30), cut)
        short = self.replay(rules.shift(START, 30), cut, prices_db=truncated,
                            ledger_db=os.path.join(self.tmp.name, "short.db"))
        for table in ("trades", "positions", "equity_history", "signals", "benchmark_holdings"):
            self.assertEqual(self.rows(full, table), self.rows(short, table), table)
        self.assertGreater(full.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)

    def test_no_trades_on_day_without_fetch(self):
        squeeze_card(self.world, 1, 50, 40, squeeze_from=30)
        self.world.save()
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("DELETE FROM fetch_log WHERE date = ?", (rules.shift(START, 32),))
        conn = self.replay(rules.shift(START, 32), rules.shift(START, 33))
        days = conn.execute("SELECT date, fetched FROM equity_history").fetchall()
        self.assertEqual(days, [(rules.shift(START, 32), 0), (rules.shift(START, 33), 1)])
        self.assertEqual({d for (d,) in conn.execute("SELECT date FROM trades")},
                         {rules.shift(START, 33)})


class ExitTests(unittest.TestCase):
    def test_exit_rules(self):
        self.assertEqual(engine._exit_reason(140, 100, 140, 5)[0], "take_profit")
        self.assertEqual(engine._exit_reason(80, 100, 100, 5)[0], "hard_stop")
        self.assertEqual(engine._exit_reason(110, 100, 130, 5)[0], "trailing_stop")  # 130*0.85=110.5
        self.assertIsNone(engine._exit_reason(112, 100, 130, 5)[0])
        self.assertIsNone(engine._exit_reason(100, 100, 119, 5)[0])  # trail not active below +20%
        self.assertEqual(engine._exit_reason(101, 100, 105, 90)[0], "time_stop")
        self.assertIsNone(engine._exit_reason(101, 100, 105, 89)[0])


class ValuationAndBenchmarkTests(EngineTestBase):
    def test_carry_forward_up_to_7_days_then_zero(self):
        market = np.array([[10.0]] + [[np.nan]] * 7)
        panel = panel_mod.PricePanel(panel_mod.calendar_days(START, rules.shift(START, 7)),
                                     [(1, "Holofoil")], market, market.copy(), np.ones(8, bool))
        self.assertEqual(rules.valuation_prices(panel)[0], 10.0)
        market = np.vstack([market, [[np.nan]]])
        panel = panel_mod.PricePanel(panel_mod.calendar_days(START, rules.shift(START, 8)),
                                     [(1, "Holofoil")], market, market.copy(), np.ones(9, bool))
        self.assertEqual(rules.valuation_prices(panel)[0], 0.0)

    def test_benchmark_is_equal_weight_top_20_fractional(self):
        for pid in range(1, 26):
            self.world.product(pid)
            self.world.series(pid, START, wiggle(20 + pid * 5, 40))
        self.world.save()
        conn = self.replay(rules.shift(START, 39), rules.shift(START, 39))
        held = conn.execute("SELECT product_id, entry_price * shares FROM benchmark_holdings").fetchall()
        self.assertEqual(sorted(p for p, _ in held), list(range(6, 26)))
        for _, value in held:
            self.assertAlmostEqual(value, 50.0)
        self.assertAlmostEqual(conn.execute("SELECT benchmark_total FROM equity_history")
                               .fetchone()[0], 1000.0)


class BacktestWindowTests(unittest.TestCase):
    def test_windows_start_after_warmup_and_split_in_half(self):
        windows = backtest.build_windows("2024-02-08", "2026-11-20")
        self.assertEqual(windows["full"], ("2024-04-07", "2026-11-20"))
        h1, h2 = windows["first_half"], windows["second_half"]
        self.assertEqual(rules.shift(h1[1], 1), h2[0])
        self.assertEqual(windows["2024"], ("2024-04-07", "2024-12-31"))
        self.assertEqual(windows["2026"], ("2026-01-01", "2026-11-20"))


if __name__ == "__main__":
    unittest.main()
