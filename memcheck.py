#!/usr/bin/env python3
"""Cross-check of the memory method: sampled PSS (the draft's method) against the
kernel's cgroup memory.peak, for one command. Must run in its own scope:

    systemd-run --user --scope -q -p MemoryAccounting=yes python3 -I memcheck.py CMD...

Sampling reads /proc/<pid>/smaps_rollup for every process. During a fork
copy-on-write burst one sweep can stall for longer than the burst, so the peak is
missed; the sweeps also slow the benchmark a little. Hence memwatch.py.
"""
import os
import statistics
import subprocess
import sys
import time


def pss_kb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def tree(root):
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as f:
                    stack.extend(int(c) for c in f.read().split())
            except (OSError, ValueError):
                pass
    return out


cg = "/sys/fs/cgroup" + open("/proc/self/cgroup").read().strip().split(":")[2]
p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, text=True)
peak, sweeps = 0, []
while p.poll() is None:
    t = time.perf_counter()
    peak = max(peak, sum(pss_kb(x) for x in tree(p.pid)))
    sweeps.append(time.perf_counter() - t)
    time.sleep(0.005)
sec = p.stdout.read().strip().split(",")[12]
kernel = int(open(f"{cg}/memory.peak").read()) / 2**20
print(f"sec={sec} samples={len(sweeps)} median_sweep_ms={statistics.median(sweeps)*1e3:.1f} "
      f"max_sweep_ms={max(sweeps)*1e3:.0f} sampled_pss_mb={peak/1024:.0f} kernel_peak_mb={kernel:.0f}")
