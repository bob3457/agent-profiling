#!/usr/bin/env python3
"""check_arm64_availability.py — probe Docker Hub for which SWE-bench
Verified instances actually have arm64 eval images, WITHOUT pulling.

Docker Hub returns "access denied" for nonexistent repos (not 404), which
is why blind `apptainer pull` fall-through looks scary. This does an
anonymous-token + manifest HEAD per instance (~0.3s each, threaded).

Usage (login node):
  # filter an existing candidates file:
  python3 scripts/check_arm64_availability.py \
      --candidates manifests/swebench_extra_candidates.tsv \
      --out manifests/swebench_extra_available.tsv

Then pull from the filtered file:
  CAND=manifests/swebench_extra_available.tsv PER_REPO=3 \
      bash scripts/pull_swe_extra_sifs.sh
"""
import argparse
import concurrent.futures as cf
import json
import urllib.request
import urllib.error
from pathlib import Path

REGISTRY = "https://registry-1.docker.io"
AUTH = ("https://auth.docker.io/token"
        "?service=registry.docker.io&scope=repository:{repo}:pull")
ACCEPT = ("application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.oci.image.index.v1+json, "
          "application/vnd.docker.distribution.manifest.v2+json, "
          "application/vnd.oci.image.manifest.v1+json")


def has_image(iid, timeout=15):
    repo = f"swebench/sweb.eval.arm64.{iid.replace('__', '_1776_')}"
    try:
        with urllib.request.urlopen(AUTH.format(repo=repo), timeout=timeout) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(
            f"{REGISTRY}/v2/{repo}/manifests/latest", method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT})
        with urllib.request.urlopen(req, timeout=timeout):
            return iid, True
    except urllib.error.HTTPError:
        return iid, False
    except Exception as e:                       # network hiccup: report, don't drop
        print(f"  [warn] {iid}: {e}")
        return iid, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True,
                    help="TSV from select_swe_extra.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    lines = Path(args.candidates).read_text().splitlines()
    header, rows = lines[0], [l.split("\t") for l in lines[1:] if l.strip()]
    ids = [r[2] for r in rows]

    print(f"probing {len(ids)} instances against Docker Hub ...")
    avail = {}
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for iid, ok in ex.map(has_image, ids):
            avail[iid] = ok

    kept, per_repo = [], {}
    for r in rows:
        repo, iid = r[0], r[2]
        ok = avail.get(iid)
        mark = {True: "yes", False: "NO", None: "?"}[ok]
        print(f"  {repo:14s} {iid:44s} arm64={mark}")
        if ok:
            per_repo[repo] = per_repo.get(repo, 0) + 1
            kept.append(r)

    out = Path(args.out)
    with out.open("w") as f:
        f.write(header + "\n")
        # re-rank within repo, preserving original score order
        for r in kept:
            f.write("\t".join(r) + "\n")

    print(f"\navailable per repo: "
          + ", ".join(f"{k}={v}" for k, v in sorted(per_repo.items())))
    empty = {r[0] for r in rows} - set(per_repo)
    if empty:
        print(f"repos with ZERO arm64 images among candidates: {sorted(empty)}")
        print("  -> re-run select_swe_extra.py with --per-repo-candidates 999 "
              "--repos <those repos>, re-probe, and pull from that.")
    print(f"filtered candidates -> {out}")


if __name__ == "__main__":
    main()
