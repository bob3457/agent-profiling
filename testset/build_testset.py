#!/usr/bin/env python3
"""build_testset.py — build a prediction test set from runs already on disk.

Zero new tokens: harvests (prediction, observed-commands) pairs from
completed arm runs, plus (optionally) raw-capture files written by the
predictor's capture files and ledger prediction records.

Sources merged per (bench, task, run_dir):
  spec.log                     `[spec] llm predictor: [...]` lines
                               (parsed prediction list; last occurrence
                               per run wins — freshest prediction)
                               plus `[spec] heuristic pytest targets: [...]`
  shelld_logs/commands.jsonl   every command the agent actually executed
  <capture-dir>/pred_*.json    RAW predictor output (stdout stream, answer
                               text, prompt, mode) — only exists for runs
                               made after the v2 patch with
                               SPEC_PRED_CAPTURE_DIR set; enables true
                               re-parsing in replay_testset.py
  <ledger-dir>/ledger.jsonl    prediction records (tokens, latency)

Usage:
  python3 build_testset.py \
      --results '/scratch/czhai/latency-eval/results/arm_C*' \
      --out testset.jsonl \
      [--capture-dir /scratch/czhai/latency-eval/pred_capture] \
      [--ledger-dir  /scratch/czhai/latency-eval/ledger]

Output: one JSON line per case:
  {"kind":"case", "bench", "task", "run_dir",
   "predicted": [...],            # as parsed at run time (old or new parser)
   "predictor": "llm",
   "observed": [...],             # agent's executed commands, order kept
   "raw": {"stdout":..., "answer_text":..., "prompt":..., "mode":...,
           "text_source":...} | null,
   "tokens": {...} | null, "latency_s": ... | null}
"""
import argparse
import ast
import glob
import json
import re
from pathlib import Path

PRED_RE = re.compile(r"\[spec\] llm predictor: (\[.*\])")


def parse_spec_log(path: Path):
    """Last llm-predictor candidate list in a worker spec.log."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    cands = None
    for m in PRED_RE.finditer(text):
        try:
            got = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(got, list):
            cands = [c for c in got if isinstance(c, str)]
    return cands


def read_observed(commands_jsonl: Path, cap: int = 400):
    out, seen = [], set()
    try:
        lines = commands_jsonl.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            cmd = json.loads(ln).get("cmd", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        cmd = (cmd or "").strip()
        if cmd and cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
        if len(out) >= cap:
            break
    return out


def load_captures(capture_dir):
    """task -> newest capture record."""
    caps = {}
    if not capture_dir:
        return caps
    for f in sorted(Path(capture_dir).glob("pred_*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        task = rec.get("task")
        if task and rec.get("ts", 0) >= caps.get(task, {}).get("ts", -1):
            caps[task] = rec
    return caps


def load_ledger_preds(ledger_dir):
    """(task, predictor) -> newest prediction record (tokens/latency)."""
    out = {}
    if not ledger_dir:
        return out
    p = Path(ledger_dir) / "ledger.jsonl"
    if not p.exists():
        return out
    for ln in p.read_text(errors="replace").splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("kind") == "prediction":
            out[(r.get("task"), r.get("predictor"))] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True,
                    help="glob(s) of arm result dirs "
                         "(structure: <glob>/<bench>/<task>/)")
    ap.add_argument("--out", default="testset.jsonl")
    ap.add_argument("--capture-dir", default=None)
    ap.add_argument("--ledger-dir", default=None)
    ap.add_argument("--require-prediction", action="store_true",
                    help="drop tasks with observed commands but no "
                         "recorded prediction")
    args = ap.parse_args()

    caps = load_captures(args.capture_dir)
    ledger = load_ledger_preds(args.ledger_dir)

    run_dirs = set()
    for pat in args.results:
        for f in glob.glob(f"{pat}/*/*/shelld_logs/commands.jsonl"):
            run_dirs.add(str(Path(f).parent.parent))
        for f in glob.glob(f"{pat}/*/*/spec.log"):
            run_dirs.add(str(Path(f).parent))

    n_raw, cases = 0, []
    for rd in sorted(run_dirs):
        rdp = Path(rd)
        bench, task = rdp.parent.name, rdp.name
        predicted = parse_spec_log(rdp / "spec.log")
        observed = read_observed(rdp / "shelld_logs" / "commands.jsonl")
        if not observed and not predicted:
            continue
        if args.require_prediction and not predicted:
            continue
        cap = caps.get(task)
        # bind captures to their own sweep: a capture written during
        # today's run must not attach to an OLD run dir of the same task
        # (run dir name arm_X.YYYYMMDD_HHMMSS gives the sweep start)
        if cap is not None:
            import re as _re, time as _time
            m = _re.search(r"\.((?:20)\d{6}_\d{6})", rd)
            rs = None
            if m:
                try:
                    rs = _time.mktime(_time.strptime(m.group(1),
                                                     "%Y%m%d_%H%M%S"))
                except ValueError:
                    pass
            if rs is None or not (rs - 300 <= cap.get("ts", 0)
                                  <= rs + 86400):
                cap = None
        led = ledger.get((task, "llm"))
        if predicted is None and cap:
            predicted = cap.get("predicted")
        if predicted is None and led:
            predicted = led.get("commands")
        raw = None
        if cap:
            raw = {"stdout": cap.get("raw_stdout"),
                   "answer_text": cap.get("answer_text"),
                   "prompt": cap.get("prompt"),
                   "mode": cap.get("mode"),
                   "text_source": cap.get("text_source")}
            n_raw += 1
        cases.append({
            "kind": "case", "bench": bench, "task": task, "run_dir": rd,
            "predictor": "llm", "predicted": predicted or [],
            "observed": observed, "raw": raw,
            "tokens": (cap or led or {}).get("tokens"),
            "latency_s": (cap or led or {}).get("latency_s"),
        })

    with open(args.out, "w") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")
    n_pred = sum(1 for c in cases if c["predicted"])
    print(f"testset: {len(cases)} cases -> {args.out}")
    print(f"  with prediction: {n_pred}   with raw capture: {n_raw}   "
          f"observed-only: {len(cases) - n_pred}")
    if n_raw == 0:
        print("  (no raw captures: re-parsing in replay is unavailable for "
              "these runs; set SPEC_PRED_CAPTURE_DIR for future runs)")


if __name__ == "__main__":
    main()
