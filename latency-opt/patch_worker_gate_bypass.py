#!/usr/bin/env python3
"""patch_worker_gate_bypass.py — fix the double-gate bug observed on
broken-python (2026-07-31 run arm_C.20260731_183452):

    [gate] GO: llm: edit-and-verify loop expected
    [spec] gate says no: workspace has no recognizable prep surface

llm_gate exec()s into the worker on GO, but the worker then re-runs its
own internal spec_gate.should_speculate() and can re-refuse on exactly
the surface heuristics the LLM gate replaced. Two edits:

  1. llm_gate.py: on GO, export SPEC_UPSTREAM_GATE=GO into the environ
     the worker inherits through execvp.
  2. speculative_worker.py: if the internal gate refuses but
     SPEC_UPSTREAM_GATE=GO is present, do not return -- fall back to a
     minimal generic plan derived from what actually exists in the
     workspace (repo_index always; git_status iff .git; the pytest
     actions iff the refusing gate itself saw py_project/has_tests).
     The internal gate's ACCEPT path is unchanged, and a refusal with
     no upstream override still returns as before.

Idempotent; asserts verbatim anchors from the deployed files and
refuses to write on drift.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt/speculation")

# ---------------------------------------------------------------- llm_gate.py
g = ROOT / "llm_gate.py"
gt = g.read_text()
G_ANCHOR = "    if verdict == \"GO\" and worker:\n        os.execvp(worker[0], worker)   # become the worker: pid continuity"
G_NEW = ("    if verdict == \"GO\" and worker:\n"
         "        os.environ[\"SPEC_UPSTREAM_GATE\"] = \"GO\"   # worker: skip internal re-gate\n"
         "        os.execvp(worker[0], worker)   # become the worker: pid continuity")
if "SPEC_UPSTREAM_GATE" in gt:
    print("llm_gate.py: already patched, no-op")
else:
    assert G_ANCHOR in gt, "ANCHOR drifted: llm_gate exec block not found verbatim"
    g.write_text(gt.replace(G_ANCHOR, G_NEW, 1))
    print("llm_gate.py: exports SPEC_UPSTREAM_GATE=GO before execvp")

# ---------------------------------------------------- speculative_worker.py
w = ROOT / "speculative_worker.py"
wt = w.read_text()
W_ANCHOR = """        d = should_speculate(args.benchmark, str(ws),
                             task_text=ctx.get("problem_statement", ""),
                             ledger_dir=args.ledger_dir)
        if not d.speculate:
            print(f"[spec] gate says no: {d.reason}")
            return
        plan = d.actions"""
W_NEW = """        d = should_speculate(args.benchmark, str(ws),
                             task_text=ctx.get("problem_statement", ""),
                             ledger_dir=args.ledger_dir)
        if not d.speculate:
            if os.environ.get("SPEC_UPSTREAM_GATE") == "GO":
                plan = ["repo_index"]
                if (ws / ".git").exists():
                    plan.insert(0, "git_status")
                feats = getattr(d, "features", None) or {}
                if feats.get("py_project") or feats.get("has_tests"):
                    plan += ["pytest_collect", "pytest_run_cached"]
                print(f"[spec] internal gate refused ({d.reason}) but "
                      f"upstream LLM gate said GO -> generic plan {plan}")
            else:
                print(f"[spec] gate says no: {d.reason}")
                return
        else:
            plan = d.actions"""
if "SPEC_UPSTREAM_GATE" in wt:
    print("speculative_worker.py: already patched, no-op")
else:
    assert W_ANCHOR in wt, "ANCHOR drifted: worker internal-gate block not found verbatim"
    wt = wt.replace(W_ANCHOR, W_NEW, 1)
    w.write_text(wt)
    print("speculative_worker.py: upstream-GO override with generic plan")

import py_compile
py_compile.compile(str(g), doraise=True)
py_compile.compile(str(w), doraise=True)
print("both compile OK")
