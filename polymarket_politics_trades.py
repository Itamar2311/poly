"""
Summarize today's trades for the top 25 traders on Polymarket's Politics leaderboard.

Usage:
    python polymarket_politics_trades.py                 # leaderboard period = MONTH
    python polymarket_politics_trades.py --period DAY    # DAY | WEEK | MONTH | ALL
    python polymarket_politics_trades.py --order VOL     # rank by volume instead of PnL

Outputs (in the current folder):
    politics_top25_trades_summary_<date>.csv  one row per trader / market / outcome / side
    politics_top25_trades_raw_<date>.csv      every individual trade
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
PAGE = 500


def get(path, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="MONTH", choices=["DAY", "WEEK", "MONTH", "ALL"])
    ap.add_argument("--order", default="PNL", choices=["PNL", "VOL"])
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
        print(f"  #{rank:>2} {name}: {len(trades)} trades today")
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


if __name__ == "__main__":
    main()
