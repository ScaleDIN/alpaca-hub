"""Reading history for the status page chart.

Two levels, like the ESP32 bridge:
* 1 s samples for the last 10 minutes, in memory only (zooming in on what just happened).
* 10-second averages, kept in memory for the chart for 24 hours (as the hub's other drivers, hub/devserver.py).
* 1-minute averages appended to one CSV file per day (history/wbstock-history-YYYY-MM-DD.csv next to the
  settings file; -2, -3 … when the columns changed that day).
Both carry each value's lowest and highest 1 s sample in the step or minute (<key>_min / <key>_max, in the files
<header>_min / <header>_max), so a spike the average hides still shows. Files older than the retention setting are deleted. The chart starts empty, like every device
  tab in the hub: the hub's Overview reads the files back (load() puts them in the chart, for tools and tests).

A minute is written when the first sample of the next minute arrives (or when the service stops), so the
current minute never shows up as a half-average. Missing readings stay empty: never zero.
"""
import csv
import io
import logging
import os
import re
import threading
import time
import zipfile
from collections import deque

log = logging.getLogger("wbox.history")

# key, CSV header, decimals. The chart uses the keys, the CSV files the headers.
COLS = (
    ("vin", "input_V", 2),
    ("iin", "input_A", 2),
    ("pwr", "input_W", 1),
    ("lens", "lens_C", 2),
    ("amb", "ambient_C", 1),
    ("hum", "humidity_pct", 1),
    ("dp", "dew_point_C", 2),
    ("dew", "heater_pct", 1),
    ("dc2", "dc2_set_V", 1),
    ("tgt", "dew_target_C", 2),
)
KEYS = tuple(k for k, _, _ in COLS)
FINE_S = 600                   # 1 s samples kept in memory
STEP_S = 10                    # the chart's averages (history.json?res=min), dated in the middle of each
DAYS_MIN, DAYS_MAX, DAYS_DEFAULT = 1, 365, 7
CHART_S = 86400               # held in memory for the chart (as the other drivers); the files go back further
PREFIX = "wbstock-history-"
_NAME = re.compile(r"^wbstock-history-(\d{4})-(\d{2})-(\d{2})(?:-\d+)?\.csv$")
HEAD = ["time_local", "unix_s"] + [x for _, h, _ in COLS for x in (h, h + "_min", h + "_max")]
CHART_KEYS = tuple(KEYS) + tuple(k + "_min" for k in KEYS) + tuple(k + "_max" for k in KEYS)


class _Acc:
    """Sum, count, lowest and highest of each value over a step or a minute."""

    def __init__(self):
        n = len(KEYS)
        self.sum, self.n, self.lo, self.hi = [0.0] * n, [0] * n, [None] * n, [None] * n

    def add(self, vals):
        for i, v in enumerate(vals):
            if v is not None:
                self.sum[i] += v
                self.n[i] += 1
                self.lo[i] = v if self.lo[i] is None else min(self.lo[i], v)
                self.hi[i] = v if self.hi[i] is None else max(self.hi[i], v)

    def empty(self):
        return not any(self.n)

    def result(self):
        """[averages], [lowest], [highest], rounded as the column says."""
        r = lambda v, d: None if v is None else round(v, d)   # noqa: E731
        return ([round(s / n, d) if n else None for s, n, (_, _, d) in zip(self.sum, self.n, COLS)],
                [r(v, d) for v, (_, _, d) in zip(self.lo, COLS)], [r(v, d) for v, (_, _, d) in zip(self.hi, COLS)])


def clamp_days(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DAYS_DEFAULT
    return max(DAYS_MIN, min(DAYS_MAX, n))


def _local_date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


class History:
    def __init__(self, folder, days=DAYS_DEFAULT, clock=time.time):
        self.folder = folder
        self.days = clamp_days(days)
        self._clock = clock
        self._lock = threading.Lock()
        self._fine = deque(maxlen=FINE_S)    # [t, v...] one per second
        self._coarse = []                    # [t, averages…, lowest…, highest…] one per 10 s, t = middle (chart)
        self._step = None                    # start of the step being averaged
        self._sacc = _Acc()
        self._minute = None                  # start of the minute being averaged (the files)
        self._macc = _Acc()
        self._heads = {}                     # day file -> its header (checked once)
        self._write_failed = False
        self._purged_for = None              # local date the last purge ran for

    def _cut(self):
        """Oldest minute the chart keeps in memory."""
        return self._clock() - min(self.days * 86400, CHART_S)

    # ---------------------------------------------------------------- start-up
    def load(self):
        """Reads the CSV files that are within the retention window. Returns the number of minutes read."""
        self.purge()
        rows = []
        first_day = _local_date(self._cut())
        for name in self._file_names():
            if "-".join(_NAME.match(name).groups()) < first_day:
                continue
            try:
                with open(os.path.join(self.folder, name), newline="", encoding="utf-8") as f:
                    r = csv.reader(f)
                    head = next(r, None)
                    if not head or "unix_s" not in head:
                        continue
                    ti = head.index("unix_s")
                    want = [h for _, h, _ in COLS] + [h + "_min" for _, h, _ in COLS] + [h + "_max" for _, h, _ in COLS]
                    idx = [head.index(h) if h in head else None for h in want]   # as CHART_KEYS; older files: no min/max
                    for row in r:
                        try:
                            t = float(row[ti])
                        except (ValueError, IndexError):
                            continue
                        rec = [t]
                        for i in idx:
                            v = row[i] if i is not None and i < len(row) else ""
                            try:
                                rec.append(float(v) if v != "" else None)
                            except ValueError:
                                rec.append(None)
                        rows.append(rec)
            except OSError as e:
                log.warning("cannot read %s: %s", name, e)
        rows.sort(key=lambda x: x[0])
        cut = self._cut()
        with self._lock:
            self._coarse = [x for x in rows if x[0] >= cut]
            n = len(self._coarse)
        if n:
            log.info("history: read %d minutes from %s", n, self.folder)
        return n

    # ---------------------------------------------------------------- samples
    def add(self, values):
        """values: {key: number or None} for this moment. Several in the same second keep only the last."""
        now = self._clock()
        t = int(now)
        rec = [t] + [None if values.get(k) is None else float(values[k]) for k in KEYS]
        flush = None
        with self._lock:
            if self._fine and self._fine[-1][0] == t:
                self._fine[-1] = rec
            else:
                self._fine.append(rec)
            st = t - t % STEP_S
            if self._step is not None and st != self._step:
                self._close_step()
            self._step = st
            m = t - t % 60
            if self._minute is not None and m != self._minute:
                flush = self._close_minute()
            self._minute = m
            self._macc.add(rec[1:])
            self._sacc.add(rec[1:])
        if flush:
            self._write(flush)

    def _close_step(self):
        """(lock held) The average of the 10 s being collected, appended to the chart's list."""
        if self._step is not None and not self._sacc.empty():
            avg, lo, hi = self._sacc.result()
            self._coarse.append([self._step + STEP_S / 2] + avg + lo + hi)
            cut = self._cut()
            if self._coarse[0][0] < cut:
                self._coarse = [x for x in self._coarse if x[0] >= cut]
        self._sacc = _Acc()
        self._step = None

    def _close_minute(self):
        """(lock held) The average of the minute being collected, for its day file."""
        if self._minute is None or self._macc.empty():
            self._minute = None
            return None
        avg, lo, hi = self._macc.result()
        rec = [self._minute + 30] + [x for i in range(len(KEYS)) for x in (avg[i], lo[i], hi[i])]
        self._macc = _Acc()
        self._minute = None
        return rec

    def flush(self):
        """Writes the minute being collected (when the service stops)."""
        with self._lock:
            rec = self._close_minute()
        if rec:
            self._write(rec)

    def _write(self, rec):
        t = rec[0]
        day = _local_date(t)
        path = os.path.join(self.folder, PREFIX + day + ".csv")
        try:
            os.makedirs(self.folder, exist_ok=True)
            path = self._day_file(day)
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(HEAD)
                    self._heads[path] = HEAD
                w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(t - 30)), int(t)] +
                           ["" if v is None else v for v in rec[1:]])
            if self._write_failed:
                log.info("history: writing to %s works again", self.folder)
            self._write_failed = False
        except OSError as e:
            if not self._write_failed:
                log.warning("history: cannot write %s: %s (the chart keeps working from memory)", path, e)
            self._write_failed = True
        if self._purged_for != day:
            self.purge()

    def _day_file(self, day):
        """That day's file with today's columns: a new one (-2, -3 …) when a driver update changed them."""
        n = 1
        while True:
            path = os.path.join(self.folder, PREFIX + day + ("" if n == 1 else f"-{n}") + ".csv")
            if path not in self._heads:
                try:
                    with open(path, newline="", encoding="utf-8") as f:
                        self._heads[path] = next(csv.reader(f), None)
                except FileNotFoundError:
                    return path
                except OSError:
                    self._heads[path] = None
            if self._heads[path] in (HEAD, None):
                return path
            n += 1

    # ---------------------------------------------------------------- reading
    def fine(self, since=None):
        with self._lock:
            rows = [r for r in self._fine if since is None or r[0] > since]
        return {"keys": KEYS, "rows": rows}

    def coarse(self, since=None):
        with self._lock:
            rows = [r for r in self._coarse if since is None or r[0] > since]
        return {"keys": CHART_KEYS, "rows": rows, "step": STEP_S}

    # ---------------------------------------------------------------- files
    def _file_names(self):
        try:
            return sorted(n for n in os.listdir(self.folder) if _NAME.match(n))
        except OSError:
            return []

    def files(self):
        """[(name, bytes)] of the history files, oldest first."""
        out = []
        for n in self._file_names():
            try:
                out.append((n, os.path.getsize(os.path.join(self.folder, n))))
            except OSError:
                pass
        return out

    def info(self):
        fl = self.files()
        with self._lock:
            first = self._coarse[0][0] if self._coarse else None
        return {"folder": self.folder, "days": self.days, "files": len(fl), "bytes": sum(b for _, b in fl),
                "first": first, "write_ok": not self._write_failed}

    def purge(self):
        """Deletes day files older than the retention window. Returns how many were deleted."""
        today = _local_date(self._clock())
        self._purged_for = today
        cut = _local_date(self._clock() - (self.days - 1) * 86400)  # keep today and the days-1 before it
        n = 0
        for name in self._file_names():
            m = _NAME.match(name)
            if "-".join(m.groups()) < cut:
                try:
                    os.remove(os.path.join(self.folder, name))
                    n += 1
                except OSError as e:
                    log.warning("history: cannot delete %s: %s", name, e)
        if n:
            log.info("history: deleted %d day file(s) older than %d days", n, self.days)
        return n

    def set_days(self, days):
        self.days = clamp_days(days)
        cut = self._cut()
        with self._lock:
            self._coarse = [x for x in self._coarse if x[0] >= cut]
        self.purge()

    def delete_all(self):
        """Deletes every history file and empties the chart. Returns (files deleted, files that could not be)."""
        done = failed = 0
        for name in self._file_names():
            try:
                os.remove(os.path.join(self.folder, name))
                done += 1
            except OSError as e:
                failed += 1
                log.warning("history: cannot delete %s: %s", name, e)
        with self._lock:
            self._fine.clear()
            self._coarse = []
            self._minute = self._step = None
            self._macc, self._sacc = _Acc(), _Acc()
            self._heads = {}
        log.info("history: deleted %d file(s) on request%s", done, f", {failed} could not be deleted" if failed else "")
        return done, failed

    def zip_bytes(self):
        """The history files as one zip (the download on the status page)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, _ in self.files():
                try:
                    z.write(os.path.join(self.folder, name), name)
                except OSError as e:
                    log.warning("history: cannot add %s to the download: %s", name, e)
        return buf.getvalue()
