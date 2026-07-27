#!/usr/bin/env bash
# materialize_swebench_ws.sh — extract /testbed from each instance SIF into a
# bare-host workspace and fetch the problem statement.
#
# Run the FETCH part on the login node (egress); extraction can run anywhere
# with apptainer. Idempotent: skips workspaces that already exist.
#
#   SIF_DIR=/scratch/czhai/<wherever the sifs are> ./materialize_swebench_ws.sh
set -euo pipefail
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
SIF_DIR=${SIF_DIR:?set SIF_DIR to the directory containing sweb.eval.arm64 SIFs}
WS_ROOT=${WS_ROOT:-/scratch/czhai/latency-eval/workspaces}
INSTANCES=${INSTANCES:-"astropy__astropy-12907 astropy__astropy-13236 django__django-10973"}

mkdir -p "$WS_ROOT"

# --- problem statements (login node; needs `datasets` in your conda env) ----
PS_DIR=$WS_ROOT/problem_statements
mkdir -p "$PS_DIR"
python3 - "$PS_DIR" $INSTANCES <<'EOF'
import json, sys
from pathlib import Path
out = Path(sys.argv[1]); wanted = set(sys.argv[2:])
missing = {i for i in wanted if not (out / f"{i}.md").exists()}
if not missing:
    print("problem statements already present"); raise SystemExit
from datasets import load_dataset
ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
for row in ds:
    if row["instance_id"] in missing:
        (out / f"{row['instance_id']}.md").write_text(row["problem_statement"])
        (out / f"{row['instance_id']}.meta.json").write_text(json.dumps(
            {"base_commit": row["base_commit"], "repo": row["repo"],
             "FAIL_TO_PASS": row["FAIL_TO_PASS"], "PASS_TO_PASS": row["PASS_TO_PASS"]}))
        missing.discard(row["instance_id"])
if missing:
    raise SystemExit(f"not found in SWE-bench_Verified: {missing}")
print("problem statements fetched")
EOF

# --- workspace extraction ----------------------------------------------------
for id in $INSTANCES; do
  WORK=$WS_ROOT/$id
  if [[ -d $WORK/.git || -d $WORK ]] && [[ -n "$(ls -A "$WORK" 2>/dev/null)" ]]; then
    echo "skip (exists): $WORK"; continue
  fi
  # SIF filename patterns seen in the wild; adjust if yours differ
  SIF=""
  for cand in "$SIF_DIR/sweb.eval.arm64.${id}.sif" \
              "$SIF_DIR/${id}.sif" \
              "$SIF_DIR"/*"${id}"*.sif; do
    [[ -f "$cand" ]] && SIF=$cand && break
  done
  [[ -z "$SIF" ]] && { echo "ERROR: no SIF for $id under $SIF_DIR" >&2; exit 1; }
  echo "extracting $id from $(basename "$SIF")"
  mkdir -p "$WORK"
  apptainer exec "$SIF" tar -C /testbed -cf - . | tar -C "$WORK" -xf -
  # keep the repo pristine for diffing the agent's patch later
  git -C "$WORK" status --porcelain | head -3 || true
done
echo "workspaces ready under $WS_ROOT"
