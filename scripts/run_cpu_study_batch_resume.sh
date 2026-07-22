#!/usr/bin/env bash
# Resume-safe batch runner. Skips completed clean runs and completed deep-dive
# runs that already have tool_perf files. Uses mapfile so child commands can't
# consume the manifest's stdin (classic while-read bug).
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 BENCHMARK MANIFEST_TSV [ITERATION]"
    exit 1
fi

BENCHMARK="$1"
MANIFEST="$2"
ITER="${3:-1}"

PROFILING_ROOT="${PROFILING_ROOT:-/projects/kzhou6/czhai/agent-profiling}"
export CODEX_SRC_BIN="${CODEX_SRC_BIN:-$PROFILING_ROOT/agent-src/codex-rs/target/release/codex}"
export PERF_EVENTS="${PERF_EVENTS:-task-clock,cycles,instructions,cache-references,cache-misses,branches,context-switches,cpu-migrations,page-faults}"
WRAPPER="$PROFILING_ROOT/scripts/codex_tool_perf_wrap.sh"

if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: manifest not found: $MANIFEST"
    exit 1
fi
if [ ! -x "$CODEX_SRC_BIN" ]; then
    echo "ERROR: CODEX_SRC_BIN not executable: $CODEX_SRC_BIN"
    exit 1
fi
if [ ! -x "$WRAPPER" ]; then
    echo "ERROR: wrapper not executable: $WRAPPER"
    exit 1
fi

strings "$CODEX_SRC_BIN" > /tmp/codex_strings_check.$$.txt 2>/dev/null || true
if ! grep -q CODEX_TOOL_PERF_WRAPPER /tmp/codex_strings_check.$$.txt; then
    echo "ERROR: CODEX_SRC_BIN does not contain CODEX_TOOL_PERF_WRAPPER"
    rm -f /tmp/codex_strings_check.$$.txt
    exit 1
fi
rm -f /tmp/codex_strings_check.$$.txt

mapfile -t MANIFEST_LINES < "$MANIFEST"

echo "=== Resume CPU Study Batch ==="
echo "BENCHMARK=$BENCHMARK"
echo "MANIFEST=$MANIFEST"
echo "ITER=$ITER"
echo "ROWS=${#MANIFEST_LINES[@]}"
echo

for LINE in "${MANIFEST_LINES[@]}"; do
    if [ -z "${LINE// }" ]; then
        continue
    fi
    if [[ "$LINE" =~ ^# ]]; then
        continue
    fi
    IFS=$'\t' read -r EXAMPLE TEMPLATE PROMPT <<< "$LINE"
    if [ -z "${EXAMPLE:-}" ] || [ -z "${TEMPLATE:-}" ] || [ -z "${PROMPT:-}" ]; then
        echo "WARNING: bad manifest row: $LINE"
        continue
    fi

    CLEAN_OUT="results_profiled/$BENCHMARK/$EXAMPLE/iter_$ITER"
    DEEP_OUT="results_cpu_deepdive/$BENCHMARK/$EXAMPLE/iter_$ITER"

    echo
    echo "============================================================"
    echo "$BENCHMARK / $EXAMPLE / iter_$ITER"
    echo "============================================================"

    if [ ! -e "$TEMPLATE" ]; then
        echo "ERROR: missing template: $TEMPLATE"
        exit 1
    fi
    if [ ! -f "$PROMPT" ]; then
        echo "ERROR: missing prompt: $PROMPT"
        exit 1
    fi

    if [ -f "$CLEAN_OUT/metadata.json" ]; then
        echo "Skipping clean run; already exists: $CLEAN_OUT"
    else
        echo "Running clean mode..."
        unset CODEX_TOOL_PERF_DIR || true
        unset CODEX_TOOL_PERF_WRAPPER || true
        unset CODEX_TOOL_ID || true
        bash scripts/profile_codex_one.sh "$BENCHMARK" "$EXAMPLE" "$ITER" "$TEMPLATE" "$PROMPT" < /dev/null
    fi

    TOOL_FILES=0
    if [ -d "$DEEP_OUT/tool_perf" ]; then
        TOOL_FILES="$(find "$DEEP_OUT/tool_perf" -maxdepth 2 -type f | wc -l || true)"
    fi

    if [ -f "$DEEP_OUT/metadata.json" ] && [ "$TOOL_FILES" -gt 0 ]; then
        echo "Skipping deep-dive run; already exists with tool files: $DEEP_OUT"
    else
        echo "Running CPU deep-dive mode..."
        rm -rf "$DEEP_OUT"
        bash scripts/profile_codex_cpu_deepdive.sh "$BENCHMARK" "$EXAMPLE" "$ITER" "$TEMPLATE" "$PROMPT" < /dev/null
    fi

    TOOL_FILES="$(find "$DEEP_OUT/tool_perf" -maxdepth 2 -type f 2>/dev/null | wc -l || true)"
    echo "Tool perf files: $TOOL_FILES"
    if [ "$TOOL_FILES" -eq 0 ]; then
        echo "WARNING: zero tool perf files for $EXAMPLE"
        echo "Check: $DEEP_OUT/stdout.jsonl"
        echo "Check: $DEEP_OUT/stderr.txt"
    fi
done

echo
echo "Resume batch complete."
