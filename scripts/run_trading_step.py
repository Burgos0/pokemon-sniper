"""Run the live paper-trading step for every fetched date not yet traded.

The live ledger is data/sniper.db, account "live". It opens with $1,000 on
the first date it trades (the latest fetched date at that point); later runs
catch up on any fetched dates since the last traded one, in order. Backtest
trades never enter it.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper import config, engine, panel as panel_mod, rules

ACCOUNT = "live"
# Longest look-back any rule needs, in days.
LOOKBACK_DAYS = max(config.C_HIGH_DAYS, config.B_BOX_LOOKBACK_DAYS + 1,
                    config.UNIVERSE_CHANGE_DAYS + 1, config.C_FLAT_DAYS + config.C_RISE_DAYS,
                    config.B_MEDIAN_DAYS + config.B_MEDIAN_OFFSET_DAYS)


def fetched_dates(prices_db):
    with sqlite3.connect(prices_db) as conn:
        return [d for (d,) in conn.execute(
            "SELECT date FROM fetch_log WHERE status = 'ok' ORDER BY date")]


def dates_to_trade(conn, all_dates, until=None):
    """Fetched dates after the ledger's last traded date (or just the latest, if new)."""
    if until:
        all_dates = [d for d in all_dates if d <= until]
    if not all_dates:
        return []
    row = conn.execute("SELECT last_date FROM accounts WHERE account = ?", (ACCOUNT,)).fetchone()
    if row is None:
        return [all_dates[-1]]
    last = row[0]
    return [d for d in all_dates if last is None or d > last]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--until", help="YYYY-MM-DD: trade no later than this date")
    parser.add_argument("--prices-db", default=config.PRICES_DB_PATH)
    parser.add_argument("--ledger-db", default=config.SNIPER_DB_PATH)
    args = parser.parse_args(argv)

    conn = engine.init_ledger(args.ledger_db)
    days = dates_to_trade(conn, fetched_dates(args.prices_db), args.until)
    if not days:
        print("Nothing to trade: no fetched dates after the last traded date.")
        conn.close()
        return 0
    engine.open_account(conn, ACCOUNT, days[0])
    meta = panel_mod.Metadata.load(args.prices_db)
    boxes = rules.choose_boxes(meta)
    panel = panel_mod.load_panel(
        args.prices_db, days[-1], rules.shift(days[0], -(LOOKBACK_DAYS - 1)),
        product_ids=[b["product_id"] for b in boxes.values()],
        ids=engine.ledger_ids(conn, ACCOUNT))
    ctx = rules.build_context(panel, meta)
    engine.record_boxes(conn, ctx, meta)
    for day in panel_mod.calendar_days(days[0], days[-1]):
        result = engine.run_step(conn, ACCOUNT, panel.upto(day), ctx, meta)
        print(result)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
