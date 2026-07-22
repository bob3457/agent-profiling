#!/usr/bin/env python3
"""Extract FreshQA responses + profiling metrics from the deep-dive tree.

Walks results_cpu_deepdive/freshqa/<qid>/iter_<n>/ and writes:

  results_cpu_deepdive/freshqa/freshqa_responses.jsonl
      One object per question with the eval-contract fields the Agent-Bench
      grader consumes (question, reference_answers, response) plus id fields
      and profiling telemetry. `response` is the agent's full final message
      (the FreshQA grader is an LLM judge that reads the whole response, so
      we keep it intact; the FINAL ANSWER line is also lifted into its own
      field for quick eyeballing).

  results_cpu_deepdive/freshqa/freshqa_profiling_metrics.csv
      Same schema as the hotpot metrics CSV for cross-benchmark comparison.

Requires the gold file the materializer wrote (for reference_answers):
  manifests/freshqa_gold_<n>.json
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
    full = messages[-1].strip() if messages else ""
    final = None
    for text in reversed(messages):
        hits = FINAL_RE.findall(text)
        if hits:
            final = hits[-1].strip().rstrip(".")
            break
    return full, final, usage, len(messages), n_commands


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
    ap.add_argument("--gold", default=None, help="manifests/freshqa_gold_N.json")
    args = ap.parse_args()

    root = Path(args.root)
    bench_root = root / "results_cpu_deepdive" / "freshqa"
    if not bench_root.exists():
        raise SystemExit(f"No results at {bench_root}")

    gold_path = Path(args.gold) if args.gold else None
    if gold_path is None:
        candidates = sorted((root / "manifests").glob("freshqa_gold_*.json"))
        gold_path = candidates[-1] if candidates else None
    gold = json.loads(gold_path.read_text()) if gold_path and gold_path.exists() else {}
    if not gold:
        print("WARNING: no gold file found; reference_answers will be empty")

    out_rows = []
    csv_rows = []
    for meta_path in sorted(bench_root.glob(f"*/iter_{args.iteration}/metadata.json")):
        run_dir = meta_path.parent
        meta = json.loads(meta_path.read_text())
        qid = meta.get("example_id") or run_dir.parent.name
        g = gold.get(qid, {})
        full, final, usage, n_msgs, n_cmds = parse_stdout(run_dir / "stdout.jsonl")
        task_ms = parse_task_clock_ms(run_dir / "perf_stat.csv")
        wall_ms = meta.get("wall_ms") or 0
        out_rows.append(
            {
                "qid": qid,
                "id": g.get("id"),
                "question": g.get("question"),
                "category": g.get("fact_type"),
                "false_premise": g.get("false_premise"),
                "reference_answers": g.get("reference_answers", []),
                "response": full,
                "final_answer": final,
                "returncode": meta.get("returncode"),
                "wall_ms": round(wall_ms, 1),
                "task_clock_ms": round(task_ms, 1) if task_ms is not None else None,
                **usage,
            }
        )
        csv_rows.append(
            {
                "qid": qid,
                "final_answer": final or "",
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

    if not out_rows:
        raise SystemExit(f"No iter_{args.iteration} runs found under {bench_root}")

    jsonl_path = bench_root / "freshqa_responses.jsonl"
    with jsonl_path.open("w") as f:
        for row in out_rows:
            f.write(json.dumps(row) + "\n")

    csv_path = bench_root / "freshqa_profiling_metrics.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"Wrote {jsonl_path} ({len(out_rows)} responses)")
    print(f"Wrote {csv_path}")
    missing = [r["qid"] for r in out_rows if not r["response"]]
    if missing:
        print(f"WARNING: empty response for: {missing}")


if __name__ == "__main__":
    main()
