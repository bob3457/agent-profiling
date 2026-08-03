#!/usr/bin/env python3
"""patch_harness_early_recon.py — close the start-of-task race observed on
the TB smoke (agent's 4th command beat the gated worker's cache-put by ~1s;
gate 14.7s + predictor 5.75s consumed the entire useful window of a ~50s
task). Idempotent; verbatim anchors; refuses on drift.
Requires patch_harness_tb.py + patch_harness_realpath.py applied first.

1. EARLY RECON (T=0, ungated). Launch a recon-only worker the moment the
   run starts: --actions workspace_recon bypasses the gate by design, is
   read-only, costs ~0.2s CPU, and is safe on any local workspace — there
   is nothing for the gate to authorize. Also pre-creates the spec dir so
   the daemon never races its existence. The gated worker keeps the
   expensive tier (LLM predictor, targeted pre-runs); recon re-running
   there is an idempotent refresh.

2. STATEMENT-ONLY GATE BY DEFAULT. The stream signal costs ~10s of waiting
   for the agent's first message before the ~5-7s LLM call — on short
   tasks that is the whole speculation window. Default every benchmark to
   --statement-only; export SPEC_GATE_STREAM=1 to opt back into the stream
   signal (gate.json records which signal ran either way, so gate-accuracy
   comparisons across both configs stay measurable).

Run:  python3 patch_harness_early_recon.py [repo_root]
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


apply("early recon worker",
      '''    if [[ $ARM == C && ( $bench == swebench || $bench == terminalbench ) ]]; then
      export CODEX_SHELLD_SPEC=$run_dir/spec_cache
      printf '%s' "$prompt" > "$run_dir/problem.txt"''',
      '''    if [[ $ARM == C && ( $bench == swebench || $bench == terminalbench ) ]]; then
      export CODEX_SHELLD_SPEC=$run_dir/spec_cache
      mkdir -p "$CODEX_SHELLD_SPEC"
      # T=0 ungated recon: read-only, ~0.2s CPU, nothing to gate; wins the
      # first-20-seconds window that the gate+predictor serialization loses.
      nohup python3 -u "$OPT/speculation/speculative_worker.py" \\
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\
        --actions workspace_recon --nice 10 \\
        > "$run_dir/spec_early.log" 2>&1 &
      echo $! > "$run_dir/spec_early.pid"
      printf '%s' "$prompt" > "$run_dir/problem.txt"''')

apply("statement-only default",
      '''      GATE_XTRA=""
      [[ $bench == swebench ]] && GATE_XTRA="--statement-only"''',
      '''      GATE_XTRA="--statement-only"
      [[ "${SPEC_GATE_STREAM:-0}" == "1" ]] && GATE_XTRA=""''')

apply("teardown early worker",
      '  [[ -f "$run_dir/spec.pid" ]] && kill "$(cat "$run_dir/spec.pid")" 2>/dev/null',
      '  [[ -f "$run_dir/spec_early.pid" ]] && kill "$(cat "$run_dir/spec_early.pid")" 2>/dev/null\n'
      '  [[ -f "$run_dir/spec.pid" ]] && kill "$(cat "$run_dir/spec.pid")" 2>/dev/null')

if src != orig:
    TARGET.write_text(src)
    print(f"wrote {TARGET}")
else:
    print("no changes (all edits already present)")
