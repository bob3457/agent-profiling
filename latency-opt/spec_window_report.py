#!/usr/bin/env python3
"""spec_window_report.py — the study table for the speculation sweep.

Walks spec.* result dirs (newest per instance by default), merges each run's
respec.log (bumps, re-runs, idle activity), serve_decisions.jsonl (every
daemon lookup with reason), and spec.log (worker/predictor), and reports:

  per instance : generations, idle runs, post-edit re-runs, speculation CPU,
                 decision counts, and for every speculation-relevant query
                 the GAP between the last generation bump and the query --
                 the edit->verify window that decides feasibility
  aggregate    : hit count, seconds served, window distribution, total
                 speculation compute (the honest arm-C overhead number)

Usage:  python3 spec_window_report.py /scratch/czhai/latency-eval/optionb
        python3 spec_window_report.py <dir> --all-runs   (not just newest)
"""
import argparse
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

BUMP_RE = re.compile(r"\[respec (\d\d:\d\d:\d\d)\] edit detected -> generation (\d+)")
RUN_RE = re.compile(r"\[respec (\d\d:\d\d:\d\d)\] gen (\d+): cached exit=(-?\d+) "
                    r"([\d.]+)s '(.*)' keys=")
IDLE_RE = re.compile(r"\[respec \d\d:\d\d:\d\d\] idle: (\d+) new")


def hms_to_s(hms):
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def analyze(run_dir: Path):
    name = run_dir.name
    out = {"dir": name,
           "instance": name.split(".")[1] if name.startswith(("spec.", "base."))
           else f"{run_dir.parent.name}/{name}"}
    respec = next((run_dir / c for c in ("logs/respec.log", "respec.log")
                   if (run_dir / c).exists()), run_dir / "respec.log")
    # classify each cached run by the most recent log marker: runs after an
    # "idle:" line are pre/between-edit speculation, runs after an "edit
    # detected" bump are post-edit re-runs (idle batches also happen at
    # gen > 0, so the generation number alone cannot distinguish the two)
    bumps, runs, idle_events, mode = [], [], 0, "idle"
    if respec.exists():
        for ln in respec.read_text(errors="replace").splitlines():
            m = BUMP_RE.search(ln)
            if m:
                bumps.append((hms_to_s(m.group(1)), int(m.group(2))))
                mode = "postedit"
            m = RUN_RE.search(ln)
            if m:
                runs.append({"t": hms_to_s(m.group(1)), "gen": int(m.group(2)),
                             "exit": int(m.group(3)), "dur": float(m.group(4)),
                             "cmd": m.group(5), "mode": mode})
            if IDLE_RE.search(ln):
                idle_events += 1
                mode = "idle"
    out["gens"] = len(bumps)
    out["idle_events"] = idle_events
    out["idle_runs"] = sum(1 for r in runs if r["mode"] == "idle")
    out["postedit_runs"] = sum(1 for r in runs if r["mode"] == "postedit")
    out["spec_cpu_s"] = round(sum(r["dur"] for r in runs), 1)
    dur_by_cmd = {r["cmd"]: r["dur"] for r in runs}

    decisions = collections.Counter()
    windows, served_saved = [], 0.0
    dfile = next((run_dir / c for c in
                  ("cache/serve_decisions.jsonl",
                   "spec_cache/serve_decisions.jsonl")
                  if (run_dir / c).exists()),
                 run_dir / "cache" / "serve_decisions.jsonl")
    if dfile.exists():
        for ln in dfile.read_text(errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            d = r["decision"].split("(")[0]
            decisions[d] += 1
            q = dt.datetime.fromtimestamp(r["ts"])
            q_s = q.hour * 3600 + q.minute * 60 + q.second + q.microsecond / 1e6
            # respec.log timestamps are seconds-of-day; a run crossing
            # midnight makes queries appear earlier than every bump
            if bumps and q_s + 0.5 < bumps[0][0]:
                q_s += 86400
            prior = [b for b, _ in bumps if b <= q_s + 0.5]
            if prior and d in ("served", "stale_generation", "prefix_serve"):
                windows.append(round(q_s - prior[-1], 1))
            if d == "served":
                dur = r.get("entry_dur_s")
                if dur is None:
                    dur = dur_by_cmd.get(r.get("entry_cmd", ""), 0.0)
                served_saved += dur or 0.0
            elif d == "prefix_serve":
                served_saved += r.get("saved_s") or 0.0
    out["decisions"] = dict(decisions)
    out["bump_to_query_s"] = windows
    out["served"] = decisions.get("served", 0) + decisions.get("prefix_serve", 0)
    out["prefix_serves"] = decisions.get("prefix_serve", 0)
    out["served_saved_s"] = round(served_saved, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="optionb results base dir")
    ap.add_argument("--all-runs", action="store_true",
                    help="analyze every spec.* dir, not just newest/instance")
    args = ap.parse_args()

    dirs = sorted(Path(args.base).glob("spec.*"))
    if not dirs:
        # run_latency_arm layout: $RESULTS/<bench>/<tid>/ with respec.log
        # and spec_cache inside each run dir
        dirs = sorted(d for d in Path(args.base).glob("*/*/") if
                      (d / "respec.log").exists() or (d / "spec_cache").is_dir())
        args.all_runs = True
    if not args.all_runs:
        newest = {}
        for d in dirs:
            inst = d.name.split(".")[1] if d.name.count(".") >= 2 else d.name
            if inst not in newest or d.stat().st_mtime > newest[inst].stat().st_mtime:
                newest[inst] = d
        dirs = sorted(newest.values())
    if not dirs:
        sys.exit(f"no spec.* dirs under {args.base}")

    rows = [analyze(d) for d in dirs]
    hdr = f"{'instance':<28}{'gens':>5}{'idle':>5}{'post':>5}{'cpu_s':>7}" \
          f"{'served':>7}{'saved_s':>8}  decisions / bump->query gaps (s)"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        dec = ",".join(f"{k}:{v}" for k, v in sorted(r["decisions"].items())) or "-"
        gaps = r["bump_to_query_s"]
        print(f"{r['instance']:<28}{r['gens']:>5}{r['idle_runs']:>5}"
              f"{r['postedit_runs']:>5}{r['spec_cpu_s']:>7}{r['served']:>7}"
              f"{r['served_saved_s']:>8}  {dec}  {gaps if gaps else ''}")

    allw = sorted(w for r in rows for w in r["bump_to_query_s"])
    tot_served = sum(r["served"] for r in rows)
    tot_saved = round(sum(r["served_saved_s"] for r in rows), 1)
    tot_cpu = round(sum(r["spec_cpu_s"] for r in rows), 1)
    print(f"\nTOTAL: {tot_served} served, {tot_saved}s saved, "
          f"{tot_cpu}s speculation compute across {len(rows)} runs")
    if allw:
        med = allw[len(allw) // 2]
        print(f"edit->verify windows (bump->query, s): n={len(allw)} "
              f"min={allw[0]} med={med} max={allw[-1]}  all={allw}")
        print("interpretation: windows below your targeted-test cost "
              "(~1.6-2.8s here) are photo-finishes no re-run can win; "
              "windows above it are the mechanism's addressable market.")


if __name__ == "__main__":
    main()
