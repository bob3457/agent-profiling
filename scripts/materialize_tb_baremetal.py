"""Materialize Terminal-Bench tasks (harbor export tree) as bare-host
profiling tasks in Tejas manifest format: task \t workspace_template \t prompt.
Workspace = the task's environment/ contents minus the Dockerfile (the task's
starting files); prompt = instruction.md + a short bare-host preamble.
"""
import shutil, sys
from pathlib import Path
ROOT = Path("/projects/kzhou6/czhai/agent-profiling")
TASKS = Path("/projects/kzhou6/czhai/tb-tasks")
SELECT = Path(sys.argv[1]) if len(sys.argv) > 1 else None   # optional task-name list
names = set(l.strip() for l in SELECT.read_text().splitlines() if l.strip()) if SELECT else None

man = ROOT/"manifests/terminalbench_arm.tsv"
(ROOT/"prompts").mkdir(exist_ok=True)
rows = []
for toml in sorted(TASKS.rglob("task.toml")):
    tdir = toml.parent; task = tdir.name
    if names is not None and task not in names: continue
    instr = tdir/"instruction.md"
    if not instr.exists(): continue
    ws = ROOT/f"runs/terminalbench-arm/{task}/base_task"
    if not ws.exists():
        ws.mkdir(parents=True)
        env = tdir/"environment"
        if env.exists():
            for item in env.iterdir():
                if item.name == "Dockerfile": continue
                dst = ws/item.name
                shutil.copytree(item, dst) if item.is_dir() else shutil.copy2(item, dst)
    p = ROOT/f"prompts/tb_{task}.txt"
    p.write_text(
        "You are working on a Terminal-Bench-style task in a plain directory "
        "(no container). Work only inside the current directory. Install any "
        "tools you need with pip (no sudo/apt available).\n\nTask instruction:\n\n"
        + instr.read_text())
    rows.append(f"{task}\t{ws.relative_to(ROOT)}\t{p.relative_to(ROOT)}")
man.write_text("\n".join(rows) + "\n")
print(f"wrote {man} with {len(rows)} rows")
