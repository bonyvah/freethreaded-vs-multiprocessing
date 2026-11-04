# Free-threaded CPython vs multiprocessing: benchmark kit

Compares, on four pure-Python workloads:

1. Threads on Python 3.14 (normal GIL build), the baseline that should not scale
2. Threads on Python 3.14t (free-threaded build)
3. Processes on Python 3.14 (`ProcessPoolExecutor`) under the `fork`, `spawn`
   and `forkserver` start methods

| Experiment | What it tests | Paper item |
|---|---|---|
| `primes`  | CPU-bound, independent tasks, almost no data exchanged | Fig. 1, start methods (#3) |
| `bigdata` | 10/100/500 MiB shared read-only input; processes get it per task (`arg`) or once per worker (`init`) | #1 + #3 |
| `shared`  | shared mutable counters: one lock, 64 sharded locks, local-then-merge; `Manager` as reference | #2 |
| `tiny`    | many short tasks; one reused pool vs a new pool per batch, chunksize 1 vs a sensible chunksize | #4 |

Wall time includes pool creation and shutdown, so process start-up cost is
counted. Sequential runs are a plain loop with no pool. Peak memory, in MiB, is the kernel's cgroup `memory.peak` for the benchmark and all
its descendants: `memwatch.py` runs each benchmark in its own systemd user scope and
reads the value afterwards. Nothing samples the benchmark while it runs, and pages
shared by several processes count once. Sampling PSS (the draft's method) missed
fork copy-on-write peaks by up to 3x and slowed runs slightly; `memcheck.py`
reproduces that comparison. Runs use `-W error::DeprecationWarning`, so a
fork-with-threads warning would fail the run.

Each CSV row also records the CPU model and physical core count, and whether
the GIL was still disabled at the end of the run.

## Run it (Ubuntu)

Your system Python is not touched. `uv` installs 3.14 and 3.14t into your home folder.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # once, if you don't have uv
cd freethreaded-vs-multiprocessing
./run_all.sh                     # all experiments, writes results/<experiment>.csv
./run_all.sh primes bigdata      # or just some
python3 analyze.py               # median tables + sanity checks
```

Faster test first: `MAXW=2 REPEATS=1 ./run_all.sh tiny`

One run by hand:

```bash
python3.14 memwatch.py -- python3.14 -I bench.py --workload bigdata \
    --mode processes --variant init --start-method spawn --workers 16 --size 100
```

`results.csv` / `plot.py` / `speedup.png` are the first experiment from the
draft (in-process memory sampler, forkserver only), kept as they were.

`results/v1_sampled_pss/` holds the first full run, measured with the sampled PSS
method the paper rejects. It is kept to show that comparison; the paper uses no
other number from it.

`figures.py` draws the paper's figures from `results/` (`fig1_primes.png`, `fig2_bigdata.png`).

`slowdown.py` times single operations in one thread on each build, for the
single-thread slowdown. `compat.py` is the library compatibility study.
`scancost.py` times the scan loop one operation at a time, on real records and on
records sharing one float and string, to find the cost of scanning data on the
free-threaded build.
`forkfaults.py` counts page faults per pass, to explain why the first pass under
fork is slower when data is sent with each task.

## Where each paper result comes from

| Paper | Data in `results/` | Made by |
|---|---|---|
| Table 1, Fig. 1 | `primes.csv` | `./run_all.sh primes` |
| Table 2, Fig. 2 | `bigdata.csv` | `./run_all.sh bigdata` |
| 30-repeat headline ratios | `bigdata_repeat.csv` | `./run_all.sh bigdata_repeat` |
| Fork first-pass faults | `forkfaults.csv` | `./run_all.sh forkfaults` |
| Table 3 | `shared.csv`, `shared_lists.csv` | `./run_all.sh shared shared_lists` |
| Table 4 | `tiny_batched.csv`; pool cost and unchunked times from `tiny.csv` | `./run_all.sh tiny tiny_batched` |
| Single-thread overhead | `slowdown.csv`, `primes.csv`, `bigdata.csv`, `scancost.csv`, `scancost_ops.txt` | `./run_all.sh slowdown scancost` |
| Table 5 | `compat.jsonl`, `top-pypi-snapshot.json` | `python3 compat.py --work <scratch dir>` |
| `lxml` case | `compat_lxml_case.txt` | `compat_lxml_case.py` |
| Rejecting sampled memory | `mem_validation.txt` | `./run_all.sh memcheck` |
| Build flags | `build_config.txt` | `./run_all.sh buildconfig` |

`compat.py` installs packages from PyPI, so it needs network access and a scratch
folder (`--work`). The other experiments are local. The 16 packages beyond the top
100 are the entries in `compat_probe.py`'s `SMOKES` that are not in the snapshot's
top 100.

## For clean numbers

- Plug the charger in and set the power mode to Performance (`powerprofilesctl set performance`)
- Close the browser and other heavy apps
- Don't touch the laptop while it runs

## Known limitations

- One machine (8 cores / 16 hardware threads)
- `memory.peak` includes kernel memory charged to the group (page tables, a little
  page cache), about 5 MB in an empty run
- `bigdata` sizes are MiB of Python objects on the GIL build (302.2 B per
  record); the same records take 350.4 B each on the free-threaded build
