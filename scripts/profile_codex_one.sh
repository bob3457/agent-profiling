#!/usr/bin/env bash
# Clean mode: perf stat + /usr/bin/time around the WHOLE agent run,
# no per-tool wrapping. Low overhead; good for wall time and CPU utilization.
set -euo pipefail

if [ "$#" -ne 5 ]; then
    echo "Usage: $0 BENCHMARK EXAMPLE_ID ITERATION WORKSPACE_TEMPLATE PROMPT_FILE"
    exit 1
fi

BENCHMARK="$1"
EXAMPLE_ID="$2"
ITER="$3"
TEMPLATE="$4"
PROMPT_FILE="$5"

if [ -z "${CODEX_SRC_BIN:-}" ]; then
    echo "ERROR: CODEX_SRC_BIN is not set"
    exit 1
fi

PERF_EVENTS="${PERF_EVENTS:-task-clock,cycles,instructions,cache-references,cache-misses,branches,context-switches,cpu-migrations,page-faults}"

ROOT="$(pwd)"
OUT="$ROOT/results_profiled/$BENCHMARK/$EXAMPLE_ID/iter_$ITER"
WORK="$OUT/workspace"
mkdir -p "$OUT"
rm -rf "$WORK"
rm -f "$OUT"/internal.jsonl "$OUT"/hooks.jsonl "$OUT"/stdout.jsonl "$OUT"/stderr.txt "$OUT"/perf_stat.csv "$OUT"/time_v.txt "$OUT"/metadata.json

if [ -d "$TEMPLATE/.git" ]; then
    git clone "$TEMPLATE" "$WORK" >/dev/null 2>&1
    git -C "$WORK" reset --hard >/dev/null 2>&1 || true
else
    mkdir -p "$WORK"
    rsync -a --exclude ".venv" --exclude "__pycache__" --exclude ".pytest_cache" --exclude "target" "$TEMPLATE"/ "$WORK"/
fi

PROMPT="$(cat "$PROMPT_FILE")"
T0="$(date +%s%N)"
set +e
(
    cd "$WORK"
    unset CODEX_TOOL_PERF_DIR || true
    unset CODEX_TOOL_PERF_WRAPPER || true
    unset CODEX_TOOL_ID || true
    CODEX_PROFILE_JSONL="$OUT/internal.jsonl" \
    AGENT_PROFILE_JSONL="$OUT/hooks.jsonl" \
    perf stat -x, -o "$OUT/perf_stat.csv" -e "$PERF_EVENTS" -- \
    /usr/bin/time -v -o "$OUT/time_v.txt" \
    "$CODEX_SRC_BIN" \
        --sandbox danger-full-access \
        --ask-for-approval never \
        exec \
        --skip-git-repo-check \
        --json \
        "$PROMPT" \
        > "$OUT/stdout.jsonl" \
        2> "$OUT/stderr.txt"
)
RC="$?"
set -e
T1="$(date +%s%N)"

python3 - <<PY
import json
from pathlib import Path
out = Path("$OUT")
meta = {
    "benchmark": "$BENCHMARK",
    "example_id": "$EXAMPLE_ID",
    "iteration": int("$ITER"),
    "returncode": int("$RC"),
    "wall_ms": (int("$T1") - int("$T0")) / 1e6,
    "workspace": "$WORK",
    "perf_events": "$PERF_EVENTS",
    "mode": "clean_whole_run_perf",
}
(out / "metadata.json").write_text(json.dumps(meta, indent=2))
PY

echo "Wrote $OUT"
echo "Return code: $RC"
