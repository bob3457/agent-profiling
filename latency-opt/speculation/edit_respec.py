#!/usr/bin/env python3
"""Trajectory-aware speculation watcher (build 7).

Two speculation modes in one process, per the Speculative Actions framing
(draft signal = the agent's own live trajectory):

  PRE-EDIT (v7 revived): while the tree is unedited, tail the agent's
  executed commands (shelld commands.jsonl) and its event stream
  (stdout.jsonl, reasoning text leaks test paths before execution). New
  test-like candidates are pre-run at the CURRENT generation -- correctly
  scoped: servable exactly until the first edit.

  POST-EDIT (v8): on any workspace edit, bump GENERATION first, then re-run
  the candidate union against the patched tree, trajectory-first. The
  agent's own pre-edit probe is the top candidate -- agents re-verify what
  they probed, so this is a near-guaranteed key match post-edit.

WHY THIS EXISTS
Start-of-task pre-runs are structurally stale on edit-first benchmarks: the
agent patches the tree before it ever runs a test, so every pre-edit cached
result is (correctly) rejected by the staleness gate. Observed on
astropy-13398: worker stored the exact predicted command within ~60s, agent
queried it 6 minutes later, post-patch -> miss, score 1.0. The window that
actually exists is edit -> verify (~12s observed) against a 1.6-2.8s test
cost. This watcher owns that window.

WHAT IT DOES
  1. Baseline: deep recursive mtime scan of the workspace (any depth,
     excluding volatile tool caches). Unlike the top-2-level fingerprint in
     shell_sessiond, this SEES in-place content edits below depth 2 and is
     NOT tripped by top-level side-effect churn alone (churn dirs excluded).
  2. Publishes a GENERATION token file in the cache dir. The daemon (after
     patch_sessiond.py) validates generation-stamped entries against this
     file in O(1) -- no scanning on the serve path.
  3. On workspace change: bump GENERATION FIRST (instantly invalidating the
     old generation -- no unsafe window), then re-run the predictor's
     candidate commands against the patched tree and store results stamped
     with the new generation. A re-run that races a newer edit discards its
     result. After --max-generations (default 5) the watcher keeps bumping
     (invalidation is free) but stops executing (cost isn't).

LAUNCH
Exactly like speculative_worker.py -- same interpreter, same env, same
container context -- right after the worker launch line:

  python3 "$ROOT/latency-opt/speculation/edit_respec.py" \
      --workspace "$workdir" --cache-dir "$CODEX_SHELLD_SPEC" \
      --spec-log "$run_dir/spec.log" >> "$run_dir/respec.log" 2>&1 &
  RESPEC_PID=$!

and make sure the harness teardown kills it (same pkill that should already
be reaping the worker -- this is also the fuse-overlayfs timeout fix).
patch_harness.py does both automatically.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

# Superset of the daemon's FP_EXCLUDE: also ignore artifacts our own re-runs
# (and the agent's runs) create, so re-run side effects don't retrigger us.
EXCLUDE = {
    ".git", "__pycache__", ".pytest_cache", ".tox", ".mypy_cache",
    "node_modules", ".ruff_cache", ".cache", "target",
    ".hypothesis", ".eggs", "build", "dist", ".coverage",
}

STDIO_CAP = 262144          # chars kept per stream in a cached entry
CMD_TIMEOUT = 120           # s per candidate re-run
MAX_CANDIDATES_PER_GEN = 6  # variants executed per generation
MAX_IDLE_RUNS = 8           # total pre-edit (idle) speculative runs
DJANGO_RE = re.compile(r"^python3? tests/runtests\.py [A-Za-z0-9_. ]+$")
# test-path tokens leaked in reasoning/message text, e.g.
#   astropy/coordinates/tests/test_x.py::test_y
STREAM_PATH_RE = re.compile(
    r"[A-Za-z0-9_./-]*test[A-Za-z0-9_./-]*\.py(?:::[A-Za-z0-9_\[\]-]+)?")

def _log(msg):
    print(f"[respec {time.strftime('%H:%M:%S')}] {msg}", flush=True)


STOP = False
CURRENT_PROC = None  # in-flight candidate re-run, killed on SIGTERM


def _sigterm(_sig, _frm):
    global STOP
    STOP = True
    p = CURRENT_PROC
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass


# ------------------------------------------------------------ deep fingerprint
def deep_fingerprint(root: str) -> str:
    """Recursive max-mtime_ns + file count, any depth, EXCLUDE-pruned.
    Sees in-place edits the top-2-level scan is blind to. Runs off the serve
    path (watcher only), so a few hundred ms under fuse-overlayfs is fine."""
    latest, count = 0, 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name in EXCLUDE:
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    latest = max(latest, st.st_mtime_ns)
                    count += 1
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            pass
    return f"{latest}:{count}"


# ------------------------------------------------------------------ candidates
PRED_RE = re.compile(r"\[spec\] llm predictor: (\[.*\])")


def parse_candidates(spec_log: str):
    """Pull the LLM predictor's candidate list straight out of the worker's
    spec.log (last occurrence wins -- the freshest prediction). Zero extra
    tokens: we reuse the prediction the worker already paid for."""
    try:
        text = Path(spec_log).read_text(errors="replace")
    except OSError:
        return []
    cands = []
    for m in PRED_RE.finditer(text):
        try:
            got = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(got, list):
            cands = [c for c in got if isinstance(c, str)]
    return cands


def expand_variants(cands):
    """File-level variants first (that's what agents actually emit in the
    edit->verify window), then seed BOTH -q and plain phrasings of each
    pytest candidate so exact-key lookups hit for either spelling.
    Family keys make phrasing mostly moot, but exact keys are free."""
    def is_pytest(c):
        toks = shlex.split(c) if c else []
        return "pytest" in toks or any(t.endswith("pytest") for t in toks)

    def has_file_target(c):
        return bool(re.search(r"\S+\.py(::\S+)?", c))

    ordered = sorted(cands, key=lambda c: (not has_file_target(c),))
    out, seen = [], set()
    for c in ordered:
        variants = [c]
        if is_pytest(c):
            if re.search(r"(^|\s)-q(\s|$)", c):
                variants.append(re.sub(r"(^|\s)-q(?=\s|$)", "", c).strip())
            else:
                variants.append(c.rstrip() + " -q")
        for v in variants:
            v = re.sub(r"\s+", " ", v).strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _family_key(cmd: str):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from spec_families import family_key
        return family_key(cmd)
    except ImportError:
        return None


# ----------------------------------------------------------------- trajectory
def is_testlike(cmd: str) -> bool:
    """Conservative: only commands we can safely and usefully pre-run."""
    if any(ch in cmd for ch in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return False
    return _family_key(cmd) is not None or bool(DJANGO_RE.match(cmd.strip()))


def file_level(cmd: str) -> str:
    """pytest node-level -> file-level sibling candidate."""
    return re.sub(r"(\.py)::\S+", r"\1", cmd)


class Trajectory:
    """Incremental readers for the agent's leaked signals."""

    def __init__(self, commands_log, agent_stream):
        self.commands_log = commands_log
        self.agent_stream = agent_stream
        self.cmd_lines = 0
        self.stream_off = 0
        self.seen = set()
        self.order = []   # insertion-ordered; newest = strongest post-edit

    def _new_agent_cmds(self):
        if not self.commands_log:
            return []
        out = []
        try:
            lines = Path(self.commands_log).read_text(errors="replace")                                            .splitlines()
        except OSError:
            return []
        for ln in lines[self.cmd_lines:]:
            try:
                cmd = json.loads(ln).get("cmd", "")
            except (json.JSONDecodeError, AttributeError):
                continue
            if is_testlike(cmd):
                out.append(cmd.strip())
        self.cmd_lines = len(lines)
        return out

    def _new_stream_paths(self):
        if not self.agent_stream:
            return []
        try:
            with open(self.agent_stream, errors="replace") as f:
                f.seek(self.stream_off)
                chunk = f.read()
                self.stream_off = f.tell()
        except OSError:
            return []
        out = []
        for tok in STREAM_PATH_RE.findall(chunk):
            out.append(f"python -m pytest {tok}")
        return out

    def harvest(self):
        """New candidates since last call: agent commands first (strongest
        signal), then stream-leaked paths. Node-level candidates also emit
        their file-level sibling. Deduped across the run."""
        fresh = []
        for src in (self._new_agent_cmds(), self._new_stream_paths()):
            for c in src:
                for v in (c, file_level(c)):
                    if v not in self.seen and is_testlike(v):
                        self.seen.add(v)
                        self.order.append(v)
                        fresh.append(v)
        return fresh


# -------------------------------------------------------------------- storage
def _atomic_write(path: Path, data: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def read_generation(cache_dir: Path) -> int:
    try:
        return int((cache_dir / "GENERATION").read_text().strip())
    except (OSError, ValueError):
        return 0


def bump_generation(cache_dir: Path) -> int:
    gen = read_generation(cache_dir) + 1
    _atomic_write(cache_dir / "GENERATION", str(gen))
    return gen


def store_entry(cache_dir: Path, workspace: str, cmd: str, res, dur: float,
                gen: int):
    entry = {
        "cmd": cmd,
        "cwd": workspace,
        "exit": res.returncode,
        "stdout": res.stdout[-STDIO_CAP:],
        "stderr": res.stderr[-STDIO_CAP:],
        "duration_s": round(dur, 3),
        "speculated_at": time.time(),
        "generation": str(gen),
        "source": "edit_respec",
    }
    keys = [hashlib.sha256(f"{workspace}\x00{cmd}".encode()).hexdigest()]
    fk = _family_key(cmd)
    if fk:
        keys.append(f"fam_{fk}")
    for key in keys:
        _atomic_write(cache_dir / f"{key}.json", json.dumps(entry))
    return keys


def schema_check(cache_dir: Path):
    """If legacy worker entries exist, warn on any field they carry that our
    entries won't -- catches a daemon/consumer schema drift at rung 1 instead
    of as a silent serve failure at rung 2."""
    ours = {"cmd", "cwd", "exit", "stdout", "stderr", "duration_s",
            "speculated_at", "generation", "source", "workspace_fingerprint"}
    for p in sorted(cache_dir.glob("*.json"))[:3]:
        try:
            legacy = set(json.loads(p.read_text()).keys())
        except (OSError, json.JSONDecodeError):
            continue
        extra = legacy - ours
        if extra:
            _log(f"WARNING: legacy entry {p.name} carries fields "
                 f"we don't write: {sorted(extra)} -- if the daemon's serve "
                 "consumer needs these, tell me and I'll add them")
        break


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--spec-log", required=True,
                    help="worker's spec.log; predictor candidates are parsed "
                         "from it (zero extra tokens)")
    ap.add_argument("--commands-log", default=None,
                    help="shelld commands.jsonl: the agent's executed "
                         "commands (strongest candidate source)")
    ap.add_argument("--agent-stream", default=None,
                    help="codex --json stdout.jsonl: reasoning text leaks "
                         "test paths before execution")
    ap.add_argument("--max-idle-runs", type=int, default=MAX_IDLE_RUNS)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--max-generations", type=int, default=5)
    ap.add_argument("--cmd-timeout", type=int, default=CMD_TIMEOUT)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    ws = str(Path(args.workspace).resolve())
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # publish generation 0 so the daemon's O(1) check has a file to read
    if not (cache_dir / "GENERATION").exists():
        _atomic_write(cache_dir / "GENERATION", "0")
    schema_check(cache_dir)

    t0 = time.time()
    baseline = deep_fingerprint(ws)
    scan_s = time.time() - t0
    poll = max(args.poll, scan_s * 2)  # auto-backoff if overlay scans are slow
    _log(f"watching {ws} poll={poll:.2f}s "
         f"(first scan {scan_s*1000:.0f}ms) gen={read_generation(cache_dir)}")

    gens_executed = 0
    idle_runs = 0
    traj = Trajectory(args.commands_log, args.agent_stream)
    done_at_gen = set()   # (gen, cmd) already cached

    def run_batch(cands, gen, budget):
        """Pre-run candidates against the current tree, newest-gen-stamped.
        Returns number executed. Discards on generation race."""
        global CURRENT_PROC
        n = 0
        for cmd in cands:
            if STOP or n >= budget:
                break
            if (gen, cmd) in done_at_gen:
                continue
            t1 = time.time()
            try:
                CURRENT_PROC = subprocess.Popen(
                    ["bash", "-lc", cmd], cwd=ws, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    preexec_fn=lambda: os.nice(10))
                out, err = CURRENT_PROC.communicate(timeout=args.cmd_timeout)
                res = subprocess.CompletedProcess(
                    cmd, CURRENT_PROC.returncode, out, err)
            except subprocess.TimeoutExpired:
                CURRENT_PROC.kill()
                CURRENT_PROC.communicate()
                _log(f"gen {gen}: TIMEOUT {cmd!r}")
                continue
            except OSError as e:
                _log(f"gen {gen}: exec error {cmd!r}: {e}")
                continue
            finally:
                CURRENT_PROC = None
            dur = time.time() - t1
            if read_generation(cache_dir) != gen:
                _log(f"gen {gen}: raced by newer edit, discarding {cmd!r}")
                break
            keys = store_entry(cache_dir, ws, cmd, res, dur, gen)
            done_at_gen.add((gen, cmd))
            n += 1
            _log(f"gen {gen}: cached exit={res.returncode} "
                 f"{dur:.2f}s {cmd!r} keys={[k[:12] for k in keys]}")
        return n

    while not STOP:
        time.sleep(poll)
        fp = deep_fingerprint(ws)

        if fp == baseline:
            # ---- PRE/BETWEEN-EDIT: idle speculation from live trajectory ---
            fresh = traj.harvest()
            if fresh and idle_runs < args.max_idle_runs:
                gen = read_generation(cache_dir)
                _log(f"idle: {len(fresh)} new trajectory candidate(s) "
                     f"at gen {gen}")
                idle_runs += run_batch(expand_variants(fresh), gen,
                                       args.max_idle_runs - idle_runs)
                baseline = deep_fingerprint(ws)  # our runs may touch mtimes
            continue

        # ---- POST-EDIT --------------------------------------------------
        gen = bump_generation(cache_dir)
        _log(f"edit detected -> generation {gen}")
        baseline = fp
        if gens_executed >= args.max_generations:
            _log(f"gen {gen}: max-generations reached; "
                 "bumping only (no re-runs)")
            continue
        gens_executed += 1
        traj.harvest()  # fold in anything new before building the union
        # union, strongest signal first: agent's own probes (newest first),
        # then stream leaks, then the worker predictor's list
        pred = parse_candidates(args.spec_log)
        ordered = list(dict.fromkeys(traj.order[::-1] + pred))
        cands = expand_variants(ordered)
        if not cands:
            _log(f"gen {gen}: no candidates from trajectory or "
                 f"{args.spec_log} yet")
            continue
        run_batch(cands, gen, MAX_CANDIDATES_PER_GEN)
        baseline = deep_fingerprint(ws)

    _log("stopped")


if __name__ == "__main__":
    main()