#!/usr/bin/env python3
"""Build the 1-hour dataset: one 'up/down' observation window per settled KXBTCD hourly event.

Window = the 60 minutes before the hourly close. The 'call' is the ladder strike nearest the BRTI proxy
at the window's open ('will BTC finish the hour above this level?'), so it is the closest analogue of the
15-minute up/down market. For each window we keep that strike's outcome and BRTI settlement value
(data/windows-1h.jsonl, committed: Kalshi will not serve this history forever) and its per-minute YES
bid/ask from Kalshi candlesticks (cached, not committed). Spot = mean of Coinbase and Bitstamp 1m closes.

    ~/.venvs/market-ml/bin/python data.py
"""
import json, os, sys, time
import feeds

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")


def spot_series(start, end):
    path = os.path.join(D, f"spot-{start}-{end}.json")
    if os.path.exists(path):
        return {int(k): v for k, v in json.load(open(path)).items()}
    print(f"fetching Coinbase + Bitstamp 1m {feeds.iso(start)} -> {feeds.iso(end)} ...", file=sys.stderr)
    cb, bs = feeds.coinbase_1m(start, end), feeds.bitstamp_1m(start, end)
    both = sorted(set(cb) & set(bs))
    out = {t: (cb[t][3] + bs[t][3]) / 2 for t in both}
    json.dump(out, open(path, "w"))
    return out


def build():
    events = feeds.kalshi_hourly_events(True)
    events = [e for e in events if e[1] % 3600 == 0]
    spot = spot_series(events[0][1] - 3600 - 4 * 3600, events[-1][1] + 120)
    wpath = os.path.join(D, "windows-1h.jsonl")
    have = {}
    if os.path.exists(wpath):
        for l in open(wpath):
            r = json.loads(l)
            have[r["event"]] = r
    cpath = os.path.join(D, "kalshi-candles-1h.json")
    candles = json.load(open(cpath)) if os.path.exists(cpath) else {}
    new = skipped = 0
    for n, (ev, close) in enumerate(events):
        if ev in have:
            continue
        t0 = close - 3600
        s0 = spot.get(t0 - 60)
        if s0 is None:
            skipped += 1
            continue
        tkr, k = feeds.strike_ticker(ev, s0)
        try:
            m = feeds.kalshi_market(tkr)
            if m.get("result") not in ("yes", "no"):
                skipped += 1
                continue
            c = feeds.kalshi_candles_ticker(tkr, t0, close)
        except Exception:
            skipped += 1
            continue
        candles[tkr] = c
        have[ev] = dict(event=ev, ticker=tkr, open_ts=t0, close_ts=close, strike=k, spot_open=round(s0, 2),
                        settle=m.get("expiration_value"), result=m["result"])
        new += 1
        if new % 100 == 0:
            json.dump(candles, open(cpath, "w"))
            print(f"  {n + 1}/{len(events)} events, {new} new windows", file=sys.stderr)
        time.sleep(0.1)
    json.dump(candles, open(cpath, "w"))
    with open(wpath, "w") as f:
        for r in sorted(have.values(), key=lambda r: r["open_ts"]):
            f.write(json.dumps(r) + "\n")
    print(f"{len(have)} windows ({new} new, {skipped} skipped)", file=sys.stderr)


if __name__ == "__main__":
    build()
