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

The weights tune themselves: once MIN_SETTLED signals have settled, every run shifts
weight toward factors that went with profitable signals (see learn_weights and
weights.json), always keeping each factor between 1% and 10% of the score.

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
# self-tuning: once enough signals have settled, each run shifts weight toward the
# factors that went with profitable signals (and away from those that didn't).
# Weights are recalculated from all settled data every run, starting from the
# PROFILES defaults, so they move gradually as data builds up.
MIN_SETTLED = 20             # settled signals needed before weights start to move
CONFIDENCE_N = 50            # at this many settled signals the shift is half its maximum
MAX_MULTIPLIER = 3.0         # a factor's weight can grow to at most 3x its default ...
MIN_MULTIPLIER = 0.25        # ... or shrink to 1/4 (always within 1%-10% of the score)
WEIGHT_FLOOR = 0.01
WEIGHT_CAP = 0.10
RETURN_CLIP = 3.0            # cap a single signal's return at +300% so one long shot can't steer
WEIGHTS_FILE = "weights.json"   # the weights currently in use, per group

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
HISTORY = "history.csv"      # signals rebuilt for past days by backfill.py (training data)
# --------------------------------------------------------------------------

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
FIELDS = ["date", "group", "score", "model_prob", "model_edge", "first_seen", "last_updated", "market", "side",
          "num_traders", "total_usdc", "opposing_usdc", "avg_price", "entry_price",
          "current_price", "price_move", "ends", "result", "return", "traders",
          "trader_scores", "score_parts", "factor_values", "raw_values", "url",
          "condition_id", "wallets"]
HISTORY_FIELDS = FIELDS + ["lookahead"]
TRADER_FIELDS = ["name", "score", "groups", "num_signals", "our_wins", "our_settled",
                 "our_avg_return", "edge", "recent_edge", "wins", "expected_wins", "bets",
                 "recent_wins", "recent_expected", "recent_bets", "cf_wins", "cf_bets",
                 "lb_rank", "lb_pnl", "lb_vol", "first_seen", "last_seen", "stats_date",
                 "wallet"]
INT_FIELDS = {"wins", "bets", "cf_wins", "cf_bets", "recent_wins", "recent_bets"}
FLOAT_FIELDS = {"expected_wins", "recent_expected", "lb_rank", "lb_pnl", "lb_vol"}


MIN_REQUEST_GAP = 0.1         # seconds between API calls, to stay under Polymarket's rate limit
_last_request = [0.0]


def get(base, path, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(6):
        time.sleep(max(0.0, MIN_REQUEST_GAP - (time.time() - _last_request[0])))
        _last_request[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 5:
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
            if len(batch) < 500 or len(out) > MAX_TRADES_PER_DAY:
                break      # over the bot limit: no need for the rest (API stops at 5000)
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

def settled_bets(wallet, days=LOOKBACK_DAYS, max_pages=40):
    """[(entry_price, won, settled_ts)] for the wallet's settled bets in the last `days`.

    Polymarket keeps winning / sold bets in closed-positions, but losing bets that were
    never cashed out stay in positions (redeemable, price 0), so both are needed.
    """
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    bets = []

    for page in range(max_pages):   # closed-positions: 50 per page, newest first
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
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                continue                    # missing or nonsense end date (e.g. year 0)
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


WEIGHTS = {g: dict(p["weights"]) for g, p in PROFILES.items()}   # replaced by learn_weights()


def capped_normalize(raw, lo=WEIGHT_FLOOR, hi=WEIGHT_CAP):
    """Scale weights to add up to 1 while keeping each between lo and hi."""
    fixed = {}
    while True:
        free = {k: v for k, v in raw.items() if k not in fixed}
        if not free:
            return fixed
        remaining = 1 - sum(fixed.values())
        total = sum(free.values()) or 1
        scaled = {k: remaining * v / total for k, v in free.items()}
        out_of_range = {k: hi if v > hi else lo for k, v in scaled.items() if not lo <= v <= hi}
        if not out_of_range:
            merged = {**fixed, **scaled}
            return {k: merged[k] for k in raw}
        fixed.update(out_of_range)


def correlation(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def learn_weights(rows):
    """Set WEIGHTS from settled signals; returns a one-line summary for the log."""
    settled = defaultdict(list)    # group -> [(factor values, outcome)]
    for r in rows.values():
        if r.get("result") in ("won", "lost") and r.get("return") not in (None, ""):
            values = factor_values(r)
            if values:
                settled[r["group"]].append((values, min(float(r["return"]), RETURN_CLIP)))
    everything = [x for xs in settled.values() for x in xs]

    report, summary = {}, []
    for group, profile in PROFILES.items():
        data = settled[group] if len(settled[group]) >= MIN_SETTLED else everything
        defaults = profile["weights"]
        if len(data) < MIN_SETTLED:
            WEIGHTS[group] = dict(defaults)
            report[group] = {"source": f"defaults (need {MIN_SETTLED} settled, have {len(data)})",
                             "weights": WEIGHTS[group]}
            continue
        n = len(data)
        mult, corr = {}, {}
        for k in defaults:
            known = [(v[k], o) for v, o in data if k in v]   # history lacks some factors
            if len(known) < MIN_SETTLED:
                continue
            c = corr[k] = correlation([x for x, _ in known], [o for _, o in known])
            confidence = len(known) / (len(known) + CONFIDENCE_N)
            # t-statistic: how sure we are the link is real and not noise (about 2 = fairly sure).
            # No evidence (t ~ 0) shrinks a factor a little, real evidence (t > 1) grows it,
            # a factor that goes with losing (t < 0) shrinks fast.
            t = c * math.sqrt(max(len(known) - 2, 1)) / math.sqrt(max(1 - c * c, 1e-9))
            mult[k] = clamp(1 + 0.5 * confidence * (t - 1), MIN_MULTIPLIER, MAX_MULTIPLIER)
        # factors without enough data stay neutral: they move with the average, not against it
        neutral = sum(mult.values()) / len(mult) if mult else 1.0
        WEIGHTS[group] = capped_normalize({k: d * mult.get(k, neutral) for k, d in defaults.items()})
        source = "own" if data is settled[group] else "all groups"
        report[group] = {"source": f"learned from {n} settled signals ({source})",
                         "weights": {k: round(v, 4) for k, v in WEIGHTS[group].items()},
                         "correlation_with_return": {k: round(v, 3) for k, v in corr.items()}}
        top = max(WEIGHTS[group], key=lambda k: WEIGHTS[group][k] / defaults[k])
        summary.append(f"{group}: {n} settled, +{top}")

    with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    return "weights: " + ("; ".join(summary) if summary else
                          f"defaults (need {MIN_SETTLED} settled signals, have {len(everything)})")


def factor_values(r):
    """{factor: 0-1} for a signals.csv row."""
    if r.get("factor_values"):
        return json.loads(r["factor_values"])
    return parse_parts(r["group"], r.get("score_parts"))   # rows from before factor_values


def trader_score(t, record, group="OVERALL"):
    """0-100 score for one trader, from the trader factors only (group's weights)."""
    weights = WEIGHTS[group]
    parts = trader_parts(t, record)
    return 100 * sum(weights[k] * v for k, v in parts.items()) / sum(weights[k] for k in parts)


AGREEMENT_RATING = {1: 0.0, 2: 0.5, 3: 0.8}   # 4+ traders = 1.0 (was 0.33 / 0.67 for 2 / 3)


def agreement_part(num_traders):
    return AGREEMENT_RATING.get(num_traders, 1.0)


def log_scale(x, lo, hi):
    """0 at lo, 1 at hi, logarithmic in between."""
    return clamp(math.log10(max(x, 1e-9) / lo) / math.log10(hi / lo))


def days_until_end(state, from_ts=None):
    """Days from now (or from_ts) until the market's end date; None if unknown."""
    try:
        end = datetime.fromisoformat(state["end"].replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None
    return (end - (from_ts if from_ts is not None else time.time())) / 86400


def timing_part(group, days):
    if days is None:
        return 0.5
    return next(v for max_days, v in PROFILES[group]["timing"] if days <= max_days)


def market_parts(group, avg_price, state, side):
    """Factors that change while the market is open, each 0-1."""
    profile = PROFILES[group]
    price = state["prices"].get(side)
    move = price - avg_price if price is not None and avg_price else 0
    return {
        "price": clamp(0.5 - move * 100 / 40),                        # +-20c -> 0 / 1
        "timing": timing_part(group, days_until_end(state)),
        "liquidity": log_scale(state["liquidity"], *profile["liquidity"]),
    }


# --- raw values (unsquashed measurements, for the regression in model.R) ----

def ratio(a, b):
    return a / b if b else None


def trader_raw(t, record):
    """One trader's raw measurements. None = unknown."""
    _, settled, total_return = record
    vol = t.get("lb_vol")
    return {
        "edge_raw": ratio(t["wins"] - t["expected_wins"], t["bets"]),
        "recent_edge_raw": ratio(t["recent_wins"] - t["recent_expected"], t["recent_bets"]),
        "coin_flip_rate": ratio(t["cf_wins"], t["cf_bets"]),
        "win_rate_raw": ratio(t["wins"], t["bets"]),
        "bets": t["bets"],
        "cf_bets": t["cf_bets"],
        "our_signals": settled,
        "our_avg_return": ratio(total_return, settled),
        "lb_rank": t.get("lb_rank") if t.get("lb_rank") not in (None, "") else None,
        "lb_roi": ratio(t.get("lb_pnl") or 0, vol) if vol not in (None, "") else None,
    }


def mean_raw(raws):
    """Average each measurement over the traders behind a signal (ignoring unknowns)."""
    out = {}
    for k in raws[0]:
        known = [r[k] for r in raws if r[k] is not None]
        out[k] = sum(known) / len(known) if known else None
    return out


def market_raw(avg_price, state, side, from_ts=None, price=None):
    price = state["prices"].get(side) if price is None else price
    return {
        "price_move_cents": round((price - avg_price) * 100, 2)
        if price is not None and avg_price else None,
        "days_to_end": days_until_end(state, from_ts),
        "liquidity_usd": state["liquidity"] if from_ts is None else None,
    }


def dump_raw(raw):
    return json.dumps({k: round(v, 5) if isinstance(v, float) else v for k, v in raw.items()})


# --- regression model (fitted by model.R) ------------------------------------

MODEL_FILE = "model_coefficients.json"
_model = []


def load_model():
    """The coefficients model.R saved, or None if it hasn't been run yet (cached)."""
    if not _model:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), MODEL_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                _model.append(json.load(f))
        except (OSError, ValueError):
            _model.append(None)
    return _model[0]


def model_features(raw, fills):
    """Rebuild the variables exactly as model.R does (section 3)."""
    def v(k, default):
        x = raw.get(k)
        return default if x is None else x
    total, opposing = v("total_usdc", 0), v("opposing_usdc", 0)
    liquidity = raw.get("liquidity_usd")
    return {
        "edge": v("edge_raw", 0),
        "recent_edge": v("recent_edge_raw", 0),
        "coin_flip_rate": v("coin_flip_rate", 0.5),
        "win_rate": v("win_rate_raw", fills["win_rate"]),
        "log_bets": math.log10(v("bets", 0) + 1),
        "our_avg_return": v("our_avg_return", 0),
        "log_our_signals": math.log10(v("our_signals", 0) + 1),
        "num_traders": v("num_traders", 0),
        "two_plus": 1 if v("num_traders", 0) >= 2 else 0,
        "log_total_usd": math.log10(max(total, 1)),
        "opposing_share": opposing / (total + opposing) if total + opposing else 0,
        "price_move_cents": v("price_move_cents", 0),
        "log_days_to_end": math.log10(max(v("days_to_end", 1), 0) + 0.1),
        "has_live_data": 1 if raw.get("lb_rank") is not None else 0,
        "lb_rank": v("lb_rank", fills["lb_rank"]),
        "lb_roi": v("lb_roi", fills["lb_roi"]),
        "log_liquidity": math.log10(liquidity + 1) if liquidity is not None
        else fills["log_liquidity"],
    }


def model_prediction(raw, group, price):
    """(win probability, edge = probability - price) from the regression, or (None, None)."""
    model = load_model()
    if not model or price is None or not raw:
        return None, None
    coefs = model["coefficients"]
    x = model_features(raw, model["fills"])
    z = model["intercept"] + coefs.get(f"group{group}", 0)
    z += sum(coefs.get(k, 0) * val for k, val in x.items())
    z += coefs.get(f"group{group}:num_traders", 0) * x["num_traders"]   # agreement x group
    p = clamp(price, 0.02, 0.98)
    z += math.log(p / (1 - p))                  # offset: the market's own odds
    prob = 1 / (1 + math.exp(-z))
    return round(prob, 4), round(prob - price, 4)


def format_parts(group, parts):
    weights = WEIGHTS[group]
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

def find_signals(group, allowed_tags, start_ts, names, leaderboard, members=None):
    """Return [(market, cid, outcome_index, {wallet: usdc}, avg_price, opposing_usdc)]."""
    traders = get(DATA, "/v1/leaderboard", category=group, timePeriod="MONTH",
                  orderBy="PNL", limit=TOP_N)
    if members is not None:
        members[group] = [t["proxyWallet"] for t in traders]
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


# --- training snapshots (every top-trader position, for the regression) -------
#
# Each time one more top trader of a group crosses MIN_USDC_PER_TRADER on the same side
# of a market, we take a snapshot: how many agree (1, 2, 3...), the price at that
# moment and the raw measurements. One trader alone is a 1-trader row, so the model
# can learn what each extra trader adds. backfill.py and the hourly run both build
# these rows with replay_day(), so past and live data are made the same way.

TRAINING = "training.csv"
TRAINING_FIELDS = ["date", "group", "snapshot_time", "market", "side", "num_traders",
                   "entry_price", "avg_price", "total_usdc", "opposing_usdc", "result",
                   "return", "raw_values", "condition_id", "wallets", "source", "lookahead"]


def replay_day(trades, allowed_tags):
    """trades: [(ts, wallet, cid, outcome_idx, side, price, size, title, outcome, slug)],
    oldest first, from non-bot traders of one group. Returns snapshots."""
    net = defaultdict(lambda: defaultdict(float))
    cost, shares, level, markets, snaps = defaultdict(float), defaultdict(float), {}, {}, []
    for ts, w, cid, idx, side, price, size, title, outcome, slug in trades:
        if price >= EXTREME_ODDS or price <= 1 - EXTREME_ODDS:
            continue
        if not market_tags(slug) & allowed_tags:
            continue
        sign = 1 if (side == "BUY") == (idx == 0) else -1
        side_price = price if side == "BUY" else 1 - price
        net[cid][w] += sign * price * size
        cost[cid, w, sign] += side_price * size
        shares[cid, w, sign] += size
        m = markets.setdefault(cid, {"title": title, "slug": slug, "outcomes": {}})
        m["outcomes"][idx] = outcome
        backers = {x: v * sign for x, v in net[cid].items() if v * sign >= MIN_USDC_PER_TRADER}
        if len(backers) > level.get((cid, sign), 0):     # one more trader on this side
            level[cid, sign] = len(backers)
            c = sum(cost[cid, x, sign] for x in backers)
            s = sum(shares[cid, x, sign] for x in backers)
            snaps.append({"ts": ts, "cid": cid, "idx": 0 if sign == 1 else 1, "market": m,
                          "backers": dict(backers), "entry": side_price,
                          "avg": c / s if s else side_price,
                          "opposing": sum(-v * sign for v in net[cid].values() if v * sign < 0)})
    return snaps


def training_rows(snaps, group, stats_of, records, names, live, lookahead=False):
    """Turn snapshots into training.csv rows. stats_of(wallet) -> trader stats or None."""
    rows = []
    for s in snaps:
        state = market_state(s["cid"])
        names_list = list(state["prices"]) or ["Yes", "No"]
        side = s["market"]["outcomes"].get(s["idx"]) or names_list[s["idx"]]
        raws = [trader_raw(t, records[w]) for w in s["backers"]
                if (t := stats_of(w)) is not None]
        total = sum(s["backers"].values())
        raw = {**(mean_raw(raws) if raws else {}), "num_traders": len(s["backers"]),
               "total_usdc": round(total, 2), "opposing_usdc": round(s["opposing"], 2),
               **market_raw(s["avg"], state, side, from_ts=s["ts"], price=s["entry"])}
        if live:
            raw["liquidity_usd"] = state["liquidity"]
        order = sorted(s["backers"], key=lambda w: -s["backers"][w])
        rows.append({
            "date": datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d"), "group": group,
            "snapshot_time": datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "market": s["market"]["title"], "side": side, "num_traders": len(s["backers"]),
            "entry_price": round(s["entry"], 4), "avg_price": round(s["avg"], 4),
            "total_usdc": round(total, 2), "opposing_usdc": round(s["opposing"], 2),
            "result": "", "return": "", "raw_values": dump_raw(raw),
            "condition_id": s["cid"], "wallets": ";".join(order),
            "source": "live" if live else "backfill", "lookahead": lookahead,
        })
    settle(rows)
    return rows


def settle(rows):
    """Fill result + return for rows whose market has ended (training.csv rows)."""
    for r in rows:
        if r.get("result"):
            continue
        state = market_state(r["condition_id"])
        price = state["prices"].get(r["side"])
        if state["closed"] and price is not None:
            won = price >= 0.99
            entry = float(r["entry_price"])
            r["result"] = "won" if won else "lost"
            r["return"] = round((1 - entry) / entry if won else -1.0, 4)


def load_training():
    if not os.path.exists(TRAINING):
        return []
    with open(TRAINING, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_training(rows):
    tmp = TRAINING + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRAINING_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["date"], r["snapshot_time"])))
    os.replace(tmp, TRAINING)


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
        raw = json.loads(r["raw_values"]) if r.get("raw_values") else None
        if raw is not None and "price_move_cents" not in raw:
            # market measurements at the moment we'd have bet (first seen), kept from then on
            raw.update(market_raw(avg, state, r["side"], price=entry))
            r["raw_values"] = dump_raw(raw)
        # regression's view at today's price: probability and edge over buying now
        r["model_prob"], r["model_edge"] = model_prediction(raw, r["group"], price)
        group = r["group"]
        parts = factor_values(r)
        if not parts:
            continue                           # row from an older version of the script
        parts.update(market_parts(group, avg, state, r["side"]))
        set_score(r, group, parts)


def set_score(r, group, parts):
    """Write factor values, points per factor and total score into a row.
    Factors missing from `parts` (unknown, e.g. in backfilled history) count as 0.5."""
    weights = WEIGHTS[group]
    r["factor_values"] = json.dumps({k: round(v, 4) for k, v in parts.items()})
    r["score_parts"] = format_parts(group, parts)
    r["score"] = round(100 * sum(w * parts.get(k, 0.5) for k, w in weights.items()), 1)


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


def load_rows(path):
    """{(date, group, condition_id, side): row} from a signals-style csv."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["date"], r["group"], r["condition_id"], r["side"]): r
                for r in csv.DictReader(f)}


def save_history(history):
    with open(HISTORY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(history.values(), key=lambda r: (r["date"], r["group"])))


def edge_text(wins, expected, bets):
    return f"{(wins - expected) / bets:+.1%}" if bets else ""


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))   # so scheduled runs find the files
    now = datetime.now()
    start_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    date, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M")

    rows, history = load_rows(OUTPUT), load_rows(HISTORY)
    update_prices(rows)            # settle finished markets first so records are current
    update_prices(history)
    learned = learn_weights({**history, **rows})   # learn from backfilled + live signals
    records = our_records({**history, **rows})
    traders = load_traders()

    names, leaderboard, members = {}, {}, {}
    found = [(group, *s) for group, tags in GROUPS.items()
             for s in find_signals(group, tags, start_ts, names, leaderboard, members)]

    # training snapshots: every top-trader position today (1, 2, 3... traders)
    snaps = {}
    for group, wallets in members.items():
        trades = [(t["timestamp"], w, t["conditionId"], t["outcomeIndex"], t["side"],
                   t["price"], t["size"], t["title"], t["outcome"], t["eventSlug"])
                  for w in wallets if len(_trades.get(w, [])) <= MAX_TRADES_PER_DAY
                  for t in _trades.get(w, [])]
        snaps[group] = replay_day(sorted(trades, key=lambda x: x[0]), GROUPS[group])

    refresh_stats(traders, {w for _, _, _, _, backers, _, _ in found for w in backers}
                  | {w for ss in snaps.values() for s in ss for w in s["backers"]})
    for w, (rank, pnl, vol) in leaderboard.items():
        if w in traders:
            traders[w].update(lb_rank=rank, lb_pnl=round(pnl, 2), lb_vol=round(vol, 2))

    today_rows = [r for group, ss in snaps.items()
                  for r in training_rows(ss, group, traders.get, records, names, live=True)]
    training = [r for r in load_training() if not (r["source"] == "live" and r["date"] == date)]
    settle(training)               # past snapshots whose markets have ended since
    save_training(training + today_rows)

    for group, m, cid, idx, backers, avg_price, opposing in found:
        side = m["outcomes"].get(idx, "Yes" if idx == 0 else "No")
        per_trader = {w: trader_parts(traders[w], records[w]) for w in backers}
        total = sum(backers.values())
        parts = {k: sum(p[k] for p in per_trader.values()) / len(per_trader)
                 for k in TRADER_FACTORS}
        parts.update(
            agreement=agreement_part(len(backers)),
            conviction=log_scale(total, *PROFILES[group]["conviction"]),
            consensus=total / (total + opposing),
        )
        order = sorted(backers, key=lambda w: -backers[w])
        key = (date, group, cid, side)
        old = rows.get(key, {})
        raw = {**mean_raw([trader_raw(traders[w], records[w]) for w in backers]),
               "num_traders": len(backers), "total_usdc": round(total, 2),
               "opposing_usdc": round(opposing, 2)}
        if old.get("raw_values"):      # keep market measurements taken when first seen
            raw.update({k: v for k, v in json.loads(old["raw_values"]).items()
                        if k in ("price_move_cents", "days_to_end", "liquidity_usd")})
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
            "factor_values": json.dumps({k: round(v, 4) for k, v in parts.items()}),
            "raw_values": dump_raw(raw),
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
    if history:
        save_history(history)

    # every trader who appears in any signal, with their latest stats and score
    seen = defaultdict(lambda: {"groups": set(), "dates": [], "n": 0})
    for r in rows.values():
        for w in filter(None, (r.get("wallets") or "").split(";")):
            seen[w]["groups"].add(r["group"])
            seen[w]["dates"].append(r["date"])
            seen[w]["n"] += 1
    refresh_stats(traders, seen)   # only fetches traders not refreshed today
    for w, t in traders.items():   # every trader we have stats for (signals + snapshots)
        if t.get("bets") in (None, ""):
            continue
        info = seen.get(w, {"groups": set(), "dates": [], "n": 0})
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
            first_seen=min(info["dates"]) if info["dates"] else t.get("first_seen", ""),
            last_seen=max(info["dates"]) if info["dates"] else t.get("last_seen", ""),
            score=round(trader_score(t, records[w])),
        )
    kept = [t for t in traders.values() if t.get("bets") not in (None, "")]
    with open(TRADERS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADER_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(kept, key=lambda t: (-int(t["num_signals"] or 0), -t["score"])))

    import paper_trading           # approve by rule, imaginary $1 bets, track + exit
    paper = paper_trading.run(snaps, traders, records, names)
    refit = refit_if_stale()

    log(f"{stamp}: {len(found)} signals today -> {OUTPUT} ({len(rows)} total), "
        f"{len(today_rows)} training snapshots today, {len(kept)} traders -> {TRADERS}; "
        f"{learned}; {paper}{refit}")


RSCRIPT = r"C:\Program Files\R\R-4.6.1\bin\Rscript.exe"
MODEL_MAX_AGE_HOURS = 24


def refit_if_stale():
    """Backup for the nightly task (which misses runs if the PC is off / asleep):
    if the model is over a day old, start model.R + patterns.R in the background."""
    import subprocess
    try:
        age = (time.time() - os.path.getmtime(MODEL_FILE)) / 3600
    except OSError:
        age = float("inf")
    if age < MODEL_MAX_AGE_HOURS or not os.path.exists(RSCRIPT):
        return ""
    command = (f'"{RSCRIPT}" model.R >> model_log.txt 2>&1 & '
               f'"{RSCRIPT}" patterns.R >> model_log.txt 2>&1')
    subprocess.Popen(f'cmd /c "{command}"', cwd=os.getcwd(),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"; model was {age:.0f}h old - refit started"


def notify(title, body):
    """Windows toast notification via PowerShell (no extra packages needed)."""
    import subprocess
    from xml.sax.saxutils import escape
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
