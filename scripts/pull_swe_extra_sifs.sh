#!/usr/bin/env bash
# pull_swe_extra_sifs.sh — pull arm64 SWE-bench eval SIFs for the ranked
# candidates until PER_REPO instances per repo succeed.
#
# LOGIN node (egress). Docker Hub naming: instance-id "__" becomes "_1776_",
# image swebench/sweb.eval.arm64.<name>:latest. Not every Verified instance
# has an arm64 image published — that is exactly why we pull down a RANKED
# candidate list and fall through on failure.
#
#   PER_REPO=3 SIF_DIR=/scratch/czhai/sifs-arm64 ./scripts/pull_swe_extra_sifs.sh
#
# Output: $SIF_DIR/<instance_id>.sif  (matches run_option_b.sh's expectation)
#         manifests/swebench_extra_ids.txt  (final selected instance list)
set -uo pipefail
ROOT=${ROOT:-/projects/kzhou6/czhai/agent-profiling}
CAND=${CAND:-$ROOT/manifests/swebench_extra_candidates.tsv}
SIF_DIR=${SIF_DIR:-/scratch/czhai/sifs-arm64}
PER_REPO=${PER_REPO:-3}
OUT_IDS=${OUT_IDS:-$ROOT/manifests/swebench_extra_ids.txt}

[[ -f $CAND ]] || { echo "no candidates file: $CAND (run select_swe_extra.py first)" >&2; exit 1; }
mkdir -p "$SIF_DIR"
: > "$OUT_IDS.tmp"

# repos in candidate order
repos=$(tail -n +2 "$CAND" | cut -f1 | awk '!seen[$0]++')

for repo in $repos; do
  got=0
  while IFS=$'\t' read -r _ rank iid _rest; do
    (( got >= PER_REPO )) && break
    sif="$SIF_DIR/$iid.sif"
    if [[ -f $sif ]]; then
      echo "[$repo] exists: $iid"
      echo "$iid" >> "$OUT_IDS.tmp"; got=$((got+1)); continue
    fi
    docker_name="swebench/sweb.eval.arm64.${iid//__/_1776_}:latest"
    echo "[$repo] pulling candidate #$rank: $iid  ($docker_name)"
    if apptainer pull "$sif" "docker://$docker_name"; then
      echo "$iid" >> "$OUT_IDS.tmp"; got=$((got+1))
    else
      echo "[$repo]   no arm64 image (or pull failed) — falling through"
      rm -f "$sif"
    fi
  done < <(tail -n +2 "$CAND" | awk -F'\t' -v r="$repo" '$1==r')
  echo "[$repo] selected $got/$PER_REPO"
done

sort "$OUT_IDS.tmp" > "$OUT_IDS" && rm "$OUT_IDS.tmp"
echo
echo "final instance list -> $OUT_IDS ($(wc -l < "$OUT_IDS") instances)"
echo "next: python3 scripts/materialize_swe_extra.py   (login node)"
