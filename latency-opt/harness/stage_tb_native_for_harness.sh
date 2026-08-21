#!/bin/bash
# stage_tb_native_for_harness.sh -- convert tb_native_prep.py output into the
# layout run_latency_arm.sh expects for terminalbench tasks:
#
#   $TB_RUNS/<task>/base_task/    agent workdir (copy of prepped work/)
#   $TB_RUNS/<task>/prompt.txt    = instruction.md
#   $TB_RUNS/<task>/venv          symlink to the prepped venv (harness patch
#                                 prepends its bin/ to PATH per run)
#   $TB_RUNS/<task>/grade.sh      symlink (harness patch runs it post-agent)
#   $TB_PRISTINE/<task>/base_task pristine copy (harness rsyncs it over the
#                                 workdir before every run -> clean resets)
#
# Also appends/refreshes an eval set at $EVAL_OUT with terminalbench lines.
#
# Usage: bash stage_tb_native_for_harness.sh [task ...]
#        (default: the five verified tasks)
set -uo pipefail

TB_NATIVE="${TB_NATIVE:-/projects/kzhou6/czhai/tb-native}"
TB_RUNS="${TB_RUNS:-/projects/kzhou6/czhai/agent-profiling/runs/terminalbench}"
TB_PRISTINE="${TB_PRISTINE:-/scratch/czhai/latency-eval/tb_pristine}"
EVAL_OUT="${EVAL_OUT:-/projects/kzhou6/czhai/agent-profiling/latency-opt/eval_sets/eval_set_tbnative.txt}"

TASKS=("$@")
[ ${#TASKS[@]} -gt 0 ] || TASKS=(llm-inference-batching-scheduler protein-assembly \
                                 train-fasttext gpt2-codegolf path-tracing)

mkdir -p "$TB_RUNS" "$TB_PRISTINE" "$(dirname "$EVAL_OUT")"
: > "$EVAL_OUT"

for t in "${TASKS[@]}"; do
  P="$TB_NATIVE/$t"
  [ -f "$P/manifest.json" ] || { echo "[$t] SKIP: not prepped at $P"; continue; }
  D="$TB_RUNS/$t"
  mkdir -p "$D"
  echo "[$t] staging base_task + pristine..."
  rsync -a --delete "$P/work/" "$D/base_task/"
  mkdir -p "$TB_PRISTINE/$t"
  rsync -a --delete "$P/work/" "$TB_PRISTINE/$t/base_task/"
  cp "$P/instruction.md" "$D/prompt.txt"
  ln -sfn "$P/venv" "$D/venv"
  ln -sfn "$P/grade.sh" "$D/grade.sh"
  printf 'terminalbench\t%s\n' "$t" >> "$EVAL_OUT"
done

echo
echo "staged into: $TB_RUNS"
echo "pristine:    $TB_PRISTINE"
echo "eval set:    $EVAL_OUT"
cat "$EVAL_OUT"
echo
echo "run the campaign (spec-active arm C, full per-command + spec-side perf):"
echo "  export PATH=/projects/kzhou6/czhai/node-v22-arm64/bin:\$PATH   # node for npm-codex tools if needed"
echo "  export CODEX_BIN=/projects/kzhou6/czhai/agent-profiling/agent-src/codex-rs/target-aarch64/release/codex"
echo "  export SPEC_LLM_MODE=qwen SPEC_PRED_TOPK=3        # EXPLICIT (unset-default trap)"
echo "  export DEEPDIVE=1 SPEC_PERF=1                     # per-command + spec-side perf"
echo "  EVAL_SET=$EVAL_OUT \\\\"
echo "  TB_TASKS_DIR=$TB_RUNS \\\\"
echo "  TB_PRISTINE=$TB_PRISTINE \\\\"
echo "    bash /projects/kzhou6/czhai/agent-profiling/latency-opt/harness/run_latency_arm.sh C"
