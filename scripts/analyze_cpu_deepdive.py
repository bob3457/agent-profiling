#!/usr/bin/env python3
"""Analyze results_cpu_deepdive/: per-run summary, per-tool perf, and
category-level CPU attribution. Adapted for x86 generic perf events
(cycles/instructions/cache-references instead of ARM cpu_cycles/inst_retired/
l1d_cache_refill). Run from the profiling root directory."""
import json
import re
from pathlib import Path

import pandas as pd


def read_json(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(errors="ignore"))
    except Exception:
        return {}


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def parse_time_v(path):
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(errors="ignore")
    pats = {
        "user_time_s": r"User time \(seconds\):\s*([0-9.]+)",
        "sys_time_s": r"System time \(seconds\):\s*([0-9.]+)",
        "cpu_percent": r"Percent of CPU this job got:\s*([0-9.]+)%",
        "max_rss_kb": r"Maximum resident set size \(kbytes\):\s*([0-9.]+)",
        "minor_faults_timev": r"Minor \(reclaiming a frame\) page faults:\s*([0-9.]+)",
        "major_faults_timev": r"Major \(requiring I/O\) page faults:\s*([0-9.]+)",
        "vol_ctx_switches_timev": r"Voluntary context switches:\s*([0-9.]+)",
        "invol_ctx_switches_timev": r"Involuntary context switches:\s*([0-9.]+)",
    }
    out = {}
    for k, pat in pats.items():
        m = re.search(pat, text)
        if m:
            out[k] = float(m.group(1))
    return out


def parse_perf(path):
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        raw, event = parts[0], parts[2]
        if raw.startswith("<") or not event:
            continue
        try:
            val = float(raw.replace(",", ""))
        except Exception:
            continue
        out["perf_" + re.sub(r"[^a-zA-Z0-9]+", "_", event).strip("_")] = val
    return out


def command_text(path):
    return Path(path).read_text(errors="ignore").strip() if Path(path).exists() else ""


def classify(command):
    c = command.lower()
    if "profile_hook.py" in c:
        return "codex_hook"
    if "shell_snapshots" in c or "snapshot file" in c or "declare -f" in c:
        return "codex_shell_snapshot"
    if "pip install" in c or "conda install" in c:
        return "dependency_setup"
    if "pytest" in c or "verify.sh" in c or "tox" in c or "unittest" in c:
        return "verification"
    if "python" in c:
        return "python_execution"
    if re.search(r"\b(rg|grep|find|fd)\b", c):
        return "search_navigation"
    if re.search(r"\b(sed|cat|head|tail|wc|od)\b", c):
        return "file_reading"
    if "git diff" in c or "git status" in c or "git show" in c:
        return "git_inspection"
    if "apply_patch" in c or "tee " in c or "echo " in c:
        return "file_edit"
    if re.search(r"\b(ls|pwd)\b", c):
        return "workspace_probe"
    return "other"


def parse_stdout_counts(path):
    rows = read_jsonl(path)
    out = {"turn_started": 0, "turn_completed": 0, "command_items": 0, "agent_messages": 0,
           "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    for e in rows:
        if e.get("type") == "turn.started":
            out["turn_started"] += 1
        if e.get("type") == "turn.completed":
            out["turn_completed"] += 1
            u = e.get("usage") or {}
            for k in ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]:
                out[k] += u.get(k, 0) or 0
        item = e.get("item") or {}
        if item.get("type") == "command_execution":
            out["command_items"] += 1
        if item.get("type") == "agent_message":
            out["agent_messages"] += 1
    return out


def first_present(row, *keys, default=0):
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return default


run_rows = []
tool_rows = []
for meta_path in sorted(Path("results_cpu_deepdive").glob("*/*/iter_*/metadata.json")):
    run_dir = meta_path.parent
    meta = read_json(meta_path)
    run = dict(meta)
    run.update(parse_time_v(run_dir / "time_v.txt"))
    run.update(parse_perf(run_dir / "perf_stat.csv"))
    run.update(parse_stdout_counts(run_dir / "stdout.jsonl"))
    wall_ms = run.get("wall_ms", 0) or 0
    task_ms = first_present(run, "perf_task_clock")
    run["cpu_time_s_timev"] = run.get("user_time_s", 0) + run.get("sys_time_s", 0)
    run["cpu_util_perf_pct"] = 100 * task_ms / wall_ms if wall_ms else 0
    run_rows.append(run)

    for tool_dir in sorted((run_dir / "tool_perf").glob("*")):
        if not tool_dir.is_dir():
            continue
        tmeta = read_json(tool_dir / "metadata.json")
        cmd = command_text(tool_dir / "command.txt")
        row = {"benchmark": meta.get("benchmark"), "example_id": meta.get("example_id"),
               "iteration": meta.get("iteration"), "run_wall_ms": meta.get("wall_ms", 0),
               "tool_id": tmeta.get("tool_id", tool_dir.name), "tool_wall_ms": tmeta.get("wall_ms", 0),
               "tool_returncode": tmeta.get("returncode"), "command": cmd, "category": classify(cmd)}
        row.update(parse_time_v(tool_dir / "time_v.txt"))
        row.update(parse_perf(tool_dir / "perf_stat.csv"))
        row["tool_cpu_time_s_timev"] = row.get("user_time_s", 0) + row.get("sys_time_s", 0)
        # x86: perf_cycles / perf_instructions. ARM fallbacks kept for
        # portability if you later run this on GH200 with ARM PMU names.
        cycles = first_present(row, "perf_cycles", "perf_cpu_cycles")
        inst = first_present(row, "perf_instructions", "perf_inst_retired")
        row["ipc"] = inst / cycles if cycles else 0
        cache_ref = first_present(row, "perf_cache_references", "perf_l1d_cache")
        cache_miss = first_present(row, "perf_cache_misses", "perf_l1d_cache_refill")
        row["cache_miss_rate"] = cache_miss / cache_ref if cache_ref else 0
        row["perf_cycles_norm"] = cycles
        row["perf_instructions_norm"] = inst
        tool_rows.append(row)

out_dir = Path("results_cpu_deepdive")
out_dir.mkdir(exist_ok=True)
run_df = pd.DataFrame(run_rows)
tool_df = pd.DataFrame(tool_rows)
run_df.to_csv(out_dir / "run_summary.csv", index=False)
tool_df.to_csv(out_dir / "per_tool_perf.csv", index=False)

if len(tool_df):
    category_df = tool_df.groupby(["benchmark", "category"]).agg(
        tool_calls=("tool_id", "count"),
        tool_wall_ms=("tool_wall_ms", "sum"),
        task_clock_ms=("perf_task_clock", "sum"),
        cpu_time_s_timev=("tool_cpu_time_s_timev", "sum"),
        cycles=("perf_cycles_norm", "sum"),
        instructions=("perf_instructions_norm", "sum"),
        context_switches=("perf_context_switches", "sum"),
        page_faults=("perf_page_faults", "sum"),
        max_rss_kb=("max_rss_kb", "max"),
    ).reset_index()
    category_df["task_clock_s"] = category_df["task_clock_ms"] / 1000.0
    category_df["ipc"] = category_df["instructions"] / category_df["cycles"]
    category_df.to_csv(out_dir / "category_perf_summary.csv", index=False)
    print(category_df.sort_values(["benchmark", "task_clock_s"], ascending=[True, False]).to_string(index=False))
else:
    print("No tool rows found.")
