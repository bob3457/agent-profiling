#!/usr/bin/env python3
"""ledger.py — persistent prediction ledger (build 4).

The memory that turns speculation from a reflex into a decision. Records
every prediction; later joins each with what the agent actually ran; keeps
running accuracy per (benchmark, predictor); prices the LLM predictor in
tokens. spec_gate consults `stats` to decide whether/how hard to speculate
and whether the LLM predictor is worth its cost.

Storage: one JSONL at <ledger-dir>/ledger.jsonl. Entry lifecycle:
  {"kind": "prediction", "task", "bench", "predictor", "commands",
   "tokens", "latency_s", "ts"}                    <- written by the worker
  {"kind": "outcome", "task", "bench", "predictor", "score",
   "matched", "observed_cmd", "ts"}                <- written by `update`

CLI:
  ledger.py update --ledger-dir D --results '/scratch/.../arm_C*'
      join unresolved predictions with observed commands from run results
  ledger.py stats --ledger-dir D [--bench swebench]
      per-(bench, predictor) accuracy, sample counts, token totals
"""

import argparse
import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_families import parse_command  # noqa: E402
from predictor_eval import score_pair  # noqa: E402


def _path(d):
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p / "ledger.jsonl"


def record_prediction(ledger_dir, task, bench, predictor, commands,
                      tokens=None, latency_s=None):
    with _path(ledger_dir).open("a") as f:
        f.write(json.dumps({"kind": "prediction", "task": task, "bench": bench,
                            "predictor": predictor, "commands": commands,
                            "tokens": tokens or {}, "latency_s": latency_s,
                            "ts": time.time()}) + "\n")


def _load(ledger_dir):
    p = _path(ledger_dir)
    rows = []
    if p.exists():
        for line in p.open():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def cmd_update(args):
    rows = _load(args.ledger_dir)
    resolved = {(r["task"], r["predictor"], r.get("pred_ts"))
                for r in rows if r["kind"] == "outcome"}
    # observed commands per task from run results
    observed = {}
    for pat in args.results:
        for f in glob.glob(f"{pat}/*/*/shelld_logs/commands.jsonl"):
            task = f.split("/")[-3]
            for line in open(f):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = parse_command(row["cmd"])
                if parsed and parsed["targets"]:
                    observed.setdefault(task, {})[row["cmd"]] = parsed
    n = 0
    with _path(args.ledger_dir).open("a") as out:
        for r in rows:
            if r["kind"] != "prediction":
                continue
            if (r["task"], r["predictor"], r["ts"]) in resolved:
                continue
            obs = observed.get(r["task"])
            if not obs:
                continue  # no run data yet; stays unresolved
            preds = [parse_command(c) for c in r["commands"]]
            preds = [p for p in preds if p]
            best, best_obs = 0.0, None
            for oc, op in obs.items():
                for pp in preds:
                    s = score_pair(pp, op)
                    if s > best:
                        best, best_obs = s, oc
            out.write(json.dumps({"kind": "outcome", "task": r["task"],
                                  "bench": r["bench"], "predictor": r["predictor"],
                                  "score": best, "observed_cmd": best_obs,
                                  "pred_ts": r["ts"], "ts": time.time()}) + "\n")
            n += 1
    print(f"resolved {n} prediction(s)")


def stats(ledger_dir, bench=None):
    """Aggregate: {(bench, predictor): {n, mean, exact, tokens_total}}."""
    rows = _load(ledger_dir)
    tok_by_pred = {}
    for r in rows:
        if r["kind"] == "prediction" and r.get("tokens"):
            key = (r["bench"], r["predictor"])
            tok_by_pred[key] = tok_by_pred.get(key, 0) + sum(r["tokens"].values())
    agg = {}
    for r in rows:
        if r["kind"] != "outcome":
            continue
        if bench and r["bench"] != bench:
            continue
        key = (r["bench"], r["predictor"])
        a = agg.setdefault(key, {"n": 0, "sum": 0.0, "exact": 0})
        a["n"] += 1
        a["sum"] += r["score"]
        a["exact"] += r["score"] == 1.0
    out = {}
    for key, a in agg.items():
        out[key] = {"n": a["n"], "mean": round(a["sum"] / a["n"], 3),
                    "exact": a["exact"],
                    "tokens_total": tok_by_pred.get(key, 0)}
    return out


def cmd_stats(args):
    s = stats(args.ledger_dir, args.bench)
    if not s:
        print("(ledger empty or no resolved outcomes)")
        return
    print(f"{'bench':15s} {'predictor':12s} {'n':>3} {'mean':>6} {'exact':>5} {'tokens':>8}")
    for (b, p), a in sorted(s.items()):
        print(f"{b:15s} {p:12s} {a['n']:>3} {a['mean']:>6.3f} {a['exact']:>5} {a['tokens_total']:>8}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--ledger-dir", required=True)
    u.add_argument("--results", nargs="+", required=True)
    s = sub.add_parser("stats")
    s.add_argument("--ledger-dir", required=True)
    s.add_argument("--bench", default=None)
    args = ap.parse_args()
    if args.cmd == "update":
        cmd_update(args)
    else:
        cmd_stats(args)


if __name__ == "__main__":
    main()
