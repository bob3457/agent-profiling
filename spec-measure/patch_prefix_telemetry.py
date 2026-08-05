#!/usr/bin/env python3
"""patch_prefix_telemetry.py — make prefix-serve failures speak.

The conversion campaign showed prefix partials firing but saving pennies,
and full attempts dying silently ("4/4 leading parts cached, no
prefix_serve fired" — cause unknowable post-hoc). This adds, with no
behavior change:

  - every prefix ATTEMPT that serves nothing logs a `prefix_attempt`
    decision with the reason the leading part was unservable:
      hazard | state_cmd | cd_cmd | no_entry | invalid_entry
    (invalid_entry = the exact key file EXISTS but failed validation --
    the generation/fingerprint case, indistinguishable from no_entry today)
  - partial and full prefix_serve records gain `stop_reason` and
    `stop_part` for the part where serving ended

Verbatim anchors; idempotent.  Usage: patch_prefix_telemetry.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
F = ROOT / "latency-opt/scripts/shell_sessiond.py"
DONE, SKIP = [], []


def patch(anchor, replacement, marker, label):
    src = F.read_text()
    if marker in src:
        SKIP.append(label)
        return
    assert anchor in src, f"{label}: anchor not found"
    assert src.count(anchor) == 1, f"{label}: anchor not unique"
    F.write_text(src.replace(anchor, replacement))
    DONE.append(label)


# ---- 1. reason tracking inside the serve loop -----------------------------------
patch('''    for i, (text, stop_on_fail, servable) in enumerate(parts):
        if not servable or is_state_cmd(text) or is_cd_cmd(text):
            break                         # ends the servable prefix
        entry = spec_cache_lookup(cache_dir, text, eff_cwd, log=False)
        if entry is None:
            break''',
      '''    stop_reason, stop_part = None, None
    for i, (text, stop_on_fail, servable) in enumerate(parts):
        if not servable or is_state_cmd(text) or is_cd_cmd(text):
            stop_reason = ("hazard" if not servable else
                           "state_cmd" if is_state_cmd(text) else "cd_cmd")
            stop_part = text[:120]
            break                         # ends the servable prefix
        entry = spec_cache_lookup(cache_dir, text, eff_cwd, log=False)
        if entry is None:
            k = hashlib.sha256(f"{eff_cwd}\\x00{text}".encode()).hexdigest()
            stop_reason = ("invalid_entry"          # exists, failed validation
                           if (Path(cache_dir) / f"{k}.json").exists()
                           else "no_entry")
            stop_part = text[:120]
            break''',
      'stop_reason, stop_part = None, None',
      "reason tracking in serve loop")

# ---- 2. attempt-failure logging + stop fields on the return ----------------------
patch('''    if n == 0:
        return None
    if n == total:
        return {"remainder": None, "cwd": eff_cwd, "stdout": "".join(out),
                "stderr": "".join(err), "exit": last_exit, "n": n,
                "total": total, "saved_s": saved}
    return {"remainder": rejoin(parts[n:]), "cwd": eff_cwd,
            "stdout": "".join(out), "stderr": "".join(err), "exit": None,
            "n": n, "total": total, "saved_s": saved}''',
      '''    if n == 0:
        try:                              # attempt died on part 0: say why
            with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": time.time(), "cmd": cmd[:300],
                    "decision": "prefix_attempt", "parts_total": total,
                    "parts_served": 0, "stop_reason": stop_reason,
                    "stop_part": stop_part}) + "\\n")
        except OSError:
            pass
        return None
    if n == total:
        return {"remainder": None, "cwd": eff_cwd, "stdout": "".join(out),
                "stderr": "".join(err), "exit": last_exit, "n": n,
                "total": total, "saved_s": saved,
                "stop_reason": stop_reason, "stop_part": stop_part}
    return {"remainder": rejoin(parts[n:]), "cwd": eff_cwd,
            "stdout": "".join(out), "stderr": "".join(err), "exit": None,
            "n": n, "total": total, "saved_s": saved,
            "stop_reason": stop_reason, "stop_part": stop_part}''',
      '"decision": "prefix_attempt"',
      "attempt-failure logging + stop fields")

# ---- 3. stop fields flow into prefix_serve records --------------------------------
patch('''def _log_prefix_decision(cache_dir, cmd, n, total, saved_s, full):
    try:
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(), "cmd": cmd[:300],
                "decision": "prefix_serve", "parts_served": n,
                "parts_total": total, "saved_s": round(saved_s, 3),
                "full": full}) + "\\n")
    except OSError:
        pass''',
      '''def _log_prefix_decision(cache_dir, cmd, n, total, saved_s, full,
                         stop_reason=None, stop_part=None):
    try:
        rec = {"ts": time.time(), "cmd": cmd[:300],
               "decision": "prefix_serve", "parts_served": n,
               "parts_total": total, "saved_s": round(saved_s, 3),
               "full": full}
        if stop_reason:
            rec["stop_reason"] = stop_reason
            rec["stop_part"] = stop_part
        with open(Path(cache_dir) / "serve_decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\\n")
    except OSError:
        pass''',
      'stop_reason=None, stop_part=None',
      "prefix log helper gains stop fields")

patch('''            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=True)''',
      '''            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=True,
                                 stop_reason=pre.get("stop_reason"),
                                 stop_part=pre.get("stop_part"))''',
      'full=True,\n                                 stop_reason=',
      "full-serve call passes stop fields")

patch('''            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=False)''',
      '''            _log_prefix_decision(self.args.spec_cache, cmd, pre["n"],
                                 pre["total"], pre["saved_s"], full=False,
                                 stop_reason=pre.get("stop_reason"),
                                 stop_part=pre.get("stop_part"))''',
      'full=False,\n                                 stop_reason=',
      "partial-serve call passes stop fields")

print(f"applied: {DONE}")
print(f"already present: {SKIP}")
