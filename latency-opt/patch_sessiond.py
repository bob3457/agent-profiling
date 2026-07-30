#!/usr/bin/env python3
"""Patch shell_sessiond.py for generation-aware, observable serving.

Adds two things to the daemon's spec-cache serve path:

  1. Generation-stamped entries (written by speculation/edit_respec.py)
     validate against the GENERATION file in the cache dir: an O(1) read,
     no scanning on the serve path. The watcher's deep scan owns edit
     detection. Entries without the field keep the legacy
     workspace_fingerprint check, so the existing worker is untouched.
  2. EVERY serve decision is logged with a reason -- served /
     stale_generation / stale_fingerprint / generation_file_missing /
     no_entry -- to <cache_dir>/serve_decisions.jsonl. This is the
     observability whose absence cost us a whole diagnostic round-trip.

Anchors assert against the exact code inspected on 2026-07-28; if the file
has drifted, this fails loudly instead of guessing.

Usage:  python3 patch_sessiond.py [path-to-shell_sessiond.py]
        (default: $ROOT/latency-opt/scripts/shell_sessiond.py)
Idempotent: re-running on a patched file is a no-op.
"""
import os
import sys
from pathlib import Path

DEFAULT = os.path.expandvars(
    "$ROOT/latency-opt/scripts/shell_sessiond.py")
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
t = path.read_text()

if "_spec_entry_invalid" in t:
    print(f"already patched: {path}")
    sys.exit(0)

# ---- ensure time is imported (serve-decision records carry ts) -------------
if "\nimport time" not in t and "import time\n" not in t.split("\n\n")[0]:
    # insert after the first import line
    lines = t.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(("import ", "from ")):
            lines.insert(i + 1, "import time")
            break
    t = "\n".join(lines)

# ---- 1. helpers, inserted just above the spec cache section ----------------
anchor_a = (
    "# ---------------------------------------------------------------- spec cache\n"
    "def spec_cache_lookup(cache_dir: str, cmd: str, cwd: str):"
)
assert anchor_a in t, "ANCHOR A drifted: spec cache section header not found"

helpers = '''\
# ------------------------------------------------- spec cache: serve policy
def _current_generation(cache_dir: str):
    try:
        return (Path(cache_dir) / "GENERATION").read_text().strip()
    except OSError:
        return None


def _spec_entry_invalid(cache_dir: str, entry: dict, cwd: str):
    """Return None if servable, else a reason string.

    Generation-stamped entries (edit_respec.py) validate against the
    GENERATION file: O(1) read, sees in-place edits at any depth because the
    watcher's deep scan owns detection. Legacy entries fall back to the
    top-2-level fingerprint (blind below depth 2, oversensitive to top-level
    churn -- kept only for compatibility with pre-edit worker entries)."""
    gen = entry.get("generation")
    if gen is not None:
        cur = _current_generation(cache_dir)
        if cur is None:
            return "generation_file_missing"
        return None if gen == cur else f"stale_generation({gen}!={cur})"
    fp = entry.get("workspace_fingerprint")
    if fp and fp != workspace_fingerprint(cwd):
        return "stale_fingerprint"
    return None


def _log_serve_decision(cache_dir: str, cmd: str, key, decision: str, entry):
    try:
        rec = {"ts": time.time(), "cmd": cmd[:300],
               "key": ("exact" if key and not str(key).startswith("fam_")
                       else key), "decision": decision}
        if entry is not None:
            rec["entry_cmd"] = entry.get("cmd", "")[:300]
            rec["entry_exit"] = entry.get("exit")
            rec["entry_gen"] = entry.get("generation")
            rec["entry_age_s"] = round(
                time.time() - entry.get("speculated_at", time.time()), 1)
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\\n")
    except OSError:
        pass


'''
t = t.replace(anchor_a, helpers + anchor_a, 1)

# ---- 2. validation loop: reason codes instead of silent continue -----------
anchor_b = '''        fp = entry.get("workspace_fingerprint")
        if fp and fp != workspace_fingerprint(cwd):
            continue  # workspace changed since speculation; stale
        return entry
    return None'''
assert anchor_b in t, "ANCHOR B drifted: validation block not found verbatim"

replacement_b = '''        reason = _spec_entry_invalid(cache_dir, entry, cwd)
        if reason:
            _log_serve_decision(cache_dir, cmd, key, reason, entry)
            continue
        _log_serve_decision(cache_dir, cmd, key, "served", entry)
        return entry
    if len(keys) > 1:  # a family key existed => speculation-relevant miss
        _log_serve_decision(cache_dir, cmd, None, "no_entry", None)
    return None'''
t = t.replace(anchor_b, replacement_b, 1)

path.write_text(t)
print(f"patched OK: {path}")
print("  + _current_generation / _spec_entry_invalid / _log_serve_decision")
print("  + generation-aware validation with reason-coded decisions")
print(f"  + decisions log: <cache_dir>/serve_decisions.jsonl")
