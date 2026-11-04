#!/usr/bin/env python3
"""Summarise results/<experiment>.csv: median time, median peak memory and
(bigdata) median per-pass times for every configuration. Also checks that
every run of the same workload/size produced the same answer and that the
GIL stayed off on the free-threaded build.

    python analyze.py                  # all results/*.csv
    python analyze.py results/bigdata.csv
"""
import csv
import glob
import statistics
import sys
from collections import defaultdict

KEY = ["workload", "size", "build", "mode", "variant", "start_method",
       "workers", "chunksize", "batches"]


def label(r):
    who = {"gil": "GIL", "free-threaded": "FT"}[r["build"]]
    s = f"{who} {r['mode']}"
    if r["start_method"]:
        s += f"/{r['start_method']}"
    if r["variant"] not in ("-", ""):
        s += f" [{r['variant']}]"
    if r["chunksize"] not in ("", "1"):
        s += f" chunk={r['chunksize']}"
    return s


def summarise(path):
    rows = list(csv.DictReader(open(path)))
    if not rows:
        print(f"{path}: empty")
        return
    groups = defaultdict(list)
    answers = defaultdict(set)
    for r in rows:
        groups[tuple(r[k] for k in KEY)].append(r)
        answers[(r["workload"], r["size"], r.get("tasks", ""))].add(r["check"])
        if r["build"] == "free-threaded" and r["gil_enabled"] == "True":
            print(f"WARNING: GIL re-enabled in a free-threaded run: {label(r)}")
    for k, v in answers.items():
        if len(v) != 1:
            print(f"WARNING: runs disagree on the answer for {k}: {v}")

    print(f"\n=== {path}  ({len(rows)} runs, {rows[0]['cpu_model']}, "
          f"{rows[0]['physical_cores']} cores / {rows[0]['cpus']} CPUs)")
    hdr = f"{'size':>7} {'workers':>3}  {'setup':<38} {'n':>2} {'median s':>9} {'spread':>7} {'peak MB':>8}"
    has_pass = any(r.get("pass_seconds") for r in rows)
    if has_pass:
        hdr += "   pass1 / pass2 / pass3 (s)"
    print(hdr)
    for key in sorted(groups, key=lambda k: (int(k[1]), int(k[6]), k[2], k[3], k[4], k[5])):
        g = groups[key]
        t = [float(r["seconds"]) for r in g]
        m = [float(r["peak_mem_mb"]) for r in g]
        med = statistics.median(t)
        spread = (max(t) - min(t)) / med * 100 if med else 0
        line = (f"{key[1]:>7} {key[6]:>3}  {label(g[0]):<38} {len(g):>2} {med:>9.4f} "
                f"{spread:>6.0f}% {statistics.median(m):>8.1f}")
        if has_pass and g[0].get("pass_seconds"):
            per = list(zip(*[[float(x) for x in r["pass_seconds"].split(";")] for r in g]))
            line += "   " + " / ".join(f"{statistics.median(p):.4f}" for p in per)
        print(line)


def main():
    paths = sys.argv[1:] or sorted(glob.glob("results/*.csv"))
    for p in paths:
        summarise(p)


if __name__ == "__main__":
    main()
