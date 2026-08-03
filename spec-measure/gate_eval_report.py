#!/usr/bin/env python3
"""gate_eval_report.py — the four measurements, per benchmark.

  1. GATE     verdict (gate.json) vs realized ground truth. A task was worth
              speculating on iff speculation actually produced value there
              (decisions-log saved_s > 0), which is only observable when
              speculation ran regardless of the verdict -> run the sweep with
              SPEC_GATE_SHADOW=1. Confusion matrix + precision/recall/acc,
              also at a >=5s materiality threshold.
  2. VOLUME + predictions the speculator executed+cached (spec_early.log,
     ACCURACY spec.log '[spec] cached' lines + respec.log re-runs) vs how
              many were accepted (serve events from serve_decisions.jsonl):
              realized precision. Plus the ledger's command-match accuracy
              per predictor (run `ledger.py update` first to resolve).
  3. SAVED    seconds saved on accepted predictions, by category (exact /
              joined-net / prefix), from decompose_serves.
  4. COST     speculation-side CPU (cpu_*.json rusage dumps) vs the agent's
              own CPU (time.txt User+System) -> overhead %; extra tokens
              (gate.json tokens + ledger prediction tokens).

Usage:
  python3 gate_eval_report.py RESULTS_ROOT [--ledger-dir DIR] [--jsonl]
  RESULTS_ROOT layout: <root>/<bench>/<task>/{gate.json,spec_cache,...}
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "spec-analysis"))
from decompose_serves import analyze as decompose  # noqa: E402

CACHED_RE = re.compile(r"^\[spec\] cached\*? ")
RESPEC_RUN_RE = re.compile(r"cached exit=-?\d+ [\d.]+s ")
TIME_RE = {"user": re.compile(r"User time \(seconds\): ([\d.]+)"),
           "sys": re.compile(r"System time \(seconds\): ([\d.]+)")}


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def task_row(tdir: Path):
    r = {"bench": tdir.parent.name, "task": tdir.name}

    gate = read_json(tdir / "gate.json")
    if gate:
        r["gate_go"] = gate.get("speculate")          # None = died pending
        r["gate_latency_s"] = gate.get("gate_latency_s")
        r["gate_shadow"] = gate.get("shadow")
        tok = gate.get("tokens") or {}
        r["gate_tokens"] = tok.get("total", 0)
        r["gate_tokens_estimated"] = tok.get("estimated")
    else:
        r["gate_go"] = None

    # realized speculation value (only meaningful if speculation ran)
    if (tdir / "spec_cache" / "serve_decisions.jsonl").exists():
        d = decompose(tdir)
        r["serves"] = d["serves"]
        r["saved_s"] = round(d["saved_total"], 2)
        r["joined_waited_s"] = round(d["joined_waited"], 2)
        r["timeout_wasted_s"] = round(d["timeout_wasted"], 2)
        r["by_cat"] = {"exact": d["exact"], "joined": d["joined"],
                       "prefix_full": d["prefix_full"],
                       "prefix_partial": d["prefix_partial"]}
        r["saved_by_cat"] = {"exact": round(d["exact_saved"], 2),
                             "joined_net": round(d["joined_saved"], 2),
                             "prefix": round(d["prefix_saved"], 2)}
        r["misses"] = sum(d["misses"].values())
        r["live_wall_s"] = round(d["live_wall_s"], 2)
    else:
        r["serves"], r["saved_s"] = 0, 0.0

    # predictions executed + cached by the speculation side
    n_pred = 0
    for lg in ("spec_early.log", "spec.log"):
        f = tdir / lg
        if f.exists():
            n_pred += sum(1 for ln in f.read_text(errors="replace")
                          .splitlines() if CACHED_RE.match(ln))
    f = tdir / "respec.log"
    if f.exists():
        n_pred += sum(1 for ln in f.read_text(errors="replace").splitlines()
                      if RESPEC_RUN_RE.search(ln))
    r["predictions_cached"] = n_pred

    # speculation-side CPU vs agent CPU
    spec_cpu = 0.0
    for f in tdir.glob("cpu_*.json"):
        j = read_json(f)
        if j:
            spec_cpu += j.get("cpu_total_s") or 0.0
    r["spec_cpu_s"] = round(spec_cpu, 2)
    agent_cpu = 0.0
    t = tdir / "time.txt"
    if t.exists():
        txt = t.read_text(errors="replace")
        for k, rx in TIME_RE.items():
            m = rx.search(txt)
            if m:
                agent_cpu += float(m.group(1))
    r["agent_cpu_s"] = round(agent_cpu, 2)
    return r


def confusion(rows, thresh=0.0):
    tp = sum(1 for r in rows if r["gate_go"] and r["saved_s"] > thresh)
    fp = sum(1 for r in rows if r["gate_go"] and r["saved_s"] <= thresh)
    fn = sum(1 for r in rows if r["gate_go"] is False
             and r["saved_s"] > thresh)
    tn = sum(1 for r in rows if r["gate_go"] is False
             and r["saved_s"] <= thresh)
    n = tp + fp + fn + tn
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "accuracy": round((tp + tn) / n, 3) if n else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--ledger-dir", type=Path, default=None)
    ap.add_argument("--thresh", type=float, default=5.0,
                    help="materiality threshold for the second confusion row")
    ap.add_argument("--jsonl", action="store_true")
    a = ap.parse_args()

    rows = [task_row(t) for t in sorted(a.root.glob("*/*/")) if t.is_dir()]
    rows = [r for r in rows if (a.root / r["bench"] / r["task"]).is_dir()]
    if not rows:
        print(f"no <bench>/<task> dirs under {a.root}", file=sys.stderr)
        sys.exit(2)
    if a.jsonl:
        for r in rows:
            print(json.dumps(r))
        return

    benches = sorted({r["bench"] for r in rows})
    W = 96
    for b in benches:
        br = [r for r in rows if r["bench"] == b]
        print("=" * W)
        print(f"BENCH {b}  ({len(br)} tasks)")

        # ---- 1. gate ------------------------------------------------------
        undecided = [r for r in br if r["gate_go"] is None]
        scored = [r for r in br if r["gate_go"] is not None]
        shadowed = sum(1 for r in scored if r.get("gate_shadow"))
        print(f"\n[1] GATE  ({len(scored)} decided, {len(undecided)} "
              f"pending/absent, {shadowed} shadow-mode)")
        if shadowed < len(scored):
            print("    WARN: non-shadow tasks — NOGO ground truth "
                  "unobservable there (FN/TN unreliable)")
        for th, lbl in ((0.0, "worth = saved>0s"),
                        (a.thresh, f"worth = saved>{a.thresh:g}s")):
            c = confusion(scored, th)
            print(f"    {lbl:22} TP={c['tp']:>2} FP={c['fp']:>2} "
                  f"FN={c['fn']:>2} TN={c['tn']:>2}  "
                  f"prec={c['precision']} rec={c['recall']} "
                  f"acc={c['accuracy']}")
        lat = [r["gate_latency_s"] for r in scored
               if r.get("gate_latency_s") is not None]
        if lat:
            print(f"    gate latency mean {sum(lat)/len(lat):.1f}s "
                  f"max {max(lat):.1f}s")

        # ---- 2. predictions -----------------------------------------------
        n_pred = sum(r["predictions_cached"] for r in br)
        n_serv = sum(r["serves"] for r in br)
        print(f"\n[2] PREDICTIONS  cached={n_pred}  accepted(serves)={n_serv}"
              f"  realized precision="
              f"{(n_serv / n_pred):.1%}" if n_pred else
              f"\n[2] PREDICTIONS  cached=0  accepted={n_serv}")

        # ---- 3. saved time --------------------------------------------------
        tot = sum(r["saved_s"] for r in br)
        cats = {}
        for r in br:
            for k, v in (r.get("saved_by_cat") or {}).items():
                cats[k] = cats.get(k, 0.0) + v
        per = (tot / n_serv) if n_serv else 0.0
        live = sum(r.get("live_wall_s", 0.0) for r in br)
        frac = tot / (tot + live) if (tot + live) > 0 else 0.0
        print(f"\n[3] SAVED  total={tot:.1f}s  per accepted={per:.1f}s  "
              + " ".join(f"{k}={v:.1f}s" for k, v in sorted(cats.items())))
        print(f"    tool-side: saved is {frac:.1%} of (saved + live "
              f"{live:.1f}s); join waits {sum(r.get('joined_waited_s', 0) for r in br):.1f}s, "
              f"timeout waste {sum(r.get('timeout_wasted_s', 0) for r in br):.1f}s")

        # ---- 4. cost ---------------------------------------------------------
        scpu = sum(r["spec_cpu_s"] for r in br)
        acpu = sum(r["agent_cpu_s"] for r in br)
        gtok = sum(r.get("gate_tokens") or 0 for r in br)
        est = any(r.get("gate_tokens_estimated") for r in br)
        print(f"\n[4] COST  spec CPU {scpu:.1f}s vs agent CPU {acpu:.1f}s"
              + (f" (+{scpu/acpu:.1%})" if acpu else "")
              + f"  |  gate tokens {gtok}"
              + (" [char-estimated]" if est else ""))
        missing_cpu = [r["task"] for r in br
                       if r["gate_go"] is not None and r["spec_cpu_s"] == 0]
        if missing_cpu:
            print(f"    WARN no cpu_*.json for: {missing_cpu[:4]}"
                  f"{'...' if len(missing_cpu) > 4 else ''} "
                  "(run with patched components)")
        print()

    # ---- predictor command-match accuracy (ledger) ---------------------------
    if a.ledger_dir:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                               / "latency-opt/speculation"))
        from ledger import stats  # noqa: E402
        s = stats(str(a.ledger_dir))
        print("=" * W)
        print("LEDGER predictor command-match accuracy "
              "(run `ledger.py update` first; predictor tokens = LLM "
              "prediction cost)")
        if not s:
            print("  (empty / unresolved)")
        for (b, p), v in sorted(s.items()):
            print(f"  {b:15s} {p:10s} n={v['n']:>3} mean={v['mean']:.3f} "
                  f"exact={v['exact']} tokens={v['tokens_total']}")


if __name__ == "__main__":
    main()
