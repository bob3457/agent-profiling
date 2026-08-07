#!/usr/bin/env python3
"""predict_parse.py — robust parsing of LLM prediction output.

Replaces llm_predictor.py's ad-hoc harvest, which had four measured
failure modes (confirmed offline against the shipped code):

  1. `lstrip("-*0123456789. ")` strips a CHARACTER SET, not a prefix:
     `./verify.sh` -> `/verify.sh`, `7z x a.zip` -> `z x a.zip`.
  2. Markdown fences survive as commands: line "```bash" -> "bash",
     which passes the old validator.
  3. The old validator accepts prose: "Check the README first",
     "Looking at the files", "Run pytest to verify" all returned True.
  4. The generic leaf-walk over --json events harvested reasoning text,
     command output, and prompt echoes, not just the model's answer;
     _walk_tokens SUMMED every int with "token" in its key across every
     event -> token accounting inflated by deltas/cumulative repeats.

Three layers, each independently testable:

  extract_agent_text(stdout)   schema-aware: agent_message items only
                               (mirrors llm_gate._event_text hardening,
                               run 20260731_183452), legacy-msg and
                               plain-text fallbacks, exact-dup dedupe.
  extract_usage(stdout)        LAST usage-shaped dict wins (cumulative
                               totals), never a sum over deltas.
  extract_commands(text, mode) JSON-array first (the v2 prompt asks for
                               one), then fenced-block/bullet-aware line
                               cleaning + a structural command validator.

All pure functions; self-test with `python3 predict_parse.py`.
"""

import json
import re
import shlex

# ------------------------------------------------------------- agent message

def _iter_json_lines(stdout: str):
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def extract_agent_text(stdout: str) -> str:
    """Only the model's own message text from a codex --json stream.

    Primary: `item` events with item.type == "agent_message" (current
    schema). Deltas are skipped; item.updated/item.completed both carrying
    the full text is handled by exact-string dedupe. Secondary: legacy
    `msg.type == "agent_message"` protocol. Reasoning, command_execution,
    lifecycle, and file_change events are never harvested — reasoning is
    where phantom "commands" came from.
    """
    msgs, seen = [], set()

    def add(txt):
        txt = (txt or "").strip()
        if txt and txt not in seen:
            seen.add(txt)
            msgs.append(txt)

    for obj in _iter_json_lines(stdout):
        etype = obj.get("type") or ""
        if "delta" in etype:
            continue
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            add(item.get("text") or item.get("message"))
            continue
        msg = obj.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            add(msg.get("message") or msg.get("text"))
    return "\n".join(msgs)


# ------------------------------------------------------------------- tokens

def _find_usage_dict(obj):
    """First dict whose values include int fields with 'token' in the key."""
    if isinstance(obj, dict):
        tok = {k: v for k, v in obj.items()
               if isinstance(v, int) and "token" in k.lower()}
        if tok:
            return tok
        for v in obj.values():
            r = _find_usage_dict(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_usage_dict(v)
            if r:
                return r
    return None


def extract_usage(stdout: str) -> dict:
    """Token usage: the LAST usage-shaped dict in the stream wins.

    codex emits per-event and cumulative token counts; summing every
    token-keyed int (the old _walk_tokens) double- and triple-counts.
    Cumulative totals are emitted last (turn.completed / token_count
    info.total_token_usage), so last-wins is the correct estimator and
    degrades gracefully to a single per-call usage record.
    """
    usage = {}
    for obj in _iter_json_lines(stdout):
        u = _find_usage_dict(obj)
        if u:
            usage = u
    return usage


# ----------------------------------------------------------------- commands

_FENCE_RE = re.compile(r"^\s*```[\w+-]*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]\s+|\d{1,3}[.)]\s+)")
_TICK_RE = re.compile(r"^`(.+)`$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEAD_RE = re.compile(r"[A-Za-z0-9_./][\w./+-]*")

# heads that begin prose sentences the old validator let through
_PROSE_HEADS = {
    "the", "this", "that", "these", "those", "here", "there", "note",
    "output", "i", "we", "it", "you", "first", "then", "next", "now",
    "a", "an", "to", "run", "runs", "running", "check", "checks", "look",
    "looking", "let", "lets", "use", "using", "try", "if", "please",
    "sure", "okay", "ok", "answer", "command", "commands", "candidate",
    "candidates", "likely", "most", "finally", "also", "and", "or",
    "based", "given", "since", "after", "before", "start", "begin",
    "inspect", "examine", "explore", "list", "read", "review", "open",
    "verify", "ensure", "step",
}

# heads that are unambiguously commands even bare ("ls" alone is valid)
_KNOWN_HEADS = {
    # spec-heads-v1: merged with uw-syfi/TraceLab public_common_executables.txt
    # (Apache-2.0) -- executables observed across 357K real agent rounds
    "7z", "accelerate", "accton", "addr2line", "alembic", "apply_patch",
    "apt", "apt-cache", "arp", "auditctl", "ausearch", "awk", "aws",
    "base64", "basename", "bash", "bc", "bibtex", "black", "brew", "bundle",
    "bwrap", "bzip2", "c++filt", "capsh", "cargo", "cat", "ccache", "cd",
    "chage", "chmod", "chown", "chsh", "clang", "cloc", "cmake", "cmp",
    "column", "comm", "compileall", "composer", "conda", "coverage", "cp",
    "crontab", "cuobjdump", "curl", "cut", "debugfs", "df", "diff",
    "dirname", "dkms", "dmesg", "dnf", "docker", "dockerd", "dot", "dotnet",
    "dpkg", "dpkg-deb", "dpkg-query", "du", "egrep", "ensurepip", "env",
    "esbuild", "ethtool", "fc-list", "fc-match", "fd", "ffmpeg", "ffprobe",
    "fgrep", "findmnt", "firewall-cmd", "flake8", "flock", "fold", "fuser",
    "g++", "gcc", "gdb", "gem", "getcap", "getconf", "getenforce", "getent",
    "getfacl", "gh", "git", "go", "gofmt", "gprof", "gradle", "grep",
    "groups", "gs", "gunzip", "gzip", "hf", "hostname", "huggingface-cli",
    "ibstat", "ibstatus", "ibv_devices", "ibv_devinfo", "id", "iostat", "ip",
    "iptables", "isort", "jar", "java", "javac", "jobs", "journalctl", "jq",
    "json.tool", "jupyter", "just", "kpsewhich", "last", "lastcomm",
    "lastlog", "latex", "latexmk", "ldconfig", "ldd", "litellm", "lm_eval",
    "ln", "locate", "loginctl", "ls", "lsattr", "lscpu", "lslocks", "lsmod",
    "lsof", "lspci", "ltrace", "make", "mamba", "md5sum", "meson",
    "micromamba", "mkdir", "mknod", "mktemp", "modinfo", "mold", "montage",
    "mpstat", "mv", "mvn", "mypy", "namei", "nc", "ncdu", "ncu", "netstat",
    "nft", "nice", "ninja", "nl", "nm", "node", "nox", "npm", "nproc", "npx",
    "nsys", "numactl", "numfmt", "nvcc", "nvdisasm", "nvidia-container-cli",
    "nvidia-ctk", "nvidia-smi", "objdump", "od", "openssl", "passwd",
    "paste", "patch", "pdffonts", "pdfimages", "pdfinfo", "pdflatex",
    "pdfseparate", "pdftocairo", "pdftohtml", "pdftoppm", "pdftotext",
    "perf", "perl", "pgrep", "php", "pidstat", "ping", "pip", "pip3",
    "pkg-config", "pkill", "pnpm", "pre-commit", "printenv", "printf",
    "protoc", "ps", "pstree", "pwd", "pwdx", "py-spy", "py_compile",
    "pybind11", "pyflakes", "pylint", "pytest", "python", "python3", "ray",
    "rdma", "readelf", "readlink", "realpath", "redis-cli", "redis-server",
    "restorecon", "rev", "rg", "rmdir", "rpm", "rsync", "ruby", "ruff",
    "rustc", "rustfmt", "rustup", "sccache", "scp", "sed", "seq", "setsid",
    "sglang", "sh", "sha1sum", "sha256sum", "shellcheck", "sphinx",
    "sqlite3", "ss", "ssh", "ssh-add", "ssh-keygen", "ssh-keyscan", "sshd",
    "sshpass", "stat", "strace", "strings", "strip", "stty", "sysctl",
    "systemctl", "systemd-analyze", "tac", "tail", "tar", "taskset", "tee",
    "timedatectl", "timeout", "tlmgr", "tmux", "torchrun", "tox", "tr",
    "tracepath", "truncate", "uname", "uniq", "unittest", "unrar", "unshare",
    "unzip", "uptime", "uv", "uvicorn", "uvx", "venv", "vllm", "vmstat", "w",
    "wandb", "wc", "wget", "whereis", "who", "whoami", "xargs", "xdg-open",
    "xelatex", "xmllint", "xxd", "xz", "yarn", "yq", "zcat", "zgrep", "zip",
    "zipinfo", "zsh",
}

# real executables that are also English sentence-starters: accept
# only with command structure (see _has_structure); a bare English
# sentence like "Sort the results" must not validate
_AMBIGUOUS_HEADS = {
    "convert", "date", "echo", "file", "find", "free", "head", "history",
    "identify", "install", "join", "kill", "link", "man", "module", "mount",
    "screen", "script", "service", "sort", "split", "test", "time", "top",
    "touch", "tree", "type", "view", "watch", "which",
}


def strip_env_prefix(toks):
    i = 0
    while i < len(toks) and _ENV_RE.match(toks[i]):
        i += 1
    return toks[i:]




def _has_structure(line, toks):
    """Command-shaped evidence beyond the head word: an option token, a
    path-ish/assignment/digit argument, a shell operator, or an argument
    that is itself a known command ("which python")."""
    for t in toks[1:]:
        if t.startswith("-") or "/" in t or "." in t or "=" in t \
                or "$" in t or t.isdigit():
            return True
    # "which python" / "man grep" / "time make test": first arg is itself
    # a known command and the line is short -- but "Convert the file to
    # JSON" must not qualify just because "file" is a command name
    if len(toks) <= 3 and toks[1].lower() in _KNOWN_HEADS:
        return True
    return any(op in line for op in ("&&", "||", "|", ">", "<", ";"))

def looks_like_command(line: str) -> bool:
    """Structural command validator.

    Accept iff, after env-prefix stripping, the head token is (a) a known
    command, or (b) path-like (contains '/' or starts with ./ ../), or
    (c) the line has command-shaped structure (an option token or a shell
    operator). Prose sentences fail all three; bare fence artifacts fail
    because fence lines are removed before this runs and unknown bare
    words have no structure.
    """
    if not line or len(line) > 400 or line.lstrip().startswith("#"):
        return False
    try:
        toks = shlex.split(line)
    except ValueError:
        return False
    toks = strip_env_prefix(toks)
    if not toks:
        return False
    head = toks[0]
    if head.lower() in _PROSE_HEADS:
        return False
    if not _HEAD_RE.fullmatch(head):
        return False
    if head.lower() in _KNOWN_HEADS:
        return True
    if head.lower() in _AMBIGUOUS_HEADS:  # real command AND plausible prose
        return _has_structure(line, toks)  # head: require command structure
    if "/" in head:
        return True
    if any(t.startswith("-") for t in toks[1:]):
        return True
    if any(op in line for op in ("&&", "||", "|", ">", "<", ";", "=")):
        return True
    return False


def _json_array_commands(text: str):
    """Find and parse the first JSON array of strings anywhere in text
    (bare, fenced, or preceded by prose). Returns list or None."""
    dec = json.JSONDecoder()
    idx = 0
    while True:
        idx = text.find("[", idx)
        if idx < 0:
            return None
        try:
            val, _end = dec.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(val, list) and val and \
                all(isinstance(v, str) for v in val):
            return [v.strip() for v in val if v.strip()]
        idx += 1


def clean_line(raw: str) -> str:
    """Prefix-aware cleaning: bullets/numbering removed as PREFIX PATTERNS
    (never a charset lstrip), one backtick-wrap layer unwrapped."""
    s = raw.strip()
    s = _BULLET_RE.sub("", s).strip()
    m = _TICK_RE.match(s)
    if m:
        s = m.group(1).strip()
    return s


def extract_commands(text: str, mode: str = "generic", limit: int = 5,
                     validator=None):
    """Commands from a model answer. JSON array first (deterministic),
    then fence-stripped line parsing. `validator` overrides
    looks_like_command (tests mode passes a family-parse check)."""
    if not text:
        return []
    validate = validator or looks_like_command
    cmds, seen = [], set()

    def take(c):
        if c and c not in seen and validate(c):
            seen.add(c)
            cmds.append(c)

    arr = _json_array_commands(text)
    if arr is not None:
        for c in arr:
            take(clean_line(c))
        return cmds[:limit]

    for raw in text.splitlines():
        if _FENCE_RE.match(raw):
            continue                     # fence delimiters are never commands
        take(clean_line(raw))
    return cmds[:limit]


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    OK, BAD = "ok ", "FAIL"
    good = True

    def check(name, got, want):
        global good
        mark = OK if got == want else BAD
        good &= got == want
        print(f"{mark} {name}: {got!r}" + ("" if got == want
                                           else f"  (want {want!r})"))

    # cleaning: the exact mangles the old code produced
    check("clean ./", clean_line("./verify.sh --all"), "./verify.sh --all")
    check("clean 7z", clean_line("7z x archive.zip"), "7z x archive.zip")
    check("clean numbered", clean_line("1. python -m pytest x.py"),
          "python -m pytest x.py")
    check("clean bullet", clean_line("- git status"), "git status")
    check("clean ticks", clean_line("`ls -la`"), "ls -la")

    # validator: prose out, commands in
    for prose in ("Check the README first", "Looking at the files",
                  "Run pytest to verify", "Here are the commands:",
                  "somethingunknown"):
        check(f"prose rejected: {prose!r}", looks_like_command(prose), False)
    for cmd in ("ls -la", "python -m pytest x.py -q", "./verify.sh",
                "FOO=1 make test", "grep -rn 'def main' src/",
                "7z x archive.zip", "cat README.md"):
        check(f"cmd accepted: {cmd!r}", looks_like_command(cmd), True)

    # ambiguous heads: prose rejected, structured commands accepted
    for prose in ("Install the dependencies", "Sort the results",
                  "Convert the file to JSON", "Which files are affected",
                  "Watch for changes"):
        check(f"ambig prose rejected: {prose!r}",
              looks_like_command(prose), False)
    for cmd in ("sort -u names.txt", "install -m 755 tool /usr/local/bin",
                "which python", "kill 1234", "split -l 100 big.log",
                "nvidia-smi", "cargo build", "docker ps -a", "man grep",
                "find . -name '*.py'", "head -20 setup.py",
                "time make test"):
        check(f"ambig/known cmd accepted: {cmd!r}",
              looks_like_command(cmd), True)

    # fences never leak
    got = extract_commands("```bash\nls -la\ngit status\n```")
    check("fence block", got, ["ls -la", "git status"])
    check("no bash artifact", "bash" in got, False)

    # JSON-first
    got = extract_commands(
        'Sure, here you go:\n```json\n["pytest tests/test_x.py -q", '
        '"git diff", "not a real $%% command...."]\n```')
    check("json array", got, ["pytest tests/test_x.py -q", "git diff"])

    # schema-aware message extraction: reasoning/exec output ignored
    stream = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "reasoning",
                             "text": "I should run rm -rf / first"}}),
        json.dumps({"type": "item.started",
                    "item": {"type": "command_execution",
                             "command": "bash -lc ls"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": "ls -la\npytest tests/test_a.py -q"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 900, "cached_input_tokens": 100,
                              "output_tokens": 40}}),
    ])
    txt = extract_agent_text(stream)
    check("agent text only", txt, "ls -la\npytest tests/test_a.py -q")
    check("usage last-wins", extract_usage(stream),
          {"input_tokens": 900, "cached_input_tokens": 100,
           "output_tokens": 40})

    # duplicate updated/completed events collapse; deltas skipped
    stream2 = "\n".join([
        json.dumps({"type": "item.delta",
                    "item": {"type": "agent_message", "text": "git st"}}),
        json.dumps({"type": "item.updated",
                    "item": {"type": "agent_message", "text": "git status"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "git status"}}),
    ])
    check("dup collapse", extract_agent_text(stream2), "git status")

    # legacy msg schema
    stream3 = json.dumps({"msg": {"type": "agent_message",
                                  "message": "cat setup.py"}})
    check("legacy msg", extract_agent_text(stream3), "cat setup.py")

    raise SystemExit(0 if good else 1)
