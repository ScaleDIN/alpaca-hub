"""Runs each driver folder's own tests (drivers/<key>/tests/test_<key>_*.py) as part of
python -m unittest discover -s tests, so one command still runs everything."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = (os.path.join(ROOT, "drivers"), os.path.join(ROOT, "docs", "example-driver"))   # and the guide's example


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for key, folder in sorted((k, os.path.join(r, k)) for r in ROOTS for k in os.listdir(r)):
        tdir = os.path.join(folder, "tests")
        if os.path.isdir(tdir):
            for p in (folder, tdir):
                if p not in sys.path:
                    sys.path.insert(0, p)
            suite.addTests(loader.discover(tdir, pattern=f"test_{key}_*.py", top_level_dir=tdir))
    return suite
