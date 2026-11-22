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
        self.groups.setdefault(group, (f"Set {group}", OLD_SET))
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


def bottoming(scale=1.0):
    """60 days of prices on which rule C fires on the last day (index 59)."""
    return [round(p * scale, 2) for p in [100.0] * 43 + wiggle(70, 14) + [72.0, 73.0, 75.0]]


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
    DAY = rules.shift(START, 39)

    def cards(self, prices, group=None, n=40, lows=None):
        """One wiggling card per price; each in its own set unless group is given."""
        for pid, price in prices.items():
            self.world.product(pid, group=group or 100 + pid)
            markets = wiggle(price, n)
            self.world.series(pid, START, markets, lows and [lows.get(pid, 0.9) * m for m in markets])
        self.world.save()

    def step(self, pids, day=None, conn=None, starting_cash=1000.0, last=None):
        """Feed these cards as rule B signals on `day`, then step to `last` with no signals."""
        day = day or self.DAY
        last = last or day
        meta = panel_mod.Metadata.load(self.prices_db)
        panel = panel_mod.load_panel(self.prices_db, last, START)
        ctx = rules.build_context(panel, meta)
        if conn is None:
            conn = engine.init_ledger(self.ledger_db)
            engine.open_account(conn, "t", day, starting_cash)
        view = panel.upto(day)
        signals = [rules.Signal("B", view.col[(p, "Holofoil")], p, "Holofoil",
                                float(view.market[-1, view.col[(p, "Holofoil")]]), "test")
                   for p in pids]
        engine.run_step(conn, "t", view, ctx, meta, signals=signals)
        for d in panel_mod.calendar_days(rules.shift(day, 1), last) if last > day else []:
            engine.run_step(conn, "t", panel.upto(d), ctx, meta, signals=[])
        return conn

    def skips(self, conn):
        return {r for (r,) in conn.execute("SELECT DISTINCT skip_reason FROM signals")}

    def test_buys_pay_higher_of_market_and_listing_and_sells_at_market(self):
        self.world.product(1)
        self.world.product(2)
        # Card 1: cheapest listing 10% above market on the buy day. Card 2: below.
        self.world.series(1, START, wiggle(50, 40) + [80.0], [45.0] * 39 + [55.0, 85.0])
        self.world.series(2, START, wiggle(40, 40) + [60.0], [36.0] * 40 + [61.0])
        self.world.save()
        conn = self.step([1, 2], last=rules.shift(START, 40))
        trades = conn.execute("SELECT date, product_id, side, price FROM trades "
                              "ORDER BY id").fetchall()
        with sqlite3.connect(self.prices_db) as prices:
            for day, pid, side, price in trades:
                low, market = prices.execute(
                    "SELECT low_price, market_price FROM printing_prices WHERE date = ? AND "
                    "product_id = ?", (day, pid)).fetchone()
                self.assertEqual(price, max(market, low) if side == "buy" else market)
        self.assertEqual([(p, s, price) for _, p, s, price in trades],
                         [(1, "buy", 55.0), (2, "buy", 40.4), (1, "sell", 80.0), (2, "sell", 60.0)])

    def test_cash_never_negative(self):
        self.cards({**{p: 190 + p / 10 for p in range(1, 6)},     # ~$955 of cards
                    **{p: 35 + p / 10 for p in range(21, 36)}})  # most unaffordable after
        conn = self.step(list(range(1, 6)) + list(range(21, 36)))
        cash = [c for (c,) in conn.execute("SELECT cash FROM equity_history")]
        self.assertTrue(all(c >= 0 for c in cash))
        self.assertIn("insufficient_cash", self.skips(conn))

    def test_max_positions_and_one_copy_per_card(self):
        self.cards({p: 25 + p / 10 for p in range(1, 16)}, n=41)
        conn = self.step(list(range(1, 16)))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
                         config.MAX_OPEN_POSITIONS)
        self.assertIn("max_positions", self.skips(conn))
        self.step([1, 2], day=rules.shift(self.DAY, 1), conn=conn)
        self.assertIn("already_held", self.skips(conn))

    def test_at_most_two_positions_per_set(self):
        self.cards({p: 30 + p for p in range(1, 5)}, group=7)
        conn = self.step([1, 2, 3, 4])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
                         config.MAX_POSITIONS_PER_SET)
        self.assertEqual(self.skips(conn), {None, "max_per_set"})

    def test_position_limit_is_20_percent_of_bankroll(self):
        self.cards({1: 150})
        conn = self.step([1], starting_cash=500.0)  # limit $100
        self.assertEqual(self.skips(conn), {"over_position_limit"})

    def test_no_trades_on_day_without_fetch(self):
        self.cards({1: 50})
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("DELETE FROM fetch_log WHERE date = ?", (self.DAY,))
        conn = self.step([1])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT fetched FROM equity_history").fetchall(), [(0,)])

    def test_stale_holding_keeps_last_price_and_is_flagged(self):
        self.world.product(1)
        self.world.series(1, START, wiggle(50, 40) + [None] * 10 + [52.0])
        self.world.product(2)
        self.world.series(2, START, wiggle(30, 51))  # keeps the fetch log going
        self.world.save()
        conn = self.step([1], last=rules.shift(START, 50))
        rows = conn.execute("SELECT date, positions_value, stale_positions FROM equity_history "
                            "ORDER BY date").fetchall()
        values = {d: (v, s) for d, v, s in rows}
        self.assertEqual(values[rules.shift(START, 46)], (50.5, 0))  # 7 days old: not stale
        self.assertEqual(values[rules.shift(START, 47)], (50.5, 1))  # 8 days: stale, not $0
        self.assertEqual(values[rules.shift(START, 50)], (52.0, 0))

    def test_step_is_idempotent(self):
        self.world.product(1)
        self.world.series(1, START, bottoming() + wiggle(80, 5))
        self.world.save()
        conn = self.replay(rules.shift(START, 50), rules.shift(START, 64))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
        before = {t: self.rows(conn, t) for t in ("trades", "positions", "equity_history", "signals",
                                                 "accounts", "benchmark_holdings")}
        meta = panel_mod.Metadata.load(self.prices_db)
        panel = panel_mod.load_panel(self.prices_db, rules.shift(START, 64), START)
        ctx = rules.build_context(panel, meta)
        for day in (rules.shift(START, 64), rules.shift(START, 59)):
            result = engine.run_step(conn, "t", panel.upto(day), ctx, meta)
            self.assertEqual(result["status"], "already_done")
        self.assertEqual(before, {t: self.rows(conn, t) for t in before})

    def test_no_lookahead(self):
        for pid in range(1, 4):  # rule C fires on days 59, 61, 63
            self.world.product(pid, group=10 + pid)
            self.world.series(pid, rules.shift(START, 2 * (pid - 1)),
                              bottoming(1 + pid / 10) + [90.0] * 12)
        self.world.save()
        cut = rules.shift(START, 64)
        truncated = os.path.join(self.tmp.name, "truncated.db")
        shutil.copy(self.prices_db, truncated)
        with sqlite3.connect(truncated) as conn:
            conn.execute("DELETE FROM printing_prices WHERE date > ?", (cut,))
            conn.execute("DELETE FROM fetch_log WHERE date > ?", (cut,))
        full = self.replay(rules.shift(START, 50), cut)
        short = self.replay(rules.shift(START, 50), cut, prices_db=truncated,
                            ledger_db=os.path.join(self.tmp.name, "short.db"))
        for table in ("trades", "positions", "equity_history", "signals", "benchmark_holdings"):
            self.assertEqual(self.rows(full, table), self.rows(short, table), table)
        self.assertGreaterEqual(full.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 3)


class ExitTests(unittest.TestCase):
    def test_exit_rules(self):
        self.assertEqual(engine._exit_reason(140, 100, 140, 5)[0], "take_profit")
        self.assertEqual(engine._exit_reason(80, 100, 100, 5)[0], "hard_stop")
        self.assertEqual(engine._exit_reason(110, 100, 130, 5)[0], "trailing_stop")  # 130*0.85=110.5
        self.assertIsNone(engine._exit_reason(112, 100, 130, 5)[0])
        self.assertIsNone(engine._exit_reason(100, 100, 119, 5)[0])  # trail not active below +20%
        self.assertEqual(engine._exit_reason(101, 100, 105, 90)[0], "time_stop")
        self.assertIsNone(engine._exit_reason(101, 100, 105, 89)[0])


class BenchmarkTests(EngineTestBase):
    def test_benchmark_is_equal_weight_top_20_liquid_cards(self):
        for pid in range(1, 26):
            self.world.product(pid)
            self.world.series(pid, START, wiggle(20 + pid * 5, 40))
        # The most expensive card, but priced on only 8 of the last 12 days.
        self.world.product(99)
        self.world.series(99, START, wiggle(190, 35) + [None] * 4 + [190.0])
        self.world.save()
        conn = self.replay(rules.shift(START, 39), rules.shift(START, 39))
        held = conn.execute("SELECT product_id, entry_price * shares FROM benchmark_holdings").fetchall()
        self.assertEqual(sorted(p for p, _ in held), list(range(6, 26)))
        for _, value in held:
            self.assertAlmostEqual(value, 50.0)
        self.assertAlmostEqual(conn.execute("SELECT benchmark_total FROM equity_history")
                               .fetchone()[0], 1000.0)

    def test_benchmark_buys_at_higher_of_market_and_listing(self):
        for pid in range(1, 21):
            self.world.product(pid)
            markets = wiggle(50, 40)
            self.world.series(pid, START, markets, [m * (1.25 if pid == 1 else 0.9) for m in markets])
        self.world.save()
        conn = self.replay(rules.shift(START, 39), rules.shift(START, 39))
        entry, shares = conn.execute("SELECT entry_price, shares FROM benchmark_holdings "
                                     "WHERE product_id = 1").fetchone()
        self.assertAlmostEqual(entry, 50.5 * 1.25)
        self.assertAlmostEqual(shares, 50 / entry)


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
