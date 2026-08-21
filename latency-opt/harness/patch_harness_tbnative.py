#!/usr/bin/env python3
"""patch_harness_tbnative.py -- teach run_latency_arm.sh about tb-native tasks.

Two additions, both no-ops for non-tb-native runs:
  1. If $TB_TASKS_DIR/<tid>/venv/bin exists, prepend it to PATH inside the
     per-run subshell (agent, sessiond, and spec workers all inherit it, and
     it does not leak across tasks).
  2. After the run, if $TB_TASKS_DIR/<tid>/grade.sh exists, run the native
     verifier from the workdir, capture grade.log and reward.txt.

Idempotent; verbatim anchors; fails loudly on drift.

Usage: python3 patch_harness_tbnative.py [path-to-run_latency_arm.sh]
"""
import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
    "/projects/kzhou6/czhai/agent-profiling/latency-opt/harness/run_latency_arm.sh")
src = TARGET.read_text()

if "tb-native venv" in src:
    print(f"already patched: {TARGET}")
    sys.exit(0)

# ---- 1. venv on PATH inside the run subshell --------------------------------
ANCHOR_VENV = '''  (
    if [[ $ARM == B || $ARM == C ]]; then
      export CODEX_TOOL_PERF_WRAPPER=$OPT/scripts/codex_persistent_shell_wrap.sh'''
assert src.count(ANCHOR_VENV) == 1, "venv anchor not found (subshell head drifted)"
src = src.replace(ANCHOR_VENV, '''  (
    # tb-native venv: task-local deps (agent + daemon + spec workers inherit;
    # subshell scope means no leak across tasks)
    if [[ $bench == terminalbench && -d "$TB_TASKS_DIR/$tid/venv/bin" ]]; then
      export PATH="$TB_TASKS_DIR/$tid/venv/bin:$PATH"
    fi
    if [[ $ARM == B || $ARM == C ]]; then
      export CODEX_TOOL_PERF_WRAPPER=$OPT/scripts/codex_persistent_shell_wrap.sh''')

# ---- 2. native grading hook in teardown --------------------------------------
ANCHOR_GRADE = '''  [[ $bench == swebench ]] && git -C "$workdir" diff > "$run_dir/pred.patch" 2>/dev/null'''
assert src.count(ANCHOR_GRADE) == 1, "grade anchor not found (teardown drifted)"
src = src.replace(ANCHOR_GRADE, ANCHOR_GRADE + '''
  if [[ $bench == terminalbench && -f "$TB_TASKS_DIR/$tid/grade.sh" ]]; then
    ( cd "$workdir" && bash "$TB_TASKS_DIR/$tid/grade.sh" ) > "$run_dir/grade.log" 2>&1
    grep -o 'reward=[01]' "$run_dir/grade.log" | tail -1 | cut -d= -f2 > "$run_dir/reward.txt"
    echo "  reward=$(cat "$run_dir/reward.txt" 2>/dev/null || echo none)"
  fi''')

TARGET.write_text(src)
print(f"patched: {TARGET} (tb-native venv PATH + native grading hook)")
