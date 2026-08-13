#!/usr/bin/env python3
"""predictor_eval.py — build a trajectory corpus and score predictors offline.

Why: live cache-hit rate conflates two random variables — whether the
prediction was right AND whether the agent happened to issue the predicted
command this particular run (12907 ran pytest in two trajectories out of
three). Offline evaluation separates them: collect every test-invocation
command agents have EVER issued per task across all recorded runs, then
score a predictor against that corpus. Deterministic, free, and the corpus
only grows.

Two subcommands:

  extract  — walk arm result dirs, pull recognized test-family commands from
             every shelld_logs/commands.jsonl, dedupe per task:
    python3 predictor_eval.py extract \\
        --results '/scratch/czhai/latency-eval/results/arm_*' \\
        --out corpus.jsonl

  score    — run the heuristic predictor for each task in the corpus and
             score predicted targets vs. actually-issued targets:
    python3 predictor_eval.py score --corpus corpus.jsonl \\
        --workspaces /scratch/czhai/latency-eval/workspaces \\
        --prompts-dir /projects/kzhou6/czhai/agent-profiling/prompts

Scoring per task (best over the task's observed commands):
  1.0  predicted target set matches an observed invocation exactly
  0.8  right file/label, wrong granularity (file vs file::test, label vs label.mod)
  0.2  right family, disjoint targets
  0.0  predicted nothing in the family the agent used / nothing at all

A future LLM speculator is scored by the SAME function — just point --predictor
at a module exposing predict(workspace, problem_statement) -> list[str] of
commands. The heuristic is the built-in default and the baseline to beat.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_families import parse_command  # noqa: E402


# ------------------------------------------------------------------- extract
def cmd_extract(args):
    corpus = {}
    files = []
    for pat in args.results:
        files += glob.glob(f"{pat}/*/*/shelld_logs/commands.jsonl")
    for f in sorted(files):
        parts = f.split("/")
        bench, task = parts[-4], parts[-3]
        key = f"{bench}/{task}"
        for line in open(f):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parse_command(row["cmd"])
            if parsed is None or not parsed["targets"]:
                continue
            entry = corpus.setdefault(key, {"bench": bench, "task": task,
                                            "observed": {}})
            entry["observed"][row["cmd"]] = parsed
    out = Path(args.out)
    with out.open("w") as fh:
        for key, entry in sorted(corpus.items()):
            fh.write(json.dumps({
                "bench": entry["bench"], "task": entry["task"],
                "observed": [{"cmd": c, "family": p["family"],
                              "targets": p["targets"]}
                             for c, p in entry["observed"].items()],
            }) + "\n")
    n_cmds = sum(len(e["observed"]) for e in corpus.values())
    print(f"corpus: {len(corpus)} tasks, {n_cmds} unique test invocations -> {out}")
    for key, e in sorted(corpus.items()):
        for c in e["observed"]:
            print(f"  {key}: {c[:90]!r}")


# --------------------------------------------------------------------- score
def default_predictor(workspace: Path, problem_statement: str):
    """The current heuristic, producing the same commands the worker would."""
    from speculative_worker import discover_pytest_targets, discover_django_labels
    cmds = []
    if (workspace / "tests" / "runtests.py").exists():
        for lab in discover_django_labels(workspace, problem_statement):
            cmds.append(f"python tests/runtests.py {lab}")
    else:
        for t in discover_pytest_targets(workspace, problem_statement):
            cmds.append(f"python -m pytest {t}")
    return cmds


def score_pair(pred, obs):
    """pred/obs are parse_command dicts. Return graded score.

    Monotone by construction: partial target overlap never scores below
    the file-level (0.8) / family-level (0.2) credit a fully DISJOINT
    prediction in the same file/family would earn."""
    if pred["family"] != obs["family"]:
        return 0.0
    pt, ot = set(pred["targets"]), set(obs["targets"])
    if not pt or not ot:
        return 0.0
    if pt == ot:
        return 1.0
    p_files = {t.split("::")[0].split(".")[0] for t in pt}
    o_files = {t.split("::")[0].split(".")[0] for t in ot}
    floor = 0.8 if (p_files & o_files) else 0.2
    if pt & ot:
        return round(max(len(pt & ot) / len(pt | ot), floor), 3)
    return floor


def cmd_score(args):
    if args.predictor:
        import importlib.util
        spec = importlib.util.spec_from_file_location("predictor", args.predictor)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        predict = mod.predict
        pname = Path(args.predictor).stem
    else:
        predict = default_predictor
        pname = "heuristic"

    rows = []
    for line in open(args.corpus):
        entry = json.loads(line)
        if entry["bench"] not in ("swebench",):  # test-prediction only applies here for now
            continue
        task = entry["task"]
        ws = Path(args.workspaces) / task
        ps_file = Path(args.prompts_dir) / f"swe_{task}.txt"
        if not ws.is_dir() or not ps_file.exists():
            print(f"skip {task}: workspace or prompt missing")
            continue
        preds = [parse_command(c) for c in predict(ws, ps_file.read_text())]
        preds = [p for p in preds if p]
        best, best_pair = 0.0, None
        for obs in entry["observed"]:
            for pred in preds:
                s = score_pair(pred, obs)
                if s > best:
                    best, best_pair = s, (pred, obs)
        rows.append({"task": task, "score": best,
                     "n_predicted": len(preds),
                     "n_observed": len(entry["observed"]),
                     "match": (best_pair[0]["targets"], best_pair[1]["targets"])
                              if best_pair else None})
    print(f"\n=== predictor: {pname} ===")
    for r in rows:
        print(f"{r['task']:38s} score={r['score']:.2f} "
              f"pred={r['n_predicted']} obs={r['n_observed']} match={r['match']}")
    if rows:
        mean = sum(r["score"] for r in rows) / len(rows)
        exact = sum(r["score"] == 1.0 for r in rows)
        print(f"\nmean graded score: {mean:.3f}   exact: {exact}/{len(rows)}   "
              f"zero: {sum(r['score'] == 0 for r in rows)}/{len(rows)}")
    if args.out:
        Path(args.out).write_text(json.dumps({"predictor": pname, "rows": rows}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--results", nargs="+", required=True,
                   help="glob(s) of arm result dirs")
    e.add_argument("--out", default="corpus.jsonl")
    s = sub.add_parser("score")
    s.add_argument("--corpus", required=True)
    s.add_argument("--workspaces", required=True)
    s.add_argument("--prompts-dir", required=True)
    s.add_argument("--predictor", default=None,
                   help="path to a .py exposing predict(workspace, problem_statement)")
    s.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.cmd == "extract":
        cmd_extract(args)
    else:
        cmd_score(args)


if __name__ == "__main__":
    main()
