"""Driver processes, from the hub's side (REFACTOR.md step 5).

Each driver module runs in its own process (python -m hub --driver <module>, hub/driverhost.py). The hub sees its
drivers through RemoteDriver and its devices through RemoteInstance, which look like the in-process Driver and
Instance to hub/host.py. No driver code runs in the hub's process.

Supervision:
- The hub pings each process every PING_S; after MISSES missed pings it ends the process.
- When a process ends (a crash, a kill, exit()), its ports stay open in the hub (the relay holds them), its tabs say
  "driver restarting", and Alpaca calls get "not connected". The process is started again after 1, 2, 4 … 60 s and
  its devices are started again on the same ports, with the same numbers, so the same tabs and UniqueIDs.
- After FAILS ends within FAIL_WINDOW_S the driver is "failed": no more restarts until retry() (Overview's
  "Try again"). Its ports stay held.
- Everything a driver process sends is checked (hub/channel.py, hub/relay.py check_driver_msg); a bad message ends
  the process and counts as one of its failures.
"""
import collections
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Listener

from .channel import CallTimeout, Channel, ChannelClosed
from .driverhost import AUTH_ENV
from .ports import PortInfo
from .usbdevs import UsbDevice
from .relay import check_driver_msg

log = logging.getLogger("hub.supervisor")

PING_S = 2.0
WATCH_S = 2.0              # how often the driver folders are looked at (installs, updates, uninstalls)
MISSES = 3
BACKOFF_FIRST_S = 1.0      # the first restart waits this long, then twice as long each time, up to BACKOFF_MAX_S
BACKOFF_MAX_S = 60.0
FAILS = 5
FAIL_WINDOW_S = 600.0
CONNECT_S = 30.0
T_RECOGNISE = 2.0
T_PROBE = 10.0
T_START = 10.0
T_STOP = 10.0
T_PING = 2.0
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_seq = iter(range(1, 1 << 30))


def _usb_set(v):
    return frozenset((int(a), int(b)) for a, b in v)


def _check_describe(d):
    ok = (isinstance(d, dict) and isinstance(d.get("key"), str) and d["key"] and isinstance(d.get("title"), str)
          and isinstance(d.get("usb_ids"), list) and isinstance(d.get("never_probe"), list)
          and all(isinstance(x, list) and len(x) == 2 and all(type(i) is int for i in x)
                  for x in d["usb_ids"] + d["never_probe"])
          and (d.get("listen_baud") is None or type(d["listen_baud"]) is int)
          and (d.get("probe_baud") is None or type(d["probe_baud"]) is int)
          and isinstance(d.get("probe_priority", 100), (int, float)) and isinstance(d.get("simulators", []), list)
          and d.get("transport", "serial") in ("serial", "usb")
          and isinstance(d.get("usb_vendors", []), list) and all(type(v) is int for v in d.get("usb_vendors", [])))
    if not ok:
        raise ValueError("bad driver description")


class RemoteDriver:
    """A Driver living in a driver process."""

    def __init__(self, proc, d):
        self.proc = proc
        self.update(d)

    def update(self, d):
        _check_describe(d)
        self.key, self.title = d["key"], d["title"]
        self.api = d.get("api")
        self.transport = d.get("transport", "serial")
        self.usb_ids = _usb_set(d["usb_ids"])
        self.usb_vendors = frozenset(int(v) for v in d.get("usb_vendors", []))
        self.listen_baud, self.probe_baud = d.get("listen_baud"), d.get("probe_baud")
        self.probe_priority = d.get("probe_priority", 100)
        self.never_probe = _usb_set(d["never_probe"])
        self.lines = bool(d.get("lines"))
        self.simulators = {n: None for n in d.get("simulators", []) if isinstance(n, str)}

    def recognise(self, data):
        if not self.proc.running:
            return None
        try:
            r = self.proc.call("recognise", self.key, bytes(data), timeout=T_RECOGNISE)
        except Exception as e:
            log.debug("%s: recognise failed: %s", self.key, e)
            return None
        return r if isinstance(r, dict) and r else None

    def probe_relay(self, relay, port):
        """The probe, in the driver process, on a RelaySerial joined to relay (the hub's side of the port)."""
        if not self.proc.running:
            return None
        self.proc.allow(port, relay)
        try:
            r = self.proc.call("probe", self.key, port.device, relay.baud, timeout=T_PROBE)
        except Exception as e:
            log.debug("%s: probe on %s failed: %s", self.key, port.device, e)
            r = None
        finally:
            self.proc.disallow(port.device, relay)
        return r if isinstance(r, dict) and r else None

    def find_remote(self, present):
        """Transport "usb": the units the driver process lists (Driver.find), or None if it couldn't say (not
        running, no answer): the hub then keeps what it knew. present: the UsbDevices of its vendors; simulated ones
        name their simulator in the driver process."""
        if not self.proc.running:
            return None
        self._present = list(present)
        sims = [[u.instance, u.sim_name, u.sim_index] for u in present if u.sim_name]
        try:
            r = self.proc.call("find", self.key, sims if sims else None, timeout=T_PROBE)
        except Exception as e:
            log.debug("%s: find failed: %s", self.key, e)
            return None
        if not isinstance(r, list):
            return None
        return [x for x in r if isinstance(x, dict) and isinstance(x.get("serial"), str) and x["serial"]]

    def start(self, ctx, port, ident):
        number = ctx._number if ctx._number is not None else 0
        inst = RemoteInstance(self, port, ident, number)
        self.proc.own(inst)
        try:
            inst.start_remote()
        except Exception:
            self.proc.disown(inst)
            raise
        return inst


class RemoteInstance:
    def __init__(self, driver, port, ident, number):
        self.driver = driver
        self.port = port
        self.ident = ident
        self.number = number
        self.slug = None
        self.title = None
        self.local_port = None
        self._status = (False, {"text": "starting", "state": "warn"})

    def start_remote(self):
        p = self.port
        d = self.driver
        if d.transport == "usb" and getattr(d, "_present", None) is not None:
            # A new process (after a restart, an update) knows no SDK or simulator until it has looked once
            d.find_remote(d._present)
        r = self.driver.proc.call("start", self.driver.key,
                                  {"device": p.device, "vid": p.vid, "pid": p.pid, "serial": p.serial,
                                   "description": p.description}, self.ident, self.number, timeout=T_START)
        if not (isinstance(r, dict) and isinstance(r.get("slug"), str) and isinstance(r.get("title"), str)
                and type(r.get("local_port")) is int and 0 < r["local_port"] < 65536):
            raise ValueError(f"{self.driver.key}: bad answer to start")
        if self.slug is not None and r["slug"] != self.slug:
            log.error("%s came back as %s; keeping the tab %s", self.slug, r["slug"], self.slug)
        else:
            self.slug, self.title = r["slug"], r["title"]
        self.local_port = r["local_port"]
        # Not up until the driver's next ping says so: its link may still be waiting for the device's first answer
        # (the box: its first status line). Saying "up" here let a client connect too early (contract test, step 9).
        self._status = (False, {"text": "starting", "state": "warn"})

    def set_status(self, up, summary):
        if isinstance(summary, dict) and isinstance(summary.get("text", ""), str) \
                and summary.get("state", "ok") in ("ok", "warn", "bad"):
            self._status = (bool(up), {"text": summary.get("text", ""), "state": summary.get("state", "ok")})

    def up(self):
        return self.driver.proc.running and self.local_port is not None and self._status[0]

    def summary(self):
        state = self.driver.proc.state
        if state == "failed":
            return {"text": "driver failed (see Overview)", "state": "bad"}
        if not self.driver.proc.running or self.local_port is None:
            return {"text": "driver restarting", "state": "warn"}
        return self._status[1]

    def stop(self):
        proc = self.driver.proc
        proc.disown(self)
        if proc.running and self.slug:
            try:
                proc.call("stop", self.slug, timeout=T_STOP)
            except Exception as e:
                log.warning("%s: stop failed: %s", self.slug, e)


class RemoteSimPort:
    """A simulated device's port, as the hub's relay sees it: its simulator runs in the driver process."""

    def __init__(self, proc, device, name, index, baud):
        self.proc, self.device = proc, device
        self.gen = proc.gen
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        proc.sim_ports[device] = self
        proc.call("sim_open", device, name, index, baud, timeout=T_START)

    def _check(self):
        if not self.is_open:
            raise OSError(f"{self.device} closed")
        if not self.proc.running or self.proc.gen != self.gen:
            raise OSError(f"{self.device}: its driver process ended")

    def feed(self, data):
        with self._lock:
            self._buf += data
            if len(self._buf) > 1 << 20:
                del self._buf[:len(self._buf) - (1 << 20)]

    @property
    def in_waiting(self):
        self._check()
        with self._lock:
            return len(self._buf)

    def read(self, n=1):
        self._check()
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def write(self, data):
        self._check()
        self.proc.channel.send(["sw", self.device, bytes(data)])
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._lock:
            self._buf.clear()

    def close(self):
        if not self.is_open:
            return
        self.is_open = False
        if self.proc.sim_ports.get(self.device) is self:
            del self.proc.sim_ports[self.device]
        try:
            if self.proc.running and self.proc.gen == self.gen:
                self.proc.channel.send(["sc", self.device])
        except Exception:
            pass


class DriverProcess:
    """One driver package's process (one catalog Entry): started, pinged, restarted, switched to a new version."""

    def __init__(self, sup, entry):
        self.sup = sup
        self.entry = entry
        self.module = entry.module
        self.name = entry.key
        self.pending = None                # a newer Entry waiting for its clients to disconnect ("update ready")
        self._switch_lock = threading.Lock()
        self.drivers = {}                  # key -> RemoteDriver
        self.state = "off"                 # starting, running, restarting, failed, off
        self.why = ""
        self._pending_why = ""             # why the hub is ending it (a hang, a bad message), for _ended
        self.pid = None
        self.gen = 0                       # one per process started
        self.channel = None
        self.popen = None
        self.ends = collections.deque()    # times the process ended
        self.restarts = collections.deque()
        self.misses = 0
        self.last_lines = []
        self._lock = threading.RLock()
        self._owned = {}                   # id(RemoteInstance) -> RemoteInstance
        self._allowed = {}                 # device -> (PortInfo, relay or None): what this process may open
        self._links = {}                   # chan -> (relay, deliver)
        self.sim_ports = {}                # device -> RemoteSimPort
        self._stopping = False
        self._live_gen = 0                 # the process generation that finished hello (ends of others are ignored)
        self._ended_gen = 0

    @property
    def running(self):
        return self.state == "running" and self.channel is not None and not self.channel.closed

    def call(self, method, *args, timeout=10.0):
        ch = self.channel
        if ch is None or ch.closed:
            raise ChannelClosed(f"{self.name} is not running")
        return ch.call(method, *args, timeout=timeout)

    # ---------------------------------------------------------------- what the process may open
    def own(self, inst):
        with self._lock:
            self._owned[id(inst)] = inst
            self._allowed[inst.port.device] = (inst.port, None)

    def disown(self, inst):
        with self._lock:
            self._owned.pop(id(inst), None)
            if not any(i.port.device == inst.port.device for i in self._owned.values()):
                self._allowed.pop(inst.port.device, None)

    def owned(self):
        with self._lock:
            return sorted(self._owned.values(), key=lambda i: (i.driver.key, i.number))

    def allow(self, port, relay):
        with self._lock:
            self._allowed[port.device] = (port, relay)

    def disallow(self, device, relay):
        with self._lock:
            if self._allowed.get(device, (None, None))[1] is relay:
                del self._allowed[device]

    # ---------------------------------------------------------------- starting
    def spawn(self):
        """Starts the process and says hello. Returns the drivers' descriptions. Raises on failure."""
        address = (rf"\\.\pipe\alpaca-hub-{os.getpid()}-{self.name}-{next(_seq)}" if sys.platform == "win32"
                   else os.path.join(self.sup.config_dir, f".hub-{os.getpid()}-{self.name}-{next(_seq)}.sock"))
        family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
        key = secrets.token_bytes(32)
        listener = Listener(address, family=family, authkey=key)
        env = dict(os.environ)
        env[AUTH_ENV] = key.hex()
        if self.sup.verbose:
            env["ALPACA_HUB_VERBOSE"] = "1"
        logs = os.path.join(self.sup.config_dir, "logs")
        os.makedirs(logs, exist_ok=True)
        err = open(os.path.join(logs, f"{self.name}.stderr.log"), "ab")
        self.module = self.entry.module
        cmd = list(self.sup.command) + ["--driver", self.module, "--pipe", address,
                                        "--config-dir", self.sup.config_dir, "--log-name", self.name]
        if self.entry.path:
            cmd += ["--driver-path", self.entry.path]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.popen = subprocess.Popen(cmd, cwd=_ROOT, env=env, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=err, creationflags=flags)
        finally:
            err.close()
        box = {}

        def accept():
            try:
                box["c"] = listener.accept()
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=accept, name=f"accept-{self.name}", daemon=True)
        t.start()
        end = time.monotonic() + CONNECT_S
        while t.is_alive() and time.monotonic() < end and self.popen.poll() is None:
            t.join(0.1)
        if "c" not in box:
            listener.close()
            self._kill()
            raise RuntimeError(f"{self.name}: the driver process did not connect"
                               + (f" (it ended, code {self.popen.returncode})" if self.popen.poll() is not None
                                  else ""))
        listener.close()
        with self._lock:
            self.gen += 1
            gen = self.gen
            self._links.clear()
        self.channel = Channel(box["c"], f"{self.name}", on_call=self._on_call, on_frame=self._on_frame,
                               on_close=lambda reason, bad, g=gen: self._on_close(g, reason, bad),
                               methods={"open"})
        self.pid = self.popen.pid
        self.state = "starting"
        try:
            info = self.channel.call("hello", self.sup.hello_info(), timeout=CONNECT_S)
            if not isinstance(info, dict) or not isinstance(info.get("drivers"), list) or not info["drivers"]:
                raise ValueError("bad answer to hello")
            for d in info["drivers"]:
                _check_describe(d)
        except Exception:
            self.channel.close("hello failed")
            self._kill()
            raise
        self.misses = 0
        self._live_gen = gen
        threading.Thread(target=self._watch, args=(self.popen, gen), name=f"watch-{self.name}", daemon=True).start()
        threading.Thread(target=self._pinger, args=(gen,), name=f"ping-{self.name}", daemon=True).start()
        return info["drivers"]

    def first_start(self):
        self._apply(self.spawn())
        self.state = "running"
        log.info("driver process %s %s (%s, pid %d): %s", self.name, self.entry.version, self.entry.source,
                 self.pid, ", ".join(self.drivers))

    def _apply(self, descs):
        """Takes the drivers a (new) process declares: same keys are updated in place (their devices keep their
        RemoteDriver), new ones added, missing ones dropped (the hub then drops their devices)."""
        keys = set()
        for d in descs:
            keys.add(d["key"])
            if d["key"] in self.drivers:
                self.drivers[d["key"]].update(d)
            else:
                self.drivers[d["key"]] = RemoteDriver(self, d)
        for k in list(self.drivers):
            if k not in keys:
                log.warning("%s no longer has driver %s; its devices go", self.name, k)
                del self.drivers[k]

    def _bring_back(self):
        """After a new process: its devices start again on the ports the hub kept, with the same numbers."""
        for inst in self.owned():
            if inst.driver.key not in self.drivers:
                continue
            try:
                inst.start_remote()
                log.info("%s is back", inst.slug)
            except Exception as e:
                log.error("%s: starting it again failed: %s", inst.slug, e)
                continue
            if self.sup.hub is not None:
                self.sup.hub.restore_clients(inst)     # its client's devices are connected again

    def _end_planned(self, why):
        """Ends the process on purpose (an update, off, uninstall): not counted as a failure, not restarted."""
        with self._lock:
            self._ended_gen = max(self._ended_gen, self.gen)
            links = list(self._links.values())
            self._links.clear()
        for inst in self.owned():
            if self.running and inst.slug:
                try:
                    self.call("stop", inst.slug, timeout=T_STOP)     # its links let go of their ports first
                except Exception as e:
                    log.warning("%s: stop failed: %s", inst.slug, e)
        if self.channel is not None:
            self.channel.close(why)
        p = self.popen
        if p is not None:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                self._kill()
        for relay, deliver in links:
            relay.detach(deliver)
        for inst in self.owned():
            inst.local_port = None

    def switch(self, entry):
        """Runs entry (a newer version) instead: the old process ends, the new one gets the same held ports, and
        the devices come back on the same tabs with the same UniqueIDs. False if the new version didn't start."""
        with self._switch_lock:
            old = self.entry
            log.info("%s: switching from %s (%s) to %s (%s)", self.name, old.version, old.source, entry.version,
                     entry.source)
            self.state = "updating"
            self._end_planned(f"updating to {entry.version}")
            self.entry, self.pending = entry, None
            try:
                self._apply(self.spawn())
            except Exception as e:
                self.state, self.why = "failed", f"version {entry.version} did not start: {e}"
                self.last_lines = self.sup.tail(self.name)
                log.error("%s: %s", self.name, self.why)
                return False
            self.state = "running"
            self.why = ""
            self.ends.clear()
            self._bring_back()
            log.info("%s: now %s (pid %d)", self.name, entry.version, self.pid)
            return True

    # ---------------------------------------------------------------- the channel
    def _on_call(self, method, args):
        if method == "open" and len(args) == 3 and type(args[0]) is int and isinstance(args[1], str) \
                and type(args[2]) is int:
            return self.sup.open_for(self, *args)
        from .channel import BadMessage
        raise BadMessage(f"bad call {method}")

    def _on_frame(self, msg):
        from .channel import BadMessage
        kind = msg[0]
        if kind == "r" and len(msg) == 3 and type(msg[1]) is int:
            try:
                check_driver_msg(msg[2])
            except ValueError as e:
                raise BadMessage(str(e)) from None
            with self._lock:
                link = self._links.get(msg[1])
                if link is not None and msg[2][0] == "detach":
                    del self._links[msg[1]]
            if link is not None:
                link[0].handle(link[1], msg[2])
            return
        if kind == "sd" and len(msg) == 3 and isinstance(msg[1], str) and isinstance(msg[2], bytes):
            p = self.sim_ports.get(msg[1])
            if p is not None:
                p.feed(msg[2])
            return
        raise BadMessage(f"unexpected {kind!r}")

    def link(self, chan, relay, deliver):
        with self._lock:
            old = self._links.get(chan)
            self._links[chan] = (relay, deliver)
        if old is not None:
            old[0].detach(old[1])

    def allowed(self, device):
        with self._lock:
            return self._allowed.get(device)

    # ---------------------------------------------------------------- supervision
    def _pinger(self, gen):
        while self.gen == gen and not self._stopping:
            time.sleep(PING_S)
            if self.gen != gen or self._stopping or self.channel is None or self.channel.closed:
                return
            if self.state != "running":
                continue
            try:
                r = self.channel.call("ping", self.sup.public_urls(), timeout=T_PING)
                self.misses = 0
            except CallTimeout:
                self.misses += 1
                log.warning("%s: no answer to ping (%d of %d)", self.name, self.misses, MISSES)
                if self.misses >= MISSES:
                    self._pending_why = f"stopped answering ({MISSES} pings missed)"
                    log.error("%s: %s; ending it", self.name, self._pending_why)
                    self._kill()
                    return
                continue
            except Exception:
                return
            if isinstance(r, dict):
                by_slug = {i.slug: i for i in self.owned()}
                for slug, v in r.items():
                    inst = by_slug.get(slug)
                    if inst is not None and isinstance(v, list) and len(v) == 2:
                        inst.set_status(v[0], v[1])

    def _watch(self, popen, gen):
        code = popen.wait()
        self._ended(gen, f"ended with code {code}")

    def _on_close(self, gen, reason, bad):
        if bad:
            self._pending_why = f"sent a bad message: {reason}"
            log.error("%s: %s; ending it", self.name, self._pending_why)
        elif self.popen is not None:
            try:                       # the pipe closes a moment before Windows has the exit code
                reason = f"ended with code {self.popen.wait(2)}"
            except subprocess.TimeoutExpired:
                reason = f"closed its pipe but kept running ({reason})"
        self._kill()
        self._ended(gen, reason)

    def _kill(self):
        p = self.popen
        if p is not None and p.poll() is None:
            try:
                p.kill()          # TerminateProcess on Windows
            except OSError:
                pass

    def _ended(self, gen, reason):
        with self._lock:
            if gen != self._live_gen or self._ended_gen >= gen:
                return                     # not started yet (spawn handles it), or already handled
            self._ended_gen = gen
            links = list(self._links.values())
            self._links.clear()
            stopping = self._stopping
        if self.channel is not None:
            self.channel.close(reason)
        for relay, deliver in links:
            relay.detach(deliver)
        for inst in self.owned():
            inst.local_port = None
        if stopping:
            self.state = "off"
            return
        now = time.monotonic()
        self.ends.append(now)
        while self.ends and now - self.ends[0] > FAIL_WINDOW_S:
            self.ends.popleft()
        self.why, self._pending_why = self._pending_why or reason, ""
        self.last_lines = self.sup.tail(self.name)
        log.error("driver process %s %s (%s)%s", self.name, reason, self.why,
                  "".join("\n    " + ln for ln in self.last_lines[-10:]))
        if len(self.ends) >= FAILS:
            self.state = "failed"
            log.error("%s: ended %d times in %d minutes: failed, not started again until 'Try again'",
                      self.name, len(self.ends), FAIL_WINDOW_S // 60)
            return
        self.state = "restarting"
        delay = self._backoff()
        threading.Thread(target=self._restart, args=(delay, gen), name=f"restart-{self.name}",
                         daemon=True).start()

    def _restart(self, delay, gen):
        time.sleep(delay)
        if self._stopping or self.state != "restarting":
            return
        try:
            descs = self.spawn()
        except Exception as e:
            log.error("%s: starting again failed: %s", self.name, e)
            self.ends.append(time.monotonic())
            self.why = f"could not be started again: {e}"
            self.last_lines = self.sup.tail(self.name)
            if len(self.ends) >= FAILS:
                self.state = "failed"
                return
            threading.Thread(target=self._restart, args=(self._backoff(), gen),
                             name=f"restart-{self.name}", daemon=True).start()
            return
        self._apply(descs)
        self.state = "running"
        self.restarts.append(time.monotonic())
        log.info("driver process %s started again (pid %d)", self.name, self.pid)
        self._bring_back()
        self.sup.drivers_changed()

    def _backoff(self):
        return min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2.0 ** (len(self.ends) - 1))

    def retry(self):
        """Overview's 'Try again' for a failed driver."""
        if self.state != "failed":
            return False
        self.ends.clear()
        self.why = ""
        self.state = "restarting"
        threading.Thread(target=self._restart, args=(0.0, self.gen), name=f"restart-{self.name}",
                         daemon=True).start()
        return True

    def stop(self, why="hub stopping"):
        self._stopping = True
        self._end_planned(why)
        self.state = "off"

    def info(self):
        now = time.monotonic()
        e = self.entry
        return {"name": self.name, "module": self.module, "drivers": sorted(self.drivers), "state": self.state,
                "why": self.why, "pid": self.pid if self.running else None, "version": e.version,
                "source": e.source, "api": e.api, "title": e.title,
                "update": self.pending.version if self.pending is not None else None,
                "devices": [i.slug for i in self.owned() if i.slug],
                "restarts_1h": sum(1 for t in self.restarts if now - t < 3600),
                "last_lines": self.last_lines[-20:] if self.state in ("failed", "restarting") else []}


class Supervisor:
    """All driver processes, kept in step with the catalog (hub/catalog.py): sync() starts a newly installed
    driver, switches to a newer version (at once if no client uses its devices, else when they disconnect or on
    "Switch now"), and stops one that was uninstalled or turned off. bind(hub) once the hub exists (the hub needs
    the drivers first); start_watching() then syncs every WATCH_S."""

    def __init__(self, modules=None, config_dir=None, simulated=False, primary=None, use_standalone_settings=True,
                 command=None, extra_path=None, verbose=False, catalog=None, off=()):
        from .catalog import Catalog
        self.config_dir = config_dir
        self.simulated = simulated
        self.primary = dict(primary or {})
        self.use_standalone_settings = use_standalone_settings
        self.command = command or ([sys.executable] if getattr(sys, "frozen", False)
                                   else [sys.executable, "-m", "hub"])
        self.verbose = verbose
        self.catalog = catalog or Catalog(installed="", source="", builtin=False, modules=modules,
                                          module_path=extra_path)
        self.off = set(off)
        self.hub = None
        self.procs = []
        self.listing = {}                  # key -> the chosen catalog Entry (refused ones too)
        self._sync_lock = threading.RLock()
        self._watching = threading.Event()
        self.sync()

    def drivers(self):
        return [d for p in list(self.procs) if p.state != "off" for d in p.drivers.values()]

    def bind(self, hub):
        self.hub = hub

    def drivers_changed(self):
        if self.hub is not None:
            self.hub.set_drivers(self.drivers())

    # ---------------------------------------------------------------- installs, updates, uninstalls
    def sync(self):
        """Looks at the catalog once and brings the processes in line. True if the set of drivers changed."""
        with self._sync_lock:
            chosen, _ = self.catalog.scan()
            self.listing = chosen
            changed, gone = False, []
            for key, e in sorted(chosen.items()):
                proc = self.find(key)
                if e.refused or key in self.off:
                    if proc is not None:
                        log.info("%s: %s", key, f"refused ({e.refused})" if e.refused else "turned off")
                        gone.append(proc)
                    continue
                if proc is None:
                    proc = DriverProcess(self, e)
                    try:
                        proc.first_start()
                    except Exception as ex:
                        log.error("driver %s %s could not be started: %s", key, e.version, ex)
                        proc.state, proc.why = "failed", str(ex)
                        proc.last_lines = self.tail(key)
                    self.procs.append(proc)
                    changed = True
                elif not proc.entry.same(e):
                    if proc.state == "failed" or not self.in_use(proc):
                        proc.switch(e)
                        changed = True
                    elif proc.pending is None or not proc.pending.same(e):
                        proc.pending = e
                        log.info("%s: version %s ready; waiting for its clients to disconnect (or Switch now)",
                                 key, e.version)
                else:
                    proc.pending = None
            for proc in list(self.procs):
                if proc.name not in chosen and proc not in gone:
                    log.info("%s was uninstalled", proc.name)
                    gone.append(proc)
            for proc in gone:
                self.procs.remove(proc)
                changed = True
            if changed:
                self.drivers_changed()            # the hub drops the devices of drivers that went
            for proc in gone:
                proc.stop("uninstalled or turned off")
            if gone and self.hub is not None:
                self.hub.rescan()                 # their ports are free: look at them again
            for proc in self.procs:
                if proc.state == "running" and proc.pending is None and proc.entry.source == "installed":
                    self.catalog.clean_up(proc.name, proc.entry)
            return changed

    def in_use(self, proc):
        return self.hub is not None and self.hub.in_use([i.slug for i in proc.owned() if i.slug])

    def switch_now(self, name):
        """Overview's "Switch now": the waiting update even though a client is connected."""
        with self._sync_lock:
            p = self.find(name)
            if p is None or p.pending is None:
                return False
            ok = p.switch(p.pending)
            self.drivers_changed()
            return ok

    def set_enabled(self, name, on):
        """Overview's on/off switch (saved in the hub's settings)."""
        with self._sync_lock:
            if on:
                self.off.discard(name)
            else:
                self.off.add(name)
            if self.hub is not None:
                self.hub.settings.drivers_off = sorted(self.off)
                self.hub._save()
            self.sync()
            return True

    def start_watching(self):
        if self._watching.is_set():
            return
        self._watching.set()
        threading.Thread(target=self._watch_catalog, name="driver-catalog", daemon=True).start()

    def _watch_catalog(self):
        while self._watching.is_set():
            time.sleep(WATCH_S)
            if not self._watching.is_set():
                return
            try:
                self.sync()
            except Exception:
                log.exception("looking for driver changes failed")

    def public_urls(self):
        return self.hub.public_urls() if self.hub is not None else []

    def hello_info(self):
        return {"config_dir": self.config_dir, "simulated": self.simulated, "primary": self.primary,
                "use_standalone_settings": self.use_standalone_settings, "public_urls": self.public_urls()}

    def open_for(self, proc, chan, device, baud):
        """A link in proc opens device: join it to the hub's relay for that port."""
        allowed = proc.allowed(device)
        if allowed is None:
            raise PermissionError(f"{proc.name} has no device on {device}")
        port, relay = allowed
        hub = self.hub
        if relay is None:
            relay = hub.relay_for(port, baud)
        lines = hub._lines(port)

        def deliver(msg, ch=proc.channel):
            try:
                ch.send(["r", chan, msg])
            except Exception:
                pass
        proc.link(chan, relay, deliver)
        relay.attach(deliver)
        return {"lines": lines, "baud": relay.baud}

    def tail(self, name, n=30):
        out = []
        for f in (f"{name}.log", f"{name}.stderr.log"):
            try:
                with open(os.path.join(self.config_dir, "logs", f), "rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 8000))
                    out += fh.read().decode("utf-8", "replace").splitlines()[-n:]
            except OSError:
                pass
        return out[-n:]

    def find(self, name):
        return next((p for p in self.procs if p.name == name), None)

    def info(self):
        """Overview's Drivers list: every driver found, running or not."""
        out = []
        for key, e in sorted(self.listing.items()):
            p = self.find(key)
            if p is not None:
                d = p.info()
            else:
                d = {"name": key, "module": e.module, "drivers": [], "pid": None, "version": e.version,
                     "source": e.source, "api": e.api, "title": e.title, "update": None, "devices": [],
                     "restarts_1h": 0, "last_lines": [],
                     "state": "refused" if e.refused else "off" if key in self.off else "stopped",
                     "why": e.refused}
            d["enabled"] = key not in self.off
            out.append(d)
        return out

    def stop(self):
        self._watching.clear()
        for p in list(self.procs):
            p.stop()

    # ---------------------------------------------------------------- simulators (--simulate)
    def simulated_ports(self, spec):
        """'nitecrawler,box' -> (port lister, opener) for simulators that run in their drivers' processes."""
        by_name = {}
        for p in self.procs:
            for d in p.drivers.values():
                for n in d.simulators:
                    by_name.setdefault(n, p)
        ports, usb, where = [], [], {}
        for i, kind in enumerate(k.strip() for k in spec.split(",") if k.strip()):
            if kind not in by_name:
                raise SystemExit(f"--simulate: unknown device {kind!r} (known: {', '.join(sorted(by_name))})")
            dev = f"SIM{i + 1}"
            proc = by_name[kind]
            vid, pid, serial, desc = proc.call("sim_info", dev, kind, i, timeout=T_START)
            if any(kind in d.simulators and d.transport == "usb" for d in proc.drivers.values()):
                usb.append(UsbDevice(vid, pid, dev, desc, sim_name=kind, sim_index=i))   # an SDK device, no port
                continue
            ports.append(PortInfo(dev, vid, pid, serial, desc))
            where[dev] = (proc, kind, i)

        def opener(device, baud, lines=False):
            if device not in where:
                raise OSError(f"{device} not found")
            proc, kind, i = where[device]
            if not proc.running:
                raise OSError(f"{device}: driver process {proc.name} is not running")
            return RemoteSimPort(proc, device, kind, i, baud)

        def lister():
            return list(ports)
        lister.usb = lambda: list(usb)            # the hub's USB device list (Hub: usb_lister)
        return lister, opener
