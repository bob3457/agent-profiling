#!/usr/bin/env python3
"""patch_gate_pending_record.py — a killed gate must leave evidence.

Run arm_C.20260731_185311 (broken-python, 18.7s wall): the fixed tap
waits for the agent's first message (~5-10s) + ~7s LLM call, so the
decision lands ~15s in; harness teardown killed the gate first and the
run has NO gate.json and an empty spec.log -- an audit hole.

Fix: write a pending gate.json immediately at startup. The decision
write later overwrites it, so a surviving record with
speculate=null/reason=pending means exactly one thing: the task ended
before the gate decided (worker never started -- correct on short
tasks, now also *recorded*).

Idempotent; verbatim anchor against the v2-patched llm_gate.py.
"""
import sys
from pathlib import Path

GATE = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt/speculation/llm_gate.py")

t = GATE.read_text()
if '"pending' in t:
    print("llm_gate.py: already patched, no-op")
else:
    A = '''    t0 = time.time()
    verdict, reason, action, kind = "GO", "fail-open default", None, None
    try:
        stmt = open(args.problem_statement, errors="replace").read()[:6000]'''
    N = '''    t0 = time.time()
    verdict, reason, action, kind = "GO", "fail-open default", None, None
    if args.gate_json:                 # killed-before-decision must be visible
        try:
            with open(args.gate_json, "w") as f:
                json.dump({"speculate": None,
                           "reason": "pending (if this record persists, the "
                                     "task ended before the gate decided)",
                           "gate": "llm_gate_v2"}, f, indent=1)
        except OSError:
            pass
    try:
        stmt = open(args.problem_statement, errors="replace").read()[:6000]'''
    assert A in t, "ANCHOR drifted: main() prologue not found verbatim"
    GATE.write_text(t.replace(A, N, 1))
    print("llm_gate.py: pending record written at startup")

import py_compile
py_compile.compile(str(GATE), doraise=True)
print("compiles OK")
