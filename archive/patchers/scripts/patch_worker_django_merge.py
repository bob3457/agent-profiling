#!/usr/bin/env python3
"""patch_worker_django_merge.py — fix the granularity collapse in the
worker's LLM->family merge.

Bug (24 granularity misses in the 10-repo audit): the merge did
`t.split(".")[0]`, collapsing the model's fine-grained django predictions
(dbshell.test_postgresql -> dbshell) — discarding exactly the granularity
the agent then queries. Fix: keep the dotted label AND its top-level parent;
act_django_targeted pre-runs dotted labels directly (its per-module
expansion just skips them via the is_dir() guard).

Idempotent: safe to run twice. Usage (repo root):
    python3 scripts/patch_worker_django_merge.py
"""
from pathlib import Path

W = Path("latency-opt/speculation/speculative_worker.py")
OLD = '''            elif pc["family"] == "django":
                ctx.setdefault("django_labels", [])
                ctx["django_labels"] += [t.split(".")[0] for t in pc["targets"]
                                         if t.split(".")[0] not in ctx["django_labels"]]'''
NEW = '''            elif pc["family"] == "django":
                ctx.setdefault("django_labels", [])
                for t in pc["targets"]:
                    # keep the model's full granularity AND the parent label
                    for lab in (t, t.split(".")[0]):
                        if lab not in ctx["django_labels"]:
                            ctx["django_labels"].append(lab)'''

src = W.read_text()
if NEW in src:
    print("[skip] already patched")
elif OLD in src:
    W.write_text(src.replace(OLD, NEW, 1))
    print(f"[ok] patched {W}")
else:
    raise SystemExit(f"[FAIL] anchor not found in {W} — file drifted, patch manually")
