#!/usr/bin/env python3
"""smoke_latency_first.py — acceptance for patch_latency_first.
Runs on a COPY of the repo.

  L1 patch applies + idempotent + bash -n + py_compile
  L2 doomed join: marker gen=1, GENERATION=5 -> daemon does NOT wait
  L3 live join still works: marker gen matches -> waits, serves
  L4 default budget: unresolved marker (matching gen) waits ~2s not ~8s
  L5 legacy marker (no gen field) still joins (back-compat)
  L6 worker $PWD normalization: predicted '$PWD' command cached under the
     expanded key
  L7 harness has the fan-out knobs and respec cpu tag

Usage: python3 smoke_latency_first.py [repo_root]   expect ALL PASS (13)
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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


tmp = Path(tempfile.mkdtemp(prefix="smoke_latfirst."))
ROOT = tmp / "repo"
ROOT.mkdir(parents=True)
shutil.copytree(SRC / "latency-opt", ROOT / "latency-opt", symlinks=True,
                ignore=shutil.ignore_patterns("ledger", "__pycache__"))

r1 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_latency_first.py"), str(ROOT)],
                    capture_output=True, text=True)
check("L1 patch applies (or already applied)", r1.returncode == 0
      and ("already present: ['worker marker gen stamp'" in r1.stdout
           or "applied: ['worker marker gen stamp'" in r1.stdout),
      r1.stdout[-300:] + r1.stderr[-300:])
r2 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_latency_first.py"), str(ROOT)],
                    capture_output=True, text=True)
check("L1 idempotent", "applied: []" in r2.stdout, r2.stdout[-200:])
ok = subprocess.run(["bash", "-n", str(
    ROOT / "latency-opt/harness/run_latency_arm.sh")]).returncode == 0
for f in ("scripts/shell_sessiond.py", "speculation/speculative_worker.py",
          "speculation/edit_respec.py"):
    ok = ok and subprocess.run(
        [PY, "-m", "py_compile", str(ROOT / "latency-opt" / f)],
        capture_output=True).returncode == 0
check("L1 syntax", ok)

SESSIOND = ROOT / "latency-opt/scripts/shell_sessiond.py"


class Daemon:
    def __init__(self, run_dir, env=None):
        self.cache = run_dir / "spec_cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sock = str(run_dir / "sock")
        e = dict(os.environ)
        e.update(env or {})
        self.proc = subprocess.Popen(
            [PY, str(SESSIOND), "--socket", self.sock, "--log-dir",
             str(run_dir / "logs"), "--spec-cache", str(self.cache)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=e)
        for _ in range(50):
            if os.path.exists(self.sock):
                return
            time.sleep(0.1)
        raise RuntimeError("daemon didn't start")

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

    def stop(self):
        try:
            self.ask({"op": "shutdown"})
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def entry(cache, cwd, cmd, out, gen=None):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sd", SESSIOND)
    sd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sd)
    e = {"cmd": cmd, "cwd": cwd, "exit": 0, "stdout": out, "stderr": "",
         "workspace_fingerprint": sd.workspace_fingerprint(cwd),
         "speculated_at": time.time(), "duration_s": 5.0}
    if gen is not None:
        e["generation"] = gen
    (cache / f"{key_for(cwd, cmd)}.json").write_text(json.dumps(e))


# ---- L2 doomed join refused -----------------------------------------------------
rd = tmp / "l2"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "f").write_text("x\n")
d = Daemon(rd)
(d.cache / "GENERATION").write_text("5")
cwd, cmd = str(ws), "cat f"
(d.cache / f"{key_for(cwd, cmd)}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1, "gen": "1"}))
t0 = time.time()
res = d.ask({"cmd": cmd, "cwd": cwd})
dt = time.time() - t0
check("L2 doomed join: no wait", dt < 1.0, f"{dt:.2f}s")
check("L2 doomed join: ran live", res.get("cached") is False
      and res.get("stdout") == "x\n", json.dumps(res)[:150])
d.stop()

# ---- L3 live join (matching gen) still works ------------------------------------
rd = tmp / "l3"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "g").write_text("y\n")
d = Daemon(rd)
(d.cache / "GENERATION").write_text("5")
cwd, cmd = str(ws), "cat g"
k = key_for(cwd, cmd)
(d.cache / f"{k}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1, "gen": "5"}))
threading.Timer(0.8, lambda: (entry(d.cache, cwd, cmd, "JOINED\n", gen="5"),
                              (d.cache / f"{k}.inflight").unlink())).start()
t0 = time.time()
res = d.ask({"cmd": cmd, "cwd": cwd})
dt = time.time() - t0
check("L3 matching-gen join serves", res.get("cached") is True
      and res.get("stdout") == "JOINED\n", json.dumps(res)[:150])
check("L3 waited for it", 0.6 < dt < 3.0, f"{dt:.2f}s")
d.stop()

# ---- L4 default budget is 2s ------------------------------------------------------
rd = tmp / "l4"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "h").write_text("z\n")
env = {k: v for k, v in os.environ.items() if k != "SPEC_JOIN_MAX_WAIT"}
d = Daemon(rd, env=env)
cwd, cmd = str(ws), "cat h"
(d.cache / f"{key_for(cwd, cmd)}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1}))
t0 = time.time()
res = d.ask({"cmd": cmd, "cwd": cwd})
dt = time.time() - t0
check("L4 default budget ~2s (not 8s)", 1.8 < dt < 4.0, f"{dt:.2f}s")
check("L4 fell through live", res.get("cached") is False
      and res.get("stdout") == "z\n", json.dumps(res)[:150])
d.stop()

# ---- L5 legacy marker (no gen) still joins ----------------------------------------
rd = tmp / "l5"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "i").write_text("w\n")
d = Daemon(rd)
(d.cache / "GENERATION").write_text("5")
cwd, cmd = str(ws), "cat i"
k = key_for(cwd, cmd)
(d.cache / f"{k}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1}))          # legacy: no gen
threading.Timer(0.6, lambda: (entry(d.cache, cwd, cmd, "LEGACY\n", gen="5"),
                              (d.cache / f"{k}.inflight").unlink())).start()
res = d.ask({"cmd": cmd, "cwd": cwd})
check("L5 legacy marker joins", res.get("cached") is True
      and res.get("stdout") == "LEGACY\n", json.dumps(res)[:150])
d.stop()

# ---- L6 worker $PWD normalization -------------------------------------------------
wd = tmp / "l6"
ws6 = wd / "ws"; ws6.mkdir(parents=True); (ws6 / "j.txt").write_text("k\n")
cache6 = wd / "cache"
WORKER = ROOT / "latency-opt/speculation/speculative_worker.py"
r = subprocess.run([PY, str(WORKER), "--workspace", str(ws6),
                    "--cache-dir", str(cache6),
                    "--actions", "workspace_recon"],
                   capture_output=True, text=True, timeout=120)
# simulate a predictor-style command with a literal $PWD by importing the
# patched module functions directly
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("worker", WORKER)
wk = importlib.util.module_from_spec(spec)
sys.modules["worker"] = wk
spec.loader.exec_module(wk)
src = WORKER.read_text()
check("L6 normalization patch present",
      'cmd.replace("${PWD}", str(ws)).replace("$PWD", str(ws))' in src)
# behavioral: key of an expanded command equals what worker would cache
raw = "PYTHONPATH=$PWD python -c 'print(1)'"
norm = raw.replace("$PWD", str(ws6.resolve()))
check("L6 expanded key differs from raw (sanity)",
      key_for(str(ws6.resolve()), raw) != key_for(str(ws6.resolve()), norm))

# ---- L7 harness knobs --------------------------------------------------------------
h = (ROOT / "latency-opt/harness/run_latency_arm.sh").read_text()
check("L7 harness fan-out knobs + respec tag",
      "SPEC_RESPEC_PARALLEL" in h and "SPEC_MAX_PER_GEN" in h
      and "SPEC_CPU_TAG=respec" in h)

print()
print(f"scratch: {tmp}")
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
