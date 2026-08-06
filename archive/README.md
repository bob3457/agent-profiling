# archive/

Historical artifacts kept for provenance. Nothing here is needed to run
anything — the tracked tree already contains every change these produced.

## patchers/

One-shot anchor-asserting patch scripts from the latency-opt, spec-agnostic,
and spec-measure build iterations. **All of their edits are already applied
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

If you need to patch a *deployed* copy on the cluster that predates one of
these, prefer rsyncing the tracked files over re-running the patcher.

## coverage/

Point-in-time outputs of `spec-agnostic/corpus_coverage.py` against the
command corpus (v1 tiers, v2 tiers, v2.1 tiers, and the TB corpus run).
Regenerable:

    python3 spec-agnostic/corpus_coverage.py <corpus files> \
        --spec-dir latency-opt/speculation

## Removed entirely (recoverable from git history)

- `latency-opt/speculation/spec_tiers.py.v1` — pre-v2 backup of the tier
  policy; superseded, and a stray importable `.py.v1` next to the live module
  invited confusion.
- `spec-agnostic/spec_tiers.py` — byte-identical duplicate of
  `latency-opt/speculation/spec_tiers.py` (the deploy copy described in
  `spec-agnostic/NOTES.md`). The live module is the single source of truth;
  `corpus_coverage.py --tiers` can point at any candidate file.
