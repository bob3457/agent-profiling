#!/usr/bin/env python3
"""spec_compound.py — conservative compound-command decomposition.

WATCHER API (used by edit_respec.py pre-runs):

split_compound(cmd) -> [(part, stop_on_fail), ...] or None. Split ONLY at
top-level `&&` and `;` (quote- and paren-aware). ANY hazard anywhere
-- pipes, redirects, command/process substitution, backticks, heredocs,
`||`, `&` backgrounding, subshells, variable assignment prefixes -- makes
the whole command non-decomposable (None): under-splitting is a cache miss,
over-splitting is wrong results.

fold_cd(parts, cwd) -> (parts', cwd'): folds a LEADING `cd X` into the
working directory for the remaining parts -- normalization, not execution.

SERVE-SIDE API (used by the daemon prefix-serve):

split_for_serve(cmd) -> [(part, stop_on_fail, servable), ...] or None.
Looser than the watcher API, grounded in live evidence (astropy 20260731:
`git diff --check; git diff --stat; git diff .. | sed ..; pytest ..` refused
whole while its first parts sat in cache): a hazard CONFINED WITHIN one part
does not
poison the split -- pipes/redirects/substitutions never cross a top-level
`&&`/`;` boundary, so splitting stays sound; the hazard part is merely
flagged servable=False (it ends any served prefix and executes live in the
re-joined remainder). Structural hazards that the tokenizer cannot see
through still refuse the WHOLE command: backticks (tokenizer is blind
inside them, a `&&` there would mis-split), heredocs (multi-line bodies),
top-level `||` and `&`, unbalanced quotes/parens.

Parts flagged servable=False, besides confined-hazard parts:
  - shell-state commands (export/set/source/alias/cd/eval/...): serving
    one would skip the session-state effect it exists to produce
  - bare or prefix VAR= assignment parts (state may flow forward)
  - subshell `( .. )` parts (conservative)
Non-servable parts split fine; forward state within the remainder is
preserved because the remainder is re-joined and executed as ONE command.

fold_cd_serve(parts3, cwd): fold_cd for 3-tuples.
is_state_cmd(text) / is_cd_cmd(text): predicates for the daemon so it
needs no regex knowledge of its own.

Semantics contract for consumers (watcher pre-run, daemon prefix-serve):
  - parts execute left-to-right; stop_on_fail on part i describes the
    joiner AFTER part i (`&&` -> True, `;` -> False; last part: True)
  - speculate/serve only a LEADING run of parts; never skip a part, since
    later parts may depend on earlier effects
  - a served part with exit!=0 followed by `;` is servable (bash would
    continue); followed by `&&` it short-circuits the rest ONLY if every
    later joiner is also `&&`
"""
import os
import re

_HAZARDS = ("|", ">", "<", "`", "$(", "<(", ">(", "<<")
# hazards that make the SPLIT itself untrustworthy (tokenizer-blind):
_STRUCTURAL = ("`", "<<")
# hazards safely confined within one part (flag it, keep the split):
_CONFINED = ("|", ">", "<", "$(", "<(", ">(")

_STATE_RE = re.compile(
    r"^\s*(?:(?:export|set|unset|source|alias|shopt|trap|eval|ulimit"
    r"|umask|declare|readonly|local|cd)\b|\.\s)")
_CD_RE = re.compile(r"^\s*cd(\s|$)")
_ASSIGN_RE = re.compile(r"^\w+=\S")


def _scan_outside_quotes(cmd, needles):
    """Return the first needle found outside quotes, else None."""
    quote = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2; continue
            if c == quote:
                quote = None
            i += 1; continue
        if c in ("'", '"'):
            quote = c; i += 1; continue
        if c == "\\":
            i += 2; continue
        for h in needles:
            if cmd.startswith(h, i):
                # `<<` must win over `<`; needles are checked per position,
                # so order needles longest-first at the call site
                return h
        i += 1
    return None


def _top_level_split(cmd):
    """Yield (part, stop_on_fail) split at top-level && and ; only.
    Returns None on || / & / unbalanced constructs."""
    parts, buf = [], []
    i, n = 0, len(cmd)
    depth = 0
    quote = None
    while i < n:
        c = cmd[i]
        nxt = cmd[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(nxt); i += 2; continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:
            buf.append(c); buf.append(nxt); i += 2; continue
        two = c + nxt
        if two == "&&":
            if depth == 0:
                parts.append(("".join(buf).strip(), True)); buf = []
            else:
                buf.append("&&")        # confined inside ( ): not a boundary
            i += 2; continue
        if depth == 0 and c == ";":
            parts.append(("".join(buf).strip(), False)); buf = []
            i += 1; continue
        if two == "||":
            if depth == 0:
                return None             # top-level || changes semantics
            buf.append("||"); i += 2; continue
        if c == "&":
            if depth == 0:
                return None             # backgrounding
            buf.append(c); i += 1; continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return None
        buf.append(c); i += 1
    if quote or depth != 0:
        return None
    parts.append(("".join(buf).strip(), True))
    return [(p, s) for p, s in parts if p]


# ================================================================ legacy API
def split_compound(cmd: str):
    """Return [(part, stop_on_fail), ...] or None if not decomposable.
    A single simple command returns a one-element list. Conservative:
    ANY hazard anywhere refuses the whole command (watcher contract)."""
    if not cmd or not cmd.strip():
        return None
    # order longest-first so `<<` is seen before `<`
    if _scan_outside_quotes(cmd, ("<<", "$(", "<(", ">(", "|", ">", "<", "`")):
        return None
    parts = _top_level_split(cmd)
    if not parts:
        return None
    for p, _ in parts:
        if _ASSIGN_RE.match(p):
            return None
        if p.startswith("("):
            return None
    return parts


def fold_cd(parts, cwd):
    """Fold a LEADING `cd <dir>` (with && semantics) into the cwd for the
    remaining parts. Returns (parts, cwd). Only simple `cd <one-token>`
    folds; `cd` with flags or expansions does not."""
    while parts:
        p, stop = parts[0]
        m = re.fullmatch(r"cd\s+((?:[^\s'\"\\]|\\.|'[^']*'|\"[^\"]*\")+)", p)
        if not m or not stop:
            break
        target = m.group(1).strip("'\"")
        if target.startswith("-") or "$" in target or "~" in target:
            break
        cwd = target if target.startswith("/") else os.path.normpath(
            os.path.join(cwd, target))
        parts = parts[1:]
    return parts, cwd


# ============================================================= serve-side API
def is_state_cmd(text: str) -> bool:
    """Shell-state command: must never be SERVED (skipping it would skip the
    session-state effect it exists to produce). Splitting around it is fine."""
    return bool(_STATE_RE.match(text))


def is_cd_cmd(text: str) -> bool:
    return bool(_CD_RE.match(text))


def _part_servable(p: str) -> bool:
    if _ASSIGN_RE.match(p):
        return False
    if p.startswith("("):
        return False
    if is_state_cmd(p):
        return False
    if _scan_outside_quotes(p, ("$(", "<(", ">(", "|", ">", "<")):
        return False
    return True


def split_for_serve(cmd: str):
    """Return [(part, stop_on_fail, servable), ...] or None.

    Confined hazards flag their part servable=False instead of refusing the
    whole compound; structural hazards (backticks, heredocs) and top-level
    ||/& still refuse (the split itself would be untrustworthy)."""
    if not cmd or not cmd.strip():
        return None
    if _scan_outside_quotes(cmd, ("<<", "`")):
        return None
    parts = _top_level_split(cmd)
    if not parts:
        return None
    return [(p, s, _part_servable(p)) for p, s in parts]


def fold_cd_serve(parts, cwd):
    """fold_cd for 3-tuples: fold a LEADING simple `cd X &&` into cwd."""
    while parts:
        p, stop, _srv = parts[0]
        m = re.fullmatch(r"cd\s+((?:[^\s'\"\\]|\\.|'[^']*'|\"[^\"]*\")+)", p)
        if not m or not stop:
            break
        target = m.group(1).strip("'\"")
        if target.startswith("-") or "$" in target or "~" in target:
            break
        cwd = target if target.startswith("/") else os.path.normpath(
            os.path.join(cwd, target))
        parts = parts[1:]
    return parts, cwd


def rejoin(parts) -> str:
    """Re-join 3-tuple parts into one command preserving joiners."""
    out = []
    for j, (text, stop, _srv) in enumerate(parts):
        out.append(text)
        if j < len(parts) - 1:
            out.append(" && " if stop else " ; ")
    return "".join(out)


# ======================================================================= suite
if __name__ == "__main__":
    OK, BAD = "ok ", "FAIL"
    good = True

    def check(cmd, want):
        global good
        got = split_compound(cmd)
        got_view = [p for p, _ in got] if got else None
        mark = OK if got_view == want else BAD
        good &= got_view == want
        print(f"{mark} {cmd[:58]!r:<62} -> {got_view}")

    # -------- legacy behavior, unchanged (regression) --------
    check("python -m pytest x.py -q", ["python -m pytest x.py -q"])
    check("cd /testbed && python -m pytest x.py -q",
          ["cd /testbed", "python -m pytest x.py -q"])
    check("python convert.py && ./verify.sh && ls -l out.parquet",
          ["python convert.py", "./verify.sh", "ls -l out.parquet"])
    check("make check; echo done", ["make check", "echo done"])
    check("a && b || c", None)                       # || semantics
    check("python app.py | od -An -t x1", None)      # pipe
    check("python app.py > out.txt && cat out.txt", None)  # redirect
    check("echo `date` && ls", None)                 # backtick
    check("x=$(ls) && echo $x", None)                # substitution
    check("python - <<'PY'\nprint(1)\nPY", None)     # heredoc
    check("FOO=1 python app.py && ls", None)         # env-assignment prefix
    check("(cd x && make) && ls", None)              # subshell part
    check("echo 'a && b' && ls", ["echo 'a && b'", "ls"])  # quoted && kept
    check("sleep 1 &", None)                         # backgrounding
    check('grep -n "a;b" f.txt && wc -l f.txt',
          ['grep -n "a;b" f.txt', "wc -l f.txt"])    # quoted ; kept

    parts, cwd = fold_cd(split_compound(
        "cd /testbed && python -m pytest x.py -q"), "/")
    ok = cwd == "/testbed" and [p for p, _ in parts] == ["python -m pytest x.py -q"]
    print(f"{OK if ok else BAD} cd-fold -> cwd={cwd} parts={[p for p, _ in parts]}")
    good &= ok

    # -------- serve-side split --------
    def scheck(cmd, want):
        global good
        got = split_for_serve(cmd)
        view = [(p, s, v) for p, s, v in got] if got else None
        mark = OK if view == want else BAD
        good &= view == want
        print(f"{mark} serve {cmd[:52]!r:<56} -> {view}")

    # the live astropy case: pipe confined in part 3 no longer poisons parts 1-2
    scheck("git diff --check; git diff --stat; git diff HEAD | sed -n '1,9p'; pytest -q",
           [("git diff --check", False, True),
            ("git diff --stat", False, True),
            ("git diff HEAD | sed -n '1,9p'", False, False),
            ("pytest -q", True, True)])
    # && joiner flags carried per boundary
    scheck("pytest x -q && pip install y > log.txt",
           [("pytest x -q", True, True),
            ("pip install y > log.txt", True, False)])
    # quoted hazard chars stay servable
    scheck("echo 'a | b' && ls",
           [("echo 'a | b'", True, True), ("ls", True, True)])
    # shell-state parts split but never serve
    scheck("export FOO=1 && make",
           [("export FOO=1", True, False), ("make", True, True)])
    scheck("FOO=1; make $FOO",
           [("FOO=1", False, False), ("make $FOO", True, True)])
    # subshell part: split kept, part not servable
    scheck("(cd x && make) && ls",
           [("(cd x && make)", True, False), ("ls", True, True)])
    # structural hazards still refuse whole
    scheck("python - <<'PY'\nprint(1)\nPY", None)
    scheck("echo `date` && ls", None)
    scheck("a && b || c", None)
    scheck("sleep 1 &", None)
    # non-leading cd survives the split (daemon ends prefix there)
    scheck("pytest x && cd sub && make",
           [("pytest x", True, True), ("cd sub", True, False),
            ("make", True, True)])

    p3, cwd = fold_cd_serve(split_for_serve(
        "cd /testbed && python -m pytest x.py -q"), "/")
    ok = cwd == "/testbed" and [t for t, _, _ in p3] == ["python -m pytest x.py -q"]
    print(f"{OK if ok else BAD} serve cd-fold -> cwd={cwd} parts={[t for t, _, _ in p3]}")
    good &= ok

    r = rejoin(split_for_serve("a1; b2 && c3")[1:])
    ok = r == "b2 && c3"
    print(f"{OK if ok else BAD} rejoin tail -> {r!r}")
    good &= ok

    ok = is_state_cmd("cd /x") and is_state_cmd("export A=1") and \
        is_state_cmd(". env/bin/activate") and not is_state_cmd("settle.py --run") \
        and not is_state_cmd("setup.py") and is_cd_cmd("cd ..") and not is_cd_cmd("cdparanoia x")
    print(f"{OK if ok else BAD} state/cd predicates")
    good &= ok

    raise SystemExit(0 if good else 1)
