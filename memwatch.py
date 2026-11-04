#!/usr/bin/env python3
"""Run a benchmark command and report its peak memory, as measured by the kernel.

    python memwatch.py -- /path/to/python -I bench.py ARGS...

The watcher re-executes itself in a new cgroup (a systemd user scope), runs the
command as a child, and reads memory.peak once the child has exited. The kernel
tracks that peak on every page charge, so nothing has to be sampled: no thread
or sampler touches the benchmark, and short copy-on-write bursts are not missed.
Pages shared by several processes are counted once. See memcheck.py for why this
replaced sampling PSS from /proc.

The child's stdout must be one CSV row; this script prints that row with
",<peak_mem_mb>" appended. The watcher's own memory, read just before the child
starts, is subtracted.
"""
import os
import subprocess
import sys


def own_cgroup() -> str:
    with open("/proc/self/cgroup") as f:
        return "/sys/fs/cgroup" + f.read().strip().split(":", 2)[2]


def read_int(path: str) -> int:
    with open(path) as f:
        return int(f.read())


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        sys.exit(__doc__)

    cgroup = own_cgroup()
    if not os.path.basename(cgroup).startswith("run-"):
        # not yet in a scope of our own: start one and run this script again in it
        os.execvp("systemd-run", ["systemd-run", "--user", "--scope", "-q",
                                  "-p", "MemoryAccounting=yes",
                                  sys.executable, "-I", os.path.abspath(__file__),
                                  "--", *argv])

    with open(f"{cgroup}/cgroup.procs") as f:
        others = set(f.read().split()) - {str(os.getpid())}
    if others:
        sys.exit(f"memwatch: cgroup {cgroup} is shared with {others}; peak would be wrong")

    baseline = read_int(f"{cgroup}/memory.current")
    proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True)
    peak = read_int(f"{cgroup}/memory.peak")
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        sys.stderr.write(f"memwatch: command failed (exit {proc.returncode}): {' '.join(argv)}\n")
        sys.exit(proc.returncode or 1)
    print(f"{out.splitlines()[-1]},{(peak - baseline) / 2**20:.1f}")


if __name__ == "__main__":
    main()
