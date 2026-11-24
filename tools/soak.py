"""Soak test: a night's worth of client traffic in a few hours, to find leaks and rare failures.

Acts like NINA, only faster: one thread per Alpaca device reads its properties in a loop and now and then moves it
(focuser, rotator, the panel's light, optionally the cover and the box's switches). Another thread acts like the
Overview page left open in a browser. Optionally, a driver process is killed every few minutes to check that the
hub brings it back and clients only see "not connected" for a short while.

Every --sample-s seconds a line is printed and a CSV row written: per process (the hub and each driver) memory,
threads and handles; per device the request count, errors and response times. At the end, a summary: how each
process's memory, threads and handles moved over the run (the first 10 % left out as warm-up), every outage (a
device answering with errors) and how long it lasted, and the slowest answers.

Process figures need the hub on this PC (psutil sees only local processes). Against a remote hub (the mini-PC)
the device side still runs; the process side is skipped.

Profiles (each value can be overridden by its option):
    sim       everything, often: moves every few seconds, cover and switch writes, a driver killed every 10 min.
    hardware  (default) gentle and safe for the real rig: reads as fast as --interval, small focuser and rotator
              moves, light on and off; NO cover moves, NO switch writes (the box stores every command it gets,
              wearing its EEPROM), NO kills. Turn those on one by one, after checking the rig.

usage:
    python -m hub --simulate nitecrawler,box,panel,lynx,gemini --http-port 11196 --bind 127.0.0.1
    python tools/soak.py --hub http://127.0.0.1:11196 --profile sim --minutes 120
    python tools/soak.py --hub http://m6s:11111 --minutes 180                 # real rig, hardware profile

At the end (or on Ctrl+C) every device is put back as it was found: focuser and rotator positions, light,
cover, switch values; then disconnected. Output goes to soak-<time>.csv / .log in --out (default: this folder's
parent's `soak` folder, which git ignores).
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import psutil
except ImportError:          # process figures are skipped without it
    psutil = None

NOT_CONNECTED = 0x407
COVER_STATES = {0: "not present", 1: "closed", 2: "moving", 3: "open", 4: "unknown", 5: "error"}
NOT_IMPLEMENTED = 0x400
BUSY = 0x40B          # "still moving": the device takes no command until its move ends
CLIENT_ID = random.randint(1000, 60000)

PROFILES = {
    #             interval  focus_s focus_steps rot_s rot_deg light_s cover_s switch_s kill_min
    "sim":      dict(interval=0.1, focus_every=5, focus_steps=2000, rot_every=10, rot_deg=20.0, light_every=5,
                     cover_every=60, switch_every=10, kill_every=10),
    "hardware": dict(interval=0.5, focus_every=60, focus_steps=100, rot_every=120, rot_deg=2.0, light_every=60,
                     cover_every=0, switch_every=0, kill_every=0),
}

READS = {
    "focuser": ["connected", "position", "ismoving", "temperature", "tempcomp", "maxstep"],
    "rotator": ["connected", "position", "mechanicalposition", "ismoving", "targetposition"],
    "covercalibrator": ["connected", "coverstate", "calibratorstate", "brightness"],
    "observingconditions": ["connected", "temperature", "humidity", "dewpoint", "pressure", "skytemperature",
                            "averageperiod"],
    "switch": ["connected"],          # plus getswitchvalue for every switch
}


def now_s():
    return time.strftime("%H:%M:%S")


class Log:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()

    def __call__(self, msg, echo=True):
        line = f"{now_s()}  {msg}"
        with self.lock:
            self.f.write(line + "\n")
            self.f.flush()
            if echo:
                print(line, flush=True)


class AlpacaError(Exception):
    def __init__(self, number, message):
        super().__init__(f"{number:#x} {message}")
        self.number = number


class Client:
    """Alpaca calls, timed. Every call is recorded on its device's Stats."""

    def __init__(self, base, timeout):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._txn = 0
        self._lock = threading.Lock()

    def _next(self):
        with self._lock:
            self._txn += 1
            return self._txn

    def raw(self, path, timeout=None):
        with urllib.request.urlopen(self.base + path, timeout=timeout or self.timeout) as r:
            return r.read()

    def call(self, stats, method, path, busy_ok=False, **params):
        """busy_ok: a BUSY answer is expected (the caller waits and tries again), so not counted as an error."""
        params.update(ClientID=CLIENT_ID, ClientTransactionID=self._next())
        data = urllib.parse.urlencode(params)
        url = f"{self.base}/api/v1/{path}"
        req = (urllib.request.Request(url, data=data.encode(), method="PUT") if method == "PUT"
               else urllib.request.Request(url + "?" + data))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            stats.record(time.perf_counter() - t0, f"http {e.code}", method)
            raise
        except Exception as e:  # timeouts, refused connections, resets
            stats.record(time.perf_counter() - t0, type(e).__name__, method)
            raise
        dt = time.perf_counter() - t0
        n = body.get("ErrorNumber", 0)
        if n:
            stats.record(dt, "not connected" if n == NOT_CONNECTED else
                         None if n == NOT_IMPLEMENTED or (busy_ok and n == BUSY) else f"alpaca {n:#x}", method)
            raise AlpacaError(n, body.get("ErrorMessage", ""))
        if method == "GET" and path.endswith("/connected") and body.get("Value") is False:
            # dropped (a hub restart forgets its clients): the one read that still answers, so not a recovery
            stats.record(dt, "not connected", method)
            raise AlpacaError(NOT_CONNECTED, "Connected is false")
        stats.record(dt, None, method)
        return body.get("Value")


class Stats:
    """Per device: response times and errors in the current sample window, outages over the whole run."""

    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.lock = threading.Lock()
        self.times, self.wtimes, self.errors = [], [], {}
        self.total_calls = self.total_errors = 0
        self.all_max = (0.0, "")          # slowest read (GET)
        self.wmax = (0.0, "")             # slowest write (PUT: moves, switch changes; a stock box takes ~2 s)
        self.down_since = None
        self.outages = []            # (start wall time, seconds, first error)
        self._first_err = ""

    def record(self, dt, err, method="GET"):
        with self.lock:
            self.total_calls += 1
            if method == "PUT":
                self.wtimes.append(dt)
                if dt > self.wmax[0]:
                    self.wmax = (dt, now_s())
            else:
                self.times.append(dt)
                if dt > self.all_max[0]:
                    self.all_max = (dt, now_s())
            if err:
                self.total_errors += 1
                self.errors[err] = self.errors.get(err, 0) + 1
                if self.down_since is None:
                    self.down_since, self._first_err = time.time(), err
                    self.log(f"{self.name}: errors start ({err})")
            elif self.down_since is not None:
                d = time.time() - self.down_since
                self.outages.append((self.down_since, d, self._first_err))
                self.log(f"{self.name}: answering again after {d:.1f} s")
                self.down_since = None

    def take(self):
        with self.lock:
            t, w, e = self.times, self.wtimes, self.errors
            self.times, self.wtimes, self.errors = [], [], {}
        return t, w, e


class Device(threading.Thread):
    def __init__(self, cli, typ, num, name, opt, log, stop):
        super().__init__(name=f"{typ}{num}", daemon=True)
        self.cli, self.typ, self.num, self.title = cli, typ, num, name
        self.path = f"{typ}/{num}"
        self.opt, self.log, self.stop = opt, log, stop
        self.stats = Stats(f"{typ} #{num} ({name})", log)
        self.reads = list(READS.get(typ, ["connected"]))
        self.saved = {}
        self.next_act = {}
        self.ready = False
        self.unconnected_since = None     # first "not connected" answer in a row, for --reconnect-after
        self.cover_move = None            # (state wanted, monotonic time sent, command) until the cover gets there
        self.cover_done = []              # seconds each cover move took
        self.cover_failed = []            # (wall time, what happened)
        self.cover_stuck = None           # the state the cover rests in when neither open nor closed, logged once

    # -- helpers
    def get(self, prop, **kw):
        return self.cli.call(self.stats, "GET", f"{self.path}/{prop}", **kw)

    def put(self, prop, busy_ok=False, **kw):
        return self.cli.call(self.stats, "PUT", f"{self.path}/{prop}", busy_ok=busy_ok, **kw)

    def put_when_free(self, prop, limit=120, **kw):
        """put, tried again while the device says it is busy (BUSY: still moving). Those refusals are expected
        here (e.g. the end-of-run restore while the cover is still moving), so they don't count as errors; a
        refusal after `limit` s does."""
        end = time.monotonic() + limit
        while True:
            try:
                return self.put(prop, busy_ok=time.monotonic() < end, **kw)
            except AlpacaError as e:
                if e.number != BUSY or time.monotonic() > end:
                    raise
                time.sleep(1)

    def quiet(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None

    def due(self, what, every):
        if not every:
            return False
        t = time.monotonic()
        if t >= self.next_act.setdefault(what, t + every * random.uniform(0.5, 1.0)):
            self.next_act[what] = t + every
            return True
        return False

    def wait_moving(self, limit=180):
        end = time.monotonic() + limit
        while time.monotonic() < end and not self.stop.is_set():
            if not self.quiet(self.get, "ismoving"):
                return
            time.sleep(0.2)

    def wait_state(self, prop, busy, limit=120):
        end = time.monotonic() + limit
        while time.monotonic() < end and not self.stop.is_set():
            if self.quiet(self.get, prop) != busy:
                return
            time.sleep(0.5)

    # -- setup and restore
    def setup(self):
        self.put("connected", Connected="True")
        if self.typ == "focuser":
            self.absolute = self.get("absolute")
            self.maxstep = self.get("maxstep")
            self.saved["position"] = self.get("position")
        elif self.typ == "rotator":
            self.saved["position"] = self.get("position")
        elif self.typ == "covercalibrator":
            self.saved["calibratorstate"] = self.get("calibratorstate")
            self.saved["brightness"] = self.get("brightness")
            self.saved["coverstate"] = self.get("coverstate")
            self.maxbright = self.quiet(self.get, "maxbrightness") or 0
        elif self.typ == "switch":
            self.nswitch = self.get("maxswitch")
            self.writable = []
            for i in range(self.nswitch):
                if self.get("canwrite", Id=i):
                    lo, hi = self.get("minswitchvalue", Id=i), self.get("maxswitchvalue", Id=i)
                    step = self.get("switchstep", Id=i)
                    self.saved[i] = self.get("getswitchvalue", Id=i)
                    if self.opt.switch_ids is None or i in self.opt.switch_ids:
                        self.writable.append((i, lo, hi, step))
        self.ready = True

    def restore(self):
        if not self.ready:
            return
        try:
            if self.typ == "focuser" and "position" in self.saved and self.absolute:
                self.wait_moving()
                self.put_when_free("move", Position=self.saved["position"])
                self.wait_moving()
            elif self.typ == "rotator":
                self.wait_moving()
                self.put_when_free("moveabsolute", Position=self.saved["position"])
                self.wait_moving()
            elif self.typ == "covercalibrator":
                self.wait_state("coverstate", 2)                    # the panel takes nothing while moving
                if self.saved["calibratorstate"] not in (0,):       # 0 = no light
                    self.put_when_free("calibratoroff")
                if self.opt.cover_every and self.saved["coverstate"] in (1, 3):   # closed / open
                    if self.get("coverstate") != self.saved["coverstate"]:
                        self.put_when_free("closecover" if self.saved["coverstate"] == 1 else "opencover")
                        time.sleep(1)
                        self.wait_state("coverstate", 2)
                if self.saved["calibratorstate"] == 3:              # was lit (only possible with the cover closed)
                    self.put_when_free("calibratoron", Brightness=self.saved["brightness"])
            elif self.typ == "switch":
                for i, v in self.saved.items():
                    if self.quiet(self.get, "getswitchvalue", Id=i) != v:
                        self.put_when_free("setswitchvalue", Id=i, Value=v)
        except Exception as e:
            self.log(f"{self.stats.name}: could not put it back as found: {e}")
        self.quiet(self.put, "connected", Connected="False")

    # -- the loop
    def run(self):
        while not self.stop.is_set() and not self.ready:
            try:
                self.setup()
            except Exception as e:
                self.log(f"{self.stats.name}: setup failed ({e}); trying again")
                self.stop.wait(5)
        reads = self.reads
        while not self.stop.is_set():
            for prop in reads:
                if self.stop.is_set():
                    break
                try:
                    self.get(prop)
                    self.unconnected_since = None
                except AlpacaError as e:
                    if e.number == NOT_IMPLEMENTED:
                        reads = [r for r in reads if r != prop]
                    elif e.number == NOT_CONNECTED:
                        self.reconnect_if_due()
                except Exception:
                    pass
                self.stop.wait(self.opt.interval)
            if self.typ == "switch":
                for i in range(self.nswitch):
                    self.quiet(self.get, "getswitchvalue", Id=i)
                    self.stop.wait(self.opt.interval)
            if self.stop.is_set():
                break
            try:
                self.act()
            except Exception as e:
                self.log(f"{self.stats.name}: action failed: {e}", echo=False)

    def reconnect_if_due(self):
        """Like a client that notices it was dropped (a hub restart forgets who was connected) and connects
        again after a while. A driver restart doesn't need this: the hub reconnects the client by itself."""
        after = self.opt.reconnect_after
        if not after:
            return
        t = time.monotonic()
        if self.unconnected_since is None:
            self.unconnected_since = t
        elif t - self.unconnected_since >= after:
            self.quiet(self.put, "connected", Connected="True")
            self.log(f"{self.stats.name}: still not connected after {t - self.unconnected_since:.0f} s; "
                     "connected it again, as a client would")
            self.unconnected_since = None

    def act(self):
        o = self.opt
        if self.typ == "focuser" and self.absolute and self.due("focus", o.focus_every):
            if not self.get("ismoving"):
                pos = self.get("position")
                target = max(0, min(self.maxstep, pos + random.choice((-1, 1)) * random.randint(1, o.focus_steps)))
                self.put("move", Position=target)
        elif self.typ == "rotator" and self.due("rot", o.rot_every):
            if not self.get("ismoving"):        # absolute, within ± rot_deg of where it was found
                target = (self.saved["position"] + random.uniform(-o.rot_deg, o.rot_deg)) % 360
                self.put("moveabsolute", Position=round(target, 2))
        elif self.typ == "covercalibrator":
            cover = self.get("coverstate")
            if self.cover_move and not self.check_cover_move(cover):
                return
            if cover == 2:                        # moving: the panel takes no command until it stops
                return
            if o.cover_every and cover not in (0, 1, 3):
                if self.cover_stuck != cover:     # say so once, not every loop: no cover moves until it is
                    self.log(f"{self.stats.name}: the cover is neither open nor closed "
                             f"({COVER_STATES.get(cover, cover)}); no cover moves until it is")
                self.cover_stuck = cover
            else:
                self.cover_stuck = None
            if self.maxbright and self.due("light", o.light_every):
                if self.get("calibratorstate") == 3:
                    self.put("calibratoroff")
                elif cover in (0, 1):             # lights only with the cover closed (or no cover)
                    self.put("calibratoron", Brightness=random.randint(1, self.maxbright))
            if self.due("cover", o.cover_every):
                if cover == 1:
                    if self.get("calibratorstate") == 3:
                        self.put("calibratoroff")
                    self.put("opencover")
                    self.cover_move = (3, time.monotonic(), "open")
                elif cover == 3:
                    self.put("closecover")
                    self.cover_move = (1, time.monotonic(), "close")
        elif self.typ == "switch" and self.writable and self.due("switch", o.switch_every):
            i, lo, hi, step = random.choice(self.writable)
            steps = int(round((hi - lo) / step)) if step else 0
            self.put("setswitchvalue", Id=i, Value=min(hi, lo + step * random.randint(0, steps)) if steps else lo)

    COVER_LIMIT_S = 120   # a full move takes ~18 s; a driver restarted mid-move reads Unknown until the panel talks

    def check_cover_move(self, cover):
        """After an open or close: True once the cover is where it was sent. A move that ends anywhere else counts
        as failed: on 2026-11-23 the cap caught on something and stopped part way, and a run that only counted
        Alpaca errors went on for 7 hours without saying so. Moving (2) and Unknown (4) wait, up to COVER_LIMIT_S."""
        want, t0, cmd = self.cover_move
        dt = time.monotonic() - t0
        if cover == want:
            self.cover_done.append(dt)
            self.cover_move = None
            return True
        if cover in (2, 4) and dt < self.COVER_LIMIT_S:
            return False
        what = f"{cmd} didn't arrive: {COVER_STATES.get(cover, cover)} after {dt:.0f} s" + self.cover_detail()
        self.cover_failed.append((now_s(), what))
        self.log(f"{self.stats.name}: COVER MOVE FAILED, {what}")
        self.cover_move = None
        return True

    def cover_detail(self):
        """Where the panel says the cap is, from the panel driver's status page (empty if there is none)."""
        try:
            st = json.loads(self.cli.raw("/panel/status.json"))
        except Exception:
            return ""
        pos = st.get("position")
        bits = [f"at {pos:g}°" if isinstance(pos, (int, float)) else "", st.get("error") or ""]
        return "".join(f"; {b}" for b in bits if b)


class Page(threading.Thread):
    """The Overview page left open: /hub.json every 10 s, each device's fine history every 3 s, coarse every
    60 s (as hub/overview_ui/device.js does)."""

    def __init__(self, cli, log, stop):
        super().__init__(name="page", daemon=True)
        self.cli, self.log, self.stop = cli, log, stop
        self.stats = Stats("Overview page", log)
        self.since = {}

    def fetch(self, path):
        t0 = time.perf_counter()
        try:
            body = json.loads(self.cli.raw(path))
        except Exception as e:
            self.stats.record(time.perf_counter() - t0, type(e).__name__)
            return None
        self.stats.record(time.perf_counter() - t0, None)
        return body

    def run(self):
        slugs, n = [], 0
        while not self.stop.is_set():
            if n % 10 == 0 or not slugs:
                h = self.fetch("/hub.json")
                if h:
                    slugs = [d["slug"] for d in h.get("devices", []) if d.get("up")]
            for s in slugs:
                for res in ("s",) + (("m",) if n % 60 == 0 else ()):
                    key = (s, res)
                    q = f"?res={res}" + (f"&since={self.since[key]}" if key in self.since else "")
                    body = self.fetch(f"/{s}/history.json{q}")
                    t = _last_t(body)
                    if t:
                        self.since[key] = t
            n += 3
            self.stop.wait(3)


def _last_t(body):
    """The newest sample time in a history.json reply ({"keys": [...], "rows": [[t, ...], ...]})."""
    try:
        return body["rows"][-1][0]
    except Exception:
        return None


class Procs:
    """The hub's process and its drivers' processes, found through /hub/drivers (a driver's parent is the hub)."""

    def __init__(self, cli, log, local):
        self.cli, self.log = cli, log
        self.local = local and psutil is not None
        self.first, self.last = {}, {}      # name -> (t, rss MB, threads, handles), after warm-up
        self.series = {}

    def drivers(self):
        try:
            return json.loads(self.cli.raw("/hub/drivers"))
        except Exception:
            return []

    def sample(self):
        out = {}
        drv = self.drivers()
        hub_pid = None
        for d in drv:
            if d.get("pid") and self.local:
                try:
                    p = psutil.Process(d["pid"])
                    hub_pid = hub_pid or p.ppid()
                    out[f"driver {d['name']}"] = self._figures(p)
                except psutil.Error:
                    pass
        if hub_pid and self.local:
            try:
                out["hub"] = self._figures(psutil.Process(hub_pid))
            except psutil.Error:
                pass
        restarts = {d["name"]: d.get("restarts_1h", 0) for d in drv}
        states = {d["name"]: d.get("state") for d in drv}
        t = time.monotonic()
        for name, fig in out.items():
            self.series.setdefault(name, []).append((t,) + fig)
        return out, restarts, states

    @staticmethod
    def _figures(p):
        with p.oneshot():
            handles = p.num_handles() if hasattr(p, "num_handles") else p.num_fds()
            return (round(p.memory_info().rss / 1e6, 1), p.num_threads(), handles)

    def kill_one(self, only=None):
        if not self.local:
            return None
        # only a driver that runs a device: killing an idle one (no device of its kind plugged in) tests nothing
        drv = [d for d in self.drivers() if d.get("pid") and d.get("state") == "running" and d.get("devices")
               and (not only or d["name"] in only)]
        if not drv:
            return None
        d = random.choice(drv)
        try:
            psutil.Process(d["pid"]).kill()
            return d["name"]
        except psutil.Error as e:
            self.log(f"kill {d['name']}: {e}")
            return None


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def slope_per_hour(points, idx):
    """Least-squares slope of column idx over time, per hour."""
    if len(points) < 3:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[idx] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if not den else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 3600


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hub", default="http://127.0.0.1:11111")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="hardware")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--sample-s", type=float, default=60)
    ap.add_argument("--timeout", type=float, default=10, help="per request (s)")
    ap.add_argument("--interval", type=float, help="pause between reads, per device (s)")
    ap.add_argument("--focus-every", type=float, help="s between focuser moves (0: none)")
    ap.add_argument("--focus-steps", type=int, help="largest focuser move (steps)")
    ap.add_argument("--rot-every", type=float, help="s between rotator moves (0: none)")
    ap.add_argument("--rot-deg", type=float, help="rotator moves stay within ± this many degrees")
    ap.add_argument("--light-every", type=float, help="s between panel light changes (0: none)")
    ap.add_argument("--cover-every", type=float, help="s between cover moves (0: none). Check the rig first")
    ap.add_argument("--switch-every", type=float,
                    help="s between switch writes (0: none). On a real box each one is a stored command")
    ap.add_argument("--switch-ids", help="only write these switches, e.g. 1,5 (default: every writable one)")
    ap.add_argument("--kill-every", type=float, help="minutes between killing a random driver process (0: none)")
    ap.add_argument("--kill-only", help="only kill these drivers, e.g. wcover (names as /hub/drivers lists them)")
    ap.add_argument("--reconnect-after", type=float, default=0,
                    help="s of 'not connected' before a device is connected again, as a client would after a "
                         "hub restart (0: never; a driver restart needs none: the hub reconnects by itself)")
    ap.add_argument("--types", help="only these device types, e.g. focuser,rotator")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "soak"))
    a = ap.parse_args()
    opt = argparse.Namespace(**PROFILES[a.profile])
    for k in PROFILES[a.profile]:
        if getattr(a, k) is not None:
            setattr(opt, k, getattr(a, k))
    opt.reconnect_after = a.reconnect_after
    opt.switch_ids = {int(x) for x in a.switch_ids.split(",")} if a.switch_ids else None

    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = Log(os.path.join(a.out, f"soak-{stamp}.log"))
    cli = Client(a.hub, a.timeout)
    host = urllib.parse.urlparse(a.hub).hostname or ""
    local = host in ("127.0.0.1", "localhost", "::1") or host.lower() == os.environ.get("COMPUTERNAME", "").lower()

    try:
        devs = json.loads(cli.raw("/management/v1/configureddevices"))["Value"]
    except Exception as e:
        sys.exit(f"no hub at {a.hub}: {e}")
    types = set(a.types.lower().split(",")) if a.types else None
    devs = [d for d in devs if types is None or d["DeviceType"].lower() in types]
    if not devs:
        sys.exit("the hub lists no devices (yet?)")
    log(f"soak {a.profile} on {a.hub} for {a.minutes:g} min; " + ", ".join(f"{k}={v}" for k, v in vars(opt).items()))
    for d in devs:
        log(f"  {d['DeviceType']} #{d['DeviceNumber']}: {d['DeviceName']}")
    if not local:
        log("the hub is on another PC: no process figures, no kills")
    elif psutil is None:
        log("psutil isn't installed: no process figures, no kills")

    stop = threading.Event()
    workers = [Device(cli, d["DeviceType"].lower(), d["DeviceNumber"], d["DeviceName"], opt, log, stop)
               for d in devs]
    page = Page(cli, log, stop)
    procs = Procs(cli, log, local)
    for w in workers + [page]:
        w.start()

    csv_path = os.path.join(a.out, f"soak-{stamp}.csv")
    fcsv = open(csv_path, "w", newline="", encoding="utf-8")
    wcsv = csv.writer(fcsv)
    wcsv.writerow(["time", "elapsed_min", "what", "rss_mb", "threads", "handles", "calls", "errors", "p50_ms",
                   "p95_ms", "max_ms", "writes", "write_max_ms", "restarts_1h", "state"])

    start = time.monotonic()
    end = start + a.minutes * 60
    next_kill = start + opt.kill_every * 60 if opt.kill_every and local else None
    kills = []
    try:
        while time.monotonic() < end:
            stop.wait(min(a.sample_s, max(0.1, end - time.monotonic())))
            if next_kill and time.monotonic() >= next_kill:
                name = procs.kill_one(set(a.kill_only.split(",")) if a.kill_only else None)
                if name:
                    log(f"killed driver process {name}")
                    kills.append(name)
                next_kill = time.monotonic() + opt.kill_every * 60
            el = (time.monotonic() - start) / 60
            figs, restarts, states = procs.sample()
            parts = []
            for name, (rss, th, h) in figs.items():
                wcsv.writerow([now_s(), f"{el:.1f}", name, rss, th, h, "", "", "", "", "", "", "",
                               restarts.get(name.replace("driver ", ""), ""), states.get(name.replace("driver ", ""), "")])
                parts.append(f"{name.replace('driver ', '')} {rss:.0f}MB/{th}t/{h}h")
            calls = errs = 0
            worst = 0.0
            for w in workers + [page]:
                t, wt, e = w.stats.take()
                ne = sum(e.values())
                calls += len(t) + len(wt)
                errs += ne
                worst = max(worst, max(t, default=0))
                wcsv.writerow([now_s(), f"{el:.1f}", w.stats.name, "", "", "", len(t) + len(wt), ne,
                               round(pct(t, 50) * 1000, 1), round(pct(t, 95) * 1000, 1),
                               round(max(t, default=0) * 1000, 1), len(wt), round(max(wt, default=0) * 1000, 1),
                               "", json.dumps(e) if e else ""])
            fcsv.flush()
            log(f"{el:5.1f} min  {calls} calls, {errs} errors, slowest read {worst * 1000:.0f} ms"
                + (" | " + ", ".join(parts) if parts else ""))
    except KeyboardInterrupt:
        log("stopped (Ctrl+C)")
    stop.set()
    log("putting the devices back as they were found...")
    for w in workers:
        w.join(timeout=a.timeout + 2)
    restore = [threading.Thread(target=w.restore) for w in workers]
    for t in restore:
        t.start()
    for t in restore:
        t.join(timeout=300)
    fcsv.close()

    # -- summary
    dur_h = (time.monotonic() - start) / 3600
    log("=" * 70)
    log(f"summary over {dur_h * 60:.0f} min (figures from {csv_path})")
    for name, pts in procs.series.items():
        pts = pts[max(1, len(pts) // 10):]          # leave out the warm-up
        if len(pts) < 3:
            continue
        first, last = pts[0], pts[-1]
        log(f"  {name:22s} memory {first[1]:.0f}->{last[1]:.0f} MB ({slope_per_hour(pts, 1):+.1f} MB/h), "
            f"threads {first[2]}->{last[2]} ({slope_per_hour(pts, 2):+.1f}/h), "
            f"handles {first[3]}->{last[3]} ({slope_per_hour(pts, 3):+.1f}/h)")
    if kills:
        log(f"  driver processes killed: {len(kills)} ({', '.join(kills)})")
    for w in workers + [page]:
        s = w.stats
        outs = s.outages + ([(s.down_since, time.time() - s.down_since, s._first_err)] if s.down_since else [])
        log(f"  {s.name}: {s.total_calls} calls, {s.total_errors} errors, slowest read {s.all_max[0] * 1000:.0f} ms "
            f"at {s.all_max[1]}" + (f", slowest write {s.wmax[0] * 1000:.0f} ms" if s.wmax[0] else "") + (f"; {len(outs)} outage(s), longest {max(o[1] for o in outs):.1f} s" if outs
                                     else ""))
        for t0, d, why in outs:
            log(f"      {time.strftime('%H:%M:%S', time.localtime(t0))}  {d:6.1f} s  ({why})", echo=False)
        if getattr(w, "cover_done", None) or getattr(w, "cover_failed", None):
            log(f"    cover moves: {len(w.cover_done)} arrived"
                + (f" (slowest {max(w.cover_done):.0f} s)" if w.cover_done else "")
                + f", {len(w.cover_failed)} failed")
            for t, what in w.cover_failed:
                log(f"      {t}  {what}")
    log("A healthy run: memory, threads and handles flat after warm-up (a few MB/h of noise is normal; "
        "anything climbing steadily is a leak), no outage except right after a kill, and each of those under ~30 s, and no failed cover move.")


if __name__ == "__main__":
    main()
