#!/usr/bin/env python3
"""Library compatibility study: on the free-threaded build, does a package install
from a wheel, does importing it turn the GIL back on, and does it still work when
16 threads use it at once? The normal GIL build is the control, so a failure on
both builds counts as a problem with the package or the test, not free threading.

    python3 compat.py --top 100 --work /path/to/scratch

Packages: the top N of hugovk's top-pypi-packages list, plus every package with a
smoke test in compat_probe.py. Wheels only (--only-binary :all:), so no package
build scripts run; importing a package does run its code. Results are appended to
results/compat.jsonl per package, and packages already there are skipped, so an
interrupted run resumes.

Phases for each package and build, each its own process:
  install   fresh venv, uv pip install --only-binary :all:
  import    top-level modules; GIL state afterwards
  deep      every compiled extension in the venv (import can be lazy)
  smoke     16 threads, SMOKE_RUNS separate runs; on the free-threaded build, if
            the GIL came back, also run with -X gil=0 to see what would break
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "compat_probe.py")
OUT = os.path.join(HERE, "results", "compat.jsonl")
SNAPSHOT = os.path.join(HERE, "results", "top-pypi-snapshot.json")
LIST_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
BUILDS = {"free-threaded": "3.14t", "gil": "3.14"}
SMOKE_RUNS = 3
lock = threading.Lock()


def sh(cmd, timeout, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "timeout", "", ""


def probe(py, args, cwd, timeout, flags=()):
    rc, out, err = sh([py, "-I", *flags, PROBE, *args], timeout, cwd)
    result = {"rc": rc}
    if rc == 0:
        try:
            result.update(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            result["rc"] = "bad-output"
    else:
        result["stderr"] = err.strip()[-300:]
    return result


def smoke(py, name, cwd, flags=()):
    runs = [probe(py, ["smoke", name], cwd, 180, flags) for _ in range(SMOKE_RUNS)]
    bad = [r for r in runs if r["rc"] != 0 or r.get("smoke") == "fail"]
    return {"status": "fail" if bad else "pass", "failed_runs": len(bad),
            "first_bad": bad[0] if bad else None, "gil_after": runs[-1].get("gil_after")}


def study(name, build, work, has_smoke):
    version = BUILDS[build]
    d = os.path.join(work, build, re.sub(r"\W", "_", name))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    py = os.path.join(d, "venv", "bin", "python")
    run = os.path.join(d, "run")
    os.makedirs(run)
    res = {"package": name, "build": build}
    try:
        rc, _, err = sh(["uv", "venv", "-q", "--python", version, os.path.join(d, "venv")], 120)
        if rc != 0:
            return {**res, "install": "venv-failed", "install_error": err[-300:]}
        rc, _, err = sh(["uv", "pip", "install", "-q", "--python", py, "--only-binary", ":all:", name], 900)
        if rc != 0:
            return {**res, "install": "failed", "install_error": str(err)[-400:]}
        res["install"] = "ok"
        res["import"] = probe(py, ["import", name], run, 300)
        res["deep"] = probe(py, ["deep"], run, 600)
        if has_smoke:
            res["smoke"] = smoke(py, name, run)
            gil_back = res["smoke"]["gil_after"] or (res["import"].get("gil_after") is True)
            if build == "free-threaded" and gil_back:
                res["smoke_forced_nogil"] = smoke(py, name, run, ("-X", "gil=0"))
        return res
    finally:
        shutil.rmtree(d, ignore_errors=True)


def job(name, build, work, has_smoke):
    r = study(name, build, work, has_smoke)
    with lock, open(OUT, "a") as f:
        f.write(json.dumps(r) + "\n")
    imp = r.get("import", {})
    print(f"{build:13} {name:28} install={r.get('install')} gil_after={imp.get('gil_after')} "
          f"smoke={r.get('smoke', {}).get('status', '-')}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--work", required=True, help="scratch directory for the throwaway venvs")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()

    if not os.path.exists(SNAPSHOT):
        with urllib.request.urlopen(LIST_URL) as resp:
            data = json.load(resp)
        data["rows"] = data["rows"][:500]
        with open(SNAPSHOT, "w") as f:
            json.dump(data, f)
    with open(SNAPSHOT) as f:
        top = [r["project"] for r in json.load(f)["rows"][: a.top]]
    top = [p for p in top if re.fullmatch(r"[A-Za-z0-9._-]+", p)]
    smokes = set(json.loads(subprocess.check_output([sys.executable, "-I", PROBE, "list-smokes"])))
    names = top + sorted(smokes - set(top))

    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            done = {(r["package"], r["build"]) for r in map(json.loads, f)}
    jobs = [(n, b) for n in names for b in BUILDS if (n, b) not in done]
    print(f"{len(names)} packages, {len(jobs)} jobs to do", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda x: job(x[0], x[1], a.work, x[0] in smokes), jobs))


if __name__ == "__main__":
    main()
