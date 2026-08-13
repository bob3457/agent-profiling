# spec-heads-v1 — TraceLab executable whitelist fold-in

> **Status: fully deployed.** The whitelist is already merged into
> `latency-opt/speculation/predict_parse.py`; the one-shot patcher lives
> in `../archive/patchers/spec-heads-v1/`. The apply steps below are a
> historical record only — do not re-run them.

Apply AFTER spec-parse-v2 (patches predict_parse.py in place):

    cd /projects/kzhou6/czhai/agent-profiling
    tar xf /scratch/czhai/spec-heads-v1.tar.gz
    python3 spec-heads-v1/patch_known_heads.py --root .
    python3 latency-opt/speculation/predict_parse.py   # self-test must pass

What it does: merges uw-syfi/TraceLab public_common_executables.txt
(354 executables observed across 357K real Claude Code/Codex rounds,
Apache-2.0) into the generic-mode command validator. Three tiers:

  - 314 bare-accept known heads (was 114): cargo/go/docker/uv/gh/
    nvidia-smi/torchrun/conda etc. now validate bare
  - 30 ambiguous sentence-starter heads (sort, which, install, convert,
    find, test, echo, head, file, date, type, ...) require command
    structure: a flag, path-ish/$/digit arg, shell operator, or the
    short "which python" shape. This also CLOSES pre-existing prose
    holes: "Sort the results" / "Find the failing test" validated
    before this patch, rejected after.
  - shell builtins excluded (alias, eval, set, return, true, wait, ...)

Known accepted trade-off: "make" stays bare-accept ("make test" too
common to lose), so "Make sure it compiles"-style prose still passes.

Scope: candidate validation in generic mode only — cache keys, tests
mode, and serving untouched. Measure the effect for free: re-run
replay_testset.py over the existing testset; only generic-mode (TB)
reparses can change.
