#!/usr/bin/env bash
# HotpotQA fullwiki profiling runner.
# Differences vs. profile_codex_cpu_deepdive.sh:
#   - adds the web-search codex flags used by the codexlow-search benchmark row
#     (-c tools.web_search=true, -c model_reasoning_effort=$CODEX_REASONING, -m $CODEX_MODEL)
#   - tool_perf wrapper stays enabled but expect ~0 entries: fullwiki searches
#     run server-side, not as local shell commands. The whole-run perf stat is
#     the primary measurement here.
# Output layout matches the deep-dive tree so existing analyzers pick it up:
#   results_cpu_deepdive/hotpotqa/<qid>/iter_<n>/
set -uo pipefail
if [ "$#" -ne 5 ]; then
  echo "Usage: $0 BENCHMARK EXAMPLE_ID ITERATION WORKSPACE_TEMPLATE PROMPT_FILE" >&2
  exit 2
fi
BENCHMARK="$1"; EXAMPLE_ID="$2"; ITER="$3"; TEMPLATE="$4"; PROMPT_FILE="$5"

ROOT="${ROOT:-/projects/kzhou6/czhai/agent-profiling}"
# prefer the aarch64 build dir used on gracehopper; fall back to default target/
_BIN_A="$ROOT/agent-src/codex-rs/target-aarch64/release/codex"
_BIN_B="$ROOT/agent-src/codex-rs/target/release/codex"
if [ -z "${CODEX_SRC_BIN:-}" ]; then
  if [ -x "$_BIN_A" ]; then CODEX_SRC_BIN="$_BIN_A"; else CODEX_SRC_BIN="$_BIN_B"; fi
fi
[ -x "$CODEX_SRC_BIN" ] || { echo "Missing executable CODEX_SRC_BIN=$CODEX_SRC_BIN" >&2; exit 1; }

CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
CODEX_REASONING="${CODEX_REASONING:-low}"
PERF_EVENTS="${PERF_EVENTS:-task-clock,cpu_cycles,inst_retired,l1d_cache,l1d_cache_refill,l2d_cache,l2d_cache_refill,br_retired,context-switches,cpu-migrations,page-faults}"

OUT="$ROOT/results_cpu_deepdive/$BENCHMARK/$EXAMPLE_ID/iter_$ITER"
WORK="$OUT/workspace"
rm -rf "$OUT"
mkdir -p "$OUT/tool_perf" "$WORK"
[ -f "$PROMPT_FILE" ] || { echo "Missing prompt file: $PROMPT_FILE" >&2; exit 1; }
[ -d "$TEMPLATE" ] || { echo "Missing workspace template: $TEMPLATE" >&2; exit 1; }
cp -a "$TEMPLATE"/. "$WORK"/

PROMPT="$(cat "$PROMPT_FILE")"
START_NS="$(date +%s%N)"
set +e
(
  cd "$WORK"
  export CODEX_PROFILE_JSONL="$OUT/internal.jsonl"
  export AGENT_PROFILE_JSONL="$OUT/hooks.jsonl"
  export CODEX_TOOL_PERF_WRAPPER="$ROOT/scripts/codex_tool_perf_wrap.sh"
  export CODEX_TOOL_PERF_DIR="$OUT/tool_perf"
  export PERF_EVENTS="$PERF_EVENTS"
  /usr/bin/time -v -o "$OUT/time_v.txt" \
    perf stat -x, -o "$OUT/perf_stat.csv" -e "$PERF_EVENTS" -- \
    "$CODEX_SRC_BIN" \
      --sandbox danger-full-access \
      --ask-for-approval never \
      exec \
      --skip-git-repo-check \
      -c "tools.web_search=true" \
      -c "model_reasoning_effort=$CODEX_REASONING" \
      -m "$CODEX_MODEL" \
      --json \
      "$PROMPT" \
      > "$OUT/stdout.jsonl" \
      2> "$OUT/stderr.txt"
)
RC=$?
set -e
END_NS="$(date +%s%N)"

python3 - <<PY
import json
from pathlib import Path
meta = {
    "benchmark": "$BENCHMARK",
    "example_id": "$EXAMPLE_ID",
    "iteration": int("$ITER"),
    "returncode": int("$RC"),
    "wall_ms": (int("$END_NS") - int("$START_NS")) / 1e6,
    "workspace": "$WORK",
    "perf_events": "$PERF_EVENTS",
    "mode": "hotpot_fullwiki_clean_perf_with_tool_wrapper",
    "codex_model": "$CODEX_MODEL",
    "codex_reasoning": "$CODEX_REASONING",
    "web_search": True,
}
Path("$OUT/metadata.json").write_text(json.dumps(meta, indent=2))
PY
echo "Wrote $OUT (rc=$RC)"
exit "$RC"
