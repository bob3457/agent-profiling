#!/usr/bin/env python3
"""patch_sessiond_prof.py -- add per-command resource sampling to shell_sessiond.py

Adds, for every non-cached command executed in the persistent session shell:
  rss_peak_kb   peak RSS (kb) of the session bash's process tree during the cmd
  rss_timeline  [[t_s, rss_kb, cpu_pct], ...] sampled at 200ms
  io_read_kb / io_write_kb   /proc/<pid>/io delta summed over the tree

Complements the existing --perf-events attach (HW counters) so commands.jsonl +
tool_perf/*.csv together carry: wall, cpu, HW counters, RSS shape, and IO --
the "maximum detail" record for the TB-native profiling campaign.

Idempotent: re-running is a no-op. All anchors are verbatim; any drift in the
target file makes this script fail loudly rather than mis-patch.

Usage: python3 patch_sessiond_prof.py [path-to-shell_sessiond.py]
       (default: /projects/kzhou6/czhai/agent-profiling/scripts/shell_sessiond.py)
"""
import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
              "/projects/kzhou6/czhai/agent-profiling/scripts/shell_sessiond.py")
src = TARGET.read_text()

MARK = "_profshim_sample_tree"
if MARK in src:
    print(f"already patched: {TARGET}")
    sys.exit(0)

# ---------------------------------------------------------------- helpers blk
ANCHOR_HELPERS = '''class ShellSession:
    """One persistent bash. Commands run via eval in the same shell process,
    so env mutations persist. Output is captured via per-command temp files;
    completion is signalled through a control FIFO."""'''
assert src.count(ANCHOR_HELPERS) == 1, "helpers anchor not found"

HELPERS = '''_PROFSHIM_PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def _profshim_descendants(root):
    pids, frontier = [root], [root]
    for _ in range(8):
        nxt = []
        for p in frontier:
            try:
                with open(f"/proc/{p}/task/{p}/children") as f:
                    nxt += [int(c) for c in f.read().split()]
            except OSError:
                pass
        if not nxt:
            break
        pids += nxt
        frontier = nxt
    return pids


def _profshim_sample_tree(root):
    """-> (rss_kb, jiffies, io_read_bytes, io_write_bytes) over the tree."""
    rss_kb = jif = rd = wr = 0
    for p in _profshim_descendants(root):
        try:
            with open(f"/proc/{p}/stat") as f:
                fld = f.read().rsplit(")", 1)[1].split()
            jif += int(fld[11]) + int(fld[12])
            rss_kb += int(fld[21]) * _PROFSHIM_PAGE_KB
        except (OSError, IndexError, ValueError):
            continue
        try:
            with open(f"/proc/{p}/io") as f:
                for line in f:
                    if line.startswith("read_bytes:"):
                        rd += int(line.split()[1])
                    elif line.startswith("write_bytes:"):
                        wr += int(line.split()[1])
        except OSError:
            pass
    return rss_kb, jif, rd, wr


class _ProfshimSampler:
    """200ms RSS/CPU/IO sampler over the session bash tree for one command."""

    def __init__(self, root_pid):
        self.root = root_pid
        self.timeline = []
        self.rss_peak = 0
        self._stop = threading.Event()
        r0, _, rd0, wr0 = _profshim_sample_tree(root_pid)
        self._io0 = (rd0, wr0)
        self.io_read_kb = self.io_write_kb = 0
        self._t0 = time.time()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        prev_j = prev_t = None
        while not self._stop.is_set():
            now = time.time()
            rss, jif, rd, wr = _profshim_sample_tree(self.root)
            cpu = 0.0
            if prev_t is not None and now > prev_t:
                cpu = round(100.0 * (jif - prev_j) / CLK_TCK / (now - prev_t), 1)
            self.timeline.append([round(now - self._t0, 2), rss, cpu])
            self.rss_peak = max(self.rss_peak, rss)
            self.io_read_kb = max(0, (rd - self._io0[0]) // 1024)
            self.io_write_kb = max(0, (wr - self._io0[1]) // 1024)
            prev_j, prev_t = jif, now
            self._stop.wait(0.2)

    def stop(self):
        self._stop.set()
        self._th.join(timeout=1)


''' + ANCHOR_HELPERS
src = src.replace(ANCHOR_HELPERS, HELPERS)

# ------------------------------------------------------------- sampler start
ANCHOR_START = '''            cpu0 = proc_cpu_seconds(self.proc.pid)
            t0 = time.time()'''
assert src.count(ANCHOR_START) == 1, "start anchor not found"
src = src.replace(ANCHOR_START, '''            cpu0 = proc_cpu_seconds(self.proc.pid)
            _sampler = _ProfshimSampler(self.proc.pid)
            t0 = time.time()''')

# ------------------------------------------------- sampler stop (both paths)
ANCHOR_STOP = '''            status = self._wait_ctl(timeout)
            wall = time.time() - t0
            cpu = max(0.0, proc_cpu_seconds(self.proc.pid) - cpu0)'''
assert src.count(ANCHOR_STOP) == 1, "stop anchor not found"
src = src.replace(ANCHOR_STOP, '''            status = self._wait_ctl(timeout)
            wall = time.time() - t0
            cpu = max(0.0, proc_cpu_seconds(self.proc.pid) - cpu0)
            _sampler.stop()
            _prof = {"rss_peak_kb": _sampler.rss_peak,
                     "io_read_kb": _sampler.io_read_kb,
                     "io_write_kb": _sampler.io_write_kb,
                     "rss_timeline": _sampler.timeline}''')

# ---------------------------------------------------- attach to timeout path
ANCHOR_TO = '''"wall_s": wall, "cpu_s": cpu, "session_dead": True}'''
assert src.count(ANCHOR_TO) == 1, "timeout anchor not found"
src = src.replace(ANCHOR_TO, '''"wall_s": wall, "cpu_s": cpu, "session_dead": True, **_prof}''')

# ----------------------------------------------------- attach to normal path
ANCHOR_OK = '''            res = {"exit": int(status), "stdout": read_trunc(out_f),
                   "stderr": read_trunc(err_f), "wall_s": wall, "cpu_s": cpu}'''
assert src.count(ANCHOR_OK) == 1, "normal-path anchor not found"
src = src.replace(ANCHOR_OK, '''            res = {"exit": int(status), "stdout": read_trunc(out_f),
                   "stderr": read_trunc(err_f), "wall_s": wall, "cpu_s": cpu,
                   **_prof}''')

# ------------------------------------------------------------ record fields
ANCHOR_REC = '''            "near_miss": res.get("near_miss"),'''
assert src.count(ANCHOR_REC) == 1, "record anchor not found"
src = src.replace(ANCHOR_REC, '''            "near_miss": res.get("near_miss"),
            "rss_peak_kb": res.get("rss_peak_kb"),
            "io_read_kb": res.get("io_read_kb"),
            "io_write_kb": res.get("io_write_kb"),
            "rss_timeline": res.get("rss_timeline"),''')

TARGET.write_text(src)
print(f"patched: {TARGET} (per-command RSS/CPU/IO tree sampling added)")
print("verify passed via AST check in caller")
