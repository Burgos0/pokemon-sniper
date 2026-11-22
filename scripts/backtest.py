"""Replay the trading engine day by day over the backfilled history.

Each evaluation window (first half, second half, each calendar year, and the
full period for reference) is its own account in data/backtest.db and starts
fresh with $1,000 for both the bot and the benchmark. Signals for a day are
computed once, from prices dated on or before that day, and fed to every
window open on that day. Thresholds come from sniper/config.py unchanged.

Writes data/backtest_report.json and data/backtest_report.md.
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper import config, engine, panel as panel_mod, rules

REPORT_JSON = "data/backtest_report{suffix}.json"
REPORT_MD = "data/backtest_report{suffix}.md"


def fetch_range(prices_db):
    with sqlite3.connect(prices_db) as conn:
        return conn.execute(
            "SELECT MIN(date), MAX(date) FROM fetch_log WHERE status = 'ok'").fetchone()


def build_windows(data_start, end, warmup=config.BACKTEST_WARMUP_DAYS):
    """Evaluation windows as {name: (first_day, last_day)}."""
    start = rules.shift(data_start, warmup - 1)
    days = panel_mod.calendar_days(start, end)
    half = len(days) // 2
    windows = {"full": (start, end),
               "first_half": (days[0], days[half - 1]),
               "second_half": (days[half], days[-1])}
    for year in range(int(start[:4]), int(end[:4]) + 1):
        first, last = max(start, f"{year}-01-01"), min(end, f"{year}-12-31")
        if first <= last:
            windows[str(year)] = (first, last)
    return windows


def run(prices_db, ledger_db, windows, only_rules=None, log=print):
    data_start = fetch_range(prices_db)[0]
    end = max(last for _, last in windows.values())
    meta = panel_mod.Metadata.load(prices_db)
    boxes = rules.choose_boxes(meta)
    t0 = time.time()
    panel = panel_mod.load_panel(prices_db, end, data_start,
                                 product_ids=[b["product_id"] for b in boxes.values()])
    ctx = rules.build_context(panel, meta)
    log(f"Panel: {len(panel.dates)} days x {len(panel.ids)} cards "
        f"({time.time() - t0:.0f}s); {len(ctx.boxes)} sets have a rule B box")

    if os.path.exists(ledger_db):
        os.remove(ledger_db)
    conn = engine.init_ledger(ledger_db)
    engine.record_boxes(conn, ctx, meta)
    for name, (first, _) in windows.items():
        engine.open_account(conn, name, first)

    first_day = min(first for first, _ in windows.values())
    for day in panel_mod.calendar_days(first_day, end):
        view = panel.upto(day)
        signals = rules.compute_signals(view, ctx) if view.fetched[-1] else []
        if only_rules:
            signals = [s for s in signals if s.rule in only_rules]
        for name, (first, last) in windows.items():
            if first <= day <= last:
                engine.run_step(conn, name, view, ctx, meta, signals=signals)
        if day.endswith("-01"):
            log(f"  {day}: {len(signals)} signals ({time.time() - t0:.0f}s)")
    conn.close()


# --- report -----------------------------------------------------------------

def _stats(values):
    return (statistics.mean(values) if values else None,
            statistics.median(values) if values else None)


def window_report(conn, account, first, last):
    equity = conn.execute(
        "SELECT date, total, benchmark_total, fetched, stale_positions, stale_benchmark "
        "FROM equity_history WHERE account = ? ORDER BY date", (account,)).fetchall()
    bench = {d: b for d, _, b, *_ in equity}
    start_total = config.STARTING_BANKROLL
    end_total, end_bench = equity[-1][1], equity[-1][2]

    def drawdown(series):
        peak, worst = series[0], 0.0
        for v in series:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        return worst

    per_rule = {}
    for rule in config.RULE_PRIORITY:
        sells = conn.execute(
            "SELECT buy_date, date, return_pct, hold_days, price - buy_price FROM trades "
            "WHERE account = ? AND side = 'sell' AND rule = ?", (account, rule)).fetchall()
        rets = [r for _, _, r, _, _ in sells]
        rel = [r - (bench[s] / bench[b] - 1) for b, s, r, _, _ in sells]
        avg, med = _stats(rets)
        open_rows = conn.execute(
            "SELECT p.buy_price, e.date FROM positions p JOIN equity_history e "
            "ON e.account = p.account AND e.date = ? WHERE p.account = ? AND p.rule = ?",
            (last, account, rule)).fetchall()
        sig = conn.execute(
            "SELECT bought, skip_reason, COUNT(*) FROM signals WHERE account = ? AND rule = ? "
            "GROUP BY bought, skip_reason", (account, rule)).fetchall()
        fired = sum(c for _, _, c in sig)
        distinct = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT product_id, printing FROM signals "
            "WHERE account = ? AND rule = ?)", (account, rule)).fetchone()[0]
        per_rule[rule] = {
            "closed_trades": len(sells),
            "win_rate": (sum(r > 0 for r in rets) / len(rets)) if rets else None,
            "avg_return": avg, "median_return": med,
            "avg_hold_days": statistics.mean(h for *_, h, _ in sells) if sells else None,
            "avg_return_vs_benchmark": statistics.mean(rel) if rel else None,
            "realized_pnl": sum(p for *_, p in sells),
            "open_at_end": len(open_rows),
            "signals_fired": fired,
            "signals_distinct_cards": distinct,
            "bought": sum(c for b, _, c in sig if b),
            "skipped": {reason: c for b, reason, c in sig if not b},
        }
    exits = Counter(r.split(":")[0] for (r,) in conn.execute(
        "SELECT reason FROM trades WHERE account = ? AND side = 'sell'", (account,)))
    open_value = conn.execute(
        "SELECT positions_value, open_positions FROM equity_history WHERE account = ? AND date = ?",
        (account, last)).fetchone()
    return {
        "window": [first, last],
        "days": len(equity),
        "no_fetch_days": [d for d, _, _, f, _, _ in equity if not f],
        "bot_end": end_total,
        "bot_return": end_total / start_total - 1,
        "benchmark_end": end_bench,
        "benchmark_return": end_bench / start_total - 1,
        "excess_return": end_total / start_total - end_bench / start_total,
        "bot_max_drawdown": drawdown([e[1] for e in equity]),
        "benchmark_max_drawdown": drawdown([e[2] for e in equity]),
        "open_positions_at_end": open_value[1],
        "open_positions_value_at_end": open_value[0],
        "exit_reasons": dict(exits),
        "stale_position_days": sum(e[4] for e in equity),
        "stale_benchmark_days": sum(e[5] for e in equity),
        "rules": per_rule,
    }


def build_report(ledger_db, windows):
    with sqlite3.connect(ledger_db) as conn:
        report = {name: window_report(conn, name, *span) for name, span in windows.items()}
        boxes = conn.execute("SELECT kind, COUNT(*) FROM rule_b_boxes GROUP BY kind").fetchall()
    return {"windows": report, "rule_b_boxes": dict(boxes)}


def _pct(x):
    return "n/a" if x is None else f"{x:+.1%}"


def to_markdown(report):
    out = ["# Backtest report", ""]
    out.append("| Window | Dates | Bot | Benchmark | Bot vs benchmark | Bot max DD | Bench max DD |")
    out.append("|---|---|---|---|---|---|---|")
    for name, w in report["windows"].items():
        out.append(f"| {name} | {w['window'][0]} .. {w['window'][1]} | {_pct(w['bot_return'])} | "
                   f"{_pct(w['benchmark_return'])} | {w['excess_return'] * 100:+.1f} pts | "
                   f"{_pct(w['bot_max_drawdown'])} | {_pct(w['benchmark_max_drawdown'])} |")
    for name, w in report["windows"].items():
        out += ["", f"## {name} ({w['window'][0]} .. {w['window'][1]})", "",
                "| Rule | Closed | Win rate | Avg return | Median | Avg hold | Avg vs benchmark "
                "| Realized P&L | Open at end | Signals (cards) | Bought |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for rule, r in w["rules"].items():
            hold = "n/a" if r["avg_hold_days"] is None else f"{r['avg_hold_days']:.0f}d"
            win = "n/a" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
            out.append(f"| {rule} | {r['closed_trades']} | {win} | {_pct(r['avg_return'])} | "
                       f"{_pct(r['median_return'])} | {hold} | {_pct(r['avg_return_vs_benchmark'])} | "
                       f"${r['realized_pnl']:,.2f} | {r['open_at_end']} | "
                       f"{r['signals_fired']} ({r['signals_distinct_cards']}) | {r['bought']} |")
        out.append("")
        out.append("Skipped signals: " + "; ".join(
            f"{rule}: " + (", ".join(f"{k} {v}" for k, v in sorted(r["skipped"].items())) or "none")
            for rule, r in w["rules"].items()))
        out.append(f"Exit reasons: {w['exit_reasons']}. Open at end: {w['open_positions_at_end']} "
                   f"(${w['open_positions_value_at_end']:,.2f}). No-fetch days: "
                   f"{len(w['no_fetch_days'])}. Stale (last price over "
                   f"{config.VALUATION_CARRY_DAYS} days old, valued at that price) position-days: "
                   f"{w['stale_position_days']}; benchmark holding-days: "
                   f"{w['stale_benchmark_days']}.")
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices-db", default=config.PRICES_DB_PATH)
    parser.add_argument("--ledger-db", default=config.BACKTEST_DB_PATH)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--rules", help="diagnostic: only these rules can buy, e.g. B "
                        "(writes data/backtest_<rules>.db and its own report)")
    args = parser.parse_args(argv)
    suffix = ""
    if args.rules:
        suffix = "_" + args.rules
        if args.ledger_db == config.BACKTEST_DB_PATH:
            args.ledger_db = config.BACKTEST_DB_PATH.replace(".db", f"{suffix}.db")
    data_start, end = fetch_range(args.prices_db)
    windows = build_windows(data_start, end)
    if not args.report_only:
        run(args.prices_db, args.ledger_db, windows, only_rules=set(args.rules or ""))
    report = build_report(args.ledger_db, windows)
    with open(REPORT_JSON.format(suffix=suffix), "w") as f:
        json.dump(report, f, indent=2)
    md = to_markdown(report)
    with open(REPORT_MD.format(suffix=suffix), "w") as f:
        f.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
