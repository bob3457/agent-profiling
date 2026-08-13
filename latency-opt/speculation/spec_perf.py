#!/usr/bin/env python3
"""spec_perf.py — per-command CPU/memory profiling for SPECULATIVE executions.

The daemon path (shell_sessiond --perf-events) already attributes PMU counters
to the agent's REAL commands. This module closes the other half: every command
the speculation side runs (worker plan actions, collect-only probes, respec
re-runs) gets wrapped in

    /usr/bin/time -v -o <dir>/time_v.txt \
        perf stat -x, -o <dir>/perf_stat.csv -e <events> -- <argv...>

writing one directory per command in the SAME schema as
scripts/codex_tool_perf_wrap.sh (command.txt, argv.json, perf_stat.csv,
time_v.txt, metadata.json), so the existing analyze_cpu_deepdive tooling can
consume speculative and real tool_perf dirs identically.

Activation: set SPEC_PERF_DIR=/path/to/run/spec_perf. Unset => every helper
is a strict passthrough to subprocess (zero behavior change, zero overhead).

Events: SPEC_PERF_EVENTS, else PERF_EVENTS (Tejas's wrapper env), else the
generic default below. On the GH200's Neoverse cores override with the ARM
PMU names if the generic cache aliases report <not supported>.

Transparency guarantees the callers rely on:
  * stdout/stderr of the wrapped command are untouched (perf and time both
    write to files via -o), so cache entries serve byte-identical output.
  * exit code of the command propagates through both wrappers.
  * counting mode only (perf stat, no sampling): overhead is one extra
    fork+exec per command, negligible against the >=1s commands that matter.
"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_EVENTS = ("task-clock,cycles,instructions,cache-references,"
                  "cache-misses,branches,context-switches,cpu-migrations,"
                  "page-faults")

_lock = threading.Lock()
_seq = 0
_avail = None            # (perf_ok: bool, time_ok: bool) after first probe


def enabled() -> bool:
    return bool(os.environ.get("SPEC_PERF_DIR"))


def events() -> str:
    return (os.environ.get("SPEC_PERF_EVENTS")
            or os.environ.get("PERF_EVENTS")
            or DEFAULT_EVENTS)


def _availability():
    """Probe once: is perf usable with the configured events, is GNU time
    present. Degrades gracefully (perf-only, or full passthrough)."""
    global _avail
    with _lock:
        if _avail is not None:
            return _avail
        perf_ok = False
        if shutil.which("perf"):
            try:
                r = subprocess.run(
                    ["perf", "stat", "-x", ",", "-o", "/dev/null",
                     "-e", events(), "--", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10)
                perf_ok = (r.returncode == 0)
            except (OSError, subprocess.TimeoutExpired):
                perf_ok = False
        time_ok = os.access("/usr/bin/time", os.X_OK)
        _avail = (perf_ok, time_ok)
        if not perf_ok:
            print("[spec-perf] perf unusable with events "
                  f"{events()!r}; falling back "
                  + ("to /usr/bin/time only" if time_ok else "to passthrough"))
        return _avail


def _new_dir(label: str) -> Path:
    """Allocate the per-command output directory: spec_<label>_<seq>_<pid>.
    Same character set as codex_tool_perf_wrap.sh's SAFE_TOOL_ID."""
    global _seq
    root = Path(os.environ["SPEC_PERF_DIR"])
    with _lock:
        _seq += 1
        n = _seq
    tool_id = f"spec_{label}_{n:04d}_{os.getpid()}"
    d = root / tool_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def wrap_argv(argv, label: str, cmd_text: str = ""):
    """Return (wrapped_argv, out_dir|None). Passthrough when disabled or
    when neither perf nor GNU time is usable. Writes command.txt/argv.json
    up front (present even if the command later times out)."""
    if not enabled():
        return list(argv), None
    perf_ok, time_ok = _availability()
    if not perf_ok and not time_ok:
        return list(argv), None
    out = _new_dir(label)
    (out / "command.txt").write_text((cmd_text or " ".join(argv)) + "\n")
    (out / "argv.json").write_text(json.dumps({"argv": list(argv)}, indent=2))
    wrapped = []
    if time_ok:
        wrapped += ["/usr/bin/time", "-v", "-o", str(out / "time_v.txt")]
    if perf_ok:
        wrapped += ["perf", "stat", "-x", ",", "-o",
                    str(out / "perf_stat.csv"), "-e", events(), "--"]
    return wrapped + list(argv), out


def finalize(out_dir, returncode, start_ns, end_ns, label: str,
             cmd_text: str = "", extra: dict = None):
    """Write metadata.json (superset of codex_tool_perf_wrap.sh's keys)."""
    if out_dir is None:
        return
    meta = {
        "tool_id": Path(out_dir).name,
        "start_ns": int(start_ns),
        "end_ns": int(end_ns),
        "wall_ms": (int(end_ns) - int(start_ns)) / 1e6,
        "returncode": returncode,
        "perf_events": events(),
        "spec_source": label,
        "command": cmd_text,
    }
    if extra:
        meta.update(extra)
    try:
        (Path(out_dir) / "metadata.json").write_text(json.dumps(meta, indent=2))
    except OSError:
        pass


def run_profiled(argv, label: str, cmd_text: str = "", extra: dict = None,
                 **kw):
    """Drop-in for subprocess.run(argv, capture_output=True, text=True, ...)
    at speculative spawn points.

    Semantics preserved for callers:
      * returns CompletedProcess with args=ORIGINAL argv
      * raises subprocess.TimeoutExpired on timeout, like subprocess.run
    Improvement over plain subprocess.run: the command runs in its own
    session, and on timeout the WHOLE process group is killed (plain run
    would kill only the wrapper/bash and orphan e.g. pytest workers).
    """
    timeout = kw.pop("timeout", None)
    preexec = kw.pop("preexec_fn", None)
    wrapped, out = wrap_argv(argv, label, cmd_text)

    def _pre():
        os.setsid()
        if preexec is not None:
            preexec()

    start_ns = time.time_ns()
    proc = subprocess.Popen(wrapped, preexec_fn=_pre,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=kw.pop("text", True), cwd=kw.pop("cwd", None))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        finalize(out, None, start_ns, time.time_ns(), label, cmd_text,
                 dict(extra or {}, timeout=True, timeout_s=timeout))
        raise subprocess.TimeoutExpired(cmd_text or argv, timeout)
    end_ns = time.time_ns()
    finalize(out, proc.returncode, start_ns, end_ns, label, cmd_text, extra)
    return subprocess.CompletedProcess(list(argv), proc.returncode,
                                       stdout, stderr)
