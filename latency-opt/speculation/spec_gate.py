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



# --------------------------------------------------------------- statement gate
# Generalized stage 1: decide from the problem statement ITSELF whether the
# task involves predictable local work, instead of a hardcoded benchmark
# table. The table is kept only as a fallback when no statement is available.
import re as _re

_LOCAL_SIGNALS = [
    (r"[\w/]+\.(py|js|ts|rs|go|c|cpp|h|java|rb|sh|toml|cfg|ini|yaml|yml)\b", 2.0, "file paths"),
    (r"```", 1.5, "code blocks"),
    (r"Traceback \(most recent call last\)|\bstack trace\b", 2.0, "stack trace"),
    (r"\b(pytest|unittest|test suite|failing test|run(s|ning)? the tests?|runtests\.py)\b", 2.0, "test framework"),
    (r"\b(repo|repository|codebase|checkout|branch|commit|diff|patch)\b", 1.5, "repo vocabulary"),
    (r"\b(build|compile|make|install(ing)? (the )?(dependenc|package)|pip install|npm install|cargo)\b", 1.5, "build/install"),
    (r"\b(fix|bug|implement|refactor|modify|edit) (the |this )?(code|function|class|method|module|file|issue)\b", 1.5, "code-change ask"),
    (r"(\$ |\bbash\b|\bshell command\b|command line|\bterminal\b)", 1.0, "shell vocabulary"),
    (r"\b(directory|folder|filesystem|/testbed|working directory)\b", 1.0, "filesystem vocabulary"),
    (r"\b(log file|\.log\b|csv|parquet|dataset\.txt|profile the|cProfile)\b", 1.0, "local data/tooling"),
]
_REMOTE_SIGNALS = [
    (r"\b(who|what|when|where|which|whose|whom)\b.{0,120}\?", 1.5, "question form"),
    (r"\banswer (the|this|each) question\b", 2.5, "QA instruction"),
    (r"\b(web search|search the web|wikipedia|look up online|browse)\b", 2.5, "web-search instruction"),
    (r"\bno (context|passages?|files?)( \w+)* (are |is )?provided\b", 2.0, "no local context"),
    (r"\b(current|latest|as of|recent(ly)?) (news|events|information)\b", 1.0, "freshness ask"),
]


def analyze_problem_statement(text: str) -> dict:
    """Score the statement for local-work vs remote/knowledge-work evidence.
    Returns {local, remote, hits: [(signal, weight)], verdict}."""
    local, remote, hits = 0.0, 0.0, []
    low = text.lower()
    for pat, w, name in _LOCAL_SIGNALS:
        if _re.search(pat, text if pat.startswith("[") or "Traceback" in pat else low):
            local += w
            hits.append(("local:" + name, w))
    for pat, w, name in _REMOTE_SIGNALS:
        if _re.search(pat, low):
            remote += w
            hits.append(("remote:" + name, w))
    if local >= 2.0 and local > remote:
        verdict = "local"
    elif remote > local:
        verdict = "remote"
    else:
        verdict = "unclear"
    return {"local": round(local, 1), "remote": round(remote, 1),
            "hits": hits, "verdict": verdict}


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
    f["django_runner"] = (ws / "tests" / "runtests.py").exists()
    try:
        f["n_top_entries"] = len(list(ws.iterdir()))
    except OSError:
        f["n_top_entries"] = 0
    return f


def should_speculate(benchmark: str, workspace: str | None = None,
                     task_text: str = "", ledger_dir: str | None = None) -> GateDecision:
    benchmark = benchmark.lower().replace("-", "").replace("_", "")
    if task_text.strip():
        # Stage 1 (generalized): decide from the problem statement itself
        a = analyze_problem_statement(task_text)
        sig = ", ".join(n for n, _ in a["hits"][:6]) or "no signals"
        if a["verdict"] == "remote":
            return GateDecision(False,
                f"statement analysis: remote/knowledge work (local={a['local']}, "
                f"remote={a['remote']}; {sig})")
        if a["verdict"] == "unclear" and workspace is None:
            return GateDecision(False,
                f"statement analysis inconclusive and no workspace (local={a['local']}, "
                f"remote={a['remote']}; {sig})")
        why = (f"statement analysis: local work indicated (local={a['local']}, "
               f"remote={a['remote']}; {sig})")
    else:
        # Fallback stage 1: benchmark prior table (no statement available)
        for key, (prior, why) in BENCHMARK_PRIOR.items():
            if key in benchmark:
                if not prior:
                    return GateDecision(False, f"benchmark prior: {why}")
                break
        else:
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
    if feat.get("django_runner"):
        actions += ["django_targeted"]
    elif feat.get("py_project"):
        actions += ["py_dep_preinstall", "pytest_collect", "pytest_targeted"]
        if feat.get("has_tests"):
            actions += ["pytest_run_cached"]              # pre-run tests, cache output
    if feat.get("node_project"):
        actions += ["npm_ci_prefetch"]
    if feat.get("rust_project"):
        actions += ["cargo_fetch"]

    if not actions:
        return GateDecision(False, f"workspace has no recognizable prep surface: {feat}")

    # ---- EV layer (build 4): consult the ledger where it has data ----------
    conf = 0.9 if (feat.get("py_project") and feat.get("has_tests")) else 0.6
    use_llm, llm_reason = False, "no ledger data; heuristic only (default)"
    per_test_ids = True
    if ledger_dir:
        try:
            from ledger import stats
            s = stats(ledger_dir)
            h = s.get((benchmark, "heuristic")) or s.get(("swebench", "heuristic"))
            l = s.get((benchmark, "llm")) or s.get(("swebench", "llm"))
            if h and h["n"] >= 3:
                conf = h["mean"]
                # heuristic weak -> LLM is worth invoking (explore if unproven,
                # exploit if it has demonstrated an edge)
                if h["mean"] < 0.6 and (l is None or l["n"] < 3 or l["mean"] > h["mean"]):
                    use_llm = True
                    llm_reason = (f"heuristic mean {h['mean']} < 0.6 over n={h['n']}; "
                                  + ("LLM unproven -> explore" if not l or l["n"] < 3
                                     else f"LLM mean {l['mean']} beats it -> exploit"))
                else:
                    llm_reason = f"heuristic mean {h['mean']} sufficient (n={h['n']})"
                # granularity: only descend to per-test ids if file-level
                # prediction is credible, else keep the worker fast
                per_test_ids = h["mean"] >= 0.4
        except Exception as e:  # ledger problems must never block speculation
            llm_reason = f"ledger unreadable ({e}); defaults"
    d = GateDecision(True, f"{why}; features={ {k: v for k, v in feat.items() if v} }",
                     actions=actions, confidence=conf)
    d.use_llm = use_llm
    d.llm_reason = llm_reason
    d.per_test_ids = per_test_ids
    return d


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--workspace")
    ap.add_argument("--ledger-dir", default=None)
    ap.add_argument("--problem-statement", default=None,
                    help="file with the task text; enables statement-based gating")
    args = ap.parse_args()
    text = ""
    if args.problem_statement:
        from pathlib import Path as _P
        if _P(args.problem_statement).exists():
            text = _P(args.problem_statement).read_text()
    d = should_speculate(args.benchmark, args.workspace, task_text=text,
                         ledger_dir=args.ledger_dir)
    print(json.dumps({"speculate": d.speculate, "reason": d.reason,
                      "actions": d.actions, "confidence": d.confidence,
                      "use_llm": getattr(d, "use_llm", False),
                      "llm_reason": getattr(d, "llm_reason", None),
                      "per_test_ids": getattr(d, "per_test_ids", True)}, indent=2))
