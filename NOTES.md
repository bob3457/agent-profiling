# spec-expand-v1 — parser audit + SWE-bench suite expansion

Two workstreams, both zero-risk to the existing tree (all new files).

## A. Parser data-gathering (Hopper, GH200 or login, zero tokens)

`testset/audit_testset.py` sits on top of the existing
build_testset -> replay_testset pipeline and turns scores into an
ACTIONABLE change list. Buckets map 1:1 to edit sites:

| bucket | edit site |
|---|---|
| validator_dropped | predict_parse.looks_like_command recall |
| model_empty | prompt / predictor, not parser |
| pred_unparseable | extract_commands output phrasing vs spec_families |
| exact_near_miss (sub-typed) | normalize_* key canonicalization |
| granularity (0.8) | file-level serve / key relaxation |
| normalizer_gaps | new families for spec_families / ecosystems.py |
| compound_opportunity | spec_compound prefix-serve |
| family_disjoint / wrong_targets | model quality — ignore for parser work |

Run:

    cd /projects/kzhou6/czhai/agent-profiling
    tar xf /scratch/czhai/spec-expand-v1.tar.gz
    # (re)build the testset from everything on disk — include ALL arm runs,
    # not just arm_C, for maximum observed-command coverage:
    python3 testset/build_testset.py \
        --results '/scratch/czhai/latency-eval/results/arm_*' \
        --out /scratch/czhai/latency-eval/testset.jsonl \
        --capture-dir /scratch/czhai/latency-eval/pred_capture \
        --ledger-dir  /scratch/czhai/latency-eval/ledger
    python3 testset/audit_testset.py \
        --testset /scratch/czhai/latency-eval/testset.jsonl \
        --bench swebench --examples 5 \
        --out /scratch/czhai/latency-eval/parser_audit.json

Interpretation notes:
- Cases with raw captures are auto-reparsed with the CURRENT parser, so
  buckets reflect what v2 still gets wrong, not old-parser bugs.
- "potential gains" at the bottom quantifies exact-hit and family
  improvements per candidate fix — use it to order parser work.
- If you haven't run with SPEC_PRED_CAPTURE_DIR set yet, most cases are
  as-parsed-only; still fully useful for normalizer_gaps / granularity /
  exact_near_miss (those score run-time predictions vs observed commands).

## B. Suite expansion: 2–5 instances from every other Verified repo

Verified has 10 repos beyond django/astropy: flask, matplotlib, pylint,
pytest, requests, scikit-learn, seaborn, sphinx, sympy, xarray. All are
pytest-graded except sympy (bin/test) — expect sympy to light up
normalizer_gaps; that's a feature (data for the agnostic ecosystems work),
but drop it via `--exclude sympy` if you want clean pytest-only expansion.

Pipeline (all LOGIN node — needs network; `datasets` in the conda env):

    # 1. rank candidates per repo (computable criteria only: F2P
    #    concentration, size, whether the issue names a test path)
    python3 scripts/select_swe_extra.py --per-repo-candidates 8

    # 2. pull arm64 SIFs, falling through ranked candidates per repo when
    #    an arm64 image isn't published. ~2-6 GB per SIF — check quota.
    PER_REPO=3 SIF_DIR=/scratch/czhai/sifs-arm64 \
        bash scripts/pull_swe_extra_sifs.sh

    # 3. materialize bare workspaces + prompts + manifest
    python3 scripts/materialize_swe_extra.py

Outputs line up with the existing harness contracts:
- SIFs land as $SIF_DIR/<iid>.sif — exactly what run_option_b.sh expects.
- manifests/swebench_extra.tsv has the same 3-column shape as
  swebench_arm25.tsv, so run_latency_arm.sh / eval-set generation can
  point at it unchanged.
- Bare workspaces: scikit-learn and matplotlib need compiled extensions,
  so bare git checkouts will NOT run tests without a build. For those
  repos run inside the SIF (option B path, /testbed +
  /opt/miniconda3/envs/testbed) — the pure-Python repos (flask, requests,
  pylint, pytest, seaborn, sphinx, xarray) work either way.

Suggested defaults: PER_REPO=3 x 10 repos = 30 new instances, ~60–150 GB
of SIFs. If quota is tight start with PER_REPO=2 and
`--repos flask requests pytest xarray seaborn` (small images, pure Python).

## Sanity

audit_testset.py was smoke-tested offline against the repo's real
speculation modules with synthetic cases covering every bucket.
