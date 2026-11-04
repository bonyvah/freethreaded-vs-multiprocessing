#!/usr/bin/env python3
"""Runs inside one package's virtual environment; prints one JSON object. Modes:

  import DIST   import DIST's top-level modules; did the GIL come back?
  deep          import every compiled extension of every installed package
                (top-level imports can be lazy and miss them)
  smoke DIST    run DIST's smoke test once, then concurrently in 16 threads

Each mode is its own process, so a crash shows up as a bad exit code in compat.py.
"""
import importlib
import importlib.machinery
import importlib.metadata as md
import json
import re
import sys
import threading
import traceback
import warnings

THREADS = 16
SKIP = {"test", "tests", "docs", "doc", "examples", "benchmarks", "scripts", "__pycache__"}
GIL_MSG = re.compile(r"enabled to load module '([^']+)'")


def gil():
    return sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True


def top_modules(dist):
    """Shallowest importable module per top-level name, so a namespace package
    such as google/protobuf gives google.protobuf and not just google."""
    best = {}
    for f in dist.files or ():
        parts = f.parts
        if not parts or parts[0].endswith((".dist-info", ".data", ".libs")) or parts[0] in SKIP or parts[0] == "..":
            continue
        name = None
        if len(parts) == 1 and any(parts[0].endswith(s) for s in importlib.machinery.all_suffixes()):
            name = (parts[0].split(".")[0],)
        elif parts[-1] == "__init__.py":
            name = parts[:-1]
        if not name or not all(p.isidentifier() for p in name):
            continue
        cur = best.get(name[0], [])
        if not cur or len(name) < len(cur[0]):
            best[name[0]] = [name]
        elif len(name) == len(cur[0]) and name not in cur:
            cur.append(name)
    return sorted(".".join(n) for ns in best.values() for n in ns)


def try_import(names):
    errors, offender = {}, None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for n in names:
            try:
                importlib.import_module(n)
            except BaseException as e:  # a broken package may raise anything
                errors[n] = f"{type(e).__name__}: {e}"[:300]
    for w in caught:
        m = GIL_MSG.search(str(w.message))
        if m:
            offender = offender or m.group(1)
    return errors, offender


def info(dist):
    files = [str(f) for f in dist.files or ()]
    compiled = any(f.endswith((".so", ".pyd")) for f in files)
    wheel = dist.read_text("WHEEL") or ""
    tags = re.findall(r"^Tag: (.+)$", wheel, re.M)
    return {"version": dist.version, "compiled": compiled, "tag": tags[0] if tags else ""}


def mode_import(name):
    dist = md.distribution(name)
    mods = top_modules(dist)
    errors, offender = try_import(mods)
    return {**info(dist), "modules": mods, "import_errors": errors,
            "gil_offender": offender, "gil_after": gil()}


def mode_deep():
    suffixes = sorted(importlib.machinery.EXTENSION_SUFFIXES, key=len, reverse=True)
    names = set()
    for dist in md.distributions():
        for f in dist.files or ():
            s = str(f)
            suf = next((x for x in suffixes if s.endswith(x)), None)
            if not suf or ".libs/" in s or s.startswith(".."):
                continue
            parts = s[: -len(suf)].split("/")
            if all(p.isidentifier() for p in parts):
                names.add(".".join(parts))
    errors, offender = try_import(sorted(names))
    return {"extensions": len(names), "deep_errors": len(errors),
            "deep_error_sample": list(errors.items())[:3],
            "gil_offender": offender, "gil_after": gil()}


def mode_smoke(name):
    if name not in SMOKES:
        return {"smoke": "none"}
    setup, work, iters = SMOKES[name]
    shared = setup()
    for _ in range(2):
        work(shared)
    errors, barrier = [], threading.Barrier(THREADS)

    def runner():
        barrier.wait()
        for _ in range(iters):
            try:
                work(shared)
            except BaseException as e:
                errors.append("".join(traceback.format_exception_only(e)).strip()[:300])
                return

    ts = [threading.Thread(target=runner) for _ in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return {"smoke": "fail" if errors else "pass", "failed_threads": len(errors),
            "first_error": errors[0] if errors else "", "gil_after": gil()}


# ------------------------------------------------------------ smoke tests --
# setup() builds what the threads share; work(shared) runs concurrently and
# asserts on a known answer, so wrong results count as failures, not only crashes.

def s_numpy():
    import numpy as np
    return np, np.arange(100_000, dtype=float)
def w_numpy(s):
    np, a = s
    assert a.sum() == 4999950000.0
    assert np.sort(a[::-1])[0] == 0.0
    assert np.dot(a[:100], a[:100]) == 328350.0

def s_pandas():
    import pandas as pd
    return pd.DataFrame({"k": [i % 10 for i in range(10_000)], "v": range(10_000)})
def w_pandas(df):
    g = df.groupby("k")["v"].sum()
    assert int(g.sum()) == 49_995_000
    assert len(df[df.v > 9_989]) == 10

def s_scipy():
    import numpy as np
    from scipy import linalg, sparse
    A = np.eye(50) * 2.0
    return np, linalg, A, sparse.identity(1000, format="csr") * 3.0
def w_scipy(s):
    np, linalg, A, M = s
    assert abs(linalg.solve(A, np.ones(50)).sum() - 25.0) < 1e-9
    assert (M @ np.ones(1000)).sum() == 3000.0

def s_sklearn():
    import numpy as np
    from sklearn.linear_model import LinearRegression
    X = np.arange(200.0).reshape(-1, 2)
    return LinearRegression, X, X.sum(axis=1) * 2.0 + 1.0
def w_sklearn(s):
    LR, X, y = s
    assert abs(LR().fit(X, y).predict(X[:1])[0] - y[0]) < 1e-6

def s_mpl():
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    return FigureCanvasAgg, Figure
def w_mpl(s):
    Canvas, Figure = s
    fig = Figure(figsize=(2, 2))
    canvas = Canvas(fig)
    fig.add_subplot().plot([0, 1, 2], [0, 1, 4])
    canvas.draw()
    assert canvas.buffer_rgba().nbytes > 0

def s_pillow():
    from PIL import Image
    return Image
def w_pillow(Image):
    import io
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 0, 0)).resize((32, 32)).save(b, "PNG")
    b.seek(0)
    assert Image.open(b).convert("RGB").getpixel((3, 3)) == (255, 0, 0)

def s_lxml():
    from lxml import etree
    return etree, etree.fromstring("<a>" + "<b>1</b>" * 100 + "</a>")
def w_lxml(s):
    etree, tree = s
    assert len(tree.xpath("//b")) == 100
    assert etree.fromstring(etree.tostring(tree)).findtext("b") == "1"

def s_yaml():
    import yaml
    return yaml
def w_yaml(yaml):
    d = {"a": [1, 2, 3], "b": {"c": "x"}}
    assert yaml.load(yaml.dump(d, Dumper=getattr(yaml, "CSafeDumper", yaml.SafeDumper)),
                     Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) == d

def s_crypto():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return hashes, AESGCM(b"k" * 32)
def w_crypto(s):
    hashes, gcm = s
    h = hashes.Hash(hashes.SHA256()); h.update(b"abc")
    assert h.finalize().hex().startswith("ba7816bf")
    assert gcm.decrypt(b"n" * 12, gcm.encrypt(b"n" * 12, b"hi", None), None) == b"hi"

def s_import(mod):
    return lambda: importlib.import_module(mod)

def w_roundtrip_orjson(m):
    d = {"a": [1, 2.5, "x"], "b": None}
    assert m.loads(m.dumps(d)) == d
def w_roundtrip_ujson(m):
    d = {"a": [1, 2, "x"], "b": None}
    assert m.loads(m.dumps(d)) == d
def w_msgpack(m):
    d = {"a": [1, 2, "x"]}
    assert m.unpackb(m.packb(d)) == d

def s_pydantic():
    from pydantic import BaseModel
    class M(BaseModel):
        a: int
        b: list[str]
    return M
def w_pydantic(M):
    assert M.model_validate({"a": "3", "b": ["x"]}).a == 3
    assert M(a=1, b=[]).model_dump() == {"a": 1, "b": []}

def s_sqlalchemy():
    import sqlalchemy as sa
    return sa
def w_sqlalchemy(sa):
    e = sa.create_engine("sqlite://")
    with e.connect() as c:
        assert c.execute(sa.text("select 1 + 2")).scalar() == 3
    e.dispose()

def w_regex(m):
    assert m.findall(r"\p{L}+", "ab 12 cd") == ["ab", "cd"]
def w_markupsafe(m):
    assert str(m.escape("<a>")) == "&lt;a&gt;"
def w_charset(m):
    assert m.from_bytes("héllo wörld".encode()).best() is not None

def s_protobuf():
    from google.protobuf import struct_pb2
    return struct_pb2
def w_protobuf(pb):
    s = pb.Struct(); s["a"] = 1.0
    t = pb.Struct(); t.ParseFromString(s.SerializeToString())
    assert t["a"] == 1.0

def s_cffi():
    import cffi
    f = cffi.FFI(); f.cdef("size_t strlen(const char*);")
    return f, f.dlopen(None)
def w_cffi(s):
    f, lib = s
    assert lib.strlen(f.new("char[]", b"abc")) == 3

def s_pyarrow():
    import pyarrow as pa
    import pyarrow.compute as pc
    return pa, pc, pa.table({"v": list(range(10_000))})
def w_pyarrow(s):
    pa, pc, t = s
    assert pc.sum(t["v"]).as_py() == 49_995_000
    assert t.slice(0, 10).num_rows == 10

def s_polars():
    import polars as pl
    return pl, pl.DataFrame({"k": [i % 10 for i in range(10_000)], "v": list(range(10_000))})
def w_polars(s):
    pl, df = s
    assert df.group_by("k").agg(pl.col("v").sum())["v"].sum() == 49_995_000

def s_zmq():
    import zmq
    return zmq
def w_zmq(zmq):
    ctx = zmq.Context()
    a, b = ctx.socket(zmq.PAIR), ctx.socket(zmq.PAIR)
    addr = f"inproc://t{threading.get_ident()}"
    a.bind(addr); b.connect(addr)
    a.send(b"x")
    assert b.recv() == b"x"
    a.close(); b.close(); ctx.term()

def w_bcrypt(m):
    h = m.hashpw(b"pw", m.gensalt(rounds=4))
    assert m.checkpw(b"pw", h)
def w_zstd(m):
    assert m.ZstdDecompressor().decompress(m.ZstdCompressor().compress(b"abc" * 100), max_output_size=300) == b"abc" * 100

def s_rapidfuzz():
    from rapidfuzz import fuzz
    return fuzz
def w_rapidfuzz(f):
    assert f.ratio("abc", "abc") == 100.0

def s_ruamel():
    from ruamel.yaml import YAML
    return YAML
def w_ruamel(YAML):
    import io
    y, b = YAML(), io.StringIO()
    y.dump({"a": 1}, b)
    assert y.load(b.getvalue())["a"] == 1

def w_wrapt(m):
    @m.decorator
    def deco(fn, inst, args, kw):
        return fn(*args, **kw) + 1
    assert deco(lambda: 1)() == 2
def w_greenlet(m):
    g = m.greenlet(lambda: m.getcurrent().parent.switch(5))
    assert g.switch() == 5
def w_frozenlist(m):
    f = m.FrozenList([1, 2]); f.append(3); f.freeze()
    assert list(f) == [1, 2, 3]
def w_multidict(m):
    d = m.MultiDict([("a", 1), ("a", 2)])
    assert d.getall("a") == [1, 2]
def w_yarl(m):
    assert m.URL("http://a.b/c?x=1").query["x"] == "1"
def w_jiter(m):
    assert m.from_json(b'{"a": [1, 2]}') == {"a": [1, 2]}
def w_psutil(m):
    assert m.cpu_count() >= 1 and m.Process().memory_info().rss > 0

def s_tokenizers():
    from tokenizers import Tokenizer, models, pre_tokenizers
    t = Tokenizer(models.WordLevel({"a": 0, "b": 1, "[U]": 2}, unk_token="[U]"))
    t.pre_tokenizer = pre_tokenizers.Whitespace()
    return t
def w_tokenizers(t):
    assert t.encode("a b z").ids == [0, 1, 2]

def s_numba():
    import numba
    return numba.njit(lambda n: sum(range(n)))
def w_numba(f):
    assert f(1000) == 499500

def s_h5py():
    import h5py
    return h5py
def w_h5py(h5py):
    with h5py.File(f"t{threading.get_ident()}.h5", "w", driver="core", backing_store=False) as f:
        f["d"] = list(range(100))
        assert f["d"][50] == 50

def s_uvloop():
    import uvloop
    return uvloop
def w_uvloop(u):
    import asyncio
    async def f():
        await asyncio.sleep(0)
        return 7
    loop = u.new_event_loop()
    assert loop.run_until_complete(f()) == 7
    loop.close()

def s_requests():
    import requests
    return requests
def w_requests(r):
    assert r.Request("GET", "http://a.b/", params={"x": 1}).prepare().url == "http://a.b/?x=1"


# dist name: (setup, work, iterations per thread)
SMOKES = {
    "numpy": (s_numpy, w_numpy, 200),
    "pandas": (s_pandas, w_pandas, 30),
    "scipy": (s_scipy, w_scipy, 50),
    "scikit-learn": (s_sklearn, w_sklearn, 20),
    "matplotlib": (s_mpl, w_mpl, 3),
    "pillow": (s_pillow, w_pillow, 50),
    "lxml": (s_lxml, w_lxml, 100),
    "pyyaml": (s_yaml, w_yaml, 100),
    "cryptography": (s_crypto, w_crypto, 200),
    "orjson": (s_import("orjson"), w_roundtrip_orjson, 2000),
    "ujson": (s_import("ujson"), w_roundtrip_ujson, 2000),
    "msgpack": (s_import("msgpack"), w_msgpack, 2000),
    "pydantic": (s_pydantic, w_pydantic, 1000),
    "sqlalchemy": (s_sqlalchemy, w_sqlalchemy, 30),
    "regex": (s_import("regex"), w_regex, 1000),
    "markupsafe": (s_import("markupsafe"), w_markupsafe, 5000),
    "charset-normalizer": (s_import("charset_normalizer"), w_charset, 50),
    "protobuf": (s_protobuf, w_protobuf, 1000),
    "cffi": (s_cffi, w_cffi, 2000),
    "pyarrow": (s_pyarrow, w_pyarrow, 200),
    "polars": (s_polars, w_polars, 50),
    "pyzmq": (s_zmq, w_zmq, 20),
    "bcrypt": (s_import("bcrypt"), w_bcrypt, 20),
    "zstandard": (s_import("zstandard"), w_zstd, 500),
    "rapidfuzz": (s_rapidfuzz, w_rapidfuzz, 5000),
    "ruamel-yaml": (s_ruamel, w_ruamel, 30),
    "wrapt": (s_import("wrapt"), w_wrapt, 1000),
    "greenlet": (s_import("greenlet"), w_greenlet, 2000),
    "frozenlist": (s_import("frozenlist"), w_frozenlist, 5000),
    "multidict": (s_import("multidict"), w_multidict, 5000),
    "yarl": (s_import("yarl"), w_yarl, 1000),
    "jiter": (s_import("jiter"), w_jiter, 5000),
    "psutil": (s_import("psutil"), w_psutil, 500),
    "tokenizers": (s_tokenizers, w_tokenizers, 1000),
    "numba": (s_numba, w_numba, 500),
    "h5py": (s_h5py, w_h5py, 30),
    "uvloop": (s_uvloop, w_uvloop, 50),
    "requests": (s_requests, w_requests, 500),
}


def main():
    mode = sys.argv[1]
    if mode == "list-smokes":
        print(json.dumps(sorted(SMOKES)))
    elif mode == "import":
        print(json.dumps(mode_import(sys.argv[2])))
    elif mode == "deep":
        print(json.dumps(mode_deep()))
    elif mode == "smoke":
        print(json.dumps(mode_smoke(sys.argv[2])))


if __name__ == "__main__":
    main()
