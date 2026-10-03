# kalshi-btc-1h-agent

Read-only cushion caller for Kalshi's **1-hour BTC** markets (`KXBTCD`, the "BTC above $X at the top of the hour" ladder). The 1-hour
sibling of [`kalshi-btc-agent`](../kalshi-btc-agent) (15-minute up/down). **BTC only**: gold and oil stay out of the 1-hour until they have
enough data for legitimate results. It never places an order.

```
agent.py              card for the current hour (no alert)
agent.py --run        scheduled (every minute): log, settle calls, alert once per hour (alerts OFF by default)
agent.py --scorecard  live record, with a per-minute table
data.py               build the hourly dataset (1,531 windows, 69 days; windows-1h.jsonl is committed)
study.py [--fast]     accuracy by minute, cushion vs Chronos vs AutoGluon, volatility analysis, gate calibration -> gate.json
recal.sh              weekly (Sun 06:40): data + fast study + scorecard, commit and push
```

## The call

Each hourly event is a ladder of ~190 strikes settled on the 60-second average of CF Benchmarks' BRTI. The agent's "call" is the strike
**nearest the BRTI proxy at the hour's open** ("finish the hour above this level?"), the closest analogue of the 15-minute up/down market.
Model: `P = Φ(gap / sqrt(σ₁ₘ²·(minutes_left − ⅔) + σ_proxy²))`, the same as the 15-minute agent, with the Coinbase + Bitstamp proxy.

## Results (1,531 hours, 2026-07-27 → 2026-10-03; see `results/study-2026-10-03.md`)

* The market favourite is right **58% at minute 1, 64% at minute 5, 68% at 15, 75% at 30, 84% at 45 and ~88% at minutes 54-55**.
* Buying the favourite at the ask has **negative expected value at every minute** (best: minute 54, -0.1¢; worst: minutes 10 and 25, -3.5¢).
* The cushion model, Chronos-Bolt, Chronos-2 and AutoGluon never beat the market price on 5,292 held-out observations; the cushion model and
  Chronos-2 tie it at most minutes, the rest are significantly worse at many minutes.
* Higher trailing volatility goes with a less accurate call (-6 pts calm → volatile), but that is entirely the cushion: beyond cushion there is no effect.
* Gate: confidence ≥ 0.85 (pre-registered rule) hit 94.0% on 654 held-out calls, +0.7¢ per contract after fees: small and unproven.

Not pre-registered; leads, not findings. Live calls are logged to `data/evals.jsonl` and need 10+ separate days before they mean anything.

## Layout
```
gate.json                  calibrated gate + hit-rate table (rewritten weekly)
config.json                alerts (off), optional min_conf_override
data/windows-1h.jsonl      every settled hourly window (strike, outcome, BRTI settlement); Kalshi will not keep it forever
data/evals.jsonl           live evaluations + settled calls (hourly autocommit)
results/                   study reports, accuracy-curve chart, scorecard snapshots
```
