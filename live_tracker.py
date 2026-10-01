"""
Watch the top Polymarket traders live and alert the moment 2+ of them take the same side.

Listens to Polymarket's real-time trade feed (every trade on the site, pushed as it
happens) and keeps only trades by the top 25 traders of each group used by
polymarket_signals.py (OVERALL, WEATHER, ECONOMICS, MENTIONS, SPORTS). It applies the
same rules: skip bot-like traders, skip extreme odds, only markets in the group's
categories, net direction per trader, at least MIN_USDC_PER_TRADER each.

When MIN_TRADERS traders from the same group are on the same side of a market today,
it shows a Windows notification and adds a row to alerts.csv. It alerts again if more
traders join (3, 4, ...). Trades already made earlier today are loaded at start-up, so
nothing is missed after a restart, but those don't pop up as new alerts.

Runs until stopped. Leaderboards are reloaded every hour and the day resets at midnight.
"""
import csv
import json
import os
import queue
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from xml.sax.saxutils import escape

import websocket   # pip install websocket-client

import polymarket_signals as ps

FEED = "wss://ws-live-data.polymarket.com"
ALERTS = "alerts.csv"
LOG = "live_log.txt"
LEADERBOARD_REFRESH = 3600   # seconds
NOTIFY = True                # Windows pop-up for each alert
ALERT_FIELDS = ["time", "group", "market", "side", "num_traders", "total_usdc", "price_now",
                "traders", "trader_scores", "url", "condition_id"]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, body):
    """Windows toast notification via PowerShell (no extra packages needed)."""
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(body)}</text></binding></visual></toast>").replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]::new();"
        f"$x.LoadXml('{xml}');"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class Tracker:
    def __init__(self):
        self.queue = queue.Queue()
        self.watch = {}          # wallet (lowercase) -> {"wallet", "name", "groups": set}
        self.tokens = {}         # outcome token id -> market info (or None if unknown)
        self.traders = ps.load_traders()   # cached stats from the hourly script, for scores
        self.leaderboards_at = 0
        self.new_day()

    # --- state ------------------------------------------------------------
    def new_day(self):
        now = datetime.now()
        self.day = now.strftime("%Y-%m-%d")
        self.start_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        self.net = defaultdict(lambda: defaultdict(float))   # (group, cid) -> wallet -> +Yes/-No $
        self.trade_count = Counter()
        self.bots = set()        # wallets over MAX_TRADES_PER_DAY today
        self.markets = {}        # cid -> market info
        self.seen = set()
        self.alerted = set()     # (group, cid, side, num_traders)
        if os.path.exists(ALERTS):
            with open(ALERTS, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r["time"].startswith(self.day):
                        self.alerted.add((r["group"], r["condition_id"], r["side"],
                                          int(r["num_traders"])))

    def load_leaderboards(self):
        watch = {}
        for group in ps.GROUPS:
            for t in ps.get(ps.DATA, "/v1/leaderboard", category=group, timePeriod="MONTH",
                            orderBy="PNL", limit=ps.TOP_N):
                w = t["proxyWallet"]
                entry = watch.setdefault(w.lower(), {"wallet": w, "groups": set(),
                                                     "name": t.get("userName") or w[:10]})
                entry["groups"].add(group)
        new = set(watch) - set(self.watch)
        self.watch = watch
        self.leaderboards_at = time.time()
        log(f"watching {len(watch)} traders across {len(ps.GROUPS)} leaderboards")
        return new

    def load_today(self, wallets):
        """Replay today's earlier trades for these wallets, then mark the signals they
        already form as known (no pop-ups for those)."""
        ps._trades.clear()
        for w in wallets:
            for t in ps.trades_today(self.watch[w]["wallet"], self.start_ts):
                info = {"cid": t["conditionId"], "idx": t["outcomeIndex"], "title": t["title"],
                        "slug": t["eventSlug"], "outcomes": {t["outcomeIndex"]: t["outcome"]}}
                self.apply(w, t["side"], t["price"], t["size"], t["transactionHash"], info,
                           alert=False)
        for (group, cid), by_wallet in self.net.items():
            for side, backers in self.signal_sides(cid, by_wallet):
                self.alerted.add((group, cid, side, len(backers)))

    # --- feed -------------------------------------------------------------
    def market_for_token(self, asset):
        if asset not in self.tokens:
            found = (ps.get(ps.GAMMA, "/markets", clob_token_ids=asset)
                     or ps.get(ps.GAMMA, "/markets", clob_token_ids=asset, closed="true"))
            info = None
            if found:
                m = found[0]
                tokens = json.loads(m["clobTokenIds"])
                events = m.get("events") or []
                info = {"cid": m["conditionId"], "idx": tokens.index(asset),
                        "title": m["question"],
                        "slug": events[0]["slug"] if events else m.get("slug", ""),
                        "outcomes": dict(enumerate(json.loads(m["outcomes"])))}
            self.tokens[asset] = info
        return self.tokens[asset]

    def on_trade(self, p):
        w = (p.get("proxyWallet") or "").lower()
        if w not in self.watch:
            return
        info = self.market_for_token(p.get("asset"))
        if info:
            self.apply(w, p["side"], float(p["price"]), float(p["size"]),
                       p.get("transactionHash"), info, alert=True)

    # --- rules (same as polymarket_signals.py) -----------------------------
    def apply(self, w, side, price, size, tx, info, alert):
        key = (tx, w, info["cid"], info["idx"], side, size, price)
        if key in self.seen:
            return
        self.seen.add(key)
        if w in self.bots:
            return
        self.trade_count[w] += 1
        if self.trade_count[w] > ps.MAX_TRADES_PER_DAY:
            self.bots.add(w)                        # bot-like today: drop everything they did
            for by_wallet in self.net.values():
                by_wallet.pop(w, None)
            return
        if price >= ps.EXTREME_ODDS or price <= 1 - ps.EXTREME_ODDS:
            return
        tags = ps.market_tags(info["slug"])
        cid = info["cid"]
        known = self.markets.setdefault(cid, {**info, "outcomes": {}})
        known["outcomes"].update(info["outcomes"])
        sign = 1 if (side == "BUY") == (info["idx"] == 0) else -1
        for group in self.watch[w]["groups"]:
            if tags & ps.GROUPS[group]:
                self.net[group, cid][w] += sign * price * size
                if alert:
                    side_price = price if side == "BUY" else 1 - price
                    yes_price = side_price if sign == 1 else 1 - side_price
                    self.check(group, cid, yes_price)

    def signal_sides(self, cid, by_wallet):
        """[(side name, {wallet: $})] for each side MIN_TRADERS+ traders agree on."""
        out = []
        for direction, idx in ((1, 0), (-1, 1)):
            backers = {w: abs(v) for w, v in by_wallet.items()
                       if v * direction >= ps.MIN_USDC_PER_TRADER}
            if len(backers) >= ps.MIN_TRADERS:
                out.append((self.markets[cid]["outcomes"].get(idx, "Yes" if idx == 0 else "No"),
                            backers))
        return out

    def check(self, group, cid, yes_price):
        for side, backers in self.signal_sides(cid, self.net[group, cid]):
            key = (group, cid, side, len(backers))
            if key in self.alerted:
                continue
            self.alerted.add(key)
            is_yes = side == self.markets[cid]["outcomes"].get(0, "Yes")
            self.alert(group, self.markets[cid], side, backers,
                       yes_price if is_yes else 1 - yes_price)

    def alert(self, group, info, side, backers, price):
        order = sorted(backers, key=lambda w: -backers[w])
        names = [self.watch[w]["name"] for w in order]
        scores = []
        for w in order:
            t = self.traders.get(self.watch[w]["wallet"])
            scores.append(f"{self.watch[w]['name']} {t['score']}" if t and t.get("score")
                          else f"{self.watch[w]['name']} (new)")
        total = sum(backers.values())
        url = f"https://polymarket.com/event/{info['slug']}"
        row = {"time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "group": group,
               "market": info["title"], "side": side, "num_traders": len(backers),
               "total_usdc": round(total, 2), "price_now": round(price, 4),
               "traders": "; ".join(f"{self.watch[w]['name']} ${backers[w]:,.0f}" for w in order),
               "trader_scores": "; ".join(scores), "url": url, "condition_id": info["cid"]}
        new_file = not os.path.exists(ALERTS)
        with open(ALERTS, "a", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=ALERT_FIELDS)
            if new_file:
                wr.writeheader()
            wr.writerow(row)
        log(f"ALERT {group}: {info['title']} -> {side} @ {price:.2f}, "
            f"{len(backers)} traders ${total:,.0f} ({', '.join(names)})")
        if NOTIFY:
            notify(f"{len(backers)} top traders: {side} @ {price * 100:.0f}c",
                   f"{info['title']}\n{group} - ${total:,.0f} - {', '.join(names)}")

    # --- main loop ---------------------------------------------------------
    def worker(self):
        while True:
            try:
                payload = self.queue.get(timeout=30)
            except queue.Empty:
                payload = None
            try:
                if datetime.now().strftime("%Y-%m-%d") != self.day:
                    log("new day - resetting")
                    self.new_day()
                    self.load_today(list(self.watch))
                if time.time() - self.leaderboards_at > LEADERBOARD_REFRESH:
                    self.traders = ps.load_traders()
                    self.load_today(self.load_leaderboards())
                if payload:
                    self.on_trade(payload)
            except Exception as e:      # keep running; one bad message shouldn't stop us
                log(f"error: {e!r}")

    def run(self):
        self.load_leaderboards()
        self.load_today(list(self.watch))
        log(f"loaded today's trades; {len(self.alerted)} signals already active today")
        threading.Thread(target=self.worker, daemon=True).start()

        def on_open(ws):
            ws.send(json.dumps({"action": "subscribe",
                                "subscriptions": [{"topic": "activity", "type": "trades"}]}))
            log("connected to live feed")

        def on_message(ws, msg):
            if not msg.strip():
                return
            try:
                data = json.loads(msg)
            except ValueError:
                return
            if data.get("topic") == "activity" and data.get("payload"):
                if (data["payload"].get("proxyWallet") or "").lower() in self.watch:
                    self.queue.put(data["payload"])

        while True:
            ws = websocket.WebSocketApp(FEED, on_open=on_open, on_message=on_message,
                                        on_error=lambda ws, e: log(f"feed error: {e!r}"))
            ws.run_forever(ping_interval=20, ping_timeout=10)
            log("feed disconnected - reconnecting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    try:
        Tracker().run()
    except Exception:
        import traceback
        log("CRASHED\n" + traceback.format_exc())
        raise
