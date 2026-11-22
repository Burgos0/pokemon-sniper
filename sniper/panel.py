"""Daily price panel: one row per calendar day, one column per card.

Identity is (product_id, printing). A panel only ever holds prices dated on or
before its `as_of` (its last row); `upto()` cuts a longer panel down to an
earlier day, so a replay can never see a later price.
"""
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from sniper import config


def calendar_days(start, end):
    """ISO dates from start to end inclusive."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


class PricePanel:
    def __init__(self, dates, ids, market, low, fetched):
        self.dates = list(dates)
        self.ids = list(ids)
        self.market = market
        self.low = low
        self.fetched = fetched
        self.col = {key: j for j, key in enumerate(self.ids)}

    @property
    def as_of(self):
        return self.dates[-1]

    def upto(self, as_of):
        """The same panel ending at as_of (array views, no copies)."""
        t = self.dates.index(as_of)
        return PricePanel(self.dates[:t + 1], self.ids, self.market[:t + 1],
                          self.low[:t + 1], self.fetched[:t + 1])


class Metadata:
    """Live product and set metadata (names, sealed flag, release dates)."""

    def __init__(self, products, groups):
        self.products = products  # product_id -> dict(name, group_id, is_sealed)
        self.groups = groups      # group_id -> dict(name, release_date)

    @classmethod
    def load(cls, db_path):
        with sqlite3.connect(db_path) as conn:
            products = {
                pid: {"name": name, "group_id": gid, "is_sealed": bool(sealed)}
                for pid, name, gid, sealed in conn.execute(
                    "SELECT product_id, name, group_id, is_sealed FROM products")}
            groups = {
                gid: {"name": name, "release_date": (release or "")[:10] or None}
                for gid, name, release in conn.execute(
                    "SELECT group_id, name, release_date FROM groups")}
        return cls(products, groups)

    def card_name(self, product_id):
        return (self.products.get(product_id) or {}).get("name")

    def set_name(self, product_id):
        gid = (self.products.get(product_id) or {}).get("group_id")
        return (self.groups.get(gid) or {}).get("name")


def load_panel(db_path, as_of, start, product_ids=(), ids=()):
    """
    Load a panel of market and low prices dated start..as_of.

    Columns: every single with live metadata whose market price was within the
    universe range on some day in start..as_of, every printing of
    `product_ids` (rule B boxes), and every identity in `ids` (open positions,
    benchmark holdings). The as_of cutoff is applied in SQL.
    """
    if not as_of:
        raise ValueError("as_of is required")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TEMP TABLE want (product_id INTEGER, printing TEXT, "
                     "PRIMARY KEY (product_id, printing))")
        conn.execute(
            """
            INSERT OR IGNORE INTO want
            SELECT DISTINCT p.product_id, p.printing FROM printing_prices p
            JOIN products r ON r.product_id = p.product_id AND r.is_sealed = 0
            WHERE p.date BETWEEN ? AND ? AND p.market_price BETWEEN ? AND ?
            """,
            (start, as_of, config.UNIVERSE_MIN_PRICE, config.UNIVERSE_MAX_PRICE))
        conn.execute("CREATE TEMP TABLE want_products (product_id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO want_products VALUES (?)",
                         [(int(p),) for p in product_ids])
        conn.executemany("INSERT OR IGNORE INTO want VALUES (?, ?)", [(int(p), s) for p, s in ids])
        df = pd.read_sql(
            """
            SELECT p.date, p.product_id, p.printing, p.market_price, p.low_price
            FROM printing_prices p
            WHERE p.date BETWEEN ? AND ?
              AND (p.product_id IN (SELECT product_id FROM want_products)
                   OR EXISTS (SELECT 1 FROM want w WHERE w.product_id = p.product_id
                              AND w.printing = p.printing))
            """, conn, params=(start, as_of))
        fetched_dates = {d for (d,) in conn.execute(
            "SELECT date FROM fetch_log WHERE status = 'ok' AND date BETWEEN ? AND ?",
            (start, as_of))}

    dates = calendar_days(start, as_of)
    ids_sorted = sorted(set(zip(df["product_id"].astype(int), df["printing"])))
    col = {key: j for j, key in enumerate(ids_sorted)}
    row = {d: i for i, d in enumerate(dates)}
    market = np.full((len(dates), len(ids_sorted)), np.nan)
    low = np.full_like(market, np.nan)
    if not df.empty:
        r = df["date"].map(row).to_numpy()
        c = np.array([col[k] for k in zip(df["product_id"].astype(int), df["printing"])])
        market[r, c] = df["market_price"].to_numpy(dtype=float)
        low[r, c] = df["low_price"].to_numpy(dtype=float)
    fetched = np.array([d in fetched_dates for d in dates])
    return PricePanel(dates, ids_sorted, market, low, fetched)
