#!/usr/bin/env python3
"""spec_perf_report.py — flatten per-command perf dirs into one CSV.

Consumes directories in the codex_tool_perf_wrap.sh / spec_perf.py schema
(each containing perf_stat.csv, time_v.txt, metadata.json) and emits one row
per command with derived metrics:

    ipc                 instructions / cycles
    cache_miss_rate     cache-misses / cache-references
    cpu_util            task_clock_ms / wall_ms   (how parallel/CPU-bound)
    max_rss_kb          from /usr/bin/time -v

Works on both the speculative dirs (SPEC_PERF_DIR) and Tejas-style agent
tool_perf dirs, so spec-side and agent-side commands can be compared in one
table. Rows are sorted by task-clock descending: the top of the file is where
the CPU went.

Usage:
    python3 spec_perf_report.py RUN_DIR/spec_perf [MORE_DIRS...] \
        [--csv out.csv] [--min-wall-ms 0]
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

EVENT_KEYS = {
    "task-clock": "task_clock_ms",
    "cycles": "cycles",
    "instructions": "instructions",
    "cache-references": "cache_references",
    "cache-misses": "cache_misses",
    "branches": "branches",
    "context-switches": "context_switches",
    "cpu-migrations": "cpu_migrations",
    "page-faults": "page_faults",
    # ARM PMU names (Neoverse) map onto the same columns
    "l1d_cache": "cache_references",
    "l1d_cache_refill": "cache_misses",
}


def parse_perf_csv(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        val, _unit, event = parts[0], parts[1], parts[2]
        col = EVENT_KEYS.get(event.strip().split(":")[0])
        if col is None:
            continue
        try:
            out[col] = float(val)
        except ValueError:
            pass  # <not counted> / <not supported>
    return out


def parse_time_v(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", text)
    if m:
        out["max_rss_kb"] = int(m.group(1))
    m = re.search(r"Voluntary context switches: (\d+)", text)
    if m:
        out["vol_ctx_switches"] = int(m.group(1))
    m = re.search(r"File system inputs: (\d+)", text)
    if m:
        out["fs_inputs"] = int(m.group(1))
    m = re.search(r"File system outputs: (\d+)", text)
    if m:
        out["fs_outputs"] = int(m.group(1))
    return out


def family_of(cmd: str) -> str:
    try:
        sys.path.insert(0, str(Path(__file__).parent / "speculation"))
        from spec_families import family_key
        return family_key(cmd) or ""
    except Exception:
        return ""


def collect(root: Path):
    rows = []
    for meta_p in sorted(root.rglob("metadata.json")):
        d = meta_p.parent
        try:
            meta = json.loads(meta_p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cmd = meta.get("command", "")
        if not cmd:
            try:
                cmd = (d / "command.txt").read_text().strip()
            except OSError:
                cmd = ""
        row = {
            "tool_id": meta.get("tool_id", d.name),
            "source": meta.get("spec_source", "agent"),
            "action": meta.get("action", ""),
            "command": cmd[:200],
            "family": family_of(cmd),
            "wall_ms": meta.get("wall_ms"),
            "returncode": meta.get("returncode"),
            "timeout": meta.get("timeout", False),
            "raced": meta.get("raced", ""),
        }
        row.update(parse_perf_csv(d / "perf_stat.csv"))
        row.update(parse_time_v(d / "time_v.txt"))
        cyc, ins = row.get("cycles"), row.get("instructions")
        if cyc and ins:
            row["ipc"] = round(ins / cyc, 3)
        refs, miss = row.get("cache_references"), row.get("cache_misses")
        if refs and miss is not None:
            row["cache_miss_rate"] = round(miss / refs, 4)
        tc, wall = row.get("task_clock_ms"), row.get("wall_ms")
        if tc and wall:
            row["cpu_util"] = round(tc / wall, 3)
        rows.append(row)
    return rows


COLS = ["tool_id", "source", "action", "family", "wall_ms", "task_clock_ms",
        "cpu_util", "ipc", "cache_miss_rate", "max_rss_kb", "instructions",
        "cycles", "cache_references", "cache_misses", "context_switches",
        "page_faults", "fs_inputs", "fs_outputs", "returncode", "timeout",
        "raced", "command"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--csv", default="")
    ap.add_argument("--min-wall-ms", type=float, default=0.0,
                    help="drop rows shorter than this (startup-noise filter)")
    args = ap.parse_args()

    rows = []
    for d in args.dirs:
        rows += collect(Path(d))
    rows = [r for r in rows if (r.get("wall_ms") or 0) >= args.min_wall_ms]
    rows.sort(key=lambda r: r.get("task_clock_ms") or 0, reverse=True)

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    out = open(args.csv, "w", newline="") if args.csv else sys.stdout
    w = csv.DictWriter(out, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    if args.csv:
        out.close()
        n_to = sum(1 for r in rows if r.get("timeout"))
        print(f"{len(rows)} commands -> {args.csv} ({n_to} timeouts)")


if __name__ == "__main__":
    main()
