"""What a device driver provides to the hub, and what the hub gives it (hub API version 1, REFACTOR.md).

A driver knows one family of devices (a brand, a model). It says how to recognise its device safely, and starts an
Instance for each one found. An Instance runs the device and serves its own Alpaca devices and status page on a
private 127.0.0.1 port; the hub forwards to it. A driver reaches the hub only through the HubContext it is given.

Recognising devices safely (the hub enforces the order):
0. A driver only ever looks at ports whose USB IDs are in its usb_ids. (Other ports, Bluetooth COM ports among them,
   are never even opened: some hang for seconds when opened.)
1. Listen first. A driver with listen_baud set recognises its device from bytes the device sends unasked
   (status lines). Listening never changes anything.
2. Only a port that stayed silent at every baud is asked anything, and only by a driver with probe_baud set, in
   order of probe_priority (lowest first). Some devices act on any byte (the WandererBox Plus V3 treats input as a
   number and stores every command): a driver that listens for such a device lists its USB IDs in never_probe, and
   the hub then refuses any driver that would probe them.
"""
import logging
import os
import re
import shutil
import uuid

API_VERSION = 1

log = logging.getLogger("hub.api")


class Driver:
    key = "device"            # short id, used in settings, URLs and UniqueIDs. Never change it once released
    title = "Device"          # what the page calls it
    api = API_VERSION         # the hub API version this driver was written for
    transport = "serial"      # "serial" only, for now (REFACTOR.md: "usb" and "network" come later)
    usb_ids = frozenset()     # {(vid, pid)}: the only ports this driver looks at
    listen_baud = None        # passive drivers: baud to listen at
    probe_baud = None         # active drivers: baud for probe()
    probe_priority = 100      # active drivers on shared USB IDs are asked in this order, lowest first
    never_probe = frozenset()  # {(vid, pid)} where any byte can do harm: no driver may probe these
    lines = False             # open with DTR/RTS on (no bytes are sent). The hub turns them on for a port when any
                              # driver for its USB IDs asks, from the first listen to the link's reopens
    # Simulated devices for --simulate and the tests: {name: factory(index) -> (simulator, (vid, pid), usb serial,
    # description)}. The simulator has opener(device) -> a serial-like object. Names are also accepted by --drivers.
    simulators = {}

    def recognise(self, data):
        """Passive: bytes heard on a port at listen_baud -> identity dict, or None."""
        return None

    def probe(self, ser):
        """Active: ask the device on an open, silent port (probe_baud) -> identity dict, or None.
        The identity should include "serial" when the device has one, so units can be told apart."""
        return None

    def start(self, ctx, port, ident):
        """Runs the device found on port. ctx is this driver's HubContext. Returns an Instance."""
        raise NotImplementedError


class Instance:
    """One running device. Subclasses set local_port once their private Alpaca server listens."""

    def __init__(self, driver, port, ident, slug, title):
        self.driver = driver
        self.port = port          # PortInfo
        self.ident = ident
        self.slug = slug          # URL prefix and tab id: "nitecrawler", "nitecrawler-2"
        self.title = title        # tab title
        self.local_port = None

    def stop(self):
        pass

    def up(self):
        """True while the device answers."""
        return True

    def summary(self):
        """Short status for the hub's overview: {"text": "...", "state": "ok"|"warn"|"bad"}."""
        return {"text": "", "state": "ok" if self.up() else "bad"}


def unit_of(ident, port):
    """Which unit this is, for its UniqueIDs and settings: the device's own serial number when it reports one, else
    the USB chip's serial number, else the COM port (the box and the panel: their CH340 has none)."""
    return str((ident or {}).get("serial") or port.serial or port.device).strip()


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_") or "unit"


class HubContext:
    """Everything a driver may use from the hub. One per driver."""

    def __init__(self, hub, driver):
        self._hub = hub
        self.key = driver.key
        self.log = logging.getLogger(f"hub.{driver.key}")
        self._number = None       # set by the hub while it starts a device (unit_number)

    @property
    def simulated(self):
        return self._hub.simulated

    def public_urls(self):
        return self._hub.public_urls()

    def is_local(self, ip):
        return self._hub.is_local(ip)

    def opener_for(self, port, baud):
        """The opener the device's link uses (see Hub.opener_for): the first call gets the port the hub recognised
        the device on, still open."""
        return self._hub.opener_for(port, baud)

    def unit_number(self):
        """The number the hub gave the device being started: 0, 1, 2… (tab names: "Power box #2"). It is the lowest
        one not in use by this driver's running devices, so slugs and titles made from it never clash."""
        if self._number is not None:
            return self._number
        return self._hub.free_number(self.key)

    def uid(self, unit, device):
        """The Alpaca UniqueID of one device of one unit, the same on every run and every PC."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpaca-hub:{self.key}:{unit}:{device}"))

    def settings_dir(self):
        d = os.path.join(self._hub.config_dir, "drivers", self.key)
        os.makedirs(d, exist_ok=True)
        return d

    def settings_path(self, unit, legacy=()):
        """<config dir>\\drivers\\<key>\\<unit>.json. The first time, the first existing file in legacy (where the hub
        kept this unit's settings before hub API 1) is copied there, so nothing is lost."""
        path = os.path.join(self.settings_dir(), _safe(unit) + ".json")
        if not os.path.exists(path):
            for old in legacy:
                if old and os.path.isfile(old):
                    try:
                        shutil.copy2(old, path)
                        self.log.info("settings for %s copied from %s to %s", unit, old, path)
                    except OSError as e:
                        self.log.warning("could not copy settings %s: %s", old, e)
                    break
        return path

    def legacy_file(self, name):
        """A file in the hub's settings folder as it was before hub API 1 (only for settings_path(legacy=...))."""
        return os.path.join(self._hub.config_dir, name)

    def legacy_primary(self, unit):
        """True if the old hub treated unit as this driver's first device (it kept the standalone settings)."""
        cur = self._hub.settings.primary.get(self.key)
        return cur == unit if cur is not None else self.unit_number() == 0

    @property
    def use_standalone_settings(self):
        """The old hub's first device used the standalone service's settings file (only for legacy paths)."""
        return self._hub.use_standalone_settings
