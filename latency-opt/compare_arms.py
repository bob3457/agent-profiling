#!/usr/bin/env python3
"""compare_arms.py — speculation's net effect: spec vs base runs.

For each instance with both a base.* and spec.* run dir (newest each),
reports wall clock, tool wall (sum of per-command wall_s from the daemon's
commands.jsonl), serve count, and speculation's token overhead (the worker's
predictor calls, parsed from spec.log; the watcher costs zero tokens).

Usage: python3 compare_arms.py /scratch/czhai/latency-eval/optionb
"""
import json
import re
import sys
from pathlib import Path

TOK_RE = re.compile(r"tokens=\{[^}]*'input_tokens': (\d+)[^}]*'output_tokens': (\d+)")
ELAPSED_RE = re.compile(r"Elapsed \(wall clock\)[^\n]*?([\d:]+\.\d+)\s*$", re.M)


def wall_s(run: Path):
    t = run / "time.txt"
    if not t.exists():
        return None
    m = ELAPSED_RE.search(t.read_text(errors="replace"))
    if not m:
        return None
    parts = m.group(1).split(":")          # ss.xx | m:ss.xx | h:mm:ss.xx
    s = float(parts[-1])
    if len(parts) > 1:
        s += int(parts[-2]) * 60
    if len(parts) > 2:
        s += int(parts[-3]) * 3600
    return round(s, 1)


def tool_stats(run: Path):
    f = run / "logs" / "shelld" / "commands.jsonl"
    tw, n, hits = 0.0, 0, 0
    if f.exists():
        for ln in f.read_text(errors="replace").splitlines():
            try:
                c = json.loads(ln)
            except json.JSONDecodeError:
                continue
            n += 1
            tw += c.get("wall_s") or 0
            hits += bool(c.get("cached"))
    return round(tw, 1), n, hits


def spec_tokens(run: Path):
    f = run / "spec.log"
    inp = out = 0
    if f.exists():
        for m in TOK_RE.finditer(f.read_text(errors="replace")):
            inp += int(m.group(1))
            out += int(m.group(2))
    return inp, out


def newest(base: Path, prefix: str):
    d = {}
    for p in base.glob(prefix + ".*"):
        parts = p.name.split(".")
        if len(parts) < 3:
            continue
        iid = parts[1]
        if iid not in d or p.stat().st_mtime > d[iid].stat().st_mtime:
            d[iid] = p
    return d


def main():
    base_dir = Path(sys.argv[1])
    bases, specs = newest(base_dir, "base"), newest(base_dir, "spec")
    both = sorted(set(bases) & set(specs))
    if not both:
        sys.exit("no instances with BOTH base.* and spec.* runs -- "
                 "run the base arm first")
    hdr = (f"{'instance':<28}{'base_wall':>10}{'spec_wall':>10}{'d_wall':>8}"
           f"{'base_tw':>9}{'spec_tw':>9}{'d_tw':>7}{'serves':>7}"
           f"{'tok_in':>8}{'tok_out':>8}")
    print(hdr)
    print("-" * len(hdr))
    tots = [0.0] * 6
    tok = [0, 0]
    for iid in both:
        bw, sw = wall_s(bases[iid]), wall_s(specs[iid])
        btw, _, _ = tool_stats(bases[iid])
        stw, _, hits = tool_stats(specs[iid])
        ti, to = spec_tokens(specs[iid])
        dw = round((sw or 0) - (bw or 0), 1) if bw and sw else None
        dtw = round(stw - btw, 1)
        print(f"{iid:<28}{bw or '?':>10}{sw or '?':>10}{dw if dw is not None else '?':>8}"
              f"{btw:>9}{stw:>9}{dtw:>7}{hits:>7}{ti:>8}{to:>8}")
        if bw and sw:
            tots[0] += bw; tots[1] += sw
        tots[2] += btw; tots[3] += stw; tots[4] += hits
        tok[0] += ti; tok[1] += to
    print("-" * len(hdr))
    print(f"TOTALS: wall {tots[0]:.0f}s -> {tots[1]:.0f}s "
          f"({tots[1]-tots[0]:+.0f}s), tool_wall {tots[2]:.1f}s -> "
          f"{tots[3]:.1f}s ({tots[3]-tots[2]:+.1f}s), "
          f"{int(tots[4])} serves, predictor tokens {tok[0]} in / {tok[1]} out")
    print("\ncaveats: wall clock is dominated by model-inference variance -- "
          "tool_wall is the meaningful column; n=1 per arm is noisy, prefer "
          "3x repeats before quoting; spec tool_wall EXCLUDES watcher/worker "
          "compute by construction (they never pass through the daemon), so "
          "d_tw isolates the agent's own command time (serves + warm-cache "
          "effects vs contention).")


if __name__ == "__main__":
    main()
