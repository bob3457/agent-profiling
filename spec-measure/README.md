# spec-measure — the four-measurement campaign

Answers, per benchmark (swebench, terminalbench, hotpotqa, freshqa):
  1. how well the gate decides whether a task is worth speculating on
  2. how many predictions the speculator makes and how many are accepted
  3. how much time an accepted prediction saves (by serve category)
  4. what it costs: extra CPU vs the agent's own CPU, and extra tokens

Depends on spec-analysis/ (decompose_serves.py) being untarred alongside.

## Why a patch is needed at all

Gate accuracy is unmeasurable in the deployed config: a NOGO kills
speculation, so you can never observe whether the task would have produced
serves — false negatives are invisible by construction. SPEC_GATE_SHADOW=1
records the verdict but runs speculation regardless, making every decision
scorable against realized value. Likewise CPU (components die by SIGTERM
with no rusage record) and gate tokens (plain `codex exec`, usage
discarded) need capture points added.

## Deploy

    cd $ROOT && tar xzf /scratch/czhai/spec-measure.tar.gz
    python3 spec-measure/patch_measure.py $ROOT
    python3 spec-measure/smoke_measure.py $ROOT     # expect ALL PASS (21)

## Run the campaign (arm C, shadow gate, speculation on all four benches)

    # eval set: one "<bench>\t<task_id>" line per task, all four benches;
    # freshqa needs manifests/freshqa_cpu_study_*.tsv
    # (scripts/materialize_freshqa_cpu_tasks.py)
    SPEC_GATE_SHADOW=1 SPEC_ALL_BENCH=1 \
      EVAL_SET=$OPT/eval_set_4bench.txt \
      bash $ROOT/latency-opt/harness/run_latency_arm.sh C

## Report

    RD=$(ls -td /scratch/czhai/latency-eval/results/arm_C.* | head -1)
    python3 $ROOT/latency-opt/speculation/ledger.py update \
        --ledger-dir $ROOT/latency-opt/ledger --results "$RD"
    python3 spec-measure/gate_eval_report.py "$RD" \
        --ledger-dir $ROOT/latency-opt/ledger

## Reading it

[1] GATE — confusion matrix at two ground truths: saved>0s (any realized
value) and saved>=5s (material value; TP at >0 but not >=5 means the gate
said yes to scraps). FN/TN require shadow mode; the report warns if any
scored task ran with an enforcing gate.

[2] PREDICTIONS — cached = commands the speculation side executed AND
cached ([spec] cached lines in spec_early.log/spec.log + respec re-runs);
accepted = serve events. Realized precision = accepted/cached. The LEDGER
table at the bottom adds command-match accuracy per predictor (did the
predicted command match what the agent ran, whether or not the cache entry
survived to be served) — the two disagree exactly when generation bumps or
fingerprints invalidate correct predictions.

[3] SAVED — total and per-accepted seconds, split exact / joined-net
(wait already subtracted) / prefix, plus join-wait and timeout waste.

[4] COST — speculation-side CPU (rusage of early worker + gated worker
chain + respec + enforced-NOGO gates) as a fraction of the agent's own CPU
(time.txt User+System); gate tokens (parsed from the LLM footer when
present, char/4-estimated otherwise and marked as such) plus per-predictor
LLM tokens from the ledger.

## Caveats

- Shadow mode is a measurement config: arm-C timing numbers from a shadow
  sweep overstate speculation cost on NOGO tasks; don't mix them into the
  A/B/C latency comparison.
- QA benches (hotpotqa/freshqa) run in empty workspaces; expected outcome
  is near-universal NOGO + zero serves, i.e. they measure the gate's
  true-negative rate and the speculator's idle cost, not savings.
- The daemon itself is shared agent infrastructure on arms B and C; its
  CPU is not attributed to speculation.
