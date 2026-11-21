"""Price-history helpers copied from Burgos0/Yugioh-Prices-Fetcher-2 app/analysis.py.

Every reader takes an `as_of` date and never returns a price dated after it.
Identity is always (product_id, printing).
"""
import math
import sqlite3
from datetime import timedelta

import pandas as pd

from sniper import config

PRICE_COLUMNS = "date, product_id, printing, low_price, mid_price, market_price, direct_low_price"


def load_printing_prices(db_path, as_of, start=None):
    """
    Load printing_prices rows dated <= as_of (and >= start if given), with
    `date` parsed to datetimes. The cutoff is applied in SQL, before any
    other processing, so later rows can never leak into a replay.
    """
    if not as_of:
        raise ValueError("as_of is required")
    query = f"SELECT {PRICE_COLUMNS} FROM printing_prices WHERE date <= ?"
    params = [as_of]
    if start:
        query += " AND date >= ?"
        params.append(start)
    try:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql(query, conn, params=params)
    except (OSError, sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _select_weekly_mover_windows(df):
    """Return the recent and baseline snapshot dates for weekly movers."""
    snapshot_dates = sorted(df["date"].dropna().unique())
    if len(snapshot_dates) < 9:
        return None
    return snapshot_dates[-3:], snapshot_dates[-9:-6]


def detect_spike(product_id, baseline_value, current_value, df, latest_date, recent_dates=None,
                 printing=None, baseline_dates=None):
    """
    Detect if a price increase is CONFIRMED or a suspicious UNCONFIRMED spike.

    Returns "UNCONFIRMED" if:
    - Latest raw price is >50% above the recent 3-day median, OR
    - Fewer than 2 of the last 3 daily prices are elevated (>=10% above baseline)
    and, when baseline_dates is given, also on reversion, single-sale steps,
    a listing floor that did not follow, or a thin market.

    Otherwise returns "CONFIRMED".

    Args:
        product_id: Card ID
        baseline_value: 3-day median from ~7 days ago
        current_value: 3-day median from recent days
        df: DataFrame of price rows (callers pass data already cut at as_of)
        latest_date: Most recent date being evaluated
    """
    recent_dates = recent_dates if recent_dates is not None else [
        latest_date - timedelta(days=2), latest_date - timedelta(days=1), latest_date
    ]

    # RULE 1: Latest raw price more than 50% above recent 3-day median?
    identity = (df["product_id"] == product_id)
    if printing is not None:
        identity &= df["printing"] == printing
    latest_price_row = df[
        identity &
        (df["date"] == latest_date) &
        (df["market_price"].notna())
    ]

    if not latest_price_row.empty:
        latest_raw_price = latest_price_row.iloc[0]["market_price"]
        if latest_raw_price > current_value * config.SPIKE_LATEST_VS_MEDIAN_MAX:
            return "UNCONFIRMED"

    # RULE 2: Persistence check - require 2+ of last 3 prices elevated (>= 10% above baseline)
    recent_prices = df[
        identity &
        (df["date"].isin(recent_dates)) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"].values

    if len(recent_prices) >= 2:
        elevated_count = sum(1 for p in recent_prices
                             if p >= baseline_value * config.SPIKE_ELEVATED_RATIO)
        if elevated_count < 2:
            return "UNCONFIRMED"

    # Stricter rules below need the baseline window; older callers skip them.
    if baseline_dates is None:
        return "CONFIRMED"

    # RULE 3: Reversion - latest price has already fallen back to baseline.
    if not latest_price_row.empty and latest_raw_price <= baseline_value * config.REVERSION_TOLERANCE:
        return "UNCONFIRMED"

    # RULE 4: Single-sale step - TCGplayer market price only updates on sales,
    # so a flat line that steps up exactly once is one sale, not a trend.
    window = df[
        identity &
        (df["date"] >= min(baseline_dates)) &
        (df["date"] <= latest_date) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"]
    price_changes = int((window.diff().fillna(0).abs() > 0.005).sum())
    if price_changes < config.MIN_PRICE_CHANGES:
        return "UNCONFIRMED"

    # RULE 5: Listing floor - if the cheapest listing didn't move up with the
    # market price, buyers can still get the card near the old price.
    if "low_price" in df.columns:
        base_low = df[identity & df["date"].isin(baseline_dates)]["low_price"].dropna()
        cur_low = df[identity & df["date"].isin(recent_dates)]["low_price"].dropna()
        if not base_low.empty and not cur_low.empty:
            base_low, cur_low = base_low.median(), cur_low.median()
            floor_rose = cur_low >= base_low * config.FLOOR_RISE_MIN
            floor_supports = cur_low >= current_value * config.FLOOR_SUPPORT_RATIO
            if not (floor_rose or floor_supports):
                return "UNCONFIRMED"

            # RULE 6: Liquidity - when the cheapest listing sits far above the
            # market price (a week ago or now), sales are too rare for the
            # market price to mean anything. Catches thin collector cards.
            if base_low > baseline_value * config.MAX_LISTING_TO_MARKET:
                return "UNCONFIRMED"
            if cur_low > current_value * config.MAX_LISTING_TO_MARKET:
                return "UNCONFIRMED"

    return "CONFIRMED"


def evaluate_horizon_outcome(prices_conn, product_id, printing, start_date, start_price, horizon,
                              max_date, jump_percent, jump_dollars):
    """
    Look up the exact-date outcome for one (product_id, printing)/start_price/horizon.

    This deliberately reads a FUTURE price (start_date + horizon) and is for
    evaluating past signals only; max_date caps it at the data that exists.

    Returns:
        (status, price, pct_return, hit) where status is "ok", "pending", or
        "unavailable"; price/pct_return/hit are None unless status == "ok".
    """
    target_date = start_date + timedelta(days=horizon)
    target_date_str = target_date.strftime("%Y-%m-%d")

    if not start_price or not math.isfinite(start_price) or start_price <= 0:
        return "unavailable", None, None, None
    if max_date is None or target_date > max_date:
        return "pending", None, None, None

    row = prices_conn.execute(
        "SELECT market_price FROM printing_prices WHERE product_id = ? AND printing = ? AND date = ?",
        (product_id, printing, target_date_str)
    ).fetchone()
    price = row[0] if row and row[0] is not None else None

    if price is None or not math.isfinite(price) or price <= 0:
        return "unavailable", None, None, None

    pct_return = ((price - start_price) / start_price) * 100
    hit = pct_return >= jump_percent and price - start_price >= jump_dollars
    return "ok", price, pct_return, hit
