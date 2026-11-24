"""Day files of every device's history, written by the hub.

Each device's driver keeps its chart's history in memory (10-second averages for 24 hours, each with its lowest and
highest 1 s sample: /history.json?res=m, hub/devserver.py History). Once a minute the hub asks every running device
for the steps it hasn't written yet, makes 1-minute rows of the finished minutes and appends them to one CSV file per
day, in the same place a driver that writes its own files uses: <config dir>\\drivers\\<key>\\<unit>-history\\
<key>-history-YYYY-MM-DD.csv (the unit: the device's serial number, else the USB chip's, else the COM port, so one
device keeps one folder whichever port it is on). The Overview lists those folders (Hub.saved_history), with or
without the device.

Columns: time_local (the minute, local time), unix_s (its start), then for each of the device's history keys its
average over the minute, <key>_min and <key>_max (the lowest and highest 1 s sample; a driver from before min/max:
the lowest and highest of its averages). Missing readings stay empty, never zero. Files older than the hub's history_days setting are deleted (the hub's
own files only: a driver's files follow that driver's setting).

Drivers that write their own files (the power box) are left out: OWN_FILES, or a history.json reply with
"files": true. Their days setting is kept the same as the hub's, so one setting covers every device: a change on the
Overview is passed to the box (its own history setting), a change on the box's tab becomes the hub's. The first
time the hub sees a box, both take the larger of the two, so that first match never deletes files.
"""
import csv
import http.client
import json
import logging
import os
import re
import threading
import time

from .api import _safe, unit_of

log = logging.getLogger("hub.recorder")

OWN_FILES = {"wbox"}                    # the box writes wbstock-history-*.csv itself (drivers/wbox/wbox/history.py)
# Their days setting, on the driver's private server: status.json's history.days, and the page's own request to
# change it (drivers/wbox/wbox/devices.py _local_control: from this PC, with its page header).
OWN_DAYS = {"wbox": ("/setup/history", "X-WBStock")}
DAYS_MIN, DAYS_MAX, DAYS_DEFAULT = 1, 365, 7
EVERY_S = 60
SUFFIX = "-history"


def clamp_days(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DAYS_DEFAULT
    return max(DAYS_MIN, min(DAYS_MAX, n))


def file_re(key):
    """The hub's day files of one driver: <key>-history-YYYY-MM-DD.csv, or -N.csv when the columns changed that day."""
    return re.compile(r"^" + re.escape(key) + r"-history-(\d{4}-\d{2}-\d{2})(?:-\d+)?\.csv$")


def history_unit(inst):
    """The unit part of a device's history folder name (<unit>-history)."""
    return _safe(unit_of(inst.ident, inst.port))


def folder_for(config_dir, inst):
    return os.path.join(config_dir, "drivers", inst.driver.key, history_unit(inst) + SUFFIX)


class Recorder:
    def __init__(self, config_dir, instances, days=lambda: DAYS_DEFAULT, clock=time.time, set_days=None):
        """instances() -> the running devices; days() -> how many days of files to keep; set_days(n): a box's tab
        changed it (the hub saves it)."""
        self.config_dir = config_dir
        self.instances = instances
        self.days = days
        self.set_days = set_days or (lambda n: None)
        self._agreed = {}                        # folder -> the days a box and the hub last agreed on
        self.clock = clock
        self._lock = threading.Lock()            # one record pass at a time (the minute's, a device leaving)
        self._last = {}                          # folder -> start (unix_s) of the newest minute written
        self._failed = set()                     # folders whose last write failed (logged once)
        self._purged_for = None

    # ---------------------------------------------------------------- recording
    def record_all(self):
        for inst in list(self.instances()):
            self.record(inst)
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self._purged_for != today:
            self._purged_for = today
            self.purge()

    def record(self, inst, final=False):
        """Appends the device's finished minutes not written yet (final: the minute still open too, as the device
        leaves). Returns how many were written."""
        if not inst.local_port:
            return 0
        if inst.driver.key in OWN_FILES:
            self.sync_days(inst)
            return 0
        with self._lock:
            folder = folder_for(self.config_dir, inst)
            if folder not in self._last:
                self._last[folder] = self._newest(inst.driver.key, folder)
            since = self._last[folder] + 59 if self._last[folder] else 0   # rows of later minutes only
            try:
                c = http.client.HTTPConnection("127.0.0.1", inst.local_port, timeout=5)
                try:
                    c.request("GET", "/history.json?res=m" + (f"&since={_num(since)}" if since else ""))
                    r = c.getresponse()
                    body = r.read()
                finally:
                    c.close()
                if r.status != 200:
                    return 0
                h = json.loads(body.decode("utf-8"))
                keys, rows = [str(k) for k in h["keys"]], h["rows"]
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
                log.debug("%s: no history: %s", inst.slug, e)
                return 0
            if h.get("files"):                   # the driver writes its own
                return 0
            rows = sorted((r for r in rows if isinstance(r, list) and r and isinstance(r[0], (int, float))
                           and r[0] > since), key=lambda r: r[0])
            base, minutes = minute_rows(keys, rows)
            if minutes and not final:
                minutes = [m for m in minutes if m[0] < rows[-1][0] - rows[-1][0] % 60]   # the open minute waits
            if not minutes:
                return 0
            if self._write(inst.driver.key, folder, base, minutes):
                self._last[folder] = minutes[-1][0]
                return len(minutes)
            return 0

    def _write(self, key, folder, keys, rows):
        """rows: [minute start, (average, lowest, highest) of each key …]."""
        head = ["time_local", "unix_s"] + [x for k in keys for x in (k, k + "_min", k + "_max")]
        by_day = {}
        for r in rows:
            by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(r[0])), []).append(r)
        try:
            os.makedirs(folder, exist_ok=True)
            for day, rs in by_day.items():
                path = self._day_file(key, folder, day, head)
                new = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(head)
                    for r in rs:
                        w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(r[0])), _num(r[0])]
                                   + ["" if v is None else _num(v) for v in r[1:]])
            if folder in self._failed:
                log.info("history: writing to %s works again", folder)
                self._failed.discard(folder)
            return True
        except OSError as e:
            if folder not in self._failed:
                log.warning("history: cannot write to %s: %s", folder, e)
                self._failed.add(folder)
            return False

    @staticmethod
    def _day_file(key, folder, day, head):
        """That day's file whose columns are head: a new one (-2, -3 …) when a driver update changed them."""
        n = 1
        while True:
            path = os.path.join(folder, f"{key}-history-{day}{'' if n == 1 else f'-{n}'}.csv")
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    if next(csv.reader(f), None) in (head, None):
                        return path
            except FileNotFoundError:
                return path
            except OSError:
                pass
            n += 1

    @staticmethod
    def _newest(key, folder):
        """unix_s of the last record in the newest of the hub's files there (0: none): a restarted hub or driver
        doesn't write the same minutes twice."""
        rx = file_re(key)
        try:
            names = sorted((n for n in os.listdir(folder) if rx.match(n)),
                           key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        except OSError:
            return 0
        for n in reversed(names):
            try:
                with open(os.path.join(folder, n), newline="", encoding="utf-8") as f:
                    r = csv.reader(f)
                    head = next(r, None) or []
                    i = head.index("unix_s") if "unix_s" in head else None
                    last = 0
                    for row in r:
                        try:
                            last = max(last, float(row[i]))
                        except (TypeError, ValueError, IndexError):
                            pass
                if last:
                    return last
            except (OSError, ValueError):
                pass
        return 0

    # ---------------------------------------------------------------- one days setting
    def sync_days(self, inst, hub_changed=False):
        """Keeps a box's own days setting and the hub's the same (hub_changed: just set on the Overview, so the
        hub's value wins). Returns the days agreed on, or None."""
        how = OWN_DAYS.get(inst.driver.key)
        if not how or not inst.local_port:
            return None
        with self._lock:
            try:
                st = json.loads(self._http(inst, "GET", "/status.json")[1].decode("utf-8"))
                box = clamp_days(st["history"]["days"])
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
                log.debug("%s: no history days: %s", inst.slug, e)
                return None
            folder = folder_for(self.config_dir, inst)
            hub, agreed = clamp_days(self.days()), self._agreed.get(folder)
            if hub_changed:
                want = hub
            elif agreed is None:
                want = max(box, hub)             # first sight: never delete files to match
            elif box != agreed:
                want = box                       # changed on the box's tab
            else:
                want = hub                       # changed on the Overview (or nothing changed)
            if box != want:
                try:
                    code, body = self._http(inst, "POST", how[0], f"days={want}", {how[1]: "1"})
                except (OSError, http.client.HTTPException) as e:
                    log.warning("%s: cannot set its history days: %s", inst.slug, e)
                    return None
                if code != 200:
                    log.warning("%s: cannot set its history days: %s", inst.slug, body.decode("utf-8", "replace"))
                    return None
                log.info("%s: history kept for %d days, as the hub's", inst.slug, want)
            if hub != want:
                log.info("history: kept for %d days, as %s's tab says", want, inst.slug)
                self.set_days(want)
            self._agreed[folder] = want
            return want

    @staticmethod
    def _http(inst, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", inst.local_port, timeout=5)
        try:
            h = dict(headers or {})
            if body is not None:
                h["Content-Type"] = "application/x-www-form-urlencoded"
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def sync_all(self):
        """After the hub's setting changed: every box now (not at the next minute)."""
        for inst in list(self.instances()):
            if inst.driver.key in OWN_DAYS:
                self.sync_days(inst, hub_changed=True)

    # ---------------------------------------------------------------- retention
    def purge(self):
        """Deletes the hub's day files older than days(), in every driver's folders. Returns how many."""
        keep = clamp_days(self.days())
        cut = time.strftime("%Y-%m-%d", time.localtime(self.clock() - (keep - 1) * 86400))
        root = os.path.join(self.config_dir, "drivers")
        n = 0
        try:
            keys = os.listdir(root)
        except OSError:
            return 0
        for key in keys:
            if key in OWN_FILES:
                continue
            rx = file_re(key)
            try:
                folders = [os.path.join(root, key, d) for d in os.listdir(os.path.join(root, key)) if d.endswith(SUFFIX)]
            except OSError:
                continue
            for folder in folders:
                try:
                    names = os.listdir(folder)
                except OSError:
                    continue
                for name in names:
                    m = rx.match(name)
                    if m and m.group(1) < cut:
                        try:
                            os.remove(os.path.join(folder, name))
                            n += 1
                        except OSError as e:
                            log.warning("history: cannot delete %s: %s", name, e)
        if n:
            log.info("history: deleted %d day file(s) older than %d days", n, keep)
        return n

    def run(self, stop):
        """The hub's recording thread: every EVERY_S until stop (an Event) is set."""
        while not stop.wait(EVERY_S):
            try:
                self.record_all()
            except Exception:
                log.exception("history recording")


def minute_rows(keys, rows):
    """history.json's keys and rows (any step: 10 s, or 1 minute from an older driver) -> (the base keys, one row per
    minute: [start, avg, min, max of each base key]). The average is over the steps that have the value; the lowest
    and highest come from <key>_min / <key>_max when the driver gives them, else from the steps' averages."""
    col = {k: i + 1 for i, k in enumerate(keys)}
    base = [k for k in keys if not ((k.endswith("_min") or k.endswith("_max")) and k[:-4] in col)]
    groups = {}
    for r in rows:
        groups.setdefault(int(r[0]) - int(r[0]) % 60, []).append(r)

    def at(r, k):
        i = col.get(k)
        v = r[i] if i is not None and i < len(r) else None
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    out = []
    for m in sorted(groups):
        rec = [m]
        for k in base:
            avgs = [v for v in (at(r, k) for r in groups[m]) if v is not None]
            los = [v for v in ((at(r, k + "_min") if (k + "_min") in col else at(r, k)) for r in groups[m]) if v is not None]
            his = [v for v in ((at(r, k + "_max") if (k + "_max") in col else at(r, k)) for r in groups[m]) if v is not None]
            rec += [round(sum(avgs) / len(avgs), 2) if avgs else None, min(los) if los else None,
                    max(his) if his else None]
        out.append(rec)
    return base, out


def _num(v):
    """A number as written: no ".0" on whole numbers."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return int(f) if f.is_integer() else f
