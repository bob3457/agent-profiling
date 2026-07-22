#!/usr/bin/env python3
"""Bridge the profiling outputs back to the HotpotQA scorer + a metrics CSV.

Walks results_cpu_deepdive/hotpotqa/<qid>/iter_<n>/, pulls:
  - the model's answer: last "FINAL ANSWER:" line in agent_message items
    from stdout.jsonl (falls back to the last agent_message text)
  - token usage from turn.completed events
  - wall_ms / returncode from metadata.json, task-clock from perf_stat.csv

Writes:
  results_cpu_deepdive/hotpotqa/hotpot_fullwiki_predictions.json
      (format hotpot_evaluate_v1.py expects: {"answer": {...}, "sp": {...}})
  results_cpu_deepdive/hotpotqa/hotpot_profiling_metrics.csv

Score afterwards from the Agent-Bench repo:
  python eval/hotpot_evaluate_v1.py \
      <root>/results_cpu_deepdive/hotpotqa/hotpot_fullwiki_predictions.json \
      datasets/hotpot_dev_fullwiki_v1.json
(sp is left empty, so the scorer's supporting-fact and joint metrics will be 0
by construction; answer EM/F1 are the meaningful numbers.)
"""
import argparse
import csv
import json
import os
import re
from pathlib import Path

DEFAULT_ROOT = os.environ.get("ROOT", "/projects/kzhou6/czhai/agent-profiling")
FINAL_RE = re.compile(r"FINAL ANSWER:\s*(.+)", re.IGNORECASE)


def read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def parse_stdout(path: Path):
    """Return (answer, usage_totals, n_messages, n_commands)."""
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    messages = []
    n_commands = 0
    for e in read_jsonl(path):
        if e.get("type") == "turn.completed":
            u = e.get("usage") or {}
            for k in usage:
                usage[k] += u.get(k, 0) or 0
        item = e.get("item") or {}
        if item.get("type") == "agent_message":
            messages.append(item.get("text") or "")
        if item.get("type") == "command_execution":
            n_commands += 1
    answer = None
    for text in reversed(messages):
        m = None
        for m in FINAL_RE.finditer(text):
            pass
        if m:
            answer = m.group(1).strip().rstrip(".")
            break
    if answer is None and messages:
        answer = messages[-1].strip().splitlines()[-1].strip()
    return answer, usage, len(messages), n_commands


def parse_task_clock_ms(path: Path):
    if not path.exists():
        return None
    for line in path.read_text(errors="ignore").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[2] == "task-clock" and not parts[0].startswith("<"):
            try:
                return float(parts[0].replace(",", ""))
            except ValueError:
                return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--iteration", type=int, default=1)
    args = ap.parse_args()

    bench_root = Path(args.root) / "results_cpu_deepdive" / "hotpotqa"
    if not bench_root.exists():
        raise SystemExit(f"No results at {bench_root}")

    answers = {}
    rows = []
    for meta_path in sorted(bench_root.glob(f"*/iter_{args.iteration}/metadata.json")):
        run_dir = meta_path.parent
        meta = json.loads(meta_path.read_text())
        qid = meta.get("example_id") or run_dir.parent.name
        answer, usage, n_msgs, n_cmds = parse_stdout(run_dir / "stdout.jsonl")
        answers[qid] = answer or ""
        task_ms = parse_task_clock_ms(run_dir / "perf_stat.csv")
        wall_ms = meta.get("wall_ms") or 0
        rows.append(
            {
                "qid": qid,
                "answer": answer or "",
                "returncode": meta.get("returncode"),
                "wall_ms": round(wall_ms, 1),
                "task_clock_ms": round(task_ms, 1) if task_ms is not None else "",
                "cpu_util_pct": round(100 * task_ms / wall_ms, 2)
                if task_ms and wall_ms
                else "",
                "agent_messages": n_msgs,
                "local_shell_commands": n_cmds,
                **usage,
            }
        )

    if not rows:
        raise SystemExit(f"No iter_{args.iteration} runs found under {bench_root}")

    pred_path = bench_root / "hotpot_fullwiki_predictions.json"
    pred_path.write_text(
        json.dumps({"answer": answers, "sp": {q: [] for q in answers}}, indent=2)
    )

    csv_path = bench_root / "hotpot_profiling_metrics.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {pred_path} ({len(answers)} answers)")
    print(f"Wrote {csv_path}")
    empty = [q for q, a in answers.items() if not a]
    if empty:
        print(f"WARNING: {len(empty)} question(s) with no extracted answer: {empty}")


if __name__ == "__main__":
    main()
