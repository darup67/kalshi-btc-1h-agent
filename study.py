#!/usr/bin/env python3
"""
1-hour BTC study (Kalshi KXBTCD above/below ladder, BTC only). One report, four questions, at minutes 1, 3, 4, 5, 15, 30, 45:

  1. How often is the market favourite right?  (market = YES mid of the strike nearest the hour's open)
  2. Does the cushion model, Chronos or AutoGluon beat the market price?
  3. Is higher or lower volatility better for the call?  (trailing 1/3/4/5-minute-candle volatility + in-hour volatility)
  4. Gate calibration for the live agent (gate.json), by the BTC rule fixed in advance: the lowest confidence whose
     fit-half hit rate has a Wilson lower bound >= 90% (n >= 30), reported on the held-out half.

Split: first half of the hours (chronological) to fit, second half to test. Not pre-registered: leads, not findings.
Volatility CIs resample whole days. Chronos/AutoGluon see only what is known at the mark.

    ~/.venvs/market-ml/bin/python study.py [--fast]      (--fast skips Chronos and AutoGluon)
"""
import json, math, os, sys, time, warnings
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist
import numpy as np
import pandas as pd
import data as D
import feeds, model

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
MARKS = [1, 3, 4, 5, 15, 30, 45]          # volatility, Chronos, AutoGluon, gate
CURVE = list(range(1, 56))                  # accuracy-by-minute curve (market, cushion, naive only)
ALL_MARKS = sorted(set(MARKS) | set(CURVE))
CURVE_TABLE = [1, 2, 3, 4, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
VOLS = ["rv1", "rv3", "rv4", "rv5", "rv_win"]
P_GRID = [0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.97, 0.99]
SIGMA_PROXY, TARGET_HIT, MIN_N, CTX, SEED = 8.61, 0.90, 30, 512, 11
QL = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
N = NormalDist()


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)).astype(float), np.argsort(np.argsort(b)).astype(float)
    return float("nan") if ra.std() == 0 or rb.std() == 0 else float(np.corrcoef(ra, rb)[0, 1])


def logit(X, y, iters=50):
    X = np.column_stack([np.ones(len(y)), X])
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ b, -30, 30)))
        W = p * (1 - p) + 1e-9
        step = np.linalg.solve(X.T @ (X * W[:, None]) + 1e-6 * np.eye(X.shape[1]), X.T @ (y - p))
        b += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


def day_boot(obs, stat, reps=300):
    by = defaultdict(list)
    for o in obs:
        by[o["day"]].append(o)
    days = sorted(by)
    rng = np.random.default_rng(SEED)
    vals = []
    for _ in range(reps):
        try:
            v = stat([o for d in rng.choice(days, len(days)) for o in by[d]])
            if v == v:
                vals.append(v)
        except Exception:
            pass
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))) if len(vals) > 20 else (float("nan"),) * 2


# ── observations ────────────────────────────────────────────────────────────
def scaled_std(closes, k):
    pts = closes[::-1][::k][::-1]
    r = np.diff(np.log(pts))
    return float(np.std(r, ddof=1) / math.sqrt(k) * 1e4) if len(r) >= 8 else None


def load():
    wins = [json.loads(l) for l in open(os.path.join(D.D, "windows-1h.jsonl"))]
    candles = json.load(open(os.path.join(D.D, "kalshi-candles-1h.json")))
    start, end = wins[0]["open_ts"] - 4 * 3600 - 3600, wins[-1]["close_ts"] + 120
    paths = [p for p in os.listdir(D.D) if p.startswith("spot-")]
    spot = {}
    for p in paths:
        spot.update({int(k): v for k, v in json.load(open(os.path.join(D.D, p))).items()})
    return wins, candles, spot


def build(wins, candles, spot):
    obs = []
    for i, w in enumerate(wins):
        t0, K = w["open_ts"], w["strike"]
        cd = candles.get(w["ticker"], {})
        y = int(w["result"] == "yes")
        for m in ALL_MARKS:
            q = cd.get(str(t0 + m * 60))
            if not q or not (0 < q[0] <= q[1] < 1) or q[1] - q[0] > 0.10:
                continue
            bar = t0 + (m - 1) * 60
            trail = [spot.get(t) for t in range(bar - 60 * 60, bar + 60, 60)]
            inwin = [spot.get(t) for t in range(t0 - 60, bar + 60, 60)]
            if any(v is None for v in trail) or any(v is None for v in inwin):
                continue
            core = m in MARKS
            ctx = [spot.get(t) for t in range(bar - (CTX - 1) * 60, bar + 60, 60)] if core else []
            ctx = [v for v in ctx if v is not None]
            px = trail[-1]
            gap = px - K
            if gap == 0 or (core and len(ctx) < 128):
                continue
            feats = {f"rv{k}": scaled_std(trail, k) for k in (1, 3, 4, 5)}
            feats["rv_win"] = float(np.sqrt(np.mean(np.diff(np.log(inwin)) ** 2)) * 1e4)
            if any(v is None or v <= 0 for v in feats.values()):
                continue
            sig = float(np.std(np.diff(trail), ddof=1))
            left = 60 - m
            p, sd = model.p_yes(gap, left, sig, SIGMA_PROXY)
            mid = (q[0] + q[1]) / 2
            fav_up = mid > 0.5
            ask = q[1] if fav_up else 1 - q[0]
            t_et = datetime.fromtimestamp(t0, timezone.utc)
            obs.append(dict(i=i, m=m, day=t_et.strftime("%Y-%m-%d"), y=y, mid=mid, ask=ask, ask_yes=q[1], ask_no=1 - q[0],
                            hit_mkt=int((y == 1) == fav_up), hit_gap=int((y == 1) == (gap > 0)), p=p, absz=abs(gap) / sd,
                            ev=int((y == 1) == fav_up) - ask - model.fee_(ask) if hasattr(model, "fee_") else int((y == 1) == fav_up) - ask - model.kalshi_fee(min(max(ask, .01), .99)),
                            ctx=ctx if core else None, strike=K, **feats,
                            f=dict(gap=gap, z=gap / sd, sig=sig, left=left, r1=(ctx[-1] / ctx[-2] - 1) * 1e4,
                                   r5=(ctx[-1] / ctx[-6] - 1) * 1e4, r15=(ctx[-1] / ctx[-16] - 1) * 1e4,
                                   r60=(ctx[-1] / ctx[-61] - 1) * 1e4, rv16=float(np.std(np.diff(ctx[-17:]))),
                                   hour=t_et.hour, dow=t_et.weekday()) if core else None))
    return obs


# ── models ──────────────────────────────────────────────────────────────────
_pipes = {}
def chronos(name, obs):
    import torch
    from chronos import BaseChronosPipeline
    if name not in _pipes:
        _pipes[name] = BaseChronosPipeline.from_pretrained(name, device_map="cpu", torch_dtype=torch.float32)
    pipe, out = _pipes[name], {}
    by = defaultdict(list)
    for k, o in enumerate(obs):
        by[o["m"]].append(k)
    for m, idx in by.items():
        h = 60 - m
        for s in range(0, len(idx), 48):
            chunk = idx[s:s + 48]
            batch = [torch.tensor(obs[k]["ctx"][-CTX:], dtype=torch.float32) for k in chunk]
            q, _ = pipe.predict_quantiles(batch, prediction_length=h, quantile_levels=QL)
            q = q.numpy() if hasattr(q, "numpy") else np.asarray(q)
            for j, k in enumerate(chunk):
                v = np.maximum.accumulate(q[j].reshape(h, len(QL))[-1].astype(float))
                K = obs[k]["strike"]
                cdf = QL[0] * .5 if K <= v[0] else (1 - (1 - QL[-1]) * .5 if K >= v[-1] else float(np.interp(K, v, QL)))
                out[k] = 1.0 - cdf
    return [out[k] for k in range(len(obs))]


def ag_tab(train, test, with_market, tag):
    from autogluon.tabular import TabularPredictor
    fr = lambda os_: pd.DataFrame([{**o["f"], "m": o["m"], **({"market": o["mid"]} if with_market else {}), "y": o["y"]} for o in os_])
    tr, te = fr(train), fr(test)
    pred = TabularPredictor(label="y", problem_type="binary", eval_metric="log_loss",
                            path=os.path.join(os.environ.get("MLTEST_WORK", "/tmp/kalshi-1h"), f"ag-{tag}"), verbosity=0)
    pred.fit(tr, presets="good_quality", time_limit=int(os.environ.get("AGTAB_LIMIT", 240)), excluded_model_types=["FASTAI"])
    return list(pred.predict_proba(te.drop(columns=["y"]))[1].to_numpy())


def metrics(p, y):
    p = np.clip(np.asarray(p, float), 1e-3, 1 - 1e-3)
    y = np.asarray(y, int)
    hit = (p > .5) == (y == 1)
    return dict(acc=float(hit.mean()), ci=wilson(int(hit.sum()), len(y)), brier=float(np.mean((p - y) ** 2)), hit=hit)


def mcnemar(a, b):
    x, z = int(np.sum(a & ~b)), int(np.sum(~a & b))
    n = x + z
    if n == 0:
        return 1.0, x, z
    k = min(x, z)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), x, z


# ── volatility ──────────────────────────────────────────────────────────────
def vol_analyze(obs, vol):
    v = np.array([o[vol] for o in obs])
    lv = np.log(v)
    mu, sd = lv.mean(), lv.std()
    qs = np.quantile(v, [.2, .4, .6, .8])
    qi = np.searchsorted(qs, v, side="right")
    quint = []
    for k in range(5):
        sel = [o for o, i in zip(obs, qi) if i == k]
        n = len(sel)
        quint.append(dict(n=n, acc=sum(o["hit_mkt"] for o in sel) / n, ev=float(np.mean([o["ev"] for o in sel])),
                          absz=float(np.median([o["absz"] for o in sel]))))

    def slope(os_, ctrl):
        zz = (np.log([o[vol] for o in os_]) - mu) / sd
        y = np.array([o["hit_mkt"] for o in os_], float)
        X = np.column_stack([zz, np.log1p([o["absz"] for o in os_])]) if ctrl else zz[:, None]
        return float(logit(X, y)[1])

    def evrho(os_):
        return spearman([o[vol] for o in os_], [o["ev"] for o in os_])
    return dict(n=len(obs), quint=quint, slope=slope(obs, False), slope_ci=day_boot(obs, lambda o: slope(o, False)),
                ctrl=slope(obs, True), ctrl_ci=day_boot(obs, lambda o: slope(o, True)),
                evrho=evrho(obs), evrho_ci=day_boot(obs, evrho))


def vol_verdict(r):
    lo, hi = r["slope_ci"]
    return "higher vol → MORE accurate" if lo > 0 else ("higher vol → LESS accurate" if hi < 0 else "no clear effect")


# ── main ────────────────────────────────────────────────────────────────────
def main():
    fast = "--fast" in sys.argv
    stamp = datetime.now().strftime("%Y-%m-%d")
    wins, candles, spot = load()
    obs_all = build(wins, candles, spot)
    obs = [o for o in obs_all if o["m"] in MARKS]
    half_i = wins[len(wins) // 2]["open_ts"]
    A = [o for o in obs if wins[o["i"]]["open_ts"] < half_i]
    B = [o for o in obs if wins[o["i"]]["open_ts"] >= half_i]
    days = len({o["day"] for o in obs})
    L = [f"# 1-hour BTC (Kalshi KXBTCD) — study — {stamp}", "",
         f"{len(wins)} hourly windows ({datetime.fromtimestamp(wins[0]['open_ts'], timezone.utc):%Y-%m-%d} → "
         f"{datetime.fromtimestamp(wins[-1]['close_ts'], timezone.utc):%Y-%m-%d}), {len(obs)} observations at minutes "
         f"{', '.join(map(str, MARKS))} (plus an every-minute accuracy curve from {len(obs_all)} observations); fit {len(A)} / test {len(B)} (chronological), {days} days. "
         "The call = the ladder strike nearest the hour's open ('finish the hour above this level'). Not pre-registered.", ""]
    res = {}

    # 1. accuracy curve across the hour
    by_m = defaultdict(list)
    for o in obs_all:
        by_m[o["m"]].append(o)
    curve = {}
    for m in CURVE:
        om = by_m.get(m, [])
        if len(om) < 50:
            continue
        k = sum(o["hit_mkt"] for o in om)
        lo, hi = wilson(k, len(om))
        curve[m] = dict(n=len(om), acc=k / len(om), lo=lo, hi=hi, gap=sum(o["hit_gap"] for o in om) / len(om),
                        cush=float(np.mean([(o["p"] > .5) == (o["y"] == 1) for o in om])),
                        brier_mkt=float(np.mean([(o["mid"] - o["y"]) ** 2 for o in om])),
                        brier_cush=float(np.mean([(o["p"] - o["y"]) ** 2 for o in om])),
                        ev=float(np.mean([o["ev"] for o in om])), absz=float(np.median([o["absz"] for o in om])))
    L += ["## 1. At what point in the hour is the call most often correct?", "",
          "Market favourite = the side Kalshi's price favours for the strike nearest the hour's open. Naive = spot above/below that strike. "
          "Cushion = the BTC model (kalshi-btc-agent). Brier: lower is better (0.25 = coin flip).", "",
          "| minute | windows | market right [95% CI] | naive spot-vs-strike | cushion model | Brier market | Brier cushion | median cushion \\|z\\| | EV favourite @ ask |",
          "|---|---|---|---|---|---|---|---|---|"]
    for m in CURVE_TABLE:
        c = curve.get(m)
        if c:
            L.append(f"| {m} | {c['n']} | {c['acc']:.1%} [{c['lo']:.0%}–{c['hi']:.0%}] | {c['gap']:.1%} | {c['cush']:.1%} | {c['brier_mkt']:.4f} | {c['brier_cush']:.4f} | {c['absz']:.2f} | {c['ev'] * 100:+.2f}¢ |")
    ms = sorted(curve)
    top = sorted(ms, key=lambda m: -curve[m]["acc"])[:5]
    lastm = ms[-1]
    L += ["", f"- Most accurate minutes (market): " + ", ".join(f"{m} ({curve[m]['acc']:.1%})" for m in top) + f"; least accurate: minute {min(ms, key=lambda m: curve[m]['acc'])} ({min(c['acc'] for c in curve.values()):.1%}).",
          f"- Accuracy rises from {curve[ms[0]]['acc']:.1%} at minute {ms[0]} to {curve[lastm]['acc']:.1%} at minute {lastm}, as less time is left for BTC to cross back over the strike.",
          f"- Where the market adds the most over the naive spot-vs-strike call: " + ", ".join(f"minute {m} ({(curve[m]['acc'] - curve[m]['gap']) * 100:+.1f} pts)" for m in sorted(ms, key=lambda m: -(curve[m]['acc'] - curve[m]['gap']))[:3]) + ".",
          f"- Best expected value buying the favourite at the ask: " + ", ".join(f"minute {m} ({curve[m]['ev'] * 100:+.1f}¢)" for m in sorted(ms, key=lambda m: -curve[m]['ev'])[:3]) + "; worst: " + ", ".join(f"minute {m} ({curve[m]['ev'] * 100:+.1f}¢)" for m in sorted(ms, key=lambda m: curve[m]['ev'])[:3]) + ".", ""]
    res["curve"] = {str(m): c for m, c in curve.items()}
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 3, figsize=(15, 4.1))
        x = ms
        ax[0].fill_between(x, [curve[m]["lo"] * 100 for m in x], [curve[m]["hi"] * 100 for m in x], color="#2f5bea", alpha=.15)
        ax[0].plot(x, [curve[m]["acc"] * 100 for m in x], color="#2f5bea", label="market favourite")
        ax[0].plot(x, [curve[m]["cush"] * 100 for m in x], color="#c25a24", ls="--", label="cushion model")
        ax[0].plot(x, [curve[m]["gap"] * 100 for m in x], color="#7d8696", ls=":", label="naive spot vs strike")
        ax[0].set_title("Correct up/down call (%)"); ax[0].legend(frameon=False)
        ax[1].plot(x, [curve[m]["brier_mkt"] for m in x], color="#2f5bea", label="market")
        ax[1].plot(x, [curve[m]["brier_cush"] for m in x], color="#c25a24", ls="--", label="cushion")
        ax[1].axhline(.25, color="#aaa", lw=.8); ax[1].set_title("Brier score (lower is better)"); ax[1].legend(frameon=False)
        ax[2].bar(x, [curve[m]["ev"] * 100 for m in x], color=["#18794e" if curve[m]["ev"] > 0 else "#b91c1c" for m in x])
        ax[2].axhline(0, color="#888", lw=.8); ax[2].set_title("EV of buying the favourite at the ask (cents)")
        for a in ax:
            a.set_xlabel("minute into the hour"); a.grid(alpha=.25)
        fig.suptitle("Kalshi 1-hour BTC (KXBTCD): how correct is the price across the hour?", fontsize=11)
        fig.tight_layout()
        png = os.path.join(HERE, "results", f"study-{stamp}-accuracy-curve.png")
        fig.savefig(png, dpi=140)
        L += [f"![accuracy across the hour](study-{stamp}-accuracy-curve.png)", ""]
    except Exception as e:
        print(f"chart skipped: {e}", file=sys.stderr)

    # 2. models vs market (test half)
    L += ["## 2. Cushion model, Chronos and AutoGluon vs. the market price (test half)", "",
          "Percent of hours where each predictor's up/down call matched the settlement; 'vs market' = only-this-model-right / only-market-right, exact McNemar p.", ""]
    tests = {m: [o for o in B if o["m"] == m] for m in MARKS}
    preds = {m: {"market": [o["mid"] for o in tests[m]], "cushion": [o["p"] for o in tests[m]]} for m in MARKS}
    if not fast:
        flat = [o for m in MARKS for o in tests[m]]
        for name, mdl in (("chronos-bolt", "amazon/chronos-bolt-base"), ("chronos-2", "amazon/chronos-2")):
            t0 = time.time()
            pr = chronos(mdl, flat)
            print(f"  {name} done in {time.time() - t0:.0f}s", file=sys.stderr)
            pos = 0
            for m in MARKS:
                preds[m][name] = pr[pos:pos + len(tests[m])]
                pos += len(tests[m])
        for name, wm in (("ag-tab", False), ("ag-tab+mkt", True)):
            t0 = time.time()
            pr = ag_tab(A, [o for m in MARKS for o in tests[m]], wm, name)
            print(f"  {name} done in {time.time() - t0:.0f}s", file=sys.stderr)
            pos = 0
            for m in MARKS:
                preds[m][name] = pr[pos:pos + len(tests[m])]
                pos += len(tests[m])
    names = list(preds[MARKS[0]].keys())
    L += ["| predictor | " + " | ".join(f"min {m}" for m in MARKS if tests[m]) + " |", "|---" * (1 + len([m for m in MARKS if tests[m]])) + "|"]
    for nme in names:
        row = []
        for m in MARKS:
            if not tests[m]:
                continue
            y = [o["y"] for o in tests[m]]
            a, base = metrics(preds[m][nme], y), metrics(preds[m]["market"], y)
            if nme == "market":
                row.append(f"**{a['acc']:.1%}**")
            else:
                pv, x, z = mcnemar(a["hit"], base["hit"])
                row.append(f"{a['acc']:.1%}" + ("*" if pv < 0.05 else ""))
        L.append(f"| {nme} | " + " | ".join(row) + " |")
    L += ["", "\\* differs from the market price at p < 0.05 (every starred model is worse unless noted in the JSON).", ""]
    res["models"] = {str(m): {n: metrics(preds[m][n], [o["y"] for o in tests[m]])["acc"] for n in names} for m in MARKS if tests[m]}

    # 3. volatility
    L += ["## 3. Is higher or lower volatility better for the call?", "",
          "Slope = change in log-odds that the market favourite is right per +1 SD of log trailing 60-minute volatility (1-minute candles). "
          "'Beyond cushion' also controls for how far price already is from the strike. CIs resample whole days.", "",
          "| minute | windows | right: calm → volatile (quintile 1 → 5) | slope [95% CI] | beyond cushion [95% CI] | EV vs vol ρ [95% CI] | verdict |", "|---|---|---|---|---|---|---|"]
    vres = {}
    for m in MARKS:
        om = [o for o in obs if o["m"] == m]
        if len(om) < 150:
            continue
        r = vol_analyze(om, "rv1")
        vres[str(m)] = r
        L.append(f"| {m} | {r['n']} | {r['quint'][0]['acc']:.1%} → {r['quint'][4]['acc']:.1%} | {r['slope']:+.3f} [{r['slope_ci'][0]:+.3f}, {r['slope_ci'][1]:+.3f}] | "
                 f"{r['ctrl']:+.3f} [{r['ctrl_ci'][0]:+.3f}, {r['ctrl_ci'][1]:+.3f}] | {r['evrho']:+.3f} [{r['evrho_ci'][0]:+.3f}, {r['evrho_ci'][1]:+.3f}] | {vol_verdict(r)} |")
    L += ["", "All marks pooled, each volatility measure (1-, 3-, 4-, 5-minute candles and in-hour volatility):", "",
          "| measure | slope [95% CI] | beyond cushion [95% CI] | EV vs vol ρ [95% CI] | top − bottom quintile |", "|---|---|---|---|---|"]
    for vol in VOLS:
        r = vol_analyze(obs, vol)
        res.setdefault("vol_pooled", {})[vol] = dict(slope=r["slope"], slope_ci=r["slope_ci"], ctrl=r["ctrl"], ctrl_ci=r["ctrl_ci"])
        L.append(f"| {vol} | {r['slope']:+.3f} [{r['slope_ci'][0]:+.3f}, {r['slope_ci'][1]:+.3f}] | {r['ctrl']:+.3f} [{r['ctrl_ci'][0]:+.3f}, {r['ctrl_ci'][1]:+.3f}] | "
                 f"{r['evrho']:+.3f} [{r['evrho_ci'][0]:+.3f}, {r['evrho_ci'][1]:+.3f}] | {(r['quint'][4]['acc'] - r['quint'][0]['acc']) * 100:+.1f} pts |")
    z = np.array([(np.log(o["rv1"]) - np.mean([np.log(x["rv1"]) for x in obs])) for o in obs])
    tq = np.searchsorted(np.quantile(z, [1 / 3, 2 / 3]), z)
    L += ["", "Buying the market favourite at the ask, by volatility tercile (all marks):", "",
          "| volatility | n | favourite right | avg ask | EV after fee |", "|---|---|---|---|---|"]
    for k, lab in enumerate(("calm", "middle", "volatile")):
        idx = np.where(tq == k)[0]
        L.append(f"| {lab} | {len(idx)} | {np.mean([obs[i]['hit_mkt'] for i in idx]):.1%} | {np.mean([obs[i]['ask'] for i in idx]):.3f} | {np.mean([obs[i]['ev'] for i in idx]) * 100:+.2f}¢ |")
    L.append("")
    res["vol_by_mark"] = {m: {k: (v if k != "quint" else v) for k, v in r.items()} for m, r in vres.items()}

    # 4. gate
    def calls(os_, pmin):
        out = []
        for o in os_:
            up = o["p"] >= .5
            conf = o["p"] if up else 1 - o["p"]
            if conf >= pmin:
                px = o["ask_yes"] if up else o["ask_no"]
                out.append((int(o["y"] == int(up)), conf, px, o))
        return out
    fee = lambda x: model.kalshi_fee(min(max(x, .01), .99))
    ev_of = lambda cs: float(np.mean([c[0] - c[2] - fee(c[2]) for c in cs])) if cs else float("nan")
    L += ["## 4. Gate calibration (the live agent's rule)", "",
          "Call = the side the cushion model favours, only when its confidence clears the bar; `paid` = Kalshi ask for that side at that minute.", "",
          "| min conf | fit n | fit hit [95% CI] | test n | test hit [95% CI] | test avg paid | test EV/contract |", "|---|---|---|---|---|---|---|"]
    chosen = None
    for pm in P_GRID:
        a, b = calls(A, pm), calls(B, pm)
        ka, kb = sum(c[0] for c in a), sum(c[0] for c in b)
        la, ha = wilson(ka, len(a))
        lb, hb = wilson(kb, len(b))
        L.append(f"| {pm:.2f} | {len(a)} | {ka / max(len(a), 1):.1%} [{la:.0%}–{ha:.0%}] | {len(b)} | {kb / max(len(b), 1):.1%} [{lb:.0%}–{hb:.0%}] | "
                 f"{np.mean([c[2] for c in b]) if b else float('nan'):.2f} | {ev_of(b):+.3f} |")
        if chosen is None and len(a) >= MIN_N and la >= TARGET_HIT:
            chosen = pm
    chosen = chosen or P_GRID[-1]
    L += ["", f"Chosen gate: **{chosen:.2f}** (lowest confidence with fit-half Wilson lower bound ≥ {TARGET_HIT:.0%}, n ≥ {MIN_N}).", ""]
    L += [f"By minute at the chosen gate (test half):", "", "| minute | calls | hit | avg ask | EV/contract |", "|---|---|---|---|---|"]
    for m in MARKS:
        b = [c for c in calls(B, chosen) if c[3]["m"] == m]
        if b:
            L.append(f"| {m} | {len(b)} | {np.mean([c[0] for c in b]):.1%} | {np.mean([c[2] for c in b]):.2f} | {ev_of(b) * 100:+.1f}¢ |")
    hit_table, edges, allc = [], [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 1.0001], calls(A + B, 0.5)
    for lo, hi in zip(edges, edges[1:]):
        sel = [c for c in allc if lo <= c[1] < hi]
        if sel:
            k = sum(c[0] for c in sel)
            l_, h_ = wilson(k, len(sel))
            hit_table.append(dict(conf_from=lo, conf_to=min(hi, 1.0), n=len(sel), hit=round(k / len(sel), 4), ci=[round(l_, 3), round(h_, 3)]))
    gate = dict(min_conf=chosen, min_z=round(N.inv_cdf(chosen), 2), min_minute=5, hit_table=hit_table, sigma_proxy=SIGMA_PROXY,
                calibrated_on=f"{len(wins)} hourly windows to {datetime.fromtimestamp(wins[-1]['close_ts'], timezone.utc):%Y-%m-%d}",
                calibrated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    json.dump(gate, open(os.path.join(HERE, "gate.json"), "w"), indent=2)
    L += ["", "```json", json.dumps(gate, indent=2), "```"]
    out = os.path.join(HERE, "results", f"study-{stamp}{'-fast' if fast else ''}.md")
    open(out, "w").write("\n".join(L) + "\n")
    json.dump(res, open(out.replace(".md", ".json"), "w"), indent=1, default=float)
    print("\n".join(L[:60]))
    print(f"\nwrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
