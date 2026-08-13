#!/usr/bin/env python3
"""replay_segment_serve.py — validate segment-serve offline: replay every
family command from the 3-arm runs through the segment-serve planner
against each run's OWN spec cache (key files on disk), and report how many
cold/near-miss commands the planner would serve, with estimated savings.

Upper bound caveat: workspace-fingerprint (generation) validity cannot be
replayed offline — a fraction of planned serves would be gen-stale live.

Usage (repo root):
  python3 scripts/replay_segment_serve.py \
      [--runs /scratch/czhai/latency-eval/optionb_3arm] [--examples 8]
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "latency-opt" / "speculation"))
from spec_compound import (split_for_serve, fold_cd_serve, ro_passthrough,
                           is_state_cmd, is_cd_cmd)   # noqa: E402
from spec_families import family_key                   # noqa: E402


def plan(cmd, cwd, cache_dir):
    """Static segment plan against on-disk keys. Returns
    (n_serve, n_live, saved_s, stop_reason) or None if untouched."""
    split = split_for_serve(cmd)
    if not split:
        return None
    parts, eff_cwd = fold_cd_serve(split, cwd)
    if not parts or (len(parts) < 2 and len(parts) == len(split)
                     and eff_cwd == cwd):
        return None
    n_serve = n_live = 0
    saved = 0.0
    stop = None
    for text, _stopf, servable in parts:
        clean = servable and not is_state_cmd(text) and not is_cd_cmd(text)
        entry = None
        if clean:
            k = hashlib.sha256(f"{eff_cwd}\x00{text}".encode()).hexdigest()
            for key in (k, f"fam_{family_key(text) or ''}"):
                p = cache_dir / f"{key}.json"
                if key.endswith("_") or not p.exists():
                    continue
                try:
                    entry = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    entry = {}
                break
        if entry is not None:
            n_serve += 1
            saved += (entry or {}).get("duration_s") or 0.0
            continue
        if ro_passthrough(text):
            n_live += 1
            continue
        stop = text[:80]
        break
    if n_serve == 0:
        return None
    return n_serve, n_live, saved, stop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="/scratch/czhai/latency-eval/optionb_3arm")
    ap.add_argument("--examples", type=int, default=8)
    args = ap.parse_args()

    runs = Path(args.runs)
    tally = Counter()
    saved_total = 0.0
    newly = []
    for arm in ("qwen", "codex"):
        for d in sorted((runs / arm).glob("spec.*")):
            j = d / "logs/shelld/commands.jsonl"
            cache = d / "cache"
            if not j.exists() or not cache.is_dir():
                continue
            for l in j.open():
                c = json.loads(l)
                cmd = c.get("cmd", "")
                if not ("pytest" in cmd or "runtests" in cmd):
                    continue
                was = ("served" if c.get("cached")
                       else "near_miss" if c.get("near_miss") else "cold")
                tally[f"was_{was}"] += 1
                if was == "served":
                    continue
                r = plan(cmd, c.get("cwd") or "/testbed", cache)
                if r:
                    ns, nl, sv, _ = r
                    tally["would_now_serve"] += 1
                    saved_total += sv
                    if len(newly) < args.examples:
                        newly.append((arm, d.name.split(".")[1], ns, nl,
                                      round(sv, 1), cmd[:110]))
                else:
                    tally[f"still_{was}"] += 1

    print("family commands across spec arms:", dict(tally))
    was_miss = tally["was_near_miss"] + tally["was_cold"]
    now = tally["would_now_serve"]
    tot = was_miss + tally["was_served"]
    if tot:
        print(f"\nfamily serve rate: {tally['was_served']}/{tot} "
              f"({100*tally['was_served']/tot:.0f}%) -> "
              f"{tally['was_served']+now}/{tot} "
              f"({100*(tally['was_served']+now)/tot:.0f}%)  [upper bound: "
              f"generation-staleness not replayable]")
        print(f"estimated additional saved time: {saved_total:.0f}s")
    print("\nnewly served (examples):")
    for arm, iid, ns, nl, sv, cmd in newly:
        print(f"  [{arm}] {iid}: serve={ns} live={nl} +{sv}s\n      {cmd}")


if __name__ == "__main__":
    main()
