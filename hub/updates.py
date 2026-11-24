"""The hub's update check (UPDATES-PLAN.md, step 3): reads the published version list now and then, and says on the
Overview when a newer hub or a newer version of an installed driver exists. It installs nothing.

The list (releases.json, written by packaging/release.py) is read from FEED_URL: once shortly after the hub starts,
if the last check is a day old, then once a day, and when "Check now" is pressed. The request carries nothing but the
hub's version in its user agent: no machine, user, device or serial details. Turned off on the Overview, it sends
nothing at all. A simulated hub doesn't check unless it's given a feed address (the tests, a trial run).

The last result is kept in <config dir>/updates.json, so a restart doesn't check again and the Overview still shows
what was found.

Installing (step 4), on the hub's PC only and only when someone asks on the Overview: the installer is downloaded
into <config dir>/updates/, its size and SHA-256 are checked against the list, and it is run with Windows'
administrator prompt. Its address must be https on the list's own host (redirects from there are followed). A driver's
installer runs as it is and the hub switches to the new version by itself; the hub's installer stops the hub and
starts it again afterwards (/RESTARTHUB). Old downloads are deleted the next time the hub starts or downloads.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from . import VERSION
from .api import API_VERSION

log = logging.getLogger("hub.updates")

FEED_URL = "https://github.com/ScaleDIN/alpaca-hub/releases/latest/download/releases.json"   # the public repository
FEED = 1                      # the list's format this hub reads
CHECK_EVERY_S = 24 * 3600
FIRST_CHECK_S = 60            # after start, so the check never competes with finding the devices
TIMEOUT_S = 10
MAX_BYTES = 256 * 1024
MAX_INSTALLER = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 30
FILE_RE = re.compile(r"^AlpacaHub-[A-Za-z0-9_.-]{1,60}-Setup\.exe$")
BUSY = ("downloading", "checking", "starting")
# Inno Setup: progress only, no wizard pages; /RESTARTHUB is the hub installer's own (packaging/hub.iss)
ARGS = {"hub": "/SILENT /SP- /NORESTART /RESTARTHUB", "driver": "/SILENT /SP- /NORESTART"}


def vt(v):
    """'1.2.0' -> (1, 2, 0); anything else -> None."""
    try:
        t = tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return None
    return t if 1 <= len(t) <= 4 and all(0 <= x < 10 ** 6 for x in t) else None


def allowed_url(url):
    """Only https, or plain http to this PC (the tests' own server)."""
    return url.startswith("https://") or url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")


def fetch(url, timeout=TIMEOUT_S):
    """The list at url, as a dict. Raises OSError or ValueError."""
    if not allowed_url(url):
        raise ValueError(f"not an https address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": f"AlpacaHub/{VERSION}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("the list is too big")
    feed = json.loads(data.decode("utf-8"))
    if not isinstance(feed, dict) or feed.get("feed") != FEED:
        raise ValueError(f"not a version list this hub reads (feed {feed.get('feed') if isinstance(feed, dict) else '?'})")
    return feed


def _entry(e):
    """The parts of a list entry the Overview shows, checked; None if it isn't usable."""
    if not isinstance(e, dict) or vt(e.get("version")) is None:
        return None
    notes = e.get("notes") if isinstance(e.get("notes"), str) else ""
    return {"version": e["version"], "date": str(e.get("date") or ""), "notes": notes[:4000],
            "url": e.get("url") if isinstance(e.get("url"), str) else "",
            "min_hub": e.get("min_hub") if vt(e.get("min_hub")) else None, "api": e.get("api"),
            "title": e.get("title") if isinstance(e.get("title"), str) else "",
            "file": e.get("file") if isinstance(e.get("file"), str) and FILE_RE.match(e["file"]) else "",
            "sha256": e.get("sha256") if isinstance(e.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
            else "", "size": e.get("size") if isinstance(e.get("size"), int) and 0 < e["size"] <= MAX_INSTALLER else 0}


def compare(feed, hub_version, drivers, api=API_VERSION):
    """What is newer in feed than what runs here: [{"kind": "hub"|"driver", "key", "title", "installed", "version",
    "date", "notes", "url", "blocked"}]. drivers: {key: (installed version, title)}. Only drivers installed here are
    offered; "blocked" says why one can't be installed yet (it needs a newer hub, or another hub API)."""
    out = []
    hub_v = vt(hub_version) or (0,)
    h = _entry(feed.get("hub"))
    newest_hub = h["version"] if h else hub_version
    if h and vt(h["version"]) > hub_v:
        out.append({"kind": "hub", "key": "hub", "title": "Alpaca Hub", "installed": hub_version, "blocked": "",
                    **{k: h[k] for k in ("version", "date", "notes", "url", "file", "sha256", "size")}})
    listed = feed.get("drivers") if isinstance(feed.get("drivers"), dict) else {}
    for key in sorted(drivers):
        mine, title = drivers[key]
        e = _entry(listed.get(key))
        if not e or vt(mine) is None or vt(e["version"]) <= vt(mine):
            continue
        blocked = ""
        if e["api"] is not None and e["api"] != api:
            blocked = f"written for hub API {e['api']}; this hub has API {api}"
        elif e["min_hub"] and vt(e["min_hub"]) > hub_v:
            blocked = (f"needs Alpaca Hub {e['min_hub']} or newer: update the hub first"
                       if vt(newest_hub) >= vt(e["min_hub"]) else f"needs Alpaca Hub {e['min_hub']} or newer")
        out.append({"kind": "driver", "key": key, "title": e["title"] or title or key, "installed": mine,
                    "blocked": blocked,
                    **{k: e[k] for k in ("version", "date", "notes", "url", "file", "sha256", "size")}})
    return out


class Updates:
    """The check's state and its thread (run). enabled() and installed() are asked at each check: the Overview's
    setting, and {key: (version, title)} of the drivers here."""

    def __init__(self, config_dir, enabled, installed, url=None, fetcher=fetch, clock=time.time, runner=None):
        # ALPACA_HUB_UPDATE_FEED overrides the built-in address ("" = none): the test runner sets it, so tests never go
        # online
        self.url = os.environ.get("ALPACA_HUB_UPDATE_FEED", FEED_URL) if url is None else url
        self.enabled = enabled
        self.installed = installed
        self.fetcher = fetcher
        self.clock = clock
        self.path = os.path.join(config_dir, "updates.json")
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.checking = False
        self.last_check = 0.0         # wall-clock time of the last attempt
        self.last_ok = 0.0            # ... of the last one that read the list
        self.error = ""
        self.feed = None
        self.dir = os.path.join(config_dir, "updates")      # downloaded installers
        self.runner = runner or run_installer
        self.installs = {}            # key -> {"version", "state", "done", "total", "error"}: the Overview's progress
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.last_check = float(d.get("last_check") or 0)
            self.last_ok = float(d.get("last_ok") or 0)
            self.error = str(d.get("error") or "")
            self.feed = d.get("feed") if isinstance(d.get("feed"), dict) else None
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_check": self.last_check, "last_ok": self.last_ok, "error": self.error,
                           "feed": self.feed}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.debug("cannot save %s: %s", self.path, e)

    def check(self):
        """One check now (the thread, or Check now). Returns the error text, "" when the list was read."""
        if not self.url:
            return "no update address in this hub yet"
        with self._lock:
            self.checking = True
        try:
            feed = self.fetcher(self.url)
            err = ""
        except Exception as e:                      # offline, timeout, a bad list: noted, tried again tomorrow
            feed, err = None, f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e)
        with self._lock:
            self.checking = False
            self.last_check = self.clock()
            self.error = err
            if feed is not None:
                self.feed, self.last_ok = feed, self.last_check
            self._save()
        if err:
            log.info("update check: %s", err)
        else:
            found = self.available()
            log.info("update check: %s", ", ".join(f"{a['title']} {a['version']}" for a in found) or "up to date")
        return err

    def check_now(self):
        """The Overview's Check now: the thread checks at once (so the request returns straight away)."""
        self._now = True
        self._wake.set()

    _now = False

    def run(self, stop, first_s=FIRST_CHECK_S):
        """The thread: the first check first_s after start if the last is a day old, then once a day."""
        self.clean()                              # installers downloaded before the last start
        due = max(self.clock() + first_s, self.last_check + CHECK_EVERY_S)
        while not stop.is_set():
            now = self.clock()
            if self._now or (now >= due and self.enabled() and self.url):
                self._now = False
                self.check()
                due = self.clock() + CHECK_EVERY_S
            elif now >= due:
                due = now + CHECK_EVERY_S         # off, or no address: look again tomorrow (or on Check now)
            self._wake.wait(min(60.0, max(0.5, due - self.clock())))
            self._wake.clear()

    def available(self):
        if not self.feed:
            return []
        try:
            return compare(self.feed, VERSION, self.installed())
        except Exception as e:                      # a list that parses but isn't shaped right: nothing offered
            log.debug("update list not usable: %s", e)
            return []

    def status(self):
        """For hub.json: the Overview's Updates line and setting."""
        with self._lock:
            installs = {k: dict(v) for k, v in self.installs.items()}
        return {"enabled": bool(self.enabled()), "address": bool(self.url), "checking": self.checking,
                "last_check": self.last_check, "last_ok": self.last_ok, "error": self.error,
                "available": self.available() if self.enabled() else [], "installs": installs}

    # ---------------------------------------------------------------- installing (step 4)
    def clean(self):
        """Deletes the downloads folder's files (an installer still running can't be deleted: it goes next time)."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for n in names:
            try:
                os.remove(os.path.join(self.dir, n))
            except OSError:
                pass

    def _same_host(self, url):
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.url)
        return allowed_url(url) and (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)

    def install(self, key):
        """The Overview's Install: starts downloading and running key's installer ("hub" or a driver key). Returns
        an error text, or "" when it has started."""
        if not self.enabled():
            return "the update check is off"
        item = next((a for a in self.available() if a["key"] == key), None)
        if item is None:
            return f"no update for {key}"
        if item["blocked"]:
            return item["blocked"]
        if not (item["file"] and item["sha256"] and item["size"]):
            return f"the list has no file, size or SHA-256 for {item['title']} {item['version']}"
        if not self._same_host(item["url"]):
            return f"its address isn't on the list's own https host: {item['url']}"
        with self._lock:
            if any(v["state"] in BUSY for v in self.installs.values()):
                return "an installer is being downloaded or started: wait for it"
            self.installs[key] = {"version": item["version"], "state": "downloading", "done": 0,
                                  "total": item["size"], "error": ""}
        threading.Thread(target=self._install, args=(key, item), name=f"update-{key}", daemon=True).start()
        return ""

    def _set(self, key, **kw):
        with self._lock:
            self.installs[key].update(kw)

    def _install(self, key, item):
        path = os.path.join(self.dir, item["file"])
        part = path + ".part"
        try:
            os.makedirs(self.dir, exist_ok=True)
            self.clean()
            req = urllib.request.Request(item["url"], headers={"User-Agent": f"AlpacaHub/{VERSION}"})
            h = hashlib.sha256()
            done = 0
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r, open(part, "wb") as f:
                if not allowed_url(r.geturl()):
                    raise ValueError(f"redirected to an address that isn't https: {r.geturl()}")
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > item["size"]:
                        raise ValueError(f"bigger than the list says ({item['size']} bytes)")
                    h.update(chunk)
                    f.write(chunk)
                    self._set(key, done=done)
            self._set(key, state="checking")
            if done != item["size"]:
                raise ValueError(f"{done} bytes, the list says {item['size']}")
            if h.hexdigest() != item["sha256"]:
                raise ValueError("its SHA-256 isn't the one on the list, so it wasn't run")
            os.replace(part, path)
            self._set(key, state="starting")
            log.info("update: running %s (%s %s)", item["file"], item["title"], item["version"])
            self.runner(path, ARGS[item["kind"]])
            self._set(key, state="started")
        except Exception as e:
            if isinstance(e, OSError) and getattr(e, "winerror", None) == 1223:
                msg = "Windows' administrator prompt was cancelled"
            else:
                msg = str(e) or type(e).__name__
            log.warning("update of %s failed: %s", key, msg)
            for p in (part, path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self._set(key, state="failed", error=msg)


def run_installer(path, args):
    """Runs an installer through the Windows shell, so Windows shows its administrator prompt (a plain process start
    fails: the installers ask for administrator rights). Returns at once."""
    os.startfile(path, "open", args)
