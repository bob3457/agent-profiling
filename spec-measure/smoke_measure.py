#!/usr/bin/env python3
"""smoke_measure.py — offline acceptance for the measurement instrumentation.
Runs against a COPY of the repo; the working tree is never touched.

  P  patcher applies cleanly, is idempotent, harness passes bash -n,
     patched python compiles
  G  gate on a fake SPEC_LLM_BIN: NOGO enforced -> worker NOT exec'd,
     cpu_gate_nogo.json written; NOGO + SPEC_GATE_SHADOW=1 -> worker IS
     exec'd, gate.json says speculate=false shadow=true; tokens parsed
     from the LLM's 'tokens used: N' footer (not estimated)
  W  worker atexit rusage dump; respec SIGTERM rusage dump
  R  gate_eval_report over a synthetic 4-benchmark tree: confusion matrix,
     realized precision, saved decomposition, CPU/token totals

Usage: python3 smoke_measure.py [repo_root]      expect ALL PASS (21)
"""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1
           else "/projects/kzhou6/czhai/agent-profiling")
HERE = Path(__file__).resolve().parent
PY = sys.executable
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="smoke_measure."))
ROOT = tmp / "repo"
ROOT.mkdir(parents=True)
shutil.copytree(SRC / "latency-opt", ROOT / "latency-opt", symlinks=True,
                ignore=shutil.ignore_patterns("ledger", "__pycache__"))

# ---- P: patch, idempotency, syntax --------------------------------------------
r1 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_measure.py"), str(ROOT)],
                    capture_output=True, text=True)
check("P patch applies", r1.returncode == 0 and "applied:" in r1.stdout,
      r1.stdout[-300:] + r1.stderr[-300:])
r2 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_measure.py"), str(ROOT)],
                    capture_output=True, text=True)
check("P idempotent", r2.returncode == 0 and "applied: []" in r2.stdout,
      r2.stdout[-300:] + r2.stderr[-300:])
rb = subprocess.run(["bash", "-n",
                     str(ROOT / "latency-opt/harness/run_latency_arm.sh")],
                    capture_output=True, text=True)
check("P harness bash -n", rb.returncode == 0, rb.stderr[-300:])
ok = True
for f in ("llm_gate.py", "speculative_worker.py", "edit_respec.py"):
    c = subprocess.run([PY, "-m", "py_compile",
                        str(ROOT / "latency-opt/speculation" / f)],
                       capture_output=True, text=True)
    ok = ok and c.returncode == 0
check("P patched python compiles", ok)
h = (ROOT / "latency-opt/harness/run_latency_arm.sh").read_text()
check("P harness gained freshqa + SPEC_ALL_BENCH",
      "freshqa)" in h and "SPEC_ALL_BENCH" in h and "SPEC_CPU_OUT" in h)

GATE = ROOT / "latency-opt/speculation/llm_gate.py"
WORKER = ROOT / "latency-opt/speculation/speculative_worker.py"
RESPEC = ROOT / "latency-opt/speculation/edit_respec.py"

# ---- G: gate behavior with a fake LLM ------------------------------------------
fake = tmp / "fakellm"
fake.write_text("#!/bin/bash\necho 'thinking...'\necho 'NO'\n"
                "echo 'tokens used: 123' >&2\n")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def run_gate(shadow, outdir):
    outdir.mkdir(parents=True)
    stmtf = outdir / "problem.txt"
    stmtf.write_text("What is the capital of France? Answer only.")
    marker = outdir / "worker_ran"
    gj = outdir / "gate.json"
    env = dict(os.environ, SPEC_LLM_BIN=str(fake), SPEC_CPU_OUT=str(outdir))
    if shadow:
        env["SPEC_GATE_SHADOW"] = "1"
    r = subprocess.run(
        [PY, str(GATE), "--problem-statement", str(stmtf),
         "--gate-json", str(gj), "--statement-only",
         "--", "bash", "-c", f"touch {marker}"],
        capture_output=True, text=True, env=env, timeout=60)
    return r, (json.loads(gj.read_text()) if gj.exists() else None), marker


r, gj, marker = run_gate(shadow=False, outdir=tmp / "g_enforced")
check("G enforced NOGO: verdict recorded",
      gj and gj.get("speculate") is False, json.dumps(gj))
check("G enforced NOGO: worker NOT exec'd", not marker.exists())
check("G enforced NOGO: cpu_gate_nogo.json written",
      (tmp / "g_enforced/cpu_gate_nogo.json").exists())
check("G tokens parsed, not estimated",
      gj and gj.get("tokens", {}).get("total") == 123
      and gj["tokens"].get("estimated") is False, json.dumps(gj))

r, gj, marker = run_gate(shadow=True, outdir=tmp / "g_shadow")
time.sleep(0.3)
check("G shadow NOGO: worker exec'd anyway", marker.exists(),
      r.stdout[-200:] + r.stderr[-200:])
check("G shadow NOGO: gate.json speculate=false shadow=true",
      gj and gj.get("speculate") is False and gj.get("shadow") is True,
      json.dumps(gj))

# ---- W: rusage dumps -----------------------------------------------------------
wd = tmp / "w"
ws = wd / "ws"; ws.mkdir(parents=True); (ws / "a.py").write_text("x=1\n")
cache = wd / "cache"
r = subprocess.run([PY, str(WORKER), "--workspace", str(ws),
                    "--cache-dir", str(cache), "--actions", "workspace_recon"],
                   capture_output=True, text=True,
                   env=dict(os.environ, SPEC_CPU_OUT=str(wd)), timeout=120)
cj = wd / "cpu_spec_worker.json"
check("W worker atexit cpu dump", cj.exists(),
      r.stdout[-200:] + r.stderr[-200:])
if cj.exists():
    j = json.loads(cj.read_text())
    check("W worker cpu fields sane",
          j.get("cpu_total_s", -1) >= 0 and j.get("wall_s", -1) >= 0,
          json.dumps(j))
else:
    check("W worker cpu fields sane", False, "no dump")

rd = tmp / "r"
rws = rd / "ws"; rws.mkdir(parents=True); (rws / "b.txt").write_text("y\n")
rcache = rd / "cache"; rcache.mkdir()
(rd / "spec.log").write_text("")
p = subprocess.Popen([PY, str(RESPEC), "--workspace", str(rws),
                      "--cache-dir", str(rcache),
                      "--spec-log", str(rd / "spec.log")],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     env=dict(os.environ, SPEC_CPU_OUT=str(rd)))
time.sleep(2.5)
p.send_signal(signal.SIGTERM)
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
check("W respec SIGTERM cpu dump", (rd / "cpu_respec.json").exists())

# ---- R: report over a synthetic 4-bench tree ------------------------------------
res = tmp / "results"


def mk_task(bench, task, go, saved_entries, gate_tokens=100, shadow=True):
    d = res / bench / task
    (d / "spec_cache").mkdir(parents=True)
    json.dump({"speculate": go, "gate_latency_s": 3.0, "shadow": shadow,
               "tokens": {"total": gate_tokens, "estimated": False},
               "gate": "llm_gate_v2"}, (d / "gate.json").open("w"))
    with (d / "spec_cache/serve_decisions.jsonl").open("w") as f:
        for dur in saved_entries:
            f.write(json.dumps({"ts": time.time(), "cmd": "pytest -x",
                                "key": "exact", "decision": "served",
                                "entry_dur_s": dur}) + "\n")
    (d / "shelld_logs").mkdir()
    with (d / "shelld_logs/commands.jsonl").open("w") as f:
        for dur in saved_entries:
            f.write(json.dumps({"ts": 0, "cmd": "pytest -x", "cwd": "/x",
                                "exit": 0, "wall_s": 0.0, "cpu_s": 0.0,
                                "cached": True, "session_reused": False})
                    + "\n")
        f.write(json.dumps({"ts": 0, "cmd": "ls", "cwd": "/x", "exit": 0,
                            "wall_s": 2.0, "cpu_s": 0.1, "cached": False,
                            "session_reused": True}) + "\n")
    json.dump({"cache_hits": len(saved_entries),
               "commands": len(saved_entries) + 1},
              (d / "daemon_stats.txt").open("w"))
    (d / "spec.log").write_text(
        "[spec] cached  (  1.0s, exit 0) pytest -x\n"
        "[spec] cached  (  2.0s, exit 0) git status\n")
    json.dump({"tag": "spec_worker", "cpu_total_s": 4.0, "wall_s": 30.0},
              (d / "cpu_spec_worker.json").open("w"))
    (d / "time.txt").write_text("\tUser time (seconds): 40.00\n"
                                "\tSystem time (seconds): 10.00\n")


# swebench: gate perfect (TP + TN); terminalbench: one FP, one FN;
# hotpotqa/freshqa: correct NOGOs
mk_task("swebench", "t1", True, [10.0, 5.0])
mk_task("swebench", "t2", False, [])
mk_task("terminalbench", "t3", True, [])        # FP
mk_task("terminalbench", "t4", False, [7.0])    # FN
mk_task("hotpotqa", "q1", False, [])
mk_task("freshqa", "f1", False, [])

r = subprocess.run([PY, str(HERE / "gate_eval_report.py"), str(res),
                    "--jsonl"], capture_output=True, text=True)
rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
by = {x["task"]: x for x in rows}
check("R report parsed all six tasks", len(rows) == 6,
      r.stderr[-300:] + str(len(rows)))
check("R saved decomposed", by.get("t1", {}).get("saved_s") == 15.0,
      json.dumps(by.get("t1")))
check("R predictions counted from spec.log",
      by.get("t1", {}).get("predictions_cached") == 2, json.dumps(by.get("t1")))
check("R cpu joined", by.get("t1", {}).get("spec_cpu_s") == 4.0
      and by.get("t1", {}).get("agent_cpu_s") == 50.0, json.dumps(by.get("t1")))

rt = subprocess.run([PY, str(HERE / "gate_eval_report.py"), str(res)],
                    capture_output=True, text=True)
sweb = rt.stdout.split("BENCH swebench")[1].split("BENCH")[0] \
    if "BENCH swebench" in rt.stdout else ""
tb = rt.stdout.split("BENCH terminalbench")[1].split("=" * 8)[0] \
    if "BENCH terminalbench" in rt.stdout else ""
check("R swebench confusion TP=1 TN=1",
      "TP= 1" in sweb and "TN= 1" in sweb and "acc=1.0" in sweb, sweb[:400])
check("R terminalbench confusion FP=1 FN=1",
      "FP= 1" in tb and "FN= 1" in tb, tb[:400])
check("R table renders all four benches",
      all(f"BENCH {b}" in rt.stdout for b in
          ("swebench", "terminalbench", "hotpotqa", "freshqa")),
      rt.stdout[:200] + rt.stderr[-200:])

print()
print(f"scratch kept under: {tmp}")
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
