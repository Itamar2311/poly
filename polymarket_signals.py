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

Scoring (0-100, 50 = average / unknown). Each trader gets a score from:
    win rate      - share of their settled bets that won (counted per bet, not per $,
                    so one big win can't hide many losses)
    50/50 rate    - win rate on bets entered at 40-60c (coin-flip odds, hardest to win)
    our record    - how signals they were part of in signals.csv turned out
All three are smoothed toward 50% so a trader with few bets doesn't look perfect.
The signal score is the average of its traders' scores, plus a bonus per extra trader.

All signals go into signals.csv. Running again on the same day updates that day's
rows instead of adding duplicates, and fills in "result" once a market settles.
"""
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

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

# scoring
LOOKBACK_DAYS = 60           # trader history window
COIN_FLIP = (0.40, 0.60)     # entry prices that count as a 50/50 bet
PRIOR_BETS = 10              # smoothing: pretend every rate starts with 10 bets at 50%
WEIGHTS = {"win_rate": 0.4, "coin_flip": 0.4, "our_record": 0.2}
BONUS_PER_EXTRA_TRADER = 5   # added to the signal score per trader above MIN_TRADERS

OUTPUT = "signals.csv"
STATS_CACHE = "trader_stats.json"   # trader history, refreshed once a day
# --------------------------------------------------------------------------

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
FIELDS = ["date", "group", "score", "first_seen", "last_updated", "market", "side",
          "num_traders", "total_usdc", "avg_price", "result", "traders", "trader_scores",
          "url", "condition_id", "wallets"]


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


# --- trader history -------------------------------------------------------

def settled_bets(wallet):
    """[(entry_price, won)] for the wallet's settled bets in the last LOOKBACK_DAYS.

    Polymarket keeps winning / sold bets in closed-positions, but losing bets that were
    never cashed out stay in positions (redeemable, price 0), so both are needed.
    """
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    bets = []

    for page in range(40):   # closed-positions: 50 per page, newest first
        batch = get(DATA, "/closed-positions", user=wallet, limit=50, offset=page * 50,
                    sortBy="TIMESTAMP", sortDirection="DESC")
        bets += [(p["avgPrice"], p["realizedPnl"] > 0) for p in batch
                 if p["timestamp"] >= cutoff.timestamp()]
        if len(batch) < 50 or batch[-1]["timestamp"] < cutoff.timestamp():
            break

    for page in range(10):   # settled but not cashed out
        batch = get(DATA, "/positions", user=wallet, redeemable="true", sizeThreshold=0,
                    limit=500, offset=page * 500)
        bets += [(p["avgPrice"], p["curPrice"] >= 0.99) for p in batch
                 if (p.get("endDate") or "9999") >= cutoff.strftime("%Y-%m-%d")]
        if len(batch) < 500:
            break

    return [(price, won) for price, won in bets if 1 - EXTREME_ODDS < price < EXTREME_ODDS]


def load_stats(wallets):
    """{wallet: {"bets", "wins", "cf_bets", "cf_wins"}}, cached for the day."""
    today = datetime.now().strftime("%Y-%m-%d")
    cache = {}
    if os.path.exists(STATS_CACHE):
        with open(STATS_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    for w in wallets:
        if cache.get(w, {}).get("date") != today:
            bets = settled_bets(w)
            cf = [won for price, won in bets if COIN_FLIP[0] <= price <= COIN_FLIP[1]]
            cache[w] = {"date": today, "bets": len(bets), "wins": sum(won for _, won in bets),
                        "cf_bets": len(cf), "cf_wins": sum(cf)}
    with open(STATS_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)
    return cache


def smoothed(wins, total):
    return (wins + PRIOR_BETS / 2) / (total + PRIOR_BETS)


def trader_score(stats, record):
    """0-100 score for one trader. record = (signal wins, signal total) from signals.csv."""
    parts = {
        "win_rate": smoothed(stats["wins"], stats["bets"]),
        "coin_flip": smoothed(stats["cf_wins"], stats["cf_bets"]),
        "our_record": smoothed(*record),
    }
    return 100 * sum(WEIGHTS[k] * v for k, v in parts.items()), parts


# --- signals --------------------------------------------------------------

def find_signals(group, allowed_tags, start_ts, names):
    """Return [(market, cid, outcome_index, {wallet: usdc}, avg_price)] for one group."""
    traders = get(DATA, "/v1/leaderboard", category=group, timePeriod="MONTH",
                  orderBy="PNL", limit=TOP_N)
    for t in traders:
        names[t["proxyWallet"]] = t.get("userName") or t["proxyWallet"][:10]

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
                c = sum(cost[cid, w, direction] for w in backers)
                s = sum(shares[cid, w, direction] for w in backers)
                signals.append((markets[cid], cid, idx, backers, c / s if s else None))
    return signals


def update_results(rows):
    """Fill in "won" / "lost" for signals whose market has settled."""
    for cid in {r["condition_id"] for r in rows.values() if not r.get("result")}:
        found = get(GAMMA, "/markets", condition_ids=cid, closed="true")
        if not found:
            continue   # still open
        prices = dict(zip(json.loads(found[0]["outcomes"]),
                          map(float, json.loads(found[0]["outcomePrices"]))))
        for r in rows.values():
            if r["condition_id"] == cid and not r.get("result") and r["side"] in prices:
                r["result"] = "won" if prices[r["side"]] >= 0.99 else "lost"


def our_records(rows):
    """{wallet: (wins, total)} over settled signals in signals.csv."""
    rec = defaultdict(lambda: [0, 0])
    for r in rows.values():
        if r.get("result") in ("won", "lost"):
            for w in filter(None, (r.get("wallets") or "").split(";")):
                rec[w][0] += r["result"] == "won"
                rec[w][1] += 1
    return defaultdict(lambda: (0, 0), {w: tuple(v) for w, v in rec.items()})


def main():
    now = datetime.now()
    start_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    date, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M")

    rows = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, newline="", encoding="utf-8") as f:
            rows = {(r["date"], r["group"], r["condition_id"], r["side"]): r
                    for r in csv.DictReader(f)}
    update_results(rows)
    records = our_records(rows)

    names = {}
    found = [(group, *s) for group, tags in GROUPS.items()
             for s in find_signals(group, tags, start_ts, names)]
    stats = load_stats({w for *_, backers, _ in found for w in backers})

    for group, m, cid, idx, backers, avg_price in found:
        side = m["outcomes"].get(idx, "Yes" if idx == 0 else "No")
        scores = {w: trader_score(stats[w], records[w]) for w in backers}
        score = (sum(s for s, _ in scores.values()) / len(scores)
                 + BONUS_PER_EXTRA_TRADER * (len(backers) - MIN_TRADERS))
        order = sorted(backers, key=lambda w: -backers[w])
        key = (date, group, cid, side)
        rows[key] = {
            "date": date,
            "group": group,
            "score": round(min(score, 100)),
            "first_seen": rows[key]["first_seen"] if key in rows else stamp,
            "last_updated": stamp,
            "market": m["title"],
            "side": side,
            "num_traders": len(backers),
            "total_usdc": round(sum(backers.values()), 2),
            "avg_price": round(avg_price, 4) if avg_price else "",
            "result": "",
            "traders": "; ".join(f"{names[w]} ${backers[w]:,.0f}" for w in order),
            "trader_scores": "; ".join(
                f"{names[w]} {scores[w][0]:.0f} "
                f"(wins {stats[w]['wins']}/{stats[w]['bets']}, "
                f"50/50 {stats[w]['cf_wins']}/{stats[w]['cf_bets']}, "
                f"ours {records[w][0]}/{records[w][1]})" for w in order),
            "url": f"https://polymarket.com/event/{m['slug']}",
            "condition_id": cid,
            "wallets": ";".join(order),
        }

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (
            r["date"], -float(r.get("score") or 0), r["group"])))
    print(f"{len(found)} signals today -> {OUTPUT} ({len(rows)} rows total)")


if __name__ == "__main__":
    main()
