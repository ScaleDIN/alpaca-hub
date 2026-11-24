"""Switch #0 (outputs, DC2 voltage, dew heater) and ObservingConditions #0 (the DHT22) on the hub's DeviceServer, and
the status page's own endpoints (App.route). Ported from wbstock/alpaca.py (REFACTOR.md step 9) with the same
answers: that handler passed ConformU on the real box (Switch #0: no errors; ObservingConditions #0 clean). The
standalone service's discovery, shutdown, port choice and its optional flat panel are gone: the hub does those, and
the panel is its own driver (drivers/wcover).
"""
import json
import logging
import os
import time

from hub.devserver import (INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED, VALUE_NOT_SET, AlpacaDevice, AlpacaError,
                           App, BadRequest)

from . import VERSION
from .dew import DewSettings
from .service import DEFAULT_GAUGES, GAUGE_NEEDS, GAUGES, SW_FIXED, TUNING, clean_gauges

log = logging.getLogger("wbox.devices")

DEVICE_NAME = "WandererBox Plus V3"
NAME_MAX = 40   # characters in the base name; " outputs"/" sensors" (and " SIMULATED") are added
CONTROL_HEADER = "X-WBStock"     # the page's script sends it; a cross-site form can't
LOCAL_SETTINGS = {"/setup/port", "/setup/gauges", "/setup/dew", "/setup/probes", "/setup/name", "/setup/box"}

SW_GET = {"maxswitch", "devicestate", "canwrite", "canasync", "getswitchname", "getswitchdescription",
          "minswitchvalue", "maxswitchvalue", "switchstep", "statechangecomplete", "getswitch", "getswitchvalue"}
SW_PUT = {"setswitch", "setswitchvalue", "setswitchname", "setasync", "setasyncvalue", "cancelasync"}
OC_SENSORS = ("CloudCover", "DewPoint", "Humidity", "Pressure", "RainRate", "SkyBrightness", "SkyQuality",
              "SkyTemperature", "StarFWHM", "Temperature", "WindDirection", "WindGust", "WindSpeed")
OC_IMPLEMENTED = {"temperature", "humidity", "dewpoint"}
OC_GET = {"averageperiod", "devicestate", "sensordescription", "timesincelastupdate"} | {s.lower() for s in OC_SENSORS}
OC_PUT = {"averageperiod", "refresh"}


def _bool(s):
    t = (s or "").strip().lower()
    if t == "true":
        return True
    if t == "false":
        return False
    raise ValueError


def _float(s):
    v = float((s or "").strip())
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError
    return v


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Names:
    """The base name both Alpaca devices of this box start with (settings.device_name, else the default). The
    UniqueIDs don't change with it."""

    def __init__(self, svc, simulated=False):
        self.svc, self.simulated = svc, simulated

    @property
    def device_name(self):
        return (self.svc.settings.device_name or DEVICE_NAME) + (" SIMULATED" if self.simulated else "")

    def set(self, name):
        name = " ".join((name or "").split())
        if len(name) > NAME_MAX:
            raise AlpacaError(INVALID_VALUE, f"Name: at most {NAME_MAX} characters")
        if any(ord(c) < 32 or c in '<>"' for c in name):
            raise AlpacaError(INVALID_VALUE, "Name: letters, digits, spaces and simple punctuation")
        self.svc.settings.device_name = "" if name == DEVICE_NAME else name
        self.svc._save()
        log.info("Alpaca device name -> %s", self.device_name)
        return self.device_name


class _BoxDevice(AlpacaDevice):
    driver_version = VERSION
    suffix = ""

    def __init__(self, svc, names, uid, description):
        # not AlpacaDevice.__init__: name and driver_info are live (settings, firmware)
        self.svc, self.names, self.uid, self.description = svc, names, uid, description
        self.number = 0
        self.connected = False

    @property
    def name(self):
        return self.names.device_name + self.suffix

    @property
    def driver_info(self):
        st = self.svc.link.status
        return f"WandererBox driver {VERSION}, box firmware {st.fw if st else 'unknown'}"

    def set_connected(self, on):
        if on and not self.svc.link.up():
            raise AlpacaError(0x500, "Box not responding on the serial link" +
                              (f" ({self.svc.link.last_error})" if self.svc.link.last_error else ""))
        self.connected = on

    def _need_connected(self):
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")


class SwitchDevice(_BoxDevice):
    type, type_name, interface_version = "switch", "Switch", 3
    suffix = " outputs"

    @property
    def actions(self):
        return tuple(self.svc.ACTIONS)

    def action(self, name, parameters):
        return self.svc.action(name, parameters)

    def _id(self, p):
        try:
            i = int((p.get("id") or "").strip())
        except ValueError:
            raise BadRequest("Missing or invalid Id")
        if not 0 <= i < self.svc.switch_count():
            raise AlpacaError(INVALID_VALUE, "Switch Id out of range")
        return i

    def get(self, m, p):
        if m not in SW_GET:
            raise KeyError(m)
        self._need_connected()
        svc = self.svc
        if m == "maxswitch":
            return svc.switch_count()
        if m == "devicestate":
            svc._status_or_raise()
            out = []
            for i in range(svc.switch_count()):
                try:
                    v = svc.switch_value(i)
                except AlpacaError:
                    continue
                out.append({"Name": f"GetSwitch{i}", "Value": v > svc.switch_def(i).min})
                out.append({"Name": f"GetSwitchValue{i}", "Value": v})
            out.append({"Name": "TimeStamp", "Value": _now_iso()})
            return out
        i = self._id(p)
        d = svc.switch_def(i)
        if m == "canwrite":
            return d.writable
        if m == "canasync":
            return False
        if m == "getswitchname":
            return d.name
        if m == "getswitchdescription":
            return d.desc
        if m == "minswitchvalue":
            return d.min
        if m == "maxswitchvalue":
            return d.max
        if m == "switchstep":
            return d.step
        if m == "statechangecomplete":
            raise AlpacaError(NOT_IMPLEMENTED, "Switches are synchronous")
        v = svc.switch_value(i)
        return v > d.min if m == "getswitch" else v

    def put(self, m, p):
        if m not in SW_PUT:
            raise KeyError(m)
        self._need_connected()
        svc = self.svc
        i = self._id(p)
        if m == "setswitch":
            try:
                on = _bool(p.get("state"))
            except ValueError:
                raise BadRequest("Missing or invalid State")
            d = svc.switch_def(i)
            svc.write_switch(i, d.max if on else d.min)
            return None
        if m == "setswitchvalue":
            try:
                v = _float(p.get("value"))
            except ValueError:
                raise BadRequest("Missing or invalid Value")
            svc.write_switch(i, v)
            return None
        if m == "setswitchname":
            raise AlpacaError(NOT_IMPLEMENTED, "Switch names are fixed")
        raise AlpacaError(NOT_IMPLEMENTED, "Switches are synchronous")


class SensorsDevice(_BoxDevice):
    type, type_name, interface_version = "observingconditions", "ObservingConditions", 2
    suffix = " sensors"

    def get(self, m, p):
        if m not in OC_GET:
            raise KeyError(m)
        self._need_connected()
        svc = self.svc
        if m == "averageperiod":
            return 0.0
        sensor = (p.get("sensorname") or "").lower()
        if not svc.settings.use_ambient and (m in OC_IMPLEMENTED or (
                m in ("sensordescription", "timesincelastupdate") and sensor in OC_IMPLEMENTED)):
            # DHT22 set to not used: like a sensor this device doesn't have, so clients leave it out
            raise AlpacaError(NOT_IMPLEMENTED, "The DHT22 is set to not used on the status page")
        if m in OC_IMPLEMENTED:
            st = svc._status_or_raise()
            if not st.ambient_ok:
                raise AlpacaError(VALUE_NOT_SET, "Ambient sensor missing or faulty")
            return {"temperature": st.ambient, "humidity": st.humidity, "dewpoint": round(st.dewpoint, 2)}[m]
        if m == "devicestate":
            st = svc._status_or_raise()
            if not st.ambient_ok:
                return []
            return [{"Name": "Temperature", "Value": st.ambient}, {"Name": "Humidity", "Value": st.humidity},
                    {"Name": "DewPoint", "Value": round(st.dewpoint, 2)}, {"Name": "TimeStamp", "Value": _now_iso()}]
        if m in ("sensordescription", "timesincelastupdate"):
            n = p.get("sensorname")
            if n is None:
                raise BadRequest("Missing SensorName")
            if n and n.lower() not in {s.lower() for s in OC_SENSORS}:
                raise AlpacaError(INVALID_VALUE, "Unknown sensor name")
            if n and n.lower() not in OC_IMPLEMENTED:
                raise AlpacaError(NOT_IMPLEMENTED, "Sensor not available on this device")
            if m == "timesincelastupdate":
                age = svc.link.status_age()
                if age is None:
                    raise AlpacaError(VALUE_NOT_SET, "No reading yet")
                return round(age, 2)
            if not n:
                raise AlpacaError(INVALID_VALUE, "SensorName required")
            return ("Calculated from the DHT22 (Magnus formula)" if n.lower() == "dewpoint"
                    else "DHT22 inside the WandererBox Plus V3")
        raise AlpacaError(NOT_IMPLEMENTED, "Sensor not available on this device")

    def put(self, m, p):
        if m not in OC_PUT:
            raise KeyError(m)
        self._need_connected()
        if m == "averageperiod":
            try:
                v = _float(p.get("averageperiod"))
            except ValueError:
                raise BadRequest("Missing or invalid AveragePeriod")
            if v != 0:
                raise AlpacaError(INVALID_VALUE, "Only an average period of 0 is supported")
        return None   # refresh: the box sends a reading every second anyway


def make_devices(svc, names, uid):
    """uid(key) -> UniqueID ("switch", "oc": the keys the hub API 1 IDs were made from)."""
    return [SwitchDevice(svc, names, uid("switch"), "WandererBox Plus V3 power outputs and dew heater (stock firmware)"),
            SensorsDevice(svc, names, uid("oc"), "WandererBox Plus V3 ambient sensor (DHT22)")]


class BoxApp(App):
    """The status page's data and its own endpoints (the page is unchanged from the standalone service)."""
    history = None      # the box keeps its own history (history.json and .zip are its endpoints, below)

    def __init__(self, svc, names, devices, ui_dir, port_name):
        self.svc, self.names, self.ui_dir, self.port_name = svc, names, ui_dir, port_name
        self.switch = next(d for d in devices if d.type == "switch")
        self.devices = devices
        self.started = time.time()

    def status(self):
        svc = self.svc
        d = svc.status_dict()
        d.update({"up": round(time.time() - self.started), "switch_client": self.switch.connected,
                  "bind": "", "http_port": "", "settings": svc.settings.path, "history": svc.history.info(),
                  "device_name": self.names.device_name, "device_name_set": svc.settings.device_name,
                  "device_name_default": DEVICE_NAME})
        return d

    def route(self, req):
        path = req.path.rstrip("/") or "/"
        if req.command in ("GET", "HEAD"):
            if path == "/setup.json":
                return 200, json.dumps(self._setup_json()), "application/json"
            if path == "/history.json":
                try:
                    since = float(req.params["since"]) if req.params.get("since") else None
                except ValueError:
                    return 400, "since: unix seconds", "text/plain; charset=utf-8"
                h = self.svc.history
                d = h.fine(since) if req.params.get("res") == "s" else h.coarse(since)
                return 200, json.dumps(d, separators=(",", ":")), "application/json"
            if path == "/history.zip":
                name = "wbstock-history-" + time.strftime("%Y-%m-%d") + ".zip"
                return 200, self.svc.history.zip_bytes(), "application/zip", {
                    "Content-Disposition": f'attachment; filename="{name}"'}
            return None
        if req.command != "POST":
            return None
        try:
            r = self._post(path, req)
        except AlpacaError as e:
            r = (400, e.message)
        if r is None:
            return None
        if len(r) == 3:                         # a redirect
            return r[0], "", "text/plain", r[2]
        return r[0], r[1], "text/plain; charset=utf-8"

    def _post(self, path, req):
        p, svc = req.params, self.svc
        # Settings change only on the PC the box is plugged into (no password to forget, and a shared network can't
        # rename or retune the box). The Controls (/control/box) work from phones too.
        if path in LOCAL_SETTINGS and not req.local:
            return 403, "Settings can only be changed on the PC the box is plugged into"
        if path == "/setup/port":
            return 400, "The hub found the box on this port and keeps it"
        if path == "/setup/gauges":
            if p.get("reset") is not None:
                svc.set_gauges(DEFAULT_GAUGES)
                svc.set_missing("hold", 0.0)
            else:
                # A gauge whose probe is set to not used is greyed out on the page and not sent: keep its choice.
                kept = [g for g in clean_gauges(svc.settings.gauges)
                        if not all(svc.probe_used(x) for x in GAUGE_NEEDS.get(g, ()))]
                svc.set_gauges(kept + [v for k, v in req.pairs if k == "gauge"])
                if p.get("missing"):
                    mv = (p.get("missing_value") or "").strip()
                    svc.set_missing(p.get("missing"), mv if mv else None)
            return 303, "", {"Location": "../"}     # back to the page (/<slug>/ behind the hub)
        if path in ("/setup/history", "/control/open-folder", "/control/delete-history"):
            return self._local_control(path, req)
        if path in ("/control/box", "/setup/dew", "/setup/probes", "/setup/name", "/setup/box"):
            if req.headers.get(CONTROL_HEADER) != "1":
                return 403, f"Missing {CONTROL_HEADER} header"
            if path == "/setup/box":
                return self._box_setup(p)
            if path == "/setup/probes":
                return self._probes_setup(p)
            if path == "/setup/name":
                self.names.set(p.get("name"))
                return 200, "Saved. Alpaca programs show it the next time they connect"  # two lines at most
            return self._box_control(req) if path == "/control/box" else self._dew_setup(p)
        return None

    def _local_control(self, path, req):
        """History controls: only from this PC, and only from the status page's script."""
        if not req.local:
            return 403, "Only on the PC the box is plugged into"
        if req.headers.get(CONTROL_HEADER) != "1":
            return 403, f"Missing {CONTROL_HEADER} header"
        h = self.svc.history
        if path == "/setup/history":
            try:
                days = int(req.params.get("days") or "")
            except ValueError:
                return 400, "days: a whole number of days"
            if not 1 <= days <= 365:
                return 400, "days: 1 to 365"
            self.svc.set_history_days(days)
            return 200, f"History kept for {days} days"
        if path == "/control/open-folder":
            if not hasattr(os, "startfile"):
                return 501, "Only on Windows"
            os.makedirs(h.folder, exist_ok=True)
            os.startfile(h.folder)
            return 200, "Opened " + h.folder
        done, failed = h.delete_all()
        if failed:
            return 500, f"Deleted {done} file(s); {failed} could not be deleted (open in another program?)"
        return 200, f"Deleted {done} history file{'' if done == 1 else 's'}; the chart starts over."

    def _box_control(self, req):
        """A switch (ids 0-7) from the status page, from this PC or a phone. While an Alpaca client has the Switch
        connected, the page has to say override=1 (its Unlock button): changing outputs under a running imaging
        sequence should be a deliberate act."""
        svc, p = self.svc, req.params
        try:
            i, v = int(p.get("id") or ""), float(p.get("value") or "")
        except ValueError:
            return 400, "id and value: numbers"
        if not 0 <= i < SW_FIXED:
            return 400, f"id: 0 to {SW_FIXED - 1}"
        if self.switch.connected and p.get("override") != "1":
            return 409, ("An Alpaca client is connected to the Switch: change it there, "
                         "or unlock the controls on the status page")
        name = svc.switch_def(i).name
        log.info("status page: %s -> %g", name, v)
        svc.write_switch(i, v)  # waits for the box to confirm; AlpacaError -> 400 with its message
        return 200, f"{name}: done"

    def _box_setup(self, p):
        """WSM firmware settings stored in the box (ramp, ceiling) and clearing its faults. Local only."""
        svc = self.svc
        if p.get("action") == "clear_faults":
            svc.clear_faults()
            return 200, "Faults cleared in the box"
        done = []
        if (p.get("dc2_max") or "").strip():
            ceil, lowered = svc.set_dc2_ceiling(p.get("dc2_max"))   # AlpacaError -> 400 with its message
            done.append(f"DC2 ceiling {ceil:g} V" + (f" (setpoint lowered to {lowered:g} V)" if lowered else ""))
        if (p.get("dc2_slew") or "").strip():
            done.append(f"DC2 ramp {svc.set_dc2_slew(p.get('dc2_slew')):g} V/s")
        if not done:
            return 400, "Nothing to save"
        return 200, ", ".join(done) + ": saved in the box"

    def _probes_setup(self, p):
        """Which temperature probes are used. Not locked: Alpaca clients can't change it, like the dew tuning."""
        try:
            lens, amb = (_bool({"1": "true", "0": "false"}.get(p.get(k) or "", p.get(k) or ""))
                         for k in ("lens", "ambient"))
        except ValueError:
            return 400, "lens and ambient: 1 or 0"
        changed = self.svc.set_probes(lens, amb)
        return 200, "Saved" if changed else "No change"

    def _dew_setup(self, p):
        svc = self.svc
        reset = p.get("reset") == "1"
        values = {k: p.get(k) for k in TUNING if (p.get(k) or "").strip()}
        changed = svc.set_dew_tuning(values, reset=reset)
        msg = ("Back to the defaults" if reset else "Saved") if changed or reset else "No change"
        d = svc.settings.dew
        capped = []   # short enough for the page's two-line message at 375 px
        if "manual_pct" in changed:
            capped.append(f"manual power lowered to {d.manual_pct:g} %")
        if d.fallback_pct > d.max_pct and ({"fallback_pct", "max_pct"} & set(changed) or reset):
            capped.append(f"fallback capped at {d.max_pct:g} %")
        if capped:
            msg += ". Power limit: " + ", ".join(capped)
        return 200, msg

    def _setup_json(self):
        svc = self.svc
        cur = self.port_name
        chosen = set(clean_gauges(svc.settings.gauges))
        controls = [dict(zip(("id", "name", "desc", "min", "max", "step"), (i, d.name, d.desc, d.min, d.max, d.step)))
                    for i, d in ((i, svc.switch_def(i)) for i in range(SW_FIXED))]
        return {"port": cur, "ports": [cur], "gauges": [
            {"key": k, "name": v[0], "desc": v[1], "on": k in chosen, "needs": list(GAUGE_NEEDS.get(k, ()))}
            for k, v in GAUGES.items()],
            "missing": {"mode": svc.settings.missing_gauge, "value": svc.settings.missing_value},
            "controls": controls, "tuning": {k: {"min": lo, "max": hi, "default": getattr(DewSettings(), k)}
                                             for k, (lo, hi) in TUNING.items()}}
