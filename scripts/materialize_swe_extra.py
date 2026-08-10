#!/usr/bin/env python3
"""materialize_swe_extra.py — materialize the extra SWE-bench instances
(selected by select_swe_extra.py, SIF-confirmed by pull_swe_extra_sifs.sh).

Mirrors scripts/materialize_swe_arm25.py: per instance, checkout the repo at
base_commit into runs/swebench-extra/<iid>/base_repo (via the shared per-repo
cache), write prompts/swe_<iid>.txt, append manifests/swebench_extra.tsv.
Also drops FAIL_TO_PASS/PASS_TO_PASS meta next to each workspace (used by
predictor_eval and later grading).

LOGIN node (network + `datasets`). Idempotent: skips existing checkouts and
rewrites the manifest from the id list each run.
"""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/projects/kzhou6/czhai/agent-profiling")
IDS_FILE = ROOT / "manifests/swebench_extra_ids.txt"
IDS = [l.strip() for l in IDS_FILE.read_text().splitlines() if l.strip()]

from datasets import load_dataset  # noqa: E402
ds = {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")}

CACHE = ROOT / "runs/swebench-arm/_repo_cache"   # reuse the arm25 cache
man = ROOT / "manifests/swebench_extra.tsv"
(ROOT / "prompts").mkdir(exist_ok=True)


def repo_cache(repo):
    d = CACHE / repo.replace("/", "__")
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none",
                        f"https://github.com/{repo}.git", str(d)], check=True)
    return d


with man.open("w") as f:
    for iid in IDS:
        if iid not in ds:
            print(f"[skip] {iid}: not in SWE-bench_Verified")
            continue
        r = ds[iid]
        base = ROOT / f"runs/swebench-extra/{iid}/base_repo"
        if not (base / ".git").exists():
            src = repo_cache(r["repo"])
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, base)
        subprocess.run(["git", "checkout", "-f", r["base_commit"]], cwd=base, check=True)
        subprocess.run(["git", "clean", "-fdx"], cwd=base, check=True)
        (base.parent / "meta.json").write_text(json.dumps(
            {"repo": r["repo"], "base_commit": r["base_commit"],
             "FAIL_TO_PASS": r["FAIL_TO_PASS"], "PASS_TO_PASS": r["PASS_TO_PASS"]}))
        p = ROOT / f"prompts/swe_{iid}.txt"
        p.write_text(
            f"You are working on a SWE-bench instance.\nInstance id: {iid}\n"
            f"Repository: {r['repo']}\nBase commit: {r['base_commit']}\n\n"
            f"Problem statement:\n{r['problem_statement']}\n\nInstructions:\n"
            "1. Inspect the repository.\n"
            "2. Make the minimal code change to fix the issue.\n"
            "3. Run relevant tests if possible.\n"
            "4. Do not make unrelated changes.\n"
            "5. Summarize files changed and validation performed.\n")
        f.write(f"{iid}\truns/swebench-extra/{iid}/base_repo\tprompts/swe_{iid}.txt\n")
        print(f"[ok] {iid}")
print(f"manifest -> {man}")
