# agent-profiling

Profiling and latency-optimization toolkit for AI coding agents (Codex CLI)
on GMU HPC (x86 Hopper nodes and the GH200 aarch64 node).

## Repository layout

| dir | what it is |
|---|---|
| `scripts/` | baseline CPU deep-dive pipeline: codex-from-source setup, shell.rs perf-wrapper patcher, per-tool perf runners, batch runner, analyzer, benchmark task materializers (Terminal-Bench, SWE-bench arm25, HotpotQA, FreshQA) |
| `codex_hooks/` | Pre/PostToolUse/Stop hook script + `hooks.json` |
| `latency-opt/` | the optimization arms: persistent shell daemon (`scripts/shell_sessiond.py` + wrapper), gated speculative execution (`speculation/`), 3-arm harness (`harness/run_latency_arm.sh`, `harness/run_option_b.sh`), eval sets (`eval_sets/`), arm comparison and window reports. See `latency-opt/README.md` |
| `spec-agnostic/` | task-agnostic speculator: corpus coverage tool, smoke tests. See `spec-agnostic/NOTES.md` |
| `spec-analysis/` | ground-truth analysis of speculation runs: serve decomposition, miss autopsy, ceiling report, task selection, in-flight stress probes. See `spec-analysis/README.md` |
| `spec-measure/` | the four-measurement campaign (gate accuracy, prediction volume, seconds saved, CPU/token cost): report generator + smoke tests. See `spec-measure/README.md` |
| `manifests/` | which benchmark instances each study ran (tiny, provenance) |
| `prompts/` | exact prompts sent to the agent (`tbench_*` = the 10 dummy CPU-study tasks; `tb_*` = bare-metal Terminal-Bench materializations; `swe_*`, `hotpot_*`, `fresh_*`) |
| `archive/` | already-applied one-shot patchers (latency-opt, spec-agnostic, spec-measure) and old coverage outputs — historical only, see `archive/README.md` |
| `env.sh` | GH200 (aarch64) environment profile — ARM PMU event names; x86 values are in the section below |

Self-tests (all runnable offline from the repo root with `ROOT=$PWD`):
`latency-opt/speculation/spec_tiers.py`, `.../spec_compound.py`,
`latency-opt/smoke_prefix_serve.py latency-opt/scripts/shell_sessiond.py`,
`spec-agnostic/smoke_agnostic.py $PWD`, `spec-agnostic/smoke_inflight.py $PWD`.

---

# Baseline study: Agent CPU Deep-Dive Profiling — Hopper `/projects` Adaptation

Adapted from Tejas's "Codex CPU Deep-Dive Profiling Reproduction Guide" for:
- Root: `/projects/kzhou6/czhai/agent-profiling` (override with `PROFILING_ROOT` / `ROOT`)
- x86 Hopper compute nodes (perf events changed from ARM PMU names to generic
  x86 names). If you later run on GH200 (aarch64), export the ARM event list
  from the original PDF instead; the analyzer handles both column sets.
- The new agent (Codex fork): set `AGENT_REPO_URL` before running setup.

## Changes vs. the PDF
1. All `/mnt/data/tramesh2/agent-profiling` paths -> `$ROOT`, configurable via env.
2. `PERF_EVENTS` default is now
   `task-clock,cycles,instructions,cache-references,cache-misses,branches,context-switches,cpu-migrations,page-faults`.
3. `analyze_cpu_deepdive.py` computes IPC from `perf_cycles`/`perf_instructions`
   (with ARM-name fallback) and replaces L1/L2 refill rates with a generic
   `cache_miss_rate`.
4. Rust toolchain installs to `$ROOT/.cargo` / `$ROOT/.rustup` (home-quota safe).
5. Hooks installer backs up any existing `~/.codex/hooks.json`.
6. `git init -b main` in the git-multibranch task (PDF version breaks if the
   node's git defaults to `master`).
7. Added `scripts/check_perf.sh` — run it on a compute node FIRST.
8. This section covers the baseline study on the 10 dummy Terminal-Bench
   tasks. (The repo has since grown SWE-bench/HotpotQA/FreshQA materializers
   in `scripts/` and the full latency-optimization arms in `latency-opt/` —
   see the layout table at the top.)

## Run order

### Login node (network needed)
```bash
export ROOT=/projects/kzhou6/czhai/agent-profiling
cd $ROOT
module load gcc                      # cargo needs a linker
export AGENT_REPO_URL=<fork git url> # ask Tejas; needs PAT if private
bash scripts/setup_codex_from_source.sh          # ~15-30 min first build

cd $ROOT/agent-src/codex-rs
python $ROOT/scripts/apply_codex_shell_wrapper_patch.py
bash $ROOT/scripts/rebuild_patched_codex.sh
export CODEX_SRC_BIN=$ROOT/agent-src/codex-rs/target/release/codex

bash $ROOT/scripts/install_codex_profile_hooks.sh
cd $ROOT
PROFILING_ROOT=$ROOT python scripts/materialize_terminalbench_cpu_tasks.py
```

### Compute node (salloc / sbatch)
```bash
export ROOT=/projects/kzhou6/czhai/agent-profiling
cd $ROOT
export CODEX_SRC_BIN=$ROOT/agent-src/codex-rs/target/release/codex
# agent auth: export OPENAI_API_KEY=... (or the fork's auth mechanism)

bash scripts/check_perf.sh           # MUST pass before anything else

# Learn on ONE task first:
bash scripts/profile_codex_cpu_deepdive.sh terminalbench broken-python 1 \
    runs/terminalbench/broken-python/base_task prompts/tbench_broken_python.txt

# Explore the output:
ls results_cpu_deepdive/terminalbench/broken-python/iter_1/tool_perf/
cat results_cpu_deepdive/terminalbench/broken-python/iter_1/tool_perf/*/command.txt

# Then the full batch (resume-safe):
bash scripts/run_cpu_study_batch_resume.sh terminalbench manifests/terminalbench_cpu_study_10.tsv 1

# Analyze (needs pandas):
python scripts/analyze_cpu_deepdive.py
```

## What each output means
- `results_profiled/` — clean mode: whole-run perf only. wall time + CPU util.
- `results_cpu_deepdive/` — deep-dive: whole-run perf PLUS one
  `tool_perf/<tool_id>/` dir per shell command the agent executed
  (command.txt, argv.json, perf_stat.csv, time_v.txt, metadata.json).
- `stdout.jsonl` — the agent's --json event stream (turns, tokens, commands).
- `hooks.jsonl` — hook-level tool events (Pre/PostToolUse, Stop).
- `internal.jsonl` — model-call timings, only if the fork carries Tejas's
  internal CODEX_PROFILE_JSONL instrumentation (may be empty; ask him).
- `run_summary.csv` / `per_tool_perf.csv` / `category_perf_summary.csv` —
  analyzer outputs; the last one is the headline category attribution table.

## Known sharp edges
- Patcher can't find `derive_exec_args` => the fork restructured shell.rs;
  find where the `bash -lc` argv is built and adapt the replacement function.
- Zero tool_perf files => wrapper not in binary (strings check), wrapper not
  executable (`chmod +x scripts/codex_tool_perf_wrap.sh`), or env not set.
- `<not supported>` rows in perf_stat.csv => trim those events from PERF_EVENTS.
- Batch stops after one row => you're using a while-read loop somewhere; the
  provided resume runner already avoids this with mapfile + `< /dev/null`.
- Compute nodes have ~50 KiB/s egress on Hopper: fine for agent API calls,
  terrible for downloads. Do all installs/clones on the login node.
