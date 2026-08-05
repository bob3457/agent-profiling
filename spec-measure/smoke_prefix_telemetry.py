#!/usr/bin/env python3
"""smoke_prefix_telemetry.py — acceptance for patch_prefix_telemetry +
ceiling_report. Copies latency-opt only; runs the REAL daemon.

  T1 patch applies, idempotent, compiles
  T2 live daemon: compound whose part 0 is uncached -> prefix_attempt
     logged with stop_reason=no_entry and behavior unchanged (runs live)
  T3 live daemon: part 0 uncached but its key file EXISTS with a bad
     generation -> stop_reason=invalid_entry (the previously-invisible case)
  T4 live daemon: part 0 cached, part 1 has a pipe -> partial prefix_serve
     with stop_reason=hazard; output spliced correctly
  T5 ceiling_report on the produced dir: counts the interior cached part's
     duration that prefix-serve could not reach

Usage: python3 smoke_prefix_telemetry.py [repo_root]  expect ALL PASS (11)
"""
import hashlib
import json
import os
import shutil
import socket
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


def key_for(cwd, cmd):
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


tmp = Path(tempfile.mkdtemp(prefix="smoke_pretel."))
ROOT = tmp / "repo"
ROOT.mkdir()
shutil.copytree(SRC / "latency-opt", ROOT / "latency-opt", symlinks=True,
                ignore=shutil.ignore_patterns("ledger", "__pycache__"))

r1 = subprocess.run([PY, str(HERE / "patch_prefix_telemetry.py"), str(ROOT)],
                    capture_output=True, text=True)
check("T1 patch applies (or already applied)", r1.returncode == 0
      and ("applied: ['reason tracking" in r1.stdout
           or "already present: ['reason tracking" in r1.stdout),
      r1.stdout[-300:] + r1.stderr[-300:])
r2 = subprocess.run([PY, str(HERE / "patch_prefix_telemetry.py"), str(ROOT)],
                    capture_output=True, text=True)
check("T1 idempotent", "applied: []" in r2.stdout, r2.stdout[-200:])
check("T1 compiles", subprocess.run(
    [PY, "-m", "py_compile",
     str(ROOT / "latency-opt/scripts/shell_sessiond.py")],
    capture_output=True).returncode == 0)

SESSIOND = ROOT / "latency-opt/scripts/shell_sessiond.py"
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("sd", SESSIOND)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


class Daemon:
    def __init__(self, run_dir):
        self.cache = run_dir / "spec_cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        (run_dir / "shelld_logs").mkdir(exist_ok=True)
        self.sock = str(run_dir / "sock")
        self.proc = subprocess.Popen(
            [PY, str(SESSIOND), "--socket", self.sock, "--log-dir",
             str(run_dir / "shelld_logs"), "--spec-cache", str(self.cache)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(50):
            if os.path.exists(self.sock):
                return
            time.sleep(0.1)
        raise RuntimeError("no daemon")

    def ask(self, req):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(30)
        s.connect(self.sock)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(65536)
        s.close()
        return json.loads(buf)

    def decisions(self):
        f = self.cache / "serve_decisions.jsonl"
        return [json.loads(l) for l in f.open()] if f.exists() else []

    def stop(self):
        try:
            self.ask({"op": "shutdown"})
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def entry(cache, cwd, cmd, out, dur=5.0):
    (cache / f"{key_for(cwd, cmd)}.json").write_text(json.dumps(
        {"cmd": cmd, "cwd": cwd, "exit": 0, "stdout": out, "stderr": "",
         "workspace_fingerprint": sd.workspace_fingerprint(cwd),
         "speculated_at": time.time(), "duration_s": dur}))


rd = tmp / "res" / "swebench" / "t1"
ws = rd / "ws"
ws.mkdir(parents=True)
(ws / "a.txt").write_text("A\n")
d = Daemon(rd)
cwd = str(ws)

# T2: part0 uncached, part1 cached -> attempt fails with no_entry
entry(d.cache, cwd, "cat a.txt", "CACHED-A\n", dur=7.5)
res = d.ask({"cmd": "echo first && cat a.txt", "cwd": cwd})
att = [x for x in d.decisions() if x["decision"] == "prefix_attempt"]
check("T2 ran live, unchanged behavior", res.get("cached") is False
      and "first" in res.get("stdout", ""), json.dumps(res)[:150])
check("T2 prefix_attempt logged, reason no_entry",
      att and att[-1].get("stop_reason") == "no_entry"
      and att[-1].get("parts_served") == 0, json.dumps(att[-1:]))

# T3: part0's key file exists but with a stale generation -> invalid_entry
(d.cache / "GENERATION").write_text("9")
entry(d.cache, cwd, "echo first", "X\n")          # gen field absent...
k = key_for(cwd, "echo first")
e = json.loads((d.cache / f"{k}.json").read_text())
e["generation"] = "1"                              # ...now stale vs 9
(d.cache / f"{k}.json").write_text(json.dumps(e))
res = d.ask({"cmd": "echo first && cat a.txt", "cwd": cwd})
att = [x for x in d.decisions() if x["decision"] == "prefix_attempt"]
check("T3 invalid_entry distinguished",
      att and att[-1].get("stop_reason") == "invalid_entry",
      json.dumps(att[-1:]))
(d.cache / "GENERATION").unlink()
(d.cache / f"{k}.json").unlink()

# T4: part0 cached, part1 hazard (pipe) -> partial serve, stop_reason hazard
entry(d.cache, cwd, "cat a.txt", "CACHED-A\n", dur=7.5)   # refresh fp
res = d.ask({"cmd": "cat a.txt; echo hi | tr a-z A-Z", "cwd": cwd})
pre = [x for x in d.decisions() if x["decision"] == "prefix_serve"]
check("T4 partial served + spliced", res.get("stdout", "").startswith(
    "CACHED-A\n") and "HI" in res.get("stdout", ""), json.dumps(res)[:200])
check("T4 stop_reason=hazard on the record",
      pre and pre[-1].get("stop_reason") == "hazard"
      and pre[-1].get("parts_served") == 1, json.dumps(pre[-1:]))
d.stop()

# T5: ceiling report sees the interior part T2 could not reach
r = subprocess.run([PY, str(SRC / "spec-analysis/ceiling_report.py"),
                    str(tmp / "res"), "--repo", str(ROOT)],
                   capture_output=True, text=True)
check("T5 ceiling report runs", r.returncode == 0, r.stderr[-300:])
check("T5 interior seconds counted (7.5s cat behind live echo)",
      "7.5" in r.stdout and "interior" in r.stdout, r.stdout[-400:])
check("T5 ceiling line present", "CEILING:" in r.stdout, r.stdout[-200:])

print()
print(f"scratch: {tmp}")
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
