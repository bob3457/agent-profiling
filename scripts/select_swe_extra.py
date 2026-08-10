#!/usr/bin/env python3
"""select_swe_extra.py — rank SWE-bench_Verified instances per repo (beyond
django/astropy) and emit a candidates manifest for the arm64 SIF pull.

Run on a LOGIN node (needs network + `datasets` in the conda env).

Selection heuristics (all computed from dataset fields — no hand-picking):
  + FAIL_TO_PASS concentrated in ONE test file  (predictable speculation
    target: heuristic and LLM predictor both key off files/labels)
  + 1..12 FAIL_TO_PASS tests                    (not a mega-refactor)
  + problem statement names a test path/file    (helps both predictors —
    but we keep a mix, this is a bonus not a filter)
  + problem statement length 400..9000 chars    (too short = vague,
    too long = pathological)
We emit MORE candidates than needed per repo, ranked, so pull_swe_extra_sifs.sh
can fall through when an arm64 image doesn't exist on Docker Hub.

Usage:
  python3 scripts/select_swe_extra.py \
      --per-repo-candidates 8 \
      --out manifests/swebench_extra_candidates.tsv \
      [--exclude django astropy] [--repos flask requests ...]
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_PATH_RE = re.compile(r"\btests?[/\w.-]*\.py\b|\btest_\w+\b")


def score_row(r):
    try:
        f2p = json.loads(r["FAIL_TO_PASS"])
    except Exception:
        f2p = []
    files = {t.split("::")[0] for t in f2p}
    ps = r["problem_statement"] or ""
    s = 0.0
    if len(files) == 1:
        s += 3.0
    elif len(files) == 2:
        s += 1.0
    n = len(f2p)
    if 1 <= n <= 12:
        s += 2.0
    elif n <= 25:
        s += 0.5
    if 400 <= len(ps) <= 9000:
        s += 1.0
    names_test = bool(TEST_PATH_RE.search(ps))
    if names_test:
        s += 1.0
    return s, n, len(files), names_test


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-repo-candidates", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "manifests/swebench_extra_candidates.tsv"))
    ap.add_argument("--exclude", nargs="*", default=["django", "astropy"])
    ap.add_argument("--repos", nargs="*", default=None,
                    help="restrict to these repo short names (e.g. flask sympy)")
    args = ap.parse_args()

    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")

    by_repo = {}
    for r in ds:
        short = r["repo"].split("/")[-1]
        if short in args.exclude:
            continue
        if args.repos and short not in args.repos:
            continue
        by_repo.setdefault(short, []).append(r)

    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    with out.open("w") as f:
        f.write("repo\trank\tinstance_id\tscore\tn_f2p\tn_test_files\tnames_test\n")
        for repo in sorted(by_repo):
            rows = by_repo[repo]
            ranked = sorted(rows, key=lambda r: score_row(r)[0], reverse=True)
            print(f"\n== {repo} ({len(rows)} instances in Verified) ==")
            for i, r in enumerate(ranked[: args.per_repo_candidates], 1):
                s, n, nf, nt = score_row(r)
                f.write(f"{repo}\t{i}\t{r['instance_id']}\t{s:.1f}\t{n}\t{nf}\t{int(nt)}\n")
                print(f"  {i}. {r['instance_id']:42s} score={s:.1f} "
                      f"f2p={n:<3d} files={nf} names_test={nt}")
    print(f"\ncandidates -> {out}")
    print("next: PER_REPO=3 scripts/pull_swe_extra_sifs.sh  (login node)")


if __name__ == "__main__":
    main()
