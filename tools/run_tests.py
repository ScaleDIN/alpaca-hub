"""Runs the whole test suite with its files side by side, each in its own Python process.

    python tools/run_tests.py            all of it (what packaging/build.py runs)
    python tools/run_tests.py -j 4       at most 4 at a time (default: the number of CPUs, at most 8)

The same tests as `python -m unittest discover -s tests` (the hub's tests in tests/, plus each driver folder's own
in drivers/<key>/tests and docs/example-driver/<key>/tests), which still works and runs them one after another.
Most of the suite's time is waiting (listening windows, driver processes starting, timeouts on purpose), so running
the files at once takes a fraction of the time. Each file gets its own process, temp folders and ports already
(every test uses port 0 and its own tempfile.mkdtemp()), so they don't meet.
Exit code 0 when every file passed; a failing file's whole output is printed.
"""
import argparse
import concurrent.futures as cf
import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER_ROOTS = (os.path.join(ROOT, "drivers"), os.path.join(ROOT, "docs", "example-driver"))


def units():
    """(name, command, cwd, extra sys.path) per test file, the slowest-known first so they don't end up last."""
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
        name = os.path.basename(p)
        if name == "test_driver_folders.py":      # it only gathers the driver folders' tests: run those directly
            continue
        out.append((f"tests/{name}", ["-m", "unittest", "discover", "-s", "tests", "-p", name], ROOT, []))
    for root in DRIVER_ROOTS:
        for key in sorted(os.listdir(root)):
            folder = os.path.join(root, key)
            tdir = os.path.join(folder, "tests")
            for p in sorted(glob.glob(os.path.join(tdir, f"test_{key}_*.py"))):
                name = os.path.basename(p)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                out.append((rel, ["-m", "unittest", "discover", "-s", tdir, "-t", tdir, "-p", name], ROOT,
                            [folder, tdir]))
    first = ("test_contract", "test_supervisor", "test_hub", "test_relay", "test_install", "test_optec")
    out.sort(key=lambda u: next((i for i, k in enumerate(first) if k in u[0]), len(first)))
    return out


def run(unit, python):
    name, args, cwd, paths = unit
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(paths + [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ALPACA_HUB_UPDATE_FEED"] = ""      # no update check online from a test hub (hub/updates.py)
    t = time.monotonic()
    r = subprocess.run([python] + args, cwd=cwd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = r.stdout + r.stderr
    m = re.search(r"Ran (\d+) tests?", out)
    return name, r.returncode, int(m.group(1)) if m else 0, time.monotonic() - t, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("--python", default=sys.executable, help="the Python to run the tests with")
    a = ap.parse_args(argv)
    todo = units()
    t0 = time.monotonic()
    failed, total = [], 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for name, code, n, secs, out in (f.result() for f in cf.as_completed([ex.submit(run, u, a.python) for u in todo])):
            total += n
            ok = code == 0 and n > 0
            print(f"{'ok  ' if ok else 'FAIL'} {secs:6.1f}s {n:4d} tests  {name}", flush=True)
            if not ok:
                failed.append((name, out))
    for name, out in failed:
        print(f"\n===== {name} =====\n{out}")
    print(f"\nRan {total} tests in {len(todo)} files, {time.monotonic() - t0:.1f}s with {a.jobs} at a time: "
          + ("OK" if not failed else f"FAILED ({len(failed)} files)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
