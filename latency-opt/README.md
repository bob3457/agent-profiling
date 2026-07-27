# agent-latency-opt

Two latency optimizations for the profiled-Codex setup, designed to plug into
the interception point you already have (`CODEX_TOOL_PERF_WRAPPER` from the
shell.rs patch).

```
scripts/
  shell_sessiond.py              persistent shell daemon (opt 1 + serves opt 2's cache)
  codex_persistent_shell_wrap.sh wrapper client: codex -> daemon
speculation/
  spec_gate.py                   should this task speculate at all?
  speculative_worker.py          parallel prep process + result cache
```

## Finding 0: what the "modified codex" actually contains

Tejas's patch adds **zero latency optimizations**. It is pure instrumentation:
`derive_exec_args` prepends `$CODEX_TOOL_PERF_WRAPPER` to the `bash -lc` argv
so every tool command can be observed/measured. If anything it adds overhead
(one extra process + perf per command). The hooks (`profile_hook.py`) and
`CODEX_PROFILE_JSONL` are likewise measurement-only.

**Upstream Codex**, however, ships its own latency features (verified in the
current openai/codex source):

| feature | what it does | default |
|---|---|---|
| `shell_snapshot` | captures the login-shell env ONCE per session, rewrites `bash -lc` into a non-login shell that sources the snapshot — amortizes profile/module/conda-init cost | on |
| `unified_exec` | `exec_command` + `write_stdin` tools: the model can open a persistent PTY process and keep sending input to it (session reuse) | on (non-Windows), was experimental in older versions |
| session startup prewarm | opens the model websocket/session during startup so turn 1 doesn't pay connection setup | on |

Check what YOUR build has (npm 0.144.1 and the source checkout may differ):

```bash
# in the built binary:
strings $CODEX_SRC_BIN | grep -c unified_exec       # >0 => code present
strings $CODEX_SRC_BIN | grep -c shell_snapshot
# what's active at runtime: run one task, then
grep -l write_stdin ~/.codex/log/*   # model was offered the persistent tool?
# or just watch process churn during a run:
ps -ef --forest | grep -A2 codex     # one bash per tool call = classic path
```

## Finding 1: "every terminal call is independent" — verified, with nuance

Accurate for the **classic shell path**: each tool call is a brand-new
process (`bash -lc "<cmd>"`); exports/venv activation/`cd` are lost between
calls. Two softeners exist upstream: `shell_snapshot` (env re-setup is
amortized even though the process is still fresh) and `unified_exec` (true
persistent sessions, but only when the feature is on AND the model chooses to
reuse a session — models frequently still fire one-shot commands).

## Optimization 1: persistent shell sessions (works on any codex version)

Routes every tool command to one long-lived bash via the perf-wrapper hook.
New shell created **only when necessary**: none exists, previous died
(`exit N`, crash), timeout kill, or `CODEX_SHELL_FRESH=1`.

Measured here (0.25s simulated login profile): stock 255ms/call constant;
persistent 28ms/call steady-state (~9x; grows with real Hopper profile cost).

```bash
# deploy
DEST=/projects/kzhou6/czhai/agent-profiling/latency-opt
mkdir -p $DEST && cp -r scripts speculation $DEST/

# chain: perf wrapper stays outermost if you still want per-call perf of the
# CLIENT side; for the optimized-variant runs, point codex at the persistent
# wrapper directly:
export CODEX_TOOL_PERF_WRAPPER=$DEST/scripts/codex_persistent_shell_wrap.sh
export CODEX_SHELLD_SOCK=$RUN_DIR/shelld.sock       # one daemon per task run
export CODEX_SHELLD_LOGDIR=$RUN_DIR/shelld_logs     # commands.jsonl lands here
codex exec ...                                       # daemon auto-starts on first call

# teardown between tasks (or let the batch runner do it):
python3 -c "import socket;s=socket.socket(socket.AF_UNIX);s.connect('$CODEX_SHELLD_SOCK');s.sendall(b'{\"op\":\"shutdown\"}\n')"
```

Semantics preserved vs. stock: per-call cwd reset (`cd` does not leak),
exit codes, stdout/stderr separation, timeout kill. Semantics changed
(deliberately): env vars/functions persist across calls — that's the point.
Kill switch: `CODEX_SHELLD_BYPASS=1` reverts to stock exec; daemon
unreachable also falls back to stock automatically.

**Perf attribution note:** with a daemon, per-command `perf stat` on the
client no longer measures the command (it runs in the daemon's shell).
Replacement: the daemon logs per-command wall + CPU (utime+stime+cutime+cstime
deltas from /proc, includes reaped children) to `commands.jsonl`. For PMU
counters on the optimized variant, use whole-run `perf stat` on the daemon
pid tree, or interval mode `perf stat -p <bash_pid> -I 1000`.

## Optimization 2: gated speculative execution

Two components, exploiting the ~90% idle CPU your runs measured while
waiting on model inference:

**Gate** (`spec_gate.py`): benchmark prior first — HotpotQA/FreshQA/WebArena
refuse (your own data: HotpotQA has zero local shell commands; nothing to
speculate). SWE-bench/Terminal-Bench pass to per-task feature inspection
(git repo? python project? tests?) which emits an ordered action plan.

**Worker** (`speculative_worker.py`): runs the plan in parallel with the
agent, niced. Two speculation types with different commit paths:

- *Result speculation* (read-only: `git status`, `pytest --collect-only`,
  pre-running the failing suite, repo indexing): results cached with a
  workspace fingerprint; the daemon serves a hit instantly ONLY if the
  fingerprint still matches. Stale/wrong speculation → silently ignored,
  command runs for real. Misprediction cost ≈ 0.
- *State speculation* (side effects: dep installs): never touches the live
  env; builds a speculation venv and warms pip/npm/cargo caches so the
  agent's own later `pip install` completes in seconds. On Hopper this
  composes with the egress throttle: run the worker's fetch phase login-node
  side before dispatch.

```bash
# per-task launch (batch runner, right before codex exec):
export CODEX_SHELLD_SPEC=$RUN_DIR/spec_cache
python3 $DEST/speculation/spec_gate.py --benchmark swebench --workspace $WS \
  && nohup python3 $DEST/speculation/speculative_worker.py \
       --workspace $WS --cache-dir $CODEX_SHELLD_SPEC >$RUN_DIR/spec.log 2>&1 &
codex exec ...   # daemon picks up CODEX_SHELLD_SPEC automatically
```

Cache hits require the agent to issue the exact command string the worker
seeded; the seed list uses the canonical phrasings agents actually emit.
Next iteration: an LLM speculator (codex-low reading the task text) emitting
additional plan entries — execution/commit machinery unchanged. That's the
cost-aware speculative-actions design from the papers, with the safety
property that all speculation is either read-only-cached or sandboxed.

## Measurement design for the study

Three arms per benchmark, identical instances:
1. stock codex (+ perf wrapper) — baseline
2. + persistent shell — isolates orchestration-overhead savings
3. + speculation — isolates prep-time savings

Compare: wall time, tool-side time (commands.jsonl), cache hit rate
(`{"op":"stats"}`), CPU utilization (task-clock/wall should RISE for arm 3 —
idle cycles converted to useful prep).

## Known sharp edges

- `exec` in a tool command replaces the session shell → fifo never signals →
  request times out, session reset. Rare; acceptable.
- Concurrent tool calls serialize per session (lock). Codex classic path is
  sequential anyway; parallel-tool codex builds should use distinct
  `CODEX_SHELLD_KEY`s.
- The wrapper's python client adds ~25ms/call; if that matters, replace with
  a tiny C client or socat.
- Interactive/PTY-needing commands (top, editors) don't get a TTY — same as
  stock `bash -lc`, no regression.
- Fingerprint is a 2-level mtime scan excluding `.git`/caches: cheap but
  coarse. Deep edits below level 2 with unchanged parent mtimes could
  falsely validate — restrict caching to commands whose output you can
  tolerate being ~seconds stale, or swap in `git status --porcelain` hash
  for git workspaces.
