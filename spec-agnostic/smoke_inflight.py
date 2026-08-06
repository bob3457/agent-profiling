#!/usr/bin/env python3
"""smoke_inflight.py — offline acceptance for the daemon's in-flight join
(waiting briefly on a speculator that is executing the requested key).
Run from the repo root:   python3 smoke_inflight.py [repo_root]

1. UNIT (imported sessiond): fresh marker + entry landing mid-wait -> join
   + serve, decisions carry joined_inflight+served; marker without entry ->
   bounded timeout (respects SPEC_JOIN_MAX_WAIT); stale marker -> no wait;
   plain miss -> no wait.
2. WORKER: after a full worker run, zero *.inflight residue.
3. DAEMON (real socket): marker exists at request time, entry written 1s
   later by a 'speculator' thread -> reply is cached=True with the entry's
   stdout; joined_inflight in serve_decisions.jsonl; stats cache_hits=1.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
SESSIOND = ROOT / "latency-opt/scripts/shell_sessiond.py"
WORKER = ROOT / "latency-opt/speculation/speculative_worker.py"
PY = sys.executable
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


spec = importlib.util.spec_from_file_location("sessiond", SESSIOND)
sd = importlib.util.module_from_spec(spec)
sys.modules["sessiond"] = sd
spec.loader.exec_module(sd)

import hashlib  # noqa: E402


def key_for(cwd, cmd):
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


def write_entry(cache, cwd, cmd, out="OUT\n"):
    e = {"cmd": cmd, "cwd": cwd, "exit": 0, "stdout": out, "stderr": "",
         "workspace_fingerprint": sd.workspace_fingerprint(cwd),
         "speculated_at": time.time(), "duration_s": 1.0}
    (Path(cache) / f"{key_for(cwd, cmd)}.json").write_text(json.dumps(e))


# ---- 1. unit -----------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    ws = td / "ws"; ws.mkdir(); (ws / "f.txt").write_text("a\nb\n")
    cache = td / "cache"; cache.mkdir()
    cwd, cmd = str(ws), "wc -l f.txt"
    k = key_for(cwd, cmd)

    # 1a. join: entry lands 0.8s into the wait
    (cache / f"{k}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))
    threading.Timer(0.8, write_entry, args=(cache, cwd, cmd)).start()
    t0 = time.time()
    hit = sd.spec_cache_lookup(str(cache), cmd, cwd, wait_inflight=True)
    dt = time.time() - t0
    check("unit join serves", hit is not None and hit["stdout"] == "OUT\n")
    check("unit join waited ~0.8s", 0.6 < dt < 3.0, f"{dt:.2f}s")
    dec = [json.loads(l)["decision"] for l in (cache / "serve_decisions.jsonl").open()]
    check("unit decisions joined+served",
          any(d.startswith("joined_inflight") for d in dec) and "served" in dec, str(dec))

    # 1b. timeout: marker, no entry, 1s cap
    cmd2 = "cat f.txt"; k2 = key_for(cwd, cmd2)
    (cache / f"{k2}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))
    os.environ["SPEC_JOIN_MAX_WAIT"] = "1"
    t0 = time.time()
    hit = sd.spec_cache_lookup(str(cache), cmd2, cwd, wait_inflight=True)
    dt = time.time() - t0
    check("unit timeout returns None", hit is None)
    check("unit timeout bounded ~1s", 0.9 < dt < 2.0, f"{dt:.2f}s")
    dec = [json.loads(l)["decision"] for l in (cache / "serve_decisions.jsonl").open()]
    check("unit timeout logged", any(d.startswith("inflight_timeout") for d in dec), str(dec))

    # 1c. stale marker: no wait
    cmd3 = "head f.txt"; k3 = key_for(cwd, cmd3)
    (cache / f"{k3}.inflight").write_text(json.dumps({"ts": time.time() - 9999, "pid": 1}))
    t0 = time.time()
    sd.spec_cache_lookup(str(cache), cmd3, cwd, wait_inflight=True)
    check("unit stale marker skipped", time.time() - t0 < 0.3)

    # 1d. plain miss: no wait
    t0 = time.time()
    sd.spec_cache_lookup(str(cache), "tail f.txt", cwd, wait_inflight=True)
    check("unit plain miss fast", time.time() - t0 < 0.3)
    del os.environ["SPEC_JOIN_MAX_WAIT"]

# ---- 2. worker leaves no residue ----------------------------------------------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    ws = td / "ws"; ws.mkdir(); (ws / "README.md").write_text("hi\n")
    cache = td / "cache"
    env = dict(os.environ, SPEC_UPSTREAM_GATE="GO")
    r = subprocess.run([PY, str(WORKER), "--workspace", str(ws),
                        "--cache-dir", str(cache), "--benchmark", "terminalbench",
                        "--actions", "workspace_recon", "--nice", "0"],
                       capture_output=True, text=True, timeout=120, env=env)
    left = list(cache.glob("*.inflight"))
    check("worker exits clean", r.returncode == 0, (r.stdout + r.stderr)[-200:])
    check("worker no inflight residue", not left, str(left))
    check("worker cached entries", len(list(cache.glob("*.json"))) >= 3)

# ---- 3. live daemon join --------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    ws = td / "ws"; ws.mkdir(); (ws / "f.txt").write_text("a\nb\n")
    cache = td / "cache"; cache.mkdir()
    logs = td / "logs"
    sock = str(td / "sock")
    cwd, cmd = str(ws), "wc -l f.txt"
    k = key_for(cwd, cmd)
    (cache / f"{k}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))

    daemon = subprocess.Popen([PY, str(SESSIOND), "--socket", sock,
                               "--log-dir", str(logs), "--spec-cache", str(cache)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.1)

    threading.Timer(1.0, lambda: (write_entry(cache, cwd, cmd, "SPECULATED\n"),
                                  (cache / f"{k}.inflight").unlink())).start()

    def ask(req):
        s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(sock)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(65536)
        return json.loads(buf)

    t0 = time.time()
    res = ask({"cmd": cmd, "cwd": cwd})
    dt = time.time() - t0
    check("daemon join cached=True", res.get("cached") is True, json.dumps(res)[:200])
    check("daemon join served output", res.get("stdout") == "SPECULATED\n")
    check("daemon join waited ~1s", 0.8 < dt < 4.0, f"{dt:.2f}s")
    stats = ask({"op": "stats"})
    check("daemon stats hit", stats.get("cache_hits") == 1, str(stats))
    dec = [json.loads(l)["decision"] for l in (cache / "serve_decisions.jsonl").open()]
    check("daemon decision joined_inflight",
          any(d.startswith("joined_inflight") for d in dec), str(dec))
    ask({"op": "shutdown"})
    daemon.wait(timeout=10)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
