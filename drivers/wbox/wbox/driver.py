"""WandererBox Plus V3 with Wanderer's stock firmware: Alpaca Switch (outputs, dew heater) + ObservingConditions.

A driver folder (drivers/wbox, REFACTOR.md step 9): it runs in its own process, reaches the hub only through hub.api
and hub.devserver, and serves its Alpaca devices on the hub's DeviceServer (devices.py). The link, service, dew
controller, history, settings and simulator were the standalone wbstock service's. The flat panel is its own driver.
"""
import logging
import os

from hub.api import Driver, Instance, unit_of
from hub.devserver import DeviceServer

from . import VERSION
from . import protocol as WP
from .devices import BoxApp, Names, make_devices
from .service import Service
from .settings import Settings, default_path

log = logging.getLogger("wbox")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

CH340 = frozenset({(0x1A86, 0x7523)})


class WandererBoxDriver(Driver):
    key = "wbox"
    title = "WandererBox Plus V3"
    usb_ids = CH340
    listen_baud = WP.BAUD      # it prints a status line once a second, unasked; nothing is ever sent to find it
    lines = True               # DTR/RTS on, as Empire and INDI open it. It keeps talking (no restart) with them on,
                               # and the panel, on the same CH340 IDs, needs them (see WandererCoverDriver)
    never_probe = CH340        # it stores every byte it receives as a command: nothing may ever probe these ports
    simulators = {
        "box": lambda i: (_sim(fw="20250411", loop_s=1.0), (0x1A86, 0x7523), "", "Simulated WandererBox Plus V3"),
        # box-wsm: the user's open firmware (WSM020)
        "box-wsm": lambda i: (_sim(fw="WSM020", loop_s=1.0), (0x1A86, 0x7523), "", "Simulated WandererBox Plus V3"),
    }

    def recognise(self, data):
        for line in data.decode("ascii", "replace").splitlines():
            st = WP.parse_status(line)
            if st:
                return {"firmware": st.fw}
        return None

    def start(self, ctx, port, ident):
        return WandererBoxInstance(self, ctx, port, ident)


def _sim(**kw):
    from .sim import SimBox
    return SimBox(**kw)


class WandererBoxInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        unit = unit_of(ident, port)   # no serial number anywhere: the COM port
        n = ctx.unit_number()
        slug = "box" if n == 0 else f"box-{n + 1}"
        title = "Power box" if n == 0 else f"Power box #{n + 1}"
        super().__init__(driver, port, ident, slug, title)
        # Settings from before hub API 1 are copied over once. They hold dew_mode_adopted: without them, the first
        # run would take over the box's dew state again (and may send a dew command to the box).
        legacy = [ctx.legacy_file(f"wbox-{port.device}.json")]
        if n == 0:
            legacy.insert(0, default_path() if ctx.use_standalone_settings else ctx.legacy_file("wbox-primary.json"))
        path = ctx.settings_path(unit, legacy)
        settings = Settings.load(path)
        settings.port = port.device
        settings.cover_port = ""                    # the flat panel is the wcover driver (drivers/wcover)
        if n and not settings.device_name:
            settings.device_name = f"WandererBox #{n + 1}"
        hist = os.path.splitext(path)[0] + "-history"
        self.svc = Service(settings, opener=ctx.opener_for(port, WP.BAUD), history_dir=hist, cover=None)
        names = Names(self.svc, ctx.simulated)
        self.devices = make_devices(self.svc, names, lambda k: ctx.uid(unit, k))   # hub API 1 IDs: switch, oc
        self.app = BoxApp(self.svc, names, self.devices, UI, port.device)
        self.srv = DeviceServer(self.devices, self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self.srv.start()
        self.local_port = self.srv.port
        self.svc.start()
        log.info("%s on %s (firmware %s, settings %s)", title, port.device, ident.get("firmware"), path)

    def stop(self):
        self.srv.stop()
        self.svc.stop()

    def up(self):
        return self.svc.link.up()

    def summary(self):
        st = self.svc.link.status
        if not self.up() or st is None:
            return {"text": "no answer", "state": "bad"}
        parts = []
        if st.voltage is not None:
            parts.append(f"{st.voltage:.1f} V")
        if st.current is not None:
            parts.append(f"{st.current:.2f} A")
        if st.lens is not None:
            parts.append(f"lens {st.lens:.1f} °C")
        parts.append(f"dew {round(st.dew_pwm * 100 / 255)} %")
        return {"text": " · ".join(parts), "state": "ok"}


DRIVERS = [WandererBoxDriver]
