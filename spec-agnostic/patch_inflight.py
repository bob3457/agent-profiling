#!/usr/bin/env python3
"""patch_inflight.py — in-flight join: when the agent asks for a command the
speculator is EXECUTING RIGHT NOW (same exact key), the daemon waits bounded
time for that run to finish and serves its result instead of duplicating the
execution. Deduplication, not throttling: expected wait <= fresh cost for an
identical command, no prediction confidence involved. Converts both measured
lost races (worker finished the byte-identical `sed` ~1s after the agent
asked) into serves. Idempotent; verbatim anchors; refuses on drift.

Mechanics:
  * writers (speculative_worker, edit_respec) drop {key}.inflight markers
    ({ts,pid,cmd}) before executing a cacheable command; markers are removed
    AFTER the entry lands (daemon sees entry first) and on every failure path.
  * daemon: spec_cache_lookup(wait_inflight=True) — top-level exact-key
    lookups only (prefix part-probes stay opportunistic) — polls up to
    SPEC_JOIN_MAX_WAIT (default 8s, env-tunable) when a fresh marker exists;
    markers older than SPEC_JOIN_MAX_AGE (300s) are ignored (crashed writer).
    Entry validation (generation/fingerprint) is unchanged after the join.
  * telemetry: joined_inflight(waited)/inflight_timeout(waited) decisions in
    serve_decisions.jsonl alongside the usual served record.

Run:  python3 patch_inflight.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")


def apply(path, name, old, new):
    src = path.read_text()
    if new in src:
        print(f"  = {name}: already applied")
        return
    assert old in src, f"ANCHOR DRIFT ({name}) in {path.name}: expected bytes not found"
    assert src.count(old) == 1, f"ANCHOR AMBIGUOUS ({name}) in {path.name}"
    path.write_text(src.replace(old, new))
    print(f"  + {name}")


# ============================================================ shell_sessiond
D = ROOT / "latency-opt/scripts/shell_sessiond.py"

apply(D, "join helper",
      "# ---------------------------------------------------------------- spec cache\n"
      "def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str, log: bool = True):",
      '''# ------------------------------------------------- spec cache: in-flight join
def _join_inflight(cache_dir: str, key: str) -> float:
    """If a speculator is executing this exact key right now (fresh
    {key}.inflight marker), wait bounded time for its entry. Returns seconds
    waited; caller re-checks entry existence and validates as usual."""
    m = Path(cache_dir) / f"{key}.inflight"
    p = Path(cache_dir) / f"{key}.json"
    try:
        info = json.loads(m.read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0
    max_age = float(os.environ.get("SPEC_JOIN_MAX_AGE", "300"))
    if time.time() - float(info.get("ts", 0)) > max_age:
        return 0.0                       # crashed/abandoned writer: ignore
    wait_max = float(os.environ.get("SPEC_JOIN_MAX_WAIT", "8"))
    t0 = time.time()
    while time.time() - t0 < wait_max:
        if p.exists():
            break
        if not m.exists():               # writer finished or died; brief grace
            time.sleep(0.05)             # for entry-write racing marker removal
            break
        time.sleep(0.05)
    return time.time() - t0


# ---------------------------------------------------------------- spec cache
def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str, log: bool = True,
                      wait_inflight: bool = False):''')

apply(D, "lookup loop with join",
      '''    for key in keys:
        p = Path(cache_dir) / f"{key}.json"
        if not p.exists():
            continue
        try:
            entry = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        reason = _spec_entry_invalid(cache_dir, entry, cwd)
        if reason:
            if log:
                _log_serve_decision(cache_dir, cmd, key, reason, entry)
            continue
        if log:
            _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry''',
      '''    waited = 0.0
    for key in keys:
        p = Path(cache_dir) / f"{key}.json"
        if not p.exists() and wait_inflight and key == keys[0]:
            waited = _join_inflight(cache_dir, key)
            if waited and not p.exists() and log:
                _log_serve_decision(cache_dir, cmd, key,
                                    f"inflight_timeout({waited:.2f}s)", None)
        if not p.exists():
            continue
        entry = None
        for _attempt in (0, 1):          # writer may be mid-write post-join
            try:
                entry = json.loads(p.read_text())
                break
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
        if entry is None:
            continue
        reason = _spec_entry_invalid(cache_dir, entry, cwd)
        if reason:
            if log:
                _log_serve_decision(cache_dir, cmd, key, reason, entry)
            continue
        if log:
            if waited and key == keys[0]:
                _log_serve_decision(cache_dir, cmd, key,
                                    f"joined_inflight({waited:.2f}s)", entry)
            _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry''')

apply(D, "handle waits",
      "        hit = spec_cache_lookup(self.args.spec_cache, cmd, cwd)",
      "        hit = spec_cache_lookup(self.args.spec_cache, cmd, cwd,\n"
      "                                wait_inflight=True)")

# ======================================================== speculative_worker
W = ROOT / "latency-opt/speculation/speculative_worker.py"

apply(W, "worker marker helpers",
      "# ------------------------------------------------------------------- actions",
      '''# ------------------------------------------------------------ in-flight marks
def _mark_inflight(cache_dir: Path, key: str, cmd: str):
    try:
        (cache_dir / f"{key}.inflight").write_text(
            json.dumps({"ts": time.time(), "pid": os.getpid(), "cmd": cmd[:200]}))
    except OSError:
        pass


def _clear_inflight(cache_dir: Path, key: str):
    try:
        (cache_dir / f"{key}.inflight").unlink()
    except OSError:
        pass


# ------------------------------------------------------------------- actions''')

apply(W, "worker marks before exec",
      '''        for cmd, cacheable in fn(ws, ctx):
            t0 = time.time()
            try:
                proc = subprocess.run(["bash", "-c", cmd], cwd=ws,
                                      capture_output=True, text=True,
                                      timeout=args.timeout_per_cmd)
            except subprocess.TimeoutExpired:
                print(f"[spec] TIMEOUT {cmd!r}")
                continue''',
      '''        for cmd, cacheable in fn(ws, ctx):
            ikey = None
            if cacheable == "family":
                from spec_families import family_key
                fk = family_key(cmd)
                ikey = f"fam_{fk}" if fk else None
            elif cacheable:
                ikey = cache_key(str(ws), cmd)
            if ikey:
                _mark_inflight(cache_dir, ikey, cmd)
            t0 = time.time()
            try:
                proc = subprocess.run(["bash", "-c", cmd], cwd=ws,
                                      capture_output=True, text=True,
                                      timeout=args.timeout_per_cmd)
            except subprocess.TimeoutExpired:
                print(f"[spec] TIMEOUT {cmd!r}")
                if ikey:
                    _clear_inflight(cache_dir, ikey)
                continue''')

apply(W, "worker clears after put",
      '            else:\n'
      '                print(f"[spec] warmed  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")',
      '            else:\n'
      '                print(f"[spec] warmed  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")\n'
      '            if ikey:\n'
      '                _clear_inflight(cache_dir, ikey)   # entry already on disk')

# ============================================================== edit_respec
R = ROOT / "latency-opt/speculation/edit_respec.py"

apply(R, "watcher marker helper + spawn marks",
      '''    def _spawn(cmd):
        pr = subprocess.Popen(["bash", "-lc", cmd], cwd=ws, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              preexec_fn=lambda: os.nice(10))
        pr._cmd, pr._t0 = cmd, time.time()
        CURRENT_PROCS.add(pr)
        return pr''',
      '''    def _inflight_path(cmd):
        k = hashlib.sha256(f"{ws}\\x00{cmd}".encode()).hexdigest()
        return cache_dir / f"{k}.inflight"

    def _spawn(cmd):
        try:
            _inflight_path(cmd).write_text(json.dumps(
                {"ts": time.time(), "pid": os.getpid(), "cmd": cmd[:200]}))
        except OSError:
            pass
        pr = subprocess.Popen(["bash", "-lc", cmd], cwd=ws, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              preexec_fn=lambda: os.nice(10))
        pr._cmd, pr._t0 = cmd, time.time()
        CURRENT_PROCS.add(pr)
        return pr''')

apply(R, "watcher clears on race-discard",
      '''        if read_generation(cache_dir) != gen:
            _log(f"gen {gen}: raced by newer edit, discarding {pr._cmd!r}")
            return False''',
      '''        if read_generation(cache_dir) != gen:
            _log(f"gen {gen}: raced by newer edit, discarding {pr._cmd!r}")
            try:
                _inflight_path(pr._cmd).unlink()
            except OSError:
                pass
            return False''')

apply(R, "watcher clears after store",
      "        done_at_gen.add((gen, pr._cmd))\n"
      "        _ledger_tier1(pr._cmd, gen)",
      "        done_at_gen.add((gen, pr._cmd))\n"
      "        try:\n"
      "            _inflight_path(pr._cmd).unlink()   # entry already on disk\n"
      "        except OSError:\n"
      "            pass\n"
      "        _ledger_tier1(pr._cmd, gen)")

print("done")
