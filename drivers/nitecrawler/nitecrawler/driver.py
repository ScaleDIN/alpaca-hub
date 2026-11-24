"""Moonlite NiteCrawler (WR25 / WR30 / WR35) with Moonlite's stock firmware: Focuser (+ aux) and Rotator.

A driver folder (drivers/nitecrawler, REFACTOR.md step 8): it runs in its own process, reaches the hub only through
hub.api and hub.devserver, and serves its Alpaca devices on the hub's DeviceServer (devices.py). The link, service,
settings and simulator were the standalone ncalpaca service's.
"""
import logging
import os

from hub.api import Driver, Instance, unit_of
from hub.devserver import DeviceServer

from . import VERSION
from . import protocol as NP
from .devices import NCApp, Names, make_devices
from .link import Transport, probe
from .service import Service
from .settings import Settings, default_path

log = logging.getLogger("nitecrawler")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# FTDI chips Moonlite has used (FT230X on the WR35; FT232R on older boxes).
FTDI = frozenset({(0x0403, 0x6015), (0x0403, 0x6001)})


class NiteCrawlerDriver(Driver):
    key = "nitecrawler"
    title = "NiteCrawler"
    usb_ids = FTDI
    probe_baud = NP.BAUD
    probe_priority = 10          # on a shared FTDI chip, before the Optec probes (115200 baud)
    simulators = {"nitecrawler": lambda i: (_sim(serial=str(10001 + i)), (0x0403, 0x6015), f"SIMNC{i + 1}",
                                            "Simulated NiteCrawler")}

    def probe(self, ser):
        found = probe(ser)            # "PF#" then "PV#"; None unless the model looks like "3.5 NC"
        if not found:
            return None
        fw, model = found
        serial = ""
        try:
            serial = (Transport(ser).ask(b"PS#", "value", timeout=0.6) or "").strip()
        except Exception:
            pass
        return {"model": model, "firmware": fw, "serial": serial}

    def start(self, ctx, port, ident):
        return NiteCrawlerInstance(self, ctx, port, ident)


def _sim(**kw):
    from .sim import SimNiteCrawler
    return SimNiteCrawler(**kw)


class NiteCrawlerInstance(Instance):
    def __init__(self, driver, ctx, port, ident, opener=None):
        unit = unit_of(ident, port)
        n = ctx.unit_number()
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        title = f"NiteCrawler {ident.get('model', '')}".strip() + ("" if n == 0 else f" #{n + 1}")
        super().__init__(driver, port, ident, slug, title)
        legacy = [ctx.legacy_file(f"nitecrawler-{unit}.json")]
        if ctx.legacy_primary(unit):
            legacy.insert(0, default_path() if ctx.use_standalone_settings
                          else ctx.legacy_file("nitecrawler-primary.json"))
        path = ctx.settings_path(unit, legacy)
        settings = Settings.load(path)
        settings.port = port.device
        if n and not settings.device_name:
            settings.device_name = f"NiteCrawler #{n + 1}"   # so the clients' device lists tell the units apart
        opener = opener or ctx.opener_for(port, NP.BAUD)
        self.svc = Service(settings, opener=opener)
        names = Names(self.svc, ctx.simulated)
        self.devices = make_devices(self.svc, names, lambda k: ctx.uid(unit, k))   # hub API 1 IDs: focus, aux, rotator
        self.app = NCApp(self.svc, names, self.devices, UI, port.device)
        self.srv = DeviceServer(self.devices, self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self.srv.start()
        self.local_port = self.srv.port
        self.svc.start()
        log.info("%s on %s (serial %s, settings %s)", title, port.device, unit, path)

    def stop(self):
        self.srv.stop()
        self.svc.stop()

    def up(self):
        return self.svc.link.up()

    def summary(self):
        st = self.svc.link.status
        if not self.up() or st is None:
            return {"text": "no answer", "state": "bad"}
        rot = self.svc.rot_position(st) if st.rotator.position is not None else None
        parts = [f"focus {st.focus.position:,}".replace(",", " ")]
        if rot is not None:
            parts.append(f"rotator {rot:.2f}°")
        moving = st.focus.moving or st.rotator.moving or st.homing
        return {"text": " · ".join(parts), "state": "warn" if moving else "ok"}


DRIVERS = [NiteCrawlerDriver]
