"""
Paper trading: approve top-trader positions by a fixed rule, place an imaginary $1 on
each approved one, follow it at the real market price and close it by rule.
Run by polymarket_signals.py at the end of every hourly run. No real money, no orders.

Candidates: every market where top traders of a group are on the same side today
(1 trader alone, or 2, 3, ... agreeing - the latest snapshot from training.csv).

Approved only if ALL of these pass:
    score            >= MIN_SCORE_SINGLE (1 trader) / MIN_SCORE_GROUP (2+ traders)
    model edge       >= MIN_EDGE  (model's win probability - current price)
                     <= MAX_REALISTIC_EDGE (bigger = model probably wrong, skip)
    price            within PRICE_RANGE
    opposition       < MAX_OPPOSING_SHARE of top-trader money on the other side
    liquidity        >= MIN_LIQUIDITY in the market
    not already holding this market, and < MAX_NEW_PER_DAY new trades today

Each approved trade: $STAKE at the current price -> shares = STAKE / price.

Closed when the first of these happens:
    market settled   -> paid $1 per share if it won, $0 if it lost
    traders sold     -> more than half of the backing traders cut their position by
                        at least EXIT_SOLD_SHARE since we entered
    edge dropped     -> model edge < edge at entry - EXIT_EDGE_DROP, in EXIT_EDGE_RUNS
                        hourly runs in a row (a brief price dip doesn't sell)
Closed trades are sold at the current market price. At HOLD_ABOVE (90c) or higher there
is no early exit - the trade is held until the market settles.
Some categories have stricter rules on top (GROUP_RULES), e.g. weather.

Files:
    paper_trades.json the record of every paper trade (the source of truth)
    paper_trades.csv  view of it, rewritten every run - open in Excel / PyCharm freely;
                      if it is open (locked) that run just skips the export
    candidates.csv    today's evaluated candidates and why each passed / failed
A Windows notification pops up whenever a paper trade is opened or closed.
"""
import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime

import polymarket_signals as ps

STAKE = 1.0
MIN_SCORE_SINGLE = 55
MIN_SCORE_GROUP = 55          # 2+ traders
MIN_EDGE = 0.10
MAX_REALISTIC_EDGE = 0.35
PRICE_RANGE = (0.10, 0.90)
MAX_OPPOSING_SHARE = 0.20
MIN_LIQUIDITY = 20 * STAKE
MAX_NEW_PER_DAY = 20
EXIT_EDGE_DROP = 0.10        # edge must fall this far below the entry edge ...
EXIT_EDGE_RUNS = 2           # ... in this many hourly runs in a row (ignores brief dips)
EXIT_SOLD_SHARE = 0.5
HOLD_ABOVE = 0.90            # at this price or higher, no early exit: hold to settlement.
# (Near 100c the model's edge falls to ~0 - nothing left to win - so "edge dropped"
#  fired on trades that were already winning, selling them at 97-99c instead of 100c.)
# Stricter rules per category, on top of the ones above.
# WEATHER: single-trader bets on exact temperature bands lost in the first days, and
# weather traders use similar forecasts, so their bets tend to win / lose together.
GROUP_RULES = {
    "WEATHER": {"min_traders": 2, "min_score": 60, "max_open": 4},
}
# Markets on one of these topics move together even across different events (October,
# December and January Fed meetings, "another hike in 2026"...): one open bet per topic.
# Everything else: one open bet per event (one game, one city-day, one speech).
# In priority order: a market tagged with several counts under the first one listed.
CORRELATED_TOPICS = ["fed-rates", "fomc", "interest-rates", "inflation", "cpi", "cpi-release",
                     "jobs-report", "unemployment", "gdp", "recession", "tariffs"]


def topic_of(event_slug):
    """The bet's correlation group: a shared macro topic, or else the event itself."""
    tags = ps.market_tags(event_slug)
    return next(("topic:" + t for t in CORRELATED_TOPICS if t in tags), event_slug)

PAPER_STATE = "paper_trades.json"
PAPER = "paper_trades.csv"
DAILY = "paper_daily.csv"
DASHBOARD = "paper_dashboard.html"
CANDIDATES = "candidates.csv"
NOTIFY = True
PAPER_FIELDS = ["trade_id", "status", "opened", "group", "topic", "event", "market", "side", "num_traders",
                "score", "entry_price", "stake", "shares", "entry_model_prob", "entry_edge",
                "current_price", "current_value", "profit_usd", "current_edge", "edge_low_runs", "traders_sold",
                "closed", "exit_price", "exit_reason", "traders", "url", "condition_id",
                "wallets", "backer_sizes", "raw_values"]
CANDIDATE_FIELDS = ["time", "decision", "reasons", "group", "market", "side", "num_traders",
                    "score", "price", "model_prob", "model_edge", "opposing_share",
                    "liquidity", "traders", "url", "condition_id"]


def position_size(wallet, cid, side):
    """Shares the wallet holds of `side` in market `cid` right now."""
    for p in ps.get(ps.DATA, "/positions", user=wallet, market=cid, sizeThreshold=0):
        if p.get("outcome") == side:
            return float(p.get("size") or 0)
    return 0.0


def evaluate(group, snap, traders, records, names):
    """Score + model view of one candidate, and the reasons it fails (empty = approve)."""
    cid, backers = snap["cid"], snap["backers"]
    state = ps.market_state(cid)
    side = snap["market"]["outcomes"].get(snap["idx"]) or \
        (list(state["prices"]) or ["Yes", "No"])[snap["idx"]]
    price = state["prices"].get(side)
    if price is None or state["closed"]:
        return None
    k, total, opposing = len(backers), sum(backers.values()), snap["opposing"]

    # score: same 14 factors and weights as signals.csv
    per_trader = [ps.trader_parts(traders[w], records[w]) for w in backers]
    parts = {f: sum(p[f] for p in per_trader) / k for f in ps.TRADER_FACTORS}
    parts.update(agreement=ps.agreement_part(k),
                 conviction=ps.log_scale(total, *ps.PROFILES[group]["conviction"]),
                 consensus=total / (total + opposing),
                 **ps.market_parts(group, snap["avg"], state, side))
    score = 100 * sum(ps.WEIGHTS[group][f] * v for f, v in parts.items())

    # model: raw values at this moment, at the price we would pay now
    raw = {**ps.mean_raw([ps.trader_raw(traders[w], records[w]) for w in backers]),
           "num_traders": k, "total_usdc": round(total, 2), "opposing_usdc": round(opposing, 2),
           **ps.market_raw(snap["avg"], state, side, price=price)}
    raw["liquidity_usd"] = state["liquidity"]
    prob, edge = ps.model_prediction(raw, group, price)
    opp_share = opposing / (total + opposing)

    reasons = []
    rules = GROUP_RULES.get(group, {})
    if k < rules.get("min_traders", 1):
        reasons.append(f"{group.lower()} needs {rules['min_traders']}+ traders")
    min_score = max(MIN_SCORE_SINGLE if k == 1 else MIN_SCORE_GROUP, rules.get("min_score", 0))
    if score < min_score:
        reasons.append(f"score {score:.0f} < {min_score}")
    if edge is None:
        reasons.append("no model")
    elif edge < MIN_EDGE:
        reasons.append(f"edge {edge * 100:+.0f} < {MIN_EDGE * 100:.0f}")
    elif edge > MAX_REALISTIC_EDGE:
        reasons.append(f"edge {edge * 100:+.0f} unrealistic")
    if not PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
        reasons.append(f"price {price:.2f} outside range")
    if opp_share >= MAX_OPPOSING_SHARE:
        reasons.append(f"opposed {opp_share:.0%}")
    if state["liquidity"] < MIN_LIQUIDITY:
        reasons.append(f"liquidity ${state['liquidity']:.0f}")

    order = sorted(backers, key=lambda w: -backers[w])
    return {"group": group, "cid": cid, "side": side, "k": k, "score": score, "price": price,
            "prob": prob, "edge": edge, "opp_share": opp_share, "state": state,
            "raw": raw, "avg": snap["avg"], "order": order, "reasons": reasons,
            "market": snap["market"],
            "traders": "; ".join(f"{names.get(w, w[:10])} ${backers[w]:,.0f}" for w in order)}


def load_paper():
    """(trades, daily totals) from paper_trades.json."""
    if not os.path.exists(PAPER_STATE):
        return [], {}
    with open(PAPER_STATE, encoding="utf-8") as f:
        state = json.load(f)
    trades, daily = (state, {}) if isinstance(state, list) else (state["trades"], state["daily"])
    for t in trades:                          # trades saved by an older version
        if "pnl" in t:
            t["profit_usd"] = t.pop("pnl")
        t.setdefault("event", t["url"].rsplit("/", 1)[-1])
        t.setdefault("topic", topic_of(t["event"]))
    return trades, daily


def save_paper(trades, daily):
    tmp = PAPER_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"trades": trades, "daily": daily}, f, indent=1)
    os.replace(tmp, PAPER_STATE)


def daily_rows(trades, daily):
    """One row per day: trades opened / closed, realized profit, total and daily profit."""
    rows, prev_total = [], 0.0
    for day in sorted(daily):
        total = daily[day]
        rows.append({
            "date": day,
            "trades_opened": sum(t["opened"].startswith(day) for t in trades),
            "trades_closed": sum((t.get("closed") or "").startswith(day) for t in trades),
            "realized_profit_usd": round(sum(float(t["profit_usd"]) for t in trades
                                             if (t.get("closed") or "").startswith(day)), 2),
            "total_profit_usd": round(total, 2),
            "daily_profit_usd": round(total - prev_total, 2),
        })
        prev_total = total
    return rows


def write_dashboard(trades, days):
    """paper_dashboard.html - reloads itself every minute in the browser."""
    from html import escape
    total = sum(float(t["profit_usd"] or 0) for t in trades)
    staked = sum(float(t["stake"]) for t in trades)
    closed = [t for t in trades if t["status"] == "closed"]
    wins = sum(float(t["profit_usd"]) > 0 for t in closed)
    today = days[-1]["daily_profit_usd"] if days else 0.0

    def money(x):
        x = float(x or 0)
        return f'<span class="{"pos" if x > 0 else "neg" if x < 0 else ""}">{x:+.2f}</span>'

    trade_rows = "".join(
        f"<tr><td>{escape(t['status'])}</td><td>{escape(t['opened'])}</td>"
        f"<td>{escape(t['group'])}</td><td><a href='{escape(t['url'])}'>{escape(t['market'])}</a></td>"
        f"<td>{escape(t['side'])}</td><td>{t['num_traders']}</td>"
        f"<td>{float(t['entry_price']) * 100:.0f}c</td><td>{float(t['current_price']) * 100:.0f}c</td>"
        f"<td>{float(t['current_edge'] or 0) * 100:+.0f}</td><td>{escape(str(t['traders_sold']))}</td>"
        f"<td>{money(t['profit_usd'])}</td><td>{escape(t.get('exit_reason') or '')}</td></tr>"
        for t in sorted(trades, key=lambda t: (t["status"] != "open", t["opened"]), reverse=False))
    day_rows = "".join(
        f"<tr><td>{d['date']}</td><td>{d['trades_opened']}</td><td>{d['trades_closed']}</td>"
        f"<td>{money(d['realized_profit_usd'])}</td><td>{money(d['daily_profit_usd'])}</td>"
        f"<td>{money(d['total_profit_usd'])}</td></tr>" for d in reversed(days))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><title>Paper trades</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#e4e4e4; --pos:#0a7d32; --neg:#c62828; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#eee; --muted:#999;
  --line:#333; --pos:#4cc777; --neg:#ff6b6b; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px system-ui,sans-serif; margin:24px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.sub {{ color:var(--muted); margin-bottom:16px; }}
.tiles {{ display:flex; gap:24px; flex-wrap:wrap; }} .tile b {{ display:block; font-size:22px; }}
table {{ border-collapse:collapse; width:100%; }} th, td {{ padding:6px 8px;
  border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; }} a {{ color:inherit; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Paper trades</h1>
<div class="sub">$1 imaginary bets - updated {datetime.now():%Y-%m-%d %H:%M} - page reloads every minute</div>
<div class="tiles">
 <div class="tile">Total profit<b>{money(total)} $</b></div>
 <div class="tile">Today<b>{money(today)} $</b></div>
 <div class="tile">Staked<b>${staked:.0f}</b></div>
 <div class="tile">Return<b>{(total / staked * 100 if staked else 0):+.1f}%</b></div>
 <div class="tile">Open / closed<b>{len(trades) - len(closed)} / {len(closed)}</b></div>
 <div class="tile">Closed won<b>{wins}/{len(closed)}</b></div>
</div>
<h2>Daily profit</h2><div class="wrap"><table>
<tr><th>Date</th><th>Opened</th><th>Closed</th><th>Realized $</th><th>Daily profit $</th><th>Total $</th></tr>
{day_rows}</table></div>
<h2>Trades</h2><div class="wrap"><table>
<tr><th>Status</th><th>Opened</th><th>Group</th><th>Market</th><th>Side</th><th>Traders</th>
<th>Bought</th><th>Now</th><th>Edge</th><th>Sold</th><th>Profit $</th><th>Exit</th></tr>
{trade_rows}</table></div></body></html>"""
    tmp = DASHBOARD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, DASHBOARD)


def write_csv(path, fields, rows):
    """Export a view; if the file is open in Excel (locked), skip it this run."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(tmp, path)
        return True
    except PermissionError:
        os.remove(tmp)
        return False


def close(t, price, reason, stamp):
    t.update(status="closed", closed=stamp, exit_price=round(price, 4), exit_reason=reason,
             current_price=round(price, 4))
    t["current_value"] = round(float(t["shares"]) * price, 4)
    t["profit_usd"] = round(float(t["current_value"]) - float(t["stake"]), 4)
    if NOTIFY:
        ps.notify(f"Paper trade closed: {t['profit_usd']:+.2f} $ ({reason})",
                  f"{t['market']} -> {t['side']}\n"
                  f"bought {float(t['entry_price']) * 100:.0f}c, sold {price * 100:.0f}c")


def manage_open(trades, stamp):
    """Update open trades at the real market price; close them by rule."""
    for t in trades:
        if t["status"] != "open":
            continue
        state = ps.market_state(t["condition_id"])
        price = state["prices"].get(t["side"])
        if price is None:
            continue
        if state["closed"]:
            close(t, 1.0 if price >= 0.99 else 0.0, "market settled", stamp)
            continue
        t["current_price"] = round(price, 4)
        t["current_value"] = round(float(t["shares"]) * price, 4)
        t["profit_usd"] = round(float(t["current_value"]) - float(t["stake"]), 4)

        raw = json.loads(t["raw_values"])
        avg = float(raw.get("avg_price") or t["entry_price"])
        raw.update(ps.market_raw(avg, state, t["side"], price=price))
        raw["liquidity_usd"] = state["liquidity"]
        _, edge = ps.model_prediction(raw, t["group"], price)
        if edge is not None:
            t["current_edge"] = edge

        sizes = json.loads(t["backer_sizes"])
        sold = sum(1 for w, size in sizes.items()
                   if size > 0 and position_size(w, t["condition_id"], t["side"])
                   <= size * (1 - EXIT_SOLD_SHARE))
        t["traders_sold"] = f"{sold}/{len(sizes)}"

        if price >= HOLD_ABOVE:
            t["edge_low_runs"] = 0             # nearly won: hold to settlement
        elif sold > len(sizes) / 2:
            close(t, price, f"traders sold ({sold}/{len(sizes)})", stamp)
        elif edge is not None and edge < float(t["entry_edge"]) - EXIT_EDGE_DROP:
            t["edge_low_runs"] = int(t.get("edge_low_runs") or 0) + 1
            if t["edge_low_runs"] >= EXIT_EDGE_RUNS:
                close(t, price, f"edge dropped {float(t['entry_edge']) * 100:+.0f} -> "
                                f"{edge * 100:+.0f} ({t['edge_low_runs']} runs)", stamp)
        else:
            t["edge_low_runs"] = 0             # recovered: start counting again


def run(snaps, traders, records, names):
    """snaps: {group: [snapshots today]} from polymarket_signals.main. Returns a summary."""
    now = datetime.now()
    stamp, today = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d")
    model = ps.load_model()
    if not model or "snapshots" not in model:     # only a model trained on training.csv
        return "paper: waiting for model.R to be trained on training.csv"
    trades, daily = load_paper()
    manage_open(trades, stamp)

    # latest snapshot per group / market / side = how many agree right now
    latest = {}
    for group, ss in snaps.items():
        for s in ss:
            latest[group, s["cid"], s["idx"]] = s
    evaluated = [e for (group, _, _), s in latest.items()
                 if (e := evaluate(group, s, traders, records, names))]
    evaluated.sort(key=lambda e: -(e["edge"] or -1))       # best edge first

    # one open bet per event / correlated topic (see CORRELATED_TOPICS)
    held_topics = {t["topic"] for t in trades if t["status"] == "open"}
    opened_today = sum(t["opened"].startswith(today) for t in trades)
    open_in_group = defaultdict(int)
    for t in trades:
        if t["status"] == "open":
            open_in_group[t["group"]] += 1
    new, rows = [], []
    for e in evaluated:
        event = e["market"]["slug"]
        topic = topic_of(event)
        if not e["reasons"] and topic in held_topics:
            e["reasons"].append("already betting on this "
                                + ("topic" if topic.startswith("topic:") else "event"))
        if not e["reasons"] and opened_today + len(new) >= MAX_NEW_PER_DAY:
            e["reasons"].append("daily limit")
        max_open = GROUP_RULES.get(e["group"], {}).get("max_open")
        if not e["reasons"] and max_open is not None and \
                open_in_group.get(e["group"], 0) >= max_open:
            e["reasons"].append(f"{max_open} {e['group'].lower()} bets already open")
        decision = "APPROVED" if not e["reasons"] else "rejected"
        if decision == "APPROVED":
            held_topics.add(topic)
            open_in_group[e["group"]] += 1
            new.append(e)
        rows.append({"time": stamp, "decision": decision, "reasons": "; ".join(e["reasons"]),
                     "group": e["group"], "market": e["market"]["title"], "side": e["side"],
                     "num_traders": e["k"], "score": round(e["score"], 1),
                     "price": e["price"], "model_prob": e["prob"], "model_edge": e["edge"],
                     "opposing_share": round(e["opp_share"], 3),
                     "liquidity": round(e["state"]["liquidity"]), "traders": e["traders"],
                     "url": f"https://polymarket.com/event/{event}",
                     "condition_id": e["cid"]})

    for e in new:
        trades.append({
            "trade_id": f"{now:%Y%m%d%H%M}-{len(trades) + 1}", "status": "open",
            "opened": stamp, "group": e["group"], "event": e["market"]["slug"],
            "topic": topic_of(e["market"]["slug"]),
            "market": e["market"]["title"],
            "side": e["side"], "num_traders": e["k"], "score": round(e["score"], 1),
            "entry_price": e["price"], "stake": STAKE, "shares": round(STAKE / e["price"], 4),
            "entry_model_prob": e["prob"], "entry_edge": e["edge"],
            "current_price": e["price"], "current_value": STAKE, "profit_usd": 0.0,
            "current_edge": e["edge"], "traders_sold": f"0/{e['k']}",
            "traders": e["traders"], "url": f"https://polymarket.com/event/{e['market']['slug']}",
            "condition_id": e["cid"], "wallets": ";".join(e["order"]),
            "backer_sizes": json.dumps({w: position_size(w, e["cid"], e["side"])
                                        for w in e["order"]}),
            "raw_values": ps.dump_raw({**e["raw"], "avg_price": e["avg"]}),
        })

    total = sum(float(t["profit_usd"] or 0) for t in trades)
    daily[today] = round(total, 4)           # total profit (closed + open at market) today
    save_paper(trades, daily)                # the record first, then the views
    for e in new:
        if NOTIFY:
            ps.notify(f"Paper trade: $1 on {e['side']} @ {e['price'] * 100:.0f}c "
                      f"(model {e['prob'] * 100:.0f}%, edge {e['edge'] * 100:+.0f})",
                      f"{e['market']['title']}\n{e['group']} - {e['k']} trader(s) - "
                      f"score {e['score']:.0f}")
    days = daily_rows(trades, daily)
    write_csv(CANDIDATES, CANDIDATE_FIELDS, rows)
    write_csv(DAILY, list(days[0]) if days else ["date"], days)
    exported = write_csv(PAPER, PAPER_FIELDS,
                         sorted(trades, key=lambda t: (t["status"] != "open", t["opened"])))
    write_dashboard(trades, days)
    open_trades = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]
    return (f"paper: {len(new)} new, {len(open_trades)} open, {len(closed)} closed, "
            f"total P&L ${total:+.2f} on ${STAKE * len(trades):.0f} staked"
            + ("" if exported else f" ({PAPER} open elsewhere - export skipped)"))
