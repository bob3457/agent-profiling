#!/usr/bin/env bash
# run_latency_arm.sh <A|B|C> — run the 9-task eval set under one optimization arm.
#
#   A: baseline       (your existing perf wrapper only)
#   B: persistent     (shell daemon via wrapper; fresh daemon per task)
#   C: +speculation   (B + speculative worker + result cache; swebench/tbench only)
#
# Run on the GH200 node inside an allocation. Whole-run perf stat wraps every
# codex exec (same events as your existing runners) so task-clock/utilization
# stay comparable across arms.
#
# Assumed layout (override via env):
#   ROOT              agent-profiling root
#   OPT               $ROOT/latency-opt (the untarred optimization scripts)
#   EVAL_SET          $OPT/eval_set.txt          (from gen_eval_set.sh)
#   WS_ROOT           swebench workspaces        (from materialize_swebench_ws.sh)
#   TB_TASKS_DIR      materialized terminal-bench task dirs
#   HOTPOT_MANIFEST   hotpotqa manifest tsv (qid<TAB>question ...)
#   CODEX_BIN         codex binary (defaults to your patched build if set upstream)
#   PERF_EVENTS       same string your existing runners use
set -uo pipefail

ARM=${1:?usage: run_latency_arm.sh A|B|C}
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
OPT=${OPT:-$ROOT/latency-opt}
EVAL_SET=${EVAL_SET:-$OPT/eval_set.txt}
WS_ROOT=${WS_ROOT:-/scratch/czhai/latency-eval/workspaces}
TB_TASKS_DIR=${TB_TASKS_DIR:-$ROOT/runs/terminalbench}
HOTPOT_MANIFEST=${HOTPOT_MANIFEST:-$(ls $ROOT/manifests/hotpotqa*.tsv 2>/dev/null | head -1)}
CODEX_BIN=${CODEX_BIN:-${CODEX_SRC_BIN:-codex}}
# gate + predictor LLM calls use the same binary as the agent by default
export SPEC_LLM_BIN=${SPEC_LLM_BIN:-$CODEX_BIN}
PERF_EVENTS=${PERF_EVENTS:-task-clock,context-switches,page-faults}
RESULTS=${RESULTS:-/scratch/czhai/latency-eval/results/arm_$ARM.$(date +%Y%m%d_%H%M%S)}

if [[ "${DEEPDIVE:-0}" == "1" ]]; then
  if [[ $ARM == A ]]; then
    export CODEX_TOOL_PERF_WRAPPER=$ROOT/scripts/codex_tool_perf_wrap.sh
  else
    export SESSIOND_PERF_EVENTS="$PERF_EVENTS"
  fi
  echo "DEEPDIVE=1: per-command perf active for arm $ARM"
fi

command -v perf >/dev/null || { echo "no perf on this node"; exit 1; }
[[ -f "$EVAL_SET" ]] || { echo "missing $EVAL_SET (run gen_eval_set.sh)"; exit 1; }
mkdir -p "$RESULTS"
cp "$EVAL_SET" "$RESULTS/eval_set.txt"
echo "arm=$ARM codex=$CODEX_BIN results=$RESULTS"

shelld_shutdown() {  # $1 = socket
  python3 - "$1" <<'PY' 2>/dev/null || true
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3)
s.connect(sys.argv[1]); s.sendall(b'{"op":"stats"}\n')
print("daemon stats:", s.recv(4096).decode().strip())
s2 = socket.socket(socket.AF_UNIX); s2.connect(sys.argv[1])
s2.sendall(b'{"op":"shutdown"}\n')
PY
}

run_one() {  # $1 bench, $2 task_id
  local bench=$1 tid=$2
  local run_dir="$RESULTS/$bench/$tid"
  mkdir -p "$run_dir"

  # ---- task prompt + workdir per benchmark ----------------------------------
  local prompt workdir
  case $bench in
    hotpotqa)
      # manifest columns: qid <TAB> base_task_path <TAB> prompt_file
      local pf
      pf=$(awk -F'\t' -v q="$tid" '$1==q {print $3; exit}' "$HOTPOT_MANIFEST")
      [[ -z "$pf" ]] && { echo "  SKIP: qid $tid not in manifest"; return; }
      [[ -f "$ROOT/$pf" ]] || { echo "  SKIP: prompt file missing: $ROOT/$pf"; return; }
      prompt=$(cat "$ROOT/$pf")
      workdir=$run_dir/work; mkdir -p "$workdir"
      ;;
    swebench)
      workdir=$WS_ROOT/$tid
      [[ -d "$workdir" ]] || { echo "  SKIP: workspace missing for $tid"; return; }
      git -C "$workdir" reset --hard -q 2>/dev/null; git -C "$workdir" clean -fdq 2>/dev/null
      local pfile=$ROOT/prompts/swe_$tid.txt
      [[ -f "$pfile" ]] || { echo "  SKIP: prompt missing at $pfile"; return; }
      prompt=$(cat "$pfile")
      ;;
    terminalbench)
      workdir=""
      for cand in "$TB_TASKS_DIR/$tid/base_task" "$TB_TASKS_DIR/$tid" \
                  "$ROOT/runs/terminalbench-arm/$tid/base_task"; do
        [[ -d "$cand" ]] && workdir=$cand && break
      done
      [[ -z "$workdir" ]] && { echo "  SKIP: task dir missing for $tid"; return; }
      TB_PRISTINE=${TB_PRISTINE:-/scratch/czhai/latency-eval/tb_pristine}
      if [[ -d "$TB_PRISTINE/$tid/base_task" ]]; then
        rsync -a --delete "$TB_PRISTINE/$tid/base_task/" "$workdir/"
      fi
      local pfile=""
      for cand in "$TB_TASKS_DIR/$tid/prompt.txt" "$TB_TASKS_DIR/$tid/task.txt" \
                  "$workdir/prompt.txt" "$workdir/task.txt" "$workdir/instruction.txt" \
                  "$ROOT/prompts/tb_$tid.txt"; do
        [[ -f "$cand" ]] && pfile=$cand && break
      done
      [[ -z "$pfile" ]] && { echo "  SKIP: no prompt file for $tid (looked for prompt.txt/task.txt)"; return; }
      prompt=$(cat "$pfile")
      ;;
  esac

  # canonicalize: cache keys are sha256(cwd+cmd); a symlinked logical cwd
  # on the agent side vs a resolved path on the speculation side misses
  # every lookup by construction (observed on runs/terminalbench-arm).
  [[ -n "$workdir" ]] && workdir=$(realpath "$workdir")

  # ---- arm-specific env ------------------------------------------------------
  local sock="/tmp/czhai_shelld/$ARM.$bench.$tid/sock" spec_pid=""
  (
    if [[ $ARM == B || $ARM == C ]]; then
      export CODEX_TOOL_PERF_WRAPPER=$OPT/scripts/codex_persistent_shell_wrap.sh
      export CODEX_SHELLD_SOCK=$sock
      export CODEX_SHELLD_LOGDIR=$run_dir/shelld_logs
    fi
    if [[ $ARM == C && ( $bench == swebench || $bench == terminalbench ) ]]; then
      export CODEX_SHELLD_SPEC=$run_dir/spec_cache
      mkdir -p "$CODEX_SHELLD_SPEC"
      printf '%s' "$prompt" > "$run_dir/problem.txt"
      # T=0 ungated worker: recon everywhere; on TB also the LLM predictor in
      # DIRECT-ONLY mode (tier0 reads; family/heavy predictions stay gated).
      # Two measured lost races showed the gate call is high-variance and
      # short-task speculation cannot sit behind it.
      EARLY_XTRA=""
      [[ $bench == terminalbench ]] && \
        EARLY_XTRA="--predictor llm --problem-statement $run_dir/problem.txt --timeout-per-cmd 20"
      SPEC_DIRECT_ONLY=1 nohup python3 -u "$OPT/speculation/speculative_worker.py" \
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \
        --actions workspace_recon $EARLY_XTRA --nice 10 \
        > "$run_dir/spec_early.log" 2>&1 &
      echo $! > "$run_dir/spec_early.pid"
      PS_ARG="--problem-statement $run_dir/problem.txt"
      [[ $bench == swebench && -f $ROOT/prompts/swe_$tid.txt ]] && PS_ARG="--problem-statement $ROOT/prompts/swe_$tid.txt"
      GATE_XTRA="--statement-only"
      [[ "${SPEC_GATE_STREAM:-0}" == "1" ]] && GATE_XTRA=""
      nohup python3 -u "$OPT/speculation/llm_gate.py" \
        --problem-statement "$run_dir/problem.txt" \
        --agent-stream "$run_dir/stdout.jsonl" \
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \
        --gate-json "$run_dir/gate.json" --timeout 90 $GATE_XTRA \
        -- python3 "$OPT/speculation/speculative_worker.py" \
          --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \
          --predictor $([[ $bench == terminalbench ]] && echo heuristic || echo both) \
          --ledger-dir "$ROOT/latency-opt/ledger" \
          $PS_ARG > "$run_dir/spec.log" 2>&1 &
      echo $! > "$run_dir/spec.pid"
      nohup python3 -u "$OPT/speculation/edit_respec.py" \
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" \
        --spec-log "$run_dir/spec.log" \
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \
        --agent-stream "$run_dir/stdout.jsonl" \
        > "$run_dir/respec.log" 2>&1 &
      echo $! > "$run_dir/respec.pid"
    fi

    # ---- the run, whole-run perf + wall time --------------------------------
    cd "$workdir"
    if [[ "${DEEPDIVE:-0}" == "1" && $ARM == A ]]; then export CODEX_TOOL_PERF_DIR="$run_dir/tool_perf"; mkdir -p "$CODEX_TOOL_PERF_DIR"; fi
    /usr/bin/time -v -o "$run_dir/time.txt" \
      perf stat -e "$PERF_EVENTS" -o "$run_dir/perf_stat.txt" -- \
      "$CODEX_BIN" exec --json --skip-git-repo-check --sandbox danger-full-access "$prompt" \
        > "$run_dir/stdout.jsonl" 2> "$run_dir/stderr.log"
    echo $? > "$run_dir/exit_code"
  )

  # ---- teardown --------------------------------------------------------------
  [[ -f "$run_dir/spec_early.pid" ]] && kill "$(cat "$run_dir/spec_early.pid")" 2>/dev/null
  [[ -f "$run_dir/spec.pid" ]] && kill "$(cat "$run_dir/spec.pid")" 2>/dev/null
  [[ -f "$run_dir/respec.pid" ]] && kill "$(cat "$run_dir/respec.pid")" 2>/dev/null
  [[ $ARM == B || $ARM == C ]] && shelld_shutdown "$sock" >> "$run_dir/daemon_stats.txt"
  [[ $bench == swebench ]] && git -C "$workdir" diff > "$run_dir/pred.patch" 2>/dev/null

  local wall=$(grep -m1 "Elapsed" "$run_dir/time.txt" 2>/dev/null | awk '{print $NF}')
  echo "  done $bench/$tid  wall=$wall  exit=$(cat "$run_dir/exit_code" 2>/dev/null)"
}

# mapfile pattern: never while-read over the task list around codex runs
mapfile -t TASKS < "$EVAL_SET"
for line in "${TASKS[@]}"; do
  bench=${line%%$'\t'*}; tid=${line#*$'\t'}
  echo "== [$ARM] $bench :: $tid"
  run_one "$bench" "$tid" < /dev/null
done

# ---- arm summary -------------------------------------------------------------
python3 - "$RESULTS" <<'PY'
import json, re, sys
from pathlib import Path
root = Path(sys.argv[1]); rows = []
for tdir in sorted(root.glob("*/*/")):
    r = {"task": f"{tdir.parent.name}/{tdir.name}"}
    t = tdir / "time.txt"
    if t.exists():
        m = re.search(r"Elapsed.*: (.*)", t.read_text())
        r["wall"] = m.group(1) if m else "?"
    p = tdir / "perf_stat.txt"
    if p.exists():
        m = re.search(r"([\d,.]+)\s+msec\s+task-clock", p.read_text())
        r["task_clock_ms"] = m.group(1) if m else "?"
    j = tdir / "shelld_logs" / "commands.jsonl"
    if j.exists():
        cmds = [json.loads(l) for l in j.open()]
        r["n_cmds"] = len(cmds)
        r["reused"] = sum(c["session_reused"] for c in cmds)
        r["cache_hits"] = sum(c["cached"] for c in cmds)
        r["tool_wall_s"] = round(sum(c.get("wall_s") or 0 for c in cmds), 2)
    rows.append(r)
out = root / "summary.json"; out.write_text(json.dumps(rows, indent=2))
for r in rows: print(r)
print(f"\nsummary -> {out}")
PY
