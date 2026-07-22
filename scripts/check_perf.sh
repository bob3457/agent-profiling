#!/usr/bin/env bash
# Run this ON A COMPUTE NODE before any profiling. Verifies perf, event names,
# /usr/bin/time, and perf_event_paranoid.
set -uo pipefail

PERF_EVENTS="${PERF_EVENTS:-task-clock,cycles,instructions,cache-references,cache-misses,branches,context-switches,cpu-migrations,page-faults}"

echo "=== Node info ==="
hostname
uname -m
nproc
echo

echo "=== perf_event_paranoid (need <= 2 for unprivileged perf stat) ==="
if [ -r /proc/sys/kernel/perf_event_paranoid ]; then
    cat /proc/sys/kernel/perf_event_paranoid
else
    echo "cannot read /proc/sys/kernel/perf_event_paranoid"
fi
echo

echo "=== perf present? ==="
if ! command -v perf >/dev/null 2>&1; then
    echo "FAIL: perf not found. Try 'module avail perf' or email ORC support."
    exit 1
fi
perf --version
echo

echo "=== /usr/bin/time present? (scripts use the binary, not the builtin) ==="
if [ ! -x /usr/bin/time ]; then
    echo "FAIL: /usr/bin/time missing."
    exit 1
fi
/usr/bin/time --version 2>&1 | head -1
echo

echo "=== Test run: perf stat + /usr/bin/time around 'sleep 1' ==="
TMP=$(mktemp -d)
if perf stat -x, -o "$TMP/perf_stat.csv" -e "$PERF_EVENTS" -- \
   /usr/bin/time -v -o "$TMP/time_v.txt" sleep 1; then
    echo "OK. perf_stat.csv:"
    cat "$TMP/perf_stat.csv"
    echo
    echo "Look for '<not supported>' rows above -- those events need to be"
    echo "removed from PERF_EVENTS on this node."
else
    echo "FAIL: perf stat returned nonzero. Check event names with 'perf list'."
    rm -rf "$TMP"
    exit 1
fi
rm -rf "$TMP"
echo
echo "All checks passed with PERF_EVENTS=$PERF_EVENTS"
