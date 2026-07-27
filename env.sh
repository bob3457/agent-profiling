export ROOT=/projects/kzhou6/czhai/agent-profiling
export CODEX_SRC_BIN=$ROOT/agent-src/codex-rs/target-aarch64/release/codex
export PERF_EVENTS="task-clock,cpu_cycles,inst_retired,l1d_cache,l1d_cache_refill,l2d_cache,l2d_cache_refill,br_retired,context-switches,cpu-migrations,page-faults"
export HF_HOME=/scratch/czhai/hf-cache
# export OPENAI_API_KEY=...   # set manually or in a non-committed file
