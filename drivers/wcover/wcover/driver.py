"""WandererCover V4-EC flat panel with Wanderer's firmware: Alpaca CoverCalibrator, plus its own status page.

A driver folder (drivers/wcover, REFACTOR.md step 7): it runs in its own process and reaches the hub only through
hub.api and hub.devserver. The serial link (link.py) was wbstock's V4ecCoverLink, checked against the real panel
(fw 20250703, 2026-11-19). The Alpaca behaviour is wbstock's CoverCalibrator handler, unchanged.
"""
import logging
import os
import threading
import time

from hub.api import Driver, Instance, unit_of
from hub.devserver import (DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED,
                           AlpacaDevice, AlpacaError, App, BadRequest, DeviceServer, History)

from . import VERSION
from .link import (V4EC_BAUD, CalibratorState, CoverBusy, CoverDevice, CoverState, V4ecCoverLink,
                   parse_cover_status)

log =logging.getLogger("wcover")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
CH340 = frozenset({(0x1A86, 0x7523)})   # its USB serial chip (it has no serial number: the unit is the COM port)
HEATER_NAMES = {0: "off", 50: "low", 100: "high", 150: "max"}


class WandererCoverDriver(Driver):
    key = "wcover"
    title = "WandererCover V4-EC"
    usb_ids = CH340
    listen_baud = V4EC_BAUD    # it sends a status line every 0.33 s, unasked
    lines = True               # 2026-11-20: the panel only talked with DTR/RTS on (it restarts and sends from ~1.7 s);
                               # with them off it stayed silent, and so was never recognised. Empire and INDI use on
    simulators = {"panel": lambda i: (_sim(), (0x1A86, 0x7523), "", "Simulated WandererCover V4-EC")}

    def recognise(self, data):
        for line in data.decode("ascii", "replace").splitlines():
            st = parse_cover_status(line)
            if st and str(st.get("model", "")).startswith("WandererCoverV4"):
                return {"model": st.get("model"), "firmware": str(st.get("firmware", "")).replace(".0", "")}
        return None

    def start(self, ctx, port, ident):
        return WandererCoverInstance(self, ctx, port, ident)


def _sim():
    from .sim import SimV4ec
    return SimV4ec()


class CoverCalibrator(AlpacaDevice):
    """Alpaca CoverCalibrator, answering exactly as wbstock's handler does."""
    type, type_name, interface_version = "covercalibrator", "CoverCalibrator", 1
    driver_info = "WandererCover V4-EC flat panel (stock firmware), alpaca-hub"
    driver_version = VERSION
    GET = {"coverstate", "calibratorstate", "brightness", "maxbrightness", "covermoving", "calibratorchanging",
           "devicestate"}
    PUT = {"opencover", "closecover", "haltcover", "calibratoron", "calibratoroff"}

    def __init__(self, cover, uid, name):
        super().__init__(uid, name, "WandererCover V4-EC flat panel")
        self.cover = cover

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        snap = self.cover.snapshot()
        return {"coverstate": lambda: int(snap.cover), "calibratorstate": lambda: int(snap.calibrator),
                "brightness": lambda: snap.brightness, "maxbrightness": lambda: self.cover.max_brightness,
                "covermoving": lambda: snap.cover == CoverState.MOVING,
                "calibratorchanging": lambda: snap.calibrator == CalibratorState.NOT_READY,
                "devicestate": lambda: [{"Name": "CoverState", "Value": int(snap.cover)},
                                        {"Name": "CalibratorState", "Value": int(snap.calibrator)},
                                        {"Name": "Brightness", "Value": snap.brightness},
                                        {"Name": "TimeStamp", "Value": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}],
                }[m]()

    def put(self, m, p):
        if m not in self.PUT:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "calibratoron":
            try:
                b = int((p.get("brightness") or "").strip())
            except ValueError:
                raise BadRequest("Missing or invalid Brightness")
        try:
            if m == "opencover":
                self.cover.open()
            elif m == "closecover":
                self.cover.close()
            elif m == "haltcover":
                self.cover.halt()
            elif m == "calibratoron":
                self.cover.calibrator_on(b)
            else:
                self.cover.calibrator_off()
        except NotImplementedError as e:
            raise AlpacaError(NOT_IMPLEMENTED, str(e) or "Not supported by this panel")
        except ValueError as e:
            raise AlpacaError(INVALID_VALUE, str(e))
        except CoverBusy as e:
            raise AlpacaError(INVALID_OPERATION, str(e))
        except (RuntimeError, OSError) as e:
            raise AlpacaError(DRIVER_ERROR, str(e))
        return None


class CoverApp(App):
    ui_dir = UI

    def __init__(self, inst):
        self.inst = inst
        self.history = History(["position", "brightness", "heater", "voltage"])

    def status(self):
        i = self.inst
        link, snap = i.link, i.cover.snapshot()
        f = link.fields()
        num = lambda k: f.get(k) if isinstance(f.get(k), float) else None   # noqa: E731
        return {"name": i.alpaca.name, "port": i.port.device, "model": i.ident.get("model"),
                "firmware": i.ident.get("firmware"), "up": i.cover.up(), "error": link.last_error,
                "connected": i.alpaca.connected, "cover_state": int(snap.cover),
                "cover": CoverState(snap.cover).name.lower().replace("_", " "),
                "calibrator_state": int(snap.calibrator),
                "calibrator": CalibratorState(snap.calibrator).name.lower().replace("_", " "),
                "brightness": snap.brightness, "max_brightness": i.cover.max_brightness,
                "position": num("position"), "close_set": num("close_set"), "open_set": num("open_set"),
                "voltage": num("voltage"), "heater": None if num("heater") is None else int(num("heater")),
                "uid": i.alpaca.uid}

    def control(self, form):
        op, cover = form.get("op", ""), self.inst.cover
        log.info("panel page: %s %s", op, form.get("value", ""))
        try:
            if op == "open":
                cover.open()
                return 200, "Opening (the panel goes quiet for about 18 s while it moves)"
            if op == "close":
                cover.close()
                return 200, "Closing (the panel goes quiet for about 18 s while it moves)"
            if op == "light":
                b = int(form.get("value") or 0)
                if b <= 0:
                    cover.calibrator_off()
                    return 200, "Light off"
                cover.calibrator_on(b)
                return 200, f"Light on at {b}"
            if op == "heater":
                lvl = int(form.get("value") or 0)
                self.inst.link.set_heater(lvl)
                return 200, f"Heater {HEATER_NAMES.get(lvl, lvl)}"
            if op == "angle":
                which = form.get("which", "")
                deg = float(form.get("value") or "nan")
                self.inst.link.set_angle(which, deg)
                name = "Closed" if which == "close" else "Open"
                return 200, f"{name} angle set to {deg:g}° on the panel (the cover doesn't move)"
            if op == "jog":
                step = float(form.get("value") or "nan")
                which, new = self.inst.link.jog(step)
                name = "Open" if which == "open" else "Closed"
                return 200, (f"Jogging: the cap moves to {new:g}°, which becomes the {name.lower()} angle "
                             "(a few seconds; the panel takes no other command meanwhile)")
        except (ValueError, CoverBusy) as e:
            return 409, str(e)
        except (RuntimeError, OSError) as e:
            return 503, str(e)
        return 400, "op: open, close, light, heater, angle or jog"


class WandererCoverInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        unit = unit_of(ident, port)   # no serial number anywhere: the COM port
        n = ctx.unit_number()
        slug = "panel" if n == 0 else f"panel-{n + 1}"
        title = "Flat panel" if n == 0 else f"Flat panel #{n + 1}"
        super().__init__(driver, port, ident, slug, title)
        opener = ctx.opener_for(port, V4EC_BAUD)
        self.link = V4ecCoverLink(port.device, opener=opener)
        self.cover = CoverDevice(self.link)
        name = "WandererCover V4-EC" if n == 0 else f"WandererCover V4-EC #{n + 1}"   # clients key on the UniqueID
        self.alpaca = CoverCalibrator(self.cover, ctx.uid(unit, "cover"), name)
        self.app = CoverApp(self)
        self.srv = DeviceServer([self.alpaca], self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self._stop = threading.Event()
        self.cover.start()
        self.srv.start()
        self.local_port = self.srv.port
        threading.Thread(target=self._sample, name="panel-history", daemon=True).start()
        log.info("%s on %s (%s)", title, port.device, ident)

    def _sample(self):
        while not self._stop.wait(1.0):
            f = self.link.fields()
            if self.cover.up() and f:
                self.app.history.add(time.time(), {k: f.get(k) if isinstance(f.get(k), float) else None
                                                   for k in ("position", "brightness", "heater", "voltage")})

    def stop(self):
        self._stop.set()
        self.srv.stop()
        self.cover.stop()

    def up(self):
        return self.cover.up()

    def summary(self):
        if not self.up():
            return {"text": "no answer", "state": "bad"}
        snap, f = self.cover.snapshot(), self.link.fields()
        heater = f.get("heater")
        cover = CoverState(snap.cover).name.lower()
        if snap.cover == CoverState.ERROR and isinstance(f.get("position"), float):
            cover = f"cover error at {f['position']:.0f}°"     # e.g. stopped short of open: caught on something
        parts = [cover, "light off" if snap.brightness == 0 else f"light {snap.brightness}"]
        if isinstance(heater, float):
            parts.append(f"heater {HEATER_NAMES.get(int(heater), int(heater))}")
        state = {CoverState.MOVING: "warn", CoverState.ERROR: "bad"}.get(snap.cover, "ok")
        return {"text": " · ".join(parts), "state": state}


DRIVERS = [WandererCoverDriver]
