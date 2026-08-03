#!/usr/bin/env python3
"""patch_harness_realpath.py — two fixes in run_latency_arm.sh from the first
live TB arm-C smoke. Idempotent; verbatim anchors; refuses on drift.
Requires patch_harness_tb.py to be applied first (anchors against it).

1. CWD CANONICALIZATION. runs/terminalbench-arm is a symlink; the agent's
   daemon-side cwd is bash's logical $PWD (symlinked) while the speculation
   stack keys the cache under the resolved path -> every lookup misses by
   construction (observed: a byte-identical watcher-cached `sed -n` query
   missed). Fix: resolve workdir once, right before anything uses it, for
   ALL benchmarks. Also makes `pwd` output consistent between the worker's
   pre-run and the agent's live session.

2. SPEC_LLM_BIN DEFAULT. llm_gate.py and llm_predictor.py shell out to
   $SPEC_LLM_BIN (default bare `codex`, not on PATH here) -> gate failed
   open ("llm answer unparseable"), predictor returned []. Default it to
   $CODEX_BIN so the gate and predictor use the same binary as the agent.

Run:  python3 patch_harness_realpath.py [repo_root]
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


apply("realpath workdir",
      '  # ---- arm-specific env ------------------------------------------------------\n'
      '  local sock="/tmp/czhai_shelld/$ARM.$bench.$tid/sock" spec_pid=""',
      '  # canonicalize: cache keys are sha256(cwd+cmd); a symlinked logical cwd\n'
      '  # on the agent side vs a resolved path on the speculation side misses\n'
      '  # every lookup by construction (observed on runs/terminalbench-arm).\n'
      '  [[ -n "$workdir" ]] && workdir=$(realpath "$workdir")\n\n'
      '  # ---- arm-specific env ------------------------------------------------------\n'
      '  local sock="/tmp/czhai_shelld/$ARM.$bench.$tid/sock" spec_pid=""')

apply("SPEC_LLM_BIN default",
      'CODEX_BIN=${CODEX_BIN:-${CODEX_SRC_BIN:-codex}}',
      'CODEX_BIN=${CODEX_BIN:-${CODEX_SRC_BIN:-codex}}\n'
      '# gate + predictor LLM calls use the same binary as the agent by default\n'
      'export SPEC_LLM_BIN=${SPEC_LLM_BIN:-$CODEX_BIN}')

if src != orig:
    TARGET.write_text(src)
    print(f"wrote {TARGET}")
else:
    print("no changes (all edits already present)")
