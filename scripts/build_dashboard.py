"""Build the one-page dashboard: site/index.html and site/dashboard.json.

Reads the live ledger (data/sniper.db, account "live"), the fetch log in
data/prices.db, and the frozen backtest results. The page is static: its data
is embedded in index.html, so it opens straight from disk or GitHub Pages.
A live ledger that does not exist yet is shown as $1,000 in cash, day 0.
"""
import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper import config

ACCOUNT = "live"
WINDOW_LABELS = {"full": "Full period", "first_half": "First half", "second_half": "Second half",
                 "2024": "2024", "2025": "2025", "2026": "2026"}
SKIP_LABELS = {"already_held": "Already held", "max_positions": "10 positions open",
               "max_per_set": "2 already held from this set", "insufficient_cash": "Not enough cash",
               "over_position_limit": "Over $200 position limit", "sold_today": "Sold today"}


# --- data -------------------------------------------------------------------

def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_live(ledger_db):
    start = config.STARTING_BANKROLL
    empty = {"started": False, "start_date": None, "last_date": None, "days_live": 0,
             "cash": start, "positions_value": 0.0, "total": start, "benchmark_total": start,
             "equity": [], "open_positions": [], "closed_trades": [], "signals": [],
             "scoreboard": []}
    if not os.path.exists(ledger_db):
        return empty
    with sqlite3.connect(ledger_db) as conn:
        try:
            acct = conn.execute("SELECT start_date, last_date FROM accounts WHERE account = ?",
                                (ACCOUNT,)).fetchone()
        except sqlite3.OperationalError:
            return empty
        if not acct or not acct[1]:
            return empty
        equity = _rows(conn, "SELECT date, cash, positions_value, total, benchmark_total, "
                       "stale_positions FROM equity_history WHERE account = ? ORDER BY date",
                       (ACCOUNT,))
        last = equity[-1]
        positions = _rows(conn, "SELECT product_id, printing, buy_date, buy_price, last_price, "
                          "last_price_date, rule FROM positions WHERE account = ? "
                          "ORDER BY buy_date, product_id", (ACCOUNT,))
        names = {(r["product_id"]): r for r in _rows(
            conn, "SELECT product_id, card, set_name FROM trades WHERE account = ? AND side = 'buy'",
            (ACCOUNT,))}
        for p in positions:
            n = names.get(p["product_id"], {})
            p["card"], p["set_name"] = n.get("card"), n.get("set_name")
            p["change"] = p["last_price"] / p["buy_price"] - 1
            p["days_held"] = (date.fromisoformat(acct[1]) - date.fromisoformat(p["buy_date"])).days
            p["stale"] = (date.fromisoformat(acct[1]) - date.fromisoformat(p["last_price_date"])).days \
                > config.VALUATION_CARRY_DAYS
        closed = _rows(conn, "SELECT product_id, printing, card, set_name, buy_date, buy_price, "
                       "date AS sell_date, price AS sell_price, return_pct, hold_days, rule, reason "
                       "FROM trades WHERE account = ? AND side = 'sell' ORDER BY date DESC, id DESC",
                       (ACCOUNT,))
        signals = _rows(conn, "SELECT s.product_id, s.printing, s.rule, s.price, s.bought, "
                        "s.skip_reason, s.detail FROM signals s WHERE s.account = ? AND s.date = ? "
                        "ORDER BY s.bought DESC, s.rule, s.price DESC", (ACCOUNT, acct[1]))
    scoreboard = []
    for rule in config.RULE_PRIORITY:
        rets = [t["return_pct"] for t in closed if t["rule"] == rule]
        scoreboard.append({"rule": rule, "trades": len(rets),
                           "open": sum(p["rule"] == rule for p in positions),
                           "win_rate": sum(r > 0 for r in rets) / len(rets) if rets else None,
                           "avg_return": sum(rets) / len(rets) if rets else None})
    return {"started": True, "start_date": acct[0], "last_date": acct[1], "days_live": len(equity),
            "cash": last["cash"], "positions_value": last["positions_value"], "total": last["total"],
            "benchmark_total": last["benchmark_total"],
            "equity": [[e["date"], round(e["total"], 2), round(e["benchmark_total"], 2)]
                       for e in equity],
            "open_positions": positions, "closed_trades": closed, "signals": signals,
            "scoreboard": scoreboard}


def load_names(prices_db, product_ids):
    """Card and set names for signal rows (signals store IDs only)."""
    if not product_ids or not os.path.exists(prices_db):
        return {}
    with sqlite3.connect(prices_db) as conn:
        marks = ",".join("?" * len(product_ids))
        return {pid: (name, set_name) for pid, name, set_name in conn.execute(
            f"SELECT p.product_id, p.name, g.name FROM products p LEFT JOIN groups g "
            f"ON g.group_id = p.group_id WHERE p.product_id IN ({marks})", list(product_ids))}


def load_health(prices_db):
    if not os.path.exists(prices_db):
        return None
    with sqlite3.connect(prices_db) as conn:
        row = conn.execute(
            "SELECT date, source, rows_total, rows_stored, groups, failed_groups, fetched_at "
            "FROM fetch_log WHERE status = 'ok' ORDER BY date DESC LIMIT 1").fetchone()
        problems = _rows(conn, "SELECT date, status, note FROM fetch_log WHERE status != 'ok' "
                         "ORDER BY date DESC LIMIT 5")
    if not row:
        return None
    failed = json.loads(row[5]) if row[5] else []
    return {"last_fetch_date": row[0], "source": row[1], "rows_fetched": row[2],
            "rows_stored": row[3], "sets": row[4], "failed_sets": failed, "fetched_at": row[6],
            "recent_problems": problems}


def load_backtest(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def build_payload(ledger_db, prices_db, backtest_path, built_at):
    live = load_live(ledger_db)
    names = load_names(prices_db, {s["product_id"] for s in live["signals"]})
    for s in live["signals"]:
        s["card"], s["set_name"] = names.get(s["product_id"], (None, None))
    return {"built_at": built_at, "starting_bankroll": config.STARTING_BANKROLL,
            "live": live, "health": load_health(prices_db),
            "backtest": load_backtest(backtest_path)}


# --- rendering --------------------------------------------------------------

def esc(value):
    return html.escape("" if value is None else str(value))


def money(x):
    return "–" if x is None else f"${x:,.2f}"


def pct(x, signed=True):
    if x is None:
        return "–"
    return f"{x:+.1%}" if signed else f"{x:.0%}"


def tone(x):
    if x is None or abs(x) < 1e-12:
        return ""
    return ' class="up"' if x > 0 else ' class="down"'


def card_link(pid, name):
    url = config.TCGPLAYER_PRODUCT_URL.format(product_id=int(pid))
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(name or f"Product {pid}")}</a>'


def table(headers, rows, empty, numeric=()):
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f'<th{" class=num" if i in numeric else ""}>{esc(h)}</th>'
                   for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f'<td{" class=num" if i in numeric else ""}>{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_live(p):
    live = p["live"]
    start = p["starting_bankroll"]
    ret = live["total"] / start - 1
    bench = live["benchmark_total"] / start - 1
    status = (f"Live since {esc(live['start_date'])}" if live["started"]
              else "Not started yet: the live ledger begins at $1,000 on the first trading day.")
    stats = [("Total value", money(live["total"]), ""), ("Cash", money(live["cash"]), ""),
             ("Total return", pct(ret), tone(ret)), ("Benchmark return", pct(bench), tone(bench)),
             ("Days live", str(live["days_live"]), ""),
             ("Last update", esc(live["last_date"] or "–"), "")]
    header = "".join(f'<div class="stat"><div class="label">{esc(k)}</div>'
                     f'<div class="value"{t}>{v}</div></div>' for k, v, t in stats)

    chart = ('<div class="chart"><canvas id="equity" aria-label="Bot vs benchmark value"></canvas></div>'
             if live["equity"] else
             '<p class="empty">The equity curve starts on the first live trading day.</p>')

    open_rows = [[card_link(x["product_id"], x["card"]), esc(x["set_name"]), esc(x["printing"]),
                  esc(x["buy_date"]), money(x["buy_price"]),
                  money(x["last_price"]) + (' <span class="tag">stale</span>' if x["stale"] else ""),
                  f'<span{tone(x["change"])}>{pct(x["change"])}</span>', str(x["days_held"]),
                  esc(x["rule"])] for x in live["open_positions"]]
    closed_rows = [[card_link(x["product_id"], x["card"]), esc(x["set_name"]), esc(x["printing"]),
                    esc(x["buy_date"]), money(x["buy_price"]), esc(x["sell_date"]),
                    money(x["sell_price"]), f'<span{tone(x["return_pct"])}>{pct(x["return_pct"])}</span>',
                    str(x["hold_days"]), esc(x["rule"]), esc((x["reason"] or "").split(":")[0]
                                                            .replace("_", " "))]
                   for x in live["closed_trades"]]
    signal_rows = [[card_link(x["product_id"], x["card"]), esc(x["set_name"]), esc(x["printing"]),
                    esc(x["rule"]), money(x["price"]),
                    '<span class="tag buy">Bought</span>' if x["bought"] else
                    esc(SKIP_LABELS.get(x["skip_reason"], x["skip_reason"])), esc(x["detail"])]
                   for x in live["signals"]]
    score_rows = [[esc(s["rule"]), str(s["trades"]), str(s["open"]), pct(s["win_rate"], False),
                   f'<span{tone(s["avg_return"])}>{pct(s["avg_return"])}</span>']
                  for s in live["scoreboard"]] or \
                 [[esc(r), "0", "0", "–", "–"] for r in config.RULE_PRIORITY]

    h = p["health"]
    if h:
        failed = ", ".join(map(str, h["failed_sets"])) or "none"
        problems = "; ".join(f"{esc(x['date'])} {esc(x['status'])}" for x in h["recent_problems"])
        health = (f'<dl class="health"><dt>Last fetch</dt><dd>{esc(h["last_fetch_date"])} '
                  f'({esc(h["source"])})</dd><dt>Rows fetched</dt><dd>{h["rows_fetched"]:,} '
                  f'({h["rows_stored"]:,} stored at $5+)</dd><dt>Sets</dt><dd>{h["sets"]}</dd>'
                  f'<dt>Failed sets</dt><dd>{esc(failed)}</dd>'
                  + (f'<dt>Recent fetch problems</dt><dd>{problems}</dd>' if problems else "")
                  + "</dl>")
    else:
        health = '<p class="empty">No price data found.</p>'

    return f"""
<header class="top">
  <h1>Pokémon Sniper</h1>
  <p class="tagline">A bot trading Pokémon cards with $1,000 of pretend money on real daily prices, tracked against simply holding.</p>
  <p class="badge">Paper trading. Returns before fees.</p>
  <p class="status">{status}</p>
  <div class="stats">{header}</div>
</header>
<section><h2>Equity curve</h2><p class="hint">Bot vs equal-weight benchmark of the 20 most expensive tradeable cards, both from $1,000.</p>{chart}</section>
<section><h2>Open positions</h2>{table(
        ["Card", "Set", "Printing", "Bought", "Buy price", "Current", "Change", "Days", "Rule"],
        open_rows, "No open positions.", numeric=(4, 5, 6, 7))}</section>
<section><h2>Closed trades</h2>{table(
        ["Card", "Set", "Printing", "Bought", "Buy price", "Sold", "Sell price", "Return", "Days",
         "Rule", "Exit"], closed_rows, "No closed trades yet.", numeric=(4, 6, 7, 8))}</section>
<section><h2>Today's signals</h2>{table(
        ["Card", "Set", "Printing", "Rule", "Price", "Result", "Why it fired"], signal_rows,
        "No signals today.", numeric=(4,))}</section>
<section><h2>Rule scoreboard</h2>{table(
        ["Rule", "Closed trades", "Open", "Win rate", "Avg return"], score_rows, "",
        numeric=(1, 2, 3, 4))}
<p class="hint">B = sealed lead (set's box up 10%+ in 30 days); C = bottoming (flat after a 20%+ drop, then turning up).</p></section>
<section><h2>Data health</h2>{health}</section>
"""


def render_backtest(bt):
    if not bt:
        return ""
    windows = bt["windows"]
    by_window = [[esc(WINDOW_LABELS.get(k, k)), f"{esc(w['window'][0])} – {esc(w['window'][1])}",
                  f'<span{tone(w["bot_return"])}>{pct(w["bot_return"])}</span>',
                  f'<span{tone(w["benchmark_return"])}>{pct(w["benchmark_return"])}</span>',
                  f'<span{tone(w["excess_return"])}>{w["excess_return"] * 100:+.1f} pts</span>',
                  pct(w["bot_max_drawdown"]), pct(w["benchmark_max_drawdown"])]
                 for k, w in windows.items()]
    rule_tables = []
    for rule in config.RULE_PRIORITY:
        rows = []
        for k, w in windows.items():
            r = w["rules"].get(rule)
            if not r:
                continue
            hold = "–" if r["avg_hold_days"] is None else f"{r['avg_hold_days']:.0f}d"
            rows.append([esc(WINDOW_LABELS.get(k, k)), str(r["closed_trades"]),
                         pct(r["win_rate"], False), pct(r["avg_return"]), pct(r["median_return"]),
                         hold, f'<span{tone(r["avg_return_vs_benchmark"])}>'
                               f'{pct(r["avg_return_vs_benchmark"])}</span>',
                         f'{r["signals_fired"]:,}', str(r["bought"])])
        rule_tables.append(f"<h3>Rule {esc(rule)}</h3>" + table(
            ["Window", "Closed", "Win rate", "Avg return", "Median", "Avg hold", "Avg vs benchmark",
             "Signals", "Bought"], rows, "No data.", numeric=(1, 2, 3, 4, 5, 6, 7, 8)))
    return f"""
<section class="backtest" aria-labelledby="bt-title">
  <h2 id="bt-title">Backtest, not live results</h2>
  <p class="hint">A replay of the frozen rules over archived prices ({esc(windows['full']['window'][0])}
  to {esc(windows['full']['window'][1])}), frozen on {esc(bt.get('frozen_on'))}. Every window starts fresh
  with $1,000 for both the bot and the benchmark. None of these trades are in the live record above.
  About 59 delisted cards in the $20–$200 range are missing from the archive's metadata and are excluded,
  which slightly flatters these results.</p>
  <h3>Bot vs benchmark by window</h3>
  {table(["Window", "Dates", "Bot", "Benchmark", "Bot vs benchmark", "Bot max drawdown",
          "Benchmark max drawdown"], by_window, "No data.", numeric=(2, 3, 4, 5, 6))}
  {"".join(rule_tables)}
  <p class="hint">Avg vs benchmark: each trade's return minus the benchmark's return over the same days.</p>
</section>
"""


CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--text:#16181d;--muted:#5d6470;--line:#e3e6eb;--accent:#d63c2f;
--up:#13804a;--down:#c0352a;--tag:#eef0f3;--bt:#fff8e6;--bt-line:#e8c66a;--bot:#d63c2f;--bench:#5b6b82}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111317;--panel:#1a1d23;
--text:#e9ebef;--muted:#9aa3b1;--line:#2b3039;--up:#4cc483;--down:#ff7b6e;--tag:#262a32;--bt:#231f12;
--bt-line:#7a6326;--bot:#ff6b5e;--bench:#9fb0c8}}
:root[data-theme="dark"]{--bg:#111317;--panel:#1a1d23;--text:#e9ebef;--muted:#9aa3b1;--line:#2b3039;
--up:#4cc483;--down:#ff7b6e;--tag:#262a32;--bt:#231f12;--bt-line:#7a6326;--bot:#ff6b5e;--bench:#9fb0c8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{margin:0;font-size:1.6rem;letter-spacing:-.01em}
h2{margin:0 0 6px;font-size:1.15rem}
h3{margin:18px 0 6px;font-size:1rem}
section,.top{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}
.tagline{margin:4px 0 0;font-size:1rem}
.badge{display:inline-block;margin:8px 0 0;padding:2px 10px;border-radius:999px;background:var(--accent);
color:#fff;font-size:.85rem;font-weight:600}
.status,.hint{color:var(--muted);margin:6px 0 10px;font-size:.9rem}
.tagline,.status,.hint,.health dd,footer{overflow-wrap:anywhere}
main,section,.top,.scroll{min-width:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.label{color:var(--muted);font-size:.8rem}
.value{font-size:1.3rem;font-weight:650;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:.8rem}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td:last-child{white-space:normal;min-width:160px}
a{color:inherit;text-decoration-color:var(--muted)}
.empty{color:var(--muted);font-style:italic;margin:6px 0}
.tag{display:inline-block;padding:0 6px;border-radius:6px;background:var(--tag);font-size:.75rem}
.tag.buy{background:var(--up);color:#fff}
.chart{position:relative;height:280px}
.health{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}
.health dt{color:var(--muted)}.health dd{margin:0}
.backtest{background:var(--bt);border:2px dashed var(--bt-line)}
.backtest h2{text-transform:uppercase;letter-spacing:.04em}
footer{color:var(--muted);font-size:.8rem;text-align:center;padding:8px 0 24px}
"""

CHART_JS = """
(function(){
  var data = JSON.parse(document.getElementById('dashboard-data').textContent);
  var eq = data.live.equity, el = document.getElementById('equity');
  if (!eq.length || !el || !window.Chart) return;
  var css = getComputedStyle(document.documentElement);
  var bot = css.getPropertyValue('--bot').trim(), bench = css.getPropertyValue('--bench').trim();
  new Chart(el, {type: 'line', data: {labels: eq.map(function(r){return r[0];}), datasets: [
    {label: 'Bot', data: eq.map(function(r){return r[1];}), borderColor: bot, pointRadius: 0, borderWidth: 2},
    {label: 'Benchmark', data: eq.map(function(r){return r[2];}), borderColor: bench, pointRadius: 0, borderWidth: 2}]},
    options: {maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
      scales: {y: {ticks: {callback: function(v){return '$' + v;}}}}}});
})();
"""


def render_html(payload):
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pokémon Sniper</title>
<meta name="description" content="A Pokémon card paper-trading bot on daily TCGplayer prices. Paper trading; returns before fees.">
<style>{CSS}</style>
</head>
<body>
<main>
{render_live(payload)}
{render_backtest(payload["backtest"])}
<footer>Built {esc(payload["built_at"])} · Prices from TCGCSV (TCGplayer market prices) · Paper trading, no real purchases · <a href="{esc(config.REPO_URL)}">Source on GitHub</a></footer>
</main>
<script id="dashboard-data" type="application/json">{data}</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>{CHART_JS}</script>
</body>
</html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-db", default=config.SNIPER_DB_PATH)
    parser.add_argument("--prices-db", default=config.PRICES_DB_PATH)
    parser.add_argument("--backtest", default=config.BACKTEST_FROZEN_PATH)
    parser.add_argument("--out", default=config.SITE_DIR)
    args = parser.parse_args(argv)
    from datetime import datetime, timezone
    payload = build_payload(args.ledger_db, args.prices_db, args.backtest,
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "dashboard.json"), "w") as f:
        json.dump(payload, f, indent=2, default=str)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(render_html(payload))
    print(f"Wrote {args.out}/index.html and {args.out}/dashboard.json "
          f"(live started: {payload['live']['started']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
