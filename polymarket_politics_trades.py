"""
Summarize today's trades for the top 25 traders on Polymarket's Politics leaderboard.

Usage:
    python polymarket_politics_trades.py                 # leaderboard period = MONTH
    python polymarket_politics_trades.py --period DAY    # DAY | WEEK | MONTH | ALL
    python polymarket_politics_trades.py --order VOL     # rank by volume instead of PnL
    python polymarket_politics_trades.py --all-markets   # include non-politics markets too

By default only trades in markets tagged "Politics" on Polymarket are kept.

Outputs (in the current folder):
    politics_top25_trades_summary_<date>.csv  one row per trader / market / outcome / side
    politics_top25_trades_raw_<date>.csv      every individual trade
    politics_top25_overlap_<date>.csv         markets traded by more than 2 of the traders
                                              (change with --min-traders N)
"""
import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
PAGE = 500


def get(path, base=API, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  retrying ({e})")
            time.sleep(2 ** attempt)


def leaderboard(period, order, limit=25):
    return get("/v1/leaderboard", category="POLITICS", timePeriod=period,
               orderBy=order, limit=limit)


def trades_since(wallet, start_ts):
    out, offset = [], 0
    while True:
        batch = get("/activity", user=wallet, type="TRADE", start=start_ts,
                    limit=PAGE, offset=offset, sortBy="TIMESTAMP", sortDirection="DESC")
        out.extend(t for t in batch if t.get("timestamp", 0) >= start_ts)
        if len(batch) < PAGE:
            return out
        offset += PAGE


_tag_cache = {}


def is_politics(event_slug):
    """True if the event carries Polymarket's "politics" tag (cached per event)."""
    if not event_slug:
        return False
    if event_slug not in _tag_cache:
        events = get("/events", base=GAMMA, slug=event_slug)
        _tag_cache[event_slug] = any(
            (tag.get("slug") or "").lower() == "politics"
            for e in events for tag in e.get("tags") or [])
    return _tag_cache[event_slug]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="MONTH", choices=["DAY", "WEEK", "MONTH", "ALL"])
    ap.add_argument("--order", default="PNL", choices=["PNL", "VOL"])
    ap.add_argument("--all-markets", action="store_true",
                    help="don't filter out non-politics markets")
    ap.add_argument("--min-traders", type=int, default=3,
                    help="min number of different traders in a market for the overlap CSV")
    args = ap.parse_args()

    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(midnight.timestamp())
    date = midnight.strftime("%Y-%m-%d")

    top = leaderboard(args.period, args.order)
    print(f"Politics leaderboard ({args.period}, by {args.order}): {len(top)} traders")

    raw_rows = []
    groups = defaultdict(lambda: {"trades": 0, "shares": 0.0, "usdc": 0.0,
                                  "first": None, "last": None})
    for t in top:
        wallet, name, rank = t["proxyWallet"], t.get("userName") or t["proxyWallet"], t["rank"]
        trades = trades_since(wallet, start_ts)
        total = len(trades)
        if not args.all_markets:
            trades = [tr for tr in trades if is_politics(tr.get("eventSlug"))]
        print(f"  #{rank:>2} {name}: {len(trades)} trades today"
              + ("" if args.all_markets else f" in politics markets ({total} total)"))
        for tr in trades:
            ts = datetime.fromtimestamp(tr["timestamp"])
            raw_rows.append({
                "rank": rank, "trader": name, "wallet": wallet,
                "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "market": tr.get("title"), "outcome": tr.get("outcome"),
                "side": tr.get("side"), "price": tr.get("price"),
                "shares": tr.get("size"), "usdc": tr.get("usdcSize"),
                "event_slug": tr.get("eventSlug"), "tx": tr.get("transactionHash"),
            })
            g = groups[(rank, name, wallet, tr.get("title"), tr.get("outcome"),
                        tr.get("side"), tr.get("eventSlug"))]
            g["trades"] += 1
            g["shares"] += float(tr.get("size") or 0)
            g["usdc"] += float(tr.get("usdcSize") or 0)
            g["first"] = ts if g["first"] is None else min(g["first"], ts)
            g["last"] = ts if g["last"] is None else max(g["last"], ts)

    summary_file = f"politics_top25_trades_summary_{date}.csv"
    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "trader", "wallet", "market", "outcome", "side", "num_trades",
                    "total_shares", "total_usdc", "avg_price", "first_trade", "last_trade",
                    "event_url"])
        for (rank, name, wallet, market, outcome, side, slug), g in sorted(
                groups.items(), key=lambda kv: (int(kv[0][0]), -kv[1]["usdc"])):
            w.writerow([rank, name, wallet, market, outcome, side, g["trades"],
                        round(g["shares"], 4), round(g["usdc"], 2),
                        round(g["usdc"] / g["shares"], 4) if g["shares"] else "",
                        g["first"].strftime("%H:%M:%S"), g["last"].strftime("%H:%M:%S"),
                        f"https://polymarket.com/event/{slug}" if slug else ""])

    raw_file = f"politics_top25_trades_raw_{date}.csv"
    with open(raw_file, "w", newline="", encoding="utf-8") as f:
        fields = ["rank", "trader", "wallet", "time", "market", "outcome", "side",
                  "price", "shares", "usdc", "event_slug", "tx"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(raw_rows, key=lambda r: (int(r["rank"]), r["time"])))

    print(f"\n{len(raw_rows)} trades -> {raw_file}")
    print(f"{len(groups)} summary rows -> {summary_file}")

    overlap_file = f"politics_top25_overlap_{date}.csv"
    n_overlap = write_overlap(raw_rows, overlap_file, args.min_traders)
    print(f"{n_overlap} markets traded by {args.min_traders}+ traders -> {overlap_file}")


def write_overlap(raw_rows, path, min_traders):
    """One row per market that at least `min_traders` different traders traded today."""
    markets = defaultdict(lambda: {"slug": "", "trades": 0, "buy": 0.0, "sell": 0.0,
                                   "traders": defaultdict(lambda: defaultdict(float))})
    for r in raw_rows:
        m = markets[r["market"]]
        m["slug"] = r["event_slug"]
        m["trades"] += 1
        usdc = float(r["usdc"] or 0)
        m["buy" if r["side"] == "BUY" else "sell"] += usdc
        m["traders"][(int(r["rank"]), r["trader"])][f'{r["side"]} {r["outcome"]}'] += usdc

    rows = [(market, m) for market, m in markets.items() if len(m["traders"]) >= min_traders]
    rows.sort(key=lambda x: (-len(x[1]["traders"]), -(x[1]["buy"] + x[1]["sell"])))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["market", "num_traders", "num_trades", "total_usdc", "buy_usdc",
                    "sell_usdc", "traders", "event_url"])
        for market, m in rows:
            detail = "; ".join(
                f"#{rank} {name}: " + ", ".join(f"{k} ${v:,.0f}" for k, v in pos.items())
                for (rank, name), pos in sorted(m["traders"].items()))
            w.writerow([market, len(m["traders"]), m["trades"],
                        round(m["buy"] + m["sell"], 2), round(m["buy"], 2),
                        round(m["sell"], 2), detail,
                        f"https://polymarket.com/event/{m['slug']}" if m["slug"] else ""])
    return len(rows)


if __name__ == "__main__":
    main()
