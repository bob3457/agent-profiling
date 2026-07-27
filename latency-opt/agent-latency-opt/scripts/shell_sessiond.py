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
import signal
import socket
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
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own pgid: timeout-kill nukes whole tree
            text=True,
        )
        self.created_at = time.time()
        self.commands_run = 0
        self.log(f"session[{self.key}] new bash pid={self.proc.pid} login={login}")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, cmd: str, cwd: str, timeout: float, reset_cwd: bool = True):
        """Execute cmd in the persistent shell. Returns result dict."""
        with self.lock:
            rid = uuid.uuid4().hex[:12]
            out_f = os.path.join(self.ctl_dir, f"{rid}.out")
            err_f = os.path.join(self.ctl_dir, f"{rid}.err")
            cpu0 = proc_cpu_seconds(self.proc.pid)
            t0 = time.time()

            # Brace group (NOT a subshell) so exports/cd/functions persist.
            # cd reset keeps stock-codex semantics: every call starts at the
            # tool-call cwd, exactly like a fresh process would.
            prologue = f"cd {shq(cwd)} 2>/dev/null; " if reset_cwd else ""
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


# ---------------------------------------------------------------- spec cache
def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str):
    """Speculation cache: results pre-computed by speculative_worker.py.
    Entry is valid only if the workspace fingerprint recorded at speculation
    time still matches (worker only caches read-only commands, but we
    double-check)."""
    if not cache_dir:
        return None
    key = hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()
    p = Path(cache_dir) / f"{key}.json"
    if not p.exists():
        return None
    try:
        entry = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    fp = entry.get("workspace_fingerprint")
    if fp and fp != workspace_fingerprint(cwd):
        return None  # workspace changed since speculation; stale
    return entry


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
        hit = spec_cache_lookup(self.args.spec_cache, cmd, cwd)
        if hit is not None:
            self.stats["cache_hits"] += 1
            self.stats["commands"] += 1
            res = {"exit": hit["exit"], "stdout": hit["stdout"],
                   "stderr": hit.get("stderr", ""), "cached": True,
                   "session_reused": False, "wall_s": 0.0,
                   "cpu_s": 0.0}
            self._record(cmd, cwd, res)
            return res

        session, reused = self.get_session(key, bool(req.get("fresh")))
        res = session.run(cmd, cwd, timeout,
                          reset_cwd=not self.args.persist_cwd)
        if res.pop("session_dead", False):
            with self.sessions_lock:
                if self.sessions.get(key) is session:
                    del self.sessions[key]
        res["cached"] = False
        res["session_reused"] = reused
        self.stats["commands"] += 1
        self._record(cmd, cwd, res)
        return res

    def _record(self, cmd, cwd, res):
        self.jsonl.write(json.dumps({
            "ts": time.time(), "cmd": cmd, "cwd": cwd,
            "exit": res["exit"], "wall_s": res.get("wall_s"),
            "cpu_s": res.get("cpu_s"), "cached": res["cached"],
            "session_reused": res["session_reused"],
        }) + "\n")

    def _shutdown(self):
        time.sleep(0.2)
        with self.sessions_lock:
            for s in self.sessions.values():
                s.close()
        os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--log-dir", default=os.environ.get("SESSIOND_LOG_DIR", "/tmp/shelld_logs"))
    ap.add_argument("--spec-cache", default=os.environ.get("SESSIOND_SPEC_CACHE", ""))
    ap.add_argument("--default-timeout", type=float, default=3600)
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
