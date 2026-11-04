#!/usr/bin/env python3
"""Single-thread cost of the free-threaded build, operation by operation.
Times small pure-Python loops in one thread (no pool). Run once per build:

    python3.14  -I slowdown.py > results/slowdown_gil.csv
    python3.14t -I slowdown.py > results/slowdown_ft.csv

`perf` is not used, so this shows which kind of operation is slower, not
which CPU instructions. Each kernel runs REPEATS times; the median is kept.
"""
import statistics
import sys
import time

N = 3_000_000
REPEATS = 15


def is_prime(n):
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


def k_is_prime():  # the paper's workload
    c = 0
    for n in range(2_500_000, 2_500_000 + N // 60):
        if is_prime(n):
            c += 1
    return c


def k_empty_loop():
    for _ in range(N):
        pass


def k_small_int_add():  # results stay in the immortal small-int cache
    x = 0
    for _ in range(N):
        x = (x + 1) & 127


def k_int_add():  # results are heap-allocated ints
    x = 1000
    for _ in range(N):
        x = x + 1


def k_int_mul_cmp():  # i * i <= n
    n = 10**12
    c = 0
    for i in range(1000, 1000 + N):
        if i * i <= n:
            c += 1


def k_int_mod():  # n % i
    n = 2_999_999
    c = 0
    for i in range(1000, 1000 + N):
        if n % i == 0:
            c += 1


def k_float_add():
    x = 0.5
    for _ in range(N):
        x = x + 1.5


def _noop(a):
    return a


def k_call():
    for i in range(N):
        _noop(i)


def k_list_index():
    lst = list(range(1000))
    for i in range(N):
        lst[i & 511]


def k_list_store():
    lst = [0] * 1000
    for i in range(N):
        lst[i & 511] = i


def k_dict_get():
    d = {"a": 1, "b": 2, "c": 3}
    for _ in range(N):
        d["b"]


class _C:
    def __init__(self):
        self.a = 1


def k_attr_get():
    o = _C()
    for _ in range(N):
        o.a


def k_alloc_obj():
    for _ in range(N // 3):
        _C()


KERNELS = [v for k, v in sorted(globals().items()) if k.startswith("k_")]


def main():
    build = "free-threaded" if not sys._is_gil_enabled() else "gil"
    print("build,kernel,median_ns_per_iter,spread_pct")
    for fn in KERNELS:
        ts = []
        for _ in range(REPEATS):
            t = time.perf_counter()
            fn()
            ts.append(time.perf_counter() - t)
        med = statistics.median(ts)
        spread = (max(ts) - min(ts)) / med * 100
        print(f"{build},{fn.__name__[2:]},{med / N * 1e9:.2f},{spread:.0f}", flush=True)


if __name__ == "__main__":
    main()
