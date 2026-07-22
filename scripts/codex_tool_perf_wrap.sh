#!/usr/bin/env bash
# Per-tool wrapper: the patched agent prefixes every shell command with this
# script when CODEX_TOOL_PERF_WRAPPER is set. It records argv, perf stat, and
# /usr/bin/time -v for each command into $CODEX_TOOL_PERF_DIR/<tool_id>/.
set -uo pipefail

if [ -z "${CODEX_TOOL_PERF_DIR:-}" ]; then
    echo "ERROR: CODEX_TOOL_PERF_DIR is not set" >&2
    exec "$@"
fi

# x86 (AMD/Intel) generic event names. On aarch64/GH200 override PERF_EVENTS
# with the ARM PMU names from the original guide.
PERF_EVENTS="${PERF_EVENTS:-task-clock,cycles,instructions,cache-references,cache-misses,branches,context-switches,cpu-migrations,page-faults}"

TOOL_ID="${CODEX_TOOL_ID:-tool_$(date +%s%N)_$$}"
SAFE_TOOL_ID="$(printf '%s' "$TOOL_ID" | tr -c 'A-Za-z0-9_.-' '_')"
OUT="$CODEX_TOOL_PERF_DIR/$SAFE_TOOL_ID"
mkdir -p "$OUT"

START_NS="$(date +%s%N)"
printf '%q ' "$@" > "$OUT/command.txt"
printf '\n' >> "$OUT/command.txt"

python3 - "$OUT/argv.json" "$@" <<'PY'
import json
import sys
from pathlib import Path
out = Path(sys.argv[1])
argv = sys.argv[2:]
out.write_text(json.dumps({"argv": argv}, indent=2))
PY

set +e
/usr/bin/time \
    -v \
    -o "$OUT/time_v.txt" \
    perf stat \
    -x, \
    -o "$OUT/perf_stat.csv" \
    -e "$PERF_EVENTS" \
    -- "$@"
RC="$?"
set -e
END_NS="$(date +%s%N)"

python3 - "$OUT/metadata.json" <<PY
import json
from pathlib import Path
meta = {
    "tool_id": "$SAFE_TOOL_ID",
    "start_ns": int("$START_NS"),
    "end_ns": int("$END_NS"),
    "wall_ms": (int("$END_NS") - int("$START_NS")) / 1e6,
    "returncode": int("$RC"),
    "perf_events": "$PERF_EVENTS",
}
Path("$OUT/metadata.json").write_text(json.dumps(meta, indent=2))
PY

exit "$RC"
