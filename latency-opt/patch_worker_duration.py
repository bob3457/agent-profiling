#!/usr/bin/env python3
"""patch_worker_duration.py — worker entries record duration_s.

Evidence (run 191603): the family serve of test_icrs (~7s of real pytest)
logged entry_dur_s=null and contributed 0 to saved_s, because both
cache_put variants build entries from proc alone while the measured wall
(`dt`, printed in every '[spec] cached (%5.1fs ...)' line) never reaches
them. Fix: ride `dt` on the proc object once before the caching branch
(one assignment covers both the family and exact branches), and both
entry dicts read it via getattr with a None default -- any other call
site keeps today's behavior. The watcher already stamps duration_s; this
closes the gap on the worker side so every serve carries real seconds.

Idempotent; verbatim anchors."""
import sys
from pathlib import Path

W = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt/speculation/speculative_worker.py")

t = W.read_text()
if "spec_duration_s" in t:
    print("speculative_worker.py: already patched, no-op")
    sys.exit(0)

A1 = '''    entry = {
        "cmd": cmd, "cwd": cwd, "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:], "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint, "speculated_at": time.time(),
        "family": True,
    }'''
N1 = '''    entry = {
        "cmd": cmd, "cwd": cwd, "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:], "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint, "speculated_at": time.time(),
        "duration_s": getattr(proc, "spec_duration_s", None),
        "family": True,
    }'''
assert A1 in t, "ANCHOR drifted: family entry dict"
t = t.replace(A1, N1, 1)

A2 = '''    entry = {
        "cmd": cmd, "cwd": cwd,
        "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:],
        "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint,
        "speculated_at": time.time(),
    }'''
N2 = '''    entry = {
        "cmd": cmd, "cwd": cwd,
        "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:],
        "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint,
        "speculated_at": time.time(),
        "duration_s": getattr(proc, "spec_duration_s", None),
    }'''
assert A2 in t, "ANCHOR drifted: exact entry dict"
t = t.replace(A2, N2, 1)

A3 = '''                if cache_put_family(cache_dir, str(ws), cmd,
                                    proc, workspace_fingerprint(str(ws))):'''
N3 = '''                proc.spec_duration_s = dt   # entries record real cost for saved_s
                if cache_put_family(cache_dir, str(ws), cmd,
                                    proc, workspace_fingerprint(str(ws))):'''
assert A3 in t, "ANCHOR drifted: caching branch head"
t = t.replace(A3, N3, 1)

# the family call is nested one level deeper than `elif cacheable:` (16 vs 12
# spaces), so the exact-put branch needs its own assignment:
A4 = """                cache_put(cache_dir, str(ws), cmd, proc, workspace_fingerprint(str(ws)))"""
N4 = """                proc.spec_duration_s = dt   # entries record real cost for saved_s
                cache_put(cache_dir, str(ws), cmd, proc, workspace_fingerprint(str(ws)))"""
assert A4 in t, "ANCHOR drifted: exact-put call site"
t = t.replace(A4, N4, 1)

W.write_text(t)
import py_compile
py_compile.compile(str(W), doraise=True)
print("speculative_worker.py: duration_s stamped on family+exact entries")
