#!/usr/bin/env python3
"""pred_vs_observed.py — qualitative report: what did the model predict vs
what the agent actually ran, with each miss CLASSIFIED so prompt iteration
targets the real failure mode.

Miss taxonomy per predicted command (pytest family):
  exact          string-normalized match with an observed command
  serve_capable  same targets as an observed command (family 1.0)
  granularity    right file, agent ran node-level (or vice versa)
  wrong_file     predicted file EXISTS in the workspace but agent tested a
                 different one -> model chose wrong among real options
                 (grounding worked; selection failed)
  invented_path  predicted file does NOT exist in the workspace -> model
                 ignored/failed the listing (grounding failed)
  unparseable    prediction isn't a recognized family command

The wrong_file/invented_path split is the decision variable: invented_path
means the listing/prompt needs work; wrong_file means the model needs more
context (or is hitting the semantic-hop ceiling).

Usage (repo root):
  python3 scripts/pred_vs_observed.py --label qwen7b-ws \
      [--results /scratch/czhai/latency-eval/bakeoff_results.jsonl] \
      [--testset /scratch/czhai/latency-eval/testset_v2.jsonl] \
      [--manifest manifests/swebench_extra.tsv] [--task <iid>]
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "latency-opt" / "speculation"))
from spec_families import parse_command  # noqa: E402


def norm_ws(c):
    return re.sub(r"\s+", " ", (c or "").strip())


def file_of(target):
    return target.split("::")[0]


def classify(pred, obs_cmds, ws):
    p = parse_command(pred)
    if not p or not p.get("targets"):
        return "unparseable", None
    oo = [q for q in (parse_command(c) for c in obs_cmds)
          if q and q.get("targets")]
    obs_norm = {norm_ws(c) for c in obs_cmds}
    if norm_ws(pred) in obs_norm:
        return "exact", None
    pt = set(p["targets"])
    pf = {file_of(t) for t in pt}
    for o in oo:
        ot = set(o["targets"])
        if pt == ot:
            return "serve_capable", None
        of_ = {file_of(t) for t in ot}
        if pf and pf == of_:
            return "granularity", sorted(ot)[:2]
    # wrong targets: real path or invented?
    if p["family"] == "pytest" and ws:
        missing = [t for t in pf if not (ws / t).exists()]
        if missing:
            return "invented_path", missing[:2]
    if p["family"] == "django" and ws:
        missing = [t for t in pt
                   if not (ws / "tests" / t.split(".")[0]).is_dir()]
        if missing:
            return "invented_path", missing[:2]
    best_obs = None
    for o in oo:
        best_obs = sorted(o["targets"])[:2]
        break
    return "wrong_file", best_obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--results",
                    default="/scratch/czhai/latency-eval/bakeoff_results.jsonl")
    ap.add_argument("--testset",
                    default="/scratch/czhai/latency-eval/testset_v2.jsonl")
    ap.add_argument("--manifest", default=str(ROOT / "manifests/swebench_extra.tsv"))
    ap.add_argument("--task", default=None, help="only this instance")
    args = ap.parse_args()

    ws_of = {}
    if Path(args.manifest).exists():
        mroot = Path(args.manifest).resolve().parent.parent
        for ln in Path(args.manifest).read_text().splitlines():
            if ln.strip():
                iid, ws, _ = ln.split("\t")
                ws_of[iid] = Path(ws) if ws.startswith("/") else mroot / ws

    obs = defaultdict(set)
    for l in open(args.testset):
        try:
            c = json.loads(l)
        except json.JSONDecodeError:
            continue
        obs[c.get("task")].update(c.get("observed") or [])

    rows, seen = [], set()
    for l in open(args.results):
        try:
            r = json.loads(l)
        except json.JSONDecodeError:
            continue
        if r.get("label") != args.label or r["task"] in seen:
            continue
        if args.task and r["task"] != args.task:
            continue
        seen.add(r["task"])
        rows.append(r)

    tax = Counter()
    for r in sorted(rows, key=lambda x: x["task"]):
        task = r["task"]
        ocmds = obs.get(task) or set()
        ofam = [c for c in sorted(ocmds)
                if parse_command(c) and parse_command(c).get("targets")]
        print(f"\n== {task}  (fam={r['family']}, {len(ocmds)} observed)")
        if not r["preds"]:
            tax["no_prediction"] += 1
            print("   model: (no prediction)")
        for pred in r["preds"]:
            kind, detail = classify(pred, ocmds, ws_of.get(task))
            tax[kind] += 1
            d = f"   [obs targets: {detail}]" if detail and kind != "invented_path" else \
                (f"   [MISSING from repo: {detail}]" if detail else "")
            print(f"   model: {pred[:110]:112s} -> {kind}{d}")
        for c in ofam[:2]:
            print(f"   agent: {c[:150]}")
        if not ofam and ocmds:
            print(f"   agent: (no family test cmds; e.g. {sorted(ocmds)[0][:100]})")

    total = sum(tax.values())
    print(f"\n== taxonomy over {len(rows)} tasks, {total} predictions ==")
    for k, n in tax.most_common():
        print(f"  {k:16s} {n:4d}  ({100*n/max(total,1):4.1f}%)")
    inv, wf = tax.get("invented_path", 0), tax.get("wrong_file", 0)
    if inv or wf:
        print(f"\n  grounding verdict: invented={inv} vs wrong-choice={wf} -> "
              + ("fix the PROMPT/listing (model ignores real options)"
                 if inv > wf else
                 "model grounds fine; misses are SELECTION (context/semantic-hop)"))


if __name__ == "__main__":
    main()
