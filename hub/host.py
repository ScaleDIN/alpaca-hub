"""The hub: finds devices on the serial ports, runs a driver instance for each, and serves them all at one address.

* One public HTTP address and one Alpaca discovery responder for the whole PC.
* Each instance runs its own Alpaca server on a private 127.0.0.1 port; the hub forwards to it.
* Public Alpaca device numbers are the hub's: Focuser #0, #1, #2 … across all brands. Each device keeps its number
  (saved by UniqueID), so NINA profiles stay valid when another device is missing on a given night.
* The status page has a tab per device that is present (/<slug>/ is that device's own page).
"""
import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from .core.net import DISCOVERY_PORT, DiscoveryDedupe
from .core.net import make_http_server as _make_http_server
from .core.net import own_addresses as _own_addresses
from .core.net import plain_ip as _plain_ip
from .core.net import route_address as _route_address
from .core.page import Page

from . import NAME, VERSION
from .api import API_VERSION, HubContext
from .ports import PortInfo, list_ports, open_port
from .recorder import Recorder, clamp_days, history_unit
from .relay import LocalLink, PortRelay, RelaySerial
from .shell import shell_page
from .updates import Updates

log = logging.getLogger("hub")

NOT_CONNECTED = 0x407  # ASCOM error: the device isn't connected (its driver process is restarting)
CLIENT_IDLE_S = 60.0   # a connected client that made no call for this long no longer holds a driver update back
RECONNECT_S = 30.0     # after a driver restart, how long the hub tries to connect a client's devices again
SCAN_S = 15.0        # look for new devices this often
LISTEN_S = 2.5       # listen this long on a port before anything is sent to it
RETRY_S = 60.0       # a port nobody recognised is looked at again after this long
GONE_S = 60.0        # a device whose port has been missing this long is dropped (page and Alpaca)
TABLE_S = 2.0        # how long the merged device table is cached
IDENTIFY_EXTRA_S = 10.0  # a port's identification may take this long beyond its listening before it counts as stuck
_STUCK = object()


def _close(ser):
    try:
        ser.close()
    except Exception:
        pass
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection",
       "content-length", "date", "server"}


def default_config_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "alpaca-hub")


class HubSettings:
    # "primary" is from before hub API 1 (the first unit of each driver); only read now, to find old settings files
    # "api": the hub API the saved numbers belong to (0: before hub API 1, when UniqueIDs were made differently)
    # "drivers_off": drivers turned off on Overview's Drivers list (they aren't started)
    FIELDS = {"http_port": 11111, "bind": "0.0.0.0", "numbers": {}, "primary": {}, "location": "", "api": 0,
              "drivers_off": [], "ports_ignored": [], "history_days": 7, "update_check": True}
    # "ports_ignored": ports ignored on Overview, as {"device", "vid", "pid", "serial", "description"}: one with a USB
    # serial number is matched by it (whatever its COM number), else by its COM port
    # "history_days": how long the day files the hub writes for each device are kept (hub/recorder.py)
    # "update_check": the Overview's "Check for updates online" (hub/updates.py); off, the hub sends nothing

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        for k, v in self.FIELDS.items():
            setattr(self, k, json.loads(json.dumps(v)))

    @classmethod
    def load(cls, path):
        s = cls(path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for k in cls.FIELDS:
                if k in d:
                    setattr(s, k, d[k])
        except (OSError, ValueError):
            pass
        return s

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: getattr(self, k) for k in self.FIELDS}, f, indent=2)
            os.replace(tmp, self.path)


class Hub:
    def __init__(self, drivers, config_dir=None, bind=None, http_port=None, port_lister=list_ports,
                 opener=open_port, simulated=False, use_standalone_settings=True, scan_s=SCAN_S, listen_s=LISTEN_S, identify_s=None,
                 supervisor=None, on_shutdown=None, running_file=None, update_feed=None):
        self.on_shutdown = on_shutdown        # POST /hub/shutdown from this PC (hub --stop) calls it
        self.running_file = running_file      # where the hub says it runs (port, pid) for driver installers
        self.supervisor = supervisor          # hub/supervisor.py: the driver processes (None: drivers run in here)
        if supervisor is not None:
            supervisor.bind(self)
        self._banned = {}                     # key -> why: refused while running (it wrote to a never_probe port)
        self._clients = {}                    # slug -> {"conn": {(type, number)}, "seen": time}: Alpaca clients
        self.drivers, self.refused = self._accept(drivers)
        self._never = self._never_map(self.drivers)   # (vid, pid) -> key of the driver listing it in never_probe
        self._ctx = {d.key: HubContext(self, d) for d in self.drivers}
        self.config_dir = config_dir or default_config_dir()
        self.settings = HubSettings.load(self.config_file("hub.json"))
        if self.settings.api < 1:
            # Hub API 1 changed every UniqueID. The numbers saved for the old ones would keep #0, #1 … taken forever,
            # so they're cleared once and the devices are numbered afresh (no NINA profile used them yet, 2026-11-22).
            if self.settings.numbers:
                log.info("hub API 1: new UniqueIDs, so the %d saved Alpaca numbers start again",
                         len(self.settings.numbers))
            self.settings.numbers = {}
        self.settings.api = API_VERSION
        if bind is not None:
            self.settings.bind = bind
        if http_port is not None:
            self.settings.http_port = http_port
        self.port_lister = port_lister
        self.opener = opener                  # opener(device, baud) -> open serial port
        self.simulated = simulated
        self.use_standalone_settings = use_standalone_settings   # where the old hub kept the first unit's settings
        self.scan_s, self.listen_s = scan_s, listen_s
        self.instances = {}                   # slug -> Instance
        self._lock = threading.RLock()
        self._rlock = threading.RLock()       # _relays, refused (never held while waiting for a driver)
        self._rejected = {}                 # device -> time a scan found nothing there
        self._missing = {}                    # slug -> time its port was first seen missing
        self._relays = {}                     # device -> PortRelay: each port a device was recognised on, held open
        self._stuck = {}                      # device -> (identify thread still inside the driver, its result box)
        self.ports_seen = {}                  # device -> PortInfo, from the last scan
        self.identify_s = identify_s if identify_s is not None else 2 * listen_s + IDENTIFY_EXTRA_S
        self._table = (0.0, [])
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.recorder = Recorder(self.config_dir, self._instance_list, days=lambda: self.settings.history_days,
                                 set_days=self._history_days_from_device)
        self.show_folder = _show_folder       # the Overview's Open folder (a stand-in in the tests)
        # The update check: a simulated hub only checks when it's given a feed address (the tests, a trial run).
        self.updates = Updates(self.config_dir, url=update_feed, installed=self._installed_drivers,
                               enabled=lambda: bool(self.settings.update_check)
                               and (not simulated or update_feed is not None))
        self.scanning = False                 # a scan pass is running (the Overview's "Scan now" shows it)
        self.scan_started = 0.0               # wall-clock time the current (or last) pass started
        self.last_scan = 0.0                  # wall-clock time the last pass finished
        self.httpd = None
        self._udp = None
        self._dedupe = DiscoveryDedupe()
        self._own = (-1e9, set())
        self.port = None
        self.started = time.time()
        self._txn = 0

    @staticmethod
    def _never_map(drivers):
        never = {}
        for d in drivers:
            for usb in d.never_probe:
                never.setdefault(usb, d.key)
        return never

    def set_drivers(self, drivers):
        """The drivers changed while running (installed, updated, uninstalled, turned on or off). Devices whose
        driver is no longer in the list are stopped and their ports released and looked at again."""
        drivers = list(drivers)
        keep = {id(d) for d in drivers}
        with self._lock:
            for slug, inst in list(self.instances.items()):
                if id(inst.driver) not in keep:
                    log.info("%s: its driver %s is gone; dropping it", slug, inst.driver.key)
                    dev = inst.port.device
                    self._remove(slug)
                    self._rejected.pop(dev, None)
            ok, refused = self._accept([d for d in drivers if d.key not in self._banned])
            refused.update(self._banned)
            with self._rlock:
                self.drivers, self.refused, self._never = ok, refused, self._never_map(ok)
            for d in ok:
                self._ctx.setdefault(d.key, HubContext(self, d))
            self._table = (0.0, [])
            # A driver that came (installed, turned on, updated) may know a port nobody recognised before: the
            # next scan looks at those again instead of waiting RETRY_S.
            self._rejected.clear()

    # ---------------------------------------------------------------- Alpaca clients (for driver updates)
    def note_client(self, slug, typ, number, method, command, body):
        """Follows Connected per device from the Alpaca calls the hub forwards."""
        now = time.monotonic()
        with self._rlock:
            st = self._clients.setdefault(slug, {"conn": set(), "seen": 0.0})
            st["seen"] = now
            if command != "PUT":
                return
            # as the device reads it: PUT names are case sensitive (a wrongly cased Connected is refused there)
            q = {k.lower(): v[-1] for k, v in urllib.parse.parse_qs((body or b"").decode("utf-8", "replace")).items()
                 if k == "Connected"}
            if method == "connected" and "connected" in q:
                if q["connected"].strip().lower() == "true":
                    st["conn"].add((typ, number))
                else:
                    st["conn"].discard((typ, number))
            elif method == "connect":
                st["conn"].add((typ, number))
            elif method == "disconnect":
                st["conn"].discard((typ, number))

    def restore_clients(self, inst):
        """inst came back in a new driver process (a restart or an update). Its Alpaca server starts disconnected,
        so each device a client had connected is connected again there: the client carries on (NINA doesn't
        reconnect by itself, and would stop on "not connected"). Retried until the device answers, for up to
        RECONNECT_S."""
        with self._rlock:
            st = self._clients.get(inst.slug)
            conn = set(st["conn"]) if st else set()
        if conn:
            threading.Thread(target=self._reconnect, args=(inst, conn), name=f"reconnect-{inst.slug}",
                             daemon=True).start()

    def _reconnect(self, inst, conn):
        rows = list(getattr(inst, "_alpaca_rows", []))
        nums = self.settings.numbers
        pending = {}
        for typ, number in conn:
            row = next((r for r in rows if r["type"] == typ and nums.get(r["uid"], [None, None])[1] == number), None)
            if row is not None:
                pending[(typ, number)] = row["internal"]
        end = time.monotonic() + RECONNECT_S
        while pending and time.monotonic() < end and not self._stop.is_set():
            for (typ, number), internal in list(pending.items()):
                port = inst.local_port
                if not port:
                    break
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    c.request("PUT", f"/api/v1/{typ}/{internal}/connected",
                              body=urllib.parse.urlencode({"Connected": "True", "ClientID": "0",
                                                           "ClientTransactionID": "0"}),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
                    r = json.loads(c.getresponse().read().decode("utf-8"))
                    if r.get("ErrorNumber") == 0:
                        del pending[(typ, number)]
                        log.info("%s: %s #%d connected again for its client", inst.slug, typ, number)
                except Exception as e:
                    log.debug("%s: reconnecting %s #%d: %s", inst.slug, typ, number, e)
                finally:
                    c.close()
            if pending:
                time.sleep(1.0)
        for typ, number in pending:
            log.warning("%s: %s #%d could not be connected again; its client must reconnect", inst.slug, typ, number)

    def in_use(self, slugs):
        """A client is connected to one of these devices (and called it in the last CLIENT_IDLE_S: a client that
        went away without disconnecting doesn't hold an update back for ever)."""
        now = time.monotonic()
        with self._rlock:
            return any(st["conn"] and now - st["seen"] < CLIENT_IDLE_S
                       for st in (self._clients.get(s) for s in slugs) if st)

    @staticmethod
    def _accept(drivers):
        """(drivers the hub runs, in probe order; {key: why} for those it refuses).

        Refused: a driver for another hub API version, and any driver that would probe USB IDs another driver lists
        in never_probe (a device that acts on any byte, like the WandererBox). Probes run lowest probe_priority
        first; the sort is stable, so equal priorities keep the order they were found in."""
        drivers = list(drivers)
        never = {}
        for d in drivers:
            for usb in d.never_probe:
                never.setdefault(usb, d.key)
        ok, refused = [], {}
        for d in drivers:
            if getattr(d, "api", None) != API_VERSION:
                refused[d.key] = f"written for hub API {getattr(d, 'api', None)}, this hub has {API_VERSION}"
            elif d.probe_baud and d.usb_ids & set(never):
                usb = sorted(d.usb_ids & set(never))[0]
                refused[d.key] = (f"it would probe {usb[0]:04X}:{usb[1]:04X}, which {never[usb]} lists as never "
                                  f"to be probed")
            else:
                ok.append(d)
                continue
            log.error("driver %s refused: %s", d.key, refused[d.key])
        ok.sort(key=lambda d: d.probe_priority if d.probe_baud else 0)
        return ok, refused

    # ---------------------------------------------------------------- helpers for drivers
    def config_file(self, name):
        return os.path.join(self.config_dir, name)

    def opener_for(self, port, baud):
        """The opener a driver's link uses: each call gives a RelaySerial on the port the hub recognised the device
        on (hub/relay.py). The hub holds that port open from detection until the device is dropped; a link's close
        and reopen only detach and attach again. Each port is opened once and kept open, as Wanderer Empire does.

        Why (2026-11-20): with USB selective suspend on (this laptop: hub suspend timeout 50 ms), every close lets the
        WandererBox's internal USB hub fall asleep once nothing behind it is open, and the next open must wake it.
        Listen, close, probe, close, reopen for the link: those cycles left SetCommState stuck for minutes or for good,
        on CH340 and FTDI ports alike. A port held open keeps the hub awake."""
        lines = self._lines(port)

        def opener(name):
            relay = self.relay_for(PortInfo(name, *port.usb, port.serial, port.description), baud)
            ser = RelaySerial(LocalLink(relay), name, relay.baud, lines=lines)
            if relay.baud != baud:
                ser.baudrate = baud
            return ser
        return opener

    def relay_for(self, port, baud):
        """The hub's relay for port, opened if it isn't held (it was released, or never recognised). Only the
        relays' own lock is taken (a driver process opens its port while the scan holds the hub's lock), and the
        open runs outside any lock: it can hang in Windows' serial driver for minutes (seen with a CH340)."""
        name = port.device
        with self._rlock:
            relay = self._relays.get(name)
        if relay is None or relay.closed:
            lines = self._lines(port)
            fresh = self._new_relay(name, self._open(name, baud, lines), baud, lines)
            with self._rlock:
                relay = self._relays.get(name)
                if relay is None or relay.closed:
                    relay = self._relays[name] = fresh
                    fresh = None
            if fresh is not None:
                fresh.close()
        return relay

    def _new_relay(self, device, ser, baud, lines, refuse=None):
        return PortRelay(ser, device, baud, reopen=lambda: self._open(device, baud, lines), refuse=refuse)

    def _lines(self, port):
        """DTR/RTS on for this port? Yes if any driver that may own its USB IDs asks (Driver.lines), so the device
        sees the same level from the first listen on, and a reopen never changes it."""
        return any(getattr(d, "lines", False) for d in self.drivers if port.usb in d.usb_ids)

    def _open(self, device, baud, lines):
        # Openers take lines= only when it's on, so simple openers (opener(device, baud)) keep working.
        return self.opener(device, baud, lines=True) if lines else self.opener(device, baud)

    def _hold(self, device, relay):
        with self._rlock:
            old = self._relays.pop(device, None)
            self._relays[device] = relay
        if old is not None and old is not relay:
            old.close()

    def _release(self, device):
        with self._rlock:
            relay = self._relays.pop(device, None)
        if relay is not None:
            relay.close()

    def _refuse_probe(self, driver, port):
        """For a probe on a port whose USB IDs another driver lists in never_probe: the relay passes nothing on, and
        a driver that writes anyway is refused from then on."""
        def refuse(device, data):
            usb = port.usb
            why = (f"it wrote to {device} ({usb[0]:04X}:{usb[1]:04X}), which {self._never.get(usb)} lists as never "
                   f"to be probed; nothing was sent")
            with self._rlock:                     # not the hub's lock: the scan holds it while the probe runs
                if driver.key not in self.refused:
                    log.error("driver %s refused: %s", driver.key, why)
                    self.refused[driver.key] = why
                self._banned[driver.key] = why
                self.drivers = [d for d in self.drivers if d is not driver]
        return refuse

    def free_number(self, key):
        """The lowest unit number no running instance of this driver has (0 for the first). Numbers are handed out
        by the hub, not counted: with #1 and #2 running and #1 unplugged, the next one is #1 again, not a second #2
        that would take #2's tab."""
        with self._lock:
            used = {getattr(i, "number", None) for i in self.instances.values() if i.driver.key == key}
        n = 0
        while n in used:
            n += 1
        return n

    def own_addresses(self):
        t, a = self._own
        if time.monotonic() - t > 30:
            a = _own_addresses()
            self._own = (time.monotonic(), a)
        return a

    def is_local(self, ip):
        ip = _plain_ip(ip)
        return ip.startswith("127.") or ip == "::1" or ip in self.own_addresses()

    def public_urls(self):
        bind = self.settings.bind
        if bind.startswith("127.") or bind in ("localhost", "::1"):
            return []
        if bind not in ("0.0.0.0", ""):
            addrs = [bind]
        else:
            first = _route_address()
            addrs = ([first] if first else []) + sorted(a for a in self.own_addresses() if a != first)
        return [f"http://{a}:{self.port}/" for a in addrs]

    def _save(self):
        try:
            self.settings.save()
        except OSError as e:
            log.warning("cannot save hub settings: %s", e)

    # ---------------------------------------------------------------- finding devices
    def _listen(self, port, baud, seconds):
        """(bytes the device sent on its own, the still-open port), or (None, None) if the port can't be opened.
        Reads only what has already arrived (in_waiting), so no read ever waits inside the driver for a timeout."""
        try:
            ser = self._open(port.device, baud, self._lines(port))
        except Exception as e:
            log.debug("%s: cannot open (%s)", port.device, e)
            return None, None
        data = b""
        try:
            end = time.monotonic() + seconds
            pyserial = hasattr(type(ser), "in_waiting")
            read = getattr(ser, "read", None)          # some simulators only have readline
            while time.monotonic() < end and not self._stop.is_set():
                if pyserial:
                    n = ser.in_waiting
                    if n:
                        data += ser.read(n)
                    else:
                        time.sleep(0.03)
                else:
                    data += (read(256) if read else ser.readline()) or b""
        except Exception as e:
            log.debug("%s: listening failed (%s)", port.device, e)
            _close(ser)
            return None, None
        return data, ser

    def identify(self, port):
        """(driver, identity) for the device on port, or None. Listens before anything is sent, and only asks
        when the port stayed silent and the driver claims the port's USB IDs. A recognised port is left open and
        held for the driver's link (opener_for), so it is never closed and reopened."""
        passive = [d for d in self.drivers if d.listen_baud and port.usb in d.usb_ids]
        # self.drivers is in probe order, and holds no driver that probes a never_probe port (_accept)
        active = [d for d in self.drivers if d.probe_baud and port.usb in d.usb_ids]
        if not passive and not active:
            return None
        heard = False
        silent = None                              # (baud, open port) of the last silent listen, reused to probe
        try:
            for baud in sorted({d.listen_baud for d in passive} | {d.probe_baud for d in active}):
                if silent is not None:
                    _close(silent[1])
                    silent = None
                data, ser = self._listen(port, baud, self.listen_s)
                if data is None:
                    return None
                if not data:
                    silent = (baud, ser)
                    continue
                heard = True
                for d in passive:
                    if d.listen_baud == baud:
                        ident = d.recognise(data)
                        if ident:
                            self._hold(port.device, self._new_relay(port.device, ser, baud, self._lines(port)))
                            return d, ident
                _close(ser)
            if heard:
                log.info("%s talks, but no driver knows it: nothing is sent to it", port.label())
                return None
            for d in active:
                if silent is not None and silent[0] == d.probe_baud:
                    ser, silent = silent[1], None
                else:
                    if silent is not None:
                        _close(silent[1])
                        silent = None
                    try:
                        ser = self._open(port.device, d.probe_baud, self._lines(port))
                    except Exception:
                        return None
                # A probe gets the port only through a relay. Where another driver lists the USB IDs in never_probe,
                # the relay passes nothing on (_accept already refuses such drivers: this is the second layer).
                guard = self._never.get(port.usb)
                refuse = self._refuse_probe(d, port) if guard is not None and guard != d.key else None
                lines = self._lines(port)
                relay = self._new_relay(port.device, ser, d.probe_baud, lines, refuse=refuse)
                if hasattr(d, "probe_relay"):         # a driver in its own process: it reaches the relay over its pipe
                    ident = d.probe_relay(relay, port)
                else:
                    end = RelaySerial(LocalLink(relay), port.device, d.probe_baud, lines=lines)
                    try:
                        ident = d.probe(end)
                    except Exception as e:
                        log.debug("%s: %s probe failed: %s", port.device, d.key, e)
                        ident = None
                    end.close()
                if ident and not relay.refused:
                    self._hold(port.device, relay)
                    return d, ident
                relay.close()
            return None
        finally:
            if silent is not None:
                _close(silent[1])

    def _identify_guarded(self, port):
        """identify() with a deadline. A port whose open/read never returns (a serial driver hang) is reported and
        left alone until it is unplugged, instead of stopping the scan for every other port."""
        box = {}

        def run():
            try:
                box["r"] = self.identify(port)
            except Exception:
                log.exception("%s: identifying failed", port.device)
                box["r"] = None

        t = threading.Thread(target=run, name=f"identify-{port.device}", daemon=True)
        t.start()
        t.join(self.identify_s)
        if t.is_alive():
            self._stuck[port.device] = (t, box)
            log.warning("%s: opening it has taken more than %d s inside Windows' serial driver. The hub tries it again "
                        "when the driver lets go (this has taken from 30 s to many minutes); if it never does, unplug "
                        "that device's USB cable and plug it back in. The other ports are still scanned.",
                        port.label(), self.identify_s)
            return _STUCK
        return box.get("r")

    def scan_once(self):
        now = time.monotonic()
        ports = {p.device: p for p in self.port_lister()}
        self.ports_seen = ports                  # for Overview's Serial ports list
        with self._lock:
            claimed = {i.port.device: slug for slug, i in self.instances.items()}
            # drop devices whose port has gone for good (unplugged, powered off for a while)
            for slug, inst in list(self.instances.items()):
                if inst.port.device in ports:
                    self._missing.pop(slug, None)
                    continue
                first = self._missing.setdefault(slug, now)
                if now - first >= GONE_S:
                    log.info("%s: %s has been gone for %d s; dropping it", slug, inst.port.device, GONE_S)
                    self._remove(slug)
        for dev in list(self._rejected):
            if dev not in ports:
                del self._rejected[dev]          # it went away; look again when it comes back
        for dev in list(self._stuck):
            if dev not in ports:
                del self._stuck[dev]             # unplugged: that is what frees a port stuck in the driver
        for dev in list(self._relays):
            if dev not in ports and dev not in claimed:
                self._release(dev)               # an instance's own port stays held until the instance is dropped
        # First identify every new port, then start the instances (recognised ports stay open meanwhile, see
        # opener_for). Detection then runs before any link starts reading.
        found = []
        for dev, port in sorted(ports.items()):
            if self._stop.is_set():
                return
            late = self._stuck.get(dev)
            if self.is_ignored(port):
                # Ignored on Overview: never opened, never probed (an open still stuck in the driver is let go).
                if late is not None and not late[0].is_alive():
                    del self._stuck[dev]
                continue
            if late is not None:
                t, box = late
                if t.is_alive():
                    continue
                del self._stuck[dev]
                if box.get("r"):
                    # It got through in the end: use what it found. Asking again would mean another open, which
                    # on 2026-11-20 took about 2 minutes every time on a port in this state.
                    log.info("%s came back from the serial driver: %s", dev, box["r"][0].title)
                    if dev not in claimed:
                        found.append((port, box["r"]))
                    continue
                log.info("%s came back from the serial driver with nothing recognised", dev)
                self._rejected[dev] = time.monotonic()
                continue
            if dev in claimed or now - self._rejected.get(dev, -1e9) < RETRY_S:
                continue
            res = self._identify_guarded(port)
            if res is _STUCK:
                continue
            if not res:
                self._rejected[dev] = time.monotonic()
                continue
            found.append((port, res))
        for port, (driver, ident) in found:
            if self._stop.is_set():
                return
            dev = port.device
            try:
                with self._lock:
                    ctx = self._ctx[driver.key]
                    ctx._number = self.free_number(driver.key)
                    try:
                        inst = driver.start(ctx, port, ident)
                    finally:
                        number, ctx._number = ctx._number, None
                    inst.number = number
                    if inst.slug in self.instances:
                        # A driver that names its tab itself and picks a name in use: never replace the running
                        # one (it would keep its port, unstopped and unreachable).
                        try:
                            inst.stop()
                        finally:
                            raise RuntimeError(f"{driver.key} named its device {inst.slug!r}, which is in use")
                    self.instances[inst.slug] = inst
                    self._table = (0.0, [])
                log.info("found %s on %s: %s", inst.title, port.label(), ident)
            except Exception:
                log.exception("%s: starting %s failed", dev, driver.key)
                self._rejected[dev] = time.monotonic()
                self._release(dev)

    def _remove(self, slug):
        inst = self.instances.pop(slug, None)
        if inst:
            try:
                self.recorder.record(inst, final=True)   # its last minutes, while its driver still answers
            except Exception:
                log.exception("recording %s", slug)
            try:
                inst.stop()
            except Exception:
                log.exception("stopping %s", slug)
            self._release(inst.port.device)       # held open by the hub for the instance's whole life
        self._missing.pop(slug, None)
        self._table = (0.0, [])

    def _scanner(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.scan_started = time.time()
            self.scanning = True
            try:
                self.scan_once()
            except Exception:
                log.exception("scan failed")
            finally:
                self.scanning = False
                self.last_scan = time.time()
            self._wake.wait(self.scan_s)

    # ---------------------------------------------------------------- ignored ports (Overview)
    @staticmethod
    def _port_matches(entry, port):
        if entry.get("serial"):
            return (port.serial == entry["serial"] and port.vid == entry.get("vid") and port.pid == entry.get("pid"))
        return port.device == entry.get("device")

    @staticmethod
    def _port_key(entry):
        return entry.get("serial") or entry.get("device")

    def is_ignored(self, port):
        return any(self._port_matches(e, port) for e in self.settings.ports_ignored)

    def ignore_port(self, device):
        """Overview's Ignore: the port is never opened or probed again (remembered in hub.json). Not a port a device
        is on: turn its driver off instead. Returns an error text, or "" when done."""
        port = self.ports_seen.get(device)
        if port is None:
            return f"{device} isn't there now"
        with self._lock:
            if any(i.port.device == device for i in self.instances.values()):
                return f"a device is on {device}: turn its driver off on Overview instead"
            if not self.is_ignored(port):
                self.settings.ports_ignored.append({"device": port.device, "vid": port.vid, "pid": port.pid,
                                                    "serial": port.serial, "description": port.description})
            self._rejected.pop(device, None)
        self._release(device)
        self._save()
        log.info("%s ignored: never opened or probed until it's no longer ignored", port.label())
        return ""

    def unignore_port(self, key):
        """Overview's Stop ignoring (key: the USB serial number, or the COM port when the chip has none)."""
        with self._lock:
            before = len(self.settings.ports_ignored)
            self.settings.ports_ignored = [e for e in self.settings.ports_ignored if self._port_key(e) != key]
            if len(self.settings.ports_ignored) == before:
                return f"{key} isn't ignored"
        self._save()
        log.info("%s no longer ignored", key)
        self.rescan()
        return ""

    def ports_json(self):
        """Overview's Serial ports list: every port of the last scan, what the hub made of it, and the ignored ones
        that aren't there now."""
        with self._lock:
            on = {i.port.device: i.slug for i in self.instances.values()}
        out = []
        for dev, p in sorted(self.ports_seen.items()):
            if dev in on:
                state = "in use"
            elif self.is_ignored(p):
                state = "ignored"
            elif dev in self._stuck:
                state = "stuck"
            elif p.usb is None or not any(p.usb in d.usb_ids for d in self.drivers):
                state = "no driver"
            elif dev in self._rejected:
                state = "not recognised"
            else:
                state = "not looked at yet"
            key = next((self._port_key(e) for e in self.settings.ports_ignored if self._port_matches(e, p)), None)
            out.append({"device": dev, "usb": f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "",
                        "serial": p.serial, "description": p.description, "state": state, "slug": on.get(dev),
                        "key": key})
        present = {r["key"] for r in out if r["key"]}
        for e in self.settings.ports_ignored:
            k = self._port_key(e)
            if k not in present:
                usb = f"{e['vid']:04X}:{e['pid']:04X}" if e.get("vid") is not None else ""
                out.append({"device": e.get("device", ""), "usb": usb, "serial": e.get("serial", ""),
                            "description": e.get("description", ""), "state": "ignored, not plugged in",
                            "slug": None, "key": k})
        return out

    def rescan(self):
        """Scan now (the Overview's button): also looks again at ports nobody recognised, without waiting RETRY_S.
        A request during a pass runs one more pass right after it, so a device plugged in meanwhile is seen."""
        self._rejected.clear()
        self._wake.set()

    # ---------------------------------------------------------------- the Alpaca device table
    def _fetch(self, inst, path):
        c = http.client.HTTPConnection("127.0.0.1", inst.local_port, timeout=5)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return json.loads(r.read().decode("utf-8"))
        finally:
            c.close()

    def device_table(self):
        """[{type, number, name, uid, slug, internal}] for every Alpaca device the present instances serve.
        number is the hub's public number: stable per UniqueID, never reused by another device."""
        t, table = self._table
        if time.monotonic() - t < TABLE_S:
            return table
        rows = []
        with self._lock:
            insts = list(self.instances.values())
        for inst in insts:
            try:
                if not inst.local_port:
                    raise OSError("not running")
                devs = self._fetch(inst, "/management/v1/configureddevices?ClientID=0&ClientTransactionID=0")["Value"]
                inst_rows = [{"type": d["DeviceType"].lower(), "name": d["DeviceName"], "uid": d["UniqueID"],
                              "slug": inst.slug, "internal": int(d["DeviceNumber"])} for d in devs]
                inst._alpaca_rows = inst_rows
            except Exception as e:
                # While its driver restarts, a device keeps its place: clients get "not connected", not "gone".
                log.debug("%s: device list failed: %s", inst.slug, e)
                inst_rows = getattr(inst, "_alpaca_rows", [])
            rows.extend(dict(r) for r in inst_rows)
        changed = False
        with self._lock:
            nums = self.settings.numbers
            for r in rows:
                if r["uid"] in nums and nums[r["uid"]][0] == r["type"]:
                    continue
                taken = {n for (typ, n) in nums.values() if typ == r["type"]}
                n = 0
                while n in taken:
                    n += 1
                nums[r["uid"]] = [r["type"], n]
                changed = True
                log.info("%s %s is Alpaca %s #%d", r["slug"], r["name"], r["type"], n)
            for r in rows:
                r["number"] = nums[r["uid"]][1]
        if changed:
            self._save()
        rows.sort(key=lambda r: (r["type"], r["number"]))
        self._table = (time.monotonic(), rows)
        return rows

    def lookup(self, typ, number):
        for r in self.device_table():
            if r["type"] == typ and r["number"] == number:
                return r
        return None

    # ---------------------------------------------------------------- lifecycle
    def start(self, scan=True, watch=True):
        hub = self

        class Handler(_Handler):
            srv = hub

        self.httpd = _make_http_server(self.settings.bind, self.settings.http_port, Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="hub-http", daemon=True).start()
        self._write_running()
        if self.supervisor is not None and watch:
            self.supervisor.start_watching()      # drivers installed, updated or uninstalled while running
        try:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind = self.settings.bind
            self._udp.bind(("" if bind == "0.0.0.0" else bind, DISCOVERY_PORT))
            self._udp.settimeout(0.5)
            threading.Thread(target=self._discovery, name="hub-discovery", daemon=True).start()
        except OSError as e:
            log.warning("Alpaca discovery disabled (UDP %d: %s)", DISCOVERY_PORT, e)
            self._udp = None
        if scan:
            threading.Thread(target=self._scanner, name="hub-scan", daemon=True).start()
        threading.Thread(target=self.recorder.run, args=(self._stop,), name="hub-history", daemon=True).start()
        threading.Thread(target=self.updates.run, args=(self._stop,), name="hub-updates", daemon=True).start()
        log.info("%s %s on http://%s:%d/", NAME, VERSION, self.settings.bind, self.port)

    def _write_running(self):
        """%ProgramData%\\AlpacaHub\\running.json: a driver's uninstaller reads the port there to wait until its driver
        has stopped. Only where the folder exists (the hub's installer makes it, writable for users)."""
        p = self.running_file
        if not p or not os.path.isdir(os.path.dirname(p)):
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"port": self.port, "pid": os.getpid(), "version": VERSION, "api": API_VERSION}, f)
        except OSError as e:
            log.debug("cannot write %s: %s", p, e)

    def _clear_running(self):
        p = self.running_file
        try:
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    if json.load(f).get("pid") == os.getpid():
                        os.remove(p)
        except (OSError, ValueError):
            pass

    def _instance_list(self):
        with self._lock:
            return list(self.instances.values())

    def stop(self):
        self._clear_running()
        self._stop.set()
        self._wake.set()
        self.updates._wake.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._udp:
            self._udp.close()
        with self._lock:
            for slug in list(self.instances):
                self._remove(slug)
            for dev in list(self._relays):
                self._release(dev)
        if self.supervisor is not None:
            self.supervisor.stop()

    def retry_driver(self, name):
        """Overview's 'Try again' for a driver process that failed."""
        p = self.supervisor.find(name) if self.supervisor is not None else None
        return bool(p and p.retry())

    def _discovery(self):
        reply = json.dumps({"AlpacaPort": self.port}).encode()
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(b"alpacadiscovery1"):
                continue
            kind = self._dedupe.classify(addr[0])
            if kind == "now":
                self._send(reply, addr)
            elif kind == "later":
                t = threading.Timer(DiscoveryDedupe.HOLD_S,
                                    lambda: self._dedupe.still_needed() and self._send(reply, addr))
                t.daemon = True
                t.start()

    def _send(self, reply, addr):
        try:
            self._udp.sendto(reply, addr)
        except OSError:
            pass

    def next_txn(self):
        with self._lock:
            self._txn += 1
            return self._txn

    def hub_json(self):
        with self._lock:
            insts = list(self.instances.values())
        return {"name": NAME, "version": VERSION, "simulated": self.simulated, "urls": self.public_urls(),
                "uptime": int(time.time() - self.started),
                "devices": [{"slug": i.slug, "title": i.title, "kind": i.driver.key, "port": i.port.device, "up": i.up(),
                             "unit": history_unit(i),     # its day files: /hub/history/<kind>/<unit>/…
                             "summary": i.summary(), "missing": i.slug in self._missing} for i in insts],
                "stuck": sorted(self._stuck), "scanning": self.scanning, "scan_started": self.scan_started,
                "ports": self.ports_json(),
                "drivers": self.supervisor.info() if self.supervisor is not None else [],
                "refused": dict(self.refused),
                "last_scan": self.last_scan,
                "scan_every": self.scan_s,
                "alpaca": [{k: r[k] for k in ("type", "number", "name", "slug")} for r in self.device_table()],
                "updates": self.updates_json()}

    def updates_json(self):
        """hub.json's "updates": the check's state, each offered update with "in_use" (an Alpaca program is
        connected to a device it would interrupt: every device for the hub, the driver's own for a driver)."""
        u = dict(self.updates.status(), on=bool(self.settings.update_check))
        if u["available"]:
            with self._lock:
                kinds = {i.slug: i.driver.key for i in self.instances.values()}
            procs = {d["name"]: set(d.get("drivers") or [d["name"]])
                     for d in (self.supervisor.info() if self.supervisor is not None else [])}
            for a in u["available"]:
                slugs = [s for s, k in kinds.items() if a["kind"] == "hub" or k in procs.get(a["key"], {a["key"]})]
                a["in_use"] = self.in_use(slugs)
        return u

    def _installed_drivers(self):
        """{key: (version, title)} of the drivers here, for the update check: an installed update counts as the
        version here (it takes over on its own). Not the ones built into the hub."""
        if self.supervisor is None:
            return {}
        return {d["name"]: (d.get("update") or d["version"], d.get("title") or d["name"])
                for d in self.supervisor.info() if d.get("source") in ("installed", "source")}

    def set_update_check(self, on):
        """The Overview's "Check for updates online". Turned on, it checks straight away."""
        self.settings.update_check = bool(on)
        self.settings.save()
        if on:
            self.updates.check_now()
        return self.settings.update_check

    # Saved history: a driver that writes its history to disk keeps it in a "<unit>-history" folder next to that
    # unit's settings (<config dir>\drivers\<key>\<unit>-history, today the power box's day files). The Overview
    # lists them, connected or not, so the files can be downloaded and plotted after the device has gone.
    _HIST_SUFFIX = "-history"

    def _history_dirs(self):
        """[(driver key, unit, folder)] of every <unit>-history folder, sorted."""
        root = os.path.join(self.config_dir, "drivers")
        out = []
        try:
            keys = sorted(os.listdir(root))
        except OSError:
            return out
        for key in keys:
            d = os.path.join(root, key)
            try:
                names = sorted(os.listdir(d)) if os.path.isdir(d) else []
            except OSError:
                continue
            for n in names:
                if n.endswith(self._HIST_SUFFIX) and len(n) > len(self._HIST_SUFFIX) and os.path.isdir(os.path.join(d, n)):
                    out.append((key, n[:-len(self._HIST_SUFFIX)], os.path.join(d, n)))
        return out

    @staticmethod
    def _csv_files(folder):
        """[(name, bytes, modified)] of the .csv files in folder, oldest name first."""
        out = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return out
        for n in names:
            p = os.path.join(folder, n)
            if n.lower().endswith(".csv") and os.path.isfile(p):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((n, st.st_size, int(st.st_mtime)))
        return out

    def saved_history(self):
        """/hub/history.json: {"days": how long every device's files are kept, "folder", "units": [{"driver", "unit",
        "folder", "files": [{"name", "bytes", "modified"}]}]}, folders with files only."""
        out = []
        for key, unit, folder in self._history_dirs():
            files = self._csv_files(folder)
            if files:
                out.append({"driver": key, "unit": unit, "folder": folder,
                            "files": [{"name": n, "bytes": b, "modified": m} for n, b, m in files]})
        return {"days": clamp_days(self.settings.history_days), "folder": os.path.join(self.config_dir, "drivers"),
                "units": out}

    def open_history_folder(self, key="", unit=""):
        """Overview's Open folder, on this PC: one listed unit's folder, else the drivers folder above them all.
        Returns an error text, or "" when it was opened."""
        if key or unit:
            folder = self.history_folder(key, unit)
            if folder is None:
                return "No such history folder"
        else:
            folder = os.path.join(self.config_dir, "drivers")
            os.makedirs(folder, exist_ok=True)
        try:
            self.show_folder(folder)
        except OSError as e:
            return f"Cannot open {folder}: {e}"
        return ""

    def set_history_days(self, days):
        """The Overview's setting, for every device: the box gets it too (Recorder.sync_days)."""
        self.settings.history_days = clamp_days(days)
        self.settings.save()
        self.recorder.purge()
        self.recorder.sync_all()
        return self.settings.history_days

    def _history_days_from_device(self, days):
        """A box's tab changed its days: that is the setting for every device now."""
        self.settings.history_days = clamp_days(days)
        self.settings.save()
        self.recorder.purge()

    def history_folder(self, key, unit):
        """The folder of one listed (driver, unit), or None. Only names the listing gives are accepted."""
        for k, u, folder in self._history_dirs():
            if k == key and u == unit:
                return folder
        return None

    def history_file(self, key, unit, name):
        """The path of one listed file, or None."""
        folder = self.history_folder(key, unit)
        if folder is None or name not in [n for n, _, _ in self._csv_files(folder)]:
            return None
        return os.path.join(folder, name)

    def history_zip(self, key, unit):
        """One unit's saved files as a zip (bytes), or None."""
        import io
        import zipfile
        folder = self.history_folder(key, unit)
        if folder is None or not self._csv_files(folder):     # as listed: folders with files only
            return None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, _, _ in self._csv_files(folder):
                try:
                    z.write(os.path.join(folder, n), n)
                except OSError as e:
                    log.warning("saved history: cannot add %s to the download: %s", n, e)
        return buf.getvalue()


_chart = []


def _guide_path():
    """USER-GUIDE.html: next to the installed exe (packaging/hub.iss), else docs/ of the repository."""
    import sys
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    p = os.path.join(here, "USER-GUIDE.html")
    return p if os.path.isfile(p) else None


def _show_folder(path):
    """Opens a folder in Explorer (Windows; elsewhere the desktop's file manager)."""
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def _safe_header(name):
    """A file name for Content-Disposition: no quotes, backslashes or control characters."""
    return "".join(c for c in name if c >= " " and c not in '"\\') or "download"


def _chart_page():
    """The Overview's chart page, built from hub/overview_ui like a device page (rebuilt when a file there changes)."""
    if not _chart:
        _chart.append(Page(os.path.join(os.path.dirname(os.path.abspath(__file__)), "overview_ui"), "Overview"))
    return _chart[0]


class _Handler(BaseHTTPRequestHandler):
    srv: Hub
    protocol_version = "HTTP/1.1"
    server_version = "alpaca-hub/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _reply(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _this_pc(self):
        """The request comes from the hub's PC itself (the socket's own address, not a forwarded one)."""
        ip = _plain_ip(self.client_address[0])
        return ip.startswith("127.") or ip == "::1"

    def _alpaca(self, value):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        q = {k.lower(): v for k, v in q.items()}
        try:
            ctid = int(q.get("clienttransactionid", ["0"])[0])
        except ValueError:
            ctid = 0
        self._reply(200, json.dumps({"Value": value, "ClientTransactionID": ctid,
                                     "ServerTransactionID": self.srv.next_txn(), "ErrorNumber": 0,
                                     "ErrorMessage": ""}))

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        try:
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            parts = [p for p in path.split("/") if p]
            hub = self.srv
            if path in ("/", "/index.html"):
                return self._reply(200, shell_page(), "text/html; charset=utf-8")
            if path in ("/hub/chart", "/hub/chart/"):    # the Overview's chart of every device (hub/overview_ui)
                return self._reply(200, _chart_page().get()[0], "text/html; charset=utf-8")
            if path in ("/hub/help", "/hub/help/"):    # the user guide (the header's ? button)
                g = _guide_path()
                if g is None:
                    return self._reply(404, "The user guide isn't installed", "text/plain")
                with open(g, "rb") as f:
                    return self._reply(200, f.read(), "text/html; charset=utf-8")
            if path == "/hub.json":
                d = hub.hub_json()
                d["local"] = self._this_pc()               # the Overview's Install buttons: on this PC only
                return self._reply(200, json.dumps(d))
            if path == "/hub/history.json":              # saved history files on disk (the Overview's links)
                d = hub.saved_history()
                d["local"] = self._this_pc()               # Open folder works on this PC only
                return self._reply(200, json.dumps(d))
            if path == "/hub/history/open" and self.command == "POST":
                # Opens a folder in Explorer on the hub's PC: from that PC itself only, like /hub/shutdown.
                if not self._this_pc() or self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Only from this PC", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace") if n else ""))
                err = hub.open_history_folder(form.get("driver", ""), form.get("unit", ""))
                return self._reply(404 if err else 200, json.dumps({"ok": not err, "error": err}))
            if path == "/hub/history/days" and self.command == "POST":
                if self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Page requests only", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace") if n else ""))
                return self._reply(200, json.dumps({"days": hub.set_history_days(form.get("days"))}))
            if len(parts) in (4, 5) and parts[:2] == ["hub", "history"] and self.command in ("GET", "HEAD"):
                # /hub/history/<driver>/<unit>.zip, /hub/history/<driver>/<unit>/<file>.csv: listed names only
                key = urllib.parse.unquote(parts[2])
                if len(parts) == 4 and parts[3].endswith(".zip"):
                    unit = urllib.parse.unquote(parts[3][:-4])
                    data = hub.history_zip(key, unit)
                    if data is None:
                        return self._reply(404, "No such history", "text/plain")
                    name = f"{key}-{unit}-history-{time.strftime('%Y-%m-%d')}.zip"
                    return self._reply(200, data, "application/zip",
                                       {"Content-Disposition": f'attachment; filename="{_safe_header(name)}"'})
                if len(parts) == 5:
                    p = hub.history_file(key, urllib.parse.unquote(parts[3]), urllib.parse.unquote(parts[4]))
                    if p is None:
                        return self._reply(404, "No such file", "text/plain")
                    try:
                        with open(p, "rb") as f:
                            data = f.read()
                    except OSError:
                        return self._reply(404, "Cannot read the file", "text/plain")
                    return self._reply(200, data, "text/csv; charset=utf-8", {
                        "Content-Disposition": f'attachment; filename="{_safe_header(os.path.basename(p))}"'})
                return self._reply(404, "Not found", "text/plain")
            if path == "/hub/shutdown" and self.command == "POST":
                # hub --stop: from this PC itself only (the socket's own address, not a forwarded one), and with the
                # header a cross-site form can't send.
                if not self._this_pc() or self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Only from this PC", "text/plain")
                if hub.on_shutdown is None:
                    return self._reply(404, "Not available", "text/plain")
                self._reply(200, json.dumps({"stopping": True}))
                threading.Thread(target=hub.on_shutdown, name="hub-shutdown", daemon=True).start()
                return
            if path in ("/hub/ports/ignore", "/hub/ports/unignore") and self.command == "POST":
                if self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Page requests only", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace") if n else ""))
                err = (hub.ignore_port(form.get("device", "")) if path.endswith("/ignore")
                       else hub.unignore_port(form.get("key", "")))
                return self._reply(400 if err else 200, json.dumps({"ok": not err, "error": err}))
            if path == "/hub/updates/install" and self.command == "POST":
                # Downloads and runs an installer: from the hub's PC itself only (Windows' administrator prompt
                # shows there), like /hub/shutdown.
                if not self._this_pc() or self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Only from this PC", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace") if n else ""))
                err = hub.updates.install(form.get("key", ""))
                return self._reply(409 if err else 200, json.dumps({"ok": not err, "error": err}))
            if path in ("/hub/updates", "/hub/updates/check") and self.command == "POST":
                # The Overview's update setting and Check now: from the hub's own page only. Check now only while
                # the check is on (turned off, the hub sends nothing).
                if self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Page requests only", "text/plain")
                if path.endswith("/check"):
                    if not hub.updates.enabled():
                        return self._reply(409, "The update check is off", "text/plain")
                    hub.updates.check_now()
                    return self._reply(200, json.dumps(hub.updates.status()))
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace") if n else ""))
                hub.set_update_check(form.get("on") == "1")
                return self._reply(200, json.dumps(hub.updates.status()))
            if path == "/hub/rescan" and self.command == "POST":
                # From the hub's own page only (a header a cross-site form can't send), from any address: a scan
                # does no more than the hub does on its own every scan_s.
                if self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Page requests only", "text/plain")
                requested = time.time()
                hub.rescan()
                return self._reply(200, json.dumps({"requested": requested}))
            if path in ("/hub/drivers", "/hub/drivers/"):
                # Every driver and its state; a driver's uninstaller waits here until its driver shows stopped.
                return self._reply(200, json.dumps(hub.supervisor.info() if hub.supervisor is not None else []))
            if len(parts) == 4 and parts[:2] == ["hub", "drivers"] and self.command == "POST" \
                    and parts[3] in ("retry", "switch", "on", "off"):
                if self.headers.get("X-Hub-Page") != "1":
                    return self._reply(403, "Page requests only", "text/plain")
                if hub.supervisor is None or (parts[3] != "on" and hub.supervisor.find(parts[2]) is None
                                              and parts[2] not in hub.supervisor.listing):
                    return self._reply(404, "No such driver", "text/plain")
                sup, name = hub.supervisor, parts[2]
                ok = (hub.retry_driver(name) if parts[3] == "retry" else sup.switch_now(name) if parts[3] == "switch"
                      else sup.set_enabled(name, parts[3] == "on"))
                return self._reply(200, json.dumps({"ok": bool(ok)}))
            if path == "/management/apiversions":
                return self._alpaca([1])
            if path == "/management/v1/description":
                return self._alpaca({"ServerName": NAME, "Manufacturer": "alpaca-hub", "ManufacturerVersion": VERSION,
                                     "Location": hub.settings.location or socket.gethostname()})
            if path == "/management/v1/configureddevices":
                return self._alpaca([{"DeviceName": r["name"], "DeviceType": _alpaca_type(r["type"]),
                                      "DeviceNumber": r["number"], "UniqueID": r["uid"]}
                                     for r in hub.device_table()])
            if len(parts) >= 3 and parts[0] in ("api", "setup") and parts[1] == "v1":
                if len(parts) < 4:
                    return self._reply(404, "Not found", "text/plain")
                try:
                    num = int(parts[3])
                except ValueError:
                    return self._reply(400, f"Invalid device number {parts[3]!r}", "text/plain")
                if parts[0] == "api" and parts[2] != parts[2].lower():
                    return self._reply(400, f"Device types are lower case in Alpaca URLs: {parts[2]!r}",
                                       "text/plain")
                row = hub.lookup(parts[2].lower(), num)
                if row is None:
                    return self._reply(400 if parts[0] == "api" else 404,
                                       f"{parts[2]} #{num} is not present on this hub", "text/plain")
                inner = "/".join([""] + parts[:3] + [str(row["internal"])] + parts[4:])
                note = None
                if parts[0] == "api" and len(parts) >= 5:
                    def note(body, r=row, m=parts[4].lower()):
                        hub.note_client(r["slug"], r["type"], r["number"], m, self.command, body)
                return self._forward(hub.instances.get(row["slug"]), inner, u.query, note)
            if parts and parts[0] in hub.instances:
                if len(parts) == 1 and not path.endswith("/"):
                    return self._reply(301, "", "text/plain", {"Location": f"/{parts[0]}/"})
                inner = "/" + "/".join(parts[1:]) + ("/" if path.endswith("/") and len(parts) > 1 else "")
                return self._forward(hub.instances[parts[0]], inner, u.query)
            if parts and parts[0] == "favicon.ico":
                return self._reply(404, "", "text/plain")
            return self._reply(404, "Not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("request failed: %s %s", self.command, self.path)
            try:
                self._reply(500, "Internal error", "text/plain")
            except Exception:
                pass

    def _not_connected(self, query, body):
        """An Alpaca "not connected" answer, so clients see a device whose driver is restarting and try again
        (an HTTP error instead would make NINA and the ASCOM library treat the device as failed)."""
        q = urllib.parse.parse_qs(query)
        if body:
            q.update(urllib.parse.parse_qs(body.decode("utf-8", "replace")))
        txn = next((v[0] for k, v in q.items() if k.lower() == "clienttransactionid"), "0")
        try:
            txn = int(txn)
        except ValueError:
            txn = 0
        reply = {"ClientTransactionID": txn, "ServerTransactionID": self.srv.next_txn(),
                 "ErrorNumber": NOT_CONNECTED, "ErrorMessage": "The device's driver is restarting"}
        return self._reply(200, json.dumps(reply))

    def _forward(self, inst, path, query, note=None):
        if inst is not None and not inst.local_port and path.startswith("/api/"):
            n = int(self.headers.get("Content-Length") or 0) if self.command == "PUT" else 0
            return self._not_connected(query, self.rfile.read(n) if n else None)
        if inst is None or not inst.local_port:
            return self._reply(503, "That device's driver is restarting" if inst is not None
                               else "That device is not running", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        if note is not None:
            note(body)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
        headers["Host"] = f"127.0.0.1:{inst.local_port}"
        headers["X-Forwarded-For"] = _plain_ip(self.client_address[0])
        if body is not None:
            headers["Content-Length"] = str(len(body))
        c = http.client.HTTPConnection("127.0.0.1", inst.local_port, timeout=60)
        try:
            c.request(self.command, path + (("?" + query) if query else ""), body=body, headers=headers)
            r = c.getresponse()
            data = r.read()
            self.send_response(r.status, r.reason)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError as e:
            log.warning("%s: forwarding %s failed: %s", inst.slug, path, e)
            if path.startswith("/api/"):
                # Its driver process died before the hub noticed (the restart follows): same as while restarting.
                self._not_connected(query, body)
            else:
                self._reply(502, "That device didn't answer", "text/plain")
        finally:
            c.close()


ALPACA_TYPES = {"camera": "Camera", "covercalibrator": "CoverCalibrator", "dome": "Dome", "filterwheel": "FilterWheel",
                "focuser": "Focuser", "observingconditions": "ObservingConditions", "rotator": "Rotator",
                "safetymonitor": "SafetyMonitor", "switch": "Switch", "telescope": "Telescope"}


def _alpaca_type(t):
    return ALPACA_TYPES.get(t, t)
