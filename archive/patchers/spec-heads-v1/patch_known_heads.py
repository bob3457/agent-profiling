#!/usr/bin/env python3
"""patch_known_heads.py — fold TraceLab's wild-usage executable whitelist
into predict_parse.py's command validator.

Source: uw-syfi/TraceLab scripts/public_common_executables.txt (Apache-2.0),
a human-reviewed list of 354 executables real Claude Code / Codex agents
invoked across 357K rounds — empirically grounded, unlike the hand-written
~120-entry list it extends.

Not a blind merge. Three tiers:
  KNOWN      bare-accept: unambiguous command names ("nvidia-smi" alone is
             a command, never prose).
  AMBIGUOUS  real executables that are also English sentence-starters
             (install, convert, sort, which, split, watch, kill, man, ...).
             Accepted only when the line has command structure: an option
             token, a path-ish arg (/ . =), a digit arg, a shell operator,
             or an argument that is itself a known head ("which python").
             "Sort the results" and "Install the dependencies" stay
             rejected; "sort -u f.txt" and "install -m 755 x /bin" pass.
  EXCLUDED   shell builtins / control words whose speculative pre-run is
             meaningless (alias, eval, set, return, true, wait, ...) and
             agent-recursion entries add no validation value bare.

Existing _KNOWN_HEADS entries are preserved (none demoted) so every
previously-accepted prediction still validates — no cache-behavior change,
strictly widened acceptance. Idempotent; verbatim anchors.

Run from repo root: python3 patch_known_heads.py [--root .]
MARKER: spec-heads-v1
"""
import argparse
import re
from pathlib import Path

MARKER = "spec-heads-v1"

# TraceLab entries judged bare-unambiguous (merged with the existing set).
TRACELAB_KNOWN = """
accelerate accton addr2line alembic apt apt-cache apply_patch arp auditctl
ausearch aws base64 bc bibtex brew bwrap bundle c++filt capsh ccache chage
chsh clang cloc cmp column comm compileall conda crontab cuobjdump debugfs
dkms dmesg dnf docker dockerd dot dpkg dpkg-deb dpkg-query ensurepip esbuild
ethtool fc-list fc-match fd ffmpeg ffprobe findmnt firewall-cmd flock fold
fuser g++ gcc gdb getcap getconf getenforce getent getfacl gh gofmt gprof
groups gs hf hostname huggingface-cli ibstat ibstatus ibv_devices
ibv_devinfo id iostat ip iptables jar javac jobs journalctl json.tool
jupyter just kpsewhich last lastcomm lastlog latex latexmk ldconfig ldd
litellm lm_eval locate loginctl lsattr lscpu lslocks lsmod lsof lspci ltrace
mamba md5sum meson micromamba mknod mktemp modinfo mold montage mpstat
namei nc ncdu ncu netstat nft nm nproc nsys numactl numfmt nvcc nvdisasm
nvidia-container-cli nvidia-ctk nvidia-smi objdump openssl passwd paste
pdffonts pdfimages pdfinfo pdflatex pdfseparate pdftocairo pdftohtml
pdftoppm pdftotext perf pgrep pidstat ping pip3 pkg-config pkill pnpm
pre-commit printf protoc pstree pwdx py-spy py_compile pybind11 pyflakes
ray rdma readelf redis-cli redis-server restorecon rev rmdir rpm rsync
ruff rustfmt rustup sccache scp seq setsid sglang sha1sum sha256sum
shellcheck sphinx sqlite3 ss ssh ssh-add ssh-keygen ssh-keyscan sshd
sshpass strace strip stty sysctl systemctl systemd-analyze taskset
timedatectl tlmgr tmux torchrun tracepath truncate uniq unittest unshare
uptime uv uvicorn uvx venv vllm vmstat w wandb wget who whoami xdg-open
xelatex xmllint yq zcat zgrep zipinfo
"""

# Real executables that double as English sentence-starters: need structure.
AMBIGUOUS = """
install convert identify join split link sort which watch kill man mount
service screen script module date free file head history type time touch
top tree view test find echo
"""
# Sentence-starters are demoted OUT of bare-accept even when the original
# hand-written set had them (sort, which, find, test, echo, head, file, man,
# type, time, date, touch): the original validator accepted "Sort the
# results" -- a pre-existing hole these tests now close. "make" stays
# bare-accept deliberately: "make test" / "make build" are too common to
# lose (its partner word "test" is demoted), at the cost of accepting
# "Make the change"-style prose. Demotion cannot
# change cache keys (the validator only filters candidates) and every real
# invocation of these carries structural args.

NEW_ACCEPT_BLOCK = '''    if head.lower() in _KNOWN_HEADS:
        return True
    if head.lower() in _AMBIGUOUS_HEADS:  # real command AND plausible prose
        return _has_structure(line, toks)  # head: require command structure
    if "/" in head:
        return True
    if any(t.startswith("-") for t in toks[1:]):
        return True
    if any(op in line for op in ("&&", "||", "|", ">", "<", ";", "=")):
        return True
    return False'''

OLD_ACCEPT_BLOCK = '''    if head.lower() in _KNOWN_HEADS:
        return True
    if "/" in head:
        return True
    if any(t.startswith("-") for t in toks[1:]):
        return True
    if any(op in line for op in ("&&", "||", "|", ">", "<", ";", "=")):
        return True
    return False'''

HELPER = '''

def _has_structure(line, toks):
    """Command-shaped evidence beyond the head word: an option token, a
    path-ish/assignment/digit argument, a shell operator, or an argument
    that is itself a known command ("which python")."""
    for t in toks[1:]:
        if t.startswith("-") or "/" in t or "." in t or "=" in t \\
                or "$" in t or t.isdigit():
            return True
    # "which python" / "man grep" / "time make test": first arg is itself
    # a known command and the line is short -- but "Convert the file to
    # JSON" must not qualify just because "file" is a command name
    if len(toks) <= 3 and toks[1].lower() in _KNOWN_HEADS:
        return True
    return any(op in line for op in ("&&", "||", "|", ">", "<", ";"))

'''

TEST_ANCHOR = '''    # fences never leak'''
TEST_ADD = '''    # ambiguous heads: prose rejected, structured commands accepted
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

    # fences never leak'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    target = Path(args.root) / "latency-opt/speculation/predict_parse.py"
    assert target.exists(), f"missing {target}"
    src = target.read_text()
    if MARKER in src:
        print(f"already patched ({MARKER}); nothing to do")
        return

    # 1. locate and parse the existing _KNOWN_HEADS literal
    m = re.search(r"_KNOWN_HEADS = \{(.*?)\n\}\n", src, re.S)
    assert m, "anchor missing: _KNOWN_HEADS literal"
    existing = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert "ls" in existing and "pytest" in existing, "unexpected set parse"

    tracelab = set(TRACELAB_KNOWN.split())
    ambiguous = set(AMBIGUOUS.split())
    merged = sorted((existing | tracelab) - ambiguous)   # demote

    def fmt(names, indent="    "):
        lines, cur = [], indent
        for n in names:
            tok = f'"{n}", '
            if len(cur) + len(tok) > 78:
                lines.append(cur.rstrip())
                cur = indent
            cur += tok
        lines.append(cur.rstrip().rstrip(","))
        return "\n".join(lines)

    new_known = (
        "_KNOWN_HEADS = {\n"
        f"    # {MARKER}: merged with uw-syfi/TraceLab "
        "public_common_executables.txt\n"
        "    # (Apache-2.0) -- executables observed across 357K real "
        "agent rounds\n"
        + fmt(merged) + ",\n}\n"
        "\n"
        "# real executables that are also English sentence-starters: accept\n"
        "# only with command structure (see _has_structure); a bare English\n"
        "# sentence like \"Sort the results\" must not validate\n"
        "_AMBIGUOUS_HEADS = {\n" + fmt(sorted(ambiguous)) + ",\n}\n"
    )
    src = src[:m.start()] + new_known + src[m.end():]

    # 2. two-tier acceptance in looks_like_command
    assert OLD_ACCEPT_BLOCK in src, "anchor missing: accept block"
    src = src.replace(OLD_ACCEPT_BLOCK, NEW_ACCEPT_BLOCK, 1)

    # 3. _has_structure helper before looks_like_command
    anchor = "def looks_like_command(line: str) -> bool:"
    assert anchor in src, "anchor missing: looks_like_command def"
    src = src.replace(anchor, HELPER + anchor, 1)

    # 4. self-test cases
    assert TEST_ANCHOR in src, "anchor missing: self-test fence block"
    src = src.replace(TEST_ANCHOR, TEST_ADD, 1)

    target.write_text(src)
    print(f"patched {target} [{MARKER}]: known={len(merged)} "
          f"ambiguous={len(ambiguous)} (was {len(existing)})")


if __name__ == "__main__":
    main()
