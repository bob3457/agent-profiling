#!/usr/bin/env python3
"""replay_testset.py — score saved predictions offline; re-parse raw
captures with the CURRENT parser. Zero tokens.

Two prediction variants per case:
  as-parsed   the `predicted` list stored at run time (whatever parser
              version produced it — this is the baseline)
  reparsed    raw capture re-run through the current predict_parse
              pipeline (answer_text if saved, else schema extraction
              from the raw --json stdout). Only for cases with raw.

Two scores per variant against the case's observed commands:
  family      best predictor_eval.score_pair over parseable test-family
              (pytest/django) pairs — the SWE-bench metric
  exact       fraction of predictions that string-match an observed
              command after whitespace normalization — the metric that
              actually gates exact-key cache serves

Usage (from repo root so speculation/ imports resolve):
  python3 replay_testset.py --testset testset.jsonl \
      [--speculation latency-opt/speculation] [--bench swebench] \
      [--diff]        # print cases where reparsed != as-parsed
      [--out report.json]
"""
import argparse
import json
import re
import sys
from pathlib import Path


def norm(c: str) -> str:
    return re.sub(r"\s+", " ", (c or "").strip())


def family_score(preds, observed, parse_command, score_pair):
    pp = [p for p in (parse_command(c) for c in preds) if p and p["targets"]]
    oo = [p for p in (parse_command(c) for c in observed)
          if p and p["targets"]]
    best, pair = 0.0, None
    for o in oo:
        for p in pp:
            s = score_pair(p, o)
            if s > best:
                best, pair = s, (p["targets"], o["targets"])
    return best, pair, len(pp), len(oo)


def exact_score(preds, observed):
    if not preds:
        return 0.0, 0
    obs = {norm(c) for c in observed}
    hits = sum(1 for c in preds if norm(c) in obs)
    return round(hits / len(preds), 3), hits


def reparse(raw, mode, predict_parse, parse_command):
    text = raw.get("answer_text") or ""
    if not text and raw.get("stdout"):
        text = predict_parse.extract_agent_text(raw["stdout"])
    if not text:
        return None
    mode = mode or raw.get("mode") or "generic"
    if mode == "generic":
        validator = predict_parse.looks_like_command
    else:
        def validator(c):
            p = parse_command(c)
            return bool(p and p["targets"])
    limit = 5 if mode == "generic" else 3
    return predict_parse.extract_commands(text, mode=mode, limit=limit,
                                          validator=validator)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--testset", required=True)
    ap.add_argument("--speculation", default="latency-opt/speculation")
    ap.add_argument("--bench", default=None)
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.speculation).resolve()))
    import predict_parse
    from spec_families import parse_command
    from predictor_eval import score_pair

    rows = []
    for ln in open(args.testset):
        try:
            case = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if args.bench and case.get("bench") != args.bench:
            continue
        observed = case.get("observed") or []
        base = case.get("predicted") or []
        row = {"bench": case.get("bench"), "task": case.get("task"),
               "n_observed": len(observed)}

        fs, pair, npp, noo = family_score(base, observed,
                                          parse_command, score_pair)
        es, hits = exact_score(base, observed)
        row["as_parsed"] = {"n": len(base), "family": round(fs, 3),
                            "exact": es, "exact_hits": hits, "match": pair}

        rep = None
        if case.get("raw"):
            rep = reparse(case["raw"], case["raw"].get("mode"),
                          predict_parse, parse_command)
        if rep is not None:
            fs2, pair2, _, _ = family_score(rep, observed,
                                            parse_command, score_pair)
            es2, hits2 = exact_score(rep, observed)
            row["reparsed"] = {"n": len(rep), "family": round(fs2, 3),
                               "exact": es2, "exact_hits": hits2,
                               "match": pair2}
            if args.diff and rep != base:
                print(f"--- {row['task']}")
                print(f"    as-parsed: {base}")
                print(f"    reparsed : {rep}")
        rows.append(row)

    def summarize(key):
        rs = [r[key] for r in rows if key in r]
        if not rs:
            return None
        n = len(rs)
        return {"cases": n,
                "family_mean": round(sum(r["family"] for r in rs) / n, 3),
                "family_exact1": sum(r["family"] == 1.0 for r in rs),
                "exact_mean": round(sum(r["exact"] for r in rs) / n, 3),
                "n_pred_mean": round(sum(r["n"] for r in rs) / n, 2)}

    print(f"\n{'task':38s} {'src':9s} {'n':>2} {'family':>6} {'exact':>5}")
    for r in rows:
        for key in ("as_parsed", "reparsed"):
            if key in r:
                a = r[key]
                print(f"{r['task']:38s} {key:9s} {a['n']:>2} "
                      f"{a['family']:>6.3f} {a['exact']:>5.2f}")
    for key in ("as_parsed", "reparsed"):
        s = summarize(key)
        if s:
            print(f"\n[{key}] cases={s['cases']} family_mean="
                  f"{s['family_mean']} exact1={s['family_exact1']} "
                  f"exact_str_mean={s['exact_mean']} "
                  f"preds/case={s['n_pred_mean']}")
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"rows": rows, "summary": {k: summarize(k)
                                       for k in ("as_parsed", "reparsed")}},
            indent=2))
        print(f"report -> {args.out}")


if __name__ == "__main__":
    main()
