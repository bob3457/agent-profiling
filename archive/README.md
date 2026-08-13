# archive/

Historical artifacts kept for provenance. Nothing here is needed to run
anything — the tracked tree already contains every change these produced.

## patchers/

One-shot anchor-asserting patch scripts from the latency-opt, spec-agnostic,
spec-measure, spec-parse-v2, spec-heads-v1, and scripts/ (spec-serve-v1,
django-merge) build iterations. **All of their edits are already applied
to the files tracked in this repo** — verified by re-running every patcher
against a fresh checkout: each one reports "already patched" / "no changes"
and leaves the tree byte-identical.

The `spec-measure/` patchers are a partial exception to "do not run": the
spec-measure smoke tests still apply them to a scratch COPY of the repo (the
smokes reference them here by path), so keep them runnable. They still must
not be run against the live tree — they will simply no-op.

They are kept only as a record of what changed and why (their docstrings are
the changelog). Do not run them against this tree; several will no-op and one
is known-broken:

- `spec-agnostic/patch_harness_early_recon.py` — fails with ANCHOR DRIFT
  against the current `run_latency_arm.sh` because a later patch
  (`patch_harness_realpath` / the T=0 recon block) superseded its anchor.
  Its intended change is already present.
- `spec-parse-v2/patch_predictor_parse.py`,
  `spec-parse-v2/patch_scoring_and_gate.py`,
  `spec-heads-v1/patch_known_heads.py` — abort with "anchor missing" against
  the current tree: their idempotency markers were version-tagged comments
  that have since been reworded to plain descriptions. They abort BEFORE
  modifying anything; their intended changes are already present.
- `scripts/patch_spec_serve_v1.py` — same comment-rewording situation, but
  it carries a code-based guard (probes for the segment-serve signature in
  shell_sessiond.py) and skips cleanly.

If you need to patch a *deployed* copy on the cluster that predates one of
these, prefer rsyncing the tracked files over re-running the patcher.

## coverage/

Point-in-time outputs of `spec-agnostic/corpus_coverage.py` against the
command corpus (v1 tiers, v2 tiers, v2.1 tiers, and the TB corpus run).
Regenerable:

    python3 spec-agnostic/corpus_coverage.py <corpus files> \
        --spec-dir latency-opt/speculation

## Removed entirely (recoverable from git history)

- `latency-opt/harness/run_option_b.sh.bak` — stale editor backup of a
  tracked file.
- `spec-parse-v2/speculation/predict_parse.py`,
  `spec-parse-v2/testset/{build_testset,replay_testset}.py` — as-shipped
  snapshots superseded by the live copies in `latency-opt/speculation/`
  and `testset/` (which carry the spec-heads whitelist and the
  capture-to-sweep binding fix).
- `latency-opt/ledger/ledger.jsonl` — runtime prediction records from one
  old sweep; the ledger is runtime state (lives under the results dir on
  scratch), nothing reads a checked-in copy.
- `latency-opt/eval_sets/eval_set_ab.txt` — 6-task pilot set, referenced
  nowhere.

- `latency-opt/speculation/spec_tiers.py.v1` — pre-v2 backup of the tier
  policy; superseded, and a stray importable `.py.v1` next to the live module
  invited confusion.
- `spec-agnostic/spec_tiers.py` — byte-identical duplicate of
  `latency-opt/speculation/spec_tiers.py` (the deploy copy described in
  `spec-agnostic/NOTES.md`). The live module is the single source of truth;
  `corpus_coverage.py --tiers` can point at any candidate file.
