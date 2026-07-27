#!/usr/bin/env bash
# gen_eval_set.sh — build eval_set.txt for the latency study.
# 9 tasks: 3 hotpotqa (first 3 in manifest), 3 swebench (2 astropy + 1 django,
# fixed), 3 terminal-bench (first alphabetical easy/medium/hard).
#
# Adjust these three paths if they differ on your side:
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
TB_TASKS_DIR=${TB_TASKS_DIR:-$ROOT/runs/terminalbench}     # materialized task dirs
HOTPOT_MANIFEST=${HOTPOT_MANIFEST:-$(ls $ROOT/manifests/hotpotqa*.tsv 2>/dev/null | head -1)}
OUT=${OUT:-$ROOT/latency-opt/eval_set.txt}
set -u

: > "$OUT"

# --- swebench (fixed selection) --------------------------------------------
for id in astropy__astropy-12907 astropy__astropy-13236 django__django-10973; do
  echo -e "swebench\t$id" >> "$OUT"
done

# --- hotpotqa: first 3 qids from the manifest -------------------------------
if [[ -f "$HOTPOT_MANIFEST" ]]; then
  # assumes qid is the first TSV column; adjust cut field if not
  head -3 "$HOTPOT_MANIFEST" | cut -f1 | while read -r qid; do
    echo -e "hotpotqa\t$qid" >> "$OUT"
  done
else
  echo "WARN: hotpot manifest not found at $HOTPOT_MANIFEST" >&2
fi

# --- terminal-bench: first alphabetical per difficulty ----------------------
# difficulty lives in each task's task.yaml (key: difficulty). The materialized
# dirs may or may not carry task.yaml; fall back to the source tasks dir via
# TB_SOURCE_DIR if grep finds nothing.
declare -A picked
for d in $(ls -d "$TB_TASKS_DIR"/*/ 2>/dev/null | sort); do
  name=$(basename "$d")
  yaml=""
  for cand in "$d/task.yaml" "$d/base_task/task.yaml"; do
    [[ -f "$cand" ]] && yaml=$cand && break
  done
  [[ -z "$yaml" ]] && continue
  diff=$(grep -m1 -E '^difficulty:' "$yaml" | awk '{print tolower($2)}')
  [[ -z "$diff" ]] && continue
  if [[ -z "${picked[$diff]:-}" ]]; then
    picked[$diff]=$name
    echo -e "terminalbench\t$name" >> "$OUT"
  fi
done
for want in easy medium hard; do
  [[ -z "${picked[$want]:-}" ]] && echo "WARN: no $want terminal-bench task found under $TB_TASKS_DIR (no task.yaml with difficulty?) — set TB_TASKS_DIR or add manually" >&2
done

echo "=== $OUT ==="
cat "$OUT"
n=$(wc -l < "$OUT")
if [[ $n -ne 9 ]]; then
  echo "WARN: expected 9 tasks, got $n — fix before running arms" >&2
fi
exit 0
