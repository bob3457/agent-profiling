#!/usr/bin/env bash
set -euo pipefail
ROOT="${ROOT:-/projects/kzhou6/czhai/agent-profiling}"

mkdir -p "$HOME/.codex/hooks"

# You already have Codex config from Agent-Bench work: back up any existing
# hooks.json before overwriting.
if [ -f "$HOME/.codex/hooks.json" ]; then
    cp "$HOME/.codex/hooks.json" "$HOME/.codex/hooks.json.bak.$(date +%Y%m%d%H%M%S)"
    echo "Backed up existing hooks.json"
fi

cp "$ROOT/codex_hooks/profile_hook.py" "$HOME/.codex/hooks/profile_hook.py"
cp "$ROOT/codex_hooks/hooks.json" "$HOME/.codex/hooks.json"
chmod +x "$HOME/.codex/hooks/profile_hook.py"

echo "Installed hooks:"
ls -lh "$HOME/.codex/hooks/profile_hook.py" "$HOME/.codex/hooks.json"
echo "If the agent asks to trust hooks, review them with /hooks."
