#!/usr/bin/env bash
# Adapted from the Codex CPU Deep-Dive Reproduction Guide for GMU Hopper.
# Run on the LOGIN NODE (needs network for rustup, crates.io, pip).
set -euo pipefail

ROOT="${ROOT:-/projects/kzhou6/czhai/agent-profiling}"

# Set this to the fork's git URL (ask Tejas). Falls back to upstream Codex.
AGENT_REPO_URL="${AGENT_REPO_URL:-https://github.com/openai/codex.git}"
AGENT_SRC="${AGENT_SRC:-$ROOT/agent-src}"
# Rust workspace inside the repo; upstream Codex uses codex-rs/
AGENT_RS="${AGENT_RS:-$AGENT_SRC/codex-rs}"

mkdir -p "$ROOT"
cd "$ROOT"

# Keep the Rust toolchain out of $HOME (quota) and on shared storage.
export CARGO_HOME="${CARGO_HOME:-$ROOT/.cargo}"
export RUSTUP_HOME="${RUSTUP_HOME:-$ROOT/.rustup}"

python3 -m pip install -U pip
python3 -m pip install -U pandas tabulate pyarrow tiktoken

if ! command -v cargo >/dev/null 2>&1 && [ ! -x "$CARGO_HOME/bin/cargo" ]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
fi
source "$CARGO_HOME/env"

# cargo needs a C linker; on Hopper load gcc if cc is missing.
if ! command -v cc >/dev/null 2>&1; then
    echo "NOTE: no 'cc' found. Run 'module load gcc' and re-run this script." >&2
    exit 1
fi

rustup component add rustfmt || true
rustup component add clippy || true

if [ ! -d "$AGENT_SRC/.git" ]; then
    git clone "$AGENT_REPO_URL" "$AGENT_SRC"
fi

if [ ! -d "$AGENT_RS" ]; then
    echo "ERROR: expected Rust workspace at $AGENT_RS not found." >&2
    echo "Set AGENT_RS to the fork's Rust workspace directory and re-run." >&2
    exit 1
fi

cd "$AGENT_RS"
env -u RUSTFLAGS cargo build --release --bin codex

export CODEX_SRC_BIN="$AGENT_RS/target/release/codex"
echo "CODEX_SRC_BIN=$CODEX_SRC_BIN"
"$CODEX_SRC_BIN" --help >/dev/null || true
echo
echo "Build OK. Next: cd $AGENT_RS && python $ROOT/scripts/apply_codex_shell_wrapper_patch.py"
