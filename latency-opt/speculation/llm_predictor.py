#!/usr/bin/env python3
"""llm_predictor.py — LLM-based first-command prediction (build 4).

Predicts the first test invocation(s) an agent will run for a task, by
asking a cheap model (codex-low by default) to read the same problem
statement the agent gets, plus a listing of the repo's test files. Where the
regex heuristic needs string overlap, a model can make the semantic hop
(NdarrayMixin -> test_mixin.py; "postgresql client" -> dbshell label).

Interface matches predictor_eval's --predictor contract:
    predict(workspace: Path, problem_statement: str) -> list[str]
so the SAME offline scorer that produced the heuristic's 0.400 baseline
scores this predictor, and the same worker flag runs it live.

Backend: shells out to the codex CLI in read-only sandbox for one short
turn. Knobs (env):
    SPEC_LLM_BIN    binary            (default: codex)
    SPEC_LLM_ARGS   extra args        (default: "-c model_reasoning_effort=low")
    SPEC_LLM_TIMEOUT seconds          (default: 120)
Token usage is extracted from the JSON stream and returned via predict_meta /
appended to the ledger when called through the worker.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_families import parse_command  # noqa: E402

MAX_TEST_FILES = 120


def _test_file_listing(ws: Path) -> str:
    """Bounded listing of test files / django labels to ground the model."""
    lines = []
    runtests = ws / "tests" / "runtests.py"
    if runtests.exists():
        labels = sorted(d.name for d in (ws / "tests").iterdir() if d.is_dir())
        lines.append("Django-style repo. Test runner: python tests/runtests.py <label>")
        lines.append("Available labels: " + ", ".join(labels[:200]))
    else:
        found = []
        for p in ws.rglob("test_*.py"):
            rp = str(p.relative_to(ws))
            if "__pycache__" in rp or "/node_modules/" in rp:
                continue
            found.append(rp)
            if len(found) >= MAX_TEST_FILES:
                break
        lines.append("pytest-style repo. Test files (partial listing):")
        lines.extend(found)
    return "\n".join(lines)


PROMPT_TEMPLATE = """You are predicting the FIRST test command a coding agent will run \
for the software issue below. Do NOT run anything. Do NOT explain.

Output exactly 3 candidate commands, one per line, most likely first, \
in verbatim runnable form (e.g. `python -m pytest path/to/test_file.py -q` \
or `python tests/runtests.py label.module`). Nothing else.

## Issue
{problem}

## Repository test layout
{listing}
"""


def _walk_tokens(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, int) and "token" in k:
                acc[k] = acc.get(k, 0) + v
            else:
                _walk_tokens(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_tokens(v, acc)


def predict_meta(workspace, problem_statement: str):
    """Full-fat entry point: returns (commands, meta) where meta carries
    token usage, latency, and the raw model lines for the ledger."""
    ws = Path(workspace)
    prompt = PROMPT_TEMPLATE.format(
        problem=problem_statement[:6000],
        listing=_test_file_listing(ws)[:6000])
    binary = os.environ.get("SPEC_LLM_BIN", "codex")
    extra = shlex.split(os.environ.get("SPEC_LLM_ARGS",
                                       "-c model_reasoning_effort=low"))
    timeout = float(os.environ.get("SPEC_LLM_TIMEOUT", "120"))
    argv = [binary, "exec", "--json", "--sandbox", "read-only",
            "--skip-git-repo-check", *extra, prompt]
    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return [], {"error": str(e), "latency_s": time.time() - t0}
    tokens, text_parts = {}, []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                text_parts.append(line)
                continue
            _walk_tokens(obj, tokens)
            # harvest any text content fields
            def grab(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("text", "content", "message", "output") and isinstance(v, str):
                            text_parts.append(v)
                        else:
                            grab(v)
                elif isinstance(o, list):
                    for v in o:
                        grab(v)
            grab(obj)
        else:
            text_parts.append(line)
    cmds, seen = [], set()
    for raw in "\n".join(text_parts).splitlines():
        cand = raw.strip().strip("`").lstrip("-*0123456789. ").strip()
        parsed = parse_command(cand)
        if parsed and parsed["targets"] and cand not in seen:
            seen.add(cand)
            cmds.append(cand)
    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text_parts), "exit": proc.returncode}
    return cmds[:3], meta


def predict(workspace, problem_statement: str):
    """predictor_eval-compatible entry point."""
    cmds, meta = predict_meta(workspace, problem_statement)
    print(f"[llm_predictor] {len(cmds)} cmds, tokens={meta.get('tokens')}, "
          f"{meta.get('latency_s')}s", file=sys.stderr)
    return cmds


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--problem-statement", required=True)
    args = ap.parse_args()
    cmds, meta = predict_meta(args.workspace,
                              Path(args.problem_statement).read_text())
    print(json.dumps({"commands": cmds, "meta": meta}, indent=2))
