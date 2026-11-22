"""Run the live paper-trading step for one date (default: latest fetched date).

The live ledger is data/sniper.db, account "live". It starts with a fresh
bankroll on its first date; backtest trades never enter it.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper import config, engine, panel as panel_mod, rules

ACCOUNT = "live"
# Longest look-back any rule or valuation needs, in days.
LOOKBACK_DAYS = max(config.C_HIGH_DAYS, config.B_BOX_LOOKBACK_DAYS + 1,
                    config.UNIVERSE_CHANGE_DAYS + 1, config.VALUATION_CARRY_DAYS + 1)


def latest_fetch_date(prices_db):
    with sqlite3.connect(prices_db) as conn:
        return conn.execute("SELECT MAX(date) FROM fetch_log WHERE status = 'ok'").fetchone()[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD (default: latest fetched date)")
    parser.add_argument("--prices-db", default=config.PRICES_DB_PATH)
    parser.add_argument("--ledger-db", default=config.SNIPER_DB_PATH)
    args = parser.parse_args(argv)

    as_of = args.date or latest_fetch_date(args.prices_db)
    if not as_of:
        print("No fetched prices yet.")
        return 1
    conn = engine.init_ledger(args.ledger_db)
    engine.open_account(conn, ACCOUNT, as_of)
    meta = panel_mod.Metadata.load(args.prices_db)
    ctx_boxes = rules.choose_boxes(meta)
    panel = panel_mod.load_panel(
        args.prices_db, as_of, rules.shift(as_of, -(LOOKBACK_DAYS - 1)),
        product_ids=[b["product_id"] for b in ctx_boxes.values()],
        ids=engine.ledger_ids(conn, ACCOUNT))
    ctx = rules.build_context(panel, meta)
    engine.record_boxes(conn, ctx, meta)
    result = engine.run_step(conn, ACCOUNT, panel, ctx, meta)
    conn.close()
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
