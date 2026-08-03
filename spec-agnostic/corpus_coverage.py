#!/usr/bin/env python3
"""corpus_coverage.py — replay a logged command corpus through the tier
policy + compound splitter and report speculation coverage.

Answers, before any live run: of the commands agents actually issued, what
fraction is servable (whole or as a compound prefix), in commands AND in
wall-seconds, and which refused heads are the biggest remaining tail.

Inputs (positional, any mix):
  * a directory            -> rglob for commands.jsonl (shelld logs)
  * a *.jsonl file         -> one JSON object per line with a "cmd" field
                              ("wall_s" used for seconds-coverage if present)
  * any other file         -> plain text, one command per line

Usage:
  python3 corpus_coverage.py /scratch/czhai/latency-eval/results \
      --spec-dir /projects/kzhou6/czhai/agent-profiling/latency-opt/speculation
  # compare a candidate tier module against the deployed one:
  python3 corpus_coverage.py corpus.txt --spec-dir ... --tiers ./spec_tiers.py
"""
import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def iter_commands(paths):
    """Yield (cmd, wall_s_or_None)."""
    for p in paths:
        p = Path(p)
        files = sorted(p.rglob("commands.jsonl")) if p.is_dir() else [p]
        for f in files:
            if f.suffix == ".jsonl":
                for line in f.open():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cmd = rec.get("cmd")
                    if cmd:
                        yield cmd, rec.get("wall_s")
            else:
                for line in f.open():
                    line = line.rstrip("\n")
                    if line.strip():
                        yield line, None


def head_of(cmd):
    tok = cmd.strip().split(None, 1)
    return tok[0].rsplit("/", 1)[-1][:30] if tok else "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--spec-dir", required=True,
                    help="latency-opt/speculation dir (for spec_compound)")
    ap.add_argument("--tiers", default=None,
                    help="path to a spec_tiers.py to evaluate "
                         "(default: the one in --spec-dir)")
    ap.add_argument("--unique", action="store_true",
                    help="dedup identical command strings before counting")
    ap.add_argument("--top", type=int, default=25,
                    help="how many refused heads to list")
    args = ap.parse_args()

    spec_dir = Path(args.spec_dir)
    compound = load_module("cc_spec_compound", spec_dir / "spec_compound.py")
    tiers = load_module("cc_spec_tiers",
                        Path(args.tiers) if args.tiers
                        else spec_dir / "spec_tiers.py")

    cmds = list(iter_commands(args.paths))
    if args.unique:
        seen, uniq = set(), []
        for c, w in cmds:
            if c not in seen:
                seen.add(c)
                uniq.append((c, w))
        cmds = uniq
    if not cmds:
        print("no commands found")
        return 1

    n = len(cmds)
    have_walls = any(w is not None for _, w in cmds)
    total_s = sum(w or 0 for _, w in cmds)

    simple = compound_n = 0
    whole_srv = 0                     # simple commands classified tier0/1
    comp_full = comp_partial = comp_none = comp_refused = 0
    lead_parts_served = lead_parts_total = 0
    srv_cmds = 0                      # commands with ANY servable surface
    srv_s = 0.0                       # seconds attributable to servable work
    refused_heads = Counter()
    refused_secs = Counter()
    part_refused_heads = Counter()

    for cmd, wall in cmds:
        parts = compound.split_for_serve(cmd)
        if parts is not None and len(parts) > 1:
            # match daemon serve semantics: a leading `cd X &&` folds into cwd
            parts, _cwd = compound.fold_cd_serve(parts, "/")
            if not parts:
                parts = [("true", True, True)]   # pure-cd compound: trivially servable
        if parts is not None and len(parts) > 1:
            compound_n += 1
            # a part is servable iff the splitter allows it AND tiers allow it
            flags = [srv and tiers.classify(p) != tiers.NONE
                     for p, _stop, srv in parts]
            lead = 0
            for f in flags:
                if not f:
                    break
                lead += 1
            lead_parts_served += lead
            lead_parts_total += len(parts)
            for (p, _s, _v), f in zip(parts, flags):
                if not f:
                    part_refused_heads[head_of(p)] += 1
            if all(flags):
                comp_full += 1
            elif lead > 0:
                comp_partial += 1
            else:
                comp_none += 1
            if lead > 0:
                srv_cmds += 1
                if wall:
                    srv_s += wall * (lead / len(parts))   # pro-rata estimate
        else:
            if parts is None and any(j in cmd for j in ("&&", ";", "||", "|")):
                comp_refused += 1     # structural refusal (heredoc/||/&)
            simple += 1
            eff = parts[0][0] if parts else cmd   # cd-folded single remainder
            t = tiers.classify(eff)
            if t != tiers.NONE:
                whole_srv += 1
                srv_cmds += 1
                if wall:
                    srv_s += wall
            else:
                refused_heads[head_of(eff)] += 1
                if wall:
                    refused_secs[head_of(eff)] += wall

    def pct(a, b):
        return f"{100.0 * a / b:5.1f}%" if b else "  n/a"

    print(f"corpus: {n} commands"
          + (f", {total_s:,.1f}s logged wall" if have_walls else "")
          + (" (unique)" if args.unique else ""))
    print(f"tiers module: {tiers.__file__}")
    print()
    print(f"simple commands        {simple:6d}  servable {whole_srv:6d}  ({pct(whole_srv, simple)})")
    print(f"compound commands      {compound_n:6d}")
    print(f"  fully servable       {comp_full:6d}  ({pct(comp_full, compound_n)})")
    print(f"  leading-prefix only  {comp_partial:6d}  ({pct(comp_partial, compound_n)})")
    print(f"  nothing servable     {comp_none:6d}  ({pct(comp_none, compound_n)})")
    print(f"  structurally refused {comp_refused:6d}  (heredoc/||/& — counted as simple)")
    if lead_parts_total:
        print(f"  leading parts served {lead_parts_served}/{lead_parts_total} "
              f"({pct(lead_parts_served, lead_parts_total)})")
    print()
    print(f"ANY servable surface   {srv_cmds:6d}/{n}  ({pct(srv_cmds, n)}) of commands")
    if have_walls:
        print(f"seconds coverage       {srv_s:,.1f}s/{total_s:,.1f}s "
              f"({pct(srv_s, total_s)}) of tool wall (prefix pro-rata)")
    print()
    print(f"top refused heads (simple commands){' — cmds / secs' if have_walls else ''}:")
    for h, c in refused_heads.most_common(args.top):
        line = f"  {h:<24} {c:5d}"
        if have_walls:
            line += f"   {refused_secs.get(h, 0):9.1f}s"
        print(line)
    if part_refused_heads:
        print("\ntop refused heads (compound parts):")
        for h, c in part_refused_heads.most_common(args.top):
            print(f"  {h:<24} {c:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
