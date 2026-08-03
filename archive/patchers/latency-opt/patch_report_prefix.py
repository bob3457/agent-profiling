#!/usr/bin/env python3
"""patch_report_prefix.py — teach spec_window_report.py the prefix_serve
decision type: count it as a serve, add its saved_s to the served-seconds
ledger, include it in the bump->query window distribution, and expose a
separate prefix_serves counter. Idempotent; verbatim anchors."""
import sys
from pathlib import Path

RPT = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt/spec_window_report.py")

t = RPT.read_text()
if "prefix_serve" in t:
    print("spec_window_report.py: already patched, no-op")
    sys.exit(0)

A1 = '''            if prior and d in ("served", "stale_generation"):
                windows.append(round(q_s - prior[-1], 1))
            if d == "served":
                dur = r.get("entry_dur_s")
                if dur is None:
                    dur = dur_by_cmd.get(r.get("entry_cmd", ""), 0.0)
                served_saved += dur or 0.0'''
N1 = '''            if prior and d in ("served", "stale_generation", "prefix_serve"):
                windows.append(round(q_s - prior[-1], 1))
            if d == "served":
                dur = r.get("entry_dur_s")
                if dur is None:
                    dur = dur_by_cmd.get(r.get("entry_cmd", ""), 0.0)
                served_saved += dur or 0.0
            elif d == "prefix_serve":
                served_saved += r.get("saved_s") or 0.0'''
assert A1 in t, "ANCHOR drifted: decision loop"
t = t.replace(A1, N1, 1)

A2 = '''    out["served"] = decisions.get("served", 0)
    out["served_saved_s"] = round(served_saved, 1)'''
N2 = '''    out["served"] = decisions.get("served", 0) + decisions.get("prefix_serve", 0)
    out["prefix_serves"] = decisions.get("prefix_serve", 0)
    out["served_saved_s"] = round(served_saved, 1)'''
assert A2 in t, "ANCHOR drifted: out served/saved lines"
t = t.replace(A2, N2, 1)

RPT.write_text(t)
import py_compile
py_compile.compile(str(RPT), doraise=True)
print("spec_window_report.py: prefix_serve counted (serves, saved_s, windows)")
