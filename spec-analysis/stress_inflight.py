#!/usr/bin/env python3
"""stress_inflight.py — behavioral probes for the in-flight join, past smoke.

smoke_inflight.py proves the mechanism works once; this asks how it behaves
under the conditions a live sweep will actually produce:

  S1 thundering herd   6 concurrent clients on one marker -> all join & serve
  S2 join-then-stale   entry lands mid-wait with a stale generation -> the
                       wait must NOT convert into a serve; live run instead
  S3 prefix no-wait    compound whose first part is only a marker -> request
                       returns fast (part probes never join)
  S4 timeout fallthru  marker never resolves -> bounded wait, live run,
                       correct output, inflight_timeout logged
  S5 net-negative join entry (dur 0.05s) lands after ~1.2s wait -> serve
                       happens, and decompose_serves flags net<0
  S6 accounting gap    joined serve writes wall_s=0.0 in commands.jsonl while
                       the decision log carries the real wait -> decomposer's
                       joined_waited recovers it
  S7 decomposer xcheck decompose_serves --check over the dirs this suite
                       produced: pairing, stats reconciliation, categories

Run on a patched tree:  python3 stress_inflight.py [repo_root]
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import hashlib
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
SESSIOND = ROOT / "latency-opt/scripts/shell_sessiond.py"
DECOMPOSE = Path(__file__).resolve().parent / "decompose_serves.py"
PY = sys.executable
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def key_for(cwd, cmd):
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


def fingerprint(cwd):
    # mirror sessiond's workspace_fingerprint by importing it
    import importlib.util
    spec = importlib.util.spec_from_file_location("sessiond", SESSIOND)
    sd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sd)
    return sd.workspace_fingerprint(cwd)


def write_entry(cache, cwd, cmd, out="OUT\n", dur=1.0, gen=None, fp=None):
    e = {"cmd": cmd, "cwd": cwd, "exit": 0, "stdout": out, "stderr": "",
         "workspace_fingerprint": fp if fp is not None else fingerprint(cwd),
         "speculated_at": time.time(), "duration_s": dur}
    if gen is not None:
        e["generation"] = gen
    (Path(cache) / f"{key_for(cwd, cmd)}.json").write_text(json.dumps(e))


class Daemon:
    """One sessiond in a synthetic run_dir shaped like the harness layout."""

    def __init__(self, run_dir: Path, env=None):
        self.run_dir = run_dir
        self.cache = run_dir / "spec_cache"
        self.logs = run_dir / "shelld_logs"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sock = str(run_dir / "sock")
        e = dict(os.environ)
        e.update(env or {})
        self.proc = subprocess.Popen(
            [PY, str(SESSIOND), "--socket", self.sock,
             "--log-dir", str(self.logs), "--spec-cache", str(self.cache)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=e)
        for _ in range(50):
            if os.path.exists(self.sock):
                return
            time.sleep(0.1)
        raise RuntimeError("daemon did not come up")

    def ask(self, req, timeout=30):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(self.sock)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf)

    def decisions(self):
        f = self.cache / "serve_decisions.jsonl"
        return [json.loads(l) for l in f.open()] if f.exists() else []

    def stop(self):
        try:
            stats = self.ask({"op": "stats"})
            (self.run_dir / "daemon_stats.txt").write_text(
                json.dumps(stats) + "\n")
            self.ask({"op": "shutdown"})
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


base = Path(tempfile.mkdtemp(prefix="stress_inflight."))
keep_dirs = []

# ---- S1 thundering herd ------------------------------------------------------
rd = base / "tb" / "s1_herd"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "f.txt").write_text("x\n")
d = Daemon(rd)
cwd, cmd = str(ws), "cat f.txt"
k = key_for(cwd, cmd)
(d.cache / f"{k}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))
threading.Timer(1.0, lambda: (write_entry(d.cache, cwd, cmd, "HERD\n", dur=3.0),
                              (d.cache / f"{k}.inflight").unlink())).start()
results, errs = [], []


def client():
    try:
        results.append(d.ask({"cmd": cmd, "cwd": cwd}))
    except Exception as e:  # noqa: BLE001
        errs.append(str(e))


t0 = time.time()
threads = [threading.Thread(target=client) for _ in range(6)]
[t.start() for t in threads]
[t.join(20) for t in threads]
dt = time.time() - t0
check("S1 all 6 clients replied", len(results) == 6 and not errs,
      f"{len(results)} ok, errs={errs}")
check("S1 all served from cache",
      all(r.get("cached") is True and r.get("stdout") == "HERD\n"
          for r in results),
      json.dumps(results)[:200])
check("S1 herd resolved in one wait window", dt < 5.0, f"{dt:.2f}s")
stats = d.ask({"op": "stats"})
check("S1 stats cache_hits==6", stats.get("cache_hits") == 6, str(stats))
joins = [x for x in d.decisions()
         if x["decision"].startswith("joined_inflight")]
check("S1 every client logged a join", len(joins) == 6, str(len(joins)))
d.stop()
keep_dirs.append(rd)

# ---- S2 join-then-stale generation -------------------------------------------
rd = base / "tb" / "s2_stale"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "g.txt").write_text("live\n")
d = Daemon(rd)
cwd, cmd = str(ws), "cat g.txt"
k = key_for(cwd, cmd)
(d.cache / "GENERATION").write_text("5")          # current generation
(d.cache / f"{k}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))
threading.Timer(0.8, lambda: (write_entry(d.cache, cwd, cmd, "STALE\n",
                                          dur=9.0, gen="1"),
                              (d.cache / f"{k}.inflight").unlink())).start()
res = d.ask({"cmd": cmd, "cwd": cwd})
check("S2 stale entry NOT served", res.get("cached") is False,
      json.dumps(res)[:200])
check("S2 live output correct", res.get("stdout") == "live\n",
      repr(res.get("stdout")))
dec = [x["decision"] for x in d.decisions()]
check("S2 stale_generation logged",
      any(x.startswith("stale_generation") for x in dec), str(dec))
d.stop()
keep_dirs.append(rd)

# ---- S3 prefix part-probes never wait -----------------------------------------
rd = base / "tb" / "s3_prefix"
ws = rd / "ws"; ws.mkdir(parents=True)
d = Daemon(rd)
cwd = str(ws)
part1, part2 = "echo one", "echo two"
compound = f"{part1} && {part2}"
# marker on part1 only; no entry ever arrives
(d.cache / f"{key_for(cwd, part1)}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1}))
t0 = time.time()
res = d.ask({"cmd": compound, "cwd": cwd})
dt = time.time() - t0
check("S3 compound ran live", res.get("cached") is False
      and "one" in res.get("stdout", "") and "two" in res.get("stdout", ""),
      json.dumps(res)[:200])
check("S3 no join wait on part probe", dt < 2.0, f"{dt:.2f}s")
d.stop()

# ---- S4 timeout fallthrough ----------------------------------------------------
rd = base / "tb" / "s4_timeout"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "h.txt").write_text("fall\n")
d = Daemon(rd, env={"SPEC_JOIN_MAX_WAIT": "1"})
cwd, cmd = str(ws), "cat h.txt"
(d.cache / f"{key_for(cwd, cmd)}.inflight").write_text(
    json.dumps({"ts": time.time(), "pid": 1}))
t0 = time.time()
res = d.ask({"cmd": cmd, "cwd": cwd})
dt = time.time() - t0
check("S4 fell through to live run", res.get("cached") is False
      and res.get("stdout") == "fall\n", json.dumps(res)[:200])
check("S4 wait bounded by SPEC_JOIN_MAX_WAIT", 0.9 < dt < 3.5, f"{dt:.2f}s")
check("S4 inflight_timeout logged",
      any(x["decision"].startswith("inflight_timeout")
          for x in d.decisions()), str([x["decision"] for x in d.decisions()]))
cmds = [json.loads(l) for l in (d.logs / "commands.jsonl").open()]
check("S4 live wall_s > 0", cmds and (cmds[-1].get("wall_s") or 0) > 0,
      json.dumps(cmds[-1] if cmds else {}))
d.stop()
keep_dirs.append(rd)

# ---- S5 net-negative join ------------------------------------------------------
rd = base / "tb" / "s5_negjoin"
ws = rd / "ws"; ws.mkdir(parents=True); (ws / "i.txt").write_text("z\n")
d = Daemon(rd)
cwd, cmd = str(ws), "cat i.txt"
k = key_for(cwd, cmd)
(d.cache / f"{k}.inflight").write_text(json.dumps({"ts": time.time(), "pid": 1}))
threading.Timer(1.2, lambda: (write_entry(d.cache, cwd, cmd, "NEG\n", dur=0.05),
                              (d.cache / f"{k}.inflight").unlink())).start()
res = d.ask({"cmd": cmd, "cwd": cwd})
check("S5 join served", res.get("cached") is True
      and res.get("stdout") == "NEG\n", json.dumps(res)[:200])
d.stop()
keep_dirs.append(rd)

# ---- S6 + S7: decomposer over everything this suite produced ------------------
r = subprocess.run([PY, str(DECOMPOSE), str(base), "--jsonl"],
                   capture_output=True, text=True)
rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
by = {Path(x["dir"]).name: x for x in rows}
check("S7 decomposer parsed all dirs",
      {"s1_herd", "s2_stale", "s4_timeout", "s5_negjoin"} <= set(by),
      str(sorted(by)))
if "s1_herd" in by:
    h = by["s1_herd"]
    check("S7 herd: 6 joined, 0 exact",
          h["joined"] == 6 and h["exact"] == 0,
          f"joined={h['joined']} exact={h['exact']}")
    check("S7 herd: stats reconcile (no warnings)",
          not h["warnings"], str(h["warnings"]))
if "s5_negjoin" in by:
    n = by["s5_negjoin"]
    check("S5/S7 net-negative join flagged",
          n["joined_negative"] == 1 and n["joined_saved"] < 0,
          f"neg={n['joined_negative']} saved={n['joined_saved']}")
    check("S6 accounting gap recovered: waited_s from decisions",
          n["joined_waited"] > 1.0, f"waited={n['joined_waited']}")
    cmds = [json.loads(l)
            for l in (base / "tb/s5_negjoin/shelld_logs/commands.jsonl").open()]
    check("S6 commands.jsonl still says wall_s=0 for the join",
          cmds and cmds[-1]["cached"] and (cmds[-1]["wall_s"] or 0) == 0,
          json.dumps(cmds[-1] if cmds else {}))
if "s4_timeout" in by:
    t = by["s4_timeout"]
    check("S7 timeout waste accounted",
          t["timeouts"] == 1 and t["timeout_wasted"] > 0.9,
          f"tmo={t['timeouts']} wasted={t['timeout_wasted']}")
if "s2_stale" in by:
    s = by["s2_stale"]
    check("S7 stale miss categorized, zero serves",
          s["misses"]["stale_generation"] >= 1 and s["serves"] == 0,
          json.dumps(s["misses"]))

print()
print(f"synthetic run dirs kept under: {base}")
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
