#!/usr/bin/env python3
"""spec_gate.py — decide whether speculative execution is worth launching.

Design: two-stage gate, cheap first.

Stage 1 (static, free): benchmark class. Your own perf data motivates this —
HotpotQA runs showed ~1.1s task-clock, ~9.7% CPU utilization and ZERO local
shell commands: there is no local work to speculate on, the latency is all
model inference + web search. SWE-bench / Terminal-Bench are the opposite:
local environment setup (deps, test discovery, repo exploration) dominates
the tool-side time and is highly predictable from the task description.

Stage 2 (per-task, still cheap): task features. Even inside SWE-bench some
instances need no env prep (image already has deps baked); the gate inspects
the workspace to estimate expected value:
    EV(speculation) = p(prediction correct) * time_saved - cost(wasted work)
Wasted speculative work is nearly free here: it runs in otherwise-idle CPU
(your runs are ~90% idle waiting on the API) and lands in an overlay/cache
that is simply discarded on a miss. So the bar for "worth it" is low; the
gate mostly exists to avoid pointless process churn on QA benchmarks.

Usage:
    from spec_gate import should_speculate
    decision = should_speculate(benchmark="swebench", workspace="/path/to/repo")
    if decision.speculate: launch speculative_worker with decision.actions
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateDecision:
    speculate: bool
    reason: str
    actions: list = field(default_factory=list)  # ordered speculative action plan
    confidence: float = 0.0


# Stage 1: benchmark-class priors. Keyed on how much *local, predictable*
# work the benchmark family front-loads.
BENCHMARK_PRIOR = {
    # benchmark: (prior_speculate, rationale)
    "hotpotqa":       (False, "pure orchestration floor: zero local shell commands, all latency is inference/web"),
    "freshqa":        (False, "open-web QA: no local workspace, nothing to pre-compute"),
    "swebench":       (True,  "env prep (deps, test discovery, repo indexing) is predictable from the instance"),
    "terminalbench":  (True,  "task setup (files, packages, build state) is predictable from the task dir"),
    "webarena":       (False, "browser-driven; local shell nearly unused; services already warm"),
}


def _workspace_features(ws: Path) -> dict:
    f = {"exists": ws.is_dir()}
    if not f["exists"]:
        return f
    f["is_git"] = (ws / ".git").exists()
    f["py_project"] = any((ws / n).exists() for n in
                          ("setup.py", "pyproject.toml", "requirements.txt", "tox.ini"))
    f["node_project"] = (ws / "package.json").exists()
    f["rust_project"] = (ws / "Cargo.toml").exists()
    f["has_tests"] = any((ws / n).exists() for n in ("tests", "test", "pytest.ini", "conftest.py"))
    try:
        f["n_top_entries"] = len(list(ws.iterdir()))
    except OSError:
        f["n_top_entries"] = 0
    return f


def should_speculate(benchmark: str, workspace: str | None = None,
                     task_text: str = "") -> GateDecision:
    benchmark = benchmark.lower().replace("-", "").replace("_", "")
    for key, (prior, why) in BENCHMARK_PRIOR.items():
        if key in benchmark:
            if not prior:
                return GateDecision(False, f"benchmark prior: {why}")
            break
    else:
        # unknown benchmark: fall through to workspace inspection
        why = "unknown benchmark; deciding from workspace features"

    if workspace is None:
        return GateDecision(False, "no local workspace: nothing to speculate on")

    ws = Path(workspace)
    feat = _workspace_features(ws)
    if not feat.get("exists"):
        return GateDecision(False, "workspace path does not exist")

    # Build the action plan from features. Every action is (a) read-only or
    # (b) confined to the speculation sandbox/cache — a wrong guess costs
    # nothing but already-idle CPU.
    actions = []
    if feat.get("is_git"):
        actions += ["git_status", "repo_index"]           # file list + symbol index
    if feat.get("py_project"):
        actions += ["py_dep_preinstall", "pytest_collect"]  # deps into spec venv; test discovery
        if feat.get("has_tests"):
            actions += ["pytest_run_cached"]              # pre-run tests, cache output
    if feat.get("node_project"):
        actions += ["npm_ci_prefetch"]
    if feat.get("rust_project"):
        actions += ["cargo_fetch"]

    if not actions:
        return GateDecision(False, f"workspace has no recognizable prep surface: {feat}")

    conf = 0.9 if (feat.get("py_project") and feat.get("has_tests")) else 0.6
    return GateDecision(True, f"{why}; features={ {k: v for k, v in feat.items() if v} }",
                        actions=actions, confidence=conf)


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--workspace")
    args = ap.parse_args()
    d = should_speculate(args.benchmark, args.workspace)
    print(json.dumps({"speculate": d.speculate, "reason": d.reason,
                      "actions": d.actions, "confidence": d.confidence}, indent=2))
