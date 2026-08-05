#!/usr/bin/env python3
"""select_tasks.py — build an eval set of tasks that MATTER for speculation.

A task matters when it has tool-side seconds to win: speculation cannot
help a task whose commands total 0.3s. This scans every historical results
root for per-task tool wall (sum of live wall_s in shelld_logs/
commands.jsonl, falling back to summary.json tool_wall_s), takes the median
across runs, and picks the top-N per benchmark. QA tasks are taken from the
manifests (they are inference-bound controls; any ranking is meaningless).

Usage:
  python3 select_tasks.py --results '/scratch/czhai/latency-eval/results' \\
      --swe 10 --tb 10 --hotpot 3 --freshqa 3 \\
      --tb-tasks-dir $ROOT/runs/terminalbench-arm \\
      --out $OPT/eval_set_26.txt
"""
import argparse
import json
from pathlib import Path


def tool_wall(run: Path):
    f = run / "shelld_logs" / "commands.jsonl"
    if f.exists():
        tw = 0.0
        for ln in f.read_text(errors="replace").splitlines():
            try:
                c = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not c.get("cached"):
                tw += c.get("wall_s") or 0.0
        return tw
    return None


def from_summaries(results: Path, bench: str):
    """{task_id: [tool_wall, ...]} across every root, both log sources."""
    hist = {}
    for root in results.glob("arm_*"):
        # per-run dirs
        for run in root.glob(f"{bench}/*/"):
            tw = tool_wall(run)
            if tw is not None:
                hist.setdefault(run.name, []).append(tw)
        # summary.json fallback (baseline arms have no daemon logs)
        sj = root / "summary.json"
        if sj.exists():
            try:
                rows = json.loads(sj.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rows, dict):
                rows = rows.get("tasks", [])
            for r in rows if isinstance(rows, list) else []:
                t = r.get("task", "")
                if t.startswith(bench + "/") and "tool_wall_s" in r:
                    hist.setdefault(t.split("/", 1)[1], []).append(
                        float(r["tool_wall_s"]))
    return hist


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--swe", type=int, default=10)
    ap.add_argument("--tb", type=int, default=10)
    ap.add_argument("--hotpot", type=int, default=3)
    ap.add_argument("--freshqa", type=int, default=3)
    ap.add_argument("--tb-tasks-dir", type=Path, default=None,
                    help="only pick TB tasks that exist here")
    ap.add_argument("--hotpot-manifest", type=Path, default=None)
    ap.add_argument("--freshqa-manifest", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    lines = []
    for bench, n in (("swebench", a.swe), ("terminalbench", a.tb)):
        hist = from_summaries(a.results, bench)
        ranked = sorted(((median(v), len(v), t) for t, v in hist.items()),
                        reverse=True)
        if bench == "terminalbench" and a.tb_tasks_dir and \
                a.tb_tasks_dir.is_dir():
            avail = {p.name for p in a.tb_tasks_dir.iterdir() if p.is_dir()}
            dropped = [t for _, _, t in ranked if t not in avail]
            ranked = [r for r in ranked if r[2] in avail]
            if dropped:
                print(f"  ({len(dropped)} ranked TB tasks not in "
                      f"{a.tb_tasks_dir}, skipped)")
        print(f"\n{bench}: top {n} by median tool wall "
              f"({len(hist)} tasks in history)")
        for mw, cnt, t in ranked[:n]:
            print(f"  {mw:8.1f}s  (n={cnt})  {t}")
            lines.append(f"{bench}\t{t}")
        if len(ranked) < n:
            print(f"  WARN only {len(ranked)} available; asked for {n}")

    for bench, n, mf in (("hotpotqa", a.hotpot, a.hotpot_manifest),
                         ("freshqa", a.freshqa, a.freshqa_manifest)):
        if not n:
            continue
        if mf and mf.exists():
            ids = [ln.split("\t")[0] for ln in
                   mf.read_text().splitlines() if ln.strip()][:n]
        else:
            hist = from_summaries(a.results, bench)
            ids = sorted(hist)[:n]
        print(f"\n{bench}: {len(ids)} control task(s)")
        for t in ids:
            lines.append(f"{bench}\t{t}")

    a.out.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {len(lines)} tasks -> {a.out}")


if __name__ == "__main__":
    main()
