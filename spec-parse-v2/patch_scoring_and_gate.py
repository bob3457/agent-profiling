#!/usr/bin/env python3
"""patch_scoring_and_gate.py — three small correctness fixes.

Idempotent; verbatim anchors; run from repo root:
    python3 patch_scoring_and_gate.py [--root .]

FIX A  spec_families.parse_command: value-taking flags polluted targets.
       Confirmed offline:
         pytest -k separable a/tests/test_separable.py
           -> targets ['separable', 'a/tests/test_separable.py']
         python -m pytest -p no:cacheprovider tests/test_x.py
           -> targets ['no:cacheprovider', 'tests/test_x.py']
       Wrong targets skew predictor_eval scores, ledger outcomes, and
       spec_near_miss telemetry (a 'separable' phantom target deflates
       Jaccard). Fix: skip the value of -k/-m/-p/-o/-W/-n/-c/--tb/
       --deselect/--ignore/--maxfail (pytest) and --settings/--parallel/
       -v/--verbosity (django). normalize_* key functions are UNTOUCHED —
       cache keys stay wire-compatible.

FIX B  predictor_eval.score_pair non-monotonicity. Confirmed offline:
       predicting 1 correct node id of an observed 9-id run scored 0.111,
       LOWER than predicting a completely disjoint test in the same file
       (0.8). Fix: partial-overlap score = max(jaccard, file/family floor).
       NOTE: re-score old corpora before comparing against the 0.400
       heuristic / 0.69-0.93 LLM baselines — this raises some scores.

FIX C  llm_gate answer scan matched raw whitespace-split tokens, so a
       model answering "YES." or "NO." was unparseable and the reversed
       scan could fall through to a YES/NO token echoed from the prompt
       header. Fix: strip punctuation when normalizing words.

MARKER: spec-score-v1
"""
import argparse
from pathlib import Path

MARKER = "spec-score-v1"

# ---------------------------------------------------------------- FIX A
FAM_OLD = '''    if (parts[0] in ("python", "python3") and parts[1:3] == ["-m", "pytest"]) \\
            or parts[0] == "pytest":
        args = parts[3:] if parts[0] != "pytest" else parts[1:]
        targets = [a for a in args if not a.startswith("-")]
        return {"family": "pytest", "targets": targets,
                "key": normalize_pytest(cmd)}
    if len(parts) >= 3 and parts[0] in ("python", "python3") \\
            and parts[1].endswith("runtests.py"):
        targets = [a for a in parts[2:]
                   if not a.startswith("-") and not a.isdigit()]
        return {"family": "django", "targets": targets,
                "key": normalize_django(cmd)}
    return None'''

FAM_NEW = '''    if (parts[0] in ("python", "python3") and parts[1:3] == ["-m", "pytest"]) \\
            or parts[0] == "pytest":
        args = parts[3:] if parts[0] != "pytest" else parts[1:]
        targets = _positional_targets(args, _PYTEST_VALUE_FLAGS)
        return {"family": "pytest", "targets": targets,
                "key": normalize_pytest(cmd)}
    if len(parts) >= 3 and parts[0] in ("python", "python3") \\
            and parts[1].endswith("runtests.py"):
        targets = [a for a in _positional_targets(parts[2:],
                                                  _DJANGO_VALUE_FLAGS)
                   if not a.isdigit()]
        return {"family": "django", "targets": targets,
                "key": normalize_django(cmd)}
    return None'''

FAM_HELPER_ANCHOR = '''def parse_command(cmd: str):'''
FAM_HELPER = '''# flags whose VALUE is a separate argv token: the value is not a target.
# (spec-score-v1: `pytest -k separable a/test_x.py` used to yield the
# phantom target 'separable', corrupting scoring and near-miss telemetry)
_PYTEST_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-W", "-n", "-c", "--tb",
                       "--deselect", "--ignore", "--maxfail", "--rootdir",
                       "--confcutdir", "--junitxml", "--durations",
                       "--cov", "--dist", "--timeout", "-r"}
_DJANGO_VALUE_FLAGS = {"-v", "--verbosity", "--settings", "--parallel",
                       "--exclude-tag", "--tag"}


def _positional_targets(args, value_flags):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in value_flags:
                i += 1                  # skip the flag's value token
        else:
            out.append(a)
        i += 1
    return out


def parse_command(cmd: str):'''

# ---------------------------------------------------------------- FIX B
SCORE_OLD = '''def score_pair(pred, obs):
    """pred/obs are parse_command dicts. Return graded score."""
    if pred["family"] != obs["family"]:
        return 0.0
    pt, ot = set(pred["targets"]), set(obs["targets"])
    if pt == ot:
        return 1.0
    if pt & ot:
        return round(len(pt & ot) / len(pt | ot), 3)
    p_files = {t.split("::")[0].split(".")[0] for t in pt}
    o_files = {t.split("::")[0].split(".")[0] for t in ot}
    if p_files & o_files:
        return 0.8
    return 0.2'''

SCORE_NEW = '''def score_pair(pred, obs):
    """pred/obs are parse_command dicts. Return graded score.

    spec-score-v1: partial target overlap is floored at the same
    file-level (0.8) / family-level (0.2) credit a fully DISJOINT
    prediction would earn — previously 1 right node id out of an
    observed 9-id run scored 0.111 while a wrong test in the right
    file scored 0.8 (non-monotone). Re-score old corpora before
    comparing against pre-fix baselines."""
    if pred["family"] != obs["family"]:
        return 0.0
    pt, ot = set(pred["targets"]), set(obs["targets"])
    if not pt or not ot:
        return 0.0
    if pt == ot:
        return 1.0
    p_files = {t.split("::")[0].split(".")[0] for t in pt}
    o_files = {t.split("::")[0].split(".")[0] for t in ot}
    floor = 0.8 if (p_files & o_files) else 0.2
    if pt & ot:
        return round(max(len(pt & ot) / len(pt | ot), floor), 3)
    return floor'''

# ---------------------------------------------------------------- FIX C
GATE_OLD = '''        words = [w.strip().upper() for w in (r.stdout or "").split()]'''
GATE_NEW = '''        words = [w.strip().upper().strip(".:,;!?'\\"`)(*_")
                 for w in (r.stdout or "").split()]  # spec-score-v1'''


def _patch(path: Path, pairs, marker_ok):
    src = path.read_text()
    if marker_ok in src:
        print(f"already patched: {path}")
        return
    for old, new in pairs:
        assert old in src, f"anchor missing in {path}: {old[:60]!r}"
        src = src.replace(old, new, 1)
    path.write_text(src)
    print(f"patched {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    spec = Path(args.root) / "latency-opt/speculation"
    _patch(spec / "spec_families.py",
           [(FAM_HELPER_ANCHOR, FAM_HELPER), (FAM_OLD, FAM_NEW)],
           MARKER)
    _patch(spec / "predictor_eval.py", [(SCORE_OLD, SCORE_NEW)], MARKER)
    _patch(spec / "llm_gate.py", [(GATE_OLD, GATE_NEW)], MARKER)


if __name__ == "__main__":
    main()
