#!/usr/bin/env python3
"""llm_gate.py — LLM speculation gate on TWO signals: the problem
statement and the FIRST INFORMATION THE AGENT EMITS -- its opening
reasoning/output snippet from the codex --json event stream, which arrives
seconds before any command and exists even on trajectories that never run
one. The first shell command remains a fallback signal if the stream
yields nothing in time. Replaces spec_gate.py's surface heuristics.

Runs as a supervisor: reads the statement, waits (bounded) for the agent's
first emitted snippet, makes ONE small LLM call,
and on GO exec()s into the worker command given after `--` -- so the pid
the harness recorded stays valid and teardown kill works unchanged. On
NOGO it exits quietly. Fails OPEN on any error: a wrong GO costs one
predictor call; a wrong NOGO is never corrected by anything.

Writes a gate.json with the verdict, reason, first action, and latency,
preserving the old observability contract.

Usage:
  llm_gate.py --problem-statement FILE --commands-log FILE \
              [--gate-json FILE] [--timeout 90] -- <worker argv...>
Env: SPEC_LLM_BIN (default: codex)
"""
import argparse
import json
import os
import subprocess
import re
import sys
import time

BOILERPLATE = ("shell_snapshots", 'if [ -z "$BASH_ENV"', "Snapshot file")
MIN_SNIPPET = 60     # chars of agent text considered a usable signal
MAX_SNIPPET = 700

PROMPT = """Answer with exactly one word, YES or NO, on the last line.

An agent is working on the task below. Speculative execution is worth
enabling only if the agent will likely MODIFY files or an environment in a
shell workspace and then RUN commands (tests, builds, scripts, checks) to
verify its work -- an edit-and-verify loop. Pure question answering,
research, or writing tasks are not worth it.

TASK:
---
{stmt}
---
The first thing the agent said/did was:
---
{action}
---
Will this trajectory likely contain an edit-and-verify loop? YES or NO:"""


_UUIDISH = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,}$")
_LIFECYCLE = re.compile(r"^[a-z_]+\.[a-z_]+$")     # thread.started, turn.completed
_STATUSY = {"in_progress", "completed", "failed", "agent_message",
            "command_execution", "file_change", "reasoning", "update", "add",
            "delete"}
_WRAP = re.compile(r"^.*?codex_persistent_shell_wrap\.sh\s+\S+\s+-lc\s+",
                   re.DOTALL)


def _clean_command(cmd):
    """Strip the persistent-shell wrapper prefix and one quote layer."""
    c = _WRAP.sub("", cmd or "").strip()
    if len(c) >= 2 and c[0] == c[-1] and c[0] in "'\"":
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
    return " ".join(out)


def first_signal(stream_path, commands_path, deadline):
    """Primary: first MIN_SNIPPET chars of agent-emitted text from the
    event stream. Fallback: first real command. Returns (kind, text)."""
    s_off = 0
    c_seen = 0
    buf = []
    while time.time() < deadline:
        if stream_path:
            try:
                with open(stream_path, errors="replace") as f:
                    f.seek(s_off)
                    chunk = f.read()
                    s_off = f.tell()
                for ln in chunk.splitlines():
                    txt = _event_text(ln)
                    if txt:
                        buf.append(txt)
                joined = " ".join(buf)
                if len(joined) >= MIN_SNIPPET:
                    return "stream", joined[:MAX_SNIPPET]
            except OSError:
                pass
        if commands_path:
            try:
                lines = open(commands_path, errors="replace").read().splitlines()
            except OSError:
                lines = []
            for ln in lines[c_seen:]:
                c_seen += 1
                try:
                    cmd = json.loads(ln).get("cmd", "")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cmd and not any(b in cmd for b in BOILERPLATE):
                    return "command", cmd[:MAX_SNIPPET]
        time.sleep(1.0)
    joined = " ".join(buf)
    return ("stream", joined[:MAX_SNIPPET]) if joined else (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--problem-statement", required=True)
    ap.add_argument("--agent-stream", default=None,
                    help="codex --json stdout.jsonl: PRIMARY signal, the "
                         "agent's first emitted snippet")
    ap.add_argument("--commands-log", default=None,
                    help="fallback signal: first real shell command")
    ap.add_argument("--gate-json", default=None)
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="max seconds to wait for the agent's first action")
    ap.add_argument("--statement-only", action="store_true",
                    help="decide from the problem statement alone; skip the "
                         "stream/command wait (SWE-bench: statement decides, "
                         "the wait only trims worker runway)")
    ap.add_argument("worker", nargs=argparse.REMAINDER,
                    help="-- followed by the worker argv to exec on GO")
    args = ap.parse_args()
    worker = args.worker[1:] if args.worker[:1] == ["--"] else args.worker

    t0 = time.time()
    verdict, reason, action, kind = "GO", "fail-open default", None, None
    gate_tokens, gate_chars = None, None
    if args.gate_json:                 # killed-before-decision must be visible
        try:
            with open(args.gate_json, "w") as f:
                json.dump({"speculate": None,
                           "reason": "pending (if this record persists, the "
                                     "task ended before the gate decided)",
                           "gate": "llm_gate_v2"}, f, indent=1)
        except OSError:
            pass
    try:
        stmt = open(args.problem_statement, errors="replace").read()[:6000]
        if args.statement_only:
            kind, action = "statement-only", None
        else:
            kind, action = first_signal(args.agent_stream, args.commands_log,
                                        t0 + args.timeout)
        llm = os.environ.get("SPEC_LLM_BIN", "codex")
        r = subprocess.run(
            [llm, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             PROMPT.format(stmt=stmt,
                           action=(f"[{kind}] {action}" if action
                                   else "(not observed; judge from the task "
                                        "alone)"))],
            capture_output=True, text=True, timeout=90)
        gate_out = (r.stdout or "") + (r.stderr or "")
        _m = re.findall(r"(?:tokens?[ _]used|total[ _]tokens)\D{0,4}([\d,]+)",
                        gate_out, re.IGNORECASE)
        _tok = max((int(x.replace(",", "")) for x in _m), default=0)
        gate_tokens = ({"total": _tok, "estimated": False} if _tok else
                       {"total": int((len(PROMPT) + len(stmt)
                                      + len(gate_out)) / 4),
                        "estimated": True})
        gate_chars = {"prompt": len(PROMPT) + len(stmt),
                      "answer": len(gate_out)}
        words = [w.strip().upper().strip(".:,;!?'\"`)(*_")
                 for w in (r.stdout or "").split()]  # spec-score-v1
        ans = next((w for w in reversed(words) if w in ("YES", "NO")), None)
        if ans == "NO":
            verdict, reason = "NOGO", "llm: no edit-and-verify loop expected"
        elif ans == "YES":
            verdict, reason = "GO", "llm: edit-and-verify loop expected"
        else:
            reason = "llm answer unparseable, failing open"
    except Exception as e:
        reason = f"gate error ({type(e).__name__}: {e}), failing open"

    shadow = os.environ.get("SPEC_GATE_SHADOW", "0") == "1"
    rec = {"speculate": verdict == "GO", "reason": reason,
           "first_action": (action or "")[:300],
           "signal": kind,
           "gate_latency_s": round(time.time() - t0, 1),
           "tokens": gate_tokens, "chars": gate_chars,
           "shadow": shadow,
           "gate": "llm_gate_v2"}
    if args.gate_json:
        try:
            with open(args.gate_json, "w") as f:
                json.dump(rec, f, indent=1)
        except OSError:
            pass
    print(f"[gate] {verdict}: {reason} (first_action={rec['first_action'][:80]!r}, "
          f"{rec['gate_latency_s']}s)", flush=True)

    if worker and (verdict == "GO" or shadow):
        # shadow: verdict recorded above, speculation runs regardless so the
        # decision can be SCORED against realized serve value
        os.environ["SPEC_UPSTREAM_GATE"] = "GO"   # worker: skip internal re-gate
        os.execvp(worker[0], worker)   # become the worker: pid continuity
    if os.environ.get("SPEC_CPU_OUT"):            # enforced NOGO: gate's own
        try:                                       # LLM-call CPU still counts
            import resource
            su = resource.getrusage(resource.RUSAGE_SELF)
            ch = resource.getrusage(resource.RUSAGE_CHILDREN)
            with open(os.path.join(os.environ["SPEC_CPU_OUT"],
                                   "cpu_gate_nogo.json"), "w") as f:
                json.dump({"tag": "gate_nogo",
                           "cpu_total_s": round(su.ru_utime + su.ru_stime
                                                + ch.ru_utime + ch.ru_stime,
                                                3)}, f)
        except OSError:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
