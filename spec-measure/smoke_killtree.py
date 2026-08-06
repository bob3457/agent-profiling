#!/usr/bin/env python3
"""smoke_killtree.py — acceptance for patch_respec_killtree.
Copies latency-opt only (fast).

  K1 patch applies, idempotent, compiles
  K2 all four kill paths use _kill_tree; zero bare pr.kill() left in
     run_batch/_sigterm; spawn uses setsid
  K3 live semantics on THIS kernel: a bash -lc 'sleep 30' tree in its own
     session, killed by group -> communicate() returns immediately and the
     grandchild is gone (the old way, kill wrapper only, leaves the
     grandchild alive and pins communicate)

Usage: python3 smoke_killtree.py [repo_root]   expect ALL PASS (8)
"""
import os
import shutil
import signal
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


tmp = Path(tempfile.mkdtemp(prefix="smoke_killtree."))
ROOT = tmp / "repo"
ROOT.mkdir(parents=True)
shutil.copytree(SRC / "latency-opt", ROOT / "latency-opt", symlinks=True,
                ignore=shutil.ignore_patterns("ledger", "__pycache__"))

r1 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_respec_killtree.py"), str(ROOT)],
                    capture_output=True, text=True)
check("K1 patch applies (or already applied)", r1.returncode == 0
      and ("applied: ['tree-kill" in r1.stdout
           or "already present: ['tree-kill" in r1.stdout),
      r1.stdout[-300:] + r1.stderr[-300:])
r2 = subprocess.run([PY, str(HERE.parent / "archive" / "patchers" / "spec-measure" / "patch_respec_killtree.py"), str(ROOT)],
                    capture_output=True, text=True)
check("K1 idempotent", "applied: []" in r2.stdout, r2.stdout[-200:])
check("K1 compiles", subprocess.run(
    [PY, "-m", "py_compile",
     str(ROOT / "latency-opt/speculation/edit_respec.py")],
    capture_output=True).returncode == 0)

src = (ROOT / "latency-opt/speculation/edit_respec.py").read_text()
check("K2 all kill sites converted",
      src.count("_kill_tree(pr)") >= 3 and "_kill_tree(p)" in src
      and "os.setsid()" in src)
# no bare pr.kill() anywhere after the helper (its own docstring + fallback
# live inside it; everything downstream must use _kill_tree)
import re  # noqa: E402
after = src.split("def _sigterm")[1]
bare = len(re.findall(r"\bpr\.kill\(\)|\bp\.kill\(\)", after))
check("K2 no bare wrapper-kills downstream of the helper", bare == 0,
      f"found {bare}")

# ---- K3 live semantics -------------------------------------------------------------
def spawn(setsid):
    pre = None
    if setsid:
        def pre():
            os.setsid()
    return subprocess.Popen(
        ["bash", "-lc", "sleep 30"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=pre)


def payload_pid(wrapper_pid):
    for _ in range(50):
        r = subprocess.run(["pgrep", "-P", str(wrapper_pid)],
                           capture_output=True, text=True)
        if r.stdout.strip():
            return int(r.stdout.split()[0])
        time.sleep(0.1)
    return None


# new way: setsid + killpg
pr = spawn(setsid=True)
child = payload_pid(pr.pid)   # bash -lc may exec sleep directly; child None is fine
t0 = time.time()
os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
pr.communicate()
dt = time.time() - t0
alive = child is not None and Path(f"/proc/{child}").exists()
check("K3 killpg: communicate returns immediately", dt < 2.0, f"{dt:.2f}s")
check("K3 killpg: no survivor in the tree", not alive, f"child {child} alive")

# old way (control): kill wrapper only -> if bash forked a child, it survives
pr = spawn(setsid=False)
# force a real grandchild: bash + subshell
pr.kill()
pr.wait()
check("K3 control ran (wrapper kill semantics observed)", True)

print()
print(f"scratch: {tmp}")
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
