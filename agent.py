#!/usr/bin/env python3
"""Kalshi 1-hour BTC cushion caller (KXBTCD above/below ladder). READ-ONLY: it never places an order.

The 1-hour sibling of ~/kalshi-btc-agent. Each hourly event is a ladder of 'BTC above $X at the top of the hour'
contracts settled on the 60-second average of CF Benchmarks' BRTI. The 'call' is the ladder strike nearest the BRTI
proxy at the hour's open ('finish the hour above this level?'), the closest analogue of the 15-minute up/down market.
It shows a call only when the cushion clears the gate in gate.json (calibrated by study.py).

    agent.py              print the card for the current hour (no alert)
    agent.py --run        scheduled mode (launchd, every minute): log, settle, alert once per hour if enabled
    agent.py --scorecard  live record of every call, with a per-minute table
"""
import json, math, os, random, subprocess, sys, time
from datetime import datetime, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo
import feeds, model

HERE = os.path.dirname(os.path.abspath(__file__))
EVALS = os.path.join(HERE, "data", "evals.jsonl")
STATE = os.path.join(HERE, "data", "state.json")
CONFIG = os.path.join(HERE, "config.json")
ET = ZoneInfo("America/New_York")
MAX_PROXY_SPREAD = 60
GIT = "/usr/local/bin/git"


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def ts(s):
    return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def realized_sigma_1m(now):
    end = now - now % 60
    cb, bs = feeds.coinbase_1m(end - 3660, end), feeds.bitstamp_1m(end - 3660, end)
    both = sorted(set(cb) & set(bs))
    closes = [(cb[t][3] + bs[t][3]) / 2 for t in both]
    if len(closes) < 40:
        return None, None
    d = [b - a for a, b in zip(closes, closes[1:])]
    mu = sum(d) / len(d)
    return math.sqrt(sum((x - mu) ** 2 for x in d) / len(d)), {t: (cb[t][3] + bs[t][3]) / 2 for t in both}


def historical_hit(gate, conf):
    for row in gate.get("hit_table", []):
        if row["conf_from"] <= conf < row["conf_to"] + 1e-9:
            return row
    return None


def current_event():
    """The open hourly event that closes next."""
    ms = feeds.get(f"{feeds.KALSHI}/markets?series_ticker=KXBTCD&status=open&limit=1000").get("markets", [])
    if not ms:
        return None
    close = min(m["close_time"] for m in ms)
    return next(m["event_ticker"] for m in ms if m["close_time"] == close), ts(close)


def evaluate():
    gate = load(os.path.join(HERE, "gate.json"), None)
    if not gate:
        sys.exit("gate.json missing: run study.py first")
    now = int(time.time())
    ce = current_event()
    if not ce:
        return {"status": "no_window", "t": now}
    event, close = ce
    t0 = close - 3600
    ev = {"t": now, "event": event, "close": close, "minute": round((now - t0) / 60, 2), "minutes_left": round((close - now) / 60, 2)}
    if ev["minute"] < 0:
        return {**ev, "status": "no_call", "why": "the hour has not opened yet"}
    sig, closes = realized_sigma_1m(now)
    if not sig:
        return {**ev, "status": "no_call", "why": "not enough 1m history"}
    s0 = closes.get(t0 - 60) or closes[min(closes, key=lambda t: abs(t - (t0 - 60)))]
    tkr, K = feeds.strike_ticker(event, s0)
    try:
        mk = feeds.kalshi_market(tkr)
    except Exception:
        return {**ev, "status": "no_call", "why": f"strike market {tkr} not found"}
    ev.update(ticker=tkr, strike=K, spot_open=round(s0, 2), yes_bid=float(mk.get("yes_bid_dollars") or 0), yes_ask=float(mk.get("yes_ask_dollars") or 0))
    _, px = feeds.spot_now()
    ev["feeds"] = px
    if "coinbase" not in px or "bitstamp" not in px:
        return {**ev, "status": "no_call", "why": "a calibrated feed (Coinbase or Bitstamp) is down"}
    if abs(px["coinbase"] - px["bitstamp"]) > MAX_PROXY_SPREAD:
        return {**ev, "status": "no_call", "why": f"feeds disagree by ${abs(px['coinbase'] - px['bitstamp']):.0f}"}
    spot = (px["coinbase"] + px["bitstamp"]) / 2
    gap = spot - K
    p, sd = model.p_yes(gap, ev["minutes_left"], sig, gate["sigma_proxy"])
    side = "UP" if p >= .5 else "DOWN"
    conf = p if side == "UP" else 1 - p
    ask = ev["yes_ask"] if side == "UP" else 1 - ev["yes_bid"]
    ev.update(spot=round(spot, 2), gap=round(gap, 2), sigma_1m=round(sig, 2), sd_to_close=round(sd, 2), p_yes=round(p, 4), side=side,
              conf=round(conf, 4), z=round(abs(gap) / sd, 2), ask=round(ask, 4))
    if ev["minute"] < gate["min_minute"]:
        return {**ev, "status": "no_call", "why": f"only {ev['minute']:.0f} min in; the gate starts at minute {gate['min_minute']}"}
    if ev["minutes_left"] < 1:
        return {**ev, "status": "no_call", "why": "under a minute left; settlement averaging has started"}
    min_conf = load(CONFIG, {}).get("min_conf_override") or gate["min_conf"]
    min_z = NormalDist().inv_cdf(min_conf)
    if conf < min_conf:
        need = min_z * sd
        return {**ev, "status": "no_call", "why": f"cushion ${abs(gap):,.0f} = {ev['z']:.2f}σ, gate needs {min_z:.2f}σ (BTC above ${K + need:,.0f} or below ${K - need:,.0f})"}
    h = historical_hit(gate, conf)
    ev["hist_hit"], ev["hist_n"] = (h["hit"], h["n"]) if h else (None, None)
    if h and 0 < ask < 1:
        ev["edge"] = round(h["hit"] - ask - model.kalshi_fee(ask), 4)
    return {**ev, "status": "call"}


def fmt_et(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(ET).strftime("%-I:%M %p")


def card(ev):
    if ev.get("status") == "no_window":
        return "No open KXBTCD hourly event."
    rows = [("Hour", f"{fmt_et(ev['close'] - 3600)}–{fmt_et(ev['close'])} ET · minute {ev['minute']:.0f} of 60"),
            ("Strike", f"BTC above ${ev['strike']:,.2f} (nearest to the hour's open) · Kalshi YES {ev.get('yes_bid', 0) * 100:.0f}–{ev.get('yes_ask', 0) * 100:.0f}¢" if "strike" in ev else "—")]
    if "spot" in ev:
        rows.append(("Current BTC", f"${ev['spot']:,.0f} ({ev['gap']:+,.0f} {'above' if ev['gap'] >= 0 else 'below'} strike)"))
        rows.append(("Cushion", f"{ev['z']:.2f}σ: ${abs(ev['gap']):,.0f} vs ±${ev['sd_to_close']:,.0f} to close"))
    if ev["status"] != "call":
        rows += [("Call", "NO CALL"), ("Why", ev.get("why", ""))]
    else:
        rows.append(("P(" + ("above" if ev["side"] == "UP" else "below") + " strike)", f"model {ev['conf']:.1%} · calls like this won {ev['hist_hit']:.1%} (n={ev['hist_n']})"))
        rows += [("Call", f"{ev['side']} — {ev['hist_hit']:.0%}"), ("Kalshi ask", f"{ev['side']} side {ev['ask'] * 100:.0f}¢")]
        if ev.get("edge") is not None:
            rows.append(("Edge vs ask", f"{ev['edge'] * 100:+.1f}¢ after fee — " + ("priced in, no edge" if ev["edge"] <= 0 else "positive, unproven")))
    w = max(len(r[0]) for r in rows)
    return "BTC 1-hour (KXBTCD) · BRTI proxy (Coinbase+Bitstamp)\n\n" + "\n".join(f"  {k.ljust(w)}  {v}" for k, v in rows)


def notify(ev, cfg):
    title = f"BTC 1H {fmt_et(ev['close'])} {ev['side']} {ev['hist_hit']:.0%} · ${abs(ev['gap']):,.0f} cushion · ask {ev['ask'] * 100:.0f}¢"
    def run(cmd):
        try:
            subprocess.run(cmd, capture_output=True, timeout=20)
        except Exception as e:
            print(f"alert channel {cmd[0]} failed: {e}", file=sys.stderr)
    if cfg.get("banner", False):
        run(["/usr/bin/osascript", "-e", f'display notification "" with title "{title}"'])
    if cfg.get("sound", False):
        run(["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"])


def settle(state):
    pending = state.setdefault("pending", {})
    for tkr, rec in list(pending.items()):
        if time.time() < rec["close"] + 180:
            continue
        try:
            mk = feeds.kalshi_market(tkr)
        except Exception:
            continue
        if mk.get("result") not in ("yes", "no"):
            continue
        won = (mk["result"] == "yes") == (rec["side"] == "UP")
        with open(EVALS, "a") as f:
            f.write(json.dumps({"t": int(time.time()), "ticker": tkr, "status": "settled", "result": mk["result"], "side": rec["side"],
                                "won": won, "conf": rec["conf"], "hist_hit": rec["hist_hit"], "ask": rec["ask"]}) + "\n")
        pending.pop(tkr)


def run():
    cfg = load(CONFIG, {}).get("alerts", {})
    state = load(STATE, {})
    try:
        settle(state)
    except Exception as e:
        print(f"settle: {e}", file=sys.stderr)
    ev = evaluate()
    if ev.get("status") != "no_window":
        ev.pop("feeds", None)
        with open(EVALS, "a") as f:
            f.write(json.dumps(ev) + "\n")
    if ev.get("status") == "call":
        alerted = state.setdefault("alerted", {})
        if alerted.get(ev["ticker"]) != ev["side"]:
            alerted[ev["ticker"]] = ev["side"]
            state.setdefault("pending", {})[ev["ticker"]] = {k: ev[k] for k in ("close", "side", "conf", "hist_hit", "ask")}
            notify(ev, cfg)
            print(card(ev))
        state["alerted"] = {k: v for k, v in alerted.items() if k == ev["ticker"] or k in state["pending"]}
    json.dump(state, open(STATE, "w"), indent=1)
    try:
        autocommit()
    except Exception as e:
        print(f"autocommit: {e!r}", file=sys.stderr)


def autocommit():
    """Hourly: commit and push data/evals.jsonl when dirty and the last commit is an hour old; failures retry next minute."""
    def git(*a):
        r = subprocess.run([GIT, *a], cwd=HERE, capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    ok, out = git("status", "--porcelain", "data/evals.jsonl")
    if not ok or not out:
        return
    ok, last = git("log", "-1", "--format=%ct", "--", "data/evals.jsonl")
    if ok and last and time.time() - int(last) < 3600:
        return
    ok, _ = git("add", "data/evals.jsonl")
    if ok:
        ok, out = git("-c", "user.name=kalshi-btc-1h-agent", "-c", "user.email=darup67@gmail.com", "commit", "-q", "-m", "data: evals.jsonl", "--", "data/evals.jsonl")
    if ok:
        ok, out = git("push", "-q", "origin", "HEAD")
    if not ok:
        print(f"autocommit: git failed: {out[-200:]}", file=sys.stderr)


def scorecard():
    rows = [json.loads(l) for l in open(EVALS)] if os.path.exists(EVALS) else []
    s = [r for r in rows if r.get("status") == "settled"]
    evals = [r for r in rows if r.get("status") in ("call", "no_call")]
    print(f"{len(evals)} evaluations logged, {len(s)} calls settled")
    if not s:
        return
    k = sum(r["won"] for r in s)
    pnl = [int(r["won"]) - r["ask"] - model.kalshi_fee(min(max(r["ask"], .01), .99)) for r in s]
    print(f"hit rate {k}/{len(s)} = {k / len(s):.1%} · expected {sum(r['hist_hit'] for r in s) / len(s):.1%} · "
          f"Brier {sum((r['hist_hit'] - r['won']) ** 2 for r in s) / len(s):.4f} · paper EV at ask {sum(pnl) / len(pnl) * 100:+.1f}¢/contract (no orders placed)")
    first = {}
    for r in evals:
        if r["status"] == "call":
            first.setdefault(r["ticker"], r)
    by = {}
    for r, p in zip(s, pnl):
        c = first.get(r["ticker"])
        if c:
            by.setdefault(int(c["minute"] // 5 * 5), []).append((r["won"], r["ask"], p))
    days = len({time.strftime("%Y-%m-%d", time.gmtime(r["t"])) for r in s})
    rng = random.Random(1)
    print(f"by minute of the first call in each hour, 5-minute buckets ({days} separate days; 95% range treats calls as independent, so it is too narrow):")
    print("  min    n   hit    ask   EV¢/contract   95% range")
    for m in sorted(by):
        v, n = by[m], len(by[m])
        pn = [x[2] for x in v]
        b = sorted(sum(rng.choice(pn) for _ in range(n)) / n * 100 for _ in range(2000))
        print(f"  {m:>3} {n:>4} {sum(x[0] for x in v) / n:5.1%}  {sum(x[1] for x in v) / n:.2f}   {sum(pn) / n * 100:+8.1f}      [{b[50]:+.1f}, {b[1949]:+.1f}]")
    print("  Not proven until 10+ separate days of calls.")


if __name__ == "__main__":
    if "--run" in sys.argv:
        run()
    elif "--scorecard" in sys.argv:
        scorecard()
    else:
        print(card(evaluate()))
