#!/usr/bin/env bash
# run_spec_set.sh — spec-arm sweep for the window/hit study.
# Usage:  bash run_spec_set.sh <instance_id> [instance_id ...]
#         bash run_spec_set.sh $(cat my_swebench_ids.txt)
# Sequential on purpose: one agent + one worker + one watcher per node at a
# time keeps the perf numbers clean. Failures don't stop the sweep.
set -uo pipefail
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
HARNESS=$ROOT/latency-opt/harness/run_option_b.sh
OUT=${RESULTS_BASE:-/scratch/czhai/latency-eval/optionb}

[ $# -ge 1 ] || { echo "usage: $0 <instance_id> [...]"; exit 1; }
echo "sweep: $# instance(s)"
for IID in "$@"; do
  echo "=============================== $IID ==============================="
  bash "$HARNESS" spec "$IID" || echo "WARN: harness exited nonzero for $IID"
  R=$(ls -dt "$OUT"/spec."$IID".* 2>/dev/null | head -1)
  if [ -z "$R" ]; then echo "WARN: no results dir for $IID"; continue; fi
  D="$R/cache/serve_decisions.jsonl"
  if [ -f "$D" ]; then
    python3 - "$D" <<'PY'
import json, sys, collections
c = collections.Counter()
for l in open(sys.argv[1]):
    c[json.loads(l)["decision"].split("(")[0]] += 1
print("  decisions:", dict(c) or "none")
PY
  else
    echo "  decisions: (no serve_decisions.jsonl)"
  fi
  grep -c 'edit detected' "$R/logs/respec.log" 2>/dev/null \
    | xargs -I{} echo "  generations: {}"
done
echo "sweep done. Analyze with: python3 spec_window_report.py $OUT"
