#!/usr/bin/env python3
"""tb_native_prep.py -- prepare a Terminal-Bench task for NATIVE (containerless)
execution on the GH200.

Rationale (2026-08-18): all alexgshaw/* task images are amd64-only; the GH200
is aarch64 and emulation would poison perf measurements. This tool translates
a tb2 task dir into a native workspace + task-local venv, mirroring how the
July TB tasks (analyze-access-logs etc.) already ran on the GH200.

Usage:
  python3 tb_native_prep.py <task_dir> [--out BASE] [--python PYBIN]
  python3 tb_native_prep.py /scratch/czhai/tb2/regex-chess \
      --out /scratch/czhai/tb-native

Produces under BASE/<task>/:
  work/          agent workspace (environment files staged, Dockerfile excluded)
  venv/          task-local venv with Dockerfile pip deps installed (native arm64)
  tests/         verifier assets (kept OUT of work/ so the agent can't see them)
  instruction.md agent prompt
  manifest.json  everything the runner needs (timeouts, env, deps, commands)
  grade.sh       run the verifier natively
  MANUAL_DEPS.txt  written only if the Dockerfile had apt/system deps to review

What it translates from environment/Dockerfile:
  FROM python:X...   -> recorded; venv uses host python (version noted/compared)
  WORKDIR /app       -> /app == work/
  COPY a b /app/     -> staged into work/
  RUN pip install .. -> pip install into venv
  RUN apt-get install ...
                     -> recorded in MANUAL_DEPS.txt (host/conda install by hand)
  ENV K=V            -> manifest env
Anything else in RUN lines is recorded for manual review rather than executed.
"""
import argparse, json, re, shlex, shutil, subprocess, sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # py<3.11
    tomllib = None


def parse_dockerfile(df_path):
    """Return dict: base, pip_pkgs, apt_pkgs, copies, env, other_runs, workdir."""
    out = {"base": None, "pip_pkgs": [], "apt_pkgs": [], "copies": [],
           "env": {}, "other_runs": [], "workdir": "/app"}
    if not df_path.exists():
        return out
    # join line continuations
    text = re.sub(r"\\\s*\n", " ", df_path.read_text())
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(\w+)\s+(.*)", line)
        if not m:
            continue
        instr, rest = m.group(1).upper(), m.group(2).strip()
        if instr == "FROM":
            out["base"] = rest
        elif instr == "WORKDIR":
            out["workdir"] = rest
        elif instr == "ENV":
            for kv in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\S+)', rest):
                out["env"][kv[0]] = kv[1].strip('"')
        elif instr in ("COPY", "ADD"):
            parts = shlex.split(rest)
            parts = [p for p in parts if not p.startswith("--")]
            if len(parts) >= 2:
                out["copies"].append((parts[:-1], parts[-1]))
        elif instr == "RUN":
            # split on && / ; into sub-commands
            for sub in re.split(r"&&|;", rest):
                sub = sub.strip()
                if not sub:
                    continue
                pm = re.match(r"(?:python3?\s+-m\s+)?pip3?\s+install\s+(.*)", sub)
                am = re.match(r"apt(?:-get)?\s+(?:install|-y\s+install)\s+(.*)", sub) or \
                     re.match(r"apt(?:-get)?\s+install\s+(.*)", sub)
                if pm:
                    pkgs = [p for p in shlex.split(pm.group(1))
                            if not p.startswith("-")]
                    out["pip_pkgs"].extend(pkgs)
                elif "apt-get" in sub or re.match(r"apt\s", sub):
                    if "install" in sub:
                        pkgs = [p for p in shlex.split(sub.split("install", 1)[1])
                                if not p.startswith("-") and p != "-y"]
                        out["apt_pkgs"].extend(pkgs)
                    # apt update/clean: ignore
                elif re.match(r"(rm|mkdir|chmod|chown|ln)\s", sub):
                    pass  # fs housekeeping inside image; workspace copy covers it
                else:
                    out["other_runs"].append(sub)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/scratch/czhai/tb-native"))
    ap.add_argument("--python", default=sys.executable,
                    help="python used to create the venv (default: this one)")
    args = ap.parse_args()

    td = args.task_dir.resolve()
    name = td.name
    base = args.out / name
    work, tests_dst = base / "work", base / "tests"
    venv = base / "venv"
    base.mkdir(parents=True, exist_ok=True)

    # --- task.toml ---
    toml_path = td / "task.toml"
    meta = {}
    if tomllib and toml_path.exists():
        meta = tomllib.loads(toml_path.read_text())
    env_cfg = meta.get("environment", {})
    ver_cfg = meta.get("verifier", {})
    agent_cfg = meta.get("agent", {})

    # --- dockerfile ---
    df = parse_dockerfile(td / "environment" / "Dockerfile")

    # --- stage workspace: environment/* minus Dockerfile ---
    work.mkdir(exist_ok=True)
    env_dir = td / "environment"
    staged = []
    if env_dir.is_dir():
        for f in env_dir.iterdir():
            if f.name == "Dockerfile":
                continue
            dest = work / f.name
            if f.is_dir():
                shutil.copytree(f, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(f, dest)
            staged.append(f.name)

    # --- stage tests + instruction (outside work/) ---
    # v1.2: container WORKDIR (usually /app) is hardcoded in tests and
    # instruction; rewrite it to the native workspace path in the copies.
    wd = df["workdir"] if df["workdir"].startswith("/") else "/app"
    def stage_translated(srcf, dstf):
        try:
            t = srcf.read_text()
        except (UnicodeDecodeError, OSError):
            shutil.copy2(srcf, dstf); return
        # v1.3: relative translation -- absolute prep paths leaked into
        # prompts made agents write into the SHARED prep workspace.
        dstf.write_text(t.replace(wd + "/", "./").replace(wd, "."))
        dstf.chmod(srcf.stat().st_mode)
    if (td / "tests").is_dir():
        tests_dst.mkdir(exist_ok=True)
        for f in (td / "tests").rglob("*"):
            rel = f.relative_to(td / "tests")
            if f.is_dir():
                (tests_dst / rel).mkdir(parents=True, exist_ok=True)
            else:
                stage_translated(f, tests_dst / rel)
    for f in ("instruction.md",):
        if (td / f).exists():
            stage_translated(td / f, base / f)

    # --- venv + pip deps (native arm64) ---
    pip_ok, pip_log = True, ""
    if not (venv / "bin" / "python3").exists():
        subprocess.run([args.python, "-m", "venv", str(venv)], check=True)
    if df["pip_pkgs"]:
        r = subprocess.run([str(venv / "bin" / "pip"), "install", "-q",
                            *df["pip_pkgs"]], capture_output=True, text=True)
        pip_ok, pip_log = r.returncode == 0, (r.stdout + r.stderr)[-2000:]

    # --- manual deps report ---
    manual = []
    if df["apt_pkgs"]:
        manual.append("apt packages (install native equivalents by hand / conda):")
        manual.append("  " + " ".join(sorted(set(df["apt_pkgs"]))))
    if df["other_runs"]:
        manual.append("unhandled RUN steps (review + apply manually if needed):")
        manual += ["  " + r for r in df["other_runs"]]
    if manual:
        (base / "MANUAL_DEPS.txt").write_text("\n".join(manual) + "\n")

    # --- grade.sh (+ native-translated test script) ---
    # TB verifiers assume container paths (/tests, /logs/verifier) and root
    # apt. Generate a translated copy: apt lines commented (host has curl),
    # /tests -> $TEST_DIR, /logs/verifier -> $LOG_DIR.
    test_sh = tests_dst / "test.sh"
    if test_sh.exists():
        t = test_sh.read_text()
        t = re.sub(r"^(\s*apt(-get)?\s.*)$", r"# [native] \1", t, flags=re.M)
        t = t.replace("/logs/verifier", '"$LOG_DIR"')
        t = t.replace("/tests/", '"$TEST_DIR"/')
        t = t.replace(" /tests ", ' "$TEST_DIR" ')
        native_sh = tests_dst / "test_native.sh"
        native_sh.write_text(t)
        native_sh.chmod(0o755)
        test_sh = native_sh
    grade = f"""#!/bin/bash
# native verifier for {name} (generated by tb_native_prep.py)
set -u
export PATH="{venv}/bin:$PATH"
export TASK_DIR="{base}"
export WORK_DIR="{work}"
export TEST_DIR="{tests_dst}"
export LOG_DIR="{base}/logs/verifier"
mkdir -p "$LOG_DIR"
cd "${{RUN_WORK:-{work}}}"
timeout {int(ver_cfg.get('timeout_sec', 3600))} bash "{test_sh}"
rc=$?
[ -f "$LOG_DIR/reward.txt" ] && echo "reward=$(cat "$LOG_DIR"/reward.txt)"
echo "grade rc=$rc"
exit $rc
"""
    (base / "grade.sh").write_text(grade)
    (base / "grade.sh").chmod(0o755)

    # --- manifest ---
    host_py = subprocess.run([str(venv / "bin" / "python3"), "--version"],
                             capture_output=True, text=True).stdout.strip()
    manifest = {
        "task": name,
        "source": str(td),
        "workspace": str(work),
        "venv": str(venv),
        "instruction": str(base / "instruction.md"),
        "tests": str(tests_dst),
        "grade_cmd": str(base / "grade.sh"),
        "agent_timeout_sec": agent_cfg.get("timeout_sec"),
        "verifier_timeout_sec": ver_cfg.get("timeout_sec"),
        "resources_from_toml": {k: env_cfg.get(k) for k in
                                ("cpus", "memory_mb", "storage_mb", "gpus",
                                 "allow_internet")},
        "docker_base_image": df["base"],
        "host_python": host_py,
        "pip_pkgs": df["pip_pkgs"],
        "pip_install_ok": pip_ok,
        "apt_pkgs_manual": sorted(set(df["apt_pkgs"])),
        "unhandled_runs": df["other_runs"],
        "env": {**df["env"], **(env_cfg.get("env") or {})},
        "staged_files": staged,
        "difficulty": (meta.get("metadata") or {}).get("difficulty"),
    }
    (base / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"[{name}] workspace: {work}")
    print(f"[{name}] venv ({host_py}); base image was {df['base']}")
    print(f"[{name}] pip deps: {df['pip_pkgs'] or 'none'}"
          + ("" if pip_ok else f"  PIP FAILED:\n{pip_log}"))
    if manual:
        print(f"[{name}] MANUAL DEPS NEEDED -- see {base/'MANUAL_DEPS.txt'}")
    print(f"[{name}] grade: bash {base}/grade.sh")


if __name__ == "__main__":
    main()
