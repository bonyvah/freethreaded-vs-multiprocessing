#!/usr/bin/env python3
"""Turn results.csv into speedup.png (two panels) and print a summary table.

Run with:  uv run --python 3.14 --with matplotlib plot.py [results.csv]
"""
import csv
import statistics
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SRC = sys.argv[1] if len(sys.argv) > 1 else "results.csv"

# label, colour (fixed categorical order), marker (second cue besides colour)
SETUPS = {
    ("gil", "threads"): ("Threads, GIL (3.14)", "#2a78d6", "o"),
    ("free-threaded", "threads"): ("Threads, free-threaded (3.14t)", "#eb6834", "s"),
    ("gil", "processes"): ("Processes (3.14)", "#1baf7a", "^"),
}
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e6e5e1", "#fcfcfb"

times, mems, primes = defaultdict(list), defaultdict(list), set()
meta = {}
with open(SRC) as f:
    for row in csv.DictReader(f):
        key = (row["build"], row["mode"], int(row["workers"]))
        times[key].append(float(row["seconds"]))
        mems[key].append(float(row["peak_mem_mb"]))
        primes.add(row["primes"])
        meta = row
        if row["build"] == "free-threaded" and row["gil_enabled"] == "True":
            print("WARNING: GIL was re-enabled in a free-threaded run; results are not valid")

if len(primes) != 1:
    print(f"WARNING: runs disagree on the prime count: {primes}")

med_t = {k: statistics.median(v) for k, v in times.items()}
med_m = {k: statistics.median(v) for k, v in mems.items()}
baseline = med_t[("gil", "threads", 1)]  # one thread, normal Python = sequential
workers = sorted({k[2] for k in med_t})

# ---------------------------------------------------------------- table --
print(f"\nSequential baseline (1 thread, GIL build): {baseline:.2f} s\n")
print("| Setup | Workers | Median time (s) | Speedup vs sequential | Peak memory (MB) |")
print("|---|---:|---:|---:|---:|")
for (build, mode), (label, _, _) in SETUPS.items():
    for w in workers:
        k = (build, mode, w)
        if k in med_t:
            print(f"| {label} | {w} | {med_t[k]:.2f} | {baseline / med_t[k]:.2f}x | {med_m[k]:.0f} |")

# ---------------------------------------------------------------- chart --
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Inter", "Calibri", "Carlito", "DejaVu Sans"],
    "font.size": 10,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.titlecolor": INK,
})
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.2), facecolor=SURFACE)

for ax in (ax1, ax2):
    ax.set_facecolor(SURFACE)
    ax.set_xscale("log", base=2)
    ax.set_xticks(workers)
    ax.set_xticklabels([str(w) for w in workers])
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("Workers")

# ideal linear scaling as a muted reference
ax1.plot(workers, workers, linestyle="--", color=MUTED, linewidth=1, zorder=1)
ax1.annotate("ideal", (workers[-1], workers[-1]), textcoords="offset points",
             xytext=(4, 0), color=MUTED, fontsize=9, va="center")

for (build, mode), (label, color, marker) in SETUPS.items():
    ws = [w for w in workers if (build, mode, w) in med_t]
    sp = [baseline / med_t[(build, mode, w)] for w in ws]
    mm = [med_m[(build, mode, w)] for w in ws]
    style = dict(color=color, marker=marker, markersize=6, linewidth=2,
                 markeredgecolor=SURFACE, markeredgewidth=1.2, label=label, zorder=3)
    ax1.plot(ws, sp, **style)
    ax2.plot(ws, mm, **style)
    # direct label at the end of each speedup line
    ax1.annotate(f"{sp[-1]:.1f}x", (ws[-1], sp[-1]), textcoords="offset points",
                 xytext=(6, 0), color=INK2, fontsize=9, va="center")

ax1.set_title("Speedup over sequential Python (higher is better)", loc="left", fontsize=11)
ax1.set_ylabel("Speedup (x)")
ax1.set_ylim(bottom=0)
ax2.set_title("Peak memory, all processes (lower is better)", loc="left", fontsize=11)
ax2.set_ylabel("Peak PSS (MB)")
ax2.set_ylim(bottom=0)

handles, labels = ax1.get_legend_handles_labels()
fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False,
           labelcolor=INK2, bbox_to_anchor=(0.5, -0.01))
fig.text(0.01, 0.995,
         f"Counting primes below {int(meta['limit']):,} in pure Python · Python {meta['python']} · "
         f"{meta['cpus']} logical CPUs · start method: forkserver · median of repeats",
         fontsize=8.5, color=MUTED, va="top")
fig.tight_layout(rect=(0, 0.07, 1, 0.95))
fig.savefig("speedup.png", dpi=200, facecolor=SURFACE)
print("\nSaved speedup.png")
