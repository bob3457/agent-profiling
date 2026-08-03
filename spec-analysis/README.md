# spec-analysis — ground-truth data tools for the speculation stack

`summary.json` cache_hits undercounts (full replies only; partial prefix
serves report `cached=False`) and `commands.jsonl` writes `wall_s=0.0` for
every cached reply, including joins where the agent really waited. The
decisions log is the ground truth; these tools read it.

## decompose_serves.py

Per-category savings decomposition from `spec_cache/serve_decisions.jsonl`
(+ `shelld_logs/commands.jsonl` + `daemon_stats.txt`):

    python3 spec-analysis/decompose_serves.py \
        /scratch/czhai/latency-eval/results/arm_C.20260803_150709
    # single task dir, machine output, invariant enforcement:
    python3 spec-analysis/decompose_serves.py $RD --events
    python3 spec-analysis/decompose_serves.py DIR --jsonl
    python3 spec-analysis/decompose_serves.py DIR --check   # exit 1 on warn

Categories: exact / joined (net = entry_dur − waited; negatives flagged) /
prefix_full / prefix_partial / miss taxonomy / inflight_timeout waste.
Handles the join double-log (joined_inflight + served = ONE event) and
reconciles against daemon_stats cache_hits and commands.jsonl cached counts.

## stress_inflight.py

Behavioral probes past smoke_inflight's 16 checks, on the real daemon:
thundering herd (6 concurrent joins, one writer), join-then-stale-generation
(must not serve), prefix part-probes never wait, timeout fallthrough,
net-negative join, wall_s accounting-gap recovery, and an end-to-end
decomposer cross-check on the dirs the suite itself produces. 23 checks.

    python3 spec-analysis/stress_inflight.py $ROOT    # expect ALL PASS (23)

## Known telemetry gap (measured by S2, not yet patched)

When a join wait resolves into an entry that then FAILS validation
(stale generation/fingerprint), sessiond logs only the miss reason — the
`joined_inflight` line is written on the success path only, and no timeout
fires because the entry did land. That wait is currently invisible waste in
the decisions log. One-line fix if it matters at sweep scale: include
`waited_s` in the miss record inside `spec_cache_lookup`.
