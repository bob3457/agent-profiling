#!/usr/bin/env bash
# preflight_option_b.sh — verify container assumptions for agent-in-container
# speculation runs. Run on the GH200 node. Costs nothing (no API calls).
#
#   SIF=/scratch/czhai/sifs-arm64/astropy__astropy-12907.sif bash preflight_option_b.sh
set -u
SIF=${SIF:?set SIF to one instance SIF}
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
CODEX_BIN=${CODEX_SRC_BIN:-$ROOT/agent-src/codex-rs/target-aarch64/release/codex}
pass=0; fail=0
ck() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1   [$2]"; fail=$((fail+1)); fi; }

echo "== host side =="
ck "SIF exists"                "[ -f $SIF ]"
ck "codex binary exists"       "[ -x $CODEX_BIN ]"
ck "codex auth present"        "[ -f $HOME/.codex/auth.json ]"
ck "latency-opt deployed"      "[ -x $ROOT/latency-opt/scripts/codex_persistent_shell_wrap.sh ]"

echo "== container: environment =="
ck "apptainer exec works"      "apptainer exec $SIF true"
ck "/testbed exists"           "apptainer exec $SIF test -d /testbed"
ck "testbed conda env"         "apptainer exec $SIF test -x /opt/miniconda3/envs/testbed/bin/python"
ck "bash present"              "apptainer exec $SIF bash -c true"
ck "writable tmpfs"            "apptainer exec --writable-tmpfs $SIF touch /testbed/.pf_probe"

echo "== container: tests actually run (the whole point) =="
apptainer exec $SIF bash -c '
  source /opt/miniconda3/bin/activate testbed 2>/dev/null || export PATH=/opt/miniconda3/envs/testbed/bin:$PATH
  cd /testbed && timeout 120 python -m pytest --collect-only -q 2>/dev/null | tail -2' \
  && echo "PASS  pytest collects in testbed env" || echo "FAIL  pytest collect"

echo "== container: daemon prerequisites =="
ck "python3 in container"      "apptainer exec $SIF bash -c 'command -v python3 || test -x /opt/miniconda3/bin/python3'"
ck "mkfifo works on tmpfs"     "apptainer exec --writable-tmpfs $SIF bash -c 'mkfifo /tmp/.pf_fifo && rm /tmp/.pf_fifo'"
ck "host bind-mount works"     "apptainer exec -B $ROOT/latency-opt:/opt/latency-opt $SIF test -x /opt/latency-opt/scripts/codex_persistent_shell_wrap.sh"

echo "== container: codex viability (bind binary + auth, check API egress) =="
ck "codex runs in container"   "apptainer exec -B $CODEX_BIN:/usr/local/bin/codex $SIF /usr/local/bin/codex --version"
apptainer exec $SIF bash -c 'timeout 10 python3 -c "
import urllib.request, ssl
try:
    urllib.request.urlopen(\"https://api.openai.com/v1/models\", timeout=8)
except Exception as e:
    # 401 = reachable+TLS ok (auth expected to fail); anything TLS/DNS = real problem
    import urllib.error
    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
        print(\"reachable\"); raise SystemExit(0)
    raise
"' && echo "PASS  API reachable + CA bundle ok from container" \
   || echo "FAIL  API egress or CA bundle (remember seed_ca_bundle from terminal-bench work)"

echo
echo "passed=$pass failed=$fail — paste this whole output"
