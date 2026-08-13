#!/usr/bin/env python3
"""replay_tool_side.py — definitive stock-vs-optimized comparison.

The problem with comparing arms by wall time: model-inference latency swings
2-3x run to run and the agent takes a different trajectory every run. Both
noise sources vanish if you take the command sequence an agent ACTUALLY
issued (recorded in commands.jsonl) and replay it with no LLM involved:

  stock mode : each command in a fresh `bash -lc` process (what codex does)
  daemon mode: each command through the persistent shell daemon

Same commands, same order, same workspace, repeated N times with workspace
reset between reps. The measured difference IS the orchestration delta —
deterministic, paired, and free.

Usage:
  # replay one task's trajectory, 10 reps each mode:
  python3 replay_tool_side.py \
    --jsonl /scratch/.../arm_C.*/swebench/astropy__astropy-12907/shelld_logs/commands.jsonl \
    --workspace /scratch/czhai/latency-eval/workspaces/astropy__astropy-12907 \
    --reset git --reps 10 \
    --daemon-script /projects/.../latency-opt/scripts/shell_sessiond.py

  --reset git      -> `git reset --hard && git clean -fdq` between reps
  --reset rsync:DIR-> restore workspace from pristine DIR between reps
  --reset none     -> no reset (read-only trajectories)

Output: per-rep totals and per-command medians for both modes, paired deltas.
"""

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SNAPSHOT_MARKER = ".codex/shell_snapshots"  # codex-internal cmds, excluded


def load_trajectory(jsonl_path: str):
    cmds = []
    for line in open(jsonl_path):
        row = json.loads(line)
        if SNAPSHOT_MARKER in row["cmd"]:
            continue  # codex snapshot machinery, not an agent command
        cmds.append({"cmd": row["cmd"], "cwd": row["cwd"]})
    return cmds


def reset_workspace(mode: str, workspace: str):
    if mode == "none":
        return
    if mode == "git":
        subprocess.run(["git", "-C", workspace, "reset", "--hard", "-q"], check=False)
        subprocess.run(["git", "-C", workspace, "clean", "-fdq"], check=False)
    elif mode.startswith("rsync:"):
        src = mode.split(":", 1)[1].rstrip("/") + "/"
        subprocess.run(["rsync", "-a", "--delete", src, workspace.rstrip("/") + "/"],
                       check=True)
    else:
        raise SystemExit(f"unknown --reset mode {mode!r}")


# ------------------------------------------------------------------ stock leg
def run_stock(cmds, login: bool):
    """Fresh `bash -lc` per command. If SPEC_PERF_DIR is set, each command is
    additionally wrapped in perf stat + /usr/bin/time -v (spec_perf schema,
    label=replay), turning a recorded trajectory into per-command PMU data
    with zero LLM cost. Timing below still measures the wrapped process, so
    only use perf-enabled replays for CPU characterization, not for the
    stock-vs-daemon latency delta."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "speculation"))
    import spec_perf
    per_cmd = []
    for i, c in enumerate(cmds):
        argv = ["bash", "-lc" if login else "-c", c["cmd"]]
        t0 = time.perf_counter()
        try:
            spec_perf.run_profiled(argv, label="replay", cmd_text=c["cmd"],
                                   extra={"seq": i}, cwd=c["cwd"],
                                   timeout=None)
        except OSError:
            pass
        per_cmd.append(time.perf_counter() - t0)
    return per_cmd


# ----------------------------------------------------------------- daemon leg
class DaemonClient:
    def __init__(self, daemon_script: str, login: bool):
        self.dir = tempfile.mkdtemp(prefix="replayd_")
        self.sock_path = os.path.join(self.dir, "sock")
        args = [sys.executable, daemon_script, "--socket", self.sock_path,
                "--log-dir", os.path.join(self.dir, "logs")]
        if not login:
            args.append("--no-login")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.05)

    def run(self, cmd: str, cwd: str) -> float:
        t0 = time.perf_counter()
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(600)
        s.connect(self.sock_path)
        s.sendall((json.dumps({"cmd": cmd, "cwd": cwd}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
        s.close()
        return time.perf_counter() - t0

    def close(self):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(3)
            s.connect(self.sock_path)
            s.sendall(b'{"op":"shutdown"}\n')
        except OSError:
            self.proc.kill()
        finally:
            self.proc.wait(timeout=10)
            shutil.rmtree(self.dir, ignore_errors=True)


def run_daemon(cmds, daemon_script: str, login: bool):
    d = DaemonClient(daemon_script, login)
    try:
        return [d.run(c["cmd"], c["cwd"]) for c in cmds]
    finally:
        d.close()


# --------------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True,
                    help="commands.jsonl from an arm B/C run (the trajectory)")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--reset", default="git", help="git | rsync:DIR | none")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--daemon-script", default=None,
                    help="path to shell_sessiond.py; omit for a STOCK-ONLY "
                         "replay (e.g. SPEC_PERF_DIR CPU characterization, "
                         "where the daemon leg adds nothing)")
    ap.add_argument("--no-login", action="store_true",
                    help="use bash -c instead of -lc in both legs")
    ap.add_argument("--out", default=None, help="write raw results json here")
    args = ap.parse_args()

    login = not args.no_login
    cmds = load_trajectory(args.jsonl)
    print(f"trajectory: {len(cmds)} commands from {args.jsonl}")

    results = {"stock": [], "daemon": []}
    modes = ("stock", "daemon") if args.daemon_script else ("stock",)
    # interleave modes each rep so drift (cache warmth, filesystem) hits both equally
    for rep in range(args.reps):
        for mode in modes:
            reset_workspace(args.reset, args.workspace)
            if mode == "stock":
                per = run_stock(cmds, login)
            else:
                per = run_daemon(cmds, args.daemon_script, login)
            results[mode].append(per)
            print(f"rep {rep+1:2d} {mode:6s} total={sum(per):7.3f}s")

    n = len(cmds)
    stock_tot = [sum(r) for r in results["stock"]]
    if not args.daemon_script:
        med_per_stock = statistics.median(x for r in results["stock"] for x in r)
        print(f"\nstock total: median {statistics.median(stock_tot):.3f}s "
              f"(min {min(stock_tot):.3f}, max {max(stock_tot):.3f}); "
              f"per-command median {med_per_stock*1000:.1f}ms ({n} cmds)")
        if args.out:
            Path(args.out).write_text(json.dumps(
                {"jsonl": args.jsonl, "n_cmds": n, "reps": args.reps,
                 "login": login, "results": results}, indent=2))
            print(f"raw -> {args.out}")
        return

    print("\n=== paired summary ===")
    daemon_tot = [sum(r) for r in results["daemon"]]
    deltas = [s - d for s, d in zip(stock_tot, daemon_tot)]
    print(f"stock  total: median {statistics.median(stock_tot):.3f}s "
          f"(min {min(stock_tot):.3f}, max {max(stock_tot):.3f})")
    print(f"daemon total: median {statistics.median(daemon_tot):.3f}s "
          f"(min {min(daemon_tot):.3f}, max {max(daemon_tot):.3f})")
    print(f"paired delta (stock - daemon): median {statistics.median(deltas):+.3f}s, "
          f"all-positive={all(x > 0 for x in deltas)}")
    med_per_stock = statistics.median(x for r in results["stock"] for x in r)
    med_per_daemon = statistics.median(x for r in results["daemon"] for x in r)
    print(f"per-command median: stock {med_per_stock*1000:.1f}ms, "
          f"daemon {med_per_daemon*1000:.1f}ms  ({n} cmds/trajectory)")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"jsonl": args.jsonl, "n_cmds": n, "reps": args.reps,
             "login": login, "results": results}, indent=2))
        print(f"raw -> {args.out}")


if __name__ == "__main__":
    main()
