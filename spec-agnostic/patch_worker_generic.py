#!/usr/bin/env python3
"""patch_worker_generic.py — task-agnostic candidate pipeline for
speculative_worker.py. Idempotent; verbatim anchors; refuses on drift.

Three changes:
  1. act_workspace_recon: the universal first-moves action — ls variants,
     bounded find, cat of small non-binary top-level files. No benchmark
     assumptions; exactly what agents do first on ANY filesystem task.
  2. LLM-direct path: predicted commands that are NOT pytest/django family
     are no longer dropped. Whole commands classifying TIER0 are pre-run and
     cached verbatim; compound predictions are split (spec_compound) and
     their TIER0 parts pre-run individually so the daemon's prefix-serve can
     assemble them. TIER1 is deliberately excluded here: the worker has no
     undo ledger (edit_respec does).
  3. Recon joins every plan (env SPEC_RECON=0 disables), and the
     upstream-GO fallback plan starts with it.

Run:  python3 patch_worker_generic.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
TARGET = ROOT / "latency-opt/speculation/speculative_worker.py"
src = TARGET.read_text()
orig = src

MARK = "act_workspace_recon"   # presence of edit 1


def apply(name, old, new):
    global src
    if new in src:
        print(f"  = {name}: already applied")
        return
    assert old in src, f"ANCHOR DRIFT ({name}): expected bytes not found"
    assert src.count(old) == 1, f"ANCHOR AMBIGUOUS ({name})"
    src = src.replace(old, new)
    print(f"  + {name}")


# ---- 1. new actions, inserted right before the ACTIONS registry -----------
NEW_FUNCS = '''
# ---------------------------------------------------- task-agnostic actions
RECON_MAX_FILES = int(os.environ.get("SPEC_RECON_MAX_FILES", "15"))
RECON_MAX_BYTES = int(os.environ.get("SPEC_RECON_MAX_BYTES", str(32 * 1024)))


def _is_texty(p: Path) -> bool:
    try:
        return b"\\0" not in p.open("rb").read(1024)
    except OSError:
        return False


def act_workspace_recon(ws, ctx):
    """Universal first moves: what every agent does on every filesystem task,
    regardless of domain. All TIER0 by construction."""
    cmds = ["ls", "ls -la", "pwd",
            "find . -type f -not -path '*/.git/*' | head -100"]
    n = 0
    for p in sorted(ws.iterdir()):
        if p.name.startswith(".") or p.name in ("__pycache__", "node_modules"):
            continue
        if p.is_dir():
            cmds.append(f"ls {p.name}")
            cmds.append(f"ls -la {p.name}")
        elif p.is_file() and n < RECON_MAX_FILES:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if 0 < size <= RECON_MAX_BYTES and _is_texty(p):
                cmds.append(f"cat {p.name}")
                cmds.append(f"wc -l {p.name}")
                n += 1
    return [(c, True) for c in cmds]


def _collect_direct(ctx, cmd):
    """Route a non-family LLM prediction through the tier policy. Whole
    TIER0 commands and TIER0 parts of compound predictions become direct
    pre-run candidates; everything else is dropped (and logged)."""
    from spec_tiers import classify, TIER0
    from spec_compound import split_for_serve, fold_cd_serve
    seen = ctx.setdefault("direct_seen", set())
    out = ctx.setdefault("direct_cmds", [])

    def add(c):
        if c not in seen:
            seen.add(c)
            out.append(c)

    if classify(cmd) == TIER0:
        add(cmd)
        return
    parts = split_for_serve(cmd)
    if parts and len(parts) > 1:
        parts, _cwd = fold_cd_serve(parts, ".")
        kept = 0
        for text, _stop, srv in parts:
            if srv and classify(text) == TIER0:
                add(text)
                kept += 1
        if kept:
            print(f"[spec] llm direct (compound): kept {kept}/{len(parts)} "
                  f"parts of {cmd!r}")
            return
    print(f"[spec] llm direct: dropped (tier policy) {cmd!r}")


def act_llm_direct(ws, ctx):
    return [(c, True) for c in ctx.get("direct_cmds", [])]


'''
apply("recon+direct actions",
      "ACTIONS = {\n    \"git_status\": act_git_status,",
      NEW_FUNCS + "ACTIONS = {\n    \"git_status\": act_git_status,")

# ---- registry entries -------------------------------------------------------
apply("registry",
      '    "django_targeted": act_django_targeted,\n}',
      '    "django_targeted": act_django_targeted,\n'
      '    "workspace_recon": act_workspace_recon,\n'
      '    "llm_direct": act_llm_direct,\n}')

# ---- 2. LLM merge loop: stop dropping non-family predictions ----------------
apply("llm merge",
      """        for c in llm_cmds:
            pc = parse_command(c)
            if not pc:
                continue""",
      """        for c in llm_cmds:
            pc = parse_command(c)
            if not pc:
                _collect_direct(ctx, c)
                continue""")

apply("llm plan append",
      """                if "django_targeted" not in plan:
                    plan.append("django_targeted")""",
      """                if "django_targeted" not in plan:
                    plan.append("django_targeted")
        if ctx.get("direct_cmds") and "llm_direct" not in plan:
            plan.append("llm_direct")
            print(f"[spec] llm direct: {len(ctx['direct_cmds'])} "
                  f"tier0 candidate(s) queued")""")

# ---- 3a. upstream-GO fallback starts with recon ------------------------------
apply("GO fallback",
      '                plan = ["repo_index"]',
      '                plan = ["workspace_recon", "repo_index"]')

# ---- 3b. recon joins every gate-approved plan --------------------------------
apply("recon everywhere",
      """        if not getattr(d, "per_test_ids", True):
            os.environ.setdefault("SPEC_MAX_TEST_IDS", "0")
            print("[spec] gate: file-level granularity only (prediction confidence low)")""",
      """        if not getattr(d, "per_test_ids", True):
            os.environ.setdefault("SPEC_MAX_TEST_IDS", "0")
            print("[spec] gate: file-level granularity only (prediction confidence low)")
        if os.environ.get("SPEC_RECON", "1") != "0" and \\
                "workspace_recon" not in plan:
            plan.insert(0, "workspace_recon")""")

if src != orig:
    TARGET.write_text(src)
    print(f"wrote {TARGET}")
else:
    print("no changes (all edits already present)")
