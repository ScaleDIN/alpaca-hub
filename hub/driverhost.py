"""The driver side of a driver process (REFACTOR.md step 5): python -m hub --driver <module> --pipe <name>.

Loads one driver module (its DRIVERS), connects to the hub over the pipe, and answers the hub's calls:
  hello(info)                          -> {"drivers": [what each Driver declares]}
  recognise(key, data)                 -> identity or None
  probe(key, device, baud)             -> identity or None (the port is a RelaySerial through the hub)
  start(key, port, ident, number)      -> {"slug", "title", "local_port"}
  stop(slug), ping(public_urls)        -> ping: {slug: [up, summary]}
  sim_info(name, index), sim_open(device, name, index, baud)   simulators live here, not in the hub
It calls the hub for one thing: open(chan, device, baud), when a link opens its port.

The hub's ports never live here: a link gets a RelaySerial, and the hub keeps the real port open even when this
process ends. When the hub goes away (the pipe closes), this process ends at once.
"""
import importlib
import itertools
import logging
import logging.handlers
import os
import sys
import threading
import time
from multiprocessing.connection import Client

from .api import HubContext
from .channel import Channel, ChannelClosed
from .core.net import own_addresses, plain_ip
from .ports import PortInfo
from .relay import RelayError, RelaySerial

log = logging.getLogger("hub.driverhost")

AUTH_ENV = "ALPACA_HUB_DRIVER_KEY"


def describe(d):
    """What the hub needs to know about a Driver, as plain values."""
    return {"key": d.key, "title": d.title, "api": d.api, "transport": d.transport,
            "usb_ids": sorted([int(v), int(p)] for v, p in d.usb_ids),
            "listen_baud": d.listen_baud, "probe_baud": d.probe_baud, "probe_priority": d.probe_priority,
            "never_probe": sorted([int(v), int(p)] for v, p in d.never_probe), "lines": bool(d.lines),
            "simulators": sorted(d.simulators)}


class _Settings:
    def __init__(self):
        self.primary = {}


class _PipeLink:
    """Joins a RelaySerial to the hub's PortRelay for one port, over the pipe."""

    def __init__(self, host, device, baud):
        self.host, self.device, self.want = host, device, baud
        self.chan = None
        self.lines = None
        self.baud = None

    def connect(self, deliver):
        self.chan = next(self.host.chan_ids)
        self.host.ends[self.chan] = deliver
        try:
            r = self.host.channel.call("open", self.chan, self.device, self.want, timeout=30)
        except Exception as e:
            self.host.ends.pop(self.chan, None)
            raise RelayError(f"cannot open {self.device}: {e}") from None
        self.lines, self.baud = bool(r["lines"]), int(r["baud"])

    def send(self, msg):
        try:
            self.host.channel.send(["r", self.chan, msg])
        finally:
            if msg[0] == "detach":
                self.host.ends.pop(self.chan, None)


class DriverHost:
    def __init__(self, module):
        self.module = module
        mod = importlib.import_module(module)
        self.drivers = {}
        for cls in getattr(mod, "DRIVERS", ()):
            d = cls()
            self.drivers[d.key] = d
        if not self.drivers:
            raise SystemExit(f"{module} exports no DRIVERS")
        self.ctx = {}
        self.instances = {}                 # slug -> Instance
        self.ends = {}                      # relay channel -> RelaySerial._deliver
        self.chan_ids = itertools.count(1)
        self.sims = {}                      # device -> (name, index, simulator)
        self.sim_ports = {}                 # device -> [open simulator port, stop event]
        self.channel = None
        # what HubContext reads from "the hub"
        self.simulated = False
        self.config_dir = ""
        self.settings = _Settings()
        self.use_standalone_settings = True
        self._public = []
        self._own = (-1e9, set())
        self._done = threading.Event()

    # ---------------------------------------------------------------- the hub, as HubContext sees it
    def public_urls(self):
        return list(self._public)

    def is_local(self, ip):
        ip = plain_ip(ip)
        t, a = self._own
        if time.monotonic() - t > 30:
            a = own_addresses()
            self._own = (time.monotonic(), a)
        return ip.startswith("127.") or ip == "::1" or ip in a

    def opener_for(self, port, baud):
        def opener(name):
            ser = RelaySerial(_PipeLink(self, name, baud), name, baud)
            if ser.baudrate != baud:
                ser.baudrate = baud
            return ser
        return opener

    def free_number(self, key):
        log.warning("%s asked for its unit number outside start; 0 given", key)
        return 0

    # ---------------------------------------------------------------- the channel
    def run(self, conn):
        self.channel = Channel(conn, f"hub<-{self.module}", on_call=self.on_call, on_frame=self.on_frame,
                               on_close=self._closed)
        self._done.wait()
        os._exit(0)

    def _closed(self, reason, bad):
        log.info("the hub went away (%s): ending", reason)
        self._done.set()

    def on_call(self, method, args):
        f = getattr(self, "c_" + method, None)
        if f is None:
            raise ValueError(f"unknown call {method}")
        return f(*args)

    def on_frame(self, msg):
        kind = msg[0]
        if kind == "r" and len(msg) == 3:
            deliver = self.ends.get(msg[1])
            if deliver is not None:
                deliver(msg[2])
        elif kind == "sw" and len(msg) == 3:
            p = self.sim_ports.get(msg[1])
            if p is not None:
                p[0].write(msg[2])
        elif kind == "sc" and len(msg) == 2:
            self._sim_close(msg[1])

    # ---------------------------------------------------------------- calls from the hub
    def c_hello(self, info):
        self.simulated = bool(info.get("simulated"))
        self.config_dir = info["config_dir"]
        self.settings.primary = dict(info.get("primary") or {})
        self.use_standalone_settings = bool(info.get("use_standalone_settings", True))
        self._public = list(info.get("public_urls") or [])
        for d in self.drivers.values():
            self.ctx[d.key] = HubContext(self, d)
        return {"pid": os.getpid(), "drivers": [describe(d) for d in self.drivers.values()]}

    def c_ping(self, public_urls=None):
        if public_urls is not None:
            self._public = list(public_urls)
        out = {}
        for slug, inst in list(self.instances.items()):
            try:
                out[slug] = [bool(inst.up()), inst.summary()]
            except Exception as e:
                out[slug] = [False, {"text": f"status failed: {e}", "state": "bad"}]
        return out

    def c_recognise(self, key, data):
        return self.drivers[key].recognise(data)

    def c_probe(self, key, device, baud):
        ser = RelaySerial(_PipeLink(self, device, baud), device, baud)
        try:
            return self.drivers[key].probe(ser)
        finally:
            ser.close()

    def c_start(self, key, port, ident, number):
        d = self.drivers[key]
        ctx = self.ctx[key]
        info = PortInfo(port["device"], port["vid"], port["pid"], port["serial"], port["description"])
        ctx._number = number
        try:
            inst = d.start(ctx, info, ident)
        finally:
            ctx._number = None
        inst.number = number
        self.instances[inst.slug] = inst
        log.info("started %s (%s) on %s", inst.slug, inst.title, info.device)
        return {"slug": inst.slug, "title": inst.title, "local_port": inst.local_port}

    def c_stop(self, slug):
        inst = self.instances.pop(slug, None)
        if inst is not None:
            inst.stop()
        return True

    # ---------------------------------------------------------------- simulators
    def _sim(self, device, name, index):
        s = self.sims.get(device)
        if s is None:
            for d in self.drivers.values():
                if name in d.simulators:
                    sim, usb, serial, desc = d.simulators[name](index)
                    s = self.sims[device] = (name, index, sim, (usb, serial, desc))
                    break
            else:
                raise ValueError(f"no simulator {name!r}")
        return s

    def c_sim_info(self, device, name, index):
        _, _, _, (usb, serial, desc) = self._sim(device, name, index)
        return [int(usb[0]), int(usb[1]), serial, desc]

    def c_sim_open(self, device, name, index, baud):
        self._sim_close(device)
        sim = self._sim(device, name, index)[2]
        try:
            port = sim.opener(device, baud)
        except TypeError:
            port = sim.opener(device)
        stop = threading.Event()
        self.sim_ports[device] = [port, stop]
        threading.Thread(target=self._sim_pump, args=(device, port, stop), name=f"sim-{device}",
                         daemon=True).start()
        return True

    def _sim_pump(self, device, port, stop):
        pyserial = hasattr(type(port), "in_waiting")
        read = getattr(port, "read", None)
        while not stop.is_set():
            try:
                if pyserial:
                    n = port.in_waiting
                    data = port.read(n) if n else b""
                    if not n:
                        time.sleep(0.005)
                else:
                    data = (read(256) if read else port.readline()) or b""
                if data:
                    self.channel.send(["sd", device, bytes(data)])
            except ChannelClosed:
                return
            except Exception as e:
                log.warning("simulator %s: %s", device, e)
                time.sleep(0.2)

    def _sim_close(self, device):
        p = self.sim_ports.pop(device, None)
        if p is not None:
            p[1].set()
            try:
                p[0].close()
            except Exception:
                pass


def main(module, pipe, config_dir, extra_path=None, log_name=None):
    # No __pycache__ in an installed driver's folder: its uninstaller removes exactly what its installer put there,
    # and an update's new folder holds only its own files.
    sys.dont_write_bytecode = True
    if extra_path:
        sys.path.insert(0, extra_path)
    logs = os.path.join(config_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    short = log_name or module.rsplit(".", 1)[-1]       # the driver's key (a folder driver's module is <key>.driver)
    handler = logging.handlers.RotatingFileHandler(os.path.join(logs, f"{short}.log"), maxBytes=1_000_000,
                                                   backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if os.environ.get("ALPACA_HUB_VERBOSE") else logging.INFO,
                        handlers=[handler], format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    key = bytes.fromhex(os.environ.pop(AUTH_ENV))
    family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
    conn = Client(pipe, family=family, authkey=key)
    log.info("driver process %s for %s (pid %d)", module, pipe, os.getpid())
    try:
        host = DriverHost(module)
    except BaseException:
        log.exception("loading %s failed", module)
        raise
    host.run(conn)
