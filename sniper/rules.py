"""Universe filter, rule B box choice, and the three entry rules.

Every function takes a PricePanel whose last row is the day being decided
(`as_of`) and only looks at rows of that panel, so nothing after as_of can be
used. Windows are calendar days. Prices are never carried forward here.
"""
import math
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from sniper import config


@dataclass
class Signal:
    rule: str
    col: int
    product_id: int
    printing: str
    price: float
    detail: str


@dataclass
class Context:
    """Per-column facts that do not depend on prices."""
    is_single: np.ndarray          # has live metadata and is not sealed
    release: np.ndarray            # set release date (datetime64[D]); NaT if unknown
    group: np.ndarray              # group_id per column (-1 if no metadata)
    boxes: dict = field(default_factory=dict)  # group_id -> box (choose_boxes + panel "col")


def choose_boxes(meta):
    """
    Pick rule B's box for each set: a sealed "Booster Box" (not a Case or
    Code Card), else an "Elite Trainer Box" (not Pokemon Center or Code Card).
    Shortest name wins, then lowest product_id. Sets with neither are absent.
    """
    candidates = {}
    for pid, p in meta.products.items():
        name = p["name"] or ""
        if not p["is_sealed"] or p["group_id"] is None:
            continue
        for kind, phrase, exclude in (("booster_box", config.B_BOX_NAME, config.B_BOX_EXCLUDE),
                                      ("etb", config.B_FALLBACK_NAME, config.B_FALLBACK_EXCLUDE)):
            if phrase in name and not any(x in name for x in exclude):
                candidates.setdefault((p["group_id"], kind), []).append((len(name), pid, name))
    boxes = {}
    for gid in {g for g, _ in candidates}:
        for kind in ("booster_box", "etb"):
            if (gid, kind) in candidates:
                _, pid, name = min(candidates[(gid, kind)])
                boxes[gid] = {"product_id": pid, "name": name, "kind": kind}
                break
    return boxes


def build_context(panel, meta):
    n = len(panel.ids)
    is_single = np.zeros(n, dtype=bool)
    release = np.full(n, np.datetime64("NaT"), dtype="datetime64[D]")
    group = np.full(n, -1)
    for j, (pid, _) in enumerate(panel.ids):
        p = meta.products.get(pid)
        if not p:
            continue
        group[j] = p["group_id"] if p["group_id"] is not None else -1
        is_single[j] = not p["is_sealed"]
        released = (meta.groups.get(p["group_id"]) or {}).get("release_date")
        if released:
            release[j] = np.datetime64(released)
    boxes = {}
    for gid, box in choose_boxes(meta).items():
        col = box_column(panel, box)
        if col is not None:
            boxes[gid] = {**box, "col": col}
    return Context(is_single, release, group, boxes)


def box_column(panel, box):
    """The panel column for a box: its Normal printing, else its first printing."""
    printings = sorted(s for (pid, s) in panel.ids if pid == box["product_id"])
    if not printings:
        return None
    printing = "Normal" if "Normal" in printings else printings[0]
    return panel.col[(box["product_id"], printing)]


# --- window helpers ---------------------------------------------------------

def required_days(n):
    return math.ceil(round(config.WINDOW_MIN_COVERAGE * n, 9))


def last_rows(arr, n):
    """The last n calendar days (rows) of arr, or None if the panel is shorter."""
    return arr[-n:] if arr.shape[0] >= n else None


def valid(win, n=None):
    """Per column: does this window have prices on enough of its days?"""
    n = n or win.shape[0]
    return np.sum(~np.isnan(win), axis=0) >= required_days(n)


def first_observed(win):
    """Per column: the earliest price in the window (NaN if none)."""
    has = ~np.isnan(win)
    idx = np.argmax(has, axis=0)
    out = win[idx, np.arange(win.shape[1])]
    out[~has.any(axis=0)] = np.nan
    return out


def last_observed(win):
    has = ~np.isnan(win)
    idx = win.shape[0] - 1 - np.argmax(has[::-1], axis=0)
    out = win[idx, np.arange(win.shape[1])]
    out[~has.any(axis=0)] = np.nan
    return out


def ffill(win):
    idx = np.where(~np.isnan(win), np.arange(win.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    return win[idx, np.arange(win.shape[1])]


def _quiet(fn, *args, **kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return fn(*args, **kwargs)


# --- universe ---------------------------------------------------------------

def universe_mask(panel, ctx):
    """Columns tradeable on panel.as_of (see spec §4 Universe filter)."""
    m = panel.market
    n_cols = m.shape[1]
    today = m[-1]
    as_of = np.datetime64(panel.as_of)
    mask = ctx.is_single.copy()
    mask &= ~np.isnat(ctx.release) & (
        ctx.release <= as_of - np.timedelta64(config.UNIVERSE_MIN_SET_AGE_DAYS, "D"))
    with np.errstate(invalid="ignore"):
        mask &= (today >= config.UNIVERSE_MIN_PRICE) & (today <= config.UNIVERSE_MAX_PRICE)

    recent = last_rows(m, config.UNIVERSE_RECENT_DAYS)
    if recent is None:
        return np.zeros(n_cols, dtype=bool)
    mask &= np.sum(~np.isnan(recent), axis=0) >= config.UNIVERSE_RECENT_MIN_PRICED

    # Changes in the last N days: a priced day whose price differs from the
    # previous priced day. One extra row supplies the "previous" for day one.
    n = config.UNIVERSE_CHANGE_DAYS
    win = last_rows(m, n + 1)
    if win is None:
        return np.zeros(n_cols, dtype=bool)
    prev = ffill(win)[:-1]
    cur = win[1:]
    with np.errstate(invalid="ignore"):
        changed = ~np.isnan(cur) & ~np.isnan(prev) & (cur != prev)
    mask &= changed.sum(axis=0) >= config.UNIVERSE_MIN_CHANGES
    mask &= valid(cur, n)
    return mask


# --- rules ------------------------------------------------------------------

def rule_a(panel, universe):
    """Floor squeeze: low >= market on 2 straight days, <10% move over 7 days."""
    m, low = panel.market, panel.low
    k = config.A_CONSECUTIVE_DAYS
    days = config.A_STABLE_DAYS
    win = last_rows(m, days + 1)
    if win is None or m.shape[0] < k:
        return []
    with np.errstate(invalid="ignore"):
        squeeze = np.all(low[-k:] >= m[-k:], axis=0)  # NaN compares False
        ref = first_observed(win[:-1])
        move = np.abs(m[-1] / ref - 1)
        fire = universe & squeeze & valid(win) & (move < config.A_MAX_MOVE)
    return [Signal("A", j, *panel.ids[j], float(m[-1, j]),
                   f"low {low[-1, j]:.2f} >= market {m[-1, j]:.2f} for {k} days; "
                   f"{days}-day move {m[-1, j] / ref[j] - 1:+.1%}")
            for j in np.flatnonzero(fire)]


def rule_b(panel, universe, ctx):
    """Sealed lead: box up >=10% over 30 days; its set's top 5 singles with rising medians."""
    m = panel.market
    look = config.B_BOX_LOOKBACK_DAYS
    md = config.B_MEDIAN_DAYS
    off = config.B_MEDIAN_OFFSET_DAYS
    box_win = last_rows(m, look + 1)
    recent = last_rows(m, md)
    earlier_all = last_rows(m, md + off)
    if box_win is None or earlier_all is None:
        return []
    earlier = earlier_all[:md]
    with np.errstate(invalid="ignore"):
        med_now = _quiet(np.nanmedian, recent, axis=0)
        med_then = _quiet(np.nanmedian, earlier, axis=0)
        rising = valid(recent) & valid(earlier) & (med_now > med_then)

    signals = []
    for gid, box in sorted(ctx.boxes.items()):
        bc = box["col"]
        col = box_win[:, bc:bc + 1]
        ref = first_observed(col[:-1])[0]
        now = col[-1, 0]
        if not valid(col)[0] or np.isnan(now) or np.isnan(ref) or now < ref * (1 + config.B_BOX_MIN_RISE):
            continue
        members = np.flatnonzero(universe & (ctx.group == gid))
        top = sorted(members, key=lambda j: (-m[-1, j], panel.ids[j]))[:config.B_TOP_SINGLES]
        for j in top:
            if rising[j]:
                signals.append(Signal(
                    "B", j, *panel.ids[j], float(m[-1, j]),
                    f"box {box['name']} {now / ref - 1:+.1%} over {look} days; "
                    f"{md}-day median {med_now[j]:.2f} > {med_then[j]:.2f} {off} days earlier"))
    return signals


def rule_c(panel, universe):
    """Bottoming: >=20% under the 60-day high, flat +/-5% for 14 days, then +5% in 3 days."""
    m = panel.market
    high_win = last_rows(m, config.C_HIGH_DAYS)
    rise = config.C_RISE_DAYS
    span = last_rows(m, config.C_FLAT_DAYS + rise)
    if high_win is None or span is None:
        return []
    flat = span[:config.C_FLAT_DAYS]
    with np.errstate(invalid="ignore"):
        high = _quiet(np.nanmax, high_win, axis=0)
        mid = _quiet(np.nanmedian, flat, axis=0)
        band = config.C_FLAT_BAND
        is_flat = (_quiet(np.nanmax, flat, axis=0) <= mid * (1 + band)) & \
                  (_quiet(np.nanmin, flat, axis=0) >= mid * (1 - band))
        base = last_observed(flat)
        fire = (universe & valid(high_win) & valid(flat) & is_flat
                & (mid <= high * (1 - config.C_MIN_DRAWDOWN))
                & (m[-1] >= base * (1 + config.C_MIN_RISE)))
    return [Signal("C", j, *panel.ids[j], float(m[-1, j]),
                   f"flat {config.C_FLAT_DAYS} days near {mid[j]:.2f} "
                   f"({mid[j] / high[j] - 1:+.1%} vs {config.C_HIGH_DAYS}-day high {high[j]:.2f}); "
                   f"{m[-1, j] / base[j] - 1:+.1%} over {rise} days")
            for j in np.flatnonzero(fire)]


def compute_signals(panel, ctx):
    """All signals for panel.as_of, in buying order: A, B, C, then higher price."""
    universe = universe_mask(panel, ctx)
    signals = rule_a(panel, universe) + rule_b(panel, universe, ctx) + rule_c(panel, universe)
    rank = {r: i for i, r in enumerate(config.RULE_PRIORITY)}
    signals.sort(key=lambda s: (rank[s.rule], -s.price, s.product_id, s.printing))
    return signals


def benchmark_picks(panel, ctx):
    """The BENCHMARK_SIZE most expensive tradeable cards on panel.as_of."""
    universe = universe_mask(panel, ctx)
    cols = np.flatnonzero(universe)
    return sorted(cols, key=lambda j: (-panel.market[-1, j], panel.ids[j]))[:config.BENCHMARK_SIZE]


def valuation_prices(panel):
    """Last price within VALUATION_CARRY_DAYS (valuation only); 0 beyond that."""
    win = last_rows(panel.market, config.VALUATION_CARRY_DAYS + 1)
    if win is None:
        win = panel.market
    out = last_observed(win)
    return np.nan_to_num(out, nan=0.0)


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def shift(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()
