import shutil, subprocess
from pathlib import Path
from datasets import load_dataset
ROOT = Path("/projects/kzhou6/czhai/agent-profiling")
IDS = [l.strip() for l in open("/projects/kzhou6/czhai/Agent-Bench/configs/instances_arm25.txt") if l.strip()]
ds = {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")}
CACHE = ROOT / "runs/swebench-arm/_repo_cache"
man = ROOT/"manifests/swebench_arm25.tsv"; man.parent.mkdir(exist_ok=True)
(ROOT/"prompts").mkdir(exist_ok=True)

def repo_cache(repo):
    d = CACHE / repo.replace("/", "__")
    if not (d/".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git","clone","--filter=blob:none",
                        f"https://github.com/{repo}.git",str(d)], check=True)
    return d

with man.open("w") as f:
    for iid in IDS:
        r = ds[iid]
        base = ROOT/f"runs/swebench-arm/{iid}/base_repo"
        if not (base/".git").exists():
            src = repo_cache(r["repo"])
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, base)                       # local copy, no network
        subprocess.run(["git","checkout","-f",r["base_commit"]],cwd=base,check=True)  # fetches needed blobs
        subprocess.run(["git","clean","-fdx"],cwd=base,check=True)
        p = ROOT/f"prompts/swe_{iid}.txt"
        p.write_text(f"You are working on a SWE-bench instance.\nInstance id: {iid}\nRepository: {r['repo']}\nBase commit: {r['base_commit']}\n\nProblem statement:\n{r['problem_statement']}\n\nInstructions:\n1. Inspect the repository.\n2. Make the minimal code change to fix the issue.\n3. Run relevant tests if possible.\n4. Do not make unrelated changes.\n5. Summarize files changed and validation performed.\n")
        f.write(f"{iid}\truns/swebench-arm/{iid}/base_repo\tprompts/swe_{iid}.txt\n")
        print(f"[ok] {iid}")
print(f"wrote {man} with {len(IDS)} rows")
