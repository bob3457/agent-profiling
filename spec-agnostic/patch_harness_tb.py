#!/usr/bin/env python3
"""patch_harness_tb.py — fix TB task resolution in run_latency_arm.sh.
Idempotent; verbatim anchors; refuses on drift.

Bug: materialize_tb_baremetal.py writes workspaces to
runs/terminalbench-arm/<task>/base_task and prompts to prompts/tb_<task>.txt,
but run_one's terminalbench branch only checks runs/terminalbench (default
TB_TASKS_DIR) and prompt.txt/task.txt inside the task dir — so every
materialized TB task hits "SKIP: no prompt file".

Fix: add the materializer's workspace dir and prompt path as candidates.

Run:  python3 patch_harness_tb.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
TARGET = ROOT / "latency-opt/harness/run_latency_arm.sh"
src = TARGET.read_text()
orig = src


def apply(name, old, new):
    global src
    if new in src:
        print(f"  = {name}: already applied")
        return
    assert old in src, f"ANCHOR DRIFT ({name}): expected bytes not found"
    assert src.count(old) == 1, f"ANCHOR AMBIGUOUS ({name})"
    src = src.replace(old, new)
    print(f"  + {name}")


apply("workdir candidates",
      '      for cand in "$TB_TASKS_DIR/$tid/base_task" "$TB_TASKS_DIR/$tid"; do',
      '      for cand in "$TB_TASKS_DIR/$tid/base_task" "$TB_TASKS_DIR/$tid" \\\n'
      '                  "$ROOT/runs/terminalbench-arm/$tid/base_task"; do')

apply("prompt candidates",
      '      for cand in "$TB_TASKS_DIR/$tid/prompt.txt" "$TB_TASKS_DIR/$tid/task.txt" \\\n'
      '                  "$workdir/prompt.txt" "$workdir/task.txt" "$workdir/instruction.txt"; do',
      '      for cand in "$TB_TASKS_DIR/$tid/prompt.txt" "$TB_TASKS_DIR/$tid/task.txt" \\\n'
      '                  "$workdir/prompt.txt" "$workdir/task.txt" "$workdir/instruction.txt" \\\n'
      '                  "$ROOT/prompts/tb_$tid.txt"; do')

if src != orig:
    TARGET.write_text(src)
    print(f"wrote {TARGET}")
else:
    print("no changes (all edits already present)")
