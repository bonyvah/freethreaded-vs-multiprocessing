#!/usr/bin/env python3
"""One benchmark run. Prints one CSV row (without peak memory) to stdout.

Memory is NOT measured here. memwatch.py runs this script in its own cgroup
and reads the kernel's peak afterwards, so the measured process has no extra
thread: no GIL competition from a sampler, and no "fork() of a multi-threaded
process" hazard for the fork start method.

Workloads (--workload):
  primes   pure-Python CPU-bound work: count primes below --size, split
           into --tasks ranges. No shared data, almost no IPC.
  bigdata  a large shared input (--size MiB of Python dicts); each task
           scans one slice of it. Tests the cost of getting data to workers.
           Process variants:
             arg   each task's slice is pickled and sent with the task
             init  each worker gets the whole dataset once via initializer=
                   (inherited for free under fork, pickled under spawn/forkserver)
           Thread / sequential variants:
             list  the records in a list (the natural way)
             tuple the same records in a tuple (no per-element lock on first
                   cross-thread read in the free-threaded build)
           The data is scanned --passes times with one pool; per-pass
           times go to pass_seconds.
  shared   shared mutable state: --size items, each hashed into one of 1024
           counters. Variants:
             global_lock  one lock around every update
             sharded      64 locks, each guarding a slice of the counters
             sharded_lists (threads only) as sharded, but each shard is its
                          own list: on the free-threaded build every list
                          write also takes that list's internal lock
             local_merge  each task counts locally, results merged at the end
             manager      (processes only) multiprocessing.Manager list + lock
  tiny     many very short tasks: --tasks tasks of --size loop iterations each.
           Variants:
             reuse        one pool for all tasks
             fresh        a new pool for every batch (--batches batches)
           --chunksize > 1 for threads batches tasks by hand (one future per
           chunk), since ThreadPoolExecutor.map ignores chunksize.

Modes (--mode): threads, processes, sequential (no pool, main thread only).

Usage:
    python bench.py --header
    python bench.py --workload primes --mode threads --workers 4
    python bench.py --workload bigdata --mode processes --variant init \\
                    --start-method spawn --size 100 --workers 16
"""
import argparse
import csv
import multiprocessing as mp
import os
import platform
import sys
import sysconfig
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

FIELDS = [
    "python", "build", "gil_enabled", "workload", "variant", "mode", "workers",
    "start_method", "size", "tasks", "batches", "chunksize", "seconds", "check",
    "passes", "pass_seconds", "cpus", "physical_cores", "cpu_model",
    "peak_mem_mb",  # appended by memwatch.py, not by this script
]

DEFAULT_SIZE = {"primes": 3_000_000, "bigdata": 100, "shared": 2_000_000, "tiny": 1_000}
DEFAULT_TASKS = {"primes": 64, "bigdata": 64, "shared": 64, "tiny": 20_000}
VARIANTS = {
    "primes": ["-"],
    "bigdata": ["arg", "init", "list", "tuple"],  # arg/init: processes; list/tuple: threads
    "shared": ["global_lock", "sharded", "sharded_lists", "local_merge", "manager"],
    "tiny": ["reuse", "fresh"],
}


def ranges(n: int, k: int):
    step = n // k
    return [(i * step, n if i == k - 1 else (i + 1) * step) for i in range(k)]


# ================================================================ primes ==

def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def count_primes(bounds):
    lo, hi = bounds
    c = 0
    for n in range(lo, hi):
        if is_prime(n):
            c += 1
    return c


# =============================================================== bigdata ==

CATS = ("alpha", "beta", "gamma", "delta")
_RECORDS = None  # set in the main process, or in each worker by _init_records


def make_record(i: int) -> dict:
    return {"id": i, "value": (i * 7919 % 1000) / 10.0, "cat": CATS[i % 4], "tag": f"rec-{i:09d}"}


def bytes_per_record() -> float:
    import tracemalloc
    tracemalloc.start()
    sample = [make_record(i) for i in range(20_000)]
    used, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del sample
    return used / 20_000


# Measured with bytes_per_record() on the GIL build (302.2 B; 350.4 B on the
# free-threaded build, whose objects are larger). Fixed so every setup scans
# exactly the same records; --size is therefore "MiB on the GIL build".
BYTES_PER_RECORD = 302.2


def make_records(size_mib: int):
    n = int(size_mib * 2**20 / BYTES_PER_RECORD)
    return [make_record(i) for i in range(n)]


def _scan(recs, lo, hi):
    # same loop for every setup; only how `recs` got here differs
    s = 0.0
    c = 0
    for i in range(lo, hi):
        r = recs[i]
        if r["cat"] == "beta":
            s += r["value"]
            c += len(r["tag"])
    return s, c


def scan(records):
    """'arg' variant: the slice itself arrived (pickled) with the task."""
    return _scan(records, 0, len(records))


def _init_records(records):
    global _RECORDS
    _RECORDS = records


def scan_range(bounds):
    """Shared / initializer variant: only the index range arrives with the task."""
    return _scan(_RECORDS, *bounds)


# ================================================================ shared ==

NBINS = 1024
NSHARDS = 64
WORK = 30          # LCG steps per item, so updates are not 100% of the work
_COUNTS = None     # list (threads) or mp.Array (processes) or Manager list
_LOCK = None       # one lock
_LOCKS = None      # NSHARDS locks


def item_key(i: int) -> int:
    x = i
    for _ in range(WORK):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return x % NBINS


def _init_shared(counts, lock, locks):
    global _COUNTS, _LOCK, _LOCKS
    _COUNTS, _LOCK, _LOCKS = counts, lock, locks


def shared_global_lock(bounds):
    counts, lock = _COUNTS, _LOCK
    for i in range(*bounds):
        k = item_key(i)
        with lock:
            counts[k] += 1
    return 0


def shared_sharded(bounds):
    counts, locks = _COUNTS, _LOCKS
    per = NBINS // NSHARDS
    for i in range(*bounds):
        k = item_key(i)
        with locks[k // per]:
            counts[k] += 1
    return 0


def shared_sharded_lists(bounds):
    """Like sharded, but each shard's counters are their own list, so the
    free-threaded build's internal per-list lock is per shard, not global."""
    shards, locks = _COUNTS, _LOCKS
    per = NBINS // NSHARDS
    for i in range(*bounds):
        k = item_key(i)
        s = k // per
        with locks[s]:
            shards[s][k - s * per] += 1
    return 0


def shared_local_merge(bounds):
    local = [0] * NBINS
    for i in range(*bounds):
        local[item_key(i)] += 1
    return local


# ================================================================== tiny ==

def tiny_task(n: int) -> int:
    s = 0
    for i in range(n):
        s += i
    return s


# ================================================================ driver ==

def build_info():
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    gil_enabled = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    return ("free-threaded" if free_threaded else "gil"), gil_enabled


def cpu_info():
    model, cores = "", set()
    phys = core = None
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if key == "model name":
                    model = val
                elif key == "physical id":
                    phys = val
                elif key == "core id":
                    core = val
                elif not key and core is not None:
                    cores.add((phys, core))
                    phys = core = None
        if core is not None:
            cores.add((phys, core))
    except OSError:
        pass
    return model, (len(cores) or "")


def make_pool(args, initializer=None, initargs=()):
    if args.mode == "threads":
        return ThreadPoolExecutor(args.workers, initializer=initializer, initargs=initargs)
    ctx = mp.get_context(args.start_method)
    return ProcessPoolExecutor(args.workers, mp_context=ctx, initializer=initializer, initargs=initargs)


class Sequential:
    """Stand-in for a pool: runs everything in the main thread."""

    def __init__(self, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def map(self, fn, it, chunksize=1):
        return map(fn, it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pool_or_seq(args, initializer=None, initargs=()):
    if args.mode == "sequential":
        return Sequential(initializer, initargs)
    return make_pool(args, initializer, initargs)


def run_primes(args):
    chunks = ranges(args.size, args.tasks)
    t0 = time.perf_counter()
    with pool_or_seq(args) as ex:
        check = sum(ex.map(count_primes, chunks))
    return time.perf_counter() - t0, check


def run_bigdata(args):
    """Scan the same data --passes times with one pool.

    Pass 1 is "cold": process workers must receive the data, and on the
    free-threaded build each list element's first read from a non-owner
    thread goes through the list's lock (see paper). Later passes are warm.
    """
    records = make_records(args.size)  # built before the timer, same for all setups
    if args.variant == "tuple":
        records = tuple(records)
    chunks = ranges(len(records), args.tasks)
    passes = []
    t0 = time.perf_counter()
    if args.variant == "arg":
        # every pass sends each task its slice again (pickled)
        with pool_or_seq(args) as ex:
            for _ in range(args.passes):
                tp = time.perf_counter()
                res = list(ex.map(scan, (records[lo:hi] for lo, hi in chunks)))
                passes.append(time.perf_counter() - tp)
    else:
        # threads / sequential use the in-memory copy; processes ("init")
        # get it once per worker via initializer=
        with pool_or_seq(args, _init_records, (records,)) as ex:
            for _ in range(args.passes):
                tp = time.perf_counter()
                res = list(ex.map(scan_range, chunks))
                passes.append(time.perf_counter() - tp)
    seconds = time.perf_counter() - t0
    args.pass_seconds = ";".join(f"{p:.4f}" for p in passes)
    check = f"{round(sum(s for s, _ in res), 1)}/{sum(c for _, c in res)}"
    return seconds, check


def run_shared(args):
    chunks = ranges(args.size, args.tasks)
    v = args.variant
    manager = None
    t0 = time.perf_counter()  # creating locks / shared arrays / Manager is part of the cost
    if v == "local_merge":
        with pool_or_seq(args) as ex:
            counts = [0] * NBINS
            for local in ex.map(shared_local_merge, chunks):
                for k, c in enumerate(local):
                    counts[k] += c
    else:
        if args.mode == "processes":
            ctx = mp.get_context(args.start_method)
            if v == "manager":
                manager = ctx.Manager()
                counts, lock = manager.list([0] * NBINS), manager.Lock()
                locks = [lock] * NSHARDS
            elif v == "sharded_lists":
                raise SystemExit("sharded_lists is a threads-only variant (mp.Array has no per-list lock)")
            else:
                counts = ctx.Array("q", NBINS, lock=False)
                lock = ctx.Lock()
                locks = [ctx.Lock() for _ in range(NSHARDS)]
        else:
            import threading
            if v == "manager":
                raise SystemExit("manager variant is for --mode processes only")
            if v == "sharded_lists":
                counts = [[0] * (NBINS // NSHARDS) for _ in range(NSHARDS)]
            else:
                counts = [0] * NBINS
            lock = threading.Lock()
            locks = [threading.Lock() for _ in range(NSHARDS)]
        fn = {"sharded": shared_sharded, "sharded_lists": shared_sharded_lists}.get(v, shared_global_lock)
        with pool_or_seq(args, _init_shared, (counts, lock, locks)) as ex:
            list(ex.map(fn, chunks))
        counts = [c for shard in counts for c in shard] if v == "sharded_lists" else list(counts)
        if manager:
            manager.shutdown()
    seconds = time.perf_counter() - t0
    check = sum(k * c for k, c in enumerate(counts))
    assert sum(counts) == args.size, f"lost updates: {sum(counts)} != {args.size}"
    return seconds, check


def tiny_batch(ns):
    return sum(tiny_task(n) for n in ns)


def _tiny_map(ex, args, ntasks):
    work = [args.size] * ntasks
    if args.mode == "threads" and args.chunksize > 1:
        # ThreadPoolExecutor.map ignores chunksize, so batch by hand: one
        # future per chunk, the same thing chunksize does for processes
        c = args.chunksize
        return sum(ex.map(tiny_batch, [work[i:i + c] for i in range(0, ntasks, c)]))
    return sum(ex.map(tiny_task, work, chunksize=args.chunksize))


def run_tiny(args):
    t0 = time.perf_counter()
    check = 0
    if args.variant == "fresh":
        per = args.tasks // args.batches
        for _ in range(args.batches):
            with pool_or_seq(args) as ex:
                check += _tiny_map(ex, args, per)
    else:
        with pool_or_seq(args) as ex:
            check = _tiny_map(ex, args, args.tasks)
    return time.perf_counter() - t0, check


RUNNERS = {"primes": run_primes, "bigdata": run_bigdata, "shared": run_shared, "tiny": run_tiny}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", action="store_true", help="print the CSV header and exit")
    ap.add_argument("--workload", choices=list(RUNNERS), default="primes")
    ap.add_argument("--mode", choices=["threads", "processes", "sequential"])
    ap.add_argument("--variant", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--start-method", choices=["fork", "spawn", "forkserver"], default=None,
                    help="processes only; default is the platform default (forkserver on Linux, 3.14)")
    ap.add_argument("--size", type=int, default=None, help="workload size, see module docstring")
    ap.add_argument("--tasks", type=int, default=None)
    ap.add_argument("--batches", type=int, default=1, help="tiny/fresh: number of pools created")
    ap.add_argument("--passes", type=int, default=3,
                    help="bigdata: scans over the data with the same pool (pass 1 is cold)")
    ap.add_argument("--chunksize", type=int, default=1, help="tiny: map() chunksize")
    args = ap.parse_args()

    if args.header:
        csv.writer(sys.stdout).writerow(FIELDS)
        return
    if not args.mode:
        ap.error("--mode is required")
    args.size = args.size if args.size is not None else DEFAULT_SIZE[args.workload]
    args.tasks = args.tasks if args.tasks is not None else DEFAULT_TASKS[args.workload]
    args.variant = args.variant or VARIANTS[args.workload][0]
    if args.workload == "bigdata":
        ok = ("arg", "init") if args.mode == "processes" else ("list", "tuple")
        if args.variant == VARIANTS["bigdata"][0] and args.mode != "processes":
            args.variant = "list"
        if args.variant not in ok:
            ap.error(f"--variant for bigdata --mode {args.mode} must be one of {ok}")
    if args.variant not in VARIANTS[args.workload]:
        ap.error(f"--variant for {args.workload} must be one of {VARIANTS[args.workload]}")
    if args.mode == "sequential":
        args.workers = 1
    if args.mode == "processes":
        args.start_method = args.start_method or mp.get_start_method()
    else:
        args.start_method = ""

    args.pass_seconds = ""
    seconds, check = RUNNERS[args.workload](args)

    build, _ = build_info()
    _, gil_enabled = build_info()  # checked after the run, in case an import re-enabled the GIL
    model, cores = cpu_info()
    row = {
        "python": platform.python_version(),
        "build": build,
        "gil_enabled": gil_enabled,
        "workload": args.workload,
        "variant": args.variant,
        "mode": args.mode,
        "workers": args.workers,
        "start_method": args.start_method,
        "size": args.size,
        "tasks": args.tasks,
        "batches": args.batches if args.variant == "fresh" else "",
        "chunksize": args.chunksize if args.workload == "tiny" else "",
        "seconds": f"{seconds:.4f}",
        "check": check,
        "passes": args.passes if args.workload == "bigdata" else "",
        "pass_seconds": args.pass_seconds,
        "cpus": os.cpu_count(),
        "physical_cores": cores,
        "cpu_model": model,
    }
    csv.writer(sys.stdout, lineterminator="").writerow([row[k] for k in FIELDS[:-1]])
    print()


if __name__ == "__main__":
    main()
