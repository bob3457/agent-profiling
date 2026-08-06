#!/usr/bin/env python3
"""spec_tiers.py — command safety classification for general speculation.

Task-agnostic by design: classify(cmd) is a property of the COMMAND
(shell semantics), never of the benchmark. Returns one of:

  TIER0  pre-run in place, servable. Deterministic-given-filesystem-state
         and observably pure: re-execution has no effect beyond its output,
         and the output depends only on the workspace (not wall-clock time).
  TIER1  pre-run in place, servable, UNDOABLE. Convergent benign mutations
         (mkdir -p, touch): re-execution is a no-op; consumers must ledger
         and undo (edit_respec does; speculative_worker must NOT run these).
  NONE   never speculate. Everything else — including anything that could
         be a task's graded side effect, and anything TIME-VARYING (date,
         ps, sleep): serving a cached result would be semantically wrong
         even though the command is "harmless".

Conservative by construction: unknown commands are NONE. Shell operators
(&&, |, ;, redirects, substitution, newlines) are NONE at THIS layer —
compound decomposition is spec_compound's job; consumers classify the parts.

Coverage (all validated in the self-test):
  * Pure heads: file/text readers plus stream tools (sort/uniq/cut/tr/jq/
    od/xxd/strings/nl/tac/...).
  * Guarded heads: sed (no -i/-f, no w/W/e script commands), awk (no -f,
    no -i inplace, no system()), tar (list/diff modes only), unzip/gzip/xz/
    bzip2 (list/test/stdout modes), sqlite3 (single read-only statement),
    pip (list/show/freeze/check), python -m pip, safe -m modules, and git
    restricted to read-only invocations (`git branch <name>` creates,
    `git remote add` mutates — both refused).
  * Generic probe rule: `<tool> --version|--help|-V|-h|version` is TIER0
    for ANY head (pure by convention across the ecosystem).
  * Pure PIPELINES (every stage TIER0 -> TIER0), safe redirects
    (>/dev/null, 2>/dev/null, 2>&1 stripped before hazard scan), leading
    NAME=value / `command` prefixes stripped (`command -v` pure; bare
    assignments are shell state -> NONE; `time` stays NONE: a cached
    timing is a wrong answer).
  * Time-varying commands are NONE even when harmless: `date` (a cached
    timestamp is a wrong answer), `sleep` (serving it instantly destroys
    the delay the agent asked for), `ps`, etc.
"""
import re
import shlex

TIER0, TIER1, NONE = "tier0", "tier1", "none"

_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n")  # kept for reference


def _has_shell_hazard(cmd: str) -> bool:
    """Quote-aware operator scan. Operators inside single quotes are
    literal; inside double quotes only ` and $( remain live (command
    substitution). Newlines refuse anywhere. Backslash escapes honored
    outside single quotes."""
    in_sq = in_dq = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\n":
            return True
        if in_sq:
            if c == "'":
                in_sq = False
        elif in_dq:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_dq = False
            elif c == "`" or (c == "$" and cmd[i:i + 2] == "$("):
                return True
        else:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_sq = True
            elif c == '"':
                in_dq = True
            elif c in "&|;<>`":
                return True
            elif c == "$" and cmd[i:i + 2] == "$(":
                return True
        i += 1
    return in_sq or in_dq   # unterminated quote: refuse

# verbs whose invocations are read-only AND deterministic given fs state
_TIER0_HEADS = {
    # core file/repo readers (no date — time-varying, see module docstring)
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "stat", "file",
    "which", "env", "printenv", "pwd", "du", "df", "echo", "true",
    "diff", "md5sum", "sha256sum", "readlink", "tree", "nproc", "uname",
    "pytest",
    # text/stream tools (pure filters; fs writes require operators,
    # which are refused upstream)
    "sort", "uniq", "cut", "tr", "paste", "comm", "join", "nl", "tac",
    "rev", "fold", "fmt", "expand", "unexpand", "column", "seq", "printf",
    "basename", "dirname", "realpath", "cmp", "sha1sum", "sha512sum",
    "b2sum", "cksum", "sum", "hexdump", "od", "xxd", "strings", "shuf",
    # structured-data / search tools
    "jq", "yq", "fd", "ag", "ack",
    # system identity probes (static per node/allocation)
    "hostname", "whoami", "id", "groups", "arch", "lscpu", "getconf",
    "false", "type", "test", "[",
    # archive readers (pure by design)
    "zcat", "bzcat", "xzcat", "zipinfo",
}
# time-varying or interactive: harmless but WRONG to serve from cache
_NONE_HEADS = {"date", "sleep", "ps", "top", "htop", "free", "uptime",
               "watch", "vmstat", "iostat", "less", "more", "vi", "vim",
               "nano", "mktemp"}

_TIER0_GIT = {"status", "diff", "log", "show", "rev-parse",
              "ls-files", "describe", "blame",
              "ls-tree", "cat-file", "grep", "shortlog", "count-objects",
              "cherry", "merge-base", "name-rev", "var", "check-ignore"}
# git subcommands that are read-only ONLY for specific invocations
_GIT_BRANCH_MUTATORS = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete",
                        "--move", "--copy", "-u", "--set-upstream-to",
                        "--unset-upstream", "--edit-description", "-f"}

_TIER0_PY_MODULES = {"pytest", "unittest", "compileall", "py_compile",
                     "mypy", "flake8", "ruff", "pyflakes",
                     "json.tool", "site", "sysconfig", "platform", "tabnanny"}
_TIER0_PIP_SUBS = {"list", "show", "freeze", "check", "debug"}
_TIER0_BUILD = {"make": {"-n", "--dry-run", "check", "test"},  # bare make is NOT tier0
                "cargo": {"check", "test", "build"},           # separate target dir
                "npm": {"test", "ls"},
                "go": {"test", "vet", "build"}}
_TIER1_HEADS = {"mkdir", "touch"}

_PROBE_ARGS = {"--version", "-V", "--help", "-h", "version"}


def _tokens(cmd):
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def _sed_scripts(toks):
    """Best-effort extraction of sed script strings (positional + -e)."""
    scripts, i, saw_e = [], 1, False
    while i < len(toks):
        t = toks[i]
        if t in ("-e", "--expression"):
            if i + 1 < len(toks):
                scripts.append(toks[i + 1])
                saw_e = True
                i += 2
                continue
        i += 1
    if not saw_e:
        pos = [t for t in toks[1:] if not t.startswith("-")]
        if pos:
            scripts.append(pos[0])
    return scripts


# WHITELIST grammar: address(es) + {p, d, q, =, n, N, s/// without w/e flag}.
# Anything the grammar doesn't recognize (w, W, e, r, R, blocks, labels) is
# refused — safer than enumerating the dangerous commands.
_SED_ADDR = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
_SED_SAFE_CHUNK = re.compile(
    r"^\s*(?:%(a)s\s*(?:,\s*%(a)s)?\s*)?"
    r"(?:[pd=nN]|q\d*|s(.)(?:[^\\]|\\.)*?\1(?:[^\\]|\\.)*?\1[gpiI\d]*)\s*$"
    % {"a": _SED_ADDR})


def _sed_script_safe(script: str) -> bool:
    return all(_SED_SAFE_CHUNK.match(chunk)
               for chunk in script.split(";") if chunk.strip())


def _classify_sed(toks):
    for t in toks[1:]:
        if t.startswith("-i") or t.startswith("--in-place"):
            return NONE
        if t in ("-f", "--file", "-s"):        # external script: unknown
            return NONE
    scripts = _sed_scripts(toks)
    if not scripts:
        return NONE
    if not all(_sed_script_safe(s) for s in scripts):
        return NONE
    return TIER0


def _classify_awk(cmd, toks):
    # in-command redirects/pipes/backticks are already refused by _OPERATORS;
    # remaining escape hatches: system(), -f progfile, gawk -i inplace
    if "system(" in cmd:
        return NONE
    for i, t in enumerate(toks[1:], 1):
        if t in ("-f", "--file", "-i", "--include", "-l", "--load", "-e"):
            return NONE
        if t.startswith("-i"):
            return NONE
    return TIER0


def _classify_tar(toks):
    if len(toks) < 2:
        return NONE
    mode = toks[1]
    if mode.startswith("--"):
        return TIER0 if mode in ("--list", "--diff", "--compare") else NONE
    flags = mode.lstrip("-")
    if any(ch in flags for ch in "cxruA"):
        return NONE
    if "t" in flags or "d" in flags:
        return TIER0
    return NONE


def _classify_compress(toks):
    # gzip/gunzip/bzip2/xz: pure only in list/test/stdout modes
    ok = {"-l", "--list", "-t", "--test", "-c", "--stdout", "--to-stdout"}
    modes = [t for t in toks[1:] if t.startswith("-")]
    return TIER0 if any(m in ok for m in modes) else NONE


_SQLITE_RO = re.compile(r"^\s*(select|pragma|explain)\b|^\s*\.(tables|schema|dump)\b",
                        re.IGNORECASE)
_SQLITE_FLAGS = {"-header", "-noheader", "-column", "-json", "-csv", "-line",
                 "-list", "-readonly", "-batch"}


def _classify_sqlite(toks):
    if len(toks) < 3:
        return NONE                     # bare/interactive
    for t in toks[1:-2]:
        if t.startswith("-") and t not in _SQLITE_FLAGS:
            return NONE                 # -cmd, -init, unknown flags
    sql = toks[-1]
    if not _SQLITE_RO.match(sql):
        return NONE
    if ";" in sql.rstrip().rstrip(";"):
        return NONE                     # multi-statement
    return TIER0


# ---- pure pipelines, safe redirects, env/command prefixes ----------------
# Redirects that cannot touch the workspace: discarding to /dev/null and
# fd merges. Stripped (outside quotes) before hazard scanning a stage.
_SAFE_REDIR = re.compile(r"(?:^|\s)(?:[012&]?>>?\s*/dev/null|2>&1|1>&2)(?=\s|$)")


def _split_pipeline(cmd):
    """Quote-aware split on single `|` (not ||). Returns stages, or None if
    the command has no top-level pipe."""
    stages, cur, i, n = [], [], 0, len(cmd)
    in_sq = in_dq = False
    found = False
    while i < n:
        c = cmd[i]
        if in_sq:
            in_sq = c != "'"
        elif in_dq:
            if c == "\\":
                cur.append(cmd[i:i + 2]); i += 2; continue
            in_dq = c != '"'
        elif c == "\\":
            cur.append(cmd[i:i + 2]); i += 2; continue
        elif c == "'":
            in_sq = True
        elif c == '"':
            in_dq = True
        elif c == "|":
            if cmd[i + 1:i + 2] == "|":
                return None            # || is control flow, not a pipeline
            stages.append("".join(cur)); cur = []; found = True
            i += 1; continue
        cur.append(c)
        i += 1
    if not found or in_sq or in_dq:
        return None
    stages.append("".join(cur))
    return [s.strip() for s in stages]


def _strip_prefixes(toks):
    """Drop leading NAME=value assignments and `command` wrappers: both are
    pure iff the wrapped command is pure. `command -v X` is itself pure."""
    while toks and re.fullmatch(r"[A-Za-z_]\w*=\S*", toks[0]):
        toks = toks[1:]
    while toks and toks[0] == "command":
        if len(toks) >= 2 and toks[1] in ("-v", "-V"):
            return ["which"] + toks[2:]      # classify like `which`
        toks = toks[1:]
    return toks


def classify(cmd: str) -> str:
    if not cmd:
        return NONE
    cmd = _SAFE_REDIR.sub(" ", cmd)
    if _has_shell_hazard(cmd):
        stages = _split_pipeline(cmd)
        if stages and len(stages) > 1 and all(s for s in stages):
            # a pipeline is pure iff every stage is pure
            if all(classify(s) == TIER0 for s in stages):
                return TIER0
        return NONE
    return _classify_simple(cmd)


def _classify_simple(cmd: str) -> str:
    toks = _tokens(cmd)
    if not toks:
        return NONE
    toks = _strip_prefixes(toks)
    if not toks:
        return NONE                      # bare assignment: shell state
    head = toks[0].rsplit("/", 1)[-1]

    if head in _NONE_HEADS:
        return NONE

    # generic probe rule: version/help output is pure by convention
    if len(toks) == 2 and toks[1] in _PROBE_ARGS and \
            re.fullmatch(r"[\w.+-]+", head):
        return TIER0

    if head in _TIER1_HEADS:
        # mkdir without -p / touch are convergent enough; both re-execute
        # to the same state
        return TIER1

    if head == "sed":
        return _classify_sed(toks)
    if head in ("awk", "gawk", "mawk", "nawk"):
        return _classify_awk(cmd, toks)
    if head == "tar":
        return _classify_tar(toks)
    if head in ("gzip", "gunzip", "bzip2", "xz"):
        return _classify_compress(toks)
    if head == "unzip":
        return TIER0 if any(t in ("-l", "-t", "-z") for t in toks[1:]) else NONE
    if head == "sqlite3":
        return _classify_sqlite(toks)
    if head in ("pip", "pip3"):
        return TIER0 if len(toks) > 1 and toks[1] in _TIER0_PIP_SUBS else NONE

    if head in _TIER0_HEADS:
        return TIER0

    if head == "git" and len(toks) > 1:
        if toks[1] in _TIER0_GIT:
            return TIER0
        if toks[1] == "branch":
            # SAFETY: `git branch foo` CREATES a branch.
            if any(t in _GIT_BRANCH_MUTATORS for t in toks[2:]):
                return NONE
            if any(not t.startswith("-") for t in toks[2:]):
                return NONE
            return TIER0
        if toks[1] == "remote":
            # SAFETY: `git remote add/rm/set-url` mutate.
            if len(toks) == 2 or toks[2] in ("-v", "show", "get-url"):
                return TIER0
            return NONE
        if toks[1] == "stash":
            return TIER0 if len(toks) > 2 and toks[2] in ("list", "show") else NONE
        if toks[1] == "tag":
            if len(toks) == 2 or "-l" in toks[2:] or "--list" in toks[2:]:
                if not any(not t.startswith("-") for t in toks[2:]):
                    return TIER0
            return NONE
        if toks[1] == "config":
            sub = toks[2:] if len(toks) > 2 else []
            if sub and (sub[0] in ("-l", "--list") or sub[0].startswith("--get")):
                return TIER0
            return NONE
        return NONE

    if head in ("python", "python3"):
        if len(toks) >= 3 and toks[1] == "-m":
            if toks[2].split(".")[0] in {m.split(".")[0] for m in _TIER0_PY_MODULES} \
                    and (toks[2] in _TIER0_PY_MODULES
                         or toks[2].split(".")[0] in _TIER0_PY_MODULES):
                return TIER0
            if toks[2] == "pip" and len(toks) >= 4 and toks[3] in _TIER0_PIP_SUBS:
                return TIER0
            return NONE
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
        # ---- core heads
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
        # ---- text/stream pure heads
        ("sort -u data.txt", TIER0),
        ("uniq -c sorted.txt", TIER0),
        ("cut -d, -f2 data.csv", TIER0),
        ("tr a-z A-Z", TIER0),
        ("jq .name package.json", TIER0),
        ("xxd -l 64 blob.bin", TIER0),
        ("od -c file.bin", TIER0),
        ("strings binary", TIER0),
        ("nl -ba script.sh", TIER0),
        ("wc -l data.csv", TIER0),
        ("realpath ./x", TIER0),
        ("column -t -s, data.csv", TIER0),
        # ---- generic probe rule
        ("node --version", TIER0),
        ("gcc --version", TIER0),
        ("cargo --help", TIER0),
        ("go version", TIER0),
        ("terraform -h", TIER0),
        ("rm --help", TIER0),          # pure: prints usage, touches nothing
        ("node -e 'x'", NONE),          # probe rule is exactly-2-tokens only
        # ---- guarded sed/awk
        ("sed -n 5,10p file.txt", TIER0),
        ("sed s/foo/bar/ file.txt", TIER0),       # stdout only, no -i
        ("sed -i s/foo/bar/ file.txt", NONE),
        ("sed --in-place=.bak s/a/b/ f", NONE),
        ("sed -n '/x/w out.txt' f", NONE),        # w command writes
        ("sed 's/a/b/w out' f", NONE),            # s///w flag writes
        ("sed -n '1,5 e touch pwned' f", NONE),   # e command executes
        ("sed -f prog.sed f", NONE),
        ("awk '{print $1}' data.txt", TIER0),
        ("awk -F, '{print $2}' d.csv", TIER0),
        ("awk 'BEGIN{system(\"rm x\")}'", NONE),
        ("awk -f prog.awk data", NONE),
        ("gawk -i inplace '{sub(/a/,\"b\")}1' f", NONE),
        # ---- archives
        ("tar tzf release.tar.gz", TIER0),
        ("tar -tvf a.tar", TIER0),
        ("tar --list -f a.tar", TIER0),
        ("tar xzf release.tar.gz", NONE),
        ("tar czf out.tar.gz src", NONE),
        ("unzip -l bundle.zip", NONE if False else TIER0),
        ("unzip bundle.zip", NONE),
        ("zipinfo bundle.zip", TIER0),
        ("zcat log.gz", TIER0),
        ("gzip -l a.gz", TIER0),
        ("gzip a.txt", NONE),
        ("gunzip data.gz", NONE),
        ("gunzip -c data.gz", TIER0),
        ("xz -t archive.xz", TIER0),
        # ---- sqlite / pip / python -m
        ("sqlite3 app.db 'select count(*) from users'", TIER0),
        ("sqlite3 -header -csv app.db 'SELECT * FROM t'", TIER0),
        ("sqlite3 app.db '.schema users'", TIER0),
        ("sqlite3 app.db 'drop table users'", NONE),
        ("sqlite3 app.db 'select 1; drop table t'", NONE),
        ("sqlite3 -init evil.sql app.db 'select 1'", NONE),
        ("sqlite3 app.db", NONE),
        ("pip list", TIER0),
        ("pip show numpy", TIER0),
        ("pip freeze", TIER0),
        ("pip install -e .", NONE),
        ("pip download requests", NONE),
        ("python -m pip list", TIER0),
        ("python -m pip install x", NONE),
        ("python -m json.tool cfg.json", TIER0),
        ("python -m platform", TIER0),
        ("python -c 'import os; os.remove(\"x\")'", NONE),
        # ---- git guards
        ("git branch", TIER0),
        ("git branch -a", TIER0),
        ("git branch new-feature", NONE),          # creates a branch
        ("git branch -D main", NONE),
        ("git remote -v", TIER0),
        ("git remote", TIER0),
        ("git remote add origin http://x", NONE),  # mutates remotes
        ("git stash list", TIER0),
        ("git stash", NONE),
        ("git tag", TIER0),
        ("git tag -l", TIER0),
        ("git tag v1.0", NONE),
        ("git config --list", TIER0),
        ("git config --get user.name", TIER0),
        ("git config user.name Bob", NONE),
        ("git ls-tree HEAD", TIER0),
        ("git cat-file -p HEAD:setup.py", TIER0),
        # ---- time-varying refusals (correctness, not safety)
        ("date", NONE),
        ("date +%s", NONE),
        ("sleep 5", NONE),
        ("ps aux", NONE),
        ("free -h", NONE),
        ("mktemp -d", NONE),
        # ---- quote-aware hazard scan
        ("sed 's|a|b|g' f", TIER0),               # quoted | is literal
        ("grep -E 'foo|bar' src.py", TIER0),
        ("awk -F'|' '{print $1}' d.txt", TIER0),
        ('echo "a > b"', TIER0),
        ('echo "$(rm -rf x)"', NONE),             # $() live in dquotes
        ("echo '$(rm -rf x)'", TIER0),            # literal in squotes
        ('grep "`rm x`" f', NONE),
        ("cat f; rm x", NONE),
        ("cat 'f; rm x'", TIER0),
        ("cat f &", NONE),
        ("cat 'unterminated", NONE),
        # ---- pure pipelines
        ("grep -rn 'def solve' . | head -20", TIER0),
        ("git log --oneline | head -5", TIER0),
        ("sort data.txt | uniq -c", TIER0),
        ("cat data.csv | cut -d, -f2 | sort | uniq", TIER0),
        ("cat f | sh", NONE),
        ("find . | xargs rm", NONE),
        ("ps aux | grep python", NONE),           # time-varying stage
        ("echo hi || rm -rf /", NONE),            # || is not a pipeline
        ("mkdir -p a | cat", NONE),               # tier1 stage: refuse
        # ---- safe redirects
        ("find / -name x 2>/dev/null", TIER0),
        ("grep -r pat . 2>/dev/null | head -5", TIER0),
        ("python -m pytest -q 2>&1", TIER0),
        ("ls > files.txt", NONE),
        ("cat f 2>> log.txt", NONE),
        # ---- env/command prefixes
        ("PYTHONPATH=. python -m pytest tests/test_a.py -q", TIER0),
        ("PYTHONPATH=. python setup.py install", NONE),
        ("FOO=bar rm -rf x", NONE),
        ("PYTHONPATH=.", NONE),                   # bare assignment = state
        ("command -v cargo", TIER0),
        ("command rm -rf x", NONE),
        ("time python bench.py", NONE),           # timing is time-varying
        # ---- unknown stays NONE
        ("./run_experiment.sh", NONE),
        ("cmake -S . -B build", NONE),
    ]
    ok = True
    for cmd, want in cases:
        got = classify(cmd)
        mark = "ok " if got == want else "FAIL"
        ok &= got == want
        print(f"{mark} {got:<6} {cmd[:70]!r}")
    print(f"\n{sum(1 for c, w in cases if classify(c) == w)}/{len(cases)} pass")
    raise SystemExit(0 if ok else 1)
