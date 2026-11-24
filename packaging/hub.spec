# PyInstaller spec, run by packaging/build.py. One folder, two programs sharing it:
#   AlpacaHub.exe   no console window: what the Start menu and autostart run, and what runs each driver process
#   alpaca-hub.exe  console: --stop, --version, and running in the foreground to watch the log
#
# The whole standard library goes in, not only what the hub imports: driver processes run on this exe and a driver
# may use any of it (REFACTOR.md, "What a driver may use"). No driver goes in: each has its own installer.
import importlib.util
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
BUILD = os.path.join(ROOT, "build")
ICON = os.path.join(BUILD, "app.ico")
VERSION_FILE = os.path.join(BUILD, "version_info.txt")

SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "pydoc_data", "this",
        "antigravity", "__phello__", "_tkinter", "_testcapi", "_testinternalcapi", "_testbuffer", "_testimportmultiple",
        "_testmultiphase", "_testsinglephase", "_testclinic", "_testlimitedcapi", "_ctypes_test", "xxsubtype",
        "xxlimited", "xxlimited_35"}


def stdlib():
    out = []
    for name in sorted(sys.stdlib_module_names):
        if name in SKIP or name.startswith("_test"):
            continue
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except Exception:
            continue
        out.append(name)
        try:
            out += collect_submodules(name, filter=lambda n: ".test" not in n and ".tests" not in n
                                      and "idle_test" not in n)
        except Exception:
            pass
    return out


hidden = stdlib() + ["serial", "serial.tools.list_ports", "serial.tools.list_ports_windows", "segno"]
hidden += collect_submodules("hub")          # hub.api, hub.devserver, hub.core: what drivers import

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    hiddenimports=hidden,
    # pages are built at run time from these folders (hub/core/page.py; each next to the module that uses it)
    datas=[(os.path.join(ROOT, "hub", "ui", "kit"), os.path.join("hub", "ui", "kit")),
           (os.path.join(ROOT, "hub", "overview_ui"), os.path.join("hub", "overview_ui"))],
    excludes=sorted(SKIP),
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, icon=ICON, version=VERSION_FILE, upx=False, debug=False, strip=False)
gui = EXE(pyz, a.scripts, [], name="AlpacaHub", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="alpaca-hub", console=True, **common)

coll = COLLECT(gui, cli, a.binaries, a.datas, upx=False, name="AlpacaHub")
