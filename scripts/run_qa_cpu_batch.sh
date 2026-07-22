#!/usr/bin/env bash
# Generalized QA profiling batch (hotpotqa, freshqa, ...) with resume.
# Supersedes run_hotpot_cpu_batch.sh (which hardcoded BENCHMARK=hotpotqa).
# Usage:
#   bash scripts/run_qa_cpu_batch.sh hotpotqa manifests/hotpotqa_cpu_study_10.tsv [ITER]
#   bash scripts/run_qa_cpu_batch.sh freshqa  manifests/freshqa_cpu_study_10.tsv  [ITER]
set -euo pipefail
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "Usage: $0 BENCHMARK MANIFEST_TSV [ITERATION]" >&2
  exit 2
fi
BENCHMARK="$1"; MANIFEST="$2"; ITER="${3:-1}"
ROOT="${ROOT:-/projects/kzhou6/czhai/agent-profiling}"
RUNNER="$ROOT/scripts/profile_codex_hotpot_one.sh"   # benchmark-agnostic despite the name

[ -f "$MANIFEST" ] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }
[ -f "$RUNNER" ] || { echo "ERROR: runner not found: $RUNNER" >&2; exit 1; }
: "${OPENAI_API_KEY:?OPENAI_API_KEY not set}"

mapfile -t ROWS < "$MANIFEST"
echo "=== $BENCHMARK CPU study batch: ${#ROWS[@]} rows, iter=$ITER ==="
for ROW in "${ROWS[@]}"; do
  [ -z "${ROW// }" ] && continue
  [[ "$ROW" =~ ^# ]] && continue
  IFS=$'\t' read -r QID TEMPLATE PROMPT <<< "$ROW"
  if [ -z "${QID:-}" ] || [ -z "${TEMPLATE:-}" ] || [ -z "${PROMPT:-}" ]; then
    echo "WARNING: bad manifest row: $ROW" >&2
    continue
  fi
  OUT="$ROOT/results_cpu_deepdive/$BENCHMARK/$QID/iter_$ITER"
  if [ -f "$OUT/metadata.json" ]; then
    echo "[skip] $QID iter_$ITER"
    continue
  fi
  echo "[run] $QID iter_$ITER"
  bash "$RUNNER" "$BENCHMARK" "$QID" "$ITER" "$ROOT/$TEMPLATE" "$ROOT/$PROMPT" < /dev/null \
    || echo "[warn] failed: $QID iter_$ITER" >&2
done
echo "Batch complete."
