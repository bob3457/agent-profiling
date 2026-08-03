#!/usr/bin/env python3
"""patch_measure.py — instrument the stack for the four-measurement campaign.

Gate accuracy needs the gate's verdict to be SCORABLE against realized
value, which means speculation must run even on NOGO. Cost accounting needs
CPU and tokens that today evaporate (components die by SIGTERM; the gate's
LLM call reports no usage). This patcher adds, without changing any default
behavior:

  llm_gate.py            SPEC_GATE_SHADOW=1 -> record verdict, exec worker
                         regardless (gate.json gains "shadow"); token/char
                         accounting on the gate's LLM call; cpu_gate.json on
                         the enforced-NOGO exit when SPEC_CPU_OUT is set
  speculative_worker.py  rusage dump (self+children) to
                         $SPEC_CPU_OUT/cpu_$SPEC_CPU_TAG.json on exit/SIGTERM
  edit_respec.py         same, default tag "respec"
  run_latency_arm.sh     freshqa case (FRESHQA_MANIFEST, mirrors hotpotqa);
                         SPEC_ALL_BENCH=1 enables arm-C speculation on all
                         benchmarks; SPEC_CPU_OUT/SPEC_CPU_TAG wiring

Verbatim anchors; idempotent.   Usage: patch_measure.py [repo_root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
OPT = ROOT / "latency-opt"
DONE, SKIP = [], []


def patch(path: Path, anchor: str, replacement: str, marker: str, label: str):
    src = path.read_text()
    if marker in src:
        SKIP.append(label)
        return
    assert anchor in src, f"{label}: anchor not found in {path}"
    assert src.count(anchor) == 1, f"{label}: anchor not unique in {path}"
    path.write_text(src.replace(anchor, replacement))
    DONE.append(label)


CPU_DUMP = '''
    # ---- speculation CPU accounting (SPEC_CPU_OUT) ---------------------------
    _cpu_out = os.environ.get("SPEC_CPU_OUT")
    if _cpu_out:
        import atexit
        import resource
        import signal as _sig
        _cpu_t0 = time.time()

        def _dump_cpu(*_a):
            try:
                su = resource.getrusage(resource.RUSAGE_SELF)
                ch = resource.getrusage(resource.RUSAGE_CHILDREN)
                rec = {"tag": os.environ.get("SPEC_CPU_TAG", "{TAG}"),
                       "utime_s": round(su.ru_utime, 3),
                       "stime_s": round(su.ru_stime, 3),
                       "children_utime_s": round(ch.ru_utime, 3),
                       "children_stime_s": round(ch.ru_stime, 3),
                       "cpu_total_s": round(su.ru_utime + su.ru_stime
                                            + ch.ru_utime + ch.ru_stime, 3),
                       "maxrss_kb": max(su.ru_maxrss, ch.ru_maxrss),
                       "wall_s": round(time.time() - _cpu_t0, 3)}
                _tag = rec["tag"]
                with open(os.path.join(_cpu_out, f"cpu_{_tag}.json"), "w") as f:
                    json.dump(rec, f)
            except OSError:
                pass
            if _a:                      # SIGTERM path: dump then die
                os._exit(0)

        atexit.register(_dump_cpu)
        _sig.signal(_sig.SIGTERM, _dump_cpu)
'''

# ---- 1. worker: rusage on exit/SIGTERM ----------------------------------------
patch(OPT / "speculation/speculative_worker.py",
      "    args = ap.parse_args()\n\n    ws = Path(args.workspace).resolve()",
      "    args = ap.parse_args()\n"
      + CPU_DUMP.replace("{TAG}", "spec_worker")
      + "\n    ws = Path(args.workspace).resolve()",
      "speculation CPU accounting (SPEC_CPU_OUT)",
      "worker cpu dump")

# ---- 2. respec: same -----------------------------------------------------------
patch(OPT / "speculation/edit_respec.py",
      "    args = ap.parse_args()\n",
      "    args = ap.parse_args()\n" + CPU_DUMP.replace("{TAG}", "respec") + "\n",
      "speculation CPU accounting (SPEC_CPU_OUT)",
      "respec cpu dump")

# ---- 3. gate: token capture ----------------------------------------------------
patch(OPT / "speculation/llm_gate.py",
      '        words = [w.strip().upper() for w in (r.stdout or "").split()]',
      '''        gate_out = (r.stdout or "") + (r.stderr or "")
        _m = re.findall(r"(?:tokens?[ _]used|total[ _]tokens)\\D{0,4}([\\d,]+)",
                        gate_out, re.IGNORECASE)
        _tok = max((int(x.replace(",", "")) for x in _m), default=0)
        gate_tokens = ({"total": _tok, "estimated": False} if _tok else
                       {"total": int((len(PROMPT) + len(stmt)
                                      + len(gate_out)) / 4),
                        "estimated": True})
        gate_chars = {"prompt": len(PROMPT) + len(stmt),
                      "answer": len(gate_out)}
        words = [w.strip().upper() for w in (r.stdout or "").split()]''',
      "gate_tokens = (",
      "gate token capture")

# ---- 4. gate: init token vars before the try -----------------------------------
patch(OPT / "speculation/llm_gate.py",
      '    t0 = time.time()\n'
      '    verdict, reason, action, kind = "GO", "fail-open default", None, None',
      '    t0 = time.time()\n'
      '    verdict, reason, action, kind = "GO", "fail-open default", None, None\n'
      '    gate_tokens, gate_chars = None, None',
      'gate_tokens, gate_chars = None, None',
      "gate token init")

# ---- 5. gate: tokens into gate.json + shadow mode ------------------------------
patch(OPT / "speculation/llm_gate.py",
      '''    rec = {"speculate": verdict == "GO", "reason": reason,
           "first_action": (action or "")[:300],
           "signal": kind,
           "gate_latency_s": round(time.time() - t0, 1),
           "gate": "llm_gate_v2"}''',
      '''    shadow = os.environ.get("SPEC_GATE_SHADOW", "0") == "1"
    rec = {"speculate": verdict == "GO", "reason": reason,
           "first_action": (action or "")[:300],
           "signal": kind,
           "gate_latency_s": round(time.time() - t0, 1),
           "tokens": gate_tokens, "chars": gate_chars,
           "shadow": shadow,
           "gate": "llm_gate_v2"}''',
      'SPEC_GATE_SHADOW',
      "gate shadow flag + tokens in gate.json")

patch(OPT / "speculation/llm_gate.py",
      '''    if verdict == "GO" and worker:
        os.environ["SPEC_UPSTREAM_GATE"] = "GO"   # worker: skip internal re-gate
        os.execvp(worker[0], worker)   # become the worker: pid continuity
    sys.exit(0)''',
      '''    if worker and (verdict == "GO" or shadow):
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
    sys.exit(0)''',
      "verdict == \"GO\" or shadow",
      "gate shadow exec + NOGO cpu")

# ---- 5. harness: FRESHQA_MANIFEST default --------------------------------------
patch(OPT / "harness/run_latency_arm.sh",
      'HOTPOT_MANIFEST=${HOTPOT_MANIFEST:-$(ls $ROOT/manifests/hotpotqa*.tsv '
      '2>/dev/null | head -1)}',
      'HOTPOT_MANIFEST=${HOTPOT_MANIFEST:-$(ls $ROOT/manifests/hotpotqa*.tsv '
      '2>/dev/null | head -1)}\n'
      'FRESHQA_MANIFEST=${FRESHQA_MANIFEST:-$(ls $ROOT/manifests/'
      'freshqa_cpu_study_*.tsv 2>/dev/null | head -1)}',
      'FRESHQA_MANIFEST=',
      "harness FRESHQA_MANIFEST")

# ---- 6. harness: freshqa case (mirrors hotpotqa) --------------------------------
patch(OPT / "harness/run_latency_arm.sh",
      '''      prompt=$(cat "$ROOT/$pf")
      workdir=$run_dir/work; mkdir -p "$workdir"
      ;;
    swebench)''',
      '''      prompt=$(cat "$ROOT/$pf")
      workdir=$run_dir/work; mkdir -p "$workdir"
      ;;
    freshqa)
      # manifest columns: qid <TAB> base_task_path <TAB> prompt_file
      local fpf
      fpf=$(awk -F'\\t' -v q="$tid" '$1==q {print $3; exit}' "$FRESHQA_MANIFEST")
      [[ -z "$fpf" ]] && { echo "  SKIP: qid $tid not in manifest"; return; }
      [[ -f "$ROOT/$fpf" ]] || { echo "  SKIP: prompt file missing: $ROOT/$fpf"; return; }
      prompt=$(cat "$ROOT/$fpf")
      workdir=$run_dir/work; mkdir -p "$workdir"
      ;;
    swebench)''',
      '    freshqa)',
      "harness freshqa case")

# ---- 7. harness: SPEC_ALL_BENCH widens arm-C speculation -------------------------
patch(OPT / "harness/run_latency_arm.sh",
      'if [[ $ARM == C && ( $bench == swebench || $bench == terminalbench ) ]]; '
      'then',
      'if [[ $ARM == C && ( $bench == swebench || $bench == terminalbench '
      '|| "${SPEC_ALL_BENCH:-0}" == "1" ) ]]; then',
      'SPEC_ALL_BENCH',
      "harness SPEC_ALL_BENCH")

# ---- 8. harness: SPEC_CPU_OUT + per-component tags -------------------------------
patch(OPT / "harness/run_latency_arm.sh",
      '      export CODEX_SHELLD_SPEC=$run_dir/spec_cache',
      '      export CODEX_SHELLD_SPEC=$run_dir/spec_cache\n'
      '      export SPEC_CPU_OUT=$run_dir',
      'export SPEC_CPU_OUT=',
      "harness SPEC_CPU_OUT")

patch(OPT / "harness/run_latency_arm.sh",
      '      SPEC_DIRECT_ONLY=1 nohup python3 -u '
      '"$OPT/speculation/speculative_worker.py"',
      '      SPEC_DIRECT_ONLY=1 SPEC_CPU_TAG=spec_early nohup python3 -u '
      '"$OPT/speculation/speculative_worker.py"',
      'SPEC_CPU_TAG=spec_early',
      "harness early-worker cpu tag")

patch(OPT / "harness/run_latency_arm.sh",
      '      nohup python3 -u "$OPT/speculation/llm_gate.py"',
      '      SPEC_CPU_TAG=spec_worker nohup python3 -u '
      '"$OPT/speculation/llm_gate.py"',
      'SPEC_CPU_TAG=spec_worker',
      "harness gated-worker cpu tag")

print(f"applied: {DONE}")
print(f"already present: {SKIP}")
