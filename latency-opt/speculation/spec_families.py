#!/usr/bin/env python3
"""spec_families.py — semantic command families for speculation matching.

Exact-string cache matching yields ~0% hits against real agents: they emit
targeted commands (`python -m pytest path/to/test.py::test_x -q`), never the
canonical seeds. Family matching normalizes commands within a recognized
family to a canonical key so a speculatively pre-run result can serve a
differently-phrased but semantically identical query.

Key = (interpreter identity, sorted target list, output-affecting flag
profile). Two invocations map to the same key iff they run the same tests in
the same environment with the same output format — the only case where
serving cached output is faithful.

v2 (spec-generalize-v1), driven by the 10-repo parser audit:
  * interpreter variants: `python3.9 -m pytest`, `/path/to/venv/bin/python
    -m pytest`, `/path/pytest` are now recognized (26 audit gaps). The
    interpreter is folded into the key when non-canonical: a venv python is
    a DIFFERENT environment, so it gets its own equivalence class rather
    than being normalized away — cross-env serving would be a correctness
    bug. Heads containing `$` (unexpanded shell vars) are refused: two
    different var values would collide on one key.
  * leading `env` prefix stripped, same policy as bare VAR=val prefixes
    (11 audit gaps: `env PYTHONPATH=. python tests/runtests.py ...`).
  * parse_command tolerates trailing redirections (`>f`, `2>&1`) for
    SCORING, returning key=None: the command is comparable for predictor
    eval but never servable — serving would skip creating the file the
    agent's next command reads.

Conservative by design: anything not confidently normalized returns None and
falls through to real execution. A miss costs nothing; a wrong hit corrupts
the agent's observation stream.
"""

import hashlib
import os
import re
import shlex

# flags that change WHICH tests run or WHAT output looks like -> part of key
OUTPUT_FLAGS = {"-q", "-qq", "-v", "-vv", "--tb=short", "--tb=long", "--tb=line",
                "--tb=no", "-x", "--no-header", "-rN", "-ra", "-rf"}
# flags that are safe to ignore (don't change output for a passing/failing run)
IGNORABLE = {"--color=no", "--color=yes", "-p", "no:cacheprovider"}
# anything else unrecognized -> refuse to normalize (conservative)

_PY_BASE = re.compile(r"python[0-9.]*$")
_COMPOUND = ("&&", "||", ";", "|", "`", "$(")
_REDIR = re.compile(r"\s+(?:\d?>{1,2}\s*\S+|2>&1|<\s*\S+)")


def strip_env_prefix(parts):
    """Drop a leading `env` command and/or VAR=value assignments."""
    i = 0
    while i < len(parts):
        if parts[i] == "env":
            i += 1
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[i]):
            i += 1
        else:
            break
    return parts[i:]


def _split_runner(parts):
    """Classify the runner head. Returns (mode, interp_id, args) where mode
    is 'pytest' | 'django' | None and interp_id is '' for the canonical
    in-workspace interpreter (python/python3/bare pytest) or the literal
    head for any other interpreter (versioned, absolute path, venv)."""
    if not parts:
        return None, None, None
    head = parts[0]
    if "$" in head:
        return None, None, None          # unexpanded var: unresolvable env
    base = os.path.basename(head)
    if base == "pytest":
        interp = "" if head == "pytest" else head
        return "pytest", interp, parts[1:]
    if _PY_BASE.fullmatch(base):
        interp = "" if head in ("python", "python3") else head
        if parts[1:3] == ["-m", "pytest"]:
            return "pytest", interp, parts[3:]
        if len(parts) >= 3 and parts[1].endswith("runtests.py"):
            return "django", interp, parts[2:]
    return None, None, None


def _prepare(cmd, allow_redirect=False):
    """Common front end: redirection handling, compound refusal, shlex,
    env-prefix strip. Returns (parts, had_redirect) or (None, False)."""
    had_redirect = False
    if allow_redirect:
        stripped = _REDIR.sub("", cmd)
        had_redirect = stripped != cmd
        cmd = stripped
    if any(tok in cmd for tok in _COMPOUND) or (">" in cmd) or ("<" in cmd):
        return None, False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None, False
    return strip_env_prefix(parts), had_redirect


def normalize_pytest(cmd: str):
    """Canonical family key for a pytest invocation, or None.
    Recognizes: pytest | python[X.Y] -m pytest | /path/python -m pytest,
    with optional env/VAR= prefixes. Single simple commands only."""
    parts, _ = _prepare(cmd)
    if not parts:
        return None
    mode, interp, args = _split_runner(parts)
    if mode != "pytest":
        return None

    targets, flags = [], set()
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in OUTPUT_FLAGS:
                flags.add(a)
            elif a in IGNORABLE:
                if a == "-p":
                    i += 1  # consume plugin arg
            else:
                return None  # unknown flag: refuse
        else:
            targets.append(a)
        i += 1
    if not targets:
        return None  # bare suite runs are workspace-wide; too broad to serve
    key_src = ("pytest\x00" + interp + "\x01"
               + "\x00".join(sorted(targets)) + "\x00" + ",".join(sorted(flags)))
    return hashlib.sha256(key_src.encode()).hexdigest()


def normalize_django(cmd: str):
    """Django's own runner: `python tests/runtests.py <labels> [--verbosity N]`.
    Labels are dotted module paths (dbshell.test_postgresql), not file paths.
    Key = (interpreter, sorted labels, verbosity)."""
    parts, _ = _prepare(cmd)
    if not parts:
        return None
    mode, interp, args = _split_runner(parts)
    if mode != "django":
        return None
    labels, verbosity = [], "1"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--verbosity", "-v"):
            i += 1
            verbosity = args[i] if i < len(args) else "1"
        elif a.startswith("--verbosity="):
            verbosity = a.split("=", 1)[1]
        elif a.startswith("-"):
            return None  # unknown flag: refuse
        else:
            labels.append(a)
        i += 1
    if not labels:
        return None
    key_src = ("djrun\x00" + interp + "\x01"
               + "\x00".join(sorted(labels)) + "\x00v" + verbosity)
    return hashlib.sha256(key_src.encode()).hexdigest()


# flags whose VALUE is a separate argv token: the value is not a target.
# (spec-score-v1: `pytest -k separable a/test_x.py` used to yield the
# phantom target 'separable', corrupting scoring and near-miss telemetry)
_PYTEST_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-W", "-n", "-c", "--tb",
                       "--deselect", "--ignore", "--maxfail", "--rootdir",
                       "--confcutdir", "--junitxml", "--durations",
                       "--cov", "--dist", "--timeout", "-r"}
_DJANGO_VALUE_FLAGS = {"-v", "--verbosity", "--settings", "--parallel",
                       "--exclude-tag", "--tag"}


def _positional_targets(args, value_flags):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in value_flags:
                i += 1                  # skip the flag's value token
        else:
            out.append(a)
        i += 1
    return out


def parse_command(cmd: str):
    """Structured parse for near-miss scoring. Returns
    {family, targets, key, interp, redirected} or None. `targets` are
    comparable units: pytest -> paths/node-ids, django -> labels.
    Trailing redirections are tolerated for scoring; a redirected command
    always has key=None (comparable, never servable)."""
    parts, had_redirect = _prepare(cmd, allow_redirect=True)
    if not parts:
        return None
    mode, interp, args = _split_runner(parts)
    if mode == "pytest":
        targets = _positional_targets(args, _PYTEST_VALUE_FLAGS)
        key = None if had_redirect else normalize_pytest(cmd)
        return {"family": "pytest", "targets": targets, "key": key,
                "interp": interp, "redirected": had_redirect}
    if mode == "django":
        targets = [a for a in _positional_targets(args, _DJANGO_VALUE_FLAGS)
                   if not a.isdigit()]
        key = None if had_redirect else normalize_django(cmd)
        return {"family": "django", "targets": targets, "key": key,
                "interp": interp, "redirected": had_redirect}
    return None


def family_key(cmd: str):
    """Dispatch across known families. Returns key or None."""
    return normalize_pytest(cmd) or normalize_django(cmd)


if __name__ == "__main__":
    tests = [
        ("python -m pytest astropy/modeling/tests/test_separable.py", True),
        ("python -m pytest astropy/modeling/tests/test_separable.py -q", True),
        ("pytest astropy/modeling/tests/test_separable.py -q", True),
        ("python -m pytest a/test_x.py::test_one -q", True),
        ("python -m pytest -x -q", False),                # no target
        ("python -m pytest a.py && git diff", False),     # compound
        ("python -m pytest a.py --unknown-flag", False),  # unknown flag
        # v2 additions
        ("python3.9 -m pytest a/test_x.py -q", True),                 # versioned
        ("/tmp/venv/bin/python -m pytest a/test_x.py -q", True),      # abs path
        ("env PYTHONPATH=. python tests/runtests.py dbshell", True),  # env prefix
        ("PYTHONPATH=. python tests/runtests.py dbshell.test_pg", True),
        ("$venv/bin/python -m pytest a/test_x.py", False),            # $ head
        ("python -m pytest a/test_x.py -q > /tmp/log 2>&1", False),   # redirect: no key
    ]
    ok = True
    for cmd, expect in tests:
        got = family_key(cmd) is not None
        ok &= (got == expect)
        print(("OK " if got == expect else "FAIL"), cmd, "->", got)
    a = family_key("python -m pytest x/test_a.py -q")
    b = family_key("pytest -q x/test_a.py")
    print("OK " if a == b else "FAIL", "phrasing-invariance")
    ok &= (a == b)
    # env separation: venv key must differ from workspace key
    c = family_key("/tmp/venv/bin/python -m pytest x/test_a.py -q")
    print("OK " if (c and c != a) else "FAIL", "interp-in-key (no cross-env serve)")
    ok &= bool(c and c != a)
    # redirected command: scoreable, unservable
    p = parse_command("python -m pytest x/test_a.py -q > /tmp/l 2>&1")
    good = p and p["family"] == "pytest" and p["targets"] == ["x/test_a.py"] \
        and p["key"] is None and p["redirected"]
    print("OK " if good else "FAIL", "redirect: parse-without-key")
    ok &= bool(good)
    # env-prefix django parses with dotted label
    p = parse_command('env PYTHONPATH="$PWD" python tests/runtests.py '
                      'dbshell.test_postgresql --verbosity 2')
    good = p and p["family"] == "django" and p["targets"] == ["dbshell.test_postgresql"]
    print("OK " if good else "FAIL", "env-prefix django parse")
    ok &= bool(good)
    raise SystemExit(0 if ok else 1)
