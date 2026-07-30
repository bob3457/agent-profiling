#!/usr/bin/env python3
"""Patch run_option_b.sh: run the edit-respec watcher INSIDE the agent
container (build 6.2).

Why: /testbed is baked into the SIF and every `apptainer exec
--writable-tmpfs` gets a PRIVATE tmpfs overlay. A sibling respec container
watches its own pristine /testbed where, by construction, nothing ever
changes (proven: run 132352 -- 113s of silence, one teardown-churn bump).
The only namespace that sees the agent's edits is the agent's own container.

What this does:
  1. Removes the standalone respec `apptainer exec` block and the host-side
     RESPEC_PID kill line, if present (migration from build 6.1).
  2. Rewrites the agent exec's bash -c to: launch edit_respec.py in the
     background BEFORE codex (gated on CODEX_SHELLD_SPEC so base mode is
     untouched), run codex, kill the watcher, exit with codex's status.
     edit_respec kills its own in-flight test child on SIGTERM, so no
     orphan holds the overlay past teardown (shared host PID namespace).
  3. Adds -B "$RESULTS":/spec_results:ro to the agent exec so the watcher
     can parse candidates from the worker's spec.log (distinct mountpoint;
     does not shadow the /spec_cache and /spec_logs binds).

Anchors assert against the exact agent-exec block inspected on 2026-07-30;
fails loudly, writes nothing, on drift. Idempotent (marker: RESPEC_IN).

Usage:  python3 patch_harness.py <harness.sh> [more.sh ...]
"""
import re
import sys
from pathlib import Path


def patch(path: Path) -> bool:
    t = path.read_text()
    if "RESPEC_IN" in t:
        print(f"already patched: {path}")
        return True

    # ---- 1. remove the build-6.1 sibling-container respec block ------------
    lines = t.split("\n")
    hit = next((i for i, ln in enumerate(lines) if "edit_respec.py" in ln
                and "apptainer" not in ln), None)
    if hit is not None:
        start = next((i for i in range(hit, -1, -1)
                      if "apptainer exec" in lines[i]), None)
        end = next((i for i in range(hit, min(hit + 8, len(lines)))
                    if re.match(r"\s*RESPEC_PID=\$!\s*$", lines[i])), None)
        if start is None or end is None or \
                any("speculative_worker" in lines[i]
                    for i in range(start, end + 1)):
            print(f"FAIL({path.name}): found edit_respec.py but could not "
                  "safely delimit its exec block -- refusing to guess")
            return False
        del lines[start:end + 1]
        t = "\n".join(lines)
        print("removed: sibling-container respec block (wrong namespace)")
    t2 = re.sub(r"^\[ -n \"\$\{RESPEC_PID:-\}\" \][^\n]*\n", "", t, count=1,
                flags=re.M)
    if t2 != t:
        print("removed: host-side RESPEC_PID kill line")
    t = t2

    # ---- 2. rewrite the agent exec -----------------------------------------
    anchor = '''  apptainer exec --writable-tmpfs "${BINDS[@]}" "$SIF" bash -c "$ACT $AGENT_ENV
    cd /testbed && /usr/local/bin/codex exec --json --skip-git-repo-check \\
      --sandbox danger-full-access \\"\\$0\\" \\
      > /spec_logs/stdout.jsonl 2> /spec_logs/stderr.log" "$PROMPT"'''
    if anchor not in t:
        print(f"FAIL({path.name}): agent exec block not found verbatim -- "
              "paste the current block and I will re-anchor")
        return False

    replacement = '''  apptainer exec --writable-tmpfs "${BINDS[@]}" -B "$RESULTS":/spec_results:ro "$SIF" bash -c "$ACT $AGENT_ENV
    RESPEC_IN=''
    if [ -n \\"\\${CODEX_SHELLD_SPEC:-}\\" ]; then
      /opt/toolpy/bin/python3 /opt/latency-opt/speculation/edit_respec.py \\
        --workspace /testbed --cache-dir /spec_cache \\
        --spec-log /spec_results/spec.log > /spec_logs/respec.log 2>&1 &
      RESPEC_IN=\\$!
    fi
    cd /testbed && /usr/local/bin/codex exec --json --skip-git-repo-check \\
      --sandbox danger-full-access \\"\\$0\\" \\
      > /spec_logs/stdout.jsonl 2> /spec_logs/stderr.log
    rc=\\$?
    [ -n \\"\\$RESPEC_IN\\" ] && kill \\$RESPEC_IN 2>/dev/null
    exit \\$rc" "$PROMPT"'''
    t = t.replace(anchor, replacement, 1)

    path.write_text(t)
    print(f"patched OK: {path}")
    print("  watcher now runs INSIDE the agent container (spec mode only),")
    print("  killed in-container after codex; codex exit status preserved")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ok = all(patch(Path(p)) for p in sys.argv[1:])
    sys.exit(0 if ok else 1)
