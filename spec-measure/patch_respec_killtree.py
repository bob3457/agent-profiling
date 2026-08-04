#!/usr/bin/env python3
"""patch_respec_killtree.py — respec must kill candidate TREES, not wrappers.

Measured on arm_C.20260804_135822 (14309 +29s, django-11066 +28s): respec's
kill paths call pr.kill() on the `bash -lc` wrapper, which ORPHANS the
actual payload (pytest). Consequences: (a) the orphan keeps executing at
nice 10 against the old workspace while the NEXT task runs, and (b) respec
itself blocks in pr.communicate() until the orphan releases the stdout/
stderr pipes -- the observed +28s lifetimes. The TIMEOUT and generation-
race paths share the bug.

Fix: spawn each candidate in its own session (setsid) and kill by process
group on every path (_sigterm, timeout, race, STOP).

Verbatim anchors; idempotent.  Usage: patch_respec_killtree.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
F = ROOT / "latency-opt/speculation/edit_respec.py"
DONE, SKIP = [], []


def patch(anchor, replacement, marker, label):
    src = F.read_text()
    if marker in src:
        SKIP.append(label)
        return
    assert anchor in src, f"{label}: anchor not found"
    assert src.count(anchor) == 1, f"{label}: anchor not unique"
    F.write_text(src.replace(anchor, replacement))
    DONE.append(label)


# ---- 0. tree-kill helper next to _sigterm ---------------------------------------
patch('''def _sigterm(_sig, _frm):
    global STOP
    STOP = True
    for p in list(CURRENT_PROCS):
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass''',
      '''def _kill_tree(pr):
    """Kill the candidate's whole process group. pr.kill() alone kills only
    the bash -lc wrapper, orphaning the payload (which keeps running against
    the workspace AND holds the output pipes open, pinning communicate())."""
    try:
        os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            pr.kill()
        except OSError:
            pass


def _sigterm(_sig, _frm):
    global STOP
    STOP = True
    for p in list(CURRENT_PROCS):
        if p.poll() is None:
            _kill_tree(p)''',
      '_kill_tree',
      "tree-kill helper + _sigterm uses it")

# ---- 1. spawn in own session (keep nice) -----------------------------------------
patch('''        pr = subprocess.Popen(["bash", "-lc", cmd], cwd=ws, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              preexec_fn=lambda: os.nice(10))''',
      '''        def _pre():                      # own session: killable as a
            os.setsid()                      # tree; nice as before
            os.nice(10)

        pr = subprocess.Popen(["bash", "-lc", cmd], cwd=ws, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              preexec_fn=_pre)''',
      'os.setsid()',
      "spawn in own session")

# ---- 2. timeout path ---------------------------------------------------------------
patch('''            for pr in list(live):
                if time.time() - pr._t0 > args.cmd_timeout:
                    pr.kill()
                    pr.communicate()
                    CURRENT_PROCS.discard(pr)
                    live.remove(pr)
                    _log(f"gen {gen}: TIMEOUT {pr._cmd!r}")''',
      '''            for pr in list(live):
                if time.time() - pr._t0 > args.cmd_timeout:
                    _kill_tree(pr)
                    pr.communicate()
                    CURRENT_PROCS.discard(pr)
                    live.remove(pr)
                    _log(f"gen {gen}: TIMEOUT {pr._cmd!r}")''',
      'cmd_timeout:\n                    _kill_tree(pr)',
      "timeout path tree-kill")

# ---- 3. generation-race path --------------------------------------------------------
patch('''            if read_generation(cache_dir) != gen:
                for pr in live:
                    pr.kill(); pr.communicate(); CURRENT_PROCS.discard(pr)
                _log(f"gen {gen}: raced by newer edit, batch abandoned")''',
      '''            if read_generation(cache_dir) != gen:
                for pr in live:
                    _kill_tree(pr); pr.communicate(); CURRENT_PROCS.discard(pr)
                _log(f"gen {gen}: raced by newer edit, batch abandoned")''',
      'raced by newer edit, batch abandoned")\n' if False else
      '_kill_tree(pr); pr.communicate(); CURRENT_PROCS.discard(pr)\n'
      '                _log(f"gen {gen}: raced',
      "race path tree-kill")

# ---- 4. STOP path --------------------------------------------------------------------
patch('''        for pr in live:  # STOP path
            pr.kill(); pr.communicate(); CURRENT_PROCS.discard(pr)
        return n''',
      '''        for pr in live:  # STOP path
            _kill_tree(pr); pr.communicate(); CURRENT_PROCS.discard(pr)
        return n''',
      '# STOP path\n            _kill_tree(pr)',
      "STOP path tree-kill")

print(f"applied: {DONE}")
print(f"already present: {SKIP}")
