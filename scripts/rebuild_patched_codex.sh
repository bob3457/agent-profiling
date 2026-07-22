#!/usr/bin/env bash
# Rebuild the patched binary and verify the wrapper hook is compiled in.
set -euo pipefail

ROOT="${ROOT:-/projects/kzhou6/czhai/agent-profiling}"
AGENT_RS="${AGENT_RS:-$ROOT/agent-src/codex-rs}"

export CARGO_HOME="${CARGO_HOME:-$ROOT/.cargo}"
export RUSTUP_HOME="${RUSTUP_HOME:-$ROOT/.rustup}"
[ -f "$CARGO_HOME/env" ] && source "$CARGO_HOME/env"

cd "$AGENT_RS"
touch core/src/shell.rs
cargo fmt || true
env -u RUSTFLAGS cargo build --release --bin codex

export CODEX_SRC_BIN="$AGENT_RS/target/release/codex"

# Avoid `strings | grep -q` under pipefail (grep -q closes the pipe early
# and makes strings exit with SIGPIPE) -- redirect to a file first.
strings "$CODEX_SRC_BIN" > /tmp/codex_strings_check.$$.txt
if ! grep -q CODEX_TOOL_PERF_WRAPPER /tmp/codex_strings_check.$$.txt; then
    echo "ERROR: CODEX_TOOL_PERF_WRAPPER not found in binary strings" >&2
    rm -f /tmp/codex_strings_check.$$.txt
    exit 1
fi
rm -f /tmp/codex_strings_check.$$.txt
echo "Patched binary verified: $CODEX_SRC_BIN"
echo "Add to your shell: export CODEX_SRC_BIN=$CODEX_SRC_BIN"
