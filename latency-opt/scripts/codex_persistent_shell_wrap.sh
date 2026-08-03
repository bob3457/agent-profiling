#!/usr/bin/env bash
# codex_persistent_shell_wrap.sh — client side of the persistent-shell optimization.
#
# Uses the EXACT interception point you already have from Tejas's profiling
# patch: shell.rs prepends $CODEX_TOOL_PERF_WRAPPER to the tool argv, so codex
# invokes:   <this script> /bin/bash -lc "<command>"
#
# Instead of exec-ing a fresh bash (stock behavior), this script:
#   1. auto-starts the daemon on first use (flock-guarded, once per run),
#   2. sends the command to the persistent session over the unix socket,
#   3. replays stdout/stderr/exit code so codex can't tell the difference.
#
# A new shell is created only when necessary (daemon decides: none alive,
# previous killed/timeout, or CODEX_SHELL_FRESH=1).
#
# Env knobs:
#   CODEX_SHELLD_SOCK     socket path        (default /tmp/codex_shelld.$UID/sock)
#   CODEX_SHELLD_PY       daemon script path (default: alongside this script)
#   CODEX_SHELLD_PYTHON   interpreter for daemon + socket client (default
#                         python3; pin this in containers so the daemon does
#                         not run under a per-instance testbed conda python)
#   CODEX_SHELLD_LOGDIR   commands.jsonl dir (default /tmp/codex_shelld.$UID/logs)
#   CODEX_SHELLD_SPEC     speculation cache dir (optional)
#   CODEX_SHELL_FRESH=1   force a fresh session for this one call
#   CODEX_SHELLD_BYPASS=1 fall through to stock exec (kill switch)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOCK="${CODEX_SHELLD_SOCK:-/tmp/codex_shelld.$(id -u)/sock}"
DAEMON="${CODEX_SHELLD_PY:-$SCRIPT_DIR/shell_sessiond.py}"
PYBIN="${CODEX_SHELLD_PYTHON:-python3}"
LOGDIR="${CODEX_SHELLD_LOGDIR:-/tmp/codex_shelld.$(id -u)/logs}"
SPEC="${CODEX_SHELLD_SPEC:-}"

# ---- 0. bypass / non-shell argv: behave exactly like stock ----------------
if [[ "${CODEX_SHELLD_BYPASS:-0}" == "1" || $# -lt 3 ]]; then
  exec "$@"
fi
# Expect: <shell> -lc|-c <command...>. Anything else -> stock.
case "$2" in
  -lc|-c) : ;;
  *) exec "$@" ;;
esac
CMD="$3"

# ---- 1. ensure daemon is up (once; flock prevents races) -------------------
# stale socket file (daemon gone) must not block a restart
if [[ -S "$SOCK" ]] && ! "$PYBIN" -c "import socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(2);s.connect(sys.argv[1])" "$SOCK" 2>/dev/null; then
  rm -f "$SOCK"
fi
if [[ ! -S "$SOCK" ]]; then
  mkdir -p "$(dirname "$SOCK")" "$LOGDIR"
  (
    flock -n 9 || exit 0
    [[ -S "$SOCK" ]] && exit 0
    nohup "$PYBIN" "$DAEMON" --socket "$SOCK" --log-dir "$LOGDIR" \
        ${SPEC:+--spec-cache "$SPEC"} \
        >> "$LOGDIR/daemon.stderr" 2>&1 &
  ) 9>"$SOCK.lock"
  for _ in $(seq 1 50); do [[ -S "$SOCK" ]] && break; sleep 0.1; done
fi

# ---- 2. send request, replay result ----------------------------------------
# python3 used as the socket client to avoid a socat/nc dependency on Hopper.
# Fallback signaling: exit code alone is ambiguous (a real command can exit
# 97), so "fall back to stock" is a marker file + 97. The marker is written
# ONLY for failures before the request was fully sent; a failure after send
# must NOT re-run the command (the daemon may already have executed it), so
# it surfaces as a plain error instead.
FAILMARK=$(mktemp "${TMPDIR:-/tmp}/shelld_fail.XXXXXX")
rm -f "$FAILMARK"
export _SHELLD_CMD="$CMD" _SHELLD_SOCK="$SOCK" _SHELLD_CWD="$PWD"
export _SHELLD_FRESH="${CODEX_SHELL_FRESH:-0}" _SHELLD_FAILMARK="$FAILMARK"
"$PYBIN" - <<'PYEOF'
import json, os, socket, sys
req = {"cmd": os.environ["_SHELLD_CMD"],
       "cwd": os.environ["_SHELLD_CWD"],
       "key": os.environ.get("CODEX_SHELLD_KEY", "default"),
       "fresh": os.environ["_SHELLD_FRESH"] == "1"}
sent = False
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(7200)
    s.connect(os.environ["_SHELLD_SOCK"])
    s.sendall((json.dumps(req) + "\n").encode())
    sent = True
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
    res = json.loads(buf)
except Exception as e:
    if not sent:  # daemon unreachable -> caller falls back to stock
        open(os.environ["_SHELLD_FAILMARK"], "w").close()
        sys.exit(97)
    # request delivered but response lost: command may have run; do NOT
    # signal fallback (a re-run would double side effects)
    sys.stderr.write(f"shelld: response lost after send: {e}\n")
    sys.exit(1)
sys.stdout.write(res.get("stdout", ""))
sys.stderr.write(res.get("stderr", ""))
sys.exit(int(res.get("exit", 1)))
PYEOF
rc=$?
# ---- 3. fallback: daemon unreachable -> stock fresh-process behavior -------
if [[ $rc -eq 97 && -e "$FAILMARK" ]]; then
  rm -f "$FAILMARK"
  exec "$@"
fi
rm -f "$FAILMARK"
exit $rc
