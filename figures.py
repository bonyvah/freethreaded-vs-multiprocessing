#!/usr/bin/env python3
"""Paper figures from results/*.csv: fig1_primes.png and fig2_bigdata.png.

    uv run --no-project --python 3.14 --with matplotlib figures.py
"""
import csv
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"
INK2, MUTED, GRID, SURFACE = "#52514e", "#898781", "#e6e5e1", "#fcfcfb"
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Calibri", "Carlito", "DejaVu Sans"],
    "font.size": 9.5,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.titlecolor": "#0b0b0b",
})


def load(name):
    groups = defaultdict(list)
    with open(f"results/{name}.csv") as f:
        for r in csv.DictReader(f):
            groups[(r["build"], r["mode"], r["variant"], r["start_method"],
                    int(r["workers"]), int(r["size"]))].append(r)
    return {k: (statistics.median(float(r["seconds"]) for r in v),
                statistics.median(float(r["peak_mem_mb"]) for r in v)) for k, v in groups.items()}


def style(ax, xs, xlabel):
    ax.set_facecolor(SURFACE)
    ax.set_xscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels([str(x) for x in xs])
    ax.minorticks_off()
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel(xlabel)


def line(ax, xs, ys, label, color, marker):
    ax.plot(xs, ys, color=color, marker=marker, markersize=6, linewidth=2, label=label,
            markeredgecolor=SURFACE, markeredgewidth=1, zorder=3)


def finish(fig, ax, path, ncol):
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=ncol, frameon=False, labelcolor=INK2)
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    fig.savefig(path, dpi=220, facecolor=SURFACE)


def fig1():
    d = load("primes")
    workers = [1, 2, 4, 8, 16]
    base = d[("gil", "sequential", "-", "", 1, 3_000_000)][0]
    setups = [
        ("Threads, GIL build", ("gil", "threads", "-", ""), BLUE, "o"),
        ("Processes, fork", ("gil", "processes", "-", "fork"), AQUA, "^"),
        ("Processes, forkserver", ("gil", "processes", "-", "forkserver"), YELLOW, "D"),
        ("Processes, spawn", ("gil", "processes", "-", "spawn"), MAGENTA, "v"),
        ("Threads, free-threaded build", ("free-threaded", "threads", "-", ""), ORANGE, "s"),
    ]
    fig, (a, b) = plt.subplots(1, 2, figsize=(8.2, 3.45), facecolor=SURFACE)
    for ax in (a, b):
        style(ax, workers, "Workers")
    a.plot(workers, workers, linestyle="--", color=MUTED, linewidth=1, zorder=1)
    a.annotate("ideal", (8, 8), textcoords="offset points", xytext=(-4, 7), color=MUTED, ha="right")
    for label, key, color, marker in setups:
        rows = [d[(*key, w, 3_000_000)] for w in workers]
        line(a, workers, [base / r[0] for r in rows], label, color, marker)
        line(b, workers, [r[1] for r in rows], label, color, marker)
    a.set_title("Speedup over sequential Python (higher is better)", loc="left", fontsize=10)
    a.set_ylabel("Speedup (×)")
    a.set_ylim(0, 16.5)
    a.set_yticks([0, 4, 8, 12, 16])
    b.set_title("Peak memory (lower is better)", loc="left", fontsize=10)
    b.set_ylabel("Peak memory (MB)")
    b.set_ylim(bottom=0)
    finish(fig, a, "fig1_primes.png", 3)


def fig2():
    d = load("bigdata")
    sizes = [10, 100, 500]
    setups = [
        ("Threads, GIL build (list)", ("gil", "threads", "list", ""), BLUE, "o"),
        ("Free-threaded (list)", ("free-threaded", "threads", "list", ""), ORANGE, "s"),
        ("Free-threaded (tuple)", ("free-threaded", "threads", "tuple", ""), VIOLET, "P"),
        ("Fork + initializer", ("gil", "processes", "init", "fork"), AQUA, "^"),
        ("Forkserver, data with each task", ("gil", "processes", "arg", "forkserver"), YELLOW, "D"),
        ("Spawn + initializer", ("gil", "processes", "init", "spawn"), MAGENTA, "v"),
    ]
    fig, (a, b) = plt.subplots(1, 2, figsize=(8.2, 3.45), facecolor=SURFACE)
    for ax in (a, b):
        style(ax, sizes, "Size of shared data (MiB)")
        ax.set_yscale("log")
    for label, key, color, marker in setups:
        rows = [d[(*key, 16, s)] for s in sizes]
        line(a, sizes, [r[0] for r in rows], label, color, marker)
        line(b, sizes, [r[1] for r in rows], label, color, marker)
    a.set_yticks([0.01, 0.1, 1, 10])
    a.set_yticklabels(["0.01", "0.1", "1", "10"])
    b.set_yticks([100, 1000, 10000])
    b.set_yticklabels(["100", "1,000", "10,000"])
    for ax in (a, b):
        ax.minorticks_off()
    a.set_title("Total time, three passes (lower is better)", loc="left", fontsize=10)
    a.set_ylabel("Seconds, log scale")
    b.set_title("Peak memory (lower is better)", loc="left", fontsize=10)
    b.set_ylabel("MB, log scale")
    finish(fig, a, "fig2_bigdata.png", 3)


if __name__ == "__main__":
    fig1()
    fig2()
