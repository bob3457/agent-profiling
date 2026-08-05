#!/usr/bin/env python3
"""compare_runs.py — task-matched wall-time comparison across arm sweeps.

Answers "did the patch fix the latency problem": for every bench/task
present in BOTH results roots, reports wall clock (time.txt), tool wall
(live commands.jsonl wall_s), serves + saved_s (decisions log), and the
deltas. Aggregates per bench and overall, with a sign summary (tasks
faster/slower under the second root).

Usage:
  python3 compare_runs.py RESULTS_A RESULTS_C [RESULTS_C2 ...]
  (first root is the baseline; each later root is compared against it)
"""
import json
import re
import sys
from pathlib import Path

ELAPSED_RE = re.compile(r"Elapsed \(wall clock\)[^\n]*?([\d:]+\.?\d*)\s*$",
                        re.M)


def wall_s(run: Path):
    t = run / "time.txt"
    if not t.exists():
        return None
    m = ELAPSED_RE.search(t.read_text(errors="replace"))
    if not m:
        return None
    parts = m.group(1).split(":")
    s = float(parts[-1])
    if len(parts) > 1:
        s += int(parts[-2]) * 60
    if len(parts) > 2:
        s += int(parts[-3]) * 3600
    return round(s, 1)


def tool_wall(run: Path):
    f = run / "shelld_logs" / "commands.jsonl"
    tw = 0.0
    if f.exists():
        for ln in f.read_text(errors="replace").splitlines():
            try:
                c = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not c.get("cached"):
                tw += c.get("wall_s") or 0.0
    return round(tw, 2)


def serves(run: Path):
    f = run / "spec_cache" / "serve_decisions.jsonl"
    n, saved = 0, 0.0
    if f.exists():
        for ln in f.read_text(errors="replace").splitlines():
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            dec = d.get("decision", "")
            if dec == "served":
                n += 1
                saved += d.get("entry_dur_s") or 0.0
            elif dec == "prefix_serve":
                n += 1
                saved += d.get("saved_s") or 0.0
            elif dec.startswith("joined_inflight("):
                saved -= float(dec.split("(")[1].rstrip("s)"))
    return n, round(saved, 2)


def tasks(root: Path):
    out = {}
    for t in sorted(root.glob("*/*/")):
        if (t / "time.txt").exists():
            out[f"{t.parent.name}/{t.name}"] = t
    return out


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def pooled(base_roots, test_roots):
    import math
    bsets = [tasks(Path(r)) for r in base_roots]
    tsets = [tasks(Path(r)) for r in test_roots]
    names = set.intersection(*[set(x) for x in bsets + tsets])
    print(f"pooled: {len(base_roots)} base root(s) x {len(test_roots)} "
          f"test root(s), {len(names)} matched tasks")
    print(f"{'task':42} {'medA':>7} {'A rng':>13} {'medC':>7} "
          f"{'C rng':>13} {'dMed':>7} {'srv':>4}")
    agg = {}
    for t in sorted(names):
        wa = [w for st in bsets if (w := wall_s(st[t])) is not None]
        wc = [w for st in tsets if (w := wall_s(st[t])) is not None]
        if not wa or not wc:
            continue
        ma, mc = median(wa), median(wc)
        srv = sum(serves(st[t])[0] for st in tsets)
        d = mc - ma
        b = t.split("/")[0]
        a = agg.setdefault(b, {"n": 0, "d": 0.0, "ma": 0.0,
                               "faster": 0, "slower": 0, "srv": 0})
        a["n"] += 1; a["d"] += d; a["ma"] += ma; a["srv"] += srv
        if d < 0: a["faster"] += 1
        elif d > 0: a["slower"] += 1
        print(f"{t[:42]:42} {ma:>7.1f} {min(wa):>5.1f}-{max(wa):<6.1f} "
              f"{mc:>7.1f} {min(wc):>5.1f}-{max(wc):<6.1f} {d:>+7.1f} "
              f"{srv:>4}")
    print("-" * 100)
    F = sum(a["faster"] for a in agg.values())
    S = sum(a["slower"] for a in agg.values())
    for b, a in sorted(agg.items()):
        pct = a["d"] / a["ma"] * 100 if a["ma"] else 0.0
        print(f"  {b:15} dMedian {a['d']:+8.1f}s ({pct:+5.1f}%)  "
              f"faster {a['faster']}/{a['n']}  serves {a['srv']}")
    n = F + S
    if n:
        # two-sided exact sign test on per-task median deltas
        pv = sum(math.comb(n, k) for k in range(0, min(F, S) + 1))             / 2 ** n * 2
        pv = min(pv, 1.0)
        td = sum(a["d"] for a in agg.values())
        tma = sum(a["ma"] for a in agg.values())
        print(f"  {'OVERALL':15} dMedian {td:+8.1f}s "
              f"({td / tma * 100 if tma else 0:+5.1f}%)  "
              f"faster {F} slower {S}  sign-test p={pv:.3f}")
        print("  (p<0.05 = the direction is real; otherwise still noise)")


def main():
    if "--base" in sys.argv:
        i, j = sys.argv.index("--base"), sys.argv.index("--test")
        pooled(sys.argv[i + 1:j], sys.argv[j + 1:])
        return
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    base_root = Path(sys.argv[1])
    base = tasks(base_root)
    print(f"baseline: {base_root.name}  ({len(base)} tasks)")
    for other in sys.argv[2:]:
        oroot = Path(other)
        o = tasks(oroot)
        common = sorted(set(base) & set(o))
        print(f"\n=== {oroot.name} vs baseline  ({len(common)} matched)")
        print(f"{'task':42} {'wallA':>7} {'wallC':>7} {'dWall':>7} "
              f"{'toolA':>7} {'toolC':>7} {'srv':>4} {'saved':>7}")
        agg = {}
        for t in common:
            wa, wc = wall_s(base[t]), wall_s(o[t])
            ta, tc = tool_wall(base[t]), tool_wall(o[t])
            n, sv = serves(o[t])
            d = (wc - wa) if wa is not None and wc is not None else None
            b = t.split("/")[0]
            a = agg.setdefault(b, {"n": 0, "dw": 0.0, "wa": 0.0,
                                   "faster": 0, "slower": 0, "srv": 0,
                                   "saved": 0.0})
            if d is not None:
                a["n"] += 1
                a["dw"] += d
                a["wa"] += wa
                a["faster" if d < 0 else "slower"] += (d != 0)
            a["srv"] += n
            a["saved"] += sv
            print(f"{t[:42]:42} {wa if wa is not None else '?':>7} "
                  f"{wc if wc is not None else '?':>7} "
                  f"{(f'{d:+.1f}' if d is not None else '?'):>7} "
                  f"{ta:>7.1f} {tc:>7.1f} {n:>4} {sv:>7.2f}")
        print("-" * 92)
        tn = sum(a["n"] for a in agg.values())
        tdw = sum(a["dw"] for a in agg.values())
        twa = sum(a["wa"] for a in agg.values())
        for b, a in sorted(agg.items()):
            pct = (a["dw"] / a["wa"] * 100) if a["wa"] else 0.0
            print(f"  {b:15} dWall {a['dw']:+8.1f}s ({pct:+5.1f}%)  "
                  f"faster {a['faster']}/{a['n']}  serves {a['srv']} "
                  f"saved {a['saved']:.1f}s")
        if twa:
            print(f"  {'OVERALL':15} dWall {tdw:+8.1f}s "
                  f"({tdw / twa * 100:+5.1f}%)  over {tn} matched tasks")
            print("  (negative dWall = second root faster; per-task noise "
                  "is real, judge the aggregate and the sign counts)")


if __name__ == "__main__":
    main()
