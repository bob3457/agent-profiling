#!/usr/bin/env python3
"""patch_sessiond_prefix.py — daemon-side compound prefix-serve.

Evidence (astropy-13398, runs 20260731_183523 / _185331): every real
serve-path lookup was a compound; the exact/family keys are structurally
blind to them even when the watcher had the parts cached individually
(e.g. 'git diff --check; git status --short; python -m pytest ... -q'
-> no_entry while both the git part and the exact pytest part sat in
cache at current generation). Corpus: 68% of all logged agent commands
are compounds; 48% of those split cleanly at avg 2.6 parts.

WHAT THIS ADDS to shell_sessiond.py, in handle()'s miss path:
  split the incoming command (spec_compound.split_for_serve, quote/paren
  aware; heredocs/backticks/top-level ||/& refuse), fold a leading
  `cd <ws>` into the effective cwd, then walk parts left-to-right serving
  from the spec cache (exact+family keys, generation-validated, no
  per-part log spam). The served prefix ends at the first miss,
  non-servable part (confined pipe/redirect, shell-state cmd, subshell,
  assignment), or non-leading `cd`. The remainder is re-joined and runs
  LIVE as one command in the session -- so `cd` replays live, forward
  state within the remainder is preserved, and the brace-group prologue
  imposes the folded cwd natively (reset_cwd semantics unchanged).

  Failure semantics per joiner: a cached part with exit!=0 followed by
  `;` serves and continues (bash would). Followed by `&&` it serves as
  the compound's FINAL result iff every later joiner is also `&&`
  (total short-circuit -- the pytest-fails && compileall case); with a
  later `;` the prefix conservatively ends BEFORE the failure and bash
  gets the whole remainder live (correct semantics, no savings).

  Disabled under --persist-cwd (cwd folding assumes per-command reset).

TELEMETRY: serve_decisions.jsonl gains
  {"decision":"prefix_serve","parts_served":n,"parts_total":m,
   "saved_s":x,"full":bool}; reply gains prefix_served/prefix_total/
  spec_saved_s; stats gain prefix_serves; a FULL prefix serve also
  counts as a cache_hit. The whole-command no_entry record preceding a
  prefix_serve record is expected and lets the analyzer join the two.

Also: spec_cache_lookup gains log=True kwarg so per-part probes don't
spam the decisions log (default behavior unchanged).

Idempotent; asserts verbatim anchors from the inspected file; refuses
to write on drift. Requires spec_compound.py v2 (split_for_serve)
deployed in latency-opt/speculation/.
"""
import sys
from pathlib import Path

SESSIOND = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt/scripts/shell_sessiond.py")

t = SESSIOND.read_text()
if "_prefix_try_serve" in t:
    print("shell_sessiond.py: already patched, no-op")
    sys.exit(0)

# ---- sanity: spec_compound v2 must be deployed next door -------------------
sc = SESSIOND.resolve().parent.parent / "speculation" / "spec_compound.py"
assert sc.exists() and "split_for_serve" in sc.read_text(), \
    f"deploy spec_compound.py v2 to {sc} first (needs split_for_serve)"

# ---- 1. spec_cache_lookup: log kwarg ---------------------------------------
A1 = "def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str):"
N1 = "def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str, log: bool = True):"
assert A1 in t, "ANCHOR drifted: spec_cache_lookup signature"
t = t.replace(A1, N1, 1)

A2 = """        reason = _spec_entry_invalid(cache_dir, entry, cwd)
        if reason:
            _log_serve_decision(cache_dir, cmd, key, reason, entry)
            continue
        _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry"""
N2 = """        reason = _spec_entry_invalid(cache_dir, entry, cwd)
        if reason:
            if log:
                _log_serve_decision(cache_dir, cmd, key, reason, entry)
            continue
        if log:
            _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry"""
assert A2 in t, "ANCHOR drifted: spec_cache_lookup serve/stale logging"
t = t.replace(A2, N2, 1)

A3 = """    if len(keys) > 1 or "pytest" in cmd or "runtests.py" in cmd:
        # family-keyed OR test-looking => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)"""
N3 = """    if log and (len(keys) > 1 or "pytest" in cmd or "runtests.py" in cmd):
        # family-keyed OR test-looking => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)"""
assert A3 in t, "ANCHOR drifted: spec_cache_lookup no_entry logging"
t = t.replace(A3, N3, 1)

# ---- 2. module-level prefix machinery ---------------------------------------
A4 = """# ---------------------------------------------------------------- spec cache
def spec_cache_lookup"""
N4 = '''def _log_prefix_decision(cache_dir, cmd, n, total, saved_s, full):
    try:
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(), "cmd": cmd[:300],
                "decision": "prefix_serve", "parts_served": n,
                "parts_total": total, "saved_s": round(saved_s, 3),
                "full": full}) + "\\n")
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
    for i, (text, stop_on_fail, servable) in enumerate(parts):
        if not servable or is_state_cmd(text) or is_cd_cmd(text):
            break                         # ends the servable prefix
        entry = spec_cache_lookup(cache_dir, text, eff_cwd, log=False)
        if entry is None:
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
        return None
    if n == total:
        return {"remainder": None, "cwd": eff_cwd, "stdout": "".join(out),
                "stderr": "".join(err), "exit": last_exit, "n": n,
                "total": total, "saved_s": saved}
    return {"remainder": rejoin(parts[n:]), "cwd": eff_cwd,
            "stdout": "".join(out), "stderr": "".join(err), "exit": None,
            "n": n, "total": total, "saved_s": saved}


# ---------------------------------------------------------------- spec cache
def spec_cache_lookup'''
assert A4 in t, "ANCHOR drifted: spec cache section header"
t = t.replace(A4, N4, 1)

# ---- 3. handle(): attempt prefix serve on whole-command miss ----------------
A5 = """            self._record(cmd, cwd, res)
            return res

        near = spec_near_miss(self.args.spec_cache, cmd, cwd)"""
N5 = """            self._record(cmd, cwd, res)
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
                                 pre["total"], pre["saved_s"], full=True)
            self._record(cmd, cwd, res)
            return res
        if pre is not None:                                # partial prefix
            live_cmd, live_cwd = pre["remainder"], pre["cwd"]

        near = spec_near_miss(self.args.spec_cache, cmd, cwd)"""
assert A5 in t, "ANCHOR drifted: handle() hit-block tail / near_miss line"
t = t.replace(A5, N5, 1)

A6 = """        res = session.run(cmd, cwd, timeout,
                          reset_cwd=not self.args.persist_cwd,"""
N6 = """        res = session.run(live_cmd, live_cwd, timeout,
                          reset_cwd=not self.args.persist_cwd,"""
assert A6 in t, "ANCHOR drifted: session.run call"
t = t.replace(A6, N6, 1)

A7 = """        res["cached"] = False
        res["session_reused"] = reused"""
N7 = """        res["cached"] = False
        res["session_reused"] = reused
        if pre is not None:                # splice served prefix into reply
            res["stdout"] = pre["stdout"] + res.get("stdout", "")
            res["stderr"] = pre["stderr"] + res.get("stderr", "")
            res["prefix_served"] = pre["n"]
            res["prefix_total"] = pre["total"]
            res["spec_saved_s"] = round(pre["saved_s"], 3)
            self.stats["prefix_serves"] = self.stats.get("prefix_serves", 0) + 1
            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=False)"""
assert A7 in t, "ANCHOR drifted: res cached/session_reused pair"
t = t.replace(A7, N7, 1)

SESSIOND.write_text(t)
import py_compile
py_compile.compile(str(SESSIOND), doraise=True)
print("shell_sessiond.py: prefix-serve spliced (lookup log kwarg, "
      "_prefix_try_serve, handle splice), compiles OK")
