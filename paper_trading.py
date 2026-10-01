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
    edge dropped     -> model edge now < edge at entry - EXIT_EDGE_DROP
Closed trades are sold at the current market price.

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
from datetime import datetime

import polymarket_signals as ps

STAKE = 1.0
MIN_SCORE_SINGLE = 65
MIN_SCORE_GROUP = 55          # 2+ traders
MIN_EDGE = 0.10
MAX_REALISTIC_EDGE = 0.35
PRICE_RANGE = (0.10, 0.90)
MAX_OPPOSING_SHARE = 0.20
MIN_LIQUIDITY = 20 * STAKE
MAX_NEW_PER_DAY = 20
EXIT_EDGE_DROP = 0.05
EXIT_SOLD_SHARE = 0.5

PAPER_STATE = "paper_trades.json"
PAPER = "paper_trades.csv"
CANDIDATES = "candidates.csv"
NOTIFY = True
PAPER_FIELDS = ["trade_id", "status", "opened", "group", "market", "side", "num_traders",
                "score", "entry_price", "stake", "shares", "entry_model_prob", "entry_edge",
                "current_price", "current_value", "pnl", "current_edge", "traders_sold",
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
    min_score = MIN_SCORE_SINGLE if k == 1 else MIN_SCORE_GROUP
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
    if not os.path.exists(PAPER_STATE):
        return []
    with open(PAPER_STATE, encoding="utf-8") as f:
        return json.load(f)


def save_paper(trades):
    tmp = PAPER_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=1)
    os.replace(tmp, PAPER_STATE)


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
    t["pnl"] = round(float(t["current_value"]) - float(t["stake"]), 4)
    if NOTIFY:
        ps.notify(f"Paper trade closed: {t['pnl']:+.2f} $ ({reason})",
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
        t["pnl"] = round(float(t["current_value"]) - float(t["stake"]), 4)

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

        if sold > len(sizes) / 2:
            close(t, price, f"traders sold ({sold}/{len(sizes)})", stamp)
        elif edge is not None and edge < float(t["entry_edge"]) - EXIT_EDGE_DROP:
            close(t, price, f"edge dropped {float(t['entry_edge']) * 100:+.0f} -> "
                            f"{edge * 100:+.0f}", stamp)


def run(snaps, traders, records, names):
    """snaps: {group: [snapshots today]} from polymarket_signals.main. Returns a summary."""
    now = datetime.now()
    stamp, today = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d")
    model = ps.load_model()
    if not model or "snapshots" not in model:     # only a model trained on training.csv
        return "paper: waiting for model.R to be trained on training.csv"
    trades = load_paper()
    manage_open(trades, stamp)

    # latest snapshot per group / market / side = how many agree right now
    latest = {}
    for group, ss in snaps.items():
        for s in ss:
            latest[group, s["cid"], s["idx"]] = s
    evaluated = [e for (group, _, _), s in latest.items()
                 if (e := evaluate(group, s, traders, records, names))]
    evaluated.sort(key=lambda e: -(e["edge"] or -1))

    held = {(t["condition_id"], t["side"]) for t in trades if t["status"] == "open"}
    opened_today = sum(t["opened"].startswith(today) for t in trades)
    new, rows = [], []
    for e in evaluated:
        key = (e["cid"], e["side"])
        if not e["reasons"] and key in held:
            e["reasons"].append("already holding")
        if not e["reasons"] and opened_today + len(new) >= MAX_NEW_PER_DAY:
            e["reasons"].append("daily limit")
        decision = "APPROVED" if not e["reasons"] else "rejected"
        if decision == "APPROVED":
            held.add(key)
            new.append(e)
        rows.append({"time": stamp, "decision": decision, "reasons": "; ".join(e["reasons"]),
                     "group": e["group"], "market": e["market"]["title"], "side": e["side"],
                     "num_traders": e["k"], "score": round(e["score"], 1),
                     "price": e["price"], "model_prob": e["prob"], "model_edge": e["edge"],
                     "opposing_share": round(e["opp_share"], 3),
                     "liquidity": round(e["state"]["liquidity"]), "traders": e["traders"],
                     "url": f"https://polymarket.com/event/{e['market']['slug']}",
                     "condition_id": e["cid"]})

    for e in new:
        trades.append({
            "trade_id": f"{now:%Y%m%d%H%M}-{len(trades) + 1}", "status": "open",
            "opened": stamp, "group": e["group"], "market": e["market"]["title"],
            "side": e["side"], "num_traders": e["k"], "score": round(e["score"], 1),
            "entry_price": e["price"], "stake": STAKE, "shares": round(STAKE / e["price"], 4),
            "entry_model_prob": e["prob"], "entry_edge": e["edge"],
            "current_price": e["price"], "current_value": STAKE, "pnl": 0.0,
            "current_edge": e["edge"], "traders_sold": f"0/{e['k']}",
            "traders": e["traders"], "url": f"https://polymarket.com/event/{e['market']['slug']}",
            "condition_id": e["cid"], "wallets": ";".join(e["order"]),
            "backer_sizes": json.dumps({w: position_size(w, e["cid"], e["side"])
                                        for w in e["order"]}),
            "raw_values": ps.dump_raw({**e["raw"], "avg_price": e["avg"]}),
        })

    save_paper(trades)             # the record first, then the views
    for e in new:
        if NOTIFY:
            ps.notify(f"Paper trade: $1 on {e['side']} @ {e['price'] * 100:.0f}c "
                      f"(model {e['prob'] * 100:.0f}%, edge {e['edge'] * 100:+.0f})",
                      f"{e['market']['title']}\n{e['group']} - {e['k']} trader(s) - "
                      f"score {e['score']:.0f}")
    write_csv(CANDIDATES, CANDIDATE_FIELDS, rows)
    exported = write_csv(PAPER, PAPER_FIELDS,
                         sorted(trades, key=lambda t: (t["status"] != "open", t["opened"])))
    open_trades = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]
    pnl = sum(float(t["pnl"] or 0) for t in trades)
    return (f"paper: {len(new)} new, {len(open_trades)} open, {len(closed)} closed, "
            f"total P&L ${pnl:+.2f} on ${STAKE * len(trades):.0f} staked"
            + ("" if exported else f" ({PAPER} open elsewhere - export skipped)"))
