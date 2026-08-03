#!/usr/bin/env python3
"""Incremental sessiond patch (v2): decision-log completeness.

  1. Serve decisions now record the entry's measured duration
     (entry_dur_s) -- the true price of a serve, so the analyzer stops
     undercounting worker-origin hits.
  2. Misses are logged for ANY test-looking command (pytest/runtests.py),
     not only family-keyed ones -- run 143834 showed compound commands
     slipping through unlogged.

Anchors on the exact code inserted by patch_sessiond.py. Idempotent.
Usage: python3 patch_sessiond_v2.py [path]   (default $ROOT/latency-opt/scripts/shell_sessiond.py)
"""
import os
import sys
from pathlib import Path

DEFAULT = os.path.expandvars("$ROOT/latency-opt/scripts/shell_sessiond.py")
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
t = path.read_text()
assert "_spec_entry_invalid" in t, "base patch (patch_sessiond.py) not applied"
if "entry_dur_s" in t:
    print(f"already patched (v2): {path}")
    sys.exit(0)

a1 = '''            rec["entry_age_s"] = round(
                time.time() - entry.get("speculated_at", time.time()), 1)'''
assert a1 in t, "ANCHOR 1 drifted: entry_age_s block not found"
t = t.replace(a1, a1 + '''
            rec["entry_dur_s"] = entry.get("duration_s")''', 1)

a2 = '''    if len(keys) > 1:  # a family key existed => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)'''
assert a2 in t, "ANCHOR 2 drifted: miss-gate not found"
t = t.replace(a2, '''    if len(keys) > 1 or "pytest" in cmd or "runtests.py" in cmd:
        # family-keyed OR test-looking => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)''', 1)

path.write_text(t)
print(f"patched OK (v2): {path}")
