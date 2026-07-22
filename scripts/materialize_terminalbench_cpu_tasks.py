#!/usr/bin/env python3
"""Materialize 10 local Terminal-Bench-style stand-in tasks for CPU profiling.
These are dummy tasks (instruction.txt + verify.sh), NOT official
Terminal-Bench tasks -- ideal for learning the profiling pipeline cheaply."""
from pathlib import Path
import os
import stat
import textwrap

ROOT = Path(os.environ.get("PROFILING_ROOT", "/projects/kzhou6/czhai/agent-profiling"))
RUNS = ROOT / "runs" / "terminalbench"
PROMPTS = ROOT / "prompts"
MANIFEST = ROOT / "manifests" / "terminalbench_cpu_study_10.tsv"

RUNS.mkdir(parents=True, exist_ok=True)
PROMPTS.mkdir(parents=True, exist_ok=True)
MANIFEST.parent.mkdir(parents=True, exist_ok=True)


def write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def prompt_for(task_id):
    return f"""You are in a Terminal-Bench-style task workspace.\n\nYour goal:\n1. Read instruction.txt.\n2. Modify or create required files.\n3. Run ./verify.sh if it exists.\n4. Stop only after the task is complete.\n\nTask id: {task_id}\n"""


def ensure_existing_count_dataset(rows):
    task = "count-dataset-tokens"
    base = RUNS / task / "base_task"
    prompt = PROMPTS / "tbench_count_dataset_tokens.txt"
    if not base.exists():
        write(base / "instruction.txt", "Count the number of cl100k_base tokens in dataset.txt and write the integer to answer.txt.\n")
        write(base / "dataset.txt", "hello world\n" * 100)
        write(base / "verify.sh", """
        #!/usr/bin/env bash
        set -euo pipefail
        test -f answer.txt
        python3 - <<'PY'
        import tiktoken
        from pathlib import Path
        enc = tiktoken.get_encoding('cl100k_base')
        expected = len(enc.encode(Path('dataset.txt').read_text()))
        actual = int(Path('answer.txt').read_text().strip())
        assert expected == actual
        print('ok')
        PY
        """, True)
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_csv_to_parquet(rows):
    task = "csv-to-parquet"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Create convert.py that reads input.csv and writes output.parquet preserving all rows and columns. Run it once.\n")
    write(base / "input.csv", "user_id,name,score\n1,Ada,91\n2,Grace,88\n3,Linus,95\n4,Barbara,99\n")
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test -f convert.py
    test -f output.parquet
    python3 - <<'PY'
    import pandas as pd
    df = pd.read_parquet('output.parquet')
    assert list(df.columns) == ['user_id', 'name', 'score']
    assert len(df) == 4
    assert int(df['score'].sum()) == 373
    print('ok')
    PY
    """, True)
    prompt = PROMPTS / "tbench_csv_to_parquet.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_cprofiling_python(rows):
    task = "cprofiling-python"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Profile slow.py, create profile_summary.txt naming the hottest function, then optimize slow.py while preserving output.\n")
    write(base / "slow.py", """
    def inner(n):
        s = 0
        for i in range(n):
            s += (i * i) % 97
        return s

    def repeated():
        total = 0
        for _ in range(200):
            total += inner(5000)
        return total

    def main():
        print(repeated())

    if __name__ == '__main__':
        main()
    """)
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test -f profile_summary.txt
    grep -qi inner profile_summary.txt
    OUT=$(python3 slow.py)
    test "$OUT" = "48009600"
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_cprofiling_python.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_broken_python(rows):
    task = "broken-python"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Fix app.py so it prints exactly 15. The bug is in data parsing.\n")
    write(base / "data.txt", "1,2,3,4,5\n")
    write(base / "app.py", """
    def load_numbers(path):
        text = open(path).read().strip()
        return [int(x) for x in text.split()]

    print(sum(load_numbers('data.txt')))
    """)
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test "$(python3 app.py)" = "15"
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_broken_python.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_debug_long_program(rows):
    task = "debug-long-program"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Optimize simulate.py so ./verify.sh completes quickly and writes sum of squares 1..3000 to answer.txt.\n")
    write(base / "simulate.py", """
    def slow_square(x):
        y = 0
        for _ in range(x):
            y += x
        return y

    total = sum(slow_square(i) for i in range(1, 3001))
    open('answer.txt', 'w').write(str(total) + '\\n')
    """)
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    timeout 5 python3 simulate.py
    test "$(cat answer.txt)" = "9004500500"
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_debug_long_program.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_analyze_access_logs(rows):
    task = "analyze-access-logs"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Analyze access.log and create summary.txt with top_path=/api/v1/items and error_count=3.\n")
    write(base / "access.log", """
    10.0.0.1 GET / 200
    10.0.0.2 GET /api/v1/items 200
    10.0.0.3 GET /api/v1/items 500
    10.0.0.4 POST /login 403
    10.0.0.5 GET /api/v1/items 200
    10.0.0.6 GET /api/v1/items 502
    10.0.0.7 GET /health 200
    10.0.0.8 GET /api/v1/users 200
    """)
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    grep -qx 'top_path=/api/v1/items' summary.txt
    grep -qx 'error_count=3' summary.txt
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_analyze_access_logs.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_deterministic_tarball(rows):
    task = "deterministic-tarball"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Create deterministic archive.tar.gz containing data/ sorted by name, owner/group 0, mtime 1970-01-01.\n")
    write(base / "data/a.txt", "alpha\n")
    write(base / "data/b.txt", "bravo\n")
    write(base / "data/c.txt", "charlie\n")
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test -f archive.tar.gz
    tar -tzf archive.tar.gz | grep -qx data/a.txt
    tar -tzf archive.tar.gz | grep -qx data/b.txt
    tar -tzf archive.tar.gz | grep -qx data/c.txt
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_deterministic_tarball.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_fix_git(rows):
    task = "fix-git"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Remove debug.log from git tracking while keeping app.py tracked. Create done.txt containing fixed.\n")
    write(base / "app.py", "print('hello')\n")
    write(base / "debug.log", "temporary debug output\n")
    os.system(f"cd {base} && git init -q && git config user.email t@example.com && git config user.name t && git add app.py debug.log && git commit -q -m init")
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test "$(cat done.txt)" = fixed
    git ls-files | grep -qx app.py
    if git ls-files | grep -qx debug.log; then exit 1; fi
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_fix_git.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_git_multibranch(rows):
    task = "git-multibranch"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Merge both feature-a and feature-b into main and create merged.txt containing A+B.\n")
    write(base / "base.txt", "base\n")
    os.system(f"cd {base} && git init -q -b main && git config user.email t@example.com && git config user.name t && git add base.txt && git commit -q -m base")
    os.system(f"cd {base} && git checkout -q -b feature-a && echo A > a.txt && git add a.txt && git commit -q -m A")
    os.system(f"cd {base} && git checkout -q main && git checkout -q -b feature-b && echo B > b.txt && git add b.txt && git commit -q -m B")
    os.system(f"cd {base} && git checkout -q main")
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    test -f a.txt
    test -f b.txt
    test "$(cat merged.txt)" = "A+B"
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_git_multibranch.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


def make_fix_pandas_version(rows):
    task = "fix-pandas-version"
    base = RUNS / task / "base_task"
    write(base / "instruction.txt", "Fix report.py so it works with installed pandas and writes report.txt containing mean=20.0.\n")
    write(base / "report.py", """
    import pandas as pd

    df = pd.DataFrame({'value': [10, 20, 30]})
    m = df.value.mean_value()
    open('report.txt', 'w').write(f'mean={m}\\n')
    """)
    write(base / "verify.sh", """
    #!/usr/bin/env bash
    set -euo pipefail
    python3 report.py
    test "$(cat report.txt)" = "mean=20.0"
    echo ok
    """, True)
    prompt = PROMPTS / "tbench_fix_pandas_version.txt"
    write(prompt, prompt_for(task))
    rows.append((task, base, prompt))


rows = []
ensure_existing_count_dataset(rows)
for maker in [make_csv_to_parquet, make_cprofiling_python, make_broken_python, make_debug_long_program,
              make_analyze_access_logs, make_deterministic_tarball, make_fix_git, make_git_multibranch,
              make_fix_pandas_version]:
    maker(rows)

with MANIFEST.open('w') as f:
    for task, base, prompt in rows:
        f.write(f"{task}\t{base.relative_to(ROOT)}\t{prompt.relative_to(ROOT)}\n")

print(f"Wrote {MANIFEST} with {len(rows)} rows")
