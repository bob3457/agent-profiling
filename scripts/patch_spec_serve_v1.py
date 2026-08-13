#!/usr/bin/env python3
"""patch_spec_serve_v1.py — segment-serve: serve cached parts ANYWHERE in a
compound (not just a leading prefix), executing read-only allowlisted parts
live in order, with bash &&/; short-circuit semantics honored on REAL exit
codes. Also: top-level newlines split like `;` (bash semantics), closing the
multiline-compound gap (55%% of family colds in the 3-arm corpus).

Safety invariants (the failure-mode review):
  - take over a line only if the static plan finds >=1 cache-servable part
    BEFORE any unhandleable part; otherwise the ORIGINAL command runs live
    untouched (never execute-some-then-bail: no double execution)
  - read-only allowlist is strict: no output redirects, no substitutions,
    sed requires -n and forbids -i, find forbids -delete/-exec/-ok,
    git restricted to diff/status/log/show/rev-parse/ls-files
  - every planned serve re-validates the entry (incl. workspace fingerprint)
    at serve time, AFTER any live parts executed; a lost entry degrades to
    live execution of that part, never a stale serve
  - shell-state parts (cd/export/assignments/set/source/subshells) end the
    handled region; the tail re-joins and runs as ONE live command so any
    forward state stays intact
Idempotent. Usage (repo root): python3 scripts/patch_spec_serve_v1.py
"""
from pathlib import Path

SC = Path("latency-opt/speculation/spec_compound.py")
SD = Path("latency-opt/scripts/shell_sessiond.py")


def apply(path, old, new, tag):
    s = path.read_text()
    if new in s:
        print(f"[skip] {tag} (already applied)")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] anchor not found for {tag} in {path}")
    path.write_text(s.replace(old, new, 1))
    print(f"[ok] {tag}")


# ---- 1. spec_compound: top-level newline == `;` -----------------------------
apply(SC, """        if depth == 0 and c == ";":
            parts.append(("".join(buf).strip(), False)); buf = []
            i += 1; continue""",
"""        if depth == 0 and c == ";":
            parts.append(("".join(buf).strip(), False)); buf = []
            i += 1; continue
        if depth == 0 and c in ("\\n", "\\r"):
            # bash: unquoted newline separates commands like `;`
            # (heredocs are refused before splitting, quotes handled above)
            parts.append(("".join(buf).strip(), False)); buf = []
            i += 1; continue""", "newline-split")

# ---- 2. spec_compound: read-only passthrough classifier ---------------------
apply(SC, """def rejoin(parts) -> str:""",
'''_RO_SIMPLE = {"ls", "pwd", "echo", "printf", "true", "false", "wc", "cat",
              "head", "tail", "grep", "rg", "egrep", "fgrep", "which",
              "file", "stat", "du", "date", "uname", "id", "whoami", "nl",
              "cut", "sort", "uniq", "tr", "column", "basename", "dirname",
              "realpath", "readlink", "md5sum", "sha256sum", "test", "[",
              "diff", "comm"}
_RO_GIT = {"diff", "status", "log", "show", "rev-parse", "ls-files",
           "describe", "branch"}
_FIND_FORBID = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                "-fprintf", "-fls"}


def _split_pipeline(text):
    """Split one part on top-level `|` (quote-aware); None on scan trouble."""
    stages, buf, quote, i = [], [], None, 0
    while i < len(text):
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1; continue
        if c in ("\\'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if c == "|":
            if text[i:i + 2] == "||":
                return None
            stages.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    if quote:
        return None
    stages.append("".join(buf))
    return [s.strip() for s in stages if s.strip()]


def ro_passthrough(text: str) -> bool:
    """True iff this part is safe to execute LIVE ahead of serving later
    cached parts: provably read-only (no workspace/env mutation), so the
    cached results behind it stay valid. Strict by construction — a miss
    here costs a serve, a false positive corrupts one."""
    import shlex
    if _ASSIGN_RE.match(text) or text.lstrip().startswith("("):
        return False
    if is_state_cmd(text) or is_cd_cmd(text):
        return False
    if _scan_outside_quotes(text, (">",)):          # any output redirect
        return False
    if _scan_outside_quotes(text, ("$(", "<(", ">(", "`", "<<")):
        return False
    stages = _split_pipeline(text)
    if not stages:
        return False
    for st in stages:
        try:
            toks = shlex.split(st)
        except ValueError:
            return False
        if not toks:
            return False
        head = os.path.basename(toks[0])
        if head == "git":
            if len(toks) < 2 or toks[1] not in _RO_GIT:
                return False
        elif head == "sed":
            if "-n" not in toks or any(t == "-i" or t.startswith("-i")
                                       for t in toks[1:]):
                return False
        elif head == "find":
            if any(t in _FIND_FORBID for t in toks):
                return False
        elif head == "command":
            if len(toks) < 2 or toks[1] != "-v":
                return False
        elif head not in _RO_SIMPLE:
            return False
    return True


def rejoin(parts) -> str:''', "ro-passthrough")

# ---- 3. spec_compound: extend self-test suite -------------------------------
apply(SC, """    raise SystemExit(0 if good else 1)""",
"""    # -------- spec-serve-v1: newline split + ro_passthrough --------
    scheck("pytest x -q\\ngit diff --check\\ngit status --short",
           [("pytest x -q", False, True), ("git diff --check", False, True),
            ("git status --short", True, True)])
    check("pytest x -q\\ngit diff --check", ["pytest x -q", "git diff --check"])
    ro_cases = [
        ("git diff --check", True), ("git diff -- a.py b.py", True),
        ("git status --short", True), ("git checkout HEAD~1 f.py", False),
        ("git stash", False), ("ls -la", True), ("grep -RIn pat src", True),
        ("grep pat f | head -40", True), ("sed -n '1,50p' f.py", True),
        ("sed -i 's/a/b/' f.py", False), ("sed '1,50p' f.py", False),
        ("find . -name '*.py'", True), ("find . -name x -delete", False),
        ("command -v python3.9", True), ("rm -rf build", False),
        ("pytest x.py -q", False), ("echo hi > f", False),
        ("cat f | tee g", False), ("python --version", False),
        ("x=1", False), ("cd sub", False)]
    for t, want in ro_cases:
        got = ro_passthrough(t)
        good &= got == want
        print(f"{OK if got == want else BAD} ro {t!r:<38} -> {got}")
    raise SystemExit(0 if good else 1)""", "self-tests")

# ---- 4. daemon: _prefix_try_serve -> segment plan+execute -------------------
apply(SD, '''def _prefix_try_serve(cache_dir: str, cmd: str, cwd: str):
    """Serve a leading run of a compound's parts from the spec cache.''',
'''def _prefix_try_serve(cache_dir: str, cmd: str, cwd: str, exec_fn=None):
    """[spec-serve-v1] Segment-serve a compound: serve cached parts ANYWHERE
    in the line; read-only allowlisted parts before/between them execute
    LIVE in order via exec_fn (bash &&/; semantics on real exit codes).
    Static plan first: take over only if >=1 serve occurs before any
    unhandleable part; otherwise None and the original command runs
    untouched. exec_fn=None restores prefix-only behavior (no live parts).

    Serve a leading run of a compound's parts from the spec cache.''',
      "segserve-doc")

apply(SD, '''    parts, eff_cwd = fold_cd_serve(split, cwd)
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
            k = hashlib.sha256(f"{eff_cwd}\\x00{text}".encode()).hexdigest()
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
        n, last_exit = i + 1, ex''',
'''    try:
        from spec_compound import ro_passthrough
    except ImportError:
        def ro_passthrough(_t):
            return False
    parts, eff_cwd = fold_cd_serve(split, cwd)
    if not parts:
        return None                       # pure-cd compound: run live
    folded = len(parts) < len(split) or eff_cwd != cwd
    if len(parts) < 2 and not folded:
        return None                       # simple command: normal path
    total = len(parts)

    # ---- pass 1: STATIC plan (no side effects) ----
    plan, stop_reason, stop_part = [], None, None
    for i, (text, stop_on_fail, servable) in enumerate(parts):
        clean = servable and not is_state_cmd(text) and not is_cd_cmd(text)
        if clean and spec_cache_lookup(cache_dir, text, eff_cwd,
                                       log=False) is not None:
            plan.append(("serve", i)); continue
        if exec_fn is not None and ro_passthrough(text):
            plan.append(("live", i)); continue
        stop_reason = ("hazard" if not servable else
                       "state_cmd" if is_state_cmd(text) else
                       "cd_cmd" if is_cd_cmd(text) else "no_entry")
        stop_part = text[:120]
        break
    if not any(op == "serve" for op, _ in plan):
        return None                       # nothing to gain: run untouched

    # ---- pass 2: execute the plan in order ----
    out, err = [], []
    saved, served_n, live_n, live_wall = 0.0, 0, 0, 0.0
    last_exit, idx_done = 0, -1
    skip_until = -1
    for op, i in plan:
        if i <= skip_until:
            idx_done = i
            continue                      # short-circuited by an earlier &&
        text, stop_on_fail, _srv = parts[i]
        entry = None
        if op == "serve":
            entry = spec_cache_lookup(cache_dir, text, eff_cwd, log=False)
        if entry is not None:
            ex = entry.get("exit", 1)
            out.append(entry.get("stdout", ""))
            err.append(entry.get("stderr", ""))
            saved += entry.get("duration_s") or 0.0
            served_n += 1
        else:                             # live part, or entry lost: run it
            res = exec_fn(text, eff_cwd)
            ex = res.get("exit", 1)
            out.append(res.get("stdout", ""))
            err.append(res.get("stderr", ""))
            live_n += 1
            live_wall += res.get("wall_s") or 0.0
        last_exit, idx_done = ex, i
        if ex != 0 and stop_on_fail and i < total - 1:
            # bash: skip subsequent &&-joined parts up to the next `;`
            k = i
            while k < total - 1 and parts[k][1]:
                k += 1
            skip_until = k
    if served_n == 0:
        return None                       # plan degraded to all-live: bail
    n = served_n''', "segserve-core")

apply(SD, '''    if n == 0:
        try:                              # attempt died on part 0: say why
            with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": time.time(), "cmd": cmd[:300],
                    "decision": "prefix_attempt", "parts_total": total,
                    "parts_served": 0, "stop_reason": stop_reason,
                    "stop_part": stop_part}) + "\\n")
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
            "stop_reason": stop_reason, "stop_part": stop_part}''',
'''    handled_all = (idx_done >= total - 1) or (skip_until >= total - 1)
    if handled_all:
        return {"remainder": None, "cwd": eff_cwd, "stdout": "".join(out),
                "stderr": "".join(err), "exit": last_exit, "n": n,
                "total": total, "saved_s": saved, "live_parts": live_n,
                "live_wall_s": round(live_wall, 3),
                "stop_reason": stop_reason, "stop_part": stop_part}
    return {"remainder": rejoin(parts[idx_done + 1:]), "cwd": eff_cwd,
            "stdout": "".join(out), "stderr": "".join(err), "exit": None,
            "n": n, "total": total, "saved_s": saved, "live_parts": live_n,
            "live_wall_s": round(live_wall, 3),
            "stop_reason": stop_reason, "stop_part": stop_part}''',
      "segserve-return")

# ---- 5. daemon: handle() passes exec_fn; wall accounting --------------------
apply(SD, '''        live_cmd, live_cwd = cmd, cwd
        pre = None
        if self.args.spec_cache and not self.args.persist_cwd:
            pre = _prefix_try_serve(self.args.spec_cache, cmd, cwd)''',
'''        live_cmd, live_cwd = cmd, cwd
        pre = None
        if self.args.spec_cache and not self.args.persist_cwd:
            def _exec_seg(text, seg_cwd, _key=key, _t=timeout):
                s, _r = self.get_session(_key, False)
                return s.run(text, seg_cwd, _t,
                             reset_cwd=not self.args.persist_cwd)
            pre = _prefix_try_serve(self.args.spec_cache, cmd, cwd,
                                    exec_fn=_exec_seg)''', "handle-execfn")

apply(SD, '''            res = {"exit": pre["exit"], "stdout": pre["stdout"],
                   "stderr": pre["stderr"], "cached": True,
                   "session_reused": False, "wall_s": 0.0, "cpu_s": 0.0,
                   "prefix_served": pre["n"], "prefix_total": pre["total"],
                   "spec_saved_s": round(pre["saved_s"], 3)}''',
'''            res = {"exit": pre["exit"], "stdout": pre["stdout"],
                   "stderr": pre["stderr"], "cached": True,
                   "session_reused": False,
                   "wall_s": pre.get("live_wall_s", 0.0), "cpu_s": 0.0,
                   "prefix_served": pre["n"], "prefix_total": pre["total"],
                   "live_parts": pre.get("live_parts", 0),
                   "spec_saved_s": round(pre["saved_s"], 3)}''',
      "handle-wall")

print("\\nspec-serve-v1 applied. Now run:")
print("  python3 latency-opt/speculation/spec_compound.py   # self-tests")
print("  python3 -m py_compile latency-opt/scripts/shell_sessiond.py")
