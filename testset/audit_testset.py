#!/usr/bin/env python3
"""audit_testset.py (v1.1) — mine a testset.jsonl for CONCRETE parser
improvements.

v1.1 fixes two v1.0 artifacts found on the first real run (192 swebench
cases, 2026-08):
  * exact scoring is now ANY-HIT per case: the worker pre-runs every
    candidate, so one key match is a serve. v1.0's fraction-of-preds metric
    dumped never-run extra candidates into a bogus "other" near-miss pile.
  * normalizer_gaps now only counts segments that actually INVOKE a test
    runner (pytest / python* -m pytest / runtests.py / tox / make test /
    go|cargo|npm test), classified by WHY they're unparseable
    (interpreter_variant, env_prefix, pytest_-k_filter, ...). v1.0 matched
    "test" anywhere, so `sed -n ... tests/test_x.py` flooded the table.
  * model_empty is split per arm — predictor-off arms (heuristic/baseline)
    otherwise drown the predictor sample.

Buckets -> edit sites:
  validator_dropped     predict_parse.looks_like_command recall
  model_empty           predictor off or model gave nothing (see arm split)
  pred_unparseable      extract_commands phrasing vs spec_families
  exact_near_miss       family 1.0, NO pred exact-hit; sub-typed by the
                        minimal normalization that would flip it
  granularity           0.8: right file/label, wrong node -> ladder pre-runs
  normalizer_gaps       real runner invocations parse_command refuses
  compound_opportunity  compound line with parseable leading segment
  family_disjoint /     model quality, not parser
  wrong_targets

Usage (repo root):
  python3 testset/audit_testset.py --testset .../testset.jsonl \
      [--speculation latency-opt/speculation] [--bench swebench] \
      [--examples 5] [--out audit.json]
"""
import argparse
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path

PY_RE = re.compile(r"python[0-9.]*$")
SEG_SPLIT = re.compile(r"&&|\|\||;|\|")


def norm_ws(c):
    return re.sub(r"\s+", " ", (c or "").strip())


# --- exact-near-miss sub-typing ---------------------------------------------
def _strip_runner(cmd):
    try:
        p = shlex.split(cmd)
    except ValueError:
        return cmd
    i = 0
    while i < len(p) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p[i]):
        i += 1
    p = p[i:]
    if len(p) >= 3 and PY_RE.fullmatch(os.path.basename(p[0])) \
            and p[1] == "-m" and p[2] == "pytest":
        p = ["pytest"] + p[3:]
    return " ".join(p)


def _strip_paths(cmd):
    return re.sub(r"(^|\s)\./", r"\1", cmd)


_FLAG_RE = re.compile(r"(^|\s)-{1,2}[A-Za-z][\w=:-]*")


def _strip_flags(cmd):
    return norm_ws(_FLAG_RE.sub(" ", cmd))


def _strip_quotes(cmd):
    return cmd.replace('"', "").replace("'", "")


def near_miss_type(pred, obs_set):
    steps = [("whitespace", norm_ws),
             ("runner_alias", lambda c: norm_ws(_strip_runner(c))),
             ("path_form", lambda c: norm_ws(_strip_paths(_strip_runner(c)))),
             ("quoting", lambda c: norm_ws(_strip_quotes(_strip_paths(_strip_runner(c))))),
             ("flag_only", lambda c: _strip_flags(_strip_quotes(_strip_paths(_strip_runner(c)))))]
    for name, fn in steps:
        try:
            if fn(pred) in {fn(o) for o in obs_set}:
                return name
        except Exception:
            continue
    return "other"


# --- runner-invocation detection (gap table) ----------------------------------
def test_invocation_sig(segment):
    """(is_test_invocation, signature) for one simple-command segment."""
    try:
        parts = shlex.split(segment.strip())
    except ValueError:
        return False, None
    env_prefix = False
    i = 0
    while i < len(parts):
        if parts[i] == "env":
            env_prefix = True
            i += 1
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[i]):
            i += 1
        else:
            break
    parts = parts[i:]
    if not parts:
        return False, None
    head = parts[0]
    base = os.path.basename(head)
    variant = ("/" in head) or head.startswith("$") \
        or (PY_RE.fullmatch(base) and base not in ("python", "python3"))

    is_test = False
    kind = None
    if base == "pytest":
        is_test, kind = True, "pytest"
    elif PY_RE.fullmatch(base):
        if len(parts) >= 3 and parts[1] == "-m" and parts[2] == "pytest":
            is_test, kind = True, "pytest"
        elif any(p.endswith("runtests.py") for p in parts[1:3]):
            is_test, kind = True, "runtests"
    elif base == "tox":
        is_test, kind = True, "tox"
    elif base == "make" and "test" in parts[1:]:
        is_test, kind = True, "make_test"
    elif base in ("go", "cargo") and len(parts) > 1 and parts[1] == "test":
        is_test, kind = True, f"{base}_test"
    elif base in ("npm", "yarn", "npx") and "test" in parts[1:3]:
        is_test, kind = True, f"{base}_test"
    if not is_test:
        return False, None

    if env_prefix:
        return True, "env_prefix"
    if variant:
        return True, "interpreter_variant"
    if kind == "pytest" and "-k" in parts:
        return True, "pytest_-k_filter"
    return True, f"runner:{kind}"


def arm_name(run_dir):
    m = re.search(r"arm_[A-Za-z0-9]+", run_dir or "")
    return m.group(0) if m else Path(run_dir or "?").name[:24]


def candidate_lines(text):
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip().strip("`")
        if not ln or ln.startswith(("#", "```")):
            continue
        ok, _ = test_invocation_sig(ln.lstrip("$ ").strip())
        if ok:
            out.append(ln.lstrip("$ ").strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--testset", required=True)
    ap.add_argument("--speculation", default="latency-opt/speculation")
    ap.add_argument("--bench", default=None)
    ap.add_argument("--examples", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.speculation).resolve()))
    import predict_parse
    from spec_families import parse_command
    from predictor_eval import score_pair

    def fam_best(preds, observed):
        pp = [(c, parse_command(c)) for c in preds]
        pp = [(c, p) for c, p in pp if p and p.get("targets")]
        oo = [p for p in (parse_command(c) for c in observed) if p and p.get("targets")]
        best, pair = 0.0, None
        for o in oo:
            for c, p in pp:
                s = score_pair(p, o)
                if s > best:
                    best, pair = s, (c, p["targets"], o["targets"])
        return best, pair, pp, oo

    buckets = defaultdict(list)
    gap_sigs = Counter()
    gap_examples = defaultdict(list)
    near_types = Counter()
    empty_by_arm = Counter()
    reparse_wins = reparse_losses = 0
    n_cases = n_with_pred = n_hit = 0
    fam_sum = anyhit_sum = 0.0
    fam_sum_pred = 0.0
    fixable_gran = 0

    for ln in open(args.testset):
        try:
            case = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if args.bench and case.get("bench") != args.bench:
            continue
        n_cases += 1
        task = case.get("task")
        observed = case.get("observed") or []
        raw = case.get("raw") or {}

        preds = case.get("predicted") or []
        if raw:
            text = raw.get("answer_text") or ""
            if not text and raw.get("stdout"):
                text = predict_parse.extract_agent_text(raw["stdout"])
            if text:
                rep = predict_parse.extract_commands(
                    text, mode=raw.get("mode") or "generic", limit=5,
                    validator=predict_parse.looks_like_command)
                if rep is not None:
                    b0, _, _, _ = fam_best(case.get("predicted") or [], observed)
                    b1, _, _, _ = fam_best(rep, observed)
                    if b1 > b0:
                        reparse_wins += 1
                    elif b1 < b0:
                        reparse_losses += 1
                    preds = rep

        best, pair, pp, oo = fam_best(preds, observed)
        fam_sum += best
        obs_norm = {norm_ws(c) for c in observed}
        any_hit = any(norm_ws(c) in obs_norm for c in preds)
        anyhit_sum += 1.0 if any_hit else 0.0

        ex_dict = {"task": task, "preds": preds[:5], "observed_n": len(observed)}

        if preds:
            n_with_pred += 1
            fam_sum_pred += best
        if not preds:
            empty_by_arm[arm_name(case.get("run_dir"))] += 1
            cands = candidate_lines(raw.get("answer_text") or "")
            if cands:
                buckets["validator_dropped"].append({**ex_dict, "dropped": cands[:5]})
            else:
                buckets["model_empty"].append(ex_dict)
        elif not pp:
            buckets["pred_unparseable"].append(ex_dict)
        elif best == 1.0 and any_hit:
            n_hit += 1
        elif best == 1.0:
            # family-perfect but NO candidate exact-hit: sub-type the pred
            # that family-matched (the others are extra candidates)
            t = near_miss_type(pair[0], observed) if pair else "other"
            near_types[t] += 1
            buckets["exact_near_miss"].append({**ex_dict, "matched_pred": pair and pair[0],
                                               "subtype": t})
        elif best == 0.8:
            fixable_gran += 1
            buckets["granularity"].append({**ex_dict, "pair": pair and pair[1:]})
        elif best == 0.2:
            buckets["family_disjoint"].append(ex_dict)
        elif best == 0.0 and oo:
            buckets["wrong_targets"].append(ex_dict)

        # observed-side coverage: real runner invocations only
        for c in observed:
            if parse_command(c):
                continue
            segs = [s for s in SEG_SPLIT.split(c) if s.strip()]
            if len(segs) > 1 and parse_command(segs[0].strip()):
                buckets["compound_opportunity"].append({"task": task, "cmd": c[:160]})
                continue
            for seg in segs:
                seg = seg.strip()
                if parse_command(seg):
                    continue
                ok, sig = test_invocation_sig(seg)
                if ok:
                    gap_sigs[sig] += 1
                    if len(gap_examples[sig]) < args.examples:
                        gap_examples[sig].append({"task": task, "cmd": seg[:160]})

    # ---- report --------------------------------------------------------------
    print(f"\ncases={n_cases} (with_pred={n_with_pred})  "
          f"family_mean(all)={fam_sum/max(n_cases,1):.3f}  "
          f"family_mean(with_pred)={fam_sum_pred/max(n_with_pred,1):.3f}")
    print(f"any-exact-hit rate: all={anyhit_sum/max(n_cases,1):.3f}  "
          f"with_pred={anyhit_sum/max(n_with_pred,1):.3f}  hits={n_hit}")
    print(f"reparse wins/losses vs as-parsed: {reparse_wins}/{reparse_losses}")

    if empty_by_arm:
        print("\n== empty predictions per arm (predictor-off arms are expected here) ==")
        for a, n in empty_by_arm.most_common():
            print(f"  {a:20s} {n:4d}")

    order = ["validator_dropped", "model_empty", "pred_unparseable",
             "exact_near_miss", "granularity", "compound_opportunity",
             "family_disjoint", "wrong_targets"]
    print("\n== buckets ==")
    for b in order:
        rows = buckets.get(b, [])
        print(f"  {b:22s} {len(rows):4d}")
        if b == "model_empty":
            continue  # arm table above is the useful view
        for r in rows[:args.examples]:
            print(f"      {json.dumps(r, default=str)[:200]}")

    if near_types:
        print("\n== exact_near_miss sub-types (family-matched pred only) ==")
        for t, n in near_types.most_common():
            print(f"  {t:14s} {n:4d}")

    if gap_sigs:
        print("\n== normalizer_gaps: RUNNER invocations parse_command refuses ==")
        for s, n in gap_sigs.most_common():
            print(f"  {s:22s} {n:4d}")
            for e in gap_examples[s][:3]:
                print(f"      [{e['task']}] {e['cmd']}")

    print("\n== potential gains ==")
    print(f"  family 0.8->1.0 via granularity-ladder pre-runs: {fixable_gran}")
    print(f"  compound prefix-serve candidates               : "
          f"{len(buckets.get('compound_opportunity', []))}")
    print(f"  runner-invocation normalizer gaps              : {sum(gap_sigs.values())}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"cases": n_cases, "with_pred": n_with_pred,
             "family_mean_all": fam_sum / max(n_cases, 1),
             "family_mean_with_pred": fam_sum_pred / max(n_with_pred, 1),
             "any_hit_rate_with_pred": anyhit_sum / max(n_with_pred, 1),
             "reparse_wins": reparse_wins, "reparse_losses": reparse_losses,
             "empty_by_arm": dict(empty_by_arm),
             "buckets": {k: v for k, v in buckets.items()},
             "near_miss_types": dict(near_types),
             "normalizer_gaps": dict(gap_sigs)}, indent=2, default=str))
        print(f"\nreport -> {args.out}")


if __name__ == "__main__":
    main()
