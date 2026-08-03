#!/usr/bin/env python3
"""Replace spec_gate.py's heuristic gate with the two-signal LLM gate in
run_latency_arm.sh.

The worker launch is wrapped by llm_gate.py, which reads the problem
statement, waits for the agent's first real command, makes one LLM call,
and exec()s into the worker on GO -- so spec.pid and the teardown kill are
unchanged. The statement comes from the harness's own $prompt (written to
$run_dir/problem.txt), making the gate benchmark-agnostic; the worker keeps
its swebench PS_ARG behavior.

Anchors on the exact block inspected 2026-07-30 (post watcher patch).
Idempotent. Usage: python3 patch_latency_arm_gate.py [path]
"""
import os
import re
import sys
from pathlib import Path

DEFAULT = os.path.expandvars("$ROOT/latency-opt/harness/run_latency_arm.sh")
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
t = path.read_text()
gate_region = re.search(r"llm_gate\.py.*?speculative_worker\.py", t, re.S)
if gate_region and "--agent-stream" in gate_region.group(0):
    print(f"already patched (v2): {path}")
    sys.exit(0)
if "llm_gate.py" in t:
    # v1 gate present: add the stream-primary signal. Anchor on the
    # commands-log + gate-json pair, unique to the gate invocation (the
    # watcher also passes --commands-log but never --gate-json).
    B = chr(92)  # single backslash, dodging nested-escaping errors
    a = ('        --commands-log "$run_dir/shelld_logs/commands.jsonl" '
         + B + chr(10) +
         '        --gate-json "$run_dir/gate.json"')
    ins = ('        --agent-stream "$run_dir/stdout.jsonl" ' + B + chr(10))
    assert a in t, "v1 gate found but its commands-log/gate-json pair drifted"
    t = t.replace(a, ins + a, 1)
    path.write_text(t)
    print(f"upgraded v1 -> v2 gate: {path}")
    sys.exit(0)

anchor = '''      gate=$(python3 "$OPT/speculation/spec_gate.py" --benchmark "$bench" --workspace "$workdir")
      echo "$gate" > "$run_dir/gate.json"
      if echo "$gate" | grep -q '"speculate": true'; then
        PS_ARG=""
        [[ $bench == swebench && -f $ROOT/prompts/swe_$tid.txt ]] && PS_ARG="--problem-statement $ROOT/prompts/swe_$tid.txt"
        nohup python3 "$OPT/speculation/speculative_worker.py" \\
          --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\
          --predictor both --ledger-dir "$ROOT/latency-opt/ledger" \\
          $PS_ARG > "$run_dir/spec.log" 2>&1 &
        echo $! > "$run_dir/spec.pid"
      fi'''
assert anchor in t, "ANCHOR drifted: spec_gate block not found verbatim"

replacement = '''      printf '%s' "$prompt" > "$run_dir/problem.txt"
      PS_ARG="--problem-statement $run_dir/problem.txt"
      [[ $bench == swebench && -f $ROOT/prompts/swe_$tid.txt ]] && PS_ARG="--problem-statement $ROOT/prompts/swe_$tid.txt"
      nohup python3 -u "$OPT/speculation/llm_gate.py" \\
        --problem-statement "$run_dir/problem.txt" \\
        --agent-stream "$run_dir/stdout.jsonl" \\
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \\
        --gate-json "$run_dir/gate.json" --timeout 90 \\
        -- python3 "$OPT/speculation/speculative_worker.py" \\
          --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" --benchmark "$bench" \\
          --predictor both --ledger-dir "$ROOT/latency-opt/ledger" \\
          $PS_ARG > "$run_dir/spec.log" 2>&1 &
      echo $! > "$run_dir/spec.pid"'''
t = t.replace(anchor, replacement, 1)
path.write_text(t)
print(f"patched OK: {path}")
print("  gate: llm_gate.py (statement + first action), exec's worker on GO")
print("  spec.log now begins with the [gate] verdict line")
