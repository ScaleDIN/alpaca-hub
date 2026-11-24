#!/usr/bin/env python3
"""Builds the Alpaca Hub's installer and its drivers' installers (REFACTOR.md step 11).

    python packaging/build.py hub                     the hub: dist/AlpacaHub-<version>-Setup.exe
    python packaging/build.py driver wcover           one driver of this repository (drivers/wcover)
    python packaging/build.py driver --folder D:/my/thermo   a driver folder anywhere (someone else's driver)
    python packaging/build.py all                     the hub and every folder in drivers/
    python packaging/build.py release --url-base https://...  [--previous releases.json]
                                                      the version list and files to publish (packaging/release.py)

Options: --skip-tests (don't run the test suite first).

The hub: a build environment in build/venv (PyInstaller, pyserial, segno), the tests, an icon and version resource,
PyInstaller (build/pyi/dist/AlpacaHub/ with AlpacaHub.exe and alpaca-hub.exe), a smoke test of the built exe (a
simulated panel from an "installed" driver folder plus a built-in simulated Optec hub, then --stop), then Inno Setup.

A driver: its driver.json is checked (key = folder name, version like 1.2.0, api, its package), the folder is staged
in build/driver-<key>/ without tests or caches, then Inno Setup (packaging/driver.iss):
dist/AlpacaHub-<key>-<version>-Setup.exe. A driver installer carries no Python: drivers run on the hub's exe.

Needs Inno Setup 6 (ISCC.exe); set ISCC to its path if it isn't in a standard place.
"""
import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
VENV = os.path.join(BUILD, "venv")
VPY = os.path.join(VENV, "Scripts", "python.exe")
APP = "AlpacaHub"

sys.path.insert(0, ROOT)
from hub import VERSION  # noqa: E402
from hub.api import API_VERSION  # noqa: E402


def step(msg):
    print(f"\n=== {msg}", flush=True)


def run(cmd, **kw):
    print("  > " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


# ------------------------------------------------------------------ icon (pure Python PNG-in-ICO)
def _png(size, pixel):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixel(x, y, size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def _icon_pixel(x, y, n):
    """Dark rounded tile with a hub: a centre dot joined to three devices (the device pages' night red)."""
    ss = 4
    bg, fg = (29, 35, 48), (224, 83, 61)
    nodes = [(0.5, 0.25), (0.27, 0.68), (0.73, 0.68)]
    cov_tile = cov_sym = 0
    for sy in range(ss):
        for sx in range(ss):
            u, v = (x + (sx + 0.5) / ss) / n, (y + (sy + 0.5) / ss) / n
            r = 0.2
            dx, dy = max(abs(u - 0.5) - (0.5 - r), 0), max(abs(v - 0.5) - (0.5 - r), 0)
            if dx * dx + dy * dy > r * r:
                continue
            cov_tile += 1
            hit = math.hypot(u - 0.5, v - 0.53) <= 0.1
            for nx, ny in nodes:
                hit = hit or math.hypot(u - nx, v - ny) <= 0.075
                # spoke from the centre to the node
                px, py = u - 0.5, v - 0.53
                qx, qy = nx - 0.5, ny - 0.53
                t = max(0.0, min(1.0, (px * qx + py * qy) / (qx * qx + qy * qy)))
                hit = hit or math.hypot(px - t * qx, py - t * qy) <= 0.028
            cov_sym += hit
    if cov_tile == 0:
        return (0, 0, 0, 0)
    k = cov_sym / cov_tile
    return (*(int(bg[i] * (1 - k) + fg[i] * k) for i in range(3)), int(255 * cov_tile / (ss * ss)))


def make_icon(path):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [_png(s, _icon_pixel) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    entries = b""
    for s, p in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(p), off)
        off += len(p)
    with open(path, "wb") as f:
        f.write(head + entries + b"".join(pngs))


def make_version_file(path):
    v = tuple(int(x) for x in VERSION.split(".")) + (0,)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Open source'),
      StringStruct('FileDescription', 'Alpaca Hub: one ASCOM Alpaca service for every device on this PC'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductName', 'Alpaca Hub'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")


def find_iscc():
    cands = [os.environ.get("ISCC", ""),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
             r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    sys.exit("Inno Setup 6 not found: install it (winget install JRSoftware.InnoSetup) or set ISCC")


def common_files():
    os.makedirs(BUILD, exist_ok=True)
    make_icon(os.path.join(BUILD, "app.ico"))
    make_version_file(os.path.join(BUILD, "version_info.txt"))


# ------------------------------------------------------------------ drivers
def _vt(v):
    return tuple(int(x) for x in v.split("."))


def read_driver(folder):
    """Checks a driver folder as the hub will (hub/catalog.py) and returns its driver.json, plus "pkg"."""
    folder = os.path.abspath(folder)
    key = os.path.basename(folder.rstrip("\\/"))
    p = os.path.join(folder, "driver.json")
    if not os.path.isfile(p):
        sys.exit(f"{folder}: no driver.json")
    d = json.load(open(p, encoding="utf-8"))
    problems = []
    if d.get("key") != key:
        problems.append(f"key {d.get('key')!r} isn't the folder's name {key!r}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
        problems.append(f"key {key!r}: lower-case letters, digits and _ only, starting with a letter")
    if not re.fullmatch(r"\d{1,6}(\.\d{1,6}){0,3}", str(d.get("version", ""))):
        problems.append(f"version {d.get('version')!r} isn't like 1.2.0")
    if not isinstance(d.get("title"), str) or not d["title"]:
        problems.append("title missing")
    if d.get("api") != API_VERSION:
        problems.append(f"api {d.get('api')!r}: this hub has API {API_VERSION}")
    if "min_hub" in d:
        if not re.fullmatch(r"\d{1,6}(\.\d{1,6}){0,3}", str(d["min_hub"])):
            problems.append(f"min_hub {d['min_hub']!r} isn't like 0.3.0")
        elif _vt(d["min_hub"]) > _vt(VERSION):
            problems.append(f"min_hub {d['min_hub']} is newer than this hub ({VERSION})")
    pkg = (d.get("module") or f"{key}.driver").split(".")[0]
    if not os.path.isdir(os.path.join(folder, pkg)):
        problems.append(f"its package folder {pkg!r} is missing")
    if problems:
        sys.exit(f"{folder}: " + "; ".join(problems))
    d["pkg"], d["folder"] = pkg, folder
    return d


def stage_driver(d):
    out = os.path.join(BUILD, f"driver-{d['key']}")
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(os.path.join(d["folder"], d["pkg"]), os.path.join(out, d["pkg"]),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test_*.py"))
    shutil.copy(os.path.join(d["folder"], "driver.json"), os.path.join(out, "driver.json"))
    return out


def build_driver(folder, iscc):
    d = read_driver(folder)
    step(f"Driver {d['key']} {d['version']} ({d['title']})")
    src = stage_driver(d)
    guid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpaca-hub-driver:{d['key']}")).upper()
    run([iscc, "/Q", f"/DKey={d['key']}", f"/DTitle={d['title']}", f"/DAppVersion={d['version']}",
         f"/DApi={d['api']}", f"/DMinHub={d.get('min_hub', '0')}", f"/DPkg={d['pkg']}", f"/DAppGuid={guid}", f"/DSrc={src}",
         os.path.join(HERE, "driver.iss")])
    out = os.path.join(DIST, f"AlpacaHub-{d['key']}-{d['version']}-Setup.exe")
    print(f"  {out} ({os.path.getsize(out) // 1024} KB)")
    return out


# ------------------------------------------------------------------ the hub
def smoke_test(exe_dir):
    """The built exe runs drivers from "installed" folders (the panel and Optec, simulated), each in its own process
    on that exe, serves their pages, and stops cleanly with --stop. The exe itself has no driver."""
    exe = os.path.join(exe_dir, "alpaca-hub.exe")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    print("  " + out)
    assert VERSION in out, out
    port = "11298"
    with tempfile.TemporaryDirectory() as tmp:
        pdata = os.path.join(tmp, "ProgramData")
        for key in ("wcover", "optec"):
            d = read_driver(os.path.join(ROOT, "drivers", key))
            shutil.copytree(stage_driver(d), os.path.join(pdata, "AlpacaHub", "drivers", d["key"], d["version"]))
        env = dict(os.environ, ProgramData=pdata)
        cfg = os.path.join(tmp, "cfg")
        args = ["--config-dir", cfg, "--http-port", port]
        p = subprocess.Popen([exe, "--simulate", "panel,lynx", "--bind", "127.0.0.1", "--log", "none"] + args,
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            j = None
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/hub.json", timeout=1) as r:
                        j = json.loads(r.read())
                    if len(j["devices"]) >= 2 and all(x["up"] for x in j["devices"]):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            assert j and sorted(x["slug"] for x in j["devices"]) == ["focuslynx", "panel"], f"devices: {j}"
            drivers = {x["name"]: x for x in j["drivers"]}
            assert sorted(drivers) == ["optec", "wcover"], f"the exe has drivers of its own: {sorted(drivers)}"
            for key in drivers:
                assert drivers[key]["source"] == "installed" and drivers[key]["state"] == "running", drivers
            print("  panel and Optec up from installed driver folders, each in its own process")
            for slug in ("panel", "focuslynx"):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/{slug}/", timeout=5) as r:
                    page = r.read().decode()
                assert "<html" in page.lower() and "K." in page, f"{slug} page not built"
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                assert "Alpaca Hub" in r.read().decode()
            print("  pages OK")
            run([exe, "--stop", "--wait"] + args, env=env)
            p.wait(timeout=30)
            print(f"  exited with {p.returncode}")
            assert p.returncode == 0
        finally:
            if p.poll() is None:
                p.kill()


def build_hub(iscc, skip_tests):
    step("Build environment")
    if not os.path.isfile(VPY):
        run([sys.executable, "-m", "venv", VENV])
    run([VPY, "-m", "pip", "install", "-q", "--disable-pip-version-check", "pyinstaller>=6.10", "-r",
         os.path.join(ROOT, "requirements.txt")])
    if not skip_tests:
        step("Tests")
        run([VPY, os.path.join(ROOT, "tools", "run_tests.py")], timeout=1200)   # the files side by side (~1.5 min)
    step("PyInstaller")
    pyi_dist = os.path.join(BUILD, "pyi", "dist")
    run([VPY, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", pyi_dist, "--workpath", os.path.join(BUILD, "pyi", "work"), os.path.join(HERE, "hub.spec")])
    step("Smoke test of the built exe")
    smoke_test(os.path.join(pyi_dist, APP))
    step("Inno Setup")
    run([iscc, "/Q", f"/DAppVersion={VERSION}", f"/DApiVersion={API_VERSION}", os.path.join(HERE, "hub.iss")])
    out = os.path.join(DIST, f"AlpacaHub-{VERSION}-Setup.exe")
    print(f"  {out} ({os.path.getsize(out) // (1024 * 1024)} MB)")
    return out


def main():
    ap = argparse.ArgumentParser(prog="build.py", description="Builds the Alpaca Hub's and its drivers' installers")
    ap.add_argument("what", choices=["hub", "driver", "all", "release"])
    ap.add_argument("key", nargs="?", help="driver: a folder name under drivers/")
    ap.add_argument("--folder", help="driver: a driver folder anywhere (its name is the driver's key)")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--url-base", help="release: where the new installers will be downloaded from (https://...)")
    ap.add_argument("--previous", help="release: the releases.json published last time")
    a = ap.parse_args()
    os.chdir(ROOT)
    os.makedirs(DIST, exist_ok=True)
    if a.what == "release":
        import release
        try:
            out, feed, files, new_names = release.release(ROOT, DIST, a.url_base, a.previous)
        except release.ReleaseError as e:
            sys.exit("Release not written:\n  " + "\n  ".join(e.problems))
        step(f"Release written to {out} (nothing published)")
        for name, e in [("Hub", feed["hub"])] + sorted(feed["drivers"].items()):
            new = f"{name} {e['version']}" in new_names
            print(f"  {name:12s} {e['version']:8s} {'NEW' if new else 'unchanged'}")
        print(f"  {len(files)} installers attached; NOTES.md is the release page's text")
        return
    iscc = find_iscc()
    common_files()
    made = []
    if a.what in ("hub", "all"):
        made.append(build_hub(iscc, a.skip_tests))
    if a.what == "driver":
        if not (a.key or a.folder):
            sys.exit("driver: give a key (drivers/<key>) or --folder")
        made.append(build_driver(a.folder or os.path.join(ROOT, "drivers", a.key), iscc))
    if a.what == "all":
        for key in sorted(os.listdir(os.path.join(ROOT, "drivers"))):
            if os.path.isfile(os.path.join(ROOT, "drivers", key, "driver.json")):
                made.append(build_driver(os.path.join(ROOT, "drivers", key), iscc))
    step("Done")
    for m in made:
        print("  " + m)


if __name__ == "__main__":
    main()
