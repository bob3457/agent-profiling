#!/usr/bin/env bash
# run_option_b.sh <base|spec> <instance_id> — agent-in-container SWE-bench run,
# without (base) or with (spec) the speculative system. This is the experiment
# that prices speculation in real seconds: inside the SIF the testbed env
# exists, so test runs cost their true 5-30s, and a served cache hit saves
# exactly that.
#
# Layout per run:
#   agent container : --writable-tmpfs, cwd /testbed, codex + wrapper + daemon
#   worker container: separate instance, own tmpfs overlay (pristine /testbed,
#                     cannot corrupt the agent's copy), shares host cache dir
#   LLM predictor   : host side, reads the bare extracted workspace
#
# Env (defaults match your layout):
#   SIF_DIR, ROOT, CODEX_SRC_BIN, WS_ROOT (bare workspaces, for the predictor),
#   PERF_EVENTS (host whole-run perf), RESULTS_BASE
set -uo pipefail
MODE=${1:?usage: run_option_b.sh base|spec <instance_id>}
IID=${2:?instance id}
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
SIF_DIR=${SIF_DIR:-/scratch/czhai/sifs-arm64}
SIF=$SIF_DIR/$IID.sif
CODEX_BIN=${CODEX_SRC_BIN:-$ROOT/agent-src/codex-rs/target-aarch64/release/codex}
WS_ROOT=${WS_ROOT:-/scratch/czhai/latency-eval/workspaces}
OPT=$ROOT/latency-opt
PROMPT_FILE=$ROOT/prompts/swe_$IID.txt
RESULTS=${RESULTS_BASE:-/scratch/czhai/latency-eval/optionb}/${MODE}.$IID.$(date +%H%M%S)
PERF_EVENTS=${PERF_EVENTS:-task-clock,context-switches,page-faults}

[ -f "$SIF" ] || { echo "no SIF: $SIF"; exit 1; }
[ -f "$PROMPT_FILE" ] || { echo "no prompt: $PROMPT_FILE"; exit 1; }
mkdir -p "$RESULTS"/{cache,logs}
echo "mode=$MODE instance=$IID results=$RESULTS"

# Common binds: optimization scripts, codex binary + auth, shared cache/logs
# host-glibc shim: image glibc is older than the build host's; run codex
# through the host loader with host libs, leaving container libs untouched
cat > "$RESULTS/codex" <<'SHIM'
#!/bin/bash
exec /opt/hostlibs/ld-linux-aarch64.so.1 --library-path /opt/hostlibs /usr/local/bin/codex.real "$@"
SHIM
chmod +x "$RESULTS/codex"
BINDS=(-B /projects/kzhou6/czhai/tools/toolpy:/opt/toolpy -B "$OPT:/opt/latency-opt" -B "$CODEX_BIN:/usr/local/bin/codex.real"
       -B "$RESULTS/codex:/usr/local/bin/codex" -B /lib/aarch64-linux-gnu:/opt/hostlibs
       -B "$HOME/.codex:/root/.codex" -B "$RESULTS/cache:/spec_cache"
       -B "$RESULTS/logs:/spec_logs")
# in-container env activation prefix used by every bash we start
ACT='export PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:$PATH; export HOME=/root;'

# ---------------------------------------------------------------- speculation
WORKER_PID=""
if [[ $MODE == spec ]]; then
  cp "$PROMPT_FILE" "$RESULTS/logs/problem.txt"
  apptainer exec --writable-tmpfs "${BINDS[@]}" "$SIF" bash -c "$ACT
    export SPEC_LLM_BIN=/usr/local/bin/codex
    cd /testbed && /opt/toolpy/bin/python3 -u /opt/latency-opt/speculation/speculative_worker.py \
      --workspace /testbed --cache-dir /spec_cache --benchmark swebench \
      --problem-statement /spec_logs/problem.txt \
      --predictor both --timeout-per-cmd 300" \
    > "$RESULTS/spec.log" 2>&1 &
  WORKER_PID=$!
fi

# --------------------------------------------------------------------- agent
AGENT_ENV=""
if [[ $MODE == spec ]]; then
  RUNTAG=$(date +%s).$$
  AGENT_ENV="export CODEX_TOOL_PERF_WRAPPER=/opt/latency-opt/scripts/codex_persistent_shell_wrap.sh;
             export CODEX_SHELLD_SOCK=/tmp/shelld.$RUNTAG/sock;
             export CODEX_SHELLD_LOGDIR=/spec_logs/shelld;
             export CODEX_SHELLD_SPEC=/spec_cache;
             export CODEX_SHELLD_PYTHON=/opt/toolpy/bin/python3;"
fi
PROMPT=$(cat "$PROMPT_FILE")

/usr/bin/time -v -o "$RESULTS/time.txt" \
  perf stat -e "$PERF_EVENTS" -o "$RESULTS/perf_stat.txt" -- \
  apptainer exec --writable-tmpfs "${BINDS[@]}" -B "$RESULTS":/spec_results:ro "$SIF" bash -c "$ACT $AGENT_ENV
    RESPEC_IN=''
    if [ -n \"\${CODEX_SHELLD_SPEC:-}\" ]; then
      /opt/toolpy/bin/python3 -u /opt/latency-opt/speculation/edit_respec.py \
        --workspace /testbed --cache-dir /spec_cache \
        --spec-log /spec_results/spec.log --commands-log /spec_logs/shelld/commands.jsonl --agent-stream /spec_logs/stdout.jsonl > /spec_logs/respec.log 2>&1 &
      RESPEC_IN=\$!
    fi
    cd /testbed && /usr/local/bin/codex exec --json --skip-git-repo-check \
      --sandbox danger-full-access \"\$0\" \
      > /spec_logs/stdout.jsonl 2> /spec_logs/stderr.log
    rc=\$?
    [ -n \"\$RESPEC_IN\" ] && kill \$RESPEC_IN 2>/dev/null
    exit \$rc" "$PROMPT"
echo $? > "$RESULTS/exit_code"

[[ -n "$WORKER_PID" ]] && { pkill -P $WORKER_PID 2>/dev/null || true; kill $WORKER_PID 2>/dev/null || true; }
# shut down the in-container daemon: host PID namespace is shared, so it
# would otherwise outlive the container (host /tmp socket + old mount ns)
if [[ $MODE == spec ]]; then
  python3 - "/tmp/shelld.$RUNTAG/sock" <<'PY' 2>/dev/null || true
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3)
s.connect(sys.argv[1]); s.sendall(b'{"op":"shutdown"}\n')
PY
  rm -rf "/tmp/shelld.$RUNTAG"
fi

# ------------------------------------------------------------------- summary
python3 - "$RESULTS" <<'PY'
import json, re, sys
from pathlib import Path
R = Path(sys.argv[1])
out = {"mode": R.name}
t = R / "time.txt"
if t.exists():
    m = re.search(r"Elapsed.*: (.*)", t.read_text())
    out["wall"] = m.group(1) if m else "?"
j = R / "logs" / "shelld" / "commands.jsonl"
if j.exists():
    cmds = [json.loads(l) for l in j.open()]
    out["n_cmds"] = len(cmds)
    out["cache_hits"] = sum(c.get("cached", False) for c in cmds)
    out["near_misses"] = sum(1 for c in cmds if c.get("near_miss"))
    out["tool_wall_s"] = round(sum(c.get("wall_s") or 0 for c in cmds), 2)
    # the money number: what did served commands cost the WORKER to pre-run?
    hits = [c["cmd"] for c in cmds if c.get("cached")]
    out["served_cmds"] = hits
print(json.dumps(out, indent=2))
PY
echo "results -> $RESULTS"

