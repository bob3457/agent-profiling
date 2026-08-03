#!/usr/bin/env python3
"""patch_llm_gate_v2.py — fix the stream tap losing to its own noise, and
stop paying the stream wait on SWE-bench.

Evidence (run arm_C.20260731_183452, broken-python): gate.json read
  first_action: "thread.started 019fb975-... turn.started"
The leaf-walk in _event_text kept "thread.started" (14 chars, no
BOILERPLATE hit), the 36-char thread UUID, and "turn.started" -- 65 chars
joined >= MIN_SNIPPET=60, so first_signal returned lifecycle garbage one
event before the agent's actual opening message ("I'll inspect the parser
and its input, make the smallest targeted fix, then verify...").

Three edits, all in place:

1. llm_gate.py _event_text -> schema-aware: agent_message / reasoning
   text is the primary signal; a command_execution's command (persistent
   shell wrapper stripped) is the secondary; lifecycle and file_change
   events yield nothing. The leaf-walk survives ONLY as a format-drift
   fallback, hardened against UUIDs, dotted event names, and status words.
2. llm_gate.py --statement-only flag: skip first_signal entirely, prompt
   from the statement alone, record signal kind in gate.json. Gate id
   bumped to llm_gate_v2; gate.json gains a "signal" field either way.
3. run_latency_arm.sh: SWE-bench launches the gate with --statement-only
   (statement decides alone there; the stream wait is pure runway loss).

Idempotent; asserts verbatim anchors; refuses to write on drift.
"""
import sys
from pathlib import Path

OPT = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    Path("/projects/kzhou6/czhai/agent-profiling/latency-opt")
GATE = OPT / "speculation" / "llm_gate.py"
HARNESS = OPT / "harness" / "run_latency_arm.sh"

# ================================================================ llm_gate.py
t = GATE.read_text()
if "_LIFECYCLE" in t:
    print("llm_gate.py: already patched, no-op")
else:
    # ---- 1a. import re ------------------------------------------------------
    A_IMP = "import sys\nimport time"
    assert A_IMP in t, "ANCHOR drifted: import block"
    t = t.replace(A_IMP, "import re\nimport sys\nimport time", 1)

    # ---- 1b. replace _event_text -------------------------------------------
    A_EVT = '''def _event_text(line):
    """Schema-agnostic: concatenate string leaves of one event, skipping
    boilerplate. Tolerates codex --json format drift."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    out = []

    def walk(v):
        if isinstance(v, str):
            if len(v) >= 8 and not any(b in v for b in BOILERPLATE):
                out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(obj)
    return " ".join(out)'''
    N_EVT = '''_UUIDISH = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,}$")
_LIFECYCLE = re.compile(r"^[a-z_]+\\.[a-z_]+$")     # thread.started, turn.completed
_STATUSY = {"in_progress", "completed", "failed", "agent_message",
            "command_execution", "file_change", "reasoning", "update", "add",
            "delete"}
_WRAP = re.compile(r"^.*?codex_persistent_shell_wrap\\.sh\\s+\\S+\\s+-lc\\s+",
                   re.DOTALL)


def _clean_command(cmd):
    """Strip the persistent-shell wrapper prefix and one quote layer."""
    c = _WRAP.sub("", cmd or "").strip()
    if len(c) >= 2 and c[0] == c[-1] and c[0] in "'\\"":
        c = c[1:-1]
    return c.strip()


def _event_text(line):
    """Schema-aware: agent_message/reasoning text is the signal; a
    command_execution's command (wrapper stripped) is secondary; lifecycle
    and file_change events yield nothing. Leaf-walk kept ONLY as a
    format-drift fallback, hardened against UUIDs/event-names/status words
    (run 20260731_183452: 'thread.started <uuid> turn.started' outran the
    agent's real opening message by one event)."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    item = obj.get("item") if isinstance(obj, dict) else None
    if isinstance(item, dict):
        it = item.get("type")
        if it in ("agent_message", "reasoning"):
            return (item.get("text") or "").strip()
        if it == "command_execution" and obj.get("type") == "item.started":
            c = _clean_command(item.get("command"))
            return f"$ {c}" if c else ""
        return ""                       # file_change etc.: no gate signal
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        return ""                       # bare lifecycle event
    out = []                            # drift fallback: hardened leaf-walk

    def walk(v):
        if isinstance(v, str):
            s = v.strip()
            if (len(s) >= 8 and not any(b in s for b in BOILERPLATE)
                    and not _UUIDISH.match(s) and not _LIFECYCLE.match(s)
                    and s not in _STATUSY):
                out.append(s)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(obj)
    return " ".join(out)'''
    assert A_EVT in t, "ANCHOR drifted: _event_text body"
    t = t.replace(A_EVT, N_EVT, 1)

    # ---- 2a. --statement-only flag ------------------------------------------
    A_ARG = '''    ap.add_argument("--timeout", type=float, default=90.0,
                    help="max seconds to wait for the agent's first action")'''
    N_ARG = A_ARG + '''
    ap.add_argument("--statement-only", action="store_true",
                    help="decide from the problem statement alone; skip the "
                         "stream/command wait (SWE-bench: statement decides, "
                         "the wait only trims worker runway)")'''
    assert A_ARG in t, "ANCHOR drifted: --timeout argparse"
    t = t.replace(A_ARG, N_ARG, 1)

    # ---- 2b. short-circuit first_signal --------------------------------------
    A_SIG = '''    t0 = time.time()
    verdict, reason, action = "GO", "fail-open default", None
    try:
        stmt = open(args.problem_statement, errors="replace").read()[:6000]
        kind, action = first_signal(args.agent_stream, args.commands_log,
                                    t0 + args.timeout)'''
    N_SIG = '''    t0 = time.time()
    verdict, reason, action, kind = "GO", "fail-open default", None, None
    try:
        stmt = open(args.problem_statement, errors="replace").read()[:6000]
        if args.statement_only:
            kind, action = "statement-only", None
        else:
            kind, action = first_signal(args.agent_stream, args.commands_log,
                                        t0 + args.timeout)'''
    assert A_SIG in t, "ANCHOR drifted: first_signal call site"
    t = t.replace(A_SIG, N_SIG, 1)

    # ---- 2c. neutral prompt line when no action ------------------------------
    A_PRM = '''                           action=(f"[{kind}] {action}" if action
                                   else "(nothing within timeout)"))],'''
    N_PRM = '''                           action=(f"[{kind}] {action}" if action
                                   else "(not observed; judge from the task "
                                        "alone)"))],'''
    assert A_PRM in t, "ANCHOR drifted: prompt action formatting"
    t = t.replace(A_PRM, N_PRM, 1)

    # ---- 2d. record signal kind, bump gate id --------------------------------
    A_REC = '''    rec = {"speculate": verdict == "GO", "reason": reason,
           "first_action": (action or "")[:300],
           "gate_latency_s": round(time.time() - t0, 1),
           "gate": "llm_gate_v1"}'''
    N_REC = '''    rec = {"speculate": verdict == "GO", "reason": reason,
           "first_action": (action or "")[:300],
           "signal": kind,
           "gate_latency_s": round(time.time() - t0, 1),
           "gate": "llm_gate_v2"}'''
    assert A_REC in t, "ANCHOR drifted: gate.json rec block"
    t = t.replace(A_REC, N_REC, 1)

    GATE.write_text(t)
    print("llm_gate.py: schema-aware tap, --statement-only, gate id v2")

import py_compile
py_compile.compile(str(GATE), doraise=True)
print("llm_gate.py compiles OK")

# ============================================================ run_latency_arm.sh
h = HARNESS.read_text()
if "GATE_XTRA" in h:
    print("run_latency_arm.sh: already patched, no-op")
else:
    A_H = '''      nohup python3 -u "$OPT/speculation/llm_gate.py" \\
        --problem-statement "$run_dir/problem.txt" \\
        --agent-stream "$run_dir/stdout.jsonl" \\
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \\
        --gate-json "$run_dir/gate.json" --timeout 90 \\'''
    N_H = '''      GATE_XTRA=""
      [[ $bench == swebench ]] && GATE_XTRA="--statement-only"
      nohup python3 -u "$OPT/speculation/llm_gate.py" \\
        --problem-statement "$run_dir/problem.txt" \\
        --agent-stream "$run_dir/stdout.jsonl" \\
        --commands-log "$run_dir/shelld_logs/commands.jsonl" \\
        --gate-json "$run_dir/gate.json" --timeout 90 $GATE_XTRA \\'''
    assert A_H in h, "ANCHOR drifted: harness gate launch block"
    HARNESS.write_text(h.replace(A_H, N_H, 1))
    print("run_latency_arm.sh: SWE-bench gate launches --statement-only")
print("done")
