#!/usr/bin/env python3
"""miss_autopsy.py — why didn't the expensive commands get served?

The sweep's failure mode is CONVERSION: the predictor's commands score high
on semantic match (ledger mean 0.712) yet realized precision is ~1%.
Between "predicted correctly" and "served" sit several distinct gaps, each
with a different fix. For every live (uncached) agent command, ranked by
wall_s, this classifies the miss:

  entry_now        the exact key HAS an entry on disk now -> it existed but
                   didn't validate at request time, or landed after the
                   request (timing loss)
  cwd_mismatch     identical command cached under a different cwd
                   (symlink/realpath drift; keys are sha256(cwd\\0cmd))
  string_near      a cached command in the same cwd differs only slightly
                   (whitespace/quotes/flag order) -> shows the diff
  family_present   the agent command's family key exists in cache but was
                   not served (family entry invalid or lookup skipped)
  prefix_missed    compound whose leading parts were individually cached
                   but no prefix_serve fired
  never_predicted  nothing close in the cache (nearest by token jaccard)

Plus per-run wiring checks: spec_cache dir present? entry count? decisions
log present? daemon saw --spec-cache? (inferred from daemon_stats /
decision activity)

Usage:
  python3 miss_autopsy.py RUN_DIR_OR_ROOT [--repo ROOT] [--top 12]
                          [--min-wall 0.5] [--jsonl]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def key_for(cwd, cmd):
    return hashlib.sha256(f"{cwd}\x00{cmd}".encode()).hexdigest()


def norm(cmd):
    return re.sub(r"\s+", " ", (cmd or "").strip().strip("'\""))


def toks(cmd):
    return set(re.findall(r"[A-Za-z0-9_./=-]+", cmd or ""))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_jsonl(path):
    out = []
    if path.exists():
        for ln in path.read_text(errors="replace").splitlines():
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


def load_cache(cache_dir: Path):
    """[{key, cmd, cwd, dur, family}] from every entry json on disk."""
    entries = []
    if not cache_dir.is_dir():
        return entries
    for f in cache_dir.glob("*.json"):
        try:
            e = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        entries.append({"key": f.stem, "cmd": e.get("cmd", ""),
                        "cwd": e.get("cwd", ""),
                        "dur": e.get("duration_s") or 0.0,
                        "at": e.get("speculated_at"),
                        "family": f.stem.startswith("fam_")})
    return entries


def autopsy(run_dir: Path, repo: Path, top: int, min_wall: float):
    r = {"dir": str(run_dir),
         "task": f"{run_dir.parent.name}/{run_dir.name}", "wiring": {},
         "rows": [], "by_cause_s": {}}

    cache_dir = run_dir / "spec_cache"
    dec_f = cache_dir / "serve_decisions.jsonl"
    cmds = load_jsonl(run_dir / "shelld_logs" / "commands.jsonl")
    entries = load_cache(cache_dir)

    stats_txt = (run_dir / "daemon_stats.txt")
    stats = None
    if stats_txt.exists():
        for ln in stats_txt.read_text(errors="replace").splitlines():
            i = ln.find("{")
            if i >= 0:
                try:
                    stats = json.loads(ln[i:])
                except json.JSONDecodeError:
                    pass
    r["wiring"] = {
        "spec_cache_dir": cache_dir.is_dir(),
        "cache_entries": len(entries),
        "decisions_log": dec_f.exists(),
        "daemon_commands": len(cmds),
        "daemon_cache_hits": (stats or {}).get("cache_hits"),
        # a daemon WITH spec cache logs no_entry for test-looking cmds and
        # serves; a daemon WITHOUT one logs nothing ever. Entries + agent
        # test commands + zero decisions => suspect unwired spec cache.
    }

    try:
        sys.path.insert(0, str(repo / "latency-opt" / "speculation"))
        from spec_families import family_key
    except ImportError:
        def family_key(_c):
            return None
    try:
        from spec_compound import split_compound  # type: ignore
    except ImportError:
        split_compound = None

    fam_keys = {e["key"] for e in entries if e["family"]}
    by_norm_cwd = {}
    by_cmd = {}
    for e in entries:
        if not e["family"]:
            by_norm_cwd.setdefault((norm(e["cmd"]), e["cwd"]), e)
            by_cmd.setdefault(norm(e["cmd"]), []).append(e)
    ent_toks = [(e, toks(e["cmd"])) for e in entries if not e["family"]]

    live = [c for c in cmds if not c.get("cached")
            and (c.get("wall_s") or 0) >= min_wall]
    live.sort(key=lambda c: -(c.get("wall_s") or 0))

    for c in live[:top]:
        cmd, cwd, wall = c["cmd"], c.get("cwd", ""), c.get("wall_s") or 0.0
        row = {"cmd": cmd[:160], "wall_s": round(wall, 2)}
        k = key_for(cwd, cmd)
        nc = norm(cmd)

        ent_k = next((e for e in entries if e["key"] == k), None)
        if ent_k is not None:
            req_ts = c.get("ts")
            if ent_k["at"] and req_ts and ent_k["at"] > req_ts:
                row["cause"] = "landed_late"
                row["note"] = (f"entry speculated {ent_k['at'] - req_ts:.0f}s "
                               "AFTER the agent ran it (trajectory-harvest "
                               "post-hoc, or respec re-run) — only a repeat "
                               "could have hit")
            else:
                row["cause"] = "present_but_invalid"
                row["note"] = ("entry predates the request but was not "
                               "served: failed generation/fingerprint "
                               "validation at request time")
        elif nc in by_cmd and all(e["cwd"] != cwd for e in by_cmd[nc]):
            other = by_cmd[nc][0]["cwd"]
            row["cause"] = "cwd_mismatch"
            row["note"] = f"same command cached under cwd={other!r} vs agent {cwd!r}"
        elif any(cw == cwd and n != nc and jaccard(toks(n), toks(nc)) >= 0.7
                 for (n, cw) in by_norm_cwd):
            best = max(((n, jaccard(toks(n), toks(nc)))
                        for (n, cw) in by_norm_cwd if cw == cwd),
                       key=lambda x: x[1])
            row["cause"] = "string_near"
            row["note"] = f"cached: {best[0][:120]!r} (jaccard {best[1]:.2f})"
        elif (lambda fk: fk and f"fam_{fk}" in fam_keys)(family_key(cmd)):
            row["cause"] = "family_present"
            row["note"] = "family entry exists; lookup skipped it or it was invalid"
        else:
            pieces = None
            if split_compound is not None:
                try:
                    pieces = split_compound(cmd)
                except Exception:
                    pieces = None
            lead_hits = 0
            if pieces and len(pieces) > 1:
                for p in pieces:
                    pc = (p[0] if isinstance(p, (tuple, list))
                          else p.get("cmd") if isinstance(p, dict) else str(p))
                    if key_for(cwd, pc) in {e["key"] for e in entries} \
                            or norm(pc) in by_cmd:
                        lead_hits += 1
                    else:
                        break
            if lead_hits:
                row["cause"] = "prefix_missed"
                row["note"] = (f"{lead_hits}/{len(pieces)} leading parts "
                               "cached, no prefix_serve fired")
            else:
                near = max(ent_toks, key=lambda et: jaccard(et[1], toks(cmd)),
                           default=(None, set()))
                j = jaccard(near[1], toks(cmd)) if near[0] else 0.0
                row["cause"] = "never_predicted"
                row["note"] = (f"nearest cached (jaccard {j:.2f}): "
                               f"{near[0]['cmd'][:110]!r}" if near[0]
                               else "cache empty")
        r["rows"].append(row)
        r["by_cause_s"][row["cause"]] = round(
            r["by_cause_s"].get(row["cause"], 0.0) + wall, 2)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--repo", type=Path,
                    default=Path("/projects/kzhou6/czhai/agent-profiling"))
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-wall", type=float, default=0.5)
    ap.add_argument("--jsonl", action="store_true")
    a = ap.parse_args()

    reports = [autopsy(d, a.repo, a.top, a.min_wall)
               for d in find_run_dirs(a.root)]
    if not reports:
        print(f"no shelld_logs/commands.jsonl under {a.root}",
              file=sys.stderr)
        sys.exit(2)
    if a.jsonl:
        for r in reports:
            print(json.dumps(r))
        return

    agg = {}
    for r in reports:
        w = r["wiring"]
        flag = (" <-- entries but NO decisions log: daemon spec-cache "
                "unwired or zero eligible lookups"
                if w["cache_entries"] and not w["decisions_log"] else "")
        print(f"\n### {r['task']}   cache_entries={w['cache_entries']} "
              f"decisions_log={w['decisions_log']} "
              f"hits={w['daemon_cache_hits']}{flag}")
        for row in r["rows"]:
            print(f"  {row['wall_s']:7.2f}s  {row['cause']:15} {row['cmd']}")
            print(f"           {' ':9}{row['note']}")
        for k, v in r["by_cause_s"].items():
            agg[k] = round(agg.get(k, 0.0) + v, 2)
    print("\n" + "=" * 90)
    print("SECONDS LEFT ON THE TABLE, by cause (top commands only):")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {v:8.2f}s  {k}")


if __name__ == "__main__":
    main()
