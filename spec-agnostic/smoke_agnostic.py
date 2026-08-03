#!/usr/bin/env python3
"""smoke_agnostic.py — offline acceptance for the task-agnostic speculator.

Run AFTER deploying (spec_tiers.py copied, three patchers applied):
    python3 smoke_agnostic.py [repo_root]

What it does, with no codex / no network / no daemon:
  1. spec_tiers v2 self-test (the full 116-case suite).
  2. corpus_coverage on an embedded mixed corpus; asserts v2 covers
     strictly more than a floor and specific known commands land where
     they must.
  3. speculative_worker end-to-end in a temp GENERIC workspace (csv + shell
     script, no python project, no git), SPEC_UPSTREAM_GATE=GO, with
     SPEC_LLM_BIN pointing at a stub that "predicts" a mix of safe, unsafe,
     and compound commands. Asserts:
       - recon cached ls/cat/wc of the workspace files
       - safe LLM predictions (wc, sqlite-style read) cached verbatim
       - unsafe predictions (rm, ./run.sh, pip install) NOT cached
       - compound prediction: TIER0 parts cached, mutating part not
  4. llm_predictor mode selection: generic ws -> mode=generic (5-cmd cap,
     command-shaped filter); pytest-shaped ws -> mode=tests.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1
            else "/projects/kzhou6/czhai/agent-profiling")
SPEC = ROOT / "latency-opt/speculation"
HERE = Path(__file__).resolve().parent
PY = sys.executable
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ---- 1. tiers self-test ------------------------------------------------------
r = subprocess.run([PY, str(SPEC / "spec_tiers.py")], capture_output=True, text=True)
check("tiers v2 self-test", r.returncode == 0, r.stdout.splitlines()[-1] if r.stdout else r.stderr[-200:])

# ---- 2. corpus coverage ------------------------------------------------------
corpus = """\
ls -la
cat README.md
pwd; ls -la
git diff --check; git status --short; python -m pytest tests/test_io.py -q
sed -n 1,40p src/main.py
wc -l data.csv && head -5 data.csv
python solve.py --input data.csv
tar xzf release.tar.gz && cd release && make
sqlite3 app.db 'select count(*) from users'
cd src && python -m pytest -x -q
echo done > /tmp/marker
sort data.txt | uniq -c
grep -E 'foo|bar' src/main.py
"""
with tempfile.TemporaryDirectory() as td:
    cf = Path(td) / "corpus.txt"
    cf.write_text(corpus)
    r = subprocess.run([PY, str(HERE / "corpus_coverage.py"), str(cf),
                        "--spec-dir", str(SPEC)],
                       capture_output=True, text=True)
    check("corpus_coverage runs", r.returncode == 0, r.stderr[-200:])
    out = r.stdout
    # 13 commands; with v2 deployed the servable set is:
    # ls -la / cat README / pwd;ls / git-triple / sed -n / wc&&head /
    # sqlite select / cd&&pytest (folded) / grep quoted-pipe  = 9
    line = next((l for l in out.splitlines() if l.startswith("ANY servable")), "")
    import re as _re
    m = _re.search(r"(\d+)/(\d+)", line)
    check("corpus coverage >= 10/13", bool(m) and int(m.group(1)) >= 10, line)

# ---- 3. worker end-to-end (generic workspace, stub LLM) ----------------------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    ws = td / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("Fix the report generator.\n")
    (ws / "data.csv").write_text("id,val\n1,2\n3,4\n")
    (ws / "run.sh").write_text("#!/bin/sh\necho run\n")
    (ws / "notes").mkdir()
    (ws / "notes" / "todo.txt").write_text("todo\n")

    stub = td / "codex"
    stub.write_text("""#!/bin/bash
# stub predictor: ignores args, emits 6 'predictions' as plain lines
cat <<'EOF'
wc -l data.csv
rm -rf notes
./run.sh
head -2 data.csv && sed -i s/2/9/ data.csv
grep val data.csv && cat notes/todo.txt
pip install pandas
EOF
""")
    stub.chmod(0o755)

    cache = td / "cache"
    env = dict(os.environ,
               SPEC_UPSTREAM_GATE="GO",
               SPEC_LLM_BIN=str(stub),
               SPEC_LLM_ARGS="",
               SPEC_PREDICT_MODE="generic")
    prob = td / "problem.txt"
    prob.write_text("Fix the report generator so totals are right.")
    r = subprocess.run([PY, str(SPEC / "speculative_worker.py"),
                        "--workspace", str(ws), "--cache-dir", str(cache),
                        "--benchmark", "terminalbench", "--predictor", "llm",
                        "--problem-statement", str(prob), "--nice", "0"],
                       capture_output=True, text=True, timeout=300, env=env)
    log = r.stdout + r.stderr
    check("worker exits clean", r.returncode == 0, log[-400:])

    entries = {}
    for f in cache.glob("*.json"):
        try:
            e = json.loads(f.read_text())
            entries[e.get("cmd", "")] = e
        except (json.JSONDecodeError, OSError):
            pass
    cached = set(entries)

    check("recon: ls cached", "ls" in cached and "ls -la" in cached)
    check("recon: cat README cached", "cat README.md" in cached)
    check("recon: cat data.csv cached", "cat data.csv" in cached)
    check("recon: subdir listed", "ls notes" in cached)
    check("llm direct: wc cached verbatim", "wc -l data.csv" in cached)
    check("llm direct: compound tier0 part cached", "head -2 data.csv" in cached)
    check("llm direct: grep part cached", "grep val data.csv" in cached)
    check("llm direct: cat via compound cached", "cat notes/todo.txt" in cached)
    check("safety: sed -i part NOT cached",
          not any("sed -i" in c for c in cached))
    check("safety: rm NOT cached", not any(c.startswith("rm") for c in cached))
    check("safety: ./run.sh NOT cached", "./run.sh" not in cached)
    check("safety: pip install NOT cached",
          not any(c.startswith("pip install") for c in cached))
    check("safety: workspace untouched",
          (ws / "data.csv").read_text() == "id,val\n1,2\n3,4\n"
          and (ws / "notes").exists())
    wc = entries.get("wc -l data.csv", {})
    check("entry sane: wc output + duration",
          wc.get("exit") == 0 and "3" in wc.get("stdout", "")
          and wc.get("duration_s") is not None, json.dumps(wc)[:200])

# ---- 4. predictor mode selection ---------------------------------------------
sys.path.insert(0, str(SPEC))
import importlib  # noqa: E402
lp = importlib.import_module("llm_predictor")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    g = td / "generic"; g.mkdir(); (g / "data.csv").write_text("x\n")
    t = td / "testy"; (t / "pkg").mkdir(parents=True)
    for i in range(3):
        (t / "pkg" / f"test_m{i}.py").write_text("def test_a(): pass\n")
    check("mode auto->generic", not lp._has_test_surface(g))
    check("mode auto->tests", lp._has_test_surface(t))
    check("filter keeps command", lp._looks_like_command("wc -l data.csv"))
    check("filter drops prose",
          not lp._looks_like_command("The agent will probably start by listing files"))
    check("filter drops empty/comment",
          not lp._looks_like_command("") and not lp._looks_like_command("# note"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL PASS")
