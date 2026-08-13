#!/usr/bin/env python3
"""bakeoff_predictors.py — rank candidate predictor models OFFLINE by
replaying captured prompts (the exact prompts codex was paid to answer)
through an OpenAI-compatible endpoint, parsing with the SAME predict_parse
path and scoring with the SAME family/any-hit rubric as the live system.

Zero API tokens. One model per invocation (llama-server serves one model);
results append to a JSONL so the comparison table accumulates across runs.

Workflow (GH200):
  llama-server -m qwen2.5-coder-7b-q5_k_m.gguf -ngl 999 -c 8192 --port 8080 &
  python3 scripts/bakeoff_predictors.py \
      --captures /scratch/czhai/latency-eval/pred_capture \
      --testset  /scratch/czhai/latency-eval/testset_v2.jsonl \
      --label qwen2.5-coder-7b
  # restart llama-server with the next model, re-run with a new --label ...
  python3 scripts/bakeoff_predictors.py --compare        # cumulative table

The codex baseline row is computed automatically from each capture's own
recorded prediction — same cases, directly comparable.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "latency-opt" / "speculation"))
import predict_parse                     # noqa: E402
from spec_families import parse_command  # noqa: E402
from predictor_eval import score_pair    # noqa: E402

DEFAULT_OUT = "/scratch/czhai/latency-eval/bakeoff_results.jsonl"


def load_manifest_cases(manifest):
    """[v2] Build fresh prompts from workspaces via the CURRENT llm_predictor
    (relevance-ranked listing) — evaluates on the clean option-B instances
    with no captures needed. No codex baseline exists for these rows."""
    from llm_predictor import PROMPT_TEMPLATE, _test_file_listing
    caps = []
    for ln in Path(manifest).read_text().splitlines():
        if not ln.strip():
            continue
        iid, ws, pf = ln.split("\t")
        root = Path(manifest).resolve().parent.parent
        ws, pf = root / ws, root / pf
        txt = pf.read_text()
        problem = txt.split("Problem statement:\n", 1)[-1]
        problem = problem.split("\nInstructions:", 1)[0].strip()
        prompt = PROMPT_TEMPLATE.format(
            problem=problem[:6000],
            listing=_test_file_listing(ws, problem)[:6000])
        caps.append({"task": iid, "prompt": prompt, "mode": "tests",
                     "codex_predicted": [], "codex_latency": None})
    return caps


def norm_ws(c):
    return re.sub(r"\s+", " ", (c or "").strip())


def load_observed(testset):
    obs = defaultdict(set)
    for ln in open(testset):
        try:
            c = json.loads(ln)
        except json.JSONDecodeError:
            continue
        obs[c.get("task")].update(c.get("observed") or [])
    return obs


def load_captures(cap_dir, limit=None):
    caps = []
    for f in sorted(Path(cap_dir).glob("pred_*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        task = r.get("task")
        if not task:
            m = re.match(r"pred_(.+)_\d+\.json", f.name)
            task = m.group(1) if m else None
        if not task or not r.get("prompt"):
            continue
        # skip captures produced by a local backend: baseline must be codex
        if r.get("backend") == "openai":
            continue
        caps.append({"task": task, "prompt": r["prompt"],
                     "mode": r.get("mode") or "tests",
                     "codex_predicted": r.get("predicted") or [],
                     "codex_latency": r.get("latency_s")})
    if limit:
        caps = caps[:limit]
    return caps


def score(preds, observed):
    """(family_best, any_exact_hit) with the live rubric."""
    pp = [p for p in (parse_command(c) for c in preds) if p and p.get("targets")]
    oo = [p for p in (parse_command(c) for c in observed) if p and p.get("targets")]
    best = 0.0
    for o in oo:
        for p in pp:
            best = max(best, score_pair(p, o))
    obs_n = {norm_ws(c) for c in observed}
    any_hit = any(norm_ws(c) in obs_n for c in preds)
    return best, any_hit


def call_model(endpoint, model, prompt, timeout, max_tokens):
    body = json.dumps({
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [
            {"role": "system",
             "content": "You predict shell commands. Respond with ONLY a "
                        "JSON array of strings. No prose, no markdown, "
                        "no thinking out loud."},
            {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    with opener.open(req, timeout=timeout) as r:
        resp = json.load(r)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get(
        "content") or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text.strip(), round(time.time() - t0, 2)


def parse_preds(text, mode):
    if mode == "generic":
        validator = predict_parse.looks_like_command
    else:
        def validator(c):
            p = parse_command(c)
            return bool(p and p["targets"])
    limit = 5 if mode == "generic" else 3
    return predict_parse.extract_commands(text, mode=mode, limit=limit,
                                          validator=validator) or []


def run_model(args, caps, observed):
    rows = []
    for i, cap in enumerate(caps, 1):
        obs = observed.get(cap["task"])
        if not obs:
            continue
        try:
            text, lat = call_model(args.endpoint, args.model_name,
                                   cap["prompt"], args.timeout,
                                   args.max_tokens)
        except Exception as e:
            print(f"  [{i}/{len(caps)}] {cap['task']}: ERROR {e}")
            continue
        preds = parse_preds(text, cap["mode"])
        fam, hit = score(preds, obs)
        rows.append({"kind": "bakeoff", "label": args.label,
                     "task": cap["task"], "family": fam, "any_hit": hit,
                     "latency_s": lat, "n_preds": len(preds),
                     "preds": preds[:5], "ts": time.time()})
        print(f"  [{i}/{len(caps)}] {cap['task']:40s} fam={fam:.1f} "
              f"hit={int(hit)} {lat:.1f}s")
    with open(args.out, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return rows


def baseline_rows(caps, observed):
    rows = []
    for cap in caps:
        obs = observed.get(cap["task"])
        if not obs:
            continue
        fam, hit = score(cap["codex_predicted"], obs)
        rows.append({"label": "codex-low(baseline)", "task": cap["task"],
                     "family": fam, "any_hit": hit,
                     "latency_s": cap.get("codex_latency"),
                     "n_preds": len(cap["codex_predicted"])})
    return rows


def table(all_rows):
    by = defaultdict(list)
    for r in all_rows:
        by[r["label"]].append(r)
    print(f"\n{'model':26s} {'cases':>5s} {'family':>7s} {'any-hit':>8s} "
          f"{'avg-lat':>8s} {'no-pred':>8s}")
    for label in sorted(by, key=lambda l: -sum(r['family'] for r in by[l]) / len(by[l])):
        rs = by[label]
        lat = [r["latency_s"] for r in rs if r.get("latency_s")]
        print(f"{label:26s} {len(rs):5d} "
              f"{sum(r['family'] for r in rs)/len(rs):7.3f} "
              f"{sum(r['any_hit'] for r in rs)/len(rs):8.3f} "
              f"{(sum(lat)/len(lat)) if lat else 0:7.1f}s "
              f"{sum(1 for r in rs if not r['n_preds'])/len(rs):8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", default="/scratch/czhai/latency-eval/pred_capture")
    ap.add_argument("--testset", default="/scratch/czhai/latency-eval/testset_v2.jsonl")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8080/v1/chat/completions")
    ap.add_argument("--model-name", default="local")
    ap.add_argument("--label", default=None, help="row name; omit with --compare")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--compare", action="store_true",
                    help="print cumulative table only (no model calls)")
    ap.add_argument("--manifest", default=None,
                    help="build prompts from a swebench manifest (iid\\tws\\tprompt) "
                         "instead of replaying captures")
    args = ap.parse_args()

    if args.manifest:
        caps = load_manifest_cases(args.manifest)
        if args.limit:
            caps = caps[:args.limit]
    else:
        caps = load_captures(args.captures, args.limit)
    observed = load_observed(args.testset)
    usable = [c for c in caps if c["task"] in observed]
    print(f"captures={len(caps)}  with-observed={len(usable)}")

    rows = [] if args.manifest else baseline_rows(caps, observed)
    if not args.compare:
        if not args.label:
            sys.exit("--label required when running a model")
        run_model(args, caps, observed)
    if Path(args.out).exists():
        for ln in open(args.out):
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    table(rows)


if __name__ == "__main__":
    main()
