#!/usr/bin/env bash
# Runs the experiments and writes results/<experiment>.csv
#
#   ./run_all.sh                    # all experiments
#   ./run_all.sh primes bigdata     # only these
#
# Experiments:
#   primes   CPU-bound, no shared data. Threads (GIL, free-threaded) vs
#            processes under fork / spawn / forkserver.          (paper Fig. 1 + #3)
#   bigdata  large shared input, 10/100/500 MiB. Processes get the data per
#            task (arg) or once per worker (init), under each start method. (#1 + #3)
#   shared   shared mutable state: global lock / sharded locks / local-then-merge
#            on both threads and processes; Manager as reference.  (#2)
#   tiny     many short tasks; one reused pool vs a new pool per batch. (#4)
#   shared_lists, tiny_batched   follow-ups for two confounds (run by name)
#   slowdown, memcheck           single-thread cost; memory method check (run by name)
#   forkfaults                   page faults behind fork's slow first pass (run by name)
#   buildconfig                  configure flags of both builds (run by name)
#
# Every run goes through memwatch.py, which reads the kernel peak memory of the
# run (cgroup memory.peak) afterwards, so nothing samples the benchmark process.
#
# Knobs (environment variables):
#   REPEATS=5    repeats per configuration; analysis uses the median
#   MAXW=$(nproc)
set -euo pipefail
cd "$(dirname "$0")"

REPEATS=${REPEATS:-5}
MAXW=${MAXW:-$(nproc)}
EXPS=("$@")
[ ${#EXPS[@]} -eq 0 ] && EXPS=(primes bigdata shared tiny)

uv python install 3.14 3.14t >/dev/null
PY=$(uv python find 3.14)
PYT=$(uv python find 3.14t)
echo "GIL build:           $PY"
echo "Free-threaded build: $PYT"
mkdir -p results

# 1, 2, 4, 8, ... up to MAXW, plus MAXW itself if it is not a power of two
ALLW=()
w=1
while [ "$w" -le "$MAXW" ]; do ALLW+=("$w"); w=$((w * 2)); done
[ "${ALLW[-1]}" -ne "$MAXW" ] && ALLW+=("$MAXW")
SMS=(fork spawn forkserver)

OUT=""
N=0
TOTAL=0
# run <python> <bench.py args...>   one run, one CSV row appended to $OUT
run() {
  local py=$1; shift
  N=$((N + 1))
  printf '[%d/%d] %-10s %s ' "$N" "$TOTAL" "$(basename "$py")" "$*"
  # -W error::DeprecationWarning makes a fork-with-threads warning fail loudly
  "$PY" -I memwatch.py -- "$py" -I -W error::DeprecationWarning bench.py "$@" \
    | tee -a "$OUT" | awk -F, '{printf "%ss  %sMB\n", $13, $NF}'
}

start() {  # start <experiment> <runs per repeat>
  OUT=results/$1.csv
  N=0
  TOTAL=$(( REPEATS * $2 ))
  "$PY" -I bench.py --header > "$OUT"
  echo "== $1: $TOTAL runs -> $OUT"
}

exp_primes() {
  start primes $(( 2 + ${#ALLW[@]} * (2 + ${#SMS[@]}) ))
  for r in $(seq "$REPEATS"); do
    run "$PY"  --workload primes --mode sequential
    run "$PYT" --workload primes --mode sequential
    for w in "${ALLW[@]}"; do
      # setups interleaved so heat/throttling hits all of them equally
      run "$PY"  --workload primes --mode threads --workers "$w"
      run "$PYT" --workload primes --mode threads --workers "$w"
      for sm in "${SMS[@]}"; do
        run "$PY" --workload primes --mode processes --workers "$w" --start-method "$sm"
      done
    done
  done
}

exp_bigdata() {
  local sizes=(10 100 500) ws=(4 "$MAXW")
  # each run scans the data 3 times with one pool (bench.py --passes default);
  # pass 1 is cold, passes 2-3 warm. list vs tuple: see bench.py docstring.
  start bigdata $(( ${#sizes[@]} * (2 + ${#ws[@]} * (3 + 2 * ${#SMS[@]})) ))
  for r in $(seq "$REPEATS"); do
    for s in "${sizes[@]}"; do
      run "$PY"  --workload bigdata --mode sequential --size "$s"
      run "$PYT" --workload bigdata --mode sequential --size "$s"
      for w in "${ws[@]}"; do
        run "$PY"  --workload bigdata --mode threads --variant list  --workers "$w" --size "$s"
        run "$PYT" --workload bigdata --mode threads --variant list  --workers "$w" --size "$s"
        run "$PYT" --workload bigdata --mode threads --variant tuple --workers "$w" --size "$s"
        for v in arg init; do
          for sm in "${SMS[@]}"; do
            run "$PY" --workload bigdata --mode processes --variant "$v" --start-method "$sm" --workers "$w" --size "$s"
          done
        done
      done
    done
  done
}

exp_shared() {
  local ws=(1 4 "$MAXW") vs=(global_lock sharded local_merge) size=2000000
  start shared $(( 2 + ${#ws[@]} * (3 * ${#vs[@]} + 1) ))
  for r in $(seq "$REPEATS"); do
    run "$PY"  --workload shared --mode sequential --variant local_merge --size "$size"
    run "$PYT" --workload shared --mode sequential --variant local_merge --size "$size"
    for w in "${ws[@]}"; do
      for v in "${vs[@]}"; do
        run "$PY"  --workload shared --mode threads   --variant "$v" --workers "$w" --size "$size"
        run "$PYT" --workload shared --mode threads   --variant "$v" --workers "$w" --size "$size"
        run "$PY"  --workload shared --mode processes --variant "$v" --workers "$w" --size "$size"
      done
      # Manager is ~100x slower per update; smaller size, compare as updates/second
      run "$PY" --workload shared --mode processes --variant manager --workers "$w" --size $(( size / 100 ))
    done
  done
}

exp_tiny() {
  local iters=(100 1000 10000) ws=(4 "$MAXW") tasks=20000 batches=20
  start tiny $(( ${#iters[@]} * ${#ws[@]} * 3 * (2 + ${#SMS[@]}) ))
  for r in $(seq "$REPEATS"); do
    for it in "${iters[@]}"; do
      for w in "${ws[@]}"; do
        local big=$(( tasks / (w * 4) ))  # a sensible chunksize, as a careful user would pick
        for cfg in "reuse 1" "reuse $big" "fresh 1"; do
          set -- $cfg
          local common=(--workload tiny --variant "$1" --chunksize "$2" --batches "$batches" --tasks "$tasks" --size "$it" --workers "$w")
          run "$PY"  "${common[@]}" --mode threads
          run "$PYT" "${common[@]}" --mode threads
          for sm in "${SMS[@]}"; do
            run "$PY" "${common[@]}" --mode processes --start-method "$sm"
          done
        done
      done
    done
  done
}

# --- follow-up runs for two confounds found in shared/tiny ---------------
# shared: does the free-threaded list's internal lock explain why sharding
# barely helps threads? Compare one list vs one list per shard.
exp_shared_lists() {
  local ws=(4 "$MAXW") size=2000000
  start shared_lists $(( ${#ws[@]} * 4 ))
  for r in $(seq "$REPEATS"); do
    for w in "${ws[@]}"; do
      run "$PYT" --workload shared --mode threads   --variant sharded       --workers "$w" --size "$size"
      run "$PYT" --workload shared --mode threads   --variant sharded_lists --workers "$w" --size "$size"
      run "$PY"  --workload shared --mode threads   --variant sharded_lists --workers "$w" --size "$size"
      run "$PY"  --workload shared --mode processes --variant sharded       --workers "$w" --size "$size"
    done
  done
}

# tiny: threads with tasks batched by hand (ThreadPoolExecutor.map ignores
# chunksize) vs processes with the same chunksize.
exp_tiny_batched() {
  local iters=(100 1000 10000) ws=(4 "$MAXW") tasks=20000
  start tiny_batched $(( ${#iters[@]} * ${#ws[@]} * 5 ))
  for r in $(seq "$REPEATS"); do
    for it in "${iters[@]}"; do
      for w in "${ws[@]}"; do
        local big=$(( tasks / (w * 4) ))
        local common=(--workload tiny --variant reuse --chunksize "$big" --tasks "$tasks" --size "$it" --workers "$w")
        run "$PY"  "${common[@]}" --mode threads
        run "$PYT" "${common[@]}" --mode threads
        for sm in "${SMS[@]}"; do
          run "$PY" "${common[@]}" --mode processes --start-method "$sm"
        done
      done
    done
  done
}

# Single-thread cost per kind of operation, 3 rounds per build, alternating.
exp_slowdown() {
  local out=results/slowdown.csv
  echo "build,kernel,median_ns_per_iter,spread_pct" > "$out"
  for r in 1 2 3; do
    "$PY"  -I slowdown.py | tail -n +2 >> "$out"
    "$PYT" -I slowdown.py | tail -n +2 >> "$out"
    echo "slowdown round $r done"
  done
}

# Sampled PSS vs kernel memory.peak: why memwatch.py does not sample.
exp_memcheck() {
  local out=results/mem_validation.txt
  : > "$out"
  check() {
    local label=$1; shift
    for r in 1 2 3; do
      printf '%s: ' "$label" >> "$out"
      systemd-run --user --scope -q -p MemoryAccounting=yes "$PY" -I memcheck.py "$@" >> "$out"
    done
  }
  local b=(-I bench.py --workload bigdata --size 500)
  for w in 1 4 8 16; do
    check "fork init w=$w" "$PY" "${b[@]}" --mode processes --variant init --start-method fork --workers "$w"
  done
  check "fork arg w=16"   "$PY"  "${b[@]}" --mode processes --variant arg --start-method fork --workers 16
  check "spawn init w=16" "$PY"  "${b[@]}" --mode processes --variant init --start-method spawn --workers 16
  check "FT list w=16"    "$PYT" "${b[@]}" --mode threads --variant list --workers 16
  check "FT tuple w=16"   "$PYT" "${b[@]}" --mode threads --variant tuple --workers 16
  echo "memcheck done -> $out"
}

# Page faults per pass: why fork is slower than forkserver in the first bigdata/arg pass.
exp_forkfaults() {
  "$PY" forkfaults.py 3 > results/forkfaults.csv
  echo "forkfaults done -> results/forkfaults.csv"
}

# The configure flags each interpreter was built with.
exp_buildconfig() {
  local cfg='import sysconfig; print(sysconfig.get_config_var("CONFIG_ARGS"))'
  { echo "# GIL build"; "$PY" -I -c "$cfg"; echo "# free-threaded build"; "$PYT" -I -c "$cfg"; } > results/build_config.txt
  echo "buildconfig done -> results/build_config.txt"
}

for e in "${EXPS[@]}"; do "exp_$e"; done
echo "Done."
