#!/usr/bin/env python3
"""shell_sessiond.py — persistent shell session daemon for Codex tool calls.

Problem it solves
-----------------
Stock Codex spawns a brand-new `bash -lc "<cmd>"` process for every shell tool
call. On an HPC login environment (module system, conda init, big profiles)
that means the login-shell setup cost is paid on EVERY command, and any
`export`/`source venv/bin/activate` the agent does is lost on the next call.

This daemon keeps ONE long-lived bash per session key. Commands are routed to
it over a Unix socket. Environment mutations (exports, conda/venv activation,
functions) persist across tool calls. A new shell is created only when:
  - no live session exists for the key,
  - the previous session died (crash, OOM, kill),
  - the previous command was killed on timeout (session is reset for safety),
  - the client explicitly requests a fresh session (CODEX_SHELL_FRESH=1).

Per-command it also records wall time and CPU time (utime+stime+cutime+cstime
deltas from /proc/<bash>/stat, which includes reaped children) to
$SESSIOND_LOG_DIR/commands.jsonl — a drop-in replacement for the per-command
attribution you lose when the perf wrapper no longer wraps a fresh process.

Speculation hook (optimization 2): before executing, the daemon consults an
optional result cache directory (SESSIOND_SPEC_CACHE). If a speculative worker
has already executed this exact command against an unchanged workspace, the
cached result is returned instantly.

Protocol (JSON lines over unix socket)
--------------------------------------
request:  {"cmd": "<shell command>", "cwd": "/abs/path", "key": "default",
           "fresh": false, "timeout": 3600}
response: {"exit": 0, "stdout": "...", "stderr": "...", "cached": false,
           "session_reused": true, "wall_s": 0.01, "cpu_s": 0.002}

Run:  python3 shell_sessiond.py --socket /tmp/codex_shelld.$UID/sock
Stop: send {"op": "shutdown"} or SIGTERM.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")


def proc_cpu_seconds(pid: int) -> float:
    """utime+stime+cutime+cstime for pid, in seconds. Includes reaped children."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        # fields[11..14] are utime, stime, cutime, cstime (0-indexed after comm)
        return sum(int(fields[i]) for i in (11, 12, 13, 14)) / CLK_TCK
    except (OSError, IndexError, ValueError):
        return 0.0


class ShellSession:
    """One persistent bash. Commands run via eval in the same shell process,
    so env mutations persist. Output is captured via per-command temp files;
    completion is signalled through a control FIFO."""

    def __init__(self, key: str, login: bool = True, log=None):
        self.key = key
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.ctl_dir = tempfile.mkdtemp(prefix="shelld_")
        self.ctl_fifo = os.path.join(self.ctl_dir, "ctl")
        os.mkfifo(self.ctl_fifo)
        args = ["bash"]
        if login:
            args.append("-l")  # pay the login/profile cost ONCE per session
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(self.ctl_dir, "session_bash.stderr"), "w"),
            start_new_session=True,  # own pgid: timeout-kill nukes whole tree
            text=True,
        )
        self.created_at = time.time()
        self.commands_run = 0
        self.log(f"session[{self.key}] new bash pid={self.proc.pid} login={login}")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, cmd: str, cwd: str, timeout: float, reset_cwd: bool = True,
            perf_events: str = "", perf_out: str = ""):
        """Execute cmd in the persistent shell. Returns result dict.

        If perf_events is set, a `perf stat -p <session bash>` is attached for
        the duration of the command (counting mode; children forked after
        attach are inherited), writing a CSV to perf_out. This restores the
        per-command PMU attribution that the fresh-process perf wrapper
        provides in the stock configuration, at comparable overhead (one perf
        process per command)."""
        with self.lock:
            rid = uuid.uuid4().hex[:12]
            out_f = os.path.join(self.ctl_dir, f"{rid}.out")
            err_f = os.path.join(self.ctl_dir, f"{rid}.err")
            perf_proc = None
            if perf_events and perf_out:
                try:
                    perf_proc = subprocess.Popen(
                        ["perf", "stat", "-e", perf_events, "-x", ",",
                         "-o", perf_out, "-p", str(self.proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(0.005)  # let the attach land before work starts
                except (OSError, FileNotFoundError):
                    perf_proc = None
            cpu0 = proc_cpu_seconds(self.proc.pid)
            t0 = time.time()

            # Brace group (NOT a subshell) so exports/cd/functions persist.
            # cd reset keeps stock-codex semantics: every call starts at the
            # tool-call cwd, exactly like a fresh process would.
            # Reset shell options each command: mirrors stock fresh-process
            # semantics. Codex's shell_snapshot validation runs `set -e` as a
            # command; in a persistent session that would otherwise leak and
            # kill the shell on the next ordinary failure.
            prologue = "set +eu +o pipefail; "
            if reset_cwd:
                prologue += f"cd {shq(cwd)} 2>/dev/null; "
            # The brace group runs in the CURRENT shell (so exports/activation
            # persist). If the command calls `exit N`, that exits the persistent
            # bash itself — the EXIT trap still delivers N to the control fifo,
            # the daemon notices the session died and transparently creates a
            # fresh one on the next call. Stock semantics preserved.
            script = (
                f"trap 'printf %s $? > {shq(self.ctl_fifo)}' EXIT\n"
                "{ " + prologue + cmd + "\n} "
                f"> {shq(out_f)} 2> {shq(err_f)} < /dev/null; "
                f"__rc=$?; trap - EXIT; printf '%s' $__rc > {shq(self.ctl_fifo)}\n"
            )
            try:
                self.proc.stdin.write(script)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return {"exit": 127, "stdout": "", "stderr": "session shell died",
                        "session_dead": True}

            # Wait for completion sentinel on the control fifo, with timeout.
            status = self._wait_ctl(timeout)
            wall = time.time() - t0
            cpu = max(0.0, proc_cpu_seconds(self.proc.pid) - cpu0)
            if perf_proc is not None:
                try:
                    perf_proc.send_signal(signal.SIGINT)
                    perf_proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    perf_proc.kill()

            if status is None:  # timeout -> kill whole session group
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.log(f"session[{self.key}] TIMEOUT after {timeout}s; session killed")
                return {"exit": 124, "stdout": read_trunc(out_f),
                        "stderr": read_trunc(err_f) + "\n[shelld: timeout, session reset]",
                        "wall_s": wall, "cpu_s": cpu, "session_dead": True}

            self.commands_run += 1
            res = {"exit": int(status), "stdout": read_trunc(out_f),
                   "stderr": read_trunc(err_f), "wall_s": wall, "cpu_s": cpu}
            if not self.alive():  # command called `exit` / `exec` and killed the shell
                res["session_dead"] = True
            return res

    def _wait_ctl(self, timeout: float):
        """Blocking read of the control fifo with timeout. Returns status str or None."""
        result = {}

        def reader():
            try:
                with open(self.ctl_fifo) as f:
                    result["status"] = f.read()
            except OSError:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None
        return result.get("status", "127").strip() or "127"

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        # a reader thread may still be blocked in open() on the control fifo
        # (timeout path: no writer will ever arrive); a momentary non-blocking
        # write-open releases it so the thread can exit
        try:
            fd = os.open(self.ctl_fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        shutil.rmtree(self.ctl_dir, ignore_errors=True)


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


MAX_CAPTURE = 512 * 1024  # keep parity with codex output truncation ballpark


def read_trunc(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CAPTURE + 1)
        txt = data[:MAX_CAPTURE].decode("utf-8", "replace")
        if len(data) > MAX_CAPTURE:
            txt += "\n[shelld: output truncated]"
        return txt
    except OSError:
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ------------------------------------------------- spec cache: serve policy
def _current_generation(cache_dir: str):
    try:
        return (Path(cache_dir) / "GENERATION").read_text().strip()
    except OSError:
        return None


def _spec_entry_invalid(cache_dir: str, entry: dict, cwd: str):
    """Return None if servable, else a reason string.

    Generation-stamped entries (edit_respec.py) validate against the
    GENERATION file: O(1) read, sees in-place edits at any depth because the
    watcher's deep scan owns detection. Legacy entries fall back to the
    top-2-level fingerprint (blind below depth 2, oversensitive to top-level
    churn -- kept only for compatibility with pre-edit worker entries)."""
    gen = entry.get("generation")
    if gen is not None:
        cur = _current_generation(cache_dir)
        if cur is None:
            return "generation_file_missing"
        return None if gen == cur else f"stale_generation({gen}!={cur})"
    fp = entry.get("workspace_fingerprint")
    if fp and fp != workspace_fingerprint(cwd):
        return "stale_fingerprint"
    return None


def _log_serve_decision(cache_dir: str, cmd: str, key, decision: str, entry,
                        waited_s=None):
    try:
        rec = {"ts": time.time(), "cmd": cmd[:300],
               "key": ("exact" if key and not str(key).startswith("fam_")
                       else key), "decision": decision}
        if waited_s:                     # join wait spent before this outcome
            rec["waited_s"] = round(waited_s, 3)
        if entry is not None:
            rec["entry_cmd"] = entry.get("cmd", "")[:300]
            rec["entry_exit"] = entry.get("exit")
            rec["entry_gen"] = entry.get("generation")
            rec["entry_age_s"] = round(
                time.time() - entry.get("speculated_at", time.time()), 1)
            rec["entry_dur_s"] = entry.get("duration_s")
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _log_prefix_decision(cache_dir, cmd, n, total, saved_s, full,
                         stop_reason=None, stop_part=None):
    try:
        rec = {"ts": time.time(), "cmd": cmd[:300],
               "decision": "prefix_serve", "parts_served": n,
               "parts_total": total, "saved_s": round(saved_s, 3),
               "full": full}
        if stop_reason:
            rec["stop_reason"] = stop_reason
            rec["stop_part"] = stop_part
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _prefix_try_serve(cache_dir: str, cmd: str, cwd: str):
    """Serve a leading run of a compound's parts from the spec cache.

    Returns None (nothing servable; caller proceeds unchanged) or a dict:
      remainder: None if fully served, else the re-joined live command
      cwd:       effective cwd after leading-cd folding (live cmd runs here)
      stdout/stderr: concatenated served output (prefix of the reply)
      exit:      compound exit code when fully served, else None
      n, total, saved_s: telemetry
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                              / "speculation"))
        from spec_compound import (split_for_serve, fold_cd_serve, rejoin,
                                   is_state_cmd, is_cd_cmd)
    except ImportError:
        return None
    split = split_for_serve(cmd)
    if not split:
        return None
    parts, eff_cwd = fold_cd_serve(split, cwd)
    if not parts:
        return None                       # pure-cd compound: run live
    folded = len(parts) < len(split) or eff_cwd != cwd
    if len(parts) < 2 and not folded:
        return None                       # simple command: normal path
    out, err = [], []
    saved, n, last_exit = 0.0, 0, 0
    total = len(parts)
    stop_reason, stop_part = None, None
    for i, (text, stop_on_fail, servable) in enumerate(parts):
        if not servable or is_state_cmd(text) or is_cd_cmd(text):
            stop_reason = ("hazard" if not servable else
                           "state_cmd" if is_state_cmd(text) else "cd_cmd")
            stop_part = text[:120]
            break                         # ends the servable prefix
        entry = spec_cache_lookup(cache_dir, text, eff_cwd, log=False)
        if entry is None:
            k = hashlib.sha256(f"{eff_cwd}\x00{text}".encode()).hexdigest()
            stop_reason = ("invalid_entry"          # exists, failed validation
                           if (Path(cache_dir) / f"{k}.json").exists()
                           else "no_entry")
            stop_part = text[:120]
            break
        ex = entry.get("exit", 1)
        if ex != 0 and stop_on_fail and i < total - 1:
            # `&&` after a failed part: total short-circuit only if every
            # later joiner is `&&` too; otherwise end the prefix BEFORE the
            # failure and let bash run the whole tail live (correct, unsaved)
            if all(s for _, s, _ in parts[i:total - 1]):
                out.append(entry.get("stdout", ""))
                err.append(entry.get("stderr", ""))
                saved += entry.get("duration_s") or 0.0
                return {"remainder": None, "cwd": eff_cwd,
                        "stdout": "".join(out), "stderr": "".join(err),
                        "exit": ex, "n": i + 1, "total": total,
                        "saved_s": saved}
            break
        out.append(entry.get("stdout", ""))
        err.append(entry.get("stderr", ""))
        saved += entry.get("duration_s") or 0.0
        n, last_exit = i + 1, ex
    if n == 0:
        try:                              # attempt died on part 0: say why
            with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": time.time(), "cmd": cmd[:300],
                    "decision": "prefix_attempt", "parts_total": total,
                    "parts_served": 0, "stop_reason": stop_reason,
                    "stop_part": stop_part}) + "\n")
        except OSError:
            pass
        return None
    if n == total:
        return {"remainder": None, "cwd": eff_cwd, "stdout": "".join(out),
                "stderr": "".join(err), "exit": last_exit, "n": n,
                "total": total, "saved_s": saved,
                "stop_reason": stop_reason, "stop_part": stop_part}
    return {"remainder": rejoin(parts[n:]), "cwd": eff_cwd,
            "stdout": "".join(out), "stderr": "".join(err), "exit": None,
            "n": n, "total": total, "saved_s": saved,
            "stop_reason": stop_reason, "stop_part": stop_part}


# ------------------------------------------------- spec cache: in-flight join
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
    mgen = info.get("gen")
    if mgen is not None:                 # doomed join: the writer started
        cur = _current_generation(cache_dir)   # under an older generation;
        if cur is not None and str(mgen) != str(cur):   # its entry cannot
            return 0.0                   # validate -- waiting is pure waste
    wait_max = float(os.environ.get("SPEC_JOIN_MAX_WAIT", "2"))
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
                      wait_inflight: bool = False):
    """Speculation cache lookup: exact string first, then semantic family key
    (see speculation/spec_families.py). Entry is valid only if the workspace
    fingerprint recorded at speculation time still matches."""
    if not cache_dir:
        return None
    keys = [hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speculation"))
        from spec_families import family_key
        fk = family_key(cmd)
        if fk:
            keys.append(f"fam_{fk}")
    except ImportError:
        pass
    waited = 0.0
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
                # a join wait that resolved into an entry that then failed
                # validation is pure waste; carry it on the miss record so
                # the decisions log stays the ground truth for wait time
                _log_serve_decision(cache_dir, cmd, key, reason, entry,
                                    waited_s=(waited if key == keys[0]
                                              else None))
            continue
        if log:
            if waited and key == keys[0]:
                _log_serve_decision(cache_dir, cmd, key,
                                    f"joined_inflight({waited:.2f}s)", entry)
            _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry
    if log and (len(keys) > 1 or "pytest" in cmd or "runtests.py" in cmd):
        # family-keyed OR test-looking => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)
    return None


FP_EXCLUDE = {".git", "__pycache__", ".pytest_cache", ".tox", ".mypy_cache",
              "node_modules", ".ruff_cache", ".cache", "target"}


def workspace_fingerprint(cwd: str) -> str:
    """Cheap staleness check: max mtime_ns + file count over top 2 levels,
    ignoring volatile tool caches (which speculation itself may create)."""
    latest, count = 0, 0
    root = Path(cwd)

    def keep(p: Path) -> bool:
        return p.name not in FP_EXCLUDE

    try:
        top = [p for p in root.iterdir() if keep(p)]
        for p in top + [q for d in top if d.is_dir() for q in d.iterdir() if keep(q)]:
            try:
                st = p.stat()
                latest = max(latest, st.st_mtime_ns)
                count += 1
            except OSError:
                pass
    except OSError:
        pass
    return f"{latest}:{count}"



def spec_near_miss(cache_dir: str, cmd: str, cwd: str):
    """After a serve-miss, score how CLOSE speculation got. Returns
    {score, matched_entry_cmd} or None. Never serves — feeds telemetry (and,
    later, the EV gate/ledger). Score: Jaccard over comparable target units,
    file-level credit for `file::test` vs `file` (0.8), family-only overlap
    floor (0.2). The near-missed command still executes for real — in the
    speculation-WARMED persistent session, which is where the residual saving
    lives (imports compiled, caches hot)."""
    if not cache_dir:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speculation"))
        from spec_families import parse_command
    except ImportError:
        return None
    q = parse_command(cmd)
    if q is None or not q["targets"]:
        return None
    q_files = {t.split("::")[0] for t in q["targets"]}
    best = None
    for pth in Path(cache_dir).glob("fam_*.json"):
        try:
            entry = json.loads(pth.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        e = parse_command(entry.get("cmd", ""))
        if e is None or e["family"] != q["family"]:
            continue
        et, qt = set(e["targets"]), set(q["targets"])
        if et & qt:
            score = len(et & qt) / len(et | qt)
        else:
            e_files = {tt.split("::")[0] for tt in e["targets"]}
            score = 0.8 if (e_files & q_files) else 0.2
        if best is None or score > best["score"]:
            best = {"score": round(score, 3), "matched_entry_cmd": entry.get("cmd")}
    return best if best and best["score"] > 0 else None


# ------------------------------------------------------------------- server
class Daemon:
    def __init__(self, args):
        self.args = args
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.log_dir = Path(args.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = open(self.log_dir / "commands.jsonl", "a", buffering=1)
        self.stats = {"commands": 0, "sessions_created": 0, "cache_hits": 0}
        self.perf_seq = 0
        self.perf_dir = self.log_dir / "tool_perf"
        if args.perf_events:
            self.perf_dir.mkdir(parents=True, exist_ok=True)

    def log(self, msg):
        print(f"[shelld {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    def get_session(self, key: str, fresh: bool):
        with self.sessions_lock:
            s = self.sessions.get(key)
            reused = True
            if fresh or s is None or not s.alive():
                if s is not None:
                    s.close()
                s = ShellSession(key, login=not self.args.no_login, log=self.log)
                self.sessions[key] = s
                self.stats["sessions_created"] += 1
                reused = False
            return s, reused

    def handle(self, req: dict) -> dict:
        if req.get("op") == "shutdown":
            threading.Thread(target=self._shutdown, daemon=True).start()
            return {"ok": True}
        if req.get("op") == "stats":
            return self.stats

        cmd = req["cmd"]
        cwd = req.get("cwd") or os.getcwd()
        key = req.get("key", "default")
        timeout = float(req.get("timeout", self.args.default_timeout))

        # speculation cache first
        hit = spec_cache_lookup(self.args.spec_cache, cmd, cwd,
                                wait_inflight=True)
        if hit is not None:
            self.stats["cache_hits"] += 1
            self.stats["commands"] += 1
            res = {"exit": hit["exit"], "stdout": hit["stdout"],
                   "stderr": hit.get("stderr", ""), "cached": True,
                   "session_reused": False, "wall_s": 0.0,
                   "cpu_s": 0.0}
            self._record(cmd, cwd, res)
            return res

        # compound prefix-serve: serve leading cached parts, run the rest live
        live_cmd, live_cwd = cmd, cwd
        pre = None
        if self.args.spec_cache and not self.args.persist_cwd:
            pre = _prefix_try_serve(self.args.spec_cache, cmd, cwd)
        if pre is not None and pre["remainder"] is None:   # fully served
            self.stats["cache_hits"] += 1
            self.stats["prefix_serves"] = self.stats.get("prefix_serves", 0) + 1
            self.stats["commands"] += 1
            res = {"exit": pre["exit"], "stdout": pre["stdout"],
                   "stderr": pre["stderr"], "cached": True,
                   "session_reused": False, "wall_s": 0.0, "cpu_s": 0.0,
                   "prefix_served": pre["n"], "prefix_total": pre["total"],
                   "spec_saved_s": round(pre["saved_s"], 3)}
            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=True,
                                 stop_reason=pre.get("stop_reason"),
                                 stop_part=pre.get("stop_part"))
            self._record(cmd, cwd, res)
            return res
        if pre is not None:                                # partial prefix
            live_cmd, live_cwd = pre["remainder"], pre["cwd"]

        near = spec_near_miss(self.args.spec_cache, cmd, cwd)
        if near:
            self.stats["near_misses"] = self.stats.get("near_misses", 0) + 1

        session, reused = self.get_session(key, bool(req.get("fresh")))
        perf_out = ""
        if self.args.perf_events:
            self.perf_seq += 1
            perf_out = str(self.perf_dir / f"cmd_{self.perf_seq:04d}.csv")
        res = session.run(live_cmd, live_cwd, timeout,
                          reset_cwd=not self.args.persist_cwd,
                          perf_events=self.args.perf_events,
                          perf_out=perf_out)
        if perf_out and os.path.exists(perf_out):
            res["perf_csv"] = perf_out
        if res.pop("session_dead", False):
            with self.sessions_lock:
                if self.sessions.get(key) is session:
                    del self.sessions[key]
            session.close()              # reap fifo reader + temp dir
        res["cached"] = False
        res["session_reused"] = reused
        if pre is not None:                # splice served prefix into reply
            res["stdout"] = pre["stdout"] + res.get("stdout", "")
            res["stderr"] = pre["stderr"] + res.get("stderr", "")
            res["prefix_served"] = pre["n"]
            res["prefix_total"] = pre["total"]
            res["spec_saved_s"] = round(pre["saved_s"], 3)
            self.stats["prefix_serves"] = self.stats.get("prefix_serves", 0) + 1
            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=False,
                                 stop_reason=pre.get("stop_reason"),
                                 stop_part=pre.get("stop_part"))
        if near:
            res["near_miss"] = near
        self.stats["commands"] += 1
        self._record(cmd, cwd, res)
        return res

    def _record(self, cmd, cwd, res):
        self.jsonl.write(json.dumps({
            "ts": time.time(), "cmd": cmd, "cwd": cwd,
            "exit": res["exit"], "wall_s": res.get("wall_s"),
            "cpu_s": res.get("cpu_s"), "cached": res["cached"],
            "session_reused": res["session_reused"],
            "perf_csv": res.get("perf_csv"),
            "near_miss": res.get("near_miss"),
        }) + "\n")

    def _shutdown(self):
        time.sleep(0.2)
        with self.sessions_lock:
            for s in self.sessions.values():
                s.close()
        try:
            os.unlink(self.args.socket)
        except OSError:
            pass
        os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--log-dir", default=os.environ.get("SESSIOND_LOG_DIR", "/tmp/shelld_logs"))
    ap.add_argument("--spec-cache", default=os.environ.get("SESSIOND_SPEC_CACHE", ""))
    ap.add_argument("--default-timeout", type=float, default=3600)
    ap.add_argument("--perf-events", default=os.environ.get("SESSIOND_PERF_EVENTS", ""),
                    help="if set, attach 'perf stat -p <session>' per command, CSVs to <log-dir>/tool_perf/")
    ap.add_argument("--no-login", action="store_true",
                    help="skip -l on session creation (faster, no profile)")
    ap.add_argument("--persist-cwd", action="store_true",
                    help="let `cd` persist across tool calls (diverges from stock codex semantics)")
    args = ap.parse_args()

    sock_path = Path(args.socket)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    daemon = Daemon(args)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                line = self.rfile.readline()
                if not line:
                    return
                req = json.loads(line)
                res = daemon.handle(req)
                self.wfile.write((json.dumps(res) + "\n").encode())
            except Exception as e:  # noqa: BLE001
                try:
                    self.wfile.write((json.dumps(
                        {"exit": 125, "stdout": "", "stderr": f"shelld error: {e}",
                         "cached": False, "session_reused": False}) + "\n").encode())
                except OSError:
                    pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    srv = Server(str(sock_path), Handler)
    os.chmod(sock_path, 0o700)
    signal.signal(signal.SIGTERM, lambda *_: daemon._shutdown())
    daemon.log(f"listening on {sock_path} (spec_cache={args.spec_cache or 'off'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
