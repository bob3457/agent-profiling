#!/usr/bin/env python3
"""Generate prompts for new SWE-bench instances in the EXACT template of an
existing prompt: fetch the template instance's problem statement from
SWE-bench_Verified, locate it inside the existing prompt file, and substitute
each new instance's statement in its place. Fails loudly if the template
prompt doesn't contain the reference statement verbatim (then paste the
template and I'll adapt)."""
import sys
from pathlib import Path

ROOT = Path("/projects/kzhou6/czhai/agent-profiling")
TEMPLATE_ID = "astropy__astropy-12907"
NEW_IDS = [l.split("\t")[1].strip() for l in open(ROOT/"latency-opt/eval_sets/eval_set_30.txt")
           if l.startswith("swebench") and TEMPLATE_ID not in l
           and not (ROOT/f"prompts/swe_{l.split(chr(9))[1].strip()}.txt").exists()]
print("generating for:", NEW_IDS)

from datasets import load_dataset
ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
stmts = {r["instance_id"]: r["problem_statement"] for r in ds
         if r["instance_id"] in set(NEW_IDS) | {TEMPLATE_ID}}
missing = set(NEW_IDS) - set(stmts)
if missing:
    sys.exit(f"not in SWE-bench_Verified: {missing}")

template = (ROOT/f"prompts/swe_{TEMPLATE_ID}.txt").read_text()
ref = stmts[TEMPLATE_ID]
# tolerate trailing-whitespace normalization between dataset and prompt file
def norm(s): return "\n".join(l.rstrip() for l in s.strip().splitlines())
if norm(ref) not in norm(template):
    # try raw
    if ref.strip() not in template:
        sys.exit("template prompt does not contain the 12907 problem statement "
                 "verbatim — paste the prompt file and the statement will be "
                 "matched manually")
    ref_in_template = ref.strip()
else:
    # locate the raw span: find longest raw match by first/last lines
    first = ref.strip().splitlines()[0].rstrip()
    i = template.find(first)
    if i < 0:
        sys.exit("could not anchor statement in template")
    last = ref.strip().splitlines()[-1].rstrip()
    j = template.find(last, i)
    if j < 0:
        sys.exit("could not find statement end in template")
    ref_in_template = template[i:j+len(last)]

for iid in NEW_IDS:
    out = template.replace(ref_in_template, stmts[iid].strip())
    (ROOT/f"prompts/swe_{iid}.txt").write_text(out)
    print(f"wrote prompts/swe_{iid}.txt ({len(stmts[iid])} chars statement)")
