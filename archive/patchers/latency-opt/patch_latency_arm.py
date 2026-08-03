#!/usr/bin/env python3
"""Patch run_latency_arm.sh: launch/kill the trajectory watcher in arm C.

Bare-host harness (GH200): the watcher runs as a host process beside the
worker -- no containers, no binds, real-filesystem scans. Inserted inside
the existing `ARM == C && (swebench|terminalbench)` block AFTER the gate/
worker section (the watcher runs regardless of the gate's worker verdict:
its trajectory sources are independent of the predictor, and it costs ~0
on trajectories with no edits). Killed in teardown beside spec.pid.

Anchors assert against the exact code inspected on 2026-07-30. Idempotent.
Usage: python3 patch_latency_arm.py [path]   (default $ROOT/latency-opt/harness/run_latency_arm.sh)
"""
import os
import sys
from pathlib import Path

DEFAULT = os.path.expandvars("$ROOT/latency-opt/harness/run_latency_arm.sh")
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
t = path.read_text()
if "respec.pid" in t:
    print(f"already patched: {path}")
    sys.exit(0)

# ---- launch: end of the gate/worker if, still inside the ARM C if -----------
a1 = '''        echo $! > "$run_dir/spec.pid"
      fi
    fi

    # ---- the run, whole-run perf + wall time --------------------------------'''
assert a1 in t, "ANCHOR 1 drifted: gate/worker block tail not found"
launch = '''        echo $! > "$run_dir/spec.pid"
      fi
      nohup python3 -u "$OPT/speculation/edit_respec.py" \\
        --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" \\
        --spec-log "$run_dir/spec.log" \\
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \\
        --agent-stream "$run_dir/stdout.jsonl" \\
        > "$run_dir/respec.log" 2>&1 &
      echo $! > "$run_dir/respec.pid"
    fi

    # ---- the run, whole-run perf + wall time --------------------------------'''
t = t.replace(a1, launch, 1)

# ---- teardown: beside the worker kill ----------------------------------------
a2 = '''  [[ -f "$run_dir/spec.pid" ]] && kill "$(cat "$run_dir/spec.pid")" 2>/dev/null'''
assert a2 in t, "ANCHOR 2 drifted: spec.pid kill not found"
t = t.replace(a2, a2 + '''
  [[ -f "$run_dir/respec.pid" ]] && kill "$(cat "$run_dir/respec.pid")" 2>/dev/null''', 1)

path.write_text(t)
print(f"patched OK: {path}")
print("  watcher: host process, launched in ARM C for swebench/terminalbench,")
print("  logs -> $run_dir/respec.log, killed in teardown via respec.pid")
