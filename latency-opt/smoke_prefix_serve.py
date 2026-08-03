#!/usr/bin/env python3
"""smoke_prefix_serve.py — end-to-end prefix-serve validation against the
REAL patched shell_sessiond.py, no harness involved. Seeds a spec cache,
starts the daemon on a temp socket, replays request shapes taken from the
astropy-13398 live runs (20260731), and asserts serve semantics.

Usage: python3 smoke_prefix_serve.py [path/to/shell_sessiond.py]
Exit 0 = all scenarios pass.
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

SESSIOND = sys.argv[1] if len(sys.argv) > 1 else \
    "/projects/kzhou6/czhai/agent-profiling/latency-opt/scripts/shell_sessiond.py"

tmp = Path(tempfile.mkdtemp(prefix="prefix_smoke_"))
cache = tmp / "spec_cache"
logs = tmp / "logs"
ws = tmp / "ws"
for d in (cache, logs, ws):
    d.mkdir(parents=True)
sock = str(tmp / "shelld.sock")

(cache / "GENERATION").write_text("3")


def seed(cmd, cwd, exit_code, stdout, dur):
    key = hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()
    (cache / f"{key}.json").write_text(json.dumps({
        "cmd": cmd, "exit": exit_code, "stdout": stdout, "stderr": "",
        "generation": "3", "duration_s": dur,
        "speculated_at": time.time()}))


CWD = str(ws)
# scenario A parts (morning decision 1 shape, ; joiners)
seed("git diff --check", CWD, 0, "", 0.4)
seed("git status --short", CWD, 0, " M itrs.py\n", 0.6)
seed("python -m pytest tests/test_x.py -q", CWD, 1,
     "1 failed, 3 passed\n", 9.9)
# scenario B parts (morning decision 3 shape, && short-circuit on failure)
seed("python -m pytest tests/test_x.py -q -k 'not t1'", CWD, 1,
     "1 failed, 2 passed\n", 6.1)
# scenario D: cd-folded normalization (cached under the folded cwd)
seed("python -m pytest tests/test_y.py -q", CWD, 0, "4 passed\n", 3.3)
# scenario F: stale-generation part must NOT serve
key = hashlib.sha256(f"{CWD}\x00echo fresh".encode()).hexdigest()
(cache / f"{key}.json").write_text(json.dumps({
    "cmd": "echo fresh", "exit": 0, "stdout": "STALEDATA\n", "stderr": "",
    "generation": "2", "duration_s": 0.5, "speculated_at": time.time()}))

daemon = subprocess.Popen(
    [sys.executable, SESSIOND, "--socket", sock, "--log-dir", str(logs),
     "--spec-cache", str(cache), "--no-login"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(50):
    if os.path.exists(sock):
        break
    time.sleep(0.1)
else:
    print(daemon.stdout.read())
    sys.exit("daemon never opened its socket")


def ask(cmd, cwd=CWD):
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.sendall((json.dumps({"cmd": cmd, "cwd": cwd}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf)


fails = []


def expect(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# A. full serve, ; joiners, mid failure continues (morning decision-1 shape)
r = ask("git diff --check; git status --short; python -m pytest tests/test_x.py -q")
expect("A full serve", r.get("cached") is True and r.get("prefix_served") == 3
       and r.get("prefix_total") == 3, json.dumps(r)[:200])
expect("A exit = last part's (1)", r.get("exit") == 1, str(r.get("exit")))
expect("A spliced stdout ordered",
       r.get("stdout") == " M itrs.py\n1 failed, 3 passed\n", repr(r.get("stdout"))[:120])
expect("A saved_s ~10.9", abs(r.get("spec_saved_s", 0) - 10.9) < 0.01,
       str(r.get("spec_saved_s")))

# B. && short-circuit: cached failure serves as final result, tail skipped
r = ask("python -m pytest tests/test_x.py -q -k 'not t1' && python -m compileall -q a.py b.py")
expect("B short-circuit full serve", r.get("cached") is True
       and r.get("prefix_served") == 1 and r.get("prefix_total") == 2,
       json.dumps(r)[:200])
expect("B exit 1, compileall never ran", r.get("exit") == 1
       and "compileall" not in r.get("stdout", ""), str(r.get("exit")))

# C. partial: 2 served + live remainder (pipe part ends prefix, runs live)
(ws / "f.txt").write_text("hello\nworld\n")
r = ask("git diff --check; git status --short; cat f.txt | wc -l")
expect("C partial serve 2/3", r.get("cached") is False
       and r.get("prefix_served") == 2 and r.get("prefix_total") == 3,
       json.dumps(r)[:200])
expect("C live tail executed in folded cwd",
       r.get("stdout", "").endswith("2\n") and r.get("stdout", "").startswith(" M itrs.py\n"),
       repr(r.get("stdout"))[:120])
expect("C exit from live tail", r.get("exit") == 0, str(r.get("exit")))

# D. cd-fold normalization: leading cd folds, single cached part serves
r = ask(f"cd {CWD} && python -m pytest tests/test_y.py -q", cwd="/")
expect("D cd-folded full serve", r.get("cached") is True
       and r.get("stdout") == "4 passed\n", json.dumps(r)[:200])

# E. heredoc untouched: refused by splitter, runs live, correct output
r = ask("python3 - <<'PY'\nprint(6*7)\nPY")
expect("E heredoc live", r.get("cached") is False and "prefix_served" not in r
       and r.get("stdout") == "42\n", json.dumps(r)[:200])

# F. stale-generation part must not serve (prefix ends, whole runs live)
r = ask("echo fresh && echo tail")
expect("F stale gen not served", "prefix_served" not in r
       and r.get("stdout") == "fresh\ntail\n"
       and "STALEDATA" not in r.get("stdout", ""), json.dumps(r)[:200])

# G. state command never served even if some fool cached it
seed("export FOO=1", CWD, 0, "", 0.1)
r = ask("export FOO=1 && echo v=$FOO")
expect("G export replayed live, state preserved", "prefix_served" not in r
       and r.get("stdout") == "v=1\n", json.dumps(r)[:200])

# H. simple command: prefix machinery stays out of the way
r = ask("echo plain")
expect("H simple cmd untouched", r.get("cached") is False
       and "prefix_served" not in r and r.get("stdout") == "plain\n",
       json.dumps(r)[:200])

# decisions log: prefix_serve records present with counts
# (D's cd-folded single-part serve is itself a 1/1 full prefix_serve)
dec = [json.loads(l) for l in open(cache / "serve_decisions.jsonl")]
pf = [d for d in dec if d.get("decision") == "prefix_serve"]
expect("I decisions logged", len(pf) == 4
       and {(d["parts_served"], d["parts_total"], d["full"]) for d in pf}
       == {(3, 3, True), (1, 2, True), (2, 3, False), (1, 1, True)},
       json.dumps(pf)[:300])

# stats: prefix_serves counted, full serves count as cache hits (A, B, D)
stats = json.loads(subprocess.run(  # one more socket round for stats
    [sys.executable, "-c", f"""
import socket, json
s = socket.socket(socket.AF_UNIX); s.connect({sock!r})
s.sendall((json.dumps({{"op": "stats"}}) + "\\n").encode())
print(s.recv(65536).decode())"""], capture_output=True, text=True).stdout)
expect("J stats", stats.get("prefix_serves") == 4 and stats.get("cache_hits") == 3,
       json.dumps(stats))

# teardown
try:
    s = socket.socket(socket.AF_UNIX); s.connect(sock)
    s.sendall((json.dumps({"op": "shutdown"}) + "\n").encode()); s.close()
except OSError:
    pass
daemon.wait(timeout=5)
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(0 if not fails else 1)
