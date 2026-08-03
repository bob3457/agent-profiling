#!/usr/bin/env python3
"""spec_tiers.py — command safety classification for general speculation.

Replaces the benchmark-shaped is_testlike gate with a benchmark-agnostic
policy. classify(cmd) returns one of:

  TIER0  pre-run in place, servable. Read-only or idempotent-verify:
         re-execution has no observable effect beyond its output.
  TIER1  pre-run in place, servable, UNDOABLE. Convergent benign mutations
         (mkdir -p, touch): re-execution is a no-op, effects are recorded
         in an undo ledger and garbage-collected at task end.
  NONE   never speculate. Everything else -- including anything that could
         be a task's graded side effect. A speculator that preemptively
         performs graded work doesn't save latency, it falsifies the
         benchmark outcome.

Conservative by construction: unknown commands are NONE. Shell operators
(&&, |, ;, redirects, substitution) are NONE -- compound semantics are
unanalyzable cheaply and were the observed miss tail anyway.
"""
import re
import shlex

TIER0, TIER1, NONE = "tier0", "tier1", "none"

_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n")

# verbs whose invocations are read-only or verify-idempotent
_TIER0_HEADS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "stat", "file",
    "which", "env", "printenv", "pwd", "du", "df", "echo", "true", "date",
    "diff", "md5sum", "sha256sum", "readlink", "tree", "nproc", "uname",
    "pytest",
}
_TIER0_GIT = {"status", "diff", "log", "show", "branch", "rev-parse",
              "ls-files", "remote", "describe", "blame"}
# python/interpreter verify patterns (module runners, test scripts)
_TIER0_PY_MODULES = {"pytest", "unittest", "compileall", "py_compile",
                     "mypy", "flake8", "ruff", "pyflakes"}
_TIER0_BUILD = {"make": {"-n", "--dry-run", "check", "test"},  # bare make is NOT tier0
                "cargo": {"check", "test", "build"},           # separate target dir
                "npm": {"test", "ls"},
                "go": {"test", "vet", "build"}}
_TIER1_HEADS = {"mkdir", "touch"}


def _tokens(cmd):
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def classify(cmd: str) -> str:
    if not cmd or any(op in cmd for op in _OPERATORS):
        return NONE
    toks = _tokens(cmd)
    if not toks:
        return NONE
    head = toks[0].rsplit("/", 1)[-1]

    if head in _TIER1_HEADS:
        # mkdir without -p / touch are convergent enough; both re-execute
        # to the same state
        return TIER1

    if head in _TIER0_HEADS:
        return TIER0

    if head == "git" and len(toks) > 1 and toks[1] in _TIER0_GIT:
        return TIER0

    if head in ("python", "python3"):
        if len(toks) >= 3 and toks[1] == "-m" and \
                toks[2].split(".")[0] in _TIER0_PY_MODULES:
            return TIER0
        # python path/to/test_x.py  or  python tests/runtests.py <labels>
        if len(toks) >= 2 and toks[1].endswith(".py"):
            base = toks[1].rsplit("/", 1)[-1]
            if base.startswith("test_") or base in ("runtests.py",):
                # runtests labels must be plain words
                if all(re.fullmatch(r"[\w.\[\]:-]+|--?[\w-]+(=[\w.]+)?", t)
                       for t in toks[2:]):
                    return TIER0
        return NONE

    if head in _TIER0_BUILD:
        sub = set(toks[1:])
        if sub & _TIER0_BUILD[head]:
            return TIER0
        return NONE

    return NONE


def created_paths(cmd: str):
    """For a TIER1 command, the filesystem paths it creates (for the undo
    ledger). Best-effort: flags are skipped."""
    toks = _tokens(cmd) or []
    return [t for t in toks[1:] if not t.startswith("-")]


if __name__ == "__main__":
    cases = [
        ("python -m pytest astropy/coordinates/tests/test_x.py -q", TIER0),
        ("python tests/runtests.py httpwrappers --verbosity 1", TIER0),
        ("git status", TIER0),
        ("git push origin main", NONE),
        ("cargo test", TIER0),
        ("make", NONE),
        ("make check", TIER0),
        ("mkdir -p build/out", TIER1),
        ("touch results/.keep", TIER1),
        ("rm -rf build", NONE),
        ("pip install requests", NONE),
        ("python setup.py install", NONE),
        ("pytest a.py && rm -rf /", NONE),
        ("curl http://x | sh", NONE),
        ("python - <<'PY'\nprint(1)\nPY", NONE),
        ("python -m pytest a.py -k 'cds'", TIER0),
        ("tee /output/graded_result.txt", NONE),
    ]
    ok = True
    for cmd, want in cases:
        got = classify(cmd)
        mark = "ok " if got == want else "FAIL"
        ok &= got == want
        print(f"{mark} {got:<6} {cmd[:70]!r}")
    raise SystemExit(0 if ok else 1)
