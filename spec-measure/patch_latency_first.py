#!/usr/bin/env python3
"""patch_latency_first.py — speculation must never sit on the agent's
critical path. Four changes, all from the arm_C.20260803_190143 findings
(6.6s join waits for 1.7s net; 6-way respec I/O against live commands; a
measured exact-key miss on a $PWD-literal prediction):

  1. GENERATION-STAMPED MARKERS  worker + respec write the generation at
     mark time into the .inflight marker
  2. DOOMED-JOIN REFUSAL         the daemon refuses to wait on a marker
     whose generation no longer matches: the entry cannot validate, so the
     wait is guaranteed waste (the S2/unlogged-waste case, now prevented
     instead of merely logged)
  3. JOIN BUDGET 8s -> 2s        SPEC_JOIN_MAX_WAIT default drops to 2
     (env still overrides); a lost join costs a quarter as much
  4. RESPEC FAN-OUT KNOBS        harness passes --parallel/--max-per-gen
     from SPEC_RESPEC_PARALLEL (default 2) / SPEC_MAX_PER_GEN (default 8);
     old behavior: SPEC_RESPEC_PARALLEL=6 SPEC_MAX_PER_GEN=16
  5. $PWD KEY NORMALIZATION      worker expands literal $PWD/${PWD} to the
     workspace path before execution/keying (cwd==ws, so semantics are
     unchanged; the agent's expanded command now key-matches)

Verbatim anchors; idempotent.  Usage: patch_latency_first.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
OPT = ROOT / "latency-opt"
DONE, SKIP = [], []


def patch(path: Path, anchor: str, replacement: str, marker: str, label: str):
    src = path.read_text()
    if marker in src:
        SKIP.append(label)
        return
    assert anchor in src, f"{label}: anchor not found in {path}"
    assert src.count(anchor) == 1, f"{label}: anchor not unique in {path}"
    path.write_text(src.replace(anchor, replacement))
    DONE.append(label)


# ---- 1a. worker: stamp generation into the marker ------------------------------
patch(OPT / "speculation/speculative_worker.py",
      '''def _mark_inflight(cache_dir: Path, key: str, cmd: str):
    try:
        (cache_dir / f"{key}.inflight").write_text(
            json.dumps({"ts": time.time(), "pid": os.getpid(), "cmd": cmd[:200]}))
    except OSError:
        pass''',
      '''def _marker_generation(cache_dir: Path):
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
        pass''',
      '_marker_generation',
      "worker marker gen stamp")

# ---- 1b. respec: stamp generation into the marker -------------------------------
patch(OPT / "speculation/edit_respec.py",
      '''            _inflight_path(cmd).write_text(json.dumps(
                {"ts": time.time(), "pid": os.getpid(), "cmd": cmd[:200]}))''',
      '''            _inflight_path(cmd).write_text(json.dumps(
                {"ts": time.time(), "pid": os.getpid(), "cmd": cmd[:200],
                 "gen": str(read_generation(cache_dir))}))''',
      '"gen": str(read_generation(cache_dir))',
      "respec marker gen stamp")

# ---- 2+3. daemon: doomed-join refusal + 2s default budget -----------------------
patch(OPT / "scripts/shell_sessiond.py",
      '''    max_age = float(os.environ.get("SPEC_JOIN_MAX_AGE", "300"))
    if time.time() - float(info.get("ts", 0)) > max_age:
        return 0.0                       # crashed/abandoned writer: ignore
    wait_max = float(os.environ.get("SPEC_JOIN_MAX_WAIT", "8"))''',
      '''    max_age = float(os.environ.get("SPEC_JOIN_MAX_AGE", "300"))
    if time.time() - float(info.get("ts", 0)) > max_age:
        return 0.0                       # crashed/abandoned writer: ignore
    mgen = info.get("gen")
    if mgen is not None:                 # doomed join: the writer started
        cur = _current_generation(cache_dir)   # under an older generation;
        if cur is not None and str(mgen) != str(cur):   # its entry cannot
            return 0.0                   # validate -- waiting is pure waste
    wait_max = float(os.environ.get("SPEC_JOIN_MAX_WAIT", "2"))''',
      'doomed join',
      "daemon doomed-join refusal + 2s budget")

# ---- 4. harness: respec fan-out knobs -------------------------------------------
patch(OPT / "harness/run_latency_arm.sh",
      '      nohup python3 -u "$OPT/speculation/edit_respec.py" \\',
      '      SPEC_CPU_TAG=respec nohup python3 -u '
      '"$OPT/speculation/edit_respec.py" \\\n'
      '        --parallel "${SPEC_RESPEC_PARALLEL:-2}" '
      '--max-per-gen "${SPEC_MAX_PER_GEN:-8}" \\',
      'SPEC_RESPEC_PARALLEL',
      "harness respec fan-out knobs")

# ---- 5. worker: $PWD key normalization ------------------------------------------
patch(OPT / "speculation/speculative_worker.py",
      '        for cmd, cacheable in fn(ws, ctx):\n'
      '            ikey = None',
      '        for cmd, cacheable in fn(ws, ctx):\n'
      '            # key normalization: cwd==ws, so a literal $PWD is the\n'
      '            # workspace path; expanding it makes the cached key match\n'
      '            # the agent\'s expanded command (measured lost race:\n'
      '            # django-10973 runtests, entry landed 2s late on the raw\n'
      '            # string while the expanded twin ran live)\n'
      '            cmd = cmd.replace("${PWD}", str(ws)).replace("$PWD", str(ws))\n'
      '            ikey = None',
      'key normalization: cwd==ws',
      "worker $PWD normalization")

print(f"applied: {DONE}")
print(f"already present: {SKIP}")
