#!/usr/bin/env python3
"""spec_families.py — semantic command families for speculation matching.

Exact-string cache matching yields ~0% hits against real agents: they emit
targeted commands (`python -m pytest path/to/test.py::test_x -q`), never the
canonical seeds. Family matching normalizes commands within a recognized
family to a canonical key so a speculatively pre-run result can serve a
differently-phrased but semantically identical query.

First family: pytest. Key = (sorted target list, output-affecting flag
profile). Two invocations map to the same key iff they run the same tests
with the same output format — the only case where serving cached output is
faithful.

Conservative by design: anything not confidently normalized returns None and
falls through to real execution. A miss costs nothing; a wrong hit corrupts
the agent's observation stream.
"""

import hashlib
import shlex

# flags that change WHICH tests run or WHAT output looks like -> part of key
OUTPUT_FLAGS = {"-q", "-qq", "-v", "-vv", "--tb=short", "--tb=long", "--tb=line",
                "--tb=no", "-x", "--no-header", "-rN", "-ra", "-rf"}
# flags that are safe to ignore (don't change output for a passing/failing run)
IGNORABLE = {"--color=no", "--color=yes", "-p", "no:cacheprovider"}
# anything else unrecognized -> refuse to normalize (conservative)



def strip_env_prefix(parts):
    """Drop leading VAR=value assignments from an argv list."""
    import re
    i = 0
    while i < len(parts) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[i]):
        i += 1
    return parts[i:]

def normalize_pytest(cmd: str):
    """Return a canonical family key for a pytest invocation, or None.

    Recognizes:  pytest ARGS | python -m pytest ARGS | python3 -m pytest ARGS
    Only single simple commands (no &&, ;, |) are eligible.
    """
    if any(tok in cmd for tok in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return None
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    parts = strip_env_prefix(parts)
    if not parts:
        return None
    if parts[0] in ("python", "python3") and parts[1:3] == ["-m", "pytest"]:
        args = parts[3:]
    elif parts[0] == "pytest":
        args = parts[1:]
    else:
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
    key_src = "pytest\x00" + "\x00".join(sorted(targets)) + "\x00" + ",".join(sorted(flags))
    return hashlib.sha256(key_src.encode()).hexdigest()


def normalize_django(cmd: str):
    """Django's own runner: `python tests/runtests.py <labels> [--verbosity N]`.
    Labels are dotted module paths (dbshell.test_postgresql), not file paths.
    Key = (sorted labels, verbosity)."""
    if any(tok in cmd for tok in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return None
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    parts = strip_env_prefix(parts)
    if len(parts) < 3 or parts[0] not in ("python", "python3"):
        return None
    if not parts[1].endswith("runtests.py"):
        return None
    labels, verbosity = [], "1"
    i = 2
    while i < len(parts):
        a = parts[i]
        if a in ("--verbosity", "-v"):
            i += 1
            verbosity = parts[i] if i < len(parts) else "1"
        elif a.startswith("--verbosity="):
            verbosity = a.split("=", 1)[1]
        elif a.startswith("-"):
            return None  # unknown flag: refuse
        else:
            labels.append(a)
        i += 1
    if not labels:
        return None
    key_src = "djrun\x00" + "\x00".join(sorted(labels)) + "\x00v" + verbosity
    return hashlib.sha256(key_src.encode()).hexdigest()


def parse_command(cmd: str):
    """Structured parse for near-miss scoring. Returns
    {family, targets, key} or None. `targets` are comparable units:
    pytest -> paths (node ids split at ::), django -> labels."""
    if any(tok in cmd for tok in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return None
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    parts = strip_env_prefix(parts)
    if not parts:
        return None
    if (parts[0] in ("python", "python3") and parts[1:3] == ["-m", "pytest"]) \
            or parts[0] == "pytest":
        args = parts[3:] if parts[0] != "pytest" else parts[1:]
        targets = [a for a in args if not a.startswith("-")]
        return {"family": "pytest", "targets": targets,
                "key": normalize_pytest(cmd)}
    if len(parts) >= 3 and parts[0] in ("python", "python3") \
            and parts[1].endswith("runtests.py"):
        targets = [a for a in parts[2:]
                   if not a.startswith("-") and not a.isdigit()]
        return {"family": "django", "targets": targets,
                "key": normalize_django(cmd)}
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
        ("python -m pytest -x -q", False),               # no target
        ("python -m pytest a.py && git diff", False),     # compound
        ("python -m pytest a.py --unknown-flag", False),  # unknown flag
    ]
    for cmd, expect in tests:
        got = family_key(cmd) is not None
        print(("OK " if got == expect else "FAIL"), cmd, "->", got)
    a = family_key("python -m pytest x/test_a.py -q")
    b = family_key("pytest -q x/test_a.py")
    print("OK " if a == b else "FAIL", "phrasing-invariance")
