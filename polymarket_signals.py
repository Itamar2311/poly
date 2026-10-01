"""
Find markets where several top Polymarket traders bet the same way today, and score them.

Runs one check per group below. Each group takes the top 25 traders (monthly PnL)
from its own leaderboard and only looks at markets in its categories:
    OVERALL   - overall leaderboard, markets in any of the four categories
    WEATHER / ECONOMICS / MENTIONS / SPORTS - that category's leaderboard and markets

For each group:
1. Skip "bot-like" traders (more than MAX_TRADES_PER_DAY trades today).
2. Ignore trades at extreme odds (>= 98% or <= 2%) and markets outside the group.
3. Work out each trader's net direction per market (buy No == sell Yes).
4. Record a signal when at least MIN_TRADERS traders of that group are on the same side.

Scoring: 0-100, the sum of 14 factors. Each factor is rated 0-1 and is worth at most
10 points, so no single factor can dominate. Every group has its own profile in
PROFILES (weights, which market end dates are best, what counts as liquid / a big bet). Trader factors are averaged
over the traders behind the signal; market factors are refreshed on every run.
The "score_parts" column shows how many points each factor gave.

All signals go into signals.csv, highest score first. Running again updates open
signals (current price, score) and fills in result + return once a market settles.
Every trader who has appeared in a signal is kept in traders.csv.
"""
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# --- settings -------------------------------------------------------------
CATEGORY_TAGS = {            # category -> market tags that count
    "WEATHER": {"weather"},
    "ECONOMICS": {"economy", "economics"},
    "MENTIONS": {"mention-markets"},
    "SPORTS": {"sports"},
}
GROUPS = {"OVERALL": set().union(*CATEGORY_TAGS.values()), **CATEGORY_TAGS}
TOP_N = 25
MAX_TRADES_PER_DAY = 200     # more than this -> probably a bot / market maker
EXTREME_ODDS = 0.98          # drop trades priced >= 0.98 or <= 0.02
MIN_USDC_PER_TRADER = 100    # a trader's net bet must be at least this to count
MIN_TRADERS = 2              # how many traders must agree

# scoring: each group has its own profile.
#   weights    - factor -> share of the score (each at most 0.10, together 1.00)
#   timing     - [(up to N days until the market ends, rating 0-1), ...]
#   liquidity  - $ in the order book that rates 0 .. 1 (log scale)
#   conviction - total $ behind the signal that rates 0 .. 1 (log scale)
#
# Factors:
#   trader (averaged over the traders behind the signal)
#     edge         won more often than their entry prices implied (60 days)
#     recent_edge  same, last RECENT_DAYS only - is the edge still working?
#     coin_flip    win rate on bets entered at 40-60c
#     our_record   average return of their past signals in signals.csv
#     win_rate     plain share of bets won (per bet, not per $)
#     experience   number of settled bets - more history, more trust
#     lb_rank      leaderboard rank (1st = full marks)
#     lb_roi       monthly profit / volume on the leaderboard
#   signal / market
#     agreement    how many traders agree (2 / 3 / 4+)
#     conviction   total $ they put in
#     price        current price vs. their average entry (cheaper now = better)
#     consensus    share of the group's $ on this side vs. the opposite side
#     timing       time until the market ends
#     liquidity    money in the market's order book (can we get filled?)
INF = float("inf")
PROFILES = {
    # general: balanced, 1-7 day markets preferred
    "OVERALL": dict(
        weights=dict(edge=.10, recent_edge=.10, coin_flip=.10, our_record=.10, win_rate=.05,
                     experience=.05, lb_rank=.05, lb_roi=.05, agreement=.10, conviction=.05,
                     price=.10, consensus=.05, timing=.05, liquidity=.05),
        timing=[(1, .6), (7, 1.0), (30, .8), (90, .5), (INF, .3)],
        liquidity=(1_000, 100_000), conviction=(100, 50_000)),
    # weather: model-driven, many small same-day bets in thin markets.
    # History and independent agreement matter most; size and liquidity barely.
    "WEATHER": dict(
        weights=dict(edge=.10, recent_edge=.10, coin_flip=.10, our_record=.10, win_rate=.05,
                     experience=.10, lb_rank=.05, lb_roi=.05, agreement=.10, conviction=.03,
                     price=.10, consensus=.05, timing=.05, liquidity=.02),
        timing=[(1, 1.0), (3, .7), (7, .4), (INF, .2)],
        liquidity=(50, 5_000), conviction=(100, 5_000)),
    # sports: prices move fast and books are deep; profitability (ROI) and
    # traders not fighting each other matter more, rank less.
    "SPORTS": dict(
        weights=dict(edge=.10, recent_edge=.10, coin_flip=.10, our_record=.10, win_rate=.05,
                     experience=.05, lb_rank=.03, lb_roi=.07, agreement=.10, conviction=.05,
                     price=.10, consensus=.07, timing=.03, liquidity=.05),
        timing=[(2, 1.0), (7, .8), (30, .5), (INF, .3)],
        liquidity=(5_000, 500_000), conviction=(200, 100_000)),
    # economics: few scheduled events (Fed, CPI) where big informed money counts;
    # weeks-out markets are normal, recent form less telling (few events).
    "ECONOMICS": dict(
        weights=dict(edge=.10, recent_edge=.07, coin_flip=.10, our_record=.10, win_rate=.05,
                     experience=.03, lb_rank=.05, lb_roi=.05, agreement=.10, conviction=.10,
                     price=.10, consensus=.05, timing=.05, liquidity=.05),
        timing=[(1, .6), (30, 1.0), (90, .7), (INF, .4)],
        liquidity=(5_000, 500_000), conviction=(200, 100_000)),
    # mentions: research-driven, settles within days of the speech / event;
    # current form and acting before the event matter.
    "MENTIONS": dict(
        weights=dict(edge=.10, recent_edge=.10, coin_flip=.07, our_record=.10, win_rate=.05,
                     experience=.05, lb_rank=.05, lb_roi=.05, agreement=.10, conviction=.05,
                     price=.10, consensus=.05, timing=.08, liquidity=.05),
        timing=[(1, .9), (7, 1.0), (30, .6), (INF, .3)],
        liquidity=(1_000, 100_000), conviction=(100, 20_000)),
}
for _g, _p in PROFILES.items():
    _w = _p["weights"]
    assert max(_w.values()) <= 0.10, f"{_g}: no factor may be worth more than 10% of the score"
    assert abs(sum(_w.values()) - 1) < 1e-9, f"{_g}: weights add up to {sum(_w.values()):.2f}, not 1"
assert set(PROFILES) == set(GROUPS), "every group needs a scoring profile"
TRADER_FACTORS = ["edge", "recent_edge", "coin_flip", "our_record", "win_rate",
                  "experience", "lb_rank", "lb_roi"]
MARKET_FACTORS = ["price", "timing", "liquidity"]   # refreshed every run

LOOKBACK_DAYS = 60           # trader history window
RECENT_DAYS = 14             # window for recent_edge
COIN_FLIP = (0.40, 0.60)     # entry prices that count as a 50/50 bet
PRIOR_BETS = 10              # smoothing: pretend every record starts with 10 average bets
EDGE_SCALE = 2               # edge of +25% (wins 25 points above implied) -> full marks

OUTPUT = "signals.csv"       # every signal, highest score first
LOG = "run_log.txt"          # one line per run, plus any errors
TRADERS = "traders.csv"      # every trader who has appeared in a signal (stats refreshed daily)
# --------------------------------------------------------------------------

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
FIELDS = ["date", "group", "score", "first_seen", "last_updated", "market", "side",
          "num_traders", "total_usdc", "opposing_usdc", "avg_price", "entry_price",
          "current_price", "price_move", "ends", "result", "return", "traders",
          "trader_scores", "score_parts", "url", "condition_id", "wallets"]
TRADER_FIELDS = ["name", "score", "groups", "num_signals", "our_wins", "our_settled",
                 "our_avg_return", "edge", "recent_edge", "wins", "expected_wins", "bets",
                 "recent_wins", "recent_expected", "recent_bets", "cf_wins", "cf_bets",
                 "lb_rank", "lb_pnl", "lb_vol", "first_seen", "last_seen", "stats_date",
                 "wallet"]
INT_FIELDS = {"wins", "bets", "cf_wins", "cf_bets", "recent_wins", "recent_bets"}
FLOAT_FIELDS = {"expected_wins", "recent_expected", "lb_rank", "lb_pnl", "lb_vol"}


def get(base, path, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


_trades = {}


def trades_today(wallet, start_ts):
    """All of a wallet's trades since start_ts (cached, a trader can be in several groups)."""
    if wallet not in _trades:
        out, offset = [], 0
        while True:
            batch = get(DATA, "/activity", user=wallet, type="TRADE", start=start_ts,
                        limit=500, offset=offset)
            out += [t for t in batch if t["timestamp"] >= start_ts]
            if len(batch) < 500:
                break
            offset += 500
        _trades[wallet] = out
    return _trades[wallet]


_tags = {}


def market_tags(event_slug):
    if event_slug not in _tags:
        events = get(GAMMA, "/events", slug=event_slug)
        _tags[event_slug] = {t["slug"].lower() for e in events for t in e.get("tags") or []}
    return _tags[event_slug]


_markets = {}


def market_state(cid):
    """{"closed", "prices": {outcome: price}, "end", "liquidity"}, cached per run."""
    if cid not in _markets:
        closed, found = False, get(GAMMA, "/markets", condition_ids=cid)
        if not found:   # the default listing only has open markets
            closed, found = True, get(GAMMA, "/markets", condition_ids=cid, closed="true")
        m = found[0] if found else {}
        _markets[cid] = {
            "closed": closed and bool(found),
            "prices": dict(zip(json.loads(m.get("outcomes") or "[]"),
                               map(float, json.loads(m.get("outcomePrices") or "[]")))),
            "end": m.get("endDate"),
            "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
        }
    return _markets[cid]


# --- trader history -------------------------------------------------------

def settled_bets(wallet):
    """[(entry_price, won, settled_ts)] for the wallet's settled bets in the last LOOKBACK_DAYS.

    Polymarket keeps winning / sold bets in closed-positions, but losing bets that were
    never cashed out stay in positions (redeemable, price 0), so both are needed.
    """
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp()
    bets = []

    for page in range(40):   # closed-positions: 50 per page, newest first
        batch = get(DATA, "/closed-positions", user=wallet, limit=50, offset=page * 50,
                    sortBy="TIMESTAMP", sortDirection="DESC")
        bets += [(p["avgPrice"], p["realizedPnl"] > 0, p["timestamp"]) for p in batch
                 if p["timestamp"] >= cutoff]
        if len(batch) < 50 or batch[-1]["timestamp"] < cutoff:
            break

    for page in range(10):   # settled but not cashed out
        batch = get(DATA, "/positions", user=wallet, redeemable="true", sizeThreshold=0,
                    limit=500, offset=page * 500)
        for p in batch:
            try:
                ts = datetime.strptime(p["endDate"][:10], "%Y-%m-%d").timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if ts >= cutoff:
                bets.append((p["avgPrice"], p["curPrice"] >= 0.99, ts))
        if len(batch) < 500:
            break

    return [b for b in bets if 1 - EXTREME_ODDS < b[0] < EXTREME_ODDS]


def load_traders():
    """{wallet: row} from traders.csv."""
    if not os.path.exists(TRADERS):
        return {}
    with open(TRADERS, newline="", encoding="utf-8") as f:
        return {r["wallet"]: {k: int(v) if k in INT_FIELDS and v != "" else
                              float(v) if k in FLOAT_FIELDS and v != "" else v
                              for k, v in r.items()} for r in csv.DictReader(f)}


def refresh_stats(traders, wallets):
    """Refresh history stats for these wallets, at most once a day."""
    today = datetime.now().strftime("%Y-%m-%d")
    recent_cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).timestamp()
    for w in wallets:
        t = traders.setdefault(w, {"wallet": w})
        if t.get("stats_date") != today or t.get("recent_expected") in (None, ""):
            bets = settled_bets(w)
            recent = [b for b in bets if b[2] >= recent_cutoff]
            cf = [won for price, won, _ in bets if COIN_FLIP[0] <= price <= COIN_FLIP[1]]
            t.update(stats_date=today,
                     bets=len(bets), wins=sum(b[1] for b in bets),
                     expected_wins=round(sum(b[0] for b in bets), 2),
                     recent_bets=len(recent), recent_wins=sum(b[1] for b in recent),
                     recent_expected=round(sum(b[0] for b in recent), 2),
                     cf_bets=len(cf), cf_wins=sum(cf))


def edge_part(wins, expected, bets):
    return clamp(0.5 + EDGE_SCALE * (wins - expected) / (bets + PRIOR_BETS))


def trader_parts(t, record):
    """Trader factors, each 0-1. record = (wins, settled, sum of returns) from signals."""
    _, settled, total_return = record
    rank = t.get("lb_rank")
    pnl, vol = t.get("lb_pnl"), t.get("lb_vol")
    return {
        "edge": edge_part(t["wins"], t["expected_wins"], t["bets"]),
        "recent_edge": edge_part(t["recent_wins"], t["recent_expected"], t["recent_bets"]),
        "coin_flip": (t["cf_wins"] + PRIOR_BETS / 2) / (t["cf_bets"] + PRIOR_BETS),
        "our_record": clamp(0.5 + total_return / (settled + PRIOR_BETS) / 2),
        "win_rate": (t["wins"] + PRIOR_BETS / 2) / (t["bets"] + PRIOR_BETS),
        "experience": clamp(math.log10(t["bets"] + 1) / 3),         # 1000 bets = full
        "lb_rank": clamp((TOP_N - rank) / (TOP_N - 1)) if rank not in (None, "") else 0.5,
        "lb_roi": clamp(0.5 + 2.5 * pnl / vol) if vol else 0.5,     # 20% ROI = full
    }


def trader_score(t, record, group="OVERALL"):
    """0-100 score for one trader, from the trader factors only (group's weights)."""
    weights = PROFILES[group]["weights"]
    parts = trader_parts(t, record)
    return 100 * sum(weights[k] * v for k, v in parts.items()) / sum(weights[k] for k in parts)


def log_scale(x, lo, hi):
    """0 at lo, 1 at hi, logarithmic in between."""
    return clamp(math.log10(max(x, 1e-9) / lo) / math.log10(hi / lo))


def market_parts(group, avg_price, state, side):
    """Factors that change while the market is open, each 0-1."""
    profile = PROFILES[group]
    price = state["prices"].get(side)
    move = price - avg_price if price is not None and avg_price else 0
    timing = 0.5
    if state["end"]:
        try:
            end = datetime.fromisoformat(state["end"].replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
            timing = next(v for max_days, v in profile["timing"] if days <= max_days)
        except ValueError:
            pass
    return {
        "price": clamp(0.5 - move * 100 / 40),                        # +-20c -> 0 / 1
        "timing": timing,
        "liquidity": log_scale(state["liquidity"], *profile["liquidity"]),
    }


def format_parts(group, parts):
    weights = PROFILES[group]["weights"]
    return "; ".join(f"{k} {100 * weights[k] * parts[k]:.1f}" for k in weights if k in parts)


def parse_parts(group, text):
    """score_parts column -> {factor: 0-1 value}."""
    weights, out = PROFILES[group]["weights"], {}
    for item in filter(None, (text or "").split("; ")):
        k, points = item.rsplit(" ", 1)
        if weights.get(k):
            out[k] = float(points) / (100 * weights[k])
    return out


# --- signals --------------------------------------------------------------

def find_signals(group, allowed_tags, start_ts, names, leaderboard):
    """Return [(market, cid, outcome_index, {wallet: usdc}, avg_price, opposing_usdc)]."""
    traders = get(DATA, "/v1/leaderboard", category=group, timePeriod="MONTH",
                  orderBy="PNL", limit=TOP_N)
    for t in traders:
        w = t["proxyWallet"]
        names[w] = t.get("userName") or w[:10]
        if w not in leaderboard or int(t["rank"]) < leaderboard[w][0]:   # keep best rank
            leaderboard[w] = (int(t["rank"]), t.get("pnl") or 0, t.get("vol") or 0)

    # net[cid][wallet]: + means betting outcome 0 (Yes), - means betting outcome 1 (No)
    net = defaultdict(lambda: defaultdict(float))
    cost = defaultdict(float)     # (cid, wallet, direction) -> $ paid for that side
    shares = defaultdict(float)   # (cid, wallet, direction) -> shares of that side
    markets = {}                  # cid -> {"title", "slug", "outcomes": {index: name}}

    for wallet in (t["proxyWallet"] for t in traders):
        trades = trades_today(wallet, start_ts)
        if len(trades) > MAX_TRADES_PER_DAY:
            continue
        for t in trades:
            if t["price"] >= EXTREME_ODDS or t["price"] <= 1 - EXTREME_ODDS:
                continue
            if not market_tags(t["eventSlug"]) & allowed_tags:
                continue
            cid = t["conditionId"]
            sign = 1 if (t["side"] == "BUY") == (t["outcomeIndex"] == 0) else -1
            net[cid][wallet] += sign * t["usdcSize"]
            # selling the other outcome at p is the same as buying this side at 1-p
            side_price = t["price"] if t["side"] == "BUY" else 1 - t["price"]
            cost[cid, wallet, sign] += side_price * t["size"]
            shares[cid, wallet, sign] += t["size"]
            m = markets.setdefault(cid, {"title": t["title"], "slug": t["eventSlug"],
                                         "outcomes": {}})
            m["outcomes"][t["outcomeIndex"]] = t["outcome"]

    signals = []
    for cid, by_wallet in net.items():
        for direction, idx in ((1, 0), (-1, 1)):
            backers = {w: abs(v) for w, v in by_wallet.items()
                       if v * direction >= MIN_USDC_PER_TRADER}
            if len(backers) >= MIN_TRADERS:
                opposing = sum(-v * direction for v in by_wallet.values() if v * direction < 0)
                c = sum(cost[cid, w, direction] for w in backers)
                s = sum(shares[cid, w, direction] for w in backers)
                signals.append((markets[cid], cid, idx, backers, c / s if s else None, opposing))
    return signals


def update_prices(rows):
    """For every unsettled signal: refresh price, market factors and score, or settle it."""
    for r in rows.values():
        if r.get("result"):
            continue
        state = market_state(r["condition_id"])
        price = state["prices"].get(r["side"])
        if price is None:
            continue
        if not r.get("entry_price"):
            r["entry_price"] = price          # what we could have bought at when first seen
        entry = float(r["entry_price"])
        if state["closed"]:
            won = price >= 0.99
            r["result"] = "won" if won else "lost"
            r["return"] = round((1 - entry) / entry if won else -1.0, 4)
            r["current_price"] = 1 if won else 0
            continue                           # score stays as it was before settling
        avg = float(r["avg_price"]) if r.get("avg_price") else price
        r["current_price"] = price
        r["price_move"] = round(price - avg, 4)
        r["ends"] = (state["end"] or "")[:10]
        group = r["group"]
        parts = parse_parts(group, r.get("score_parts"))
        if not parts:
            continue                           # row from an older version of the script
        parts.update(market_parts(group, avg, state, r["side"]))
        weights = PROFILES[group]["weights"]
        r["score_parts"] = format_parts(group, parts)
        r["score"] = round(100 * sum(weights[k] * v for k, v in parts.items()), 1)


def our_records(rows):
    """{wallet: (wins, settled, sum of returns)} over settled signals in signals.csv."""
    rec = defaultdict(lambda: [0, 0, 0.0])
    for r in rows.values():
        if r.get("result") in ("won", "lost") and r.get("return") not in (None, ""):
            for w in filter(None, (r.get("wallets") or "").split(";")):
                rec[w][0] += r["result"] == "won"
                rec[w][1] += 1
                rec[w][2] += float(r["return"])
    return defaultdict(lambda: (0, 0, 0.0), {w: tuple(v) for w, v in rec.items()})


def edge_text(wins, expected, bets):
    return f"{(wins - expected) / bets:+.1%}" if bets else ""


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))   # so scheduled runs find the files
    now = datetime.now()
    start_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    date, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M")

    rows = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, newline="", encoding="utf-8") as f:
            rows = {(r["date"], r["group"], r["condition_id"], r["side"]): r
                    for r in csv.DictReader(f)}
    update_prices(rows)            # settle finished markets first so records are current
    records = our_records(rows)
    traders = load_traders()

    names, leaderboard = {}, {}
    found = [(group, *s) for group, tags in GROUPS.items()
             for s in find_signals(group, tags, start_ts, names, leaderboard)]
    refresh_stats(traders, {w for _, _, _, _, backers, _, _ in found for w in backers})
    for w, (rank, pnl, vol) in leaderboard.items():
        if w in traders:
            traders[w].update(lb_rank=rank, lb_pnl=round(pnl, 2), lb_vol=round(vol, 2))

    for group, m, cid, idx, backers, avg_price, opposing in found:
        side = m["outcomes"].get(idx, "Yes" if idx == 0 else "No")
        per_trader = {w: trader_parts(traders[w], records[w]) for w in backers}
        total = sum(backers.values())
        parts = {k: sum(p[k] for p in per_trader.values()) / len(per_trader)
                 for k in TRADER_FACTORS}
        parts.update(
            agreement=clamp((len(backers) - 1) / 3),                         # 2/3/4+ traders
            conviction=log_scale(total, *PROFILES[group]["conviction"]),
            consensus=total / (total + opposing),
        )
        order = sorted(backers, key=lambda w: -backers[w])
        key = (date, group, cid, side)
        old = rows.get(key, {})
        rows[key] = {
            "date": date,
            "group": group,
            "first_seen": old.get("first_seen") or stamp,
            "last_updated": stamp,
            "market": m["title"],
            "side": side,
            "num_traders": len(backers),
            "total_usdc": round(total, 2),
            "opposing_usdc": round(opposing, 2),
            "avg_price": round(avg_price, 4) if avg_price else "",
            "entry_price": old.get("entry_price", ""),
            "result": old.get("result", ""),
            "return": old.get("return", ""),
            "traders": "; ".join(f"{names[w]} ${backers[w]:,.0f}" for w in order),
            "trader_scores": "; ".join(
                f"{names[w]} {trader_score(traders[w], records[w], group):.0f} "
                f"(edge {edge_text(traders[w]['wins'], traders[w]['expected_wins'], traders[w]['bets'])}"
                f" over {traders[w]['bets']} bets, "
                f"50/50 {traders[w]['cf_wins']}/{traders[w]['cf_bets']}, "
                f"ours {records[w][0]}/{records[w][1]})" for w in order),
            "score_parts": format_parts(group, parts),   # market factors added by update_prices
            "url": f"https://polymarket.com/event/{m['slug']}",
            "condition_id": cid,
            "wallets": ";".join(order),
        }
    update_prices(rows)            # price, market factors + score for today's signals

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        newest_first = sorted(rows.values(), key=lambda r: r["last_updated"], reverse=True)
        w.writerows(sorted(newest_first, key=lambda r: -float(r.get("score") or 0)))

    # every trader who appears in any signal, with their latest stats and score
    seen = defaultdict(lambda: {"groups": set(), "dates": [], "n": 0})
    for r in rows.values():
        for w in filter(None, (r.get("wallets") or "").split(";")):
            seen[w]["groups"].add(r["group"])
            seen[w]["dates"].append(r["date"])
            seen[w]["n"] += 1
    refresh_stats(traders, seen)   # only fetches traders not refreshed today
    for w, info in seen.items():
        t = traders[w]
        wins, settled, total_return = records[w]
        old_groups = set(filter(None, (t.get("groups") or "").split(", ")))
        t.update(
            name=names.get(w) or t.get("name") or w[:10],
            groups=", ".join(sorted(info["groups"] | old_groups)),
            num_signals=info["n"],
            our_wins=wins, our_settled=settled,
            our_avg_return=f"{total_return / settled:+.0%}" if settled else "",
            edge=edge_text(t["wins"], t["expected_wins"], t["bets"]),
            recent_edge=edge_text(t["recent_wins"], t["recent_expected"], t["recent_bets"]),
            first_seen=min(info["dates"]), last_seen=max(info["dates"]),
            score=round(trader_score(t, records[w])),
        )
    with open(TRADERS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADER_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted((traders[w] for w in seen), key=lambda t: -t["score"]))

    log(f"{stamp}: {len(found)} signals today -> {OUTPUT} ({len(rows)} total), "
        f"{len(seen)} traders -> {TRADERS}")


def log(msg):
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log(f"{datetime.now():%Y-%m-%d %H:%M} ERROR\n{traceback.format_exc()}")
        raise
