#!/usr/bin/env python3
"""patch_predictor_parse.py — rewire llm_predictor.py onto predict_parse.

Idempotent; verbatim anchor assertions; run from repo root:
    python3 patch_predictor_parse.py [--root .]

Changes to latency-opt/speculation/llm_predictor.py:
  1. PROMPTS ask for a strict JSON array of command strings (parsing
     becomes deterministic; line format kept as model-drift fallback).
  2. INVOCATION adds `--output-last-message <tmpfile>`: the final agent
     message verbatim, no stream scraping for text. Auto-detected via
     `codex exec --help`; falls back to schema-aware stream extraction
     (predict_parse.extract_agent_text) and only then to the legacy
     leaf-walk. Fixes reasoning-text / command-output / prompt-echo
     contamination of candidates.
  3. TOKENS via predict_parse.extract_usage (last usage dict wins)
     instead of _walk_tokens' sum over every token-keyed int.
  4. LINE CLEANING via predict_parse.clean_line (prefix regex, fence
     removal) instead of the charset lstrip that mangled `./x` and `7z`.
  5. VALIDATION via predict_parse.looks_like_command in generic mode;
     tests mode still requires a family parse (unchanged contract).

predict_meta()'s signature, return shape, env knobs, and the meta keys the
worker/ledger consume (tokens, latency_s, exit, mode) are unchanged; new
meta keys: text_source ('last_message'|'stream'|'legacy'), parse ('json'|
'lines'), capture (bool).
  6. RAW CAPTURE: with SPEC_PRED_CAPTURE_DIR set, every predictor call
     saves prompt + raw --json stdout + extracted answer text + parsed
     commands to <dir>/pred_<task>_<ms>.json, so parser iterations
     replay offline against paid-for model output (see testset/). MARKER: spec-parse-v2
"""
import argparse
import sys
from pathlib import Path

MARKER = "spec-parse-v2"

NEW_PROMPT = '''PROMPT_TEMPLATE = """You are predicting the FIRST test command a coding agent will run \\
for the software issue below. Do NOT run anything. Do NOT explain.

Output a JSON array of exactly 3 strings, most likely first. Each string \\
is one verbatim runnable command (e.g. "python -m pytest path/to/test_file.py -q" \\
or "python tests/runtests.py label.module"). Output ONLY the JSON array — \\
no prose, no markdown fences, no numbering.

## Issue
{problem}

## Repository test layout
{listing}
"""



GENERIC_PROMPT_TEMPLATE = """You are predicting the FIRST shell commands a \\
terminal agent will run for the task below. Do NOT run anything. Do NOT explain.

Output a JSON array of exactly 5 strings, most likely first. Each string is \\
one verbatim runnable shell command. Prefer simple read-only exploration and \\
verification commands (inspecting files, listing, searching, running existing \\
test or check commands). Output ONLY the JSON array — no prose, no markdown \\
fences, no numbering.

## Task
{problem}

## Workspace contents
{listing}
"""'''

OLD_PROMPT_HEAD = 'PROMPT_TEMPLATE = """You are predicting the FIRST test command a coding agent will run \\'
OLD_PROMPT_TAIL = '''## Workspace contents
{listing}
"""'''

OLD_PARSE_BLOCK = '''    argv = [binary, "exec", "--json", "--sandbox", "read-only",
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
                cmds.append(cand)
    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text_parts), "exit": proc.returncode,
            "mode": mode}
    return cmds[:5 if mode == "generic" else 3], meta'''

NEW_PARSE_BLOCK = '''    # ---- invocation + parsing: spec-parse-v1 (see predict_parse.py) ----
    from predict_parse import (extract_agent_text, extract_usage,
                               extract_commands, looks_like_command)
    import tempfile
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
        text = "\\n".join(parts)
    if mode == "generic":
        validator = looks_like_command
    else:
        def validator(c):
            p = parse_command(c)
            return bool(p and p["targets"])
    limit = 5 if mode == "generic" else 3
    cmds = extract_commands(text, mode=mode, limit=limit,
                            validator=validator)
    meta = {"latency_s": round(time.time() - t0, 2), "tokens": tokens,
            "n_raw_lines": len(text.splitlines()), "exit": proc.returncode,
            "mode": mode, "text_source": source,
            "parse": ("json" if text.lstrip().startswith("[")
                      or "[\\"" in text[:200] else "lines")}
    # ---- raw capture (SPEC_PRED_CAPTURE_DIR): save the paid-for model ----
    # output so parser changes replay offline with ZERO new tokens
    # (testset/build_testset.py + replay_testset.py consume these files)
    cap = os.environ.get("SPEC_PRED_CAPTURE_DIR")
    if cap:
        try:
            Path(cap).mkdir(parents=True, exist_ok=True)
            rec = {"kind": "capture", "task": ws.name, "ts": time.time(),
                   "mode": mode, "prompt": prompt[:20000],
                   "raw_stdout": proc.stdout[-400000:],
                   "answer_text": text[:100000],
                   "text_source": source, "predicted": cmds,
                   "tokens": tokens, "latency_s": meta["latency_s"]}
            (Path(cap) / f"pred_{ws.name}_{int(time.time() * 1000)}.json"
             ).write_text(json.dumps(rec))
            meta["capture"] = True
        except OSError:
            pass
    return cmds, meta'''

HELPER = '''

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

'''

HELPER_ANCHOR = '''def predict_meta(workspace, problem_statement: str):'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    target = Path(args.root) / "latency-opt/speculation/llm_predictor.py"
    parse_mod = Path(args.root) / "latency-opt/speculation/predict_parse.py"
    assert target.exists(), f"missing {target}"
    assert parse_mod.exists(), (
        f"missing {parse_mod} — copy speculation/predict_parse.py in first")
    src = target.read_text()
    if MARKER in src:
        print(f"already patched ({MARKER}); nothing to do")
        return

    # 1. prompts
    i = src.find(OLD_PROMPT_HEAD)
    j = src.find(OLD_PROMPT_TAIL)
    assert i >= 0, "anchor missing: PROMPT_TEMPLATE head"
    assert j > i, "anchor missing: GENERIC prompt tail"
    src = src[:i] + NEW_PROMPT + src[j + len(OLD_PROMPT_TAIL):]

    # 2+3+4+5. invocation + parsing block
    assert OLD_PARSE_BLOCK in src, "anchor missing: parse block"
    src = src.replace(OLD_PARSE_BLOCK, NEW_PARSE_BLOCK)

    # helper
    assert HELPER_ANCHOR in src, "anchor missing: predict_meta def"
    src = src.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)

    src = src.replace('"""llm_predictor.py — LLM-based first-command prediction.',
                      f'"""llm_predictor.py — LLM-based first-command prediction. [{MARKER}]',
                      1)
    target.write_text(src)
    print(f"patched {target} [{MARKER}]")
    print("note: _walk_tokens and _looks_like_command remain defined but "
          "unused by predict_meta (predictor_eval imports nothing from them).")


if __name__ == "__main__":
    main()
