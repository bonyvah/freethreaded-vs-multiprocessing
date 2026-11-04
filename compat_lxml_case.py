#!/usr/bin/env python3
"""Case study for the paper: lxml on the free-threaded build (run in a venv with lxml).

    python -I compat_lxml_case.py cost        primes on 16 threads, before and after
                                              `from lxml import etree` re-enables the GIL
    python -I -X gil=0 compat_lxml_case.py crash shared|private
                                              16 threads, one shared tree or one each
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

XML = "<a>" + "<b>1</b>" * 100 + "</a>"


def count(bounds):
    c = 0
    for n in range(*bounds):
        i, p = 3, n % 2 != 0
        while p and i * i <= n:
            p = n % i != 0
            i += 2
        c += p
    return c


def timed():
    step = 500_000 // 64
    t = time.perf_counter()
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(count, [(k * step, (k + 1) * step) for k in range(64)]))
    return time.perf_counter() - t


if sys.argv[1] == "cost":
    before = timed()
    gil_before = sys._is_gil_enabled()
    import lxml
    gil_lxml = sys._is_gil_enabled()
    from lxml import etree
    assert etree.fromstring(XML).findtext("b") == "1"
    print(f"after `import lxml` gil={gil_lxml}; ", end="")
    print(f"before: {before:.3f}s gil={gil_before}  after: {timed():.3f}s gil={sys._is_gil_enabled()}")
else:
    from lxml import etree
    shared = etree.fromstring(XML)

    def work():
        for _ in range(100):
            tree = shared if sys.argv[2] == "shared" else etree.fromstring(XML)
            assert len(tree.xpath("//b")) == 100
            assert etree.fromstring(etree.tostring(tree)).findtext("b") == "1"

    ts = [threading.Thread(target=work) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("ok", "gil =", sys._is_gil_enabled())
