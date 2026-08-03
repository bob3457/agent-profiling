# spec-agnostic — task-agnostic speculator + TB enablement

> **Status (2026-08): fully deployed.** Every change described below is
> already applied to the files tracked in this repo; the one-shot patchers
> have been moved to `../archive/patchers/spec-agnostic/`. The deploy steps
> below are kept as a historical record only — do not re-run them.


Contents: one module replacement, three patchers, two tools. All verified
offline against a copy of the current repo (all 23 smoke checks green,
patchers idempotent, `bash -n` / `py_compile` clean, spec_compound suite
and edit_respec/worker imports unaffected).

## Deploy (on gracehopper, repo root = /projects/kzhou6/czhai/agent-profiling)

    cd /scratch/czhai && tar xzf spec-agnostic.tar.gz && cd spec-agnostic
    ROOT=/projects/kzhou6/czhai/agent-profiling
    cp $ROOT/latency-opt/speculation/spec_tiers.py $ROOT/latency-opt/speculation/spec_tiers.py.v1  # backup
    cp spec_tiers.py $ROOT/latency-opt/speculation/spec_tiers.py
    python3 patch_worker_generic.py    $ROOT
    python3 patch_predictor_generic.py $ROOT
    python3 patch_harness_tb.py        $ROOT
    python3 $ROOT/latency-opt/speculation/spec_tiers.py     # 116/116
    python3 smoke_agnostic.py $ROOT                          # ALL PASS expected

## What changed

**spec_tiers.py v2 (replacement, API-compatible: classify/created_paths/TIER0/TIER1/NONE)**
- Quote-aware hazard scan replaces the blunt substring operator check:
  quoted `|`/`;`/`>` are literal; backtick and `$(` stay live inside double
  quotes; unterminated quotes refuse. Lifts grep/awk/sed-with-quoted-pipes
  out of the refused set.
- ~40 new pure heads (sort/uniq/cut/tr/jq/od/xxd/strings/nl/column/...),
  archive readers (zcat/zipinfo/tar-list/gzip -l/unzip -l), guarded
  sed (whitelist script grammar: p/d/q/=/n/N/s/// without w/e; no -i/-f),
  guarded awk (no -f, no -i inplace, no system()), sqlite3 single read-only
  statement, pip list/show/freeze/check, extra safe `python -m` modules,
  generic `<tool> --version|--help` probe rule.
- SAFETY FIXES vs v1: `git branch <name>` and `git remote add` were TIER0
  (they mutate) — now guarded. `date` was TIER0 — now NONE (time-varying:
  cached output is a wrong answer). `sleep` explicitly NONE.
- BEHAVIOR NOTE: any pre-existing serve of `date`/`git branch`-with-args
  disappears. That is intended.

**patch_worker_generic.py → speculative_worker.py**
- `workspace_recon` action: ls/ls -la/pwd/bounded find, `ls` of top dirs,
  `cat` + `wc -l` of small (≤32KB, non-binary, ≤15 files) top-level files.
  Joins EVERY plan (SPEC_RECON=0 disables); the upstream-GO fallback plan
  becomes ["workspace_recon", "repo_index"].
- LLM-direct path: non-pytest/django predictions are no longer dropped.
  Whole TIER0 predictions pre-run + cached verbatim; compound predictions
  split via spec_compound, leading cd folded, TIER0 parts pre-run
  individually (prefix-serve assembles at serve time). TIER1 deliberately
  excluded in the worker (no undo ledger there; edit_respec keeps its own
  tier1 handling).

**patch_predictor_generic.py → llm_predictor.py**
- SPEC_PREDICT_MODE=auto|tests|generic (default auto: tests iff
  runtests.py or ≥3 test_*.py). Generic mode: instruction + general
  two-level workspace listing → "first 5 shell commands", command-shaped
  line filter instead of pytest/django parse (tier policy downstream is
  the real gate). meta gains "mode" → lands in the ledger.

**patch_harness_tb.py → run_latency_arm.sh**
- Fixes TB SKIP bug: adds runs/terminalbench-arm/<tid>/base_task as a
  workdir candidate and prompts/tb_<tid>.txt as a prompt candidate,
  matching what materialize_tb_baremetal.py actually writes.

## Tools

**corpus_coverage.py** — the measurement to run FIRST:

    python3 corpus_coverage.py /scratch/czhai/latency-eval/results \
        --spec-dir $ROOT/latency-opt/speculation                    # v2 (deployed)
    python3 corpus_coverage.py ... --tiers $ROOT/latency-opt/speculation/spec_tiers.py.v1   # v1 baseline

  Reports simple/compound servability, leading-prefix part coverage
  (fold_cd_serve applied, matching daemon semantics), ANY-servable-surface
  % of commands AND of logged wall-seconds, and the top refused heads by
  count and seconds — i.e., the ranked list of what to add to the
  vocabulary next. Run it on the full TB + SWE commands.jsonl corpus and
  diff v1 vs v2; that delta is a result on its own. Note it's a LOWER
  bound: whole-command exact/family serves of worker-seeded strings don't
  pass through classify.

**smoke_agnostic.py** — offline acceptance (no codex/daemon/network):
  tiers suite, coverage sanity, worker end-to-end in a generic non-python
  workspace with a stubbed LLM (asserts recon + direct caching, refusal of
  rm/sed -i/pip install/./script predictions, workspace untouched),
  predictor mode selection + prose filter.

## Before the first TB Arm C run

- Seed pristine copies: rsync the materialized workspaces to
  /scratch/czhai/latency-eval/tb_pristine/<task>/base_task (the harness
  rsyncs them back before each run).
- Keep the eval set to TB tasks already proven runnable baremetal.
- Gate on TB uses the stream signal (statement-only stays SWE-only);
  expect the pending-gate outcome on short TB tasks — that's measurement.

## Scoped out (unchanged from last session)

Heredoc/pipe-between-parts class; skip-until-`;` mixed-joiner refinement;
shadow-workspace empirical purity (the principled replacement for the
allowlist — follow-on project).
