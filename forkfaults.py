#!/usr/bin/env python3
"""Why is the first pass of "bigdata, data sent with each task" slower under fork?

Runs the same loop as bench.py --workload bigdata --variant arg (500 MiB, 16
workers, 3 passes) and counts minor page faults per pass, in the parent and summed
over the workers. Three setups, interleaved:

    fork         as in the benchmark
    fork+freeze  gc.freeze() in the parent before forking, so the garbage collector
                 in the workers does not walk the inherited objects
    forkserver   workers inherit nothing from the parent (control)

    python forkfaults.py [runs] > results/forkfaults.csv
"""
import gc
import multiprocessing as mp
import os
import resource
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import bench

SIZE_MIB = 500
WORKERS = 16
TASKS = 64
PASSES = 3


def minflt(pid: int) -> int:
    with open(f"/proc/{pid}/stat") as f:
        # field 10 of proc_pid_stat; the command name in field 2 may contain spaces
        return int(f.read().rsplit(")", 1)[1].split()[7])


def run(setup: str, repeat: int):
    records = bench.make_records(SIZE_MIB)
    chunks = bench.ranges(len(records), TASKS)
    if setup == "fork+freeze":
        gc.collect()
        gc.freeze()
    method = "forkserver" if setup == "forkserver" else "fork"
    with ProcessPoolExecutor(WORKERS, mp_context=mp.get_context(method)) as ex:
        parent0 = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
        workers0 = 0
        for p in range(1, PASSES + 1):
            t = time.perf_counter()
            list(ex.map(bench.scan, (records[lo:hi] for lo, hi in chunks)))
            seconds = time.perf_counter() - t
            parent = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
            workers = sum(minflt(pid) for pid in ex._processes)
            print(f"{setup},{repeat},{p},{seconds:.4f},{parent - parent0},{workers - workers0}", flush=True)
            parent0, workers0 = parent, workers
    gc.unfreeze()


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print("setup,repeat,pass,seconds,parent_minflt,worker_minflt", flush=True)
    for r in range(1, runs + 1):
        for setup in ("fork", "fork+freeze", "forkserver"):
            # a new process per run, so one run's heap cannot affect the next
            pid = os.fork()
            if pid == 0:
                run(setup, r)
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            if status != 0:
                sys.exit(f"{setup} failed: status {status}")


if __name__ == "__main__":
    main()
