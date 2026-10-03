"""
Rebuild signals for past days, so the scoring / regression has data to learn from.

For each of the last DAYS_BACK days it replays that day's trades by today's top traders
(same groups and rules as polymarket_signals.py) and records every market where 2+
traders of a group ended up on the same side. Factors are calculated as they would
have been on that day:
    - trader edge / recent edge / 50-50 rate / win rate / experience use only bets
      that had settled before that day
    - our_record uses only earlier rebuilt signals whose markets had already ended
    - entry_price = the price at the moment the 2nd trader joined
    - timing = time from that moment until the market ended
Unknown in the past, so left out (count as neutral 0.5 in the score, and the weight
tuning ignores them for these rows): lb_rank, lb_roi, liquidity.

Caveat: we only know who is in the top 25 *today*. For days inside the last
LEADERBOARD_WINDOW days, part of the reason they are top now is those very trades,
so those rows are marked lookahead=True (they look better than reality would have).

Output: history.csv (read by polymarket_signals.py for weight tuning and records,
which also settles any of these markets that were still open).
Raw downloads are cached in backfill_cache.json, so a re-run resumes instead of
starting over. Run:  python backfill.py
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import polymarket_signals as ps

DAYS_BACK = 60
LEADERBOARD_WINDOW = 30      # monthly leaderboard: days whose trades shaped today's ranks
# kept outside the project folder so PyCharm doesn't index / lock this large file
# (in the home folder, not AppData: apps like the Claude desktop app see a private AppData)
CACHE = os.path.join(os.path.expanduser("~"), ".polymarket_backfill", "backfill_cache.json")
BETS_HISTORY_DAYS = 60 + 60       # trader history fetched once (cached), enough for 60 days back


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {"trades": {}, "tags": {}, "bets": {}}


def save_cache(cache):
    cache["tags"] = {k: sorted(v) for k, v in ps._tags.items()}
    tmp = CACHE + ".tmp"
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    for attempt in range(10):      # Windows: an editor / virus scanner may briefly lock it
        try:
            os.replace(tmp, CACHE)
            return
        except PermissionError:
            time.sleep(2)
    os.replace(tmp, CACHE)


def day_trades(wallet, day_start):
    """Compact trades for one wallet on one day, or "BOT" if over MAX_TRADES_PER_DAY."""
    batch = ps.get(ps.DATA, "/activity", user=wallet, type="TRADE", start=day_start,
                   end=day_start + 86399, limit=500)
    if len(batch) > ps.MAX_TRADES_PER_DAY:
        return "BOT"
    return [[t["timestamp"], t["conditionId"], t["outcomeIndex"], t["side"], t["price"],
             t["size"], t["title"], t["outcome"], t["eventSlug"]] for t in batch]


def stats_at(bets, day_ts):
    """Trader history stats using only bets settled before day_ts."""
    lo, recent_lo = day_ts - ps.LOOKBACK_DAYS * 86400, day_ts - ps.RECENT_DAYS * 86400
    window = [b for b in bets if lo <= b[2] < day_ts]
    recent = [b for b in window if b[2] >= recent_lo]
    cf = [won for price, won, _ in window if ps.COIN_FLIP[0] <= price <= ps.COIN_FLIP[1]]
    return {"bets": len(window), "wins": sum(b[1] for b in window),
            "expected_wins": sum(b[0] for b in window),
            "recent_bets": len(recent), "recent_wins": sum(b[1] for b in recent),
            "recent_expected": sum(b[0] for b in recent),
            "cf_bets": len(cf), "cf_wins": sum(cf)}


def end_ts(state):
    try:
        return datetime.fromisoformat(state["end"].replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    started = time.time()
    cache = load_cache()
    ps._tags.update({k: set(v) for k, v in cache["tags"].items()})

    # today's top traders per group
    groups_of, names = defaultdict(set), {}
    for group in ps.GROUPS:
        for t in ps.get(ps.DATA, "/v1/leaderboard", category=group, timePeriod="MONTH",
                        orderBy="PNL", limit=ps.TOP_N):
            groups_of[t["proxyWallet"]].add(group)
            names[t["proxyWallet"]] = t.get("userName") or t["proxyWallet"][:10]
    print(f"{len(groups_of)} traders, {DAYS_BACK} days")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=i) for i in range(DAYS_BACK, 0, -1)]   # oldest first

    # 1. download trades (cached)
    for n, w in enumerate(groups_of, 1):
        missing = [d for d in days if f"{w}|{d:%Y-%m-%d}" not in cache["trades"]]
        for d in missing:
            cache["trades"][f"{w}|{d:%Y-%m-%d}"] = day_trades(w, int(d.timestamp()))
        if missing:
            save_cache(cache)
        print(f"  trades {n}/{len(groups_of)} {names[w]}  ({time.time() - started:.0f}s)", flush=True)

    # 2. replay each day per group
    found = []   # (day, group, cid, idx, info dict)
    for d in days:
        day_key, day_ts = f"{d:%Y-%m-%d}", int(d.timestamp())
        for group, allowed in ps.GROUPS.items():
            trades = []
            for w, gs in groups_of.items():
                day = cache["trades"].get(f"{w}|{day_key}")
                if group in gs and isinstance(day, list):
                    trades += [(t, w) for t in day]
            trades.sort(key=lambda x: x[0][0])
            net = defaultdict(lambda: defaultdict(float))
            cost, shares, markets, trigger = defaultdict(float), defaultdict(float), {}, {}
            for t, w in trades:
                ts, cid, idx, side, price, size, title, outcome, slug = t
                if price >= ps.EXTREME_ODDS or price <= 1 - ps.EXTREME_ODDS:
                    continue
                if not ps.market_tags(slug) & allowed:
                    continue
                sign = 1 if (side == "BUY") == (idx == 0) else -1
                side_price = price if side == "BUY" else 1 - price
                net[cid][w] += sign * price * size
                cost[cid, w, sign] += side_price * size
                shares[cid, w, sign] += size
                m = markets.setdefault(cid, {"title": title, "slug": slug, "outcomes": {}})
                m["outcomes"][idx] = outcome
                # first moment MIN_TRADERS agree on this trade's side -> entry price
                backers = [x for x, v in net[cid].items() if v * sign >= ps.MIN_USDC_PER_TRADER]
                if len(backers) >= ps.MIN_TRADERS and (cid, sign) not in trigger:
                    trigger[cid, sign] = (ts, side_price)
            for (cid, direction), (ts, entry) in trigger.items():
                by_wallet = net[cid]
                backers = {x: abs(v) for x, v in by_wallet.items()
                           if v * direction >= ps.MIN_USDC_PER_TRADER}
                if len(backers) < ps.MIN_TRADERS:
                    continue              # they agreed for a while but not at day end
                opposing = sum(-v * direction for v in by_wallet.values() if v * direction < 0)
                c = sum(cost[cid, x, direction] for x in backers)
                s = sum(shares[cid, x, direction] for x in backers)
                found.append((d, group, cid, 0 if direction == 1 else 1, {
                    "market": markets[cid], "backers": backers, "opposing": opposing,
                    "avg": c / s if s else entry, "entry": entry, "trigger_ts": ts}))
    save_cache(cache)                    # market tags looked up during the replay
    print(f"{len(found)} signals found, scoring ({time.time() - started:.0f}s)", flush=True)

    # 3. trader history (cached), back far enough for the oldest day
    wallets = {w for *_, info in found for w in info["backers"]}
    for n, w in enumerate(sorted(wallets), 1):
        if w not in cache["bets"]:
            cache["bets"][w] = ps.settled_bets(w, days=BETS_HISTORY_DAYS,
                                               max_pages=100)
            save_cache(cache)
        print(f"  history {n}/{len(wallets)} {names.get(w, w[:10])}", flush=True)

    # 4. score, settle, write (chronological so our_record only sees the past)
    rows, settled_so_far = {}, []        # settled_so_far: (end_ts, row)
    for d, group, cid, idx, info in found:
        day_ts = int(d.timestamp())
        state = ps.market_state(cid)
        m, backers = info["market"], info["backers"]
        outcome_names = list(state["prices"]) or ["Yes", "No"]
        side = m["outcomes"].get(idx) or outcome_names[idx]
        records = ps.our_records({i: r for i, (e, r) in enumerate(settled_so_far)
                                  if e is not None and e < day_ts})
        per_trader, raws = {}, []
        for w in backers:
            stats = stats_at(cache["bets"][w], day_ts)
            parts = ps.trader_parts(stats, records[w])
            per_trader[w] = {k: v for k, v in parts.items() if k not in ("lb_rank", "lb_roi")}
            raws.append(ps.trader_raw(stats, records[w]))
        total = sum(backers.values())
        profile = ps.PROFILES[group]
        parts = {k: sum(p[k] for p in per_trader.values()) / len(per_trader)
                 for k in ps.TRADER_FACTORS if k not in ("lb_rank", "lb_roi")}
        end = end_ts(state)
        days_left = ps.days_until_end(state, info["trigger_ts"])
        parts.update(
            agreement=ps.agreement_part(len(backers)),
            conviction=ps.log_scale(total, *profile["conviction"]),
            consensus=total / (total + info["opposing"]),
            price=ps.clamp(0.5 - (info["entry"] - info["avg"]) * 100 / 40),
            timing=ps.timing_part(group, days_left),
        )
        raw = {**ps.mean_raw(raws), "num_traders": len(backers), "total_usdc": round(total, 2),
               "opposing_usdc": round(info["opposing"], 2),
               **ps.market_raw(info["avg"], state, None, from_ts=info["trigger_ts"],
                               price=info["entry"])}   # liquidity unknown in the past -> None
        price_now = state["prices"].get(side)
        row = {
            "date": f"{d:%Y-%m-%d}", "group": group,
            "first_seen": f"{datetime.fromtimestamp(info['trigger_ts']):%Y-%m-%d %H:%M}",
            "last_updated": f"{d:%Y-%m-%d} 23:59",
            "market": m["title"], "side": side, "num_traders": len(backers),
            "total_usdc": round(total, 2), "opposing_usdc": round(info["opposing"], 2),
            "avg_price": round(info["avg"], 4), "entry_price": round(info["entry"], 4),
            "price_move": round(info["entry"] - info["avg"], 4),
            "ends": (state["end"] or "")[:10], "result": "", "return": "",
            "traders": "; ".join(f"{names.get(w, w[:10])} ${backers[w]:,.0f}"
                                 for w in sorted(backers, key=lambda w: -backers[w])),
            "url": f"https://polymarket.com/event/{m['slug']}", "condition_id": cid,
            "wallets": ";".join(sorted(backers, key=lambda w: -backers[w])),
            "lookahead": (today - d).days <= LEADERBOARD_WINDOW,
        }
        ps.set_score(row, group, parts)
        row["raw_values"] = ps.dump_raw(raw)
        if state["closed"] and price_now is not None:
            won = price_now >= 0.99
            row.update(result="won" if won else "lost", current_price=1 if won else 0,
                       **{"return": round((1 - info["entry"]) / info["entry"] if won else -1.0, 4)})
            settled_so_far.append((end, row))
        elif price_now is not None:
            row["current_price"] = price_now
        rows[row["date"], group, cid, side] = row

    ps.save_history(rows)
    settled = [r for r in rows.values() if r["result"]]
    won = sum(r["result"] == "won" for r in settled)
    print(f"\n{len(rows)} signals -> {ps.HISTORY}: {len(settled)} settled "
          f"({won} won), {len(rows) - len(settled)} still open "
          f"({time.time() - started:.0f}s)", flush=True)

    # 5. training snapshots: every top-trader position per day (1, 2, 3... traders),
    #    built by the same replay the hourly run uses -> training.csv for model.R
    ps._markets.update({cid: s for cid, s in cache.get("states", {}).items()})
    training = []
    for n, d in enumerate(days, 1):
        day_key, day_ts = f"{d:%Y-%m-%d}", int(d.timestamp())
        records = ps.our_records({i: r for i, (e, r) in enumerate(settled_so_far)
                                  if e is not None and e < day_ts})
        for group, allowed in ps.GROUPS.items():
            trades = [(t[0], w, *t[1:]) for w, gs in groups_of.items() if group in gs
                      for t in (cache["trades"].get(f"{w}|{day_key}") or [])
                      if isinstance(cache["trades"].get(f"{w}|{day_key}"), list)]
            snaps = ps.replay_day(sorted(trades, key=lambda x: x[0]), allowed)
            for w in {w for s in snaps for w in s["backers"]} - set(cache["bets"]):
                cache["bets"][w] = ps.settled_bets(w, days=BETS_HISTORY_DAYS,
                                                   max_pages=100)
                save_cache(cache)
            training += ps.training_rows(
                snaps, group, lambda w, ts=day_ts: stats_at(cache["bets"][w], ts), records,
                names, live=False, lookahead=(today - d).days <= LEADERBOARD_WINDOW)
        # remember markets that have ended (they won't change) so a re-run is quick
        cache["states"] = {cid: s for cid, s in ps._markets.items() if s["closed"]}
        save_cache(cache)
        print(f"  snapshots {n}/{len(days)} {day_key}: {len(training)} rows "
              f"({time.time() - started:.0f}s)", flush=True)

    live_rows = [r for r in ps.load_training() if r["source"] == "live"]
    ps.save_training(live_rows + training)
    settled = [r for r in training if r["result"]]
    by_n = defaultdict(lambda: [0, 0])
    for r in settled:
        k = min(int(r["num_traders"]), 4)
        by_n[k][0] += r["result"] == "won"
        by_n[k][1] += 1
    print(f"\n{len(training)} snapshots -> {ps.TRAINING}: {len(settled)} settled")
    for k in sorted(by_n):
        print(f"  {k}{'+' if k == 4 else ''} trader(s): {by_n[k][1]} settled, "
              f"{by_n[k][0] / by_n[k][1]:.0%} won")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:          # python backfill.py 7  -> only the last 7 days
        DAYS_BACK = int(sys.argv[1])
    main()
