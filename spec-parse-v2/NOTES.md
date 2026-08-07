# spec-parse-v2 — prediction parser rewrite + LLM invocation change + replayable test set

## Confirmed bugs (all reproduced offline against the shipped code)

**llm_predictor.py (the parser you asked about)**
1. `lstrip("-*0123456789. ")` strips a charset, not a prefix:
   `./verify.sh` -> `/verify.sh`, `7z x a.zip` -> `z x a.zip`. Mangled
   predictions can never key-match.
2. Fence lines survive: "```bash" -> "bash", passes the old validator ->
   phantom prediction, wastes a pre-run slot.
3. `_looks_like_command` accepts prose: "Check the README first",
   "Looking at the files", "Run pytest to verify" all True.
4. The `grab()` leaf-walk harvests reasoning text, command output, and
   any "text"/"content"/"message"/"output" field — the same failure class
   llm_gate was already hardened against (run 20260731_183452) but the
   predictor never got the fix. Reasoning prose is where junk candidates
   came from.
5. `_walk_tokens` SUMS every int with "token" in its key across every
   event -> token accounting inflated by deltas/cumulative repeats
   (ledger prices the LLM predictor off this number).

**spec_families.py / predictor_eval.py (scoring parser)**
6. `parse_command` puts value-flag arguments into targets:
   `pytest -k separable a/tests/test_separable.py` ->
   targets `['separable', 'a/...py']`. Skews predictor_eval, ledger
   outcomes, and spec_near_miss Jaccard. (Cache KEYS were fine —
   normalize_pytest handled `-p X` — but targets disagreed with keys.)
7. `score_pair` non-monotone: 1 correct node id out of an observed 9-id
   run scored 0.111 < 0.8 for a fully disjoint test in the same file.

**llm_gate.py**
8. YES/NO scan on raw whitespace tokens: "YES." is unparseable, and the
   reversed scan can then hit a YES/NO token echoed from the prompt
   header -> wrong verdict source.

## What ships

- `speculation/predict_parse.py`  NEW module, self-tested
  (`python3 predict_parse.py`): schema-aware agent_message extraction,
  last-wins usage extraction, JSON-array-first command extraction,
  prefix-regex line cleaning, structural command validator.
- `patch_predictor_parse.py`  rewires llm_predictor.py:
  * prompts now request a strict JSON array (deterministic parse; line
    format kept as fallback)
  * adds `--output-last-message <tmp>` when the codex build supports it
    (probed via `exec --help`, cached) -> final message verbatim, zero
    stream scraping for text; falls back to schema-aware stream parse,
    then legacy
  * tokens via extract_usage
  * meta gains text_source / parse keys; everything the worker/ledger
    reads (tokens, latency_s, exit, mode) unchanged
- `patch_scoring_and_gate.py`  fixes 6/7/8. Cache keys wire-compatible
  (normalize_* untouched). NOTE: score_pair change means old corpora
  must be re-scored before comparing to the 0.400 heuristic and
  0.69–0.93 LLM baselines.
- `smoke_predict_parse.py`  offline end-to-end: stub codex binary — 6
  parse cases (±last-message support x json/prose/tests answers) plus a
  capture->build->replay stage; asserts reasoning "commands" never leak,
  tokens equal the final usage dict, and reparsed beats a mangled
  as-parsed baseline on exact-string hits.

## Replayable prediction test set (zero new tokens)

Point: iterate on the parser without re-paying for LLM calls.

- CAPTURE (needs the v2 patch): set `SPEC_PRED_CAPTURE_DIR` wherever the
  worker runs (e.g. export it in run_latency_arm.sh next to SPEC_LLM_BIN).
  Every predictor call then saves prompt + raw --json stdout + extracted
  answer text + parsed commands to `<dir>/pred_<task>_<ms>.json`.
- `testset/build_testset.py`  harvests EXISTING runs — spec.log
  `[spec] llm predictor: [...]` lines + shelld_logs/commands.jsonl —
  and merges capture files / ledger prediction records when present:

      python3 testset/build_testset.py \
          --results '/scratch/czhai/latency-eval/results/arm_C*' \
          --out testset.jsonl \
          --capture-dir /scratch/czhai/latency-eval/pred_capture \
          --ledger-dir  /scratch/czhai/latency-eval/ledger

  Old runs contribute (as-parsed prediction, observed) pairs only — the
  raw model output was never saved pre-v2, so re-parsing applies to
  captures going forward.
- `testset/replay_testset.py`  scores every case: `as_parsed` (the list
  stored at run time, i.e. the old parser's output = baseline) vs
  `reparsed` (raw capture re-run through the CURRENT predict_parse
  pipeline). Metrics: family score (score_pair best-pair, the SWE-bench
  metric) and exact-string hit rate (what actually gates exact-key cache
  serves). `--diff` prints cases where the parsers disagree; `--out`
  writes a JSON report.

      python3 testset/replay_testset.py --testset testset.jsonl --diff

## Deploy (Hopper/GH200)

    cd /projects/kzhou6/czhai/agent-profiling
    tar xf /scratch/czhai/spec-parse-v2.tar.gz
    cp spec-parse-v2/speculation/predict_parse.py latency-opt/speculation/
    cp -r spec-parse-v2/testset .
    python3 spec-parse-v2/patch_predictor_parse.py --root .
    python3 spec-parse-v2/patch_scoring_and_gate.py --root .
    python3 latency-opt/speculation/predict_parse.py          # self-test
    python3 latency-opt/speculation/spec_families.py          # regression
    python3 spec-parse-v2/smoke_predict_parse.py --root .     # e2e, offline
    # then build the test set from existing arm_C runs (no tokens):
    python3 testset/build_testset.py --results '/scratch/czhai/latency-eval/results/arm_C*' --out /scratch/czhai/latency-eval/testset.jsonl
    python3 testset/replay_testset.py --testset /scratch/czhai/latency-eval/testset.jsonl

Both patchers are idempotent (marker-guarded) and assert verbatim anchors.
edit_respec.parse_candidates is untouched and stays compatible: the worker
still logs `[spec] llm predictor: [...]` as a Python list repr.

## Not fixed (known, listed for later)

- llm_gate.first_signal advances the stream offset past a partially
  written trailing line (fragment lost); low impact since buf accumulates.
- Prompt echo in the gate is only mitigated (punctuation strip), not
  eliminated; a --json + agent_message parse like the predictor's would
  eliminate it — left out to keep the gate patch minimal before a sweep.
