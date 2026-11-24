"""Example driver: a USB serial thermometer, served as Alpaca ObservingConditions (Temperature), with a status page.

The parts every driver has (docs/WRITING-DRIVERS.md):
- a Driver: how the hub recognises the device (here by listening only: it talks unasked) and how to start it;
- an Instance per device found: its link, its settings, its Alpaca devices on a DeviceServer, its page;
- DRIVERS = [...] at the end, which the hub looks for.
"""
import json
import logging
import os
import threading
import time

from hub.api import Driver, Instance, unit_of
from hub.devserver import (INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED, VALUE_NOT_SET, AlpacaDevice, AlpacaError,
                           App, BadRequest, DeviceServer, History)

from . import VERSION
from .link import ThermoLink
from .protocol import BAUD, parse_line

log = logging.getLogger("thermo")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
# pid.codes' test VID:PID. A real driver lists its device's USB IDs (the hub only shows it ports with these).
USB_IDS = frozenset({(0x1209, 0x0001)})


class ThermoDriver(Driver):
    key = "thermo"                       # = the folder name and driver.json's key; never change it once released
    title = "Example thermometer"
    usb_ids = USB_IDS
    listen_baud = BAUD                   # it talks unasked: recognise() gets what it said; nothing is ever sent
    simulators = {"thermo": lambda i: (_sim(serial=f"T{i + 1}"), (0x1209, 0x0001), "", "Simulated thermometer")}

    def recognise(self, data):
        """Bytes the hub heard on a port at listen_baud -> identity (plain values: it crosses a pipe), or None."""
        for line in data.splitlines():
            got = parse_line(line)
            if got:
                return {"serial": got[0]}   # "serial": the unit's own ID, so two thermometers are told apart
        return None

    def start(self, ctx, port, ident):
        return ThermoInstance(self, ctx, port, ident)


def _sim(**kw):
    from .sim import SimThermo
    return SimThermo(**kw)


class Settings:
    """This unit's settings, in the file the hub gives it (HubContext.settings_path)."""
    DEFAULTS = {"offset_c": 0.0, "name": ""}

    def __init__(self, path):
        self.path = path
        self.values = dict(self.DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                self.values.update({k: v for k, v in json.load(f).items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.values, f, indent=2)
        os.replace(tmp, self.path)


class ThermoInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        unit = unit_of(ident, port)              # its serial number, else the USB chip's, else the COM port
        n = ctx.unit_number()                    # 0 for the first thermometer, 1 for the second… (the hub's)
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        super().__init__(driver, port, ident, slug, "Thermometer" + ("" if n == 0 else f" #{n + 1}"))
        self.settings = Settings(ctx.settings_path(unit))
        self.link = ThermoLink(port.device, ctx.opener_for(port, BAUD))
        self.sensor = TemperatureDevice(self, ctx.uid(unit, "sensor"),
                                        "Example thermometer" + ("" if n == 0 else f" #{n + 1}"))
        self.app = ThermoApp(self)
        self.srv = DeviceServer([self.sensor], self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self.srv.start()
        self.local_port = self.srv.port          # the hub forwards /<slug>/ and the Alpaca calls here
        self.link.start()
        self.app.start_history()
        log.info("%s on %s (serial %s)", self.title, port.device, ident.get("serial"))

    def temperature(self):
        t = self.link.temperature
        return None if t is None else round(t + float(self.settings.values["offset_c"]), 2)

    def up(self):
        return self.link.up()

    def summary(self):
        if not self.up():
            return {"text": "no answer", "state": "bad"}
        return {"text": f"{self.temperature():.1f} °C", "state": "ok"}

    def stop(self):
        self.app.stop_history()
        self.srv.stop()
        self.link.stop()


# ------------------------------------------------------------------ Alpaca: ObservingConditions #0, Temperature only
SENSORS = ("CloudCover", "DewPoint", "Humidity", "Pressure", "RainRate", "SkyBrightness", "SkyQuality",
           "SkyTemperature", "StarFWHM", "Temperature", "WindDirection", "WindGust", "WindSpeed")


class TemperatureDevice(AlpacaDevice):
    type, type_name, interface_version = "observingconditions", "ObservingConditions", 2
    driver_info = "Example thermometer driver for the Alpaca Hub"
    driver_version = VERSION
    GET = {"averageperiod", "devicestate", "sensordescription", "timesincelastupdate"} | {s.lower() for s in SENSORS}

    def __init__(self, inst, uid, name):
        super().__init__(uid, name, "Example USB serial thermometer")
        self.inst = inst

    def set_connected(self, on):
        if on and not self.inst.up():
            raise AlpacaError(0x500, "The thermometer isn't answering on its serial port")
        self.connected = on

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)                    # DeviceServer answers 400 "Unknown member"
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "averageperiod":
            return 0.0
        if m == "temperature":
            t = self.inst.temperature()
            if t is None:
                raise AlpacaError(VALUE_NOT_SET, "No reading yet")
            return t
        if m == "devicestate":
            t = self.inst.temperature()
            return [] if t is None else [{"Name": "Temperature", "Value": t},
                                         {"Name": "TimeStamp", "Value": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                                                      time.gmtime())}]
        if m in ("sensordescription", "timesincelastupdate"):
            n = p.get("sensorname")
            if n is None:
                raise BadRequest("Missing SensorName")
            if n and n.lower() not in {s.lower() for s in SENSORS}:
                raise AlpacaError(INVALID_VALUE, "Unknown sensor name")
            if n and n.lower() != "temperature":
                raise AlpacaError(NOT_IMPLEMENTED, "Sensor not available on this device")
            if m == "timesincelastupdate":
                age = self.inst.link.age()
                if age is None:
                    raise AlpacaError(VALUE_NOT_SET, "No reading yet")
                return round(age, 2)
            if not n:
                raise AlpacaError(INVALID_VALUE, "SensorName required")
            return "Example USB serial thermometer"
        raise AlpacaError(NOT_IMPLEMENTED, "Sensor not available on this device")

    def put(self, m, p):
        if m not in ("averageperiod", "refresh"):
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "averageperiod":
            try:
                v = float(p.get("averageperiod") or "")
            except ValueError:
                raise BadRequest("Missing or invalid AveragePeriod")
            if v != 0:
                raise AlpacaError(INVALID_VALUE, "Only an average period of 0 is supported")
        return None


# ------------------------------------------------------------------ the status page's data and its control
class ThermoApp(App):
    ui_dir = UI

    def __init__(self, inst):
        self.inst = inst
        self.history = History(["temp"])          # /history.json for the chart: 1 s for 10 min, 10 s (with min/max) for 24 h
        self._t = None

    def start_history(self):
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._sample, name="thermo-history", daemon=True)
        self._t.start()

    def stop_history(self):
        if self._t:
            self._stop.set()

    def _sample(self):
        while not self._stop.wait(1.0):
            if self.inst.up():
                self.history.add(time.time(), {"temp": self.inst.temperature()})

    def status(self):
        """/status.json for the page (DeviceServer adds build, uptime, simulated, client, local, urls)."""
        i = self.inst
        return {"up": i.up(), "port": i.port.device, "serial": i.link.serial, "temperature": i.temperature(),
                "raw": i.link.temperature, "offset_c": i.settings.values["offset_c"], "lines": i.link.lines,
                "error": i.link.last_error, "name": i.sensor.name, "uid": i.sensor.uid}

    def control(self, form):
        """POST /control from the page (DeviceServer has checked it came from the page; it refuses while a client is
        connected unless the page sent override=1)."""
        if form.get("op") == "offset":
            try:
                v = float(form.get("value") or "")
            except ValueError:
                return 400, "offset: a number of °C"
            if not -10 <= v <= 10:
                return 400, "offset: -10 to 10 °C"
            self.inst.settings.values["offset_c"] = v
            self.inst.settings.save()
            return 200, f"Offset {v:g} °C saved"
        return 400, "op: offset"


DRIVERS = [ThermoDriver]
