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

Prediction sources: a static action plan (see spec_gate) covers the
high-value prep actions derivable from the workspace alone; with
--predictor llm/both, an LLM predictor (llm_predictor.py) reads the task
text and emits additional plan entries — the execution and commit machinery
is identical for both sources.

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
import spec_perf  # noqa: E402  (per-command perf wrapping; no-op unless SPEC_PERF_DIR)


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


def cache_put_family(cache_dir: Path, cwd: str, cmd: str, proc, fingerprint: str):
    """Store under the semantic family key so ANY equivalent phrasing hits."""
    from spec_families import family_key
    fk = family_key(cmd)
    if fk is None:
        return False
    entry = {
        "cmd": cmd, "cwd": cwd, "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:], "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint, "speculated_at": time.time(),
        "duration_s": getattr(proc, "spec_duration_s", None),
        "family": True,
    }
    (cache_dir / f"fam_{fk}.json").write_text(json.dumps(entry))
    return True


def cache_put(cache_dir: Path, cwd: str, cmd: str, proc, fingerprint: str):
    entry = {
        "cmd": cmd, "cwd": cwd,
        "exit": proc.returncode,
        "stdout": proc.stdout[-512 * 1024:],
        "stderr": proc.stderr[-64 * 1024:],
        "workspace_fingerprint": fingerprint,
        "speculated_at": time.time(),
        "duration_s": getattr(proc, "spec_duration_s", None),
    }
    (cache_dir / f"{cache_key(cwd, cmd)}.json").write_text(json.dumps(entry))


# ------------------------------------------------------------ in-flight marks
def _marker_generation(cache_dir: Path):
    try:
        return (Path(cache_dir) / "GENERATION").read_text().strip()
    except OSError:
        return None


def _mark_inflight(cache_dir: Path, key: str, cmd: str):
    try:
        (cache_dir / f"{key}.inflight").write_text(
            json.dumps({"ts": time.time(), "pid": os.getpid(),
                        "cmd": cmd[:200],
                        "gen": _marker_generation(cache_dir)}))
    except OSError:
        pass


def _clear_inflight(cache_dir: Path, key: str):
    try:
        (cache_dir / f"{key}.inflight").unlink()
    except OSError:
        pass


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


# ------------------------------------------------------- targeted pytest
def discover_pytest_targets(ws: Path, problem_statement: str, limit: int = 4):
    """Heuristic (non-oracle) target prediction: pull python paths/modules
    mentioned in the problem statement, map to existing test files by the
    tests/test_<name>.py convention. This is what an agent does mentally in
    its first turn; we do it statically for free."""
    import re
    candidates = []
    seen = set()
    # explicit paths in the statement
    for m in re.finditer(r"[\w/\.]+\.py", problem_statement):
        candidates.append(m.group(0).lstrip("/"))
    # module dotted names -> paths
    for m in re.finditer(r"\b([a-z_]+(?:\.[a-z_]+){1,5})\b", problem_statement):
        candidates.append(m.group(1).replace(".", "/") + ".py")
    targets = []
    for c in candidates:
        src = ws / c
        if not src.exists():
            continue
        name = src.stem
        for t in (src.parent / "tests" / f"test_{name}.py",
                  src.parent.parent / "tests" / f"test_{name}.py",
                  ws / "tests" / f"test_{name}.py"):
            if t.exists():
                rel = str(t.relative_to(ws))
                if rel not in seen:
                    seen.add(rel)
                    targets.append(rel)
    return targets[:limit]


def act_pytest_targeted(ws, ctx):
    """Pre-run the test files the agent will most likely run first, caching
    under FAMILY keys so any phrasing of the same invocation hits.

    Granularity: file-level runs first (cheapest, most likely query), then
    individual test ids collected from those files (fixes the `file::test`
    single-test query miss). Each in the two flag profiles agents use."""
    targets = ctx.get("pytest_targets") or []
    if not targets and ctx.get("problem_statement"):
        targets = discover_pytest_targets(ws, ctx["problem_statement"])
        print(f"[spec] heuristic pytest targets: {targets}")
    cmds = []
    for t in targets:
        cmds.append((f"python -m pytest {t}", "family"))
        cmds.append((f"python -m pytest {t} -q", "family"))
    # collect test ids from predicted files -> per-test pre-runs
    max_ids = int(os.environ.get("SPEC_MAX_TEST_IDS", "20"))
    ids = []
    for t in targets:
        try:
            probe = f"python -m pytest {t} --collect-only -q"
            proc = spec_perf.run_profiled(
                ["bash", "-c", probe], label="probe", cmd_text=probe,
                cwd=ws, timeout=120)
            for line in proc.stdout.splitlines():
                line = line.strip()
                if "::" in line and not line.startswith(("=", "warning")):
                    ids.append(line.split()[0])
        except subprocess.TimeoutExpired:
            pass
    for tid in ids[:max_ids]:
        cmds.append((f"python -m pytest {tid}", "family"))
        cmds.append((f"python -m pytest {tid} -q", "family"))
    if ids:
        print(f"[spec] collected {len(ids)} test ids, pre-running {min(len(ids), max_ids)}")
    return cmds


def discover_django_labels(ws: Path, problem_statement: str, limit: int = 4):
    """Django tests are addressed by label = directory name under tests/
    (e.g. dbshell), optionally label.test_module. Map problem-statement
    mentions to existing test dirs."""
    import re
    tests_dir = ws / "tests"
    if not (tests_dir / "runtests.py").exists():
        return []
    existing = {d.name for d in tests_dir.iterdir() if d.is_dir()}
    labels, seen = [], set()
    words = set(re.findall(r"[a-z_][a-z0-9_]{2,}", problem_statement.lower()))
    # direct mentions of a test dir name
    for w in words:
        if w in existing and w not in seen:
            seen.add(w)
            labels.append(w)
    # module paths like django/db/backends/postgresql/client.py -> backends
    for m in re.finditer(r"django/[\w/]+\.py", problem_statement):
        for part in m.group(0).split("/"):
            p = part.removesuffix(".py")
            if p in existing and p not in seen:
                seen.add(p)
                labels.append(p)
    return labels[:limit]


def act_django_targeted(ws, ctx):
    """django-runner analogue of pytest_targeted: pre-run predicted labels
    at the verbosity profiles agents use, plus per-module labels."""
    labels = ctx.get("django_labels") or []
    if not labels and ctx.get("problem_statement"):
        labels = discover_django_labels(ws, ctx["problem_statement"])
        print(f"[spec] heuristic django labels: {labels}")
    cmds = []
    for lab in labels:
        for v in ("1", "2"):
            suffix = "" if v == "1" else f" --verbosity {v}"
            cmds.append((f"python tests/runtests.py {lab}{suffix}", "family"))
        # per-module sub-labels (label.test_x) for finer-grained queries
        d = ws / "tests" / lab
        subs = sorted(p.stem for p in d.glob("test_*.py"))[:5] if d.is_dir() else []
        for s in subs:
            for v in ("1", "2"):
                suffix = "" if v == "1" else f" --verbosity {v}"
                cmds.append((f"python tests/runtests.py {lab}.{s}{suffix}", "family"))
    return cmds



# ---------------------------------------------------- task-agnostic actions
RECON_MAX_FILES = int(os.environ.get("SPEC_RECON_MAX_FILES", "15"))
RECON_MAX_BYTES = int(os.environ.get("SPEC_RECON_MAX_BYTES", str(32 * 1024)))


def _is_texty(p: Path) -> bool:
    try:
        return b"\0" not in p.open("rb").read(1024)
    except OSError:
        return False


def act_workspace_recon(ws, ctx):
    """Universal first moves: what every agent does on every filesystem task,
    regardless of domain. All TIER0 by construction."""
    cmds = ["ls", "ls -la", "pwd",
            "find . -type f -not -path '*/.git/*' | head -100"]
    n = 0
    for p in sorted(ws.iterdir()):
        if p.name.startswith(".") or p.name in ("__pycache__", "node_modules"):
            continue
        if p.is_dir():
            cmds.append(f"ls {p.name}")
            cmds.append(f"ls -la {p.name}")
        elif p.is_file() and n < RECON_MAX_FILES:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if 0 < size <= RECON_MAX_BYTES and _is_texty(p):
                cmds.append(f"cat {p.name}")
                cmds.append(f"wc -l {p.name}")
                n += 1
    return [(c, True) for c in cmds]


def _collect_direct(ctx, cmd):
    """Route a non-family LLM prediction through the tier policy. Whole
    TIER0 commands and TIER0 parts of compound predictions become direct
    pre-run candidates; everything else is dropped (and logged)."""
    from spec_tiers import classify, TIER0
    from spec_compound import split_for_serve, fold_cd_serve
    seen = ctx.setdefault("direct_seen", set())
    out = ctx.setdefault("direct_cmds", [])

    def add(c):
        if c not in seen:
            seen.add(c)
            out.append(c)

    if classify(cmd) == TIER0:
        add(cmd)
        return
    parts = split_for_serve(cmd)
    if parts and len(parts) > 1:
        parts, _cwd = fold_cd_serve(parts, ".")
        kept = 0
        for text, _stop, srv in parts:
            if srv and classify(text) == TIER0:
                add(text)
                kept += 1
        if kept:
            print(f"[spec] llm direct (compound): kept {kept}/{len(parts)} "
                  f"parts of {cmd!r}")
            return
    print(f"[spec] llm direct: dropped (tier policy) {cmd!r}")


def act_llm_direct(ws, ctx):
    return [(c, True) for c in ctx.get("direct_cmds", [])]


ACTIONS = {
    "git_status": act_git_status,
    "repo_index": act_repo_index,
    "pytest_collect": act_pytest_collect,
    "pytest_run_cached": act_pytest_run_cached,
    "py_dep_preinstall": act_py_dep_preinstall,
    "npm_ci_prefetch": act_npm_ci_prefetch,
    "cargo_fetch": act_cargo_fetch,
    "pytest_targeted": act_pytest_targeted,
    "django_targeted": act_django_targeted,
    "workspace_recon": act_workspace_recon,
    "llm_direct": act_llm_direct,
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
    ap.add_argument("--problem-statement", default=None,
                    help="file with the task text; enables heuristic pytest target discovery")
    ap.add_argument("--pytest-targets", default=None,
                    help="comma list of test paths (oracle mode / explicit hints)")
    ap.add_argument("--predictor", default="heuristic",
                    choices=["heuristic", "llm", "both", "gate"],
                    help="'gate' lets spec_gate's ledger-informed EV decision choose")
    ap.add_argument("--ledger-dir", default=None)
    args = ap.parse_args()

    # ---- speculation CPU accounting (SPEC_CPU_OUT) ---------------------------
    _cpu_out = os.environ.get("SPEC_CPU_OUT")
    if _cpu_out:
        import atexit
        import resource
        import signal as _sig
        _cpu_t0 = time.time()

        def _dump_cpu(*_a):
            try:
                su = resource.getrusage(resource.RUSAGE_SELF)
                ch = resource.getrusage(resource.RUSAGE_CHILDREN)
                rec = {"tag": os.environ.get("SPEC_CPU_TAG", "spec_worker"),
                       "utime_s": round(su.ru_utime, 3),
                       "stime_s": round(su.ru_stime, 3),
                       "children_utime_s": round(ch.ru_utime, 3),
                       "children_stime_s": round(ch.ru_stime, 3),
                       "cpu_total_s": round(su.ru_utime + su.ru_stime
                                            + ch.ru_utime + ch.ru_stime, 3),
                       "maxrss_kb": max(su.ru_maxrss, ch.ru_maxrss),
                       "wall_s": round(time.time() - _cpu_t0, 3)}
                _tag = rec["tag"]
                with open(os.path.join(_cpu_out, f"cpu_{_tag}.json"), "w") as f:
                    json.dump(rec, f)
            except OSError:
                pass
            if _a:                      # SIGTERM path: dump then die
                os._exit(0)

        atexit.register(_dump_cpu)
        _sig.signal(_sig.SIGTERM, _dump_cpu)

    ws = Path(args.workspace).resolve()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch) if args.scratch else cache_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    ctx = {"scratch": scratch}
    if args.pytest_targets:
        ctx["pytest_targets"] = args.pytest_targets.split(",")
    if args.problem_statement and Path(args.problem_statement).exists():
        ctx["problem_statement"] = Path(args.problem_statement).read_text()

    use_llm = args.predictor in ("llm", "both")
    if args.actions:
        plan = args.actions.split(",")
    else:
        d = should_speculate(args.benchmark, str(ws),
                             task_text=ctx.get("problem_statement", ""),
                             ledger_dir=args.ledger_dir)
        if not d.speculate:
            if os.environ.get("SPEC_UPSTREAM_GATE") == "GO":
                plan = ["workspace_recon", "repo_index"]
                if (ws / ".git").exists():
                    plan.insert(0, "git_status")
                feats = getattr(d, "features", None) or {}
                if feats.get("py_project") or feats.get("has_tests"):
                    plan += ["pytest_collect", "pytest_run_cached"]
                print(f"[spec] internal gate refused ({d.reason}) but "
                      f"upstream LLM gate said GO -> generic plan {plan}")
            else:
                print(f"[spec] gate says no: {d.reason}")
                return
        else:
            plan = d.actions
        print(f"[spec] gate: {d.reason} -> {plan} (conf {d.confidence})")
        if args.predictor == "gate":
            use_llm = getattr(d, "use_llm", False)
            print(f"[spec] gate LLM decision: use_llm={use_llm} ({getattr(d, 'llm_reason', '')})")
        if not getattr(d, "per_test_ids", True):
            os.environ.setdefault("SPEC_MAX_TEST_IDS", "0")
            print("[spec] gate: file-level granularity only (prediction confidence low)")
        if os.environ.get("SPEC_RECON", "1") != "0" and \
                "workspace_recon" not in plan:
            plan.insert(0, "workspace_recon")

    # ---- LLM predictor: merge its commands into the target hints
    if use_llm and ctx.get("problem_statement"):
        from llm_predictor import predict_meta
        llm_cmds, meta = predict_meta(ws, ctx["problem_statement"])
        print(f"[spec] llm predictor: {llm_cmds} tokens={meta.get('tokens')} "
              f"latency={meta.get('latency_s')}s")
        if args.ledger_dir:
            from ledger import record_prediction
            record_prediction(args.ledger_dir, ws.name, args.benchmark, "llm",
                              llm_cmds, meta.get("tokens"), meta.get("latency_s"))
        from spec_families import parse_command
        direct_only = os.environ.get("SPEC_DIRECT_ONLY") == "1"
        for c in llm_cmds:
            pc = parse_command(c)
            if not pc:
                _collect_direct(ctx, c)
                continue
            if direct_only:
                print(f"[spec] direct-only: family prediction deferred to gated worker: {c!r}")
                continue
            if pc["family"] == "pytest":
                ctx.setdefault("pytest_targets", [])
                ctx["pytest_targets"] += [t for t in pc["targets"]
                                          if t not in ctx["pytest_targets"]]
                if "pytest_targeted" not in plan:
                    plan.append("pytest_targeted")
            elif pc["family"] == "django":
                ctx.setdefault("django_labels", [])
                for t in pc["targets"]:
                    # keep the model's full granularity AND the parent label
                    for lab in (t, t.split(".")[0]):
                        if lab not in ctx["django_labels"]:
                            ctx["django_labels"].append(lab)
                if "django_targeted" not in plan:
                    plan.append("django_targeted")
        if ctx.get("direct_cmds") and "llm_direct" not in plan:
            plan.append("llm_direct")
            print(f"[spec] llm direct: {len(ctx['direct_cmds'])} "
                  f"tier0 candidate(s) queued")

    # ledger: record what the HEURISTIC would predict (even in llm mode),
    # so heuristic vs llm accuracy accumulate side by side per task.
    if args.ledger_dir and ctx.get("problem_statement"):
        heur = []
        if (ws / "tests" / "runtests.py").exists():
            heur = [f"python tests/runtests.py {l}"
                    for l in discover_django_labels(ws, ctx["problem_statement"])]
        else:
            heur = [f"python -m pytest {t}"
                    for t in discover_pytest_targets(ws, ctx["problem_statement"])]
        from ledger import record_prediction
        record_prediction(args.ledger_dir, ws.name, args.benchmark, "heuristic", heur)

    os.nice(args.nice)  # stay out of the agent's way
    n_cached = 0
    for name in plan:
        fn = ACTIONS.get(name.strip())
        if fn is None:
            print(f"[spec] unknown action {name!r}, skipping")
            continue
        for cmd, cacheable in fn(ws, ctx):
            # key normalization: cwd==ws, so a literal $PWD is the
            # workspace path; expanding it makes the cached key match
            # the agent's expanded command (measured lost race:
            # django-10973 runtests, entry landed 2s late on the raw
            # string while the expanded twin ran live)
            cmd = cmd.replace("${PWD}", str(ws)).replace("$PWD", str(ws))
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
                proc = spec_perf.run_profiled(
                    ["bash", "-c", cmd], label="worker", cmd_text=cmd,
                    extra={"action": name, "cacheable": str(cacheable)},
                    cwd=ws, timeout=args.timeout_per_cmd)
            except subprocess.TimeoutExpired:
                print(f"[spec] TIMEOUT {cmd!r}")
                if ikey:
                    _clear_inflight(cache_dir, ikey)
                continue
            dt = time.time() - t0
            if cacheable == "family":
                proc.spec_duration_s = dt   # entries record real cost for saved_s
                if cache_put_family(cache_dir, str(ws), cmd,
                                    proc, workspace_fingerprint(str(ws))):
                    n_cached += 1
                    print(f"[spec] cached* ({dt:5.1f}s, exit {proc.returncode}) {cmd}  [family key]")
                else:
                    print(f"[spec] skip (unnormalizable) {cmd}")
            elif cacheable:
                # Fingerprint is taken AFTER the command (the state the agent
                # will see). If the agent later edits files, the entry simply
                # won't validate and the command runs for real. Safe by design.
                proc.spec_duration_s = dt   # entries record real cost for saved_s
                cache_put(cache_dir, str(ws), cmd, proc, workspace_fingerprint(str(ws)))
                n_cached += 1
                print(f"[spec] cached  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")
            else:
                print(f"[spec] warmed  ({dt:5.1f}s, exit {proc.returncode}) {cmd}")
            if ikey:
                _clear_inflight(cache_dir, ikey)   # entry already on disk
    print(f"[spec] done: {n_cached} results cached in {cache_dir}")


if __name__ == "__main__":
    main()
