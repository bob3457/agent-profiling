#!/usr/bin/env python3
"""Materialize FreshQA questions as CPU-profiling tasks.

Reads Agent-Bench's datasets/freshqa.csv, takes the first N TEST-split
questions (deterministic), and writes prompts, empty workspaces, a manifest,
and a gold-reference JSON in the same layout as the hotpot materializer:

  prompts/fresh_<id>.txt
  runs/freshqa/<id>/base_task/
  manifests/freshqa_cpu_study_N.tsv
  manifests/freshqa_gold_N.json   id -> {question, reference_answers, ...}

Usage:
  python3 scripts/materialize_freshqa_cpu_tasks.py --n 10
"""
import argparse
import csv
import json
import os
from pathlib import Path

DEFAULT_ROOT = os.environ.get("ROOT", "/projects/kzhou6/czhai/agent-profiling")
DEFAULT_INPUT = "/projects/kzhou6/czhai/Agent-Bench/datasets/freshqa.csv"

PROMPT_TEMPLATE = """You are answering a FreshQA question. These questions may
depend on current, recently changed, or false-premise facts, so you MUST use
your web search tool to verify against the live web before answering.

Question: {question}

Rules:
- Search the web; do not answer from memory alone.
- If the question contains a false premise, say so and correct it.
- Do not run shell commands; this is a question-answering task.
- Give a concise answer, then end your reply with exactly one line:
FINAL ANSWER: <answer>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--split", default="TEST")
    args = ap.parse_args()

    root = Path(args.root)
    runs = root / "runs" / "freshqa"
    prompts = root / "prompts"
    manifests = root / "manifests"
    for d in (runs, prompts, manifests):
        d.mkdir(parents=True, exist_ok=True)

    selected = []
    with open(args.input, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if args.split and row.get("split", "").strip() != args.split:
                continue
            refs = [
                row[f"answer_{i}"].strip()
                for i in range(10)
                if row.get(f"answer_{i}", "").strip()
            ]
            if not row.get("question", "").strip() or not refs:
                continue
            selected.append(
                {
                    "id": row["id"].strip(),
                    "question": row["question"].strip(),
                    "reference_answers": refs,
                    "false_premise": row.get("false_premise", "").strip(),
                    "fact_type": row.get("fact_type", "").strip(),
                    "num_hops": row.get("num_hops", "").strip(),
                }
            )
            if len(selected) >= args.n:
                break
    if len(selected) < args.n:
        raise SystemExit(f"Only {len(selected)} usable rows, wanted {args.n}")

    manifest = manifests / f"freshqa_cpu_study_{args.n}.tsv"
    gold_path = manifests / f"freshqa_gold_{args.n}.json"

    with manifest.open("w") as mf:
        for item in selected:
            qid = f"fresh{int(item['id']):04d}"
            base = runs / qid / "base_task"
            base.mkdir(parents=True, exist_ok=True)
            (base / "README.txt").write_text(
                "Scratch workspace for FreshQA profiling run; no files needed.\n"
            )
            prompt_path = prompts / f"fresh_{qid}.txt"
            prompt_path.write_text(PROMPT_TEMPLATE.format(question=item["question"]))
            mf.write(
                f"{qid}\t{base.relative_to(root)}\t{prompt_path.relative_to(root)}\n"
            )
            item["qid"] = qid

    gold_path.write_text(json.dumps({it["qid"]: it for it in selected}, indent=2))
    print(f"Wrote {manifest} with {len(selected)} rows")
    print(f"Wrote {gold_path}")


if __name__ == "__main__":
    main()
