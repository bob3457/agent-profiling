#!/usr/bin/env python3
"""task_gate.py — benchmark-agnostic content gate for speculation.

Decides whether to spend the up-front speculation budget (the worker's LLM
predictor call) on a task, from the problem statement alone -- no benchmark
names, no format assumptions. This is stage 1 of a two-stage gate; stage 2
is behavioral and free: the watcher arms itself only when the trajectory
actually shows edits or verify-class commands, so a wrong GO here wastes at
most one predictor call and a wrong NOGO is corrected by observed behavior.

Prints "GO" or "NOGO" plus a reason; exit 0 = GO. Fails open (GO) on any
error, since the downstream cost is bounded and behavioral.

Usage:  task_gate.py <problem_statement_file>
Env:    SPEC_LLM_BIN (default: codex)
"""
import os
import subprocess
import sys

PROMPT = """Answer with exactly one word, YES or NO.

A "speculation-eligible" task is one where an agent will modify files or an
environment inside a shell workspace and then run commands (tests, builds,
checks) to verify its work. A task that is answered purely by reasoning,
web search, or writing text -- with no workspace to mutate and verify -- is
not eligible.

Is the following task speculation-eligible?

---
{stmt}
---
One word, YES or NO:"""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        stmt = open(sys.argv[1], errors="replace").read()[:6000]
        llm = os.environ.get("SPEC_LLM_BIN", "codex")
        r = subprocess.run(
            [llm, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             PROMPT.format(stmt=stmt)],
            capture_output=True, text=True, timeout=60)
        ans = (r.stdout or "").strip().splitlines()
        verdict = next((ln.strip().upper() for ln in reversed(ans)
                        if ln.strip().upper() in ("YES", "NO")), None)
        if verdict == "NO":
            print("NOGO content-gate: no mutate-and-verify loop expected")
            sys.exit(1)
        print(f"GO content-gate: {'verified eligible' if verdict else 'indeterminate, failing open'}")
        sys.exit(0)
    except Exception as e:  # fail open by policy
        print(f"GO content-gate error ({e}), failing open")
        sys.exit(0)


if __name__ == "__main__":
    main()
