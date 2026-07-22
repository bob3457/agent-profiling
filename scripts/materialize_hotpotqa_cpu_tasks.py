#!/usr/bin/env python3
"""Materialize HotpotQA fullwiki questions as CPU-profiling tasks.

Reads the same hotpot_dev_fullwiki_v1.json the Agent-Bench harness uses,
takes the first N questions (deterministic, so runs are comparable across
variants/iterations), and writes:
  prompts/hotpot_<qid>.txt          one prompt per question
  runs/hotpotqa/<qid>/base_task/    empty workspace template (QA needs no files)
  manifests/hotpotqa_cpu_study_N.tsv
  manifests/hotpotqa_gold_N.json    qid -> gold answer, for later scoring

Usage:
  python3 scripts/materialize_hotpotqa_cpu_tasks.py --n 10
  python3 scripts/materialize_hotpotqa_cpu_tasks.py --n 10 \
      --input /projects/kzhou6/czhai/Agent-Bench/datasets/hotpot_dev_fullwiki_v1.json
"""
import argparse
import json
import os
from pathlib import Path

DEFAULT_ROOT = os.environ.get("ROOT", "/projects/kzhou6/czhai/agent-profiling")
DEFAULT_INPUT = "/projects/kzhou6/czhai/Agent-Bench/datasets/hotpot_dev_fullwiki_v1.json"

PROMPT_TEMPLATE = """You are answering a HotpotQA question in the FULLWIKI setting.
No context passages are provided. Use your web search tool to find the
evidence on Wikipedia, then answer.

Question: {question}

Rules:
- Search the web as needed; multi-hop questions usually need 2+ searches.
- The answer is a short span (a name, date, number, yes/no, or short phrase).
- Do not run shell commands; this is a question-answering task, not a coding task.
- End your reply with exactly one line of the form:
FINAL ANSWER: <answer>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()

    root = Path(args.root)
    runs = root / "runs" / "hotpotqa"
    prompts = root / "prompts"
    manifests = root / "manifests"
    for d in (runs, prompts, manifests):
        d.mkdir(parents=True, exist_ok=True)

    data = json.loads(Path(args.input).read_text())
    selected = data[: args.n]
    if len(selected) < args.n:
        raise SystemExit(f"Dataset has only {len(selected)} items, wanted {args.n}")

    manifest = manifests / f"hotpotqa_cpu_study_{args.n}.tsv"
    gold_path = manifests / f"hotpotqa_gold_{args.n}.json"
    gold = {}

    with manifest.open("w") as mf:
        for item in selected:
            qid = item["_id"]
            gold[qid] = item["answer"]
            base = runs / qid / "base_task"
            base.mkdir(parents=True, exist_ok=True)
            # keep the workspace non-empty so rsync/cp -a template checks pass
            (base / "README.txt").write_text(
                "Scratch workspace for HotpotQA profiling run; no files needed.\n"
            )
            prompt_path = prompts / f"hotpot_{qid}.txt"
            prompt_path.write_text(PROMPT_TEMPLATE.format(question=item["question"]))
            mf.write(
                f"{qid}\t{base.relative_to(root)}\t{prompt_path.relative_to(root)}\n"
            )

    gold_path.write_text(json.dumps(gold, indent=2))
    print(f"Wrote {manifest} with {len(selected)} rows")
    print(f"Wrote {gold_path}")


if __name__ == "__main__":
    main()
