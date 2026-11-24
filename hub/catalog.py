"""Where drivers are, and which version of each to run (REFACTOR.md step 6).

Three places, looked at every scan:
- installed:  %ProgramData%\\AlpacaHub\\drivers\\<key>\\<version>\\driver.json (+ the package). Each version in its own
  folder, so an update never touches files a running driver process has open. A driver installer writes driver.json
  last, so a folder without it is half-written and ignored; an uninstaller deletes it first.
- source:     <repository>\\drivers\\<key>\\driver.json (no version folder), for development.
- built in:   the modules in hub/drivers that export DRIVERS, until steps 7-10 move them into folders.

driver.json: {"key": "wpanel", "title": "WandererCover V4-EC", "version": "1.2.0", "api": 1,
              "module": "wpanel.driver" (default "<key>.driver"), "python": "3.14" (only with compiled modules),
              "min_hub": "0.3.0" (optional: the oldest hub it works with)}
For each key the highest version wins; at the same version an installed one beats a source folder. A driver for
another hub API, another Python version or a newer hub (min_hub) is listed as refused and not run.
"""
import importlib
import json
import logging
import os
import pkgutil
import re
import shutil
import sys

from . import VERSION
from .api import API_VERSION

log = logging.getLogger("hub.catalog")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(_ROOT, "drivers")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def installed_root():
    return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "AlpacaHub", "drivers")


def parse_version(v):
    """'1.2.0' -> (1, 2, 0); anything else -> None."""
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,6}(\.\d{1,6}){0,3}", v):
        return None
    return tuple(int(x) for x in v.split("."))


class Entry:
    """One version of one driver."""

    def __init__(self, key, version, source, module, path=None, title="", folder=None, api=API_VERSION, python=None):
        self.key = key
        self.version = version
        self.vtuple = parse_version(version) or (0,)
        self.source = source            # "installed", "source", "built in", "module" (tests)
        self.module = module
        self.path = path                # added to the driver process's sys.path (None: importable as it is)
        self.title = title or key
        self.folder = folder            # the version folder (installed), for cleaning up old versions
        self.api = api
        self.python = python
        self.refused = ""

    def same(self, other):
        return other is not None and (self.key, self.version, self.source, self.path, self.module) == \
            (other.key, other.version, other.source, other.path, other.module)

    def info(self):
        return {"key": self.key, "version": self.version, "source": self.source, "api": self.api,
                "title": self.title}

    def __repr__(self):
        return f"<{self.key} {self.version} {self.source}>"


def _read(folder, key, source):
    """The Entry for a folder with driver.json, or None if it has none (half-written, or being uninstalled)."""
    p = os.path.join(folder, "driver.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        e2 = Entry(key, "0", source, "", folder, folder=folder)
        e2.refused = f"driver.json can't be read: {e}"
        return e2
    version = d.get("version")
    module = d.get("module") or f"{key}.driver"
    e = Entry(key, version if isinstance(version, str) else "0", source, module, folder,
              title=d.get("title") if isinstance(d.get("title"), str) else key, folder=folder,
              api=d.get("api"), python=d.get("python"))
    if d.get("key") != key:
        e.refused = f"driver.json says key {d.get('key')!r}, but its folder is {key!r}"
    elif parse_version(version) is None:
        e.refused = f"version {version!r} isn't like 1.2.0"
    elif not isinstance(module, str) or not MODULE_RE.match(module):
        e.refused = f"module {module!r} isn't a Python module name"
    elif d.get("api") != API_VERSION:
        e.refused = f"written for hub API {d.get('api')}, this hub has {API_VERSION}"
    elif "min_hub" in d and parse_version(d["min_hub"]) is None:
        e.refused = f"min_hub {d['min_hub']!r} isn't like 0.3.0"
    elif "min_hub" in d and parse_version(d["min_hub"]) > parse_version(VERSION):
        e.refused = f"needs Alpaca Hub {d['min_hub']} or newer, this is {VERSION}"
    elif e.python is not None and e.python != f"{sys.version_info.major}.{sys.version_info.minor}":
        e.refused = (f"built for Python {e.python}, the hub runs {sys.version_info.major}."
                     f"{sys.version_info.minor} (it has compiled modules)")
    elif not os.path.isdir(os.path.join(folder, module.split(".")[0])) \
            and not os.path.isfile(os.path.join(folder, module.split(".")[0] + ".py")):
        e.refused = f"its package {module.split('.')[0]!r} is missing"
    return e


class Catalog:
    def __init__(self, installed=None, source=None, builtin=True, modules=None, module_path=None, extra=()):
        """installed/source: folders (None: the defaults; "" to leave one out). builtin: the hub/drivers modules.
        extra: more source folders (hub --driver-folder: a driver being written, outside this repository).
        modules: extra modules by name, all with module_path on sys.path (tests)."""
        self.installed = installed_root() if installed is None else installed
        self.source = SOURCE_ROOT if source is None else source
        self.extra = [os.path.abspath(x) for x in extra]
        self.builtin = builtin
        self.modules = list(modules or [])
        self.module_path = module_path

    def scan(self):
        """{key: chosen Entry} (refused entries included, to be listed), and {key: [every Entry]}."""
        found = {}
        if self.installed and os.path.isdir(self.installed):
            for key in _listdir(self.installed):
                kdir = os.path.join(self.installed, key)
                if not KEY_RE.match(key) or not os.path.isdir(kdir):
                    continue
                for ver in _listdir(kdir):
                    vdir = os.path.join(kdir, ver)
                    if os.path.isdir(vdir):
                        e = _read(vdir, key, "installed")
                        if e is not None:
                            found.setdefault(key, []).append(e)
        for root in [self.source] + self.extra:
            if not root or not os.path.isdir(root):
                continue
            # a folder that is itself a driver (has driver.json), or a folder of driver folders
            dirs = [root] if os.path.isfile(os.path.join(root, "driver.json")) else                 [os.path.join(root, k) for k in _listdir(root)]
            for kdir in dirs:
                key = os.path.basename(kdir)
                if KEY_RE.match(key) and os.path.isdir(kdir):
                    e = _read(kdir, key, "source")
                    if e is not None:
                        found.setdefault(key, []).append(e)
        if self.builtin:
            from . import drivers as pkg
            for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
                if not m.ispkg and not m.name.startswith("_"):
                    found.setdefault(m.name, []).append(Entry(m.name, VERSION, "built in", f"{pkg.__name__}.{m.name}"))
        for mod in self.modules:
            key = mod.rsplit(".", 1)[-1]
            found.setdefault(key, []).append(Entry(key, VERSION, "module", mod, self.module_path))
        rank = {"installed": 3, "source": 2, "module": 1, "built in": 0}
        chosen = {}
        for key, es in found.items():
            ok = [e for e in es if not e.refused]
            pool = ok or es
            chosen[key] = max(pool, key=lambda e: (e.vtuple, rank.get(e.source, 0)))
        return chosen, found

    def clean_up(self, key, keep):
        """Deletes the installed version folders of key older than keep (the one running), once nothing uses them.
        Never a newer one (an update being installed) and never anything outside the installed folder."""
        if not self.installed or keep is None:
            return []
        kdir = os.path.join(self.installed, key)
        gone = []
        for ver in _listdir(kdir):
            vdir = os.path.join(kdir, ver)
            v = parse_version(ver)
            if v is None or not os.path.isdir(vdir) or os.path.normcase(vdir) == os.path.normcase(keep.folder or ""):
                continue
            if v >= keep.vtuple or not os.path.isfile(os.path.join(vdir, "driver.json")):
                continue
            if os.path.commonpath([os.path.abspath(vdir), os.path.abspath(self.installed)]) \
                    != os.path.abspath(self.installed):
                continue
            try:
                shutil.rmtree(vdir)
                gone.append(ver)
                log.info("%s: old version %s deleted (%s)", key, ver, vdir)
            except OSError as e:
                log.warning("%s: could not delete old version %s yet: %s", key, ver, e)
        return gone


def load_in_process(catalog):
    """Imports the chosen drivers into this process and returns one instance of each Driver class, for
    --in-process and the in-process tests only (normally a driver runs in its own process). Refused ones are left
    out, and one that fails to import is logged and left out."""
    chosen, _ = catalog.scan()
    out = []
    for key, e in sorted(chosen.items()):
        if e.refused:
            log.error("driver %s not loaded: %s", key, e.refused)
            continue
        if e.path and e.path not in sys.path:
            sys.path.insert(0, e.path)
        try:
            mod = importlib.import_module(e.module)
        except Exception:
            log.exception("driver %s (%s) failed to load; its devices won't be found", key, e.module)
            continue
        out.extend(cls() for cls in getattr(mod, "DRIVERS", ()))
    return out


def _listdir(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []
