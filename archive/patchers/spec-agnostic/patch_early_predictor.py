#!/usr/bin/env python3
"""patch_early_predictor.py — move the LLM predictor off the gate's critical
path on terminalbench. Two lost races measured (gate 14.7s stream, 32.1s
statement-only vs agent asks at T+15-20s): the gate call is high-variance
and short-task speculation cannot wait for it. Idempotent; verbatim anchors.
Requires patch_worker_generic.py + patch_harness_early_recon.py first.

Worker: SPEC_DIRECT_ONLY=1 routes ONLY non-family predictions through the
tier-gated direct path and DROPS family-parsed ones (pytest/django) — the
heavy execution tier stays behind the gate.

Harness: on terminalbench the T=0 early worker gains
  --predictor llm --problem-statement ... --timeout-per-cmd 20  (+ SPEC_DIRECT_ONLY=1)
and the gated worker drops to --predictor heuristic there (no duplicate LLM
call/tokens). swebench is unchanged: recon-only early, gated LLM predictor.

Run:  python3 patch_early_predictor.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")


def apply(path, name, old, new):
    src = path.read_text()
    if new in src:
        print(f"  = {name}: already applied")
        return
    assert old in src, f"ANCHOR DRIFT ({name}): expected bytes not found"
    assert src.count(old) == 1, f"ANCHOR AMBIGUOUS ({name})"
    path.write_text(src.replace(old, new))
    print(f"  + {name}")


W = ROOT / "latency-opt/speculation/speculative_worker.py"
apply(W, "direct-only guard",
      """        for c in llm_cmds:
            pc = parse_command(c)
            if not pc:
                _collect_direct(ctx, c)
                continue""",
      """        direct_only = os.environ.get("SPEC_DIRECT_ONLY") == "1"
        for c in llm_cmds:
            pc = parse_command(c)
            if not pc:
                _collect_direct(ctx, c)
                continue
            if direct_only:
                print(f"[spec] direct-only: family prediction deferred to gated worker: {c!r}")
                continue""")

H = ROOT / "latency-opt/harness/run_latency_arm.sh"
apply(H, "early predictor on tb",
      """      mkdir -p "$CODEX_SHELLD_SPEC"
      # T=0 ungated recon: read-only, ~0.2s CPU, nothing to gate; wins the
      # first-20-seconds window that the gate+predictor serialization loses.
      nohup python3 -u "$OPT/speculation/speculative_worker.py" \\
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\
        --actions workspace_recon --nice 10 \\
        > "$run_dir/spec_early.log" 2>&1 &
      echo $! > "$run_dir/spec_early.pid"
      printf '%s' "$prompt" > "$run_dir/problem.txt\"""",
      """      mkdir -p "$CODEX_SHELLD_SPEC"
      printf '%s' "$prompt" > "$run_dir/problem.txt"
      # T=0 ungated worker: recon everywhere; on TB also the LLM predictor in
      # DIRECT-ONLY mode (tier0 reads; family/heavy predictions stay gated).
      # Two measured lost races showed the gate call is high-variance and
      # short-task speculation cannot sit behind it.
      EARLY_XTRA=""
      [[ $bench == terminalbench ]] && \\
        EARLY_XTRA="--predictor llm --problem-statement $run_dir/problem.txt --timeout-per-cmd 20"
      SPEC_DIRECT_ONLY=1 nohup python3 -u "$OPT/speculation/speculative_worker.py" \\
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\
        --actions workspace_recon $EARLY_XTRA --nice 10 \\
        > "$run_dir/spec_early.log" 2>&1 &
      echo $! > "$run_dir/spec_early.pid\"""")

apply(H, "gated worker heuristic on tb",
      '          --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\\n'
      '          --predictor both --ledger-dir "$ROOT/latency-opt/ledger" \\',
      '          --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\\n'
      '          --predictor $([[ $bench == terminalbench ]] && echo heuristic || echo both) \\\n'
      '          --ledger-dir "$ROOT/latency-opt/ledger" \\')

print("done")
