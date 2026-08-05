#!/usr/bin/env python3
"""ceiling_report.py — how many seconds could interior/suffix serving win?

Prefix-serve reaches only the LEADING cached run of a compound; the
measured pattern puts cheap unpredictable parts first and the expensive
predictable test invocation at position 2+. Before building interior
serving (run live parts, serve later cached parts that still validate),
measure its ceiling from data we already have.

For every live agent compound (commands.jsonl, cached=False): split with
the daemon's own split_for_serve/fold_cd_serve, and for each part check
whether an exact-key cache entry exists on disk NOW, weighting by the
entry's recorded duration_s. Parts classify as:

  leading   in the leading servable cached run  -> prefix-serve's reach
  interior  cached but after the first miss/hazard -> interior serving's
            ADDITIONAL reach (the ceiling this report exists to measure)
  unreachable  hazard/state/cd parts, or never cached

Cache state is end-of-run, so this is an upper bound (some entries landed
after the request); read it as a ceiling, not a forecast.

Usage: ceiling_report.py RESULTS_ROOT_OR_RUN_DIR [--repo ROOT]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def key_for(cwd, cmd):
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


def load_jsonl(p):
    out = []
    if p.exists():
        for ln in p.read_text(errors="replace").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def find_run_dirs(root: Path):
    if (root / "shelld_logs" / "commands.jsonl").exists():
        yield root
        return
    for p in sorted(root.glob("**/shelld_logs/commands.jsonl")):
        yield p.parent.parent


def entry_dur(cache: Path, cwd, text):
    f = cache / f"{key_for(cwd, text)}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("duration_s") or 0.0
    except (OSError, json.JSONDecodeError):
        return None


def analyze(run, split_for_serve, fold_cd_serve, is_state_cmd, is_cd_cmd):
    cache = run / "spec_cache"
    r = {"task": f"{run.parent.name}/{run.name}",
         "live_s": 0.0, "leading_s": 0.0, "interior_s": 0.0,
         "interior_parts": 0, "compounds": 0, "examples": []}
    for c in load_jsonl(run / "shelld_logs" / "commands.jsonl"):
        if c.get("cached"):
            continue
        wall = c.get("wall_s") or 0.0
        r["live_s"] += wall
        split = split_for_serve(c.get("cmd", ""))
        if not split:
            continue
        parts, eff_cwd = fold_cd_serve(split, c.get("cwd", ""))
        if len(parts) < 2:
            continue
        r["compounds"] += 1
        in_leading = True
        for text, _stop, servable in parts:
            if not servable or is_state_cmd(text) or is_cd_cmd(text):
                in_leading = False
                continue
            d = entry_dur(cache, eff_cwd, text)
            if d is None:
                in_leading = False
                continue
            if in_leading:
                r["leading_s"] += d
            else:
                r["interior_s"] += d
                r["interior_parts"] += 1
                if d >= 1.0 and len(r["examples"]) < 3:
                    r["examples"].append((round(d, 1), text[:100]))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--repo", type=Path,
                    default=Path("/projects/kzhou6/czhai/agent-profiling"))
    a = ap.parse_args()
    sys.path.insert(0, str(a.repo / "latency-opt" / "speculation"))
    from spec_compound import (split_for_serve, fold_cd_serve,  # noqa: E402
                               is_state_cmd, is_cd_cmd)

    rows = [analyze(d, split_for_serve, fold_cd_serve, is_state_cmd,
                    is_cd_cmd) for d in find_run_dirs(a.root)]
    if not rows:
        print(f"no run dirs under {a.root}", file=sys.stderr)
        sys.exit(2)
    print(f"{'task':44} {'live_s':>8} {'leading':>8} {'INTERIOR':>9} "
          f"{'parts':>6}")
    tot = {"live_s": 0.0, "leading_s": 0.0, "interior_s": 0.0,
           "interior_parts": 0}
    for r in rows:
        for k in tot:
            tot[k] += r[k]
        if r["compounds"] == 0:
            continue
        print(f"{r['task'][:44]:44} {r['live_s']:8.1f} {r['leading_s']:8.1f} "
              f"{r['interior_s']:9.1f} {r['interior_parts']:6}")
        for d, ex in r["examples"]:
            print(f"    {d:6.1f}s interior: {ex}")
    print("-" * 80)
    frac = (tot["interior_s"] / tot["live_s"] * 100) if tot["live_s"] else 0
    print(f"CEILING: leading (prefix reach) {tot['leading_s']:.1f}s | "
          f"interior (suffix-serve would ADD) {tot['interior_s']:.1f}s "
          f"across {tot['interior_parts']} parts = {frac:.1f}% of "
          f"{tot['live_s']:.1f}s live tool time")
    print("(upper bound: end-of-run cache state; entries that landed late "
          "count as reachable)")


if __name__ == "__main__":
    main()
