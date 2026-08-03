#!/usr/bin/env python3
"""patch_predictor_generic.py — task-agnostic prediction mode for
llm_predictor.py. Idempotent; verbatim anchors; refuses on drift.

Adds a "generic" mode next to the existing test-command mode:
  * mode selection (env SPEC_PREDICT_MODE=auto|tests|generic, default auto):
    auto -> "tests" iff the workspace has a test surface (runtests.py or
    >=3 test_*.py), else "generic".
  * generic prompt: instruction + a general workspace listing -> "first 5
    shell commands the agent will run", no test-command framing.
  * generic candidate filter: keeps command-shaped lines instead of
    requiring a pytest/django parse (the tier policy downstream is the real
    safety gate); returns up to 5 in generic mode (3 in tests mode).
  * meta gains {"mode": ...} so the ledger records which prompt ran.

Run:  python3 patch_predictor_generic.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
TARGET = ROOT / "latency-opt/speculation/llm_predictor.py"
src = TARGET.read_text()
orig = src


def apply(name, old, new):
    global src
    if new in src:
        print(f"  = {name}: already applied")
        return
    assert old in src, f"ANCHOR DRIFT ({name}): expected bytes not found"
    assert src.count(old) == 1, f"ANCHOR AMBIGUOUS ({name})"
    src = src.replace(old, new)
    print(f"  + {name}")


NEW_DEFS = '''
GENERIC_PROMPT_TEMPLATE = """You are predicting the FIRST shell commands a \\
terminal agent will run for the task below. Do NOT run anything. Do NOT explain.

Output exactly 5 candidate commands, one per line, most likely first, in \\
verbatim runnable form. Prefer simple read-only exploration and verification \\
commands (inspecting files, listing, searching, running existing test or \\
check commands). Nothing else — no numbering, no backticks, no prose.

## Task
{problem}

## Workspace contents
{listing}
"""

_PROSE_HEADS = {"the", "this", "here", "note", "output", "i", "we", "it",
                "first", "then", "a", "an", "to"}


def _workspace_listing(ws: Path, max_entries: int = 150) -> str:
    """General (non-test-shaped) listing: top two levels with sizes."""
    lines, n = [], 0
    skip = {".git", "__pycache__", "node_modules", ".pytest_cache", ".tox"}
    try:
        top = sorted(p for p in ws.iterdir() if p.name not in skip)
    except OSError:
        return "(unreadable workspace)"
    for p in top:
        if n >= max_entries:
            break
        try:
            if p.is_dir():
                lines.append(f"{p.name}/")
                n += 1
                for q in sorted(p.iterdir())[:20]:
                    if q.name in skip or n >= max_entries:
                        continue
                    suffix = "/" if q.is_dir() else f"  ({q.stat().st_size}B)"
                    lines.append(f"  {p.name}/{q.name}{suffix}")
                    n += 1
            else:
                lines.append(f"{p.name}  ({p.stat().st_size}B)")
                n += 1
        except OSError:
            continue
    return "\\n".join(lines) or "(empty workspace)"


def _has_test_surface(ws: Path) -> bool:
    if (ws / "tests" / "runtests.py").exists():
        return True
    found = 0
    try:
        for p in ws.rglob("test_*.py"):
            if "__pycache__" in str(p):
                continue
            found += 1
            if found >= 3:
                return True
    except OSError:
        pass
    return False


def _looks_like_command(c: str) -> bool:
    import re as _re
    if not c or len(c) > 300 or c.startswith("#"):
        return False
    try:
        toks = shlex.split(c)
    except ValueError:
        return False
    if not toks:
        return False
    head = toks[0]
    if head.lower() in _PROSE_HEADS:
        return False
    return bool(_re.fullmatch(r"[A-Za-z0-9_./][\\w./+-]*", head))


'''
apply("generic defs",
      "def _walk_tokens(obj, acc):",
      NEW_DEFS + "def _walk_tokens(obj, acc):")

apply("mode-aware prompt",
      """    ws = Path(workspace)
    prompt = PROMPT_TEMPLATE.format(
        problem=problem_statement[:6000],
        listing=_test_file_listing(ws)[:6000])""",
      """    ws = Path(workspace)
    mode = os.environ.get("SPEC_PREDICT_MODE", "auto")
    if mode == "auto":
        mode = "tests" if _has_test_surface(ws) else "generic"
    if mode == "generic":
        prompt = GENERIC_PROMPT_TEMPLATE.format(
            problem=problem_statement[:6000],
            listing=_workspace_listing(ws)[:6000])
    else:
        prompt = PROMPT_TEMPLATE.format(
            problem=problem_statement[:6000],
            listing=_test_file_listing(ws)[:6000])""")

apply("mode-aware filter",
      """    cmds, seen = [], set()
    for raw in "\\n".join(text_parts).splitlines():
        cand = raw.strip().strip("`").lstrip("-*0123456789. ").strip()
        parsed = parse_command(cand)
        if parsed and parsed["targets"] and cand not in seen:
            seen.add(cand)
            cmds.append(cand)""",
      """    cmds, seen = [], set()
    for raw in "\\n".join(text_parts).splitlines():
        cand = raw.strip().strip("`").lstrip("-*0123456789. ").strip()
        if cand in seen:
            continue
        if mode == "generic":
            if _looks_like_command(cand):
                seen.add(cand)
                cmds.append(cand)
        else:
            parsed = parse_command(cand)
            if parsed and parsed["targets"]:
                seen.add(cand)
                cmds.append(cand)""")

apply("mode-aware cap + meta",
      """    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text_parts), "exit": proc.returncode}
    return cmds[:3], meta""",
      """    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text_parts), "exit": proc.returncode,
            "mode": mode}
    return cmds[:5 if mode == "generic" else 3], meta""")

if src != orig:
    TARGET.write_text(src)
    print(f"wrote {TARGET}")
else:
    print("no changes (all edits already present)")
