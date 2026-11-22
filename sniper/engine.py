"""Paper-trading engine: one idempotent trading step per account per date.

A step on date D, given a PricePanel ending at D:
  1. (fetch days only) sets up the benchmark on the account's first fetch day,
     sells positions that hit an exit, then buys signals in priority order;
  2. values cash, positions and benchmark at each holding's last known market
     price (flagged stale when older than VALUATION_CARRY_DAYS) and records the
     day in equity_history.
Buys execute at the higher of D's market price and cheapest listing (nothing
was for sale below the listing); sells execute at D's market price.
Re-running a processed date is a no-op.
"""
import os
import sqlite3

import numpy as np

from sniper import config, rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    last_date TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    account TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    printing TEXT NOT NULL,
    buy_date TEXT NOT NULL,
    buy_price REAL NOT NULL,
    peak_price REAL NOT NULL,
    rule TEXT NOT NULL,
    last_price REAL NOT NULL,       -- latest market price, for valuation
    last_price_date TEXT NOT NULL,
    PRIMARY KEY (account, product_id, printing)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    printing TEXT NOT NULL,
    card TEXT,
    set_name TEXT,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price REAL NOT NULL,
    rule TEXT NOT NULL,
    reason TEXT,
    buy_date TEXT,
    buy_price REAL,
    return_pct REAL,
    hold_days INTEGER
);
CREATE TABLE IF NOT EXISTS equity_history (
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    fetched INTEGER NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total REAL NOT NULL,
    benchmark_total REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    stale_positions INTEGER NOT NULL,
    stale_benchmark INTEGER NOT NULL,
    PRIMARY KEY (account, date)
);
CREATE TABLE IF NOT EXISTS signals (
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    printing TEXT NOT NULL,
    rule TEXT NOT NULL,
    price REAL NOT NULL,
    bought INTEGER NOT NULL,
    skip_reason TEXT,
    detail TEXT,
    PRIMARY KEY (account, date, product_id, printing, rule)
);
CREATE TABLE IF NOT EXISTS benchmark_holdings (
    account TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    printing TEXT NOT NULL,
    start_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    shares REAL NOT NULL,
    last_price REAL NOT NULL,
    last_price_date TEXT NOT NULL,
    PRIMARY KEY (account, product_id, printing)
);
CREATE TABLE IF NOT EXISTS rule_b_boxes (
    group_id INTEGER PRIMARY KEY,
    set_name TEXT,
    product_id INTEGER NOT NULL,
    box_name TEXT NOT NULL,
    kind TEXT NOT NULL
);
"""


def init_ledger(db_path):
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def open_account(conn, account, start_date, starting_cash=config.STARTING_BANKROLL):
    """Create an account with a fresh bankroll (no-op if it exists)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (account, start_date, starting_cash, cash) "
            "VALUES (?, ?, ?, ?)", (account, start_date, starting_cash, starting_cash))


def record_boxes(conn, ctx, meta):
    """Log which product rule B uses as each set's box."""
    with conn:
        conn.execute("DELETE FROM rule_b_boxes")
        conn.executemany(
            "INSERT INTO rule_b_boxes VALUES (?, ?, ?, ?, ?)",
            [(gid, (meta.groups.get(gid) or {}).get("name"), b["product_id"], b["name"], b["kind"])
             for gid, b in sorted(ctx.boxes.items())])


def ledger_ids(conn, account):
    """Identities the account holds or benchmarks (so a panel can include them)."""
    return [tuple(r) for r in conn.execute(
        "SELECT product_id, printing FROM positions WHERE account = ? UNION "
        "SELECT product_id, printing FROM benchmark_holdings WHERE account = ?",
        (account, account))]


def _price(panel, key, prices):
    j = panel.col.get(key)
    return np.nan if j is None else prices[j]


def _exit_reason(price, buy, peak, held_days):
    eps = 1e-9  # 140 / 100 - 1 is 0.3999... in floating point
    r = price / buy - 1
    if r >= config.TAKE_PROFIT - eps:
        return "take_profit", f"{r:+.1%} >= +{config.TAKE_PROFIT:.0%}"
    if r <= config.HARD_STOP + eps:
        return "hard_stop", f"{r:+.1%} <= {config.HARD_STOP:.0%}"
    if (peak / buy - 1 >= config.TRAIL_ACTIVATE - eps
            and price / peak - 1 <= -config.TRAIL_DROP + eps):
        return "trailing_stop", f"{price / peak - 1:+.1%} from peak {peak:.2f} ({peak / buy - 1:+.1%})"
    if held_days >= config.TIME_STOP_DAYS:
        return "time_stop", f"held {held_days} days ({r:+.1%})"
    return None, None


def run_step(conn, account, panel, ctx, meta, signals=None):
    """
    Run one trading step for `account` on panel.as_of. Returns a summary dict.
    `signals` may be passed in (the backtest computes them once per day for all
    accounts); otherwise they are computed from the panel.
    """
    as_of = panel.as_of
    row = conn.execute("SELECT start_date, starting_cash, cash, last_date FROM accounts "
                       "WHERE account = ?", (account,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown account {account!r}")
    start, starting_cash, cash, last = row
    if as_of < start:
        raise ValueError(f"{as_of} is before {account} starts ({start})")
    if last is not None and as_of <= last:
        return {"date": as_of, "status": "already_done"}

    today = panel.market[-1]
    today_low = panel.low[-1]
    fetched = bool(panel.fetched[-1])
    bought = sold = 0
    max_position = starting_cash * config.MAX_POSITION_FRACTION
    with conn:
        if fetched:
            # Latest market price of every holding, for valuation.
            for table in ("positions", "benchmark_holdings"):
                for pid, printing in conn.execute(
                        f"SELECT product_id, printing FROM {table} WHERE account = ?",
                        (account,)).fetchall():
                    price = _price(panel, (pid, printing), today)
                    if not np.isnan(price):
                        conn.execute(
                            f"UPDATE {table} SET last_price = ?, last_price_date = ? "
                            "WHERE account = ? AND product_id = ? AND printing = ?",
                            (float(price), as_of, account, pid, printing))

            # Benchmark: set up on the account's first fetch day with a universe.
            # It buys like the bot does, at the higher of market and listing.
            if not conn.execute("SELECT 1 FROM benchmark_holdings WHERE account = ? LIMIT 1",
                                (account,)).fetchone():
                picks = rules.benchmark_picks(panel, ctx)
                each = starting_cash / len(picks) if picks else 0
                rows = []
                for j in picks:
                    entry = rules.buy_price(today[j], today_low[j])
                    rows.append((account, *panel.ids[j], as_of, entry, each / entry,
                                 float(today[j]), as_of))
                conn.executemany(
                    "INSERT INTO benchmark_holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

            # Exits, at today's market price.
            sold_today = set()
            for pid, printing, buy_date, buy_price, peak, rule in conn.execute(
                    "SELECT product_id, printing, buy_date, buy_price, peak_price, rule "
                    "FROM positions WHERE account = ? ORDER BY product_id, printing",
                    (account,)).fetchall():
                price = _price(panel, (pid, printing), today)
                if np.isnan(price):
                    continue  # no price today: nothing can trade
                peak = max(peak, price)
                held_days = rules.days_between(buy_date, as_of)
                reason, detail = _exit_reason(price, buy_price, peak, held_days)
                if reason is None:
                    conn.execute("UPDATE positions SET peak_price = ? WHERE account = ? AND "
                                 "product_id = ? AND printing = ?", (peak, account, pid, printing))
                    continue
                cash += price
                conn.execute(
                    "INSERT INTO trades (account, date, product_id, printing, card, set_name, side, "
                    "price, rule, reason, buy_date, buy_price, return_pct, hold_days) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?)",
                    (account, as_of, pid, printing, meta.card_name(pid), meta.set_name(pid),
                     float(price), rule, f"{reason}: {detail}", buy_date, buy_price,
                     float(price / buy_price - 1), held_days))
                conn.execute("DELETE FROM positions WHERE account = ? AND product_id = ? "
                             "AND printing = ?", (account, pid, printing))
                sold_today.add((pid, printing))
                sold += 1

            # Entries, in priority order.
            if signals is None:
                signals = rules.compute_signals(panel, ctx)
            held = {tuple(r) for r in conn.execute(
                "SELECT product_id, printing FROM positions WHERE account = ?", (account,))}
            per_set = {}
            for pid, _ in held:
                gid = (meta.products.get(pid) or {}).get("group_id")
                per_set[gid] = per_set.get(gid, 0) + 1
            bought_by = {}
            for s in signals:
                key = (s.product_id, s.printing)
                j = panel.col[key]
                market = float(today[j])
                price = rules.buy_price(today[j], today_low[j])
                gid = (meta.products.get(s.product_id) or {}).get("group_id")
                if key in bought_by:
                    skip = f"bought_by_rule_{bought_by[key]}"
                elif key in held:
                    skip = "already_held"
                elif key in sold_today:
                    skip = "sold_today"
                elif len(held) >= config.MAX_OPEN_POSITIONS:
                    skip = "max_positions"
                elif per_set.get(gid, 0) >= config.MAX_POSITIONS_PER_SET:
                    skip = "max_per_set"
                elif price > max_position:
                    skip = "over_position_limit"
                elif price > cash:
                    skip = "insufficient_cash"
                else:
                    skip = None
                    cash -= price
                    held.add(key)
                    per_set[gid] = per_set.get(gid, 0) + 1
                    bought_by[key] = s.rule
                    bought += 1
                    conn.execute(
                        "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (account, s.product_id, s.printing, as_of, price, market, s.rule,
                         market, as_of))
                    conn.execute(
                        "INSERT INTO trades (account, date, product_id, printing, card, set_name, "
                        "side, price, rule, reason) VALUES (?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?)",
                        (account, as_of, s.product_id, s.printing, meta.card_name(s.product_id),
                         meta.set_name(s.product_id), price, s.rule,
                         f"{s.detail}; paid {price:.2f} (market {market:.2f})"))
                conn.execute("INSERT INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (account, as_of, s.product_id, s.printing, s.rule, price,
                              int(skip is None), skip, s.detail))
            if cash < -1e-9:
                raise AssertionError(f"cash went negative: {cash}")

        # Valuation: last known market price; stale if older than the carry limit.
        def stale(price_date):
            return rules.days_between(price_date, as_of) > config.VALUATION_CARRY_DAYS

        positions = conn.execute("SELECT last_price, last_price_date FROM positions "
                                 "WHERE account = ?", (account,)).fetchall()
        bench = conn.execute("SELECT shares, last_price, last_price_date FROM benchmark_holdings "
                             "WHERE account = ?", (account,)).fetchall()
        positions_value = float(sum(p for p, _ in positions))
        bench_total = float(sum(sh * p for sh, p, _ in bench)) if bench else starting_cash
        conn.execute(
            "INSERT INTO equity_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account, as_of, int(fetched), cash, positions_value, cash + positions_value,
             bench_total, len(positions), sum(stale(d) for _, d in positions),
             sum(stale(d) for _, _, d in bench)))
        conn.execute("UPDATE accounts SET cash = ?, last_date = ? WHERE account = ?",
                     (cash, as_of, account))
    return {"date": as_of, "status": "ok" if fetched else "no_fetch", "bought": bought,
            "sold": sold, "cash": cash, "total": cash + positions_value,
            "benchmark": bench_total}
