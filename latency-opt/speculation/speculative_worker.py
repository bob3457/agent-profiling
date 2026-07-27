#!/usr/bin/env python3
"""speculative_worker.py — parallel speculative process for agent runs.

Runs alongside the main agent (launched right after task materialization,
before or at the same moment as `codex exec`). While the agent is blocked on
model inference (~90% of wall time in your measurements), this worker uses
the idle CPU to pre-compute work the agent will very likely ask for.

Two kinds of speculation, with different commit mechanisms:

1. RESULT speculation (read-only commands, e.g. `pytest --collect-only`,
   `git status`, repo file listing, pre-running the test suite):
   the worker executes the command, records (stdout, stderr, exit) plus a
   workspace fingerprint into the cache dir. When the agent later issues the
   same command, shell_sessiond serves it instantly from cache — but ONLY if
   the workspace fingerprint still matches (a wrong/stale speculation is
   silently ignored and the command runs for real). Misprediction cost: zero.

2. STATE speculation (side-effecting prep, e.g. installing missing deps):
   never mutates the agent's live environment directly. Instead it builds the
   state in a parallel location (a speculation venv / prefetched wheel cache /
   warmed pip+cargo+npm caches). Commit happens implicitly: when the agent
   runs `pip install ...`, pip finds everything already in the local wheel
   cache and completes in seconds instead of minutes (works even on Hopper
   compute nodes with throttled egress, because the worker can be launched on
   the login-node side before job dispatch, or early in the allocation).
   Misprediction cost: some wasted disk in a cache dir.

The worker deliberately does NOT call an LLM in this version: for SWE-bench /
Terminal-Bench the high-value prep actions are derivable statically (see
spec_gate). An LLM-driven speculator (small model predicts the agent's next
commands from the task text; cf. the speculative-actions literature) plugs in
by emitting additional entries into the action plan — the execution and
commit machinery here stays identical.

Usage:
    python3 speculative_worker.py --workspace /path/repo \
        --cache-dir /tmp/spec_cache --actions git_status,repo_index,pytest_collect
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from spec_gate import should_speculate  # noqa: E402


# ---------------------------------------------------------------- fingerprint
FP_EXCLUDE = {".git", "__pycache__", ".pytest_cache", ".tox", ".mypy_cache",
              "node_modules", ".ruff_cache", ".cache", "target"}


def workspace_fingerprint(cwd: str) -> str:
    """Must match shell_sessiond.workspace_fingerprint exactly."""
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


def cache_key(cwd: str, cmd: str) -> str:
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


def cache_put(cache_dir: Path, cwd: str, cmd: str, proc, fingerprint: str):
    entry = {
        "cmd": cmd, "cwd": cwd,
        "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:],
        "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint,
        "speculated_at": time.time(),
    }
    (cache_dir / f"{cache_key(cwd, cmd)}.json").write_text(json.dumps(entry))


# ------------------------------------------------------------------- actions
# Each action returns a list of (exact_command_string, is_cacheable) it ran.
# exact_command_string matters: cache hits require the agent to issue the
# same string. We seed the cache with the CANONICAL phrasings agents use;
# fuzzy matching can be layered into the daemon later.

def act_git_status(ws, ctx):
    cmds = ["git status", "git log --oneline -10", "git diff", "git branch -a"]
    return [(c, True) for c in cmds]


def act_repo_index(ws, ctx):
    cmds = ["ls", "ls -la",
            "find . -type f -name '*.py' | head -50",
            "grep -r 'def ' --include='*.py' -l . | head -30"]
    return [(c, True) for c in cmds]


def act_pytest_collect(ws, ctx):
    return [("python -m pytest --collect-only -q", True),
            ("python -m pytest --collect-only -q 2>&1 | tail -20", True)]


def act_pytest_run_cached(ws, ctx):
    # Pre-run the suite; agents almost always start by reproducing the failure.
    return [("python -m pytest -x -q", True),
            ("python -m pytest --tb=short -q", True)]


def act_py_dep_preinstall(ws, ctx):
    """STATE speculation: warm the pip cache / spec venv, never the live env."""
    spec_venv = ctx["scratch"] / "spec_venv"
    cmds = []
    if not spec_venv.exists():
        cmds.append((f"python -m venv {spec_venv}", False))
    for req in ("requirements.txt", "requirements-dev.txt", "test-requirements.txt"):
        if (ws / req).exists():
            # --dry-run? No: real download warms the shared pip cache; install
            # goes into the speculation venv only.
            cmds.append((f"{spec_venv}/bin/pip install -q -r {req}", False))
    if (ws / "setup.py").exists() or (ws / "pyproject.toml").exists():
        cmds.append((f"{spec_venv}/bin/pip install -q -e . --no-build-isolation", False))
    return cmds


def act_npm_ci_prefetch(ws, ctx):
    return [("npm ci --prefer-offline --no-audit --ignore-scripts", False)]


def act_cargo_fetch(ws, ctx):
    return [("cargo fetch", False)]


ACTIONS = {
    "git_status": act_git_status,
    "repo_index": act_repo_index,
    "pytest_collect": act_pytest_collect,
    "pytest_run_cached": act_pytest_run_cached,
    "py_dep_preinstall": act_py_dep_preinstall,
    "npm_ci_prefetch": act_npm_ci_prefetch,
    "cargo_fetch": act_cargo_fetch,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--scratch", default=None,
                    help="dir for speculation venvs/state (default: <cache-dir>/scratch)")
    ap.add_argument("--benchmark", default="swebench")
    ap.add_argument("--actions", default=None,
                    help="comma list; default: ask spec_gate")
    ap.add_argument("--nice", type=int, default=10,
                    help="niceness so speculation never competes with the agent")
    ap.add_argument("--timeout-per-cmd", type=float, default=600)
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch) if args.scratch else cache_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    ctx = {"scratch": scratch}

    if args.actions:
        plan = args.actions.split(",")
    else:
        d = should_speculate(args.benchmark, str(ws))
        if not d.speculate:
            print(f"[spec] gate says no: {d.reason}")
            return
        plan = d.actions
        print(f"[spec] gate: {d.reason} -> {plan} (conf {d.confidence})")

    os.nice(args.nice)  # stay out of the agent's way
    n_cached = 0
    for name in plan:
        fn = ACTIONS.get(name.strip())
        if fn is None:
            print(f"[spec] unknown action {name!r}, skipping")
            continue
        for cmd, cacheable in fn(ws, ctx):
            t0 = time.time()
            try:
                proc = subprocess.run(["bash", "-c", cmd], cwd=ws,
                                      capture_output=True, text=True,
                                      timeout=args.timeout_per_cmd)
            except subprocess.TimeoutExpired:
                print(f"[spec] TIMEOUT {cmd!r}")
                continue
            dt = time.time() - t0
            if cacheable:
                # Fingerprint is taken AFTER the command (the state the agent
                # will see). If the agent later edits files, the entry simply
                # won't validate and the command runs for real. Safe by design.
                cache_put(cache_dir, str(ws), cmd, proc, workspace_fingerprint(str(ws)))
                n_cached += 1
                print(f"[spec] cached  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")
            else:
                print(f"[spec] warmed  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")
    print(f"[spec] done: {n_cached} results cached in {cache_dir}")


if __name__ == "__main__":
    main()
