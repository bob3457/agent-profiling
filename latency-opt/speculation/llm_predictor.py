#!/usr/bin/env python3
"""llm_predictor.py — LLM-based first-command prediction.

Predicts the first test invocation(s) an agent will run for a task, by
asking a cheap model (codex-low by default) to read the same problem
statement the agent gets, plus a listing of the repo's test files. Where the
regex heuristic needs string overlap, a model can make the semantic hop
(NdarrayMixin -> test_mixin.py; "postgresql client" -> dbshell label).

Interface matches predictor_eval's --predictor contract:
    predict(workspace: Path, problem_statement: str) -> list[str]
so the SAME offline scorer that produced the heuristic's 0.400 baseline
scores this predictor, and the same worker flag runs it live.

Backends (SPEC_LLM_MODE):
  codex (default)  shells out to the codex CLI in read-only sandbox.
    SPEC_LLM_BIN    binary            (default: codex)
    SPEC_LLM_ARGS   extra args        (default: "-c model_reasoning_effort=low")
  openai           POSTs to an OpenAI-compatible endpoint (llama.cpp
    llama-server on the GH200's own GPU: zero tokens, zero quota
    contention with the agent). [spec-localpred-v1]
    SPEC_LLM_ENDPOINT  (default: http://127.0.0.1:8080/v1/chat/completions)
    SPEC_LLM_MODEL     model name for the request body (default: local)
    SPEC_LLM_MAX_TOKENS (default: 400)
  Shared: SPEC_LLM_TIMEOUT seconds    (default: 120)
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


def _test_file_listing(ws: Path, problem_statement: str = "") -> str:
    """Bounded listing of test files / django labels to ground the model.

    [spec-localpred-v2] RELEVANCE-RANKED: the old version truncated in
    rglob walk order, so on large repos (astropy: 400+ test files) the
    correct file was often absent from the prompt entirely — the model was
    asked to choose from a list that didn't contain the answer. Files are
    now scored by token overlap with the problem statement; the cap keeps
    prompts bounded but truncation drops the LEAST relevant files."""
    import re as _re
    lines = []
    runtests = ws / "tests" / "runtests.py"
    if runtests.exists():
        labels = sorted(d.name for d in (ws / "tests").iterdir() if d.is_dir())
        lines.append("Django-style repo. Test runner: python tests/runtests.py <label>")
        lines.append("Available labels: " + ", ".join(labels[:200]))
    else:
        found = []
        for p in ws.rglob("*.py"):
            n = p.name
            # test_*.py | unittest_*.py (pylint) | *_test.py (go-style)
            if not (n.startswith("test_") or n.startswith("unittest_")
                    or n.endswith("_test.py")):
                continue
            rp = str(p.relative_to(ws))
            if "__pycache__" in rp or "/node_modules/" in rp:
                continue
            found.append(rp)
            if len(found) >= 4000:      # walk safety cap, not the prompt cap
                break
        toks = set(_re.findall(r"[a-z0-9]{3,}", problem_statement.lower()))
        def rel(item):
            i, rp = item
            ptoks = set(_re.findall(r"[a-z0-9]{3,}", rp.lower()))
            ptoks -= {"tests", "test", "py"}   # ubiquitous, not signal
            score = len(toks & ptoks)
            # fixture/example trees are real files agents never invoke
            if any(seg in rp.lower() for seg in
                   ("example", "fixture", "testdata", "/data/", "sample")):
                score -= 3
            return (-score, i)                 # overlap desc, walk order tie
        ranked = [rp for _, rp in sorted(enumerate(found), key=rel)]
        lines.append("pytest-style repo. Test files (most relevant first, "
                     "partial listing):")
        lines.extend(ranked[:MAX_TEST_FILES])
    return "\n".join(lines)


def _pred_limit(mode):
    """Parse-time prediction depth. SPEC_PRED_TOPK=N truncates the model's
    ranked list to its top-N valid commands (prompt still asks for 3, so
    offline captures stay replay-comparable; rank-1 is taken verbatim).
    Unset/0 keeps the full default depth (generic=5, tests=3)."""
    limit = 5 if mode == "generic" else 3
    try:
        topk = int(os.environ.get("SPEC_PRED_TOPK", "0") or 0)
    except ValueError:
        topk = 0
    return min(limit, topk) if topk > 0 else limit


PROMPT_TEMPLATE = """You are predicting the FIRST test command a coding agent will run \
for the software issue below. Do NOT run anything. Do NOT explain.

Output a JSON array of exactly 3 strings, most likely first. The 3 commands \
must target DIFFERENT test files or labels, chosen from the listing below, so \
they cover multiple plausible locations for this issue. Each string is one \
verbatim runnable command (e.g. "python -m pytest path/to/test_file.py -q" \
or "python tests/runtests.py label.module"). Output ONLY the JSON array — \
no prose, no markdown fences, no numbering.

## Issue
{problem}

## Repository test layout
{listing}
"""



GENERIC_PROMPT_TEMPLATE = """You are predicting the FIRST shell commands a \
terminal agent will run for the task below. Do NOT run anything. Do NOT explain.

Output a JSON array of exactly 5 strings, most likely first. Each string is \
one verbatim runnable shell command. Prefer simple read-only exploration and \
verification commands (inspecting files, listing, searching, running existing \
test or check commands). Output ONLY the JSON array — no prose, no markdown \
fences, no numbering.

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
    return "\n".join(lines) or "(empty workspace)"


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
    return bool(_re.fullmatch(r"[A-Za-z0-9_./][\w./+-]*", head))


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




def _openai_completion(prompt: str, timeout: float):
    """One chat completion against an OpenAI-compatible endpoint.
    Returns (text, tokens_dict, exit_code). urllib only — no deps."""
    import urllib.request
    import urllib.error
    endpoint = os.environ.get("SPEC_LLM_ENDPOINT",
                              "http://127.0.0.1:8080/v1/chat/completions")
    body = json.dumps({
        "model": os.environ.get("SPEC_LLM_MODEL", "local"),
        "temperature": 0,
        "max_tokens": int(os.environ.get("SPEC_LLM_MAX_TOKENS", "400")),
        "messages": [
            {"role": "system",
             "content": "You predict shell commands. Respond with ONLY a "
                        "JSON array of strings. No prose, no markdown, "
                        "no thinking out loud."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    # bypass any http_proxy env (cluster proxies break localhost endpoints)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            resp = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as e:
        return "", {}, 1
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get(
        "content") or ""
    # reasoning models (e.g. Qwen3) may wrap deliberation in <think> tags
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    usage = resp.get("usage") or {}
    tokens = {k: v for k, v in usage.items()
              if isinstance(v, int) and "token" in k}
    return text.strip(), tokens, 0


_LAST_MSG_SUPPORT = {}


def _supports_last_message(binary: str) -> bool:
    """Does `<binary> exec` support --output-last-message? Probed once per
    binary via --help; any failure means no (fall back to stream parse)."""
    if binary not in _LAST_MSG_SUPPORT:
        try:
            h = subprocess.run([binary, "exec", "--help"],
                               capture_output=True, text=True, timeout=15)
            _LAST_MSG_SUPPORT[binary] = "--output-last-message" in (
                (h.stdout or "") + (h.stderr or ""))
        except Exception:
            _LAST_MSG_SUPPORT[binary] = False
    return _LAST_MSG_SUPPORT[binary]

def predict_meta(workspace, problem_statement: str):
    """Full-fat entry point: returns (commands, meta) where meta carries
    token usage, latency, and the raw model lines for the ledger."""
    ws = Path(workspace)
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
            listing=_test_file_listing(ws, problem_statement)[:6000])
    binary = os.environ.get("SPEC_LLM_BIN", "codex")
    extra = shlex.split(os.environ.get("SPEC_LLM_ARGS",
                                       "-c model_reasoning_effort=low"))
    timeout = float(os.environ.get("SPEC_LLM_TIMEOUT", "120"))
    backend = os.environ.get("SPEC_LLM_MODE", "codex")
    if backend not in ("codex", "openai"):
        raise SystemExit(f"SPEC_LLM_MODE={backend!r} unknown (use 'codex' or "
                         f"'openai' for the local llama.cpp endpoint); refusing "
                         f"to silently fall back")
    # ---- invocation + parsing (see predict_parse.py) ----
    from predict_parse import (extract_agent_text, extract_usage,
                               extract_commands, looks_like_command)
    import tempfile
    if backend == "openai":
        t0 = time.time()
        text, tokens, rc = _openai_completion(prompt, timeout)
        source = "openai"
        if mode == "generic":
            validator = looks_like_command
        else:
            def validator(c):
                p = parse_command(c)
                return bool(p and p["targets"])
        limit = _pred_limit(mode)
        cmds = extract_commands(text, mode=mode, limit=limit,
                                validator=validator)
        meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
                "n_raw_lines": len(text.splitlines()), "exit": rc,
                "mode": mode, "text_source": source, "backend": "openai",
                "parse": ("json" if text.lstrip().startswith("[")
                          or "[\"" in text[:200] else "lines")}
        cap = os.environ.get("SPEC_PRED_CAPTURE_DIR")
        if cap:
            try:
                Path(cap).mkdir(parents=True, exist_ok=True)
                rec = {"kind": "capture",
                   "task": os.environ.get("SPEC_TASK_NAME") or ws.name,
                   "ts": time.time(),
                       "mode": mode, "prompt": prompt[:20000],
                       "raw_stdout": "", "answer_text": text[:100000],
                       "text_source": source, "predicted": cmds,
                       "tokens": tokens, "latency_s": meta["latency_s"],
                       "backend": "openai"}
                (Path(cap) / f"pred_{os.environ.get('SPEC_TASK_NAME') or ws.name}_{int(time.time() * 1000)}.json"
                 ).write_text(json.dumps(rec))
                meta["capture"] = True
            except OSError:
                pass
        return cmds, meta
    argv = [binary, "exec", "--json", "--sandbox", "read-only",
            "--skip-git-repo-check", *extra]
    last_msg_path = None
    if _supports_last_message(binary):
        fd, last_msg_path = tempfile.mkstemp(prefix="spec_lastmsg_",
                                             suffix=".txt")
        os.close(fd)
        argv += ["--output-last-message", last_msg_path]
    argv.append(prompt)
    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if last_msg_path:
            try:
                os.unlink(last_msg_path)
            except OSError:
                pass
        return [], {"error": str(e), "latency_s": time.time() - t0}
    tokens = extract_usage(proc.stdout)
    text, source = "", "legacy"
    if last_msg_path:
        try:
            text = Path(last_msg_path).read_text(errors="replace").strip()
        except OSError:
            text = ""
        finally:
            try:
                os.unlink(last_msg_path)
            except OSError:
                pass
        if text:
            source = "last_message"
    if not text:
        text = extract_agent_text(proc.stdout)
        if text:
            source = "stream"
    if not text:                        # last resort: old naive harvest
        parts = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("{"):
                parts.append(line)
        text = "\n".join(parts)
    if mode == "generic":
        validator = looks_like_command
    else:
        def validator(c):
            p = parse_command(c)
            return bool(p and p["targets"])
    limit = _pred_limit(mode)
    cmds = extract_commands(text, mode=mode, limit=limit,
                            validator=validator)
    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text.splitlines()), "exit": proc.returncode,
            "mode": mode, "text_source": source,
            "parse": ("json" if text.lstrip().startswith("[")
                      or "[\"" in text[:200] else "lines")}
    # ---- raw capture (SPEC_PRED_CAPTURE_DIR): save the paid-for model ----
    # output so parser changes replay offline with ZERO new tokens
    # (testset/build_testset.py + replay_testset.py consume these files)
    cap = os.environ.get("SPEC_PRED_CAPTURE_DIR")
    if cap:
        try:
            Path(cap).mkdir(parents=True, exist_ok=True)
            rec = {"kind": "capture",
                   "task": os.environ.get("SPEC_TASK_NAME") or ws.name,
                   "ts": time.time(),
                   "mode": mode, "prompt": prompt[:20000],
                   "raw_stdout": proc.stdout[-400000:],
                   "answer_text": text[:100000],
                   "text_source": source, "predicted": cmds,
                   "tokens": tokens, "latency_s": meta["latency_s"]}
            (Path(cap) / f"pred_{os.environ.get('SPEC_TASK_NAME') or ws.name}_{int(time.time() * 1000)}.json"
             ).write_text(json.dumps(rec))
            meta["capture"] = True
        except OSError:
            pass
    return cmds, meta


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
