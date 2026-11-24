"""Builds a device's status page from its ui folder (body.html, device.js, device.css) and the hub's one copy of
the device-web-ui kit (hub/ui/kit). A device's ui folder has no kit of its own.

The same assembly as the kit's embed_ui.py, done at run time: the page is built once, kept gzipped in memory,
and rebuilt when a file under the folder changes (handy while editing; an installed copy never changes).

Taken from ncalpaca/webpage.py and wbstock/webpage.py (2026-11-22), which were the same apart from their folder and
the title used when body.html has no <h1>.
"""
import gzip
import os
import re
import sys
import threading

KIT_JS = ("core.js", "chart.js")


def kit_dir():
    """The hub's kit: hub/ui/kit, or under _internal in a PyInstaller one-folder build."""
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.isdir(os.path.join(base, "hub", "ui", "kit")):
        return os.path.join(base, "hub", "ui", "kit")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "kit")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _strip_css(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return "\n".join(line.strip() for line in s.splitlines() if line.strip())


def _strip_js(s):
    """Drops whole-line comments and indentation only; strings are left alone."""
    out, block = [], False
    for line in s.splitlines():
        t = line.strip()
        if block:
            block = "*/" not in t
            continue
        if t.startswith("/*") and "*/" not in t:
            block = True
            continue
        if not t or t.startswith("//") or (t.startswith("/*") and t.endswith("*/")):
            continue
        out.append(t)
    return "\n".join(out)


def build(ui, title="Device"):
    """The page's HTML. title is used only when body.html has no <h1>."""
    kit = kit_dir()
    body = re.sub(r"<!--.*?-->", "", _read(os.path.join(ui, "body.html")), flags=re.S)
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    css = _strip_css(_read(os.path.join(kit, "base.css")))
    if os.path.isfile(os.path.join(ui, "device.css")):
        css += "\n" + _strip_css(_read(os.path.join(ui, "device.css")))
    page = _read(os.path.join(kit, "template.html"))
    for k, v in (("__TITLE__", title), ("__CSS__", css), ("__BODY__", body.strip()),
                 ("__KIT_JS__", "\n".join(_strip_js(_read(os.path.join(kit, f))) for f in KIT_JS)),
                 ("__DEVICE_JS__", _strip_js(_read(os.path.join(ui, "device.js"))))):
        page = page.replace(k, v)
    return page


class Page:
    def __init__(self, ui, title="Device"):
        self.ui = ui
        self.title = title
        self._lock = threading.Lock()
        self._stamp = None
        self.html = b""
        self.gz = b""

    def _mtime(self):
        newest = 0.0
        for base, _, files in (*os.walk(self.ui), *os.walk(kit_dir())):
            for fn in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(base, fn)))
                except OSError:
                    pass
        return newest

    def get(self):
        """(html bytes, gzipped bytes), rebuilt if a file under the ui folder or the kit changed."""
        with self._lock:
            stamp = self._mtime()
            if stamp != self._stamp:
                self.html = build(self.ui, self.title).encode("utf-8")
                self.gz = gzip.compress(self.html, 9, mtime=0)
                self._stamp = stamp
            return self.html, self.gz
