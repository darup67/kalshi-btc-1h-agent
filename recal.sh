#!/bin/bash
# Weekly: top up the hourly dataset, re-run the fast study (rewrites gate.json by the same pre-registered rule),
# snapshot the scorecard, commit and push. The full Chronos/AutoGluon run is heavy and stays manual:
#   ~/.venvs/market-ml/bin/python study.py
set -euo pipefail
cd "$(dirname "$0")"
export DYLD_FALLBACK_LIBRARY_PATH="$HOME/.venvs/market-ml/lib/python3.11/site-packages/torch/lib"
PY="$HOME/.venvs/market-ml/bin/python"
$PY data.py > /dev/null
$PY study.py --fast > /dev/null
$PY agent.py --scorecard > results/scorecard.txt
GIT=/usr/local/bin/git
$GIT add gate.json results data/windows-1h.jsonl data/events-settled.json data/evals.jsonl
$GIT diff --cached --quiet || $GIT commit -q -m "Weekly recalibration $(date +%F)"
$GIT push -q origin HEAD 2>&1 || echo "push failed" >&2
