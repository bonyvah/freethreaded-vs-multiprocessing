#!/usr/bin/env python3
"""Why is the bigdata scan slower on the free-threaded build, single-threaded?

Times the scan loop from bench.py one piece at a time, on the real records, in one
thread. Each kernel adds one operation to the one before, so differences between
kernels give the cost of each operation. Also reports the specialized bytecodes
the interpreter ended up using, because a build that cannot specialize an operation
runs a slower, generic version of it.

    python3.14  scancost.py > results/scancost_gil.csv
    python3.14t scancost.py > results/scancost_ft.csv

    python3.14 scancost.py --ops    # specialized bytecodes per kernel

Times are nanoseconds per record, median of REPEATS.
"""
import dis
import statistics
import sys
import time

import bench

SIZES = (10, 100, 500)
REPEATS = 9


def k1_loop(recs, n):
    for i in range(n):
        pass


def k2_index(recs, n):
    for i in range(n):
        r = recs[i]


def k3_lookup(recs, n):
    for i in range(n):
        r = recs[i]
        r["cat"]


def k4_compare(recs, n):
    for i in range(n):
        r = recs[i]
        if r["cat"] == "beta":
            pass


def k5_value(recs, n):  # also reads the float on one record in four
    for i in range(n):
        r = recs[i]
        if r["cat"] == "beta":
            r["value"]


def k6_add(recs, n):  # also adds it up
    s = 0.0
    for i in range(n):
        r = recs[i]
        if r["cat"] == "beta":
            s += r["value"]
    return s


def k7_scan(recs, n):  # the benchmark's own loop: also reads the tag string
    bench._scan(recs, 0, n)


def k8_scan_shared(recs, n):
    # The same loop on records whose float and tag are one shared object each, so
    # the loop does the same operations but stops touching a different float and
    # string per record. See main().
    bench._scan(recs, 0, n)


KERNELS = (k1_loop, k2_index, k3_lookup, k4_compare, k5_value, k6_add, k7_scan, k8_scan_shared)
SHARED_VALUE, SHARED_TAG = 79.19, "rec-000000000"


def shared_records(n):
    return [{"id": i, "value": SHARED_VALUE, "cat": bench.CATS[i % 4], "tag": SHARED_TAG} for i in range(n)]


def opnames(fn):
    wanted = ("BINARY_OP", "COMPARE_OP", "FOR_ITER", "STORE_SUBSCR")
    return sorted({i.opname for i in dis.get_instructions(fn, adaptive=True) if i.opname.startswith(wanted)})


def main():
    build = "free-threaded" if not sys._is_gil_enabled() else "gil"
    if "--ops" in sys.argv:
        recs = bench.make_records(1)
        for fn in KERNELS[:6] + (bench._scan,):
            for _ in range(5):
                fn(recs, 0, len(recs)) if fn is bench._scan else fn(recs, len(recs))
            print(f"{build} {fn.__name__}: {' '.join(opnames(fn))}")
        return
    print("build,size_mib,kernel,median_ns_per_record,first_ns_per_record,spread_pct", flush=True)
    for size in SIZES:
        for fn in KERNELS:
            # new records per kernel, so the first timing is a first pass over fresh data
            recs = bench.make_records(size)
            if fn is k8_scan_shared:
                recs = shared_records(len(recs))
            n = len(recs)
            ts = []
            for _ in range(REPEATS):
                t = time.perf_counter()
                fn(recs, n)
                ts.append(time.perf_counter() - t)
            med = statistics.median(ts)
            print(f"{build},{size},{fn.__name__},{med / n * 1e9:.1f},{ts[0] / n * 1e9:.1f},"
                  f"{(max(ts) - min(ts)) / med * 100:.0f}", flush=True)


if __name__ == "__main__":
    main()
