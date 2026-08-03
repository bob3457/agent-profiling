#!/usr/bin/env python3
"""decompose_serves.py — ground-truth decomposition of speculation savings.

`summary.json` cache_hits only counts fully-cached replies and the daemon's
commands.jsonl reports wall_s=0.0 for every cached reply (including joins,
where the agent really waited). serve_decisions.jsonl is the ground truth;
this tool turns it into the per-category breakdown:

  exact_serve     entry already on disk        saved = entry_dur_s
  joined_inflight waited for in-flight writer  saved = entry_dur_s - waited
                                               (net can be NEGATIVE -> flagged)
  prefix_full     whole compound from parts    saved = record's saved_s
  prefix_partial  leading parts only           saved = record's saved_s
  misses          no_entry / stale_generation / stale_fingerprint /
                  generation_file_missing / inflight_timeout (wasted wait s)

Dedup rule: a successful join logs TWO lines (joined_inflight then served,
same cmd, back-to-back). The pair is ONE serve event, category "joined".

Cross-checks (--check adds exit-code enforcement):
  daemon_stats cache_hits == exact + joined + prefix_full
  commands.jsonl cached=True count == same
  net-negative joins, timeout waste

Usage:
  python3 decompose_serves.py RUN_DIR             # one .../bench/task dir
  python3 decompose_serves.py RESULTS_ROOT        # walks arm_C.* layout
  python3 decompose_serves.py DIR --jsonl         # machine output
  python3 decompose_serves.py DIR --check         # exit 1 on inconsistency
"""
import argparse
import json
import re
import sys
from pathlib import Path

JOIN_RE = re.compile(r"joined_inflight\(([\d.]+)s?\)")
TMO_RE = re.compile(r"inflight_timeout\(([\d.]+)s?\)")
MISS_KINDS = ("no_entry", "stale_generation", "stale_fingerprint",
              "generation_file_missing")


def find_run_dirs(root: Path):
    """Yield dirs that contain spec_cache/serve_decisions.jsonl."""
    if (root / "spec_cache" / "serve_decisions.jsonl").exists():
        yield root
        return
    for p in sorted(root.glob("**/spec_cache/serve_decisions.jsonl")):
        yield p.parent.parent


def load_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def parse_daemon_stats(run_dir: Path):
    """daemon_stats.txt holds the shutdown-time stats JSON (possibly with
    shell noise around it); grab the last parseable JSON object."""
    f = run_dir / "daemon_stats.txt"
    if not f.exists():
        return None
    stats = None
    for ln in f.read_text(errors="replace").splitlines():
        ln = ln.strip()
        i = ln.find("{")
        if i >= 0:
            try:
                stats = json.loads(ln[i:])
            except json.JSONDecodeError:
                pass
    return stats


def analyze(run_dir: Path):
    dec = load_jsonl(run_dir / "spec_cache" / "serve_decisions.jsonl")
    cmds = load_jsonl(run_dir / "shelld_logs" / "commands.jsonl")
    stats = parse_daemon_stats(run_dir)

    r = {"dir": str(run_dir),
         "task": f"{run_dir.parent.name}/{run_dir.name}",
         "events": [],
         "exact": 0, "exact_saved": 0.0,
         "joined": 0, "joined_saved": 0.0, "joined_waited": 0.0,
         "joined_negative": 0,
         "prefix_full": 0, "prefix_partial": 0, "prefix_saved": 0.0,
         "prefix_parts": 0, "prefix_parts_total": 0,
         "timeouts": 0, "timeout_wasted": 0.0,
         "misses": {k: 0 for k in MISS_KINDS},
         "warnings": []}

    pending_join = None  # (waited_s, rec) awaiting its paired 'served' line
    for d in dec:
        decision = d.get("decision", "")
        m = JOIN_RE.match(decision)
        if m:
            if pending_join is not None:
                r["warnings"].append("joined_inflight without paired served")
            pending_join = (float(m.group(1)), d)
            continue
        m = TMO_RE.match(decision)
        if m:
            r["timeouts"] += 1
            r["timeout_wasted"] += float(m.group(1))
            continue
        if decision == "served":
            dur = d.get("entry_dur_s") or 0.0
            if pending_join is not None and \
                    d.get("cmd") == pending_join[1].get("cmd"):
                waited = pending_join[0]
                net = dur - waited
                r["joined"] += 1
                r["joined_waited"] += waited
                r["joined_saved"] += net
                if net < 0:
                    r["joined_negative"] += 1
                r["events"].append({"kind": "joined", "cmd": d.get("cmd"),
                                    "entry_dur_s": dur, "waited_s": waited,
                                    "net_saved_s": round(net, 3)})
                pending_join = None
            else:
                r["exact"] += 1
                r["exact_saved"] += dur
                r["events"].append({"kind": "exact", "cmd": d.get("cmd"),
                                    "entry_dur_s": dur})
            continue
        if pending_join is not None:
            # join resolved but the entry failed validation -> the wait was
            # pure waste; the following miss line tells us why
            r["timeout_wasted"] += pending_join[0]
            r["warnings"].append(
                f"join wait wasted on invalid entry ({decision})")
            pending_join = None
        if decision == "prefix_serve":
            full = bool(d.get("full"))
            r["prefix_full" if full else "prefix_partial"] += 1
            r["prefix_saved"] += d.get("saved_s") or 0.0
            r["prefix_parts"] += d.get("parts_served") or 0
            r["prefix_parts_total"] += d.get("parts_total") or 0
            r["events"].append({"kind": "prefix_full" if full
                                else "prefix_partial", "cmd": d.get("cmd"),
                                "saved_s": d.get("saved_s"),
                                "parts": f"{d.get('parts_served')}/"
                                         f"{d.get('parts_total')}"})
            continue
        base = decision.split("(")[0]
        if base in MISS_KINDS:
            r["misses"][base] += 1
    if pending_join is not None:
        r["warnings"].append("trailing joined_inflight without served")

    # ---- totals & tool-time context -------------------------------------
    r["serves"] = r["exact"] + r["joined"] + r["prefix_full"] \
        + r["prefix_partial"]
    r["saved_total"] = r["exact_saved"] + r["joined_saved"] \
        + r["prefix_saved"]
    live = [c for c in cmds if not c.get("cached")]
    r["live_wall_s"] = sum(c.get("wall_s") or 0.0 for c in live)
    r["n_commands"] = len(cmds)
    r["n_cached_replies"] = sum(1 for c in cmds if c.get("cached"))
    denom = r["saved_total"] + r["live_wall_s"]
    r["savings_frac"] = (r["saved_total"] / denom) if denom > 0 else 0.0

    # ---- consistency vs daemon_stats & commands.jsonl --------------------
    full_hits = r["exact"] + r["joined"] + r["prefix_full"]
    if stats is not None:
        r["stats_cache_hits"] = stats.get("cache_hits")
        if stats.get("cache_hits") is not None and \
                stats["cache_hits"] != full_hits:
            r["warnings"].append(
                f"daemon_stats cache_hits={stats['cache_hits']} != "
                f"decision-log full serves={full_hits}")
    if cmds and r["n_cached_replies"] != full_hits:
        r["warnings"].append(
            f"commands.jsonl cached=True={r['n_cached_replies']} != "
            f"decision-log full serves={full_hits}")
    if r["joined_negative"]:
        r["warnings"].append(
            f"{r['joined_negative']} net-NEGATIVE join(s): waiting cost more "
            "than the command; consider lowering SPEC_JOIN_MAX_WAIT")
    return r


def fmt_row(r):
    miss = sum(r["misses"].values())
    return (f"{r['task'][:44]:44} "
            f"{r['exact']:>3}ex {r['joined']:>3}jn {r['prefix_full']:>3}pf "
            f"{r['prefix_partial']:>3}pp {miss:>3}ms {r['timeouts']:>2}to  "
            f"saved {r['saved_total']:7.2f}s  waited {r['joined_waited']:5.2f}s  "
            f"live {r['live_wall_s']:8.2f}s  {r['savings_frac']*100:5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--jsonl", action="store_true")
    ap.add_argument("--events", action="store_true",
                    help="print every serve event")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any consistency warning fires")
    a = ap.parse_args()

    rows = [analyze(d) for d in find_run_dirs(a.root)]
    if not rows:
        print(f"no spec_cache/serve_decisions.jsonl under {a.root}",
              file=sys.stderr)
        sys.exit(2)

    if a.jsonl:
        for r in rows:
            print(json.dumps(r))
    else:
        print(f"{'task':44} {'serves (ex/join/pfull/ppart/miss/tmo)':>38}")
        for r in rows:
            print(fmt_row(r))
            if a.events:
                for e in r["events"]:
                    print(f"    {e['kind']:14} "
                          f"{json.dumps({k: v for k, v in e.items() if k not in ('kind',)})[:140]}")
            for w in r["warnings"]:
                print(f"    WARN {w}")
        # aggregate
        agg = {k: sum(r[k] for r in rows) for k in
               ("exact", "joined", "prefix_full", "prefix_partial",
                "timeouts", "exact_saved", "joined_saved", "joined_waited",
                "prefix_saved", "saved_total", "live_wall_s",
                "timeout_wasted", "joined_negative")}
        miss_tot = {k: sum(r["misses"][k] for r in rows) for k in MISS_KINDS}
        denom = agg["saved_total"] + agg["live_wall_s"]
        print("-" * 118)
        print(f"AGGREGATE over {len(rows)} run(s):")
        print(f"  serves     exact={agg['exact']} joined={agg['joined']} "
              f"prefix_full={agg['prefix_full']} "
              f"prefix_partial={agg['prefix_partial']}")
        print(f"  saved_s    exact={agg['exact_saved']:.2f} "
              f"joined_net={agg['joined_saved']:.2f} "
              f"(waited {agg['joined_waited']:.2f}, "
              f"{agg['joined_negative']} negative) "
              f"prefix={agg['prefix_saved']:.2f} "
              f"TOTAL={agg['saved_total']:.2f}")
        print(f"  waste_s    inflight_timeouts={agg['timeouts']} "
              f"wasted={agg['timeout_wasted']:.2f}")
        print(f"  misses     " + " ".join(f"{k}={v}"
                                          for k, v in miss_tot.items()))
        print(f"  tool time  live={agg['live_wall_s']:.2f}s -> savings "
              f"{(agg['saved_total']/denom*100 if denom else 0):.1f}% of "
              f"(saved+live)")

    if a.check and any(r["warnings"] for r in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
