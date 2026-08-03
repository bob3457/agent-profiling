#!/usr/bin/env python3
"""Make run_option_b.sh's base mode a true arm-B baseline.

Currently base = arm A (no wrapper, no daemon): commands never pass through
shelld, so commands.jsonl is empty and no tool-level comparison against spec
runs is possible. This patch gives base the wrapper + daemon env WITHOUT the
spec cache (CODEX_SHELLD_SPEC stays spec-only), and extends the daemon
shutdown to base runs -- without that, every base run leaks a daemon into
the shared host PID namespace.

Anchors assert against the exact code inspected on 2026-07-30. Idempotent.
Usage: python3 patch_option_b_base.py [path]
"""
import os
import sys
from pathlib import Path

DEFAULT = os.path.expandvars("$ROOT/latency-opt/harness/run_option_b.sh")
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
t = path.read_text()
if 'MODE == spec || $MODE == base' in t:
    print(f"already patched: {path}")
    sys.exit(0)

a1 = '''AGENT_ENV=""
if [[ $MODE == spec ]]; then
  RUNTAG=$(date +%s).$$
  AGENT_ENV="export CODEX_TOOL_PERF_WRAPPER=/opt/latency-opt/scripts/codex_persistent_shell_wrap.sh;
             export CODEX_SHELLD_SOCK=/tmp/shelld.$RUNTAG/sock;
             export CODEX_SHELLD_LOGDIR=/spec_logs/shelld;
             export CODEX_SHELLD_SPEC=/spec_cache;
             export CODEX_SHELLD_PYTHON=/opt/toolpy/bin/python3;"
fi'''
assert a1 in t, "ANCHOR 1 drifted: AGENT_ENV block not found verbatim"
t = t.replace(a1, '''AGENT_ENV=""
if [[ $MODE == spec || $MODE == base ]]; then
  RUNTAG=$(date +%s).$$
  AGENT_ENV="export CODEX_TOOL_PERF_WRAPPER=/opt/latency-opt/scripts/codex_persistent_shell_wrap.sh;
             export CODEX_SHELLD_SOCK=/tmp/shelld.$RUNTAG/sock;
             export CODEX_SHELLD_LOGDIR=/spec_logs/shelld;
             export CODEX_SHELLD_PYTHON=/opt/toolpy/bin/python3;"
  [[ $MODE == spec ]] && AGENT_ENV="$AGENT_ENV
             export CODEX_SHELLD_SPEC=/spec_cache;"
fi''', 1)

a2 = '''if [[ $MODE == spec ]]; then
  python3 - "/tmp/shelld.$RUNTAG/sock"'''
assert a2 in t, "ANCHOR 2 drifted: daemon-shutdown gate not found"
t = t.replace(a2, '''if [[ $MODE == spec || $MODE == base ]]; then
  python3 - "/tmp/shelld.$RUNTAG/sock"''', 1)

path.write_text(t)
print(f"patched OK: {path}")
print("  base mode = arm B: wrapper + daemon + shutdown, NO spec cache")
