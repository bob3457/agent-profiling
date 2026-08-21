#!/bin/bash
# run_tb_native.sh -- orchestrate profiled, speculator-active runs of native
# Terminal-Bench tasks on the GH200. Emits the standard latency-eval layout:
#
#   $RESULTS_BASE/<ARM>.<stamp>/terminalbench/<task>/
#       work/                    per-run copy of the prepped workspace
#       shelld_logs/commands.jsonl   per-command wall/cpu/HW-perf/RSS/IO
#       shelld_logs/tool_perf/*.csv  per-command perf stat counters
#       agent.log                agent stdout/stderr
#       reward.txt               verifier reward (1/0)
#       grade.log                verifier output
#   $RESULTS_BASE/<ARM>.<stamp>/summary.json   [{task, wall, task_clock_ms}...]
#
# Prereqs: tb_native_prep.py has prepped tasks under $TB_NATIVE;
#          shell_sessiond.py patched with patch_sessiond_prof.py;
#          speculator components in place (spec cache, gate, worker).
#
# Usage:
#   bash run_tb_native.sh [task ...]        # default: the five verified tasks
#   ARM=arm_S SPEC=1 bash run_tb_native.sh regex-chess
#   SPEC=0 bash run_tb_native.sh            # baseline arm (no speculation)
set -uo pipefail

# ----------------------------------------------------------------- config
ARM="${ARM:-arm_S}"                       # S = spec-active native TB campaign
SPEC="${SPEC:-1}"                         # 1 = speculator on, 0 = baseline
TB_NATIVE="${TB_NATIVE:-/projects/kzhou6/czhai/tb-native}"
RESULTS_BASE="${RESULTS_BASE:-/scratch/czhai/latency-eval/results}"
SCRIPTS="${SCRIPTS:-/projects/kzhou6/czhai/agent-profiling/scripts}"
SESSIOND="${SESSIOND:-$SCRIPTS/shell_sessiond.py}"
PERF_EVENTS="${PERF_EVENTS:-task-clock,cycles,instructions,cache-references,cache-misses,branches,branch-misses,context-switches,page-faults}"
AGENT_TIMEOUT_DEFAULT=3600

# Speculator knobs -- match the arm_C conventions; override per-experiment.
export SPEC_PRED_TOPK="${SPEC_PRED_TOPK:-3}"
export SPEC_LLM_MODE="${SPEC_LLM_MODE:-qwen}"   # EXPLICIT (the unset-default trap)

# >>> SITE HOOKS -- the two lines that encode how YOUR harness wires codex <<<
# 1) How codex routes shell calls through the sessiond socket. Fill in to
#    match the July TB runs (env var, codex config, or client shim on PATH).
agent_shell_env() { # $1=socket path -- export whatever codex needs
  export SESSIOND_SOCKET="$1"
}
# 2) The agent invocation itself, run with cwd=$WORK. $INSTR = instruction.md.
agent_cmd() {
  codex exec --skip-git-repo-check "$(cat "$INSTR")"
}
# >>> END SITE HOOKS <<<

TASKS=("$@")
[ ${#TASKS[@]} -gt 0 ] || TASKS=(llm-inference-batching-scheduler protein-assembly \
                                 train-fasttext gpt2-codegolf path-tracing)

STAMP=$(date +%Y%m%d_%H%M%S)
RUN_DIR="$RESULTS_BASE/$ARM.$STAMP"
mkdir -p "$RUN_DIR"
echo "${TASKS[@]}" | tr ' ' '\n' > "$RUN_DIR/eval_set.txt"
SUMMARY="$RUN_DIR/summary.json"
echo "[" > "$SUMMARY.tmp"
first=1

echo "== run $ARM.$STAMP  spec=$SPEC topk=$SPEC_PRED_TOPK llm=$SPEC_LLM_MODE =="
echo "== tasks: ${TASKS[*]} =="

for task in "${TASKS[@]}"; do
  PREP="$TB_NATIVE/$task"
  [ -f "$PREP/manifest.json" ] || { echo "[$task] SKIP: not prepped"; continue; }
  TDIR="$RUN_DIR/terminalbench/$task"
  WORK="$TDIR/work"
  LOGS="$TDIR/shelld_logs"
  INSTR="$PREP/instruction.md"
  mkdir -p "$LOGS"

  echo "[$task] staging workspace..."
  rsync -a "$PREP/work/" "$WORK/"

  # task venv first on PATH for the agent's shell commands
  VENV="$PREP/venv"
  TIMEOUT=$(python3 -c "import json;m=json.load(open('$PREP/manifest.json'));print(int(m.get('agent_timeout_sec') or $AGENT_TIMEOUT_DEFAULT))")

  # --- sessiond: one daemon per task, full profiling on ---
  SOCK="$TDIR/shelld.sock"
  SPEC_CACHE=""
  if [ "$SPEC" = "1" ]; then
    SPEC_CACHE="$TDIR/spec_cache"
    mkdir -p "$SPEC_CACHE"
  fi
  SESSIOND_LOG_DIR="$LOGS" \
  SESSIOND_SPEC_CACHE="$SPEC_CACHE" \
  SESSIOND_PERF_EVENTS="$PERF_EVENTS" \
    python3 "$SESSIOND" --socket "$SOCK" \
      ${SPEC_CACHE:+--spec-cache "$SPEC_CACHE"} \
      --log-dir "$LOGS" --perf-events "$PERF_EVENTS" \
      > "$TDIR/sessiond.log" 2>&1 &
  SESSIOND_PID=$!
  sleep 1
  kill -0 $SESSIOND_PID 2>/dev/null || { echo "[$task] sessiond failed to start"; cat "$TDIR/sessiond.log"; continue; }

  # --- agent run, timed + perf-totaled (matches summary.json convention) ---
  agent_shell_env "$SOCK"
  export PATH="$VENV/bin:$PATH"
  export PROFWRAP_TASK="$task" PROFWRAP_ARM="$ARM" PROFWRAP_RUN="$STAMP"
  echo "[$task] agent running (timeout ${TIMEOUT}s)..."
  T0=$(date +%s.%N)
  ( cd "$WORK" && timeout "$TIMEOUT" \
      perf stat -e task-clock -x , -o "$TDIR/agent_perf.csv" \
      bash -c "$(declare -f agent_cmd); INSTR='$INSTR' agent_cmd" \
      > "$TDIR/agent.log" 2>&1 )
  AGENT_RC=$?
  WALL=$(awk -v a=$(date +%s.%N) -v b=$T0 'BEGIN{printf "%.2f", a-b}')
  echo "[$task] agent rc=$AGENT_RC wall=${WALL}s"

  # --- stop daemon, grade ---
  kill $SESSIOND_PID 2>/dev/null; wait $SESSIOND_PID 2>/dev/null
  echo "[$task] grading..."
  ( cd "$WORK" && bash "$PREP/grade.sh" ) > "$TDIR/grade.log" 2>&1
  REWARD=$(grep -o 'reward=[01]' "$TDIR/grade.log" | tail -1 | cut -d= -f2)
  echo "${REWARD:-0}" > "$TDIR/reward.txt"
  echo "[$task] reward=${REWARD:-0}"

  # --- summary row (wall as m:ss.xx, task_clock from agent-level perf) ---
  CLOCK=$(awk -F, '/task-clock/{printf "%.2f",$1; exit}' "$TDIR/agent_perf.csv" 2>/dev/null)
  MIN=$(awk -v w=$WALL 'BEGIN{printf "%d", w/60}')
  SEC=$(awk -v w=$WALL 'BEGIN{printf "%05.2f", w%60}')
  [ $first -eq 1 ] || echo "," >> "$SUMMARY.tmp"
  first=0
  printf '  {"task": "terminalbench/%s", "wall": "%s:%s", "task_clock_ms": "%s", "reward": %s, "agent_rc": %s}' \
    "$task" "$MIN" "$SEC" "${CLOCK:-}" "${REWARD:-0}" "$AGENT_RC" >> "$SUMMARY.tmp"
done

echo "" >> "$SUMMARY.tmp"; echo "]" >> "$SUMMARY.tmp"
mv "$SUMMARY.tmp" "$SUMMARY"
echo
echo "== done: $RUN_DIR =="
python3 -m json.tool "$SUMMARY"
