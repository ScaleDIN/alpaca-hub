"""Focuser #0 (focus), Focuser #1 (aux, when enabled) and Rotator #0 on the hub's DeviceServer, and the status
page's own endpoints (App.route). Ported from ncalpaca/alpaca.py (REFACTOR.md step 8) with the same answers: that
handler passed ConformU on the real NiteCrawler (Focuser #0 with focus max 60000, Rotator #0). The standalone
service's discovery, shutdown and port choice are gone: the hub does those.
"""
import json
import logging
import time

from hub.devserver import (DRIVER_ERROR, INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED, AlpacaDevice, AlpacaError,
                           App, BadRequest)

from . import VERSION
from . import protocol as P

log = logging.getLogger("nitecrawler.devices")

DEVICE_NAME = "NiteCrawler"
NAME_MAX = 40
CONTROL_HEADER = "X-NCAlpaca"      # the page's controls send it (a cross-site form can't)

FOC_GET = {"absolute", "ismoving", "maxincrement", "maxstep", "position", "stepsize", "tempcomp",
           "tempcompavailable", "temperature", "devicestate"}
FOC_PUT = {"tempcomp", "halt", "move"}
ROT_GET = {"canreverse", "ismoving", "mechanicalposition", "position", "reverse", "stepsize", "targetposition",
           "devicestate"}
ROT_PUT = {"reverse", "halt", "move", "moveabsolute", "movemechanical", "sync"}

DEV_SUFFIX = {"focus": " focuser", "aux": " aux", "rotator": " rotator"}
DEV_DESC = {"focus": "Moonlite NiteCrawler focus motor (stock firmware)",
            "aux": "Moonlite NiteCrawler auxiliary motor (stock firmware)",
            "rotator": "Moonlite NiteCrawler rotator (stock firmware)"}
DEV_AXIS = {"focus": P.FOCUS, "aux": P.AUX, "rotator": P.ROTATOR}


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


def _int(s):
    t = (s or "").strip()
    if not t.lstrip("+-").isdigit():
        raise ValueError
    return int(t)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Names:
    """The device name every Alpaca device of this NiteCrawler starts with (settings.device_name, else the default)."""

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
        return self.device_name


class _NCDevice(AlpacaDevice):
    interface_version = 4
    driver_version = VERSION
    actions = ("FindHome", "Encoders")

    def __init__(self, svc, names, key, uid, number):
        # not AlpacaDevice.__init__: name and driver_info are live (settings, firmware)
        self.svc, self.names, self.key, self.uid, self.number = svc, names, key, uid, number
        self.description = DEV_DESC[key]
        self.axis = DEV_AXIS[key]
        self.connected = False

    @property
    def name(self):
        return self.names.device_name + DEV_SUFFIX[self.key]

    @property
    def driver_info(self):
        lk = self.svc.link
        return f"NiteCrawler driver {VERSION}, NiteCrawler {lk.model or '?'} firmware {lk.firmware or 'unknown'}"

    def present(self):
        return self.key != "aux" or bool(self.svc.settings.aux_enabled)

    def set_connected(self, on):
        if on and not self.svc.link.up():
            raise AlpacaError(DRIVER_ERROR, "NiteCrawler not responding on the serial link" +
                              (f" ({self.svc.link.last_error})" if self.svc.link.last_error else ""))
        self.connected = on

    def action(self, name, parameters):
        return self.svc.action(name, parameters)

    def _need_connected(self):
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")


class FocuserDevice(_NCDevice):
    type, type_name = "focuser", "Focuser"

    def get(self, m, p):
        if m not in FOC_GET:
            raise KeyError(m)
        self._need_connected()
        svc, axis = self.svc, self.axis
        if m == "absolute":
            return True
        if m in ("maxstep", "maxincrement"):
            return svc.max_step(axis)
        if m == "ismoving":
            return svc.is_moving(axis)
        if m == "position":
            return svc.position(axis)
        if m == "stepsize":
            raise AlpacaError(NOT_IMPLEMENTED, "Step size is not known")
        if m in ("tempcomp", "tempcompavailable"):
            return False
        if m == "temperature":
            return svc.temperature()
        st = svc.status_or_raise()
        out = [{"Name": "IsMoving", "Value": st.axis(axis).moving or st.homing},
               {"Name": "Position", "Value": st.axis(axis).position}]
        if st.temperature is not None:
            out.append({"Name": "Temperature", "Value": st.temperature})
        out.append({"Name": "TimeStamp", "Value": _now_iso()})
        return out

    def put(self, m, p):
        if m not in FOC_PUT:
            raise KeyError(m)
        self._need_connected()
        if m == "tempcomp":
            try:
                _bool(p.get("tempcomp"))
            except ValueError:
                raise BadRequest("Missing or invalid TempComp")
            raise AlpacaError(NOT_IMPLEMENTED, "No temperature compensation")
        if m == "halt":
            self.svc.halt(self.axis)
            return None
        try:
            pos = _int(p.get("position"))
        except ValueError:
            raise BadRequest("Missing or invalid Position")
        self.svc.focuser_move(self.axis, pos)
        return None


class RotatorDevice(_NCDevice):
    type, type_name = "rotator", "Rotator"

    def get(self, m, p):
        if m not in ROT_GET:
            raise KeyError(m)
        self._need_connected()
        svc = self.svc
        if m == "canreverse":
            return True
        if m == "reverse":
            return svc.settings.rot_reverse
        if m == "stepsize":
            return 360.0 / svc.steps_per_rev
        if m == "ismoving":
            return svc.is_moving(P.ROTATOR)
        if m == "position":
            return round(svc.rot_position(), 4)
        if m == "mechanicalposition":
            return round(svc.rot_mechanical(), 4)
        if m == "targetposition":
            return round(svc.rot_target(), 4)
        st = svc.status_or_raise()
        return [{"Name": "IsMoving", "Value": st.rotator.moving or st.homing},
                {"Name": "MechanicalPosition", "Value": round(svc.rot_mechanical(st), 4)},
                {"Name": "Position", "Value": round(svc.rot_position(st), 4)},
                {"Name": "TimeStamp", "Value": _now_iso()}]

    def put(self, m, p):
        if m not in ROT_PUT:
            raise KeyError(m)
        self._need_connected()
        svc = self.svc
        if m == "reverse":
            try:
                svc.set_reverse(_bool(p.get("reverse")))
            except ValueError:
                raise BadRequest("Missing or invalid Reverse")
            return None
        if m == "halt":
            svc.halt(P.ROTATOR)
            return None
        try:
            v = _float(p.get("position"))
        except ValueError:
            raise BadRequest("Missing or invalid Position")
        {"move": svc.rot_move_relative, "moveabsolute": svc.rot_move_absolute,
         "movemechanical": svc.rot_move_mechanical, "sync": svc.rot_sync}[m](v)
        return None


def make_devices(svc, names, uid):
    """uid(key) -> UniqueID. Focuser #0 = focus, Focuser #1 = aux (only when enabled), Rotator #0."""
    return [FocuserDevice(svc, names, "focus", uid("focus"), 0), FocuserDevice(svc, names, "aux", uid("aux"), 1),
            RotatorDevice(svc, names, "rotator", uid("rotator"), 0)]


class NCApp(App):
    """The status page's data and its own endpoints (the page is unchanged from the standalone service)."""

    def __init__(self, svc, names, devices, ui_dir, port_name):
        self.svc, self.names, self.devices = svc, names, devices
        self.ui_dir = ui_dir
        self.history = svc.history
        self.port_name = port_name

    def status(self):
        svc = self.svc
        d = svc.status_dict()
        d.update({"clients": {dv.key: dv.connected for dv in self.devices if dv.present()},
                  "http_port": "", "settings_file": svc.settings.path, "device_name": self.names.device_name,
                  "device_name_set": svc.settings.device_name, "device_name_default": DEVICE_NAME})
        return d

    def route(self, req):
        path = req.path.rstrip("/")
        if req.command == "GET" and path == "/setup.json":
            # The hub found the NiteCrawler on this port and holds it: there's nothing to choose.
            return 200, json.dumps({"port": self.port_name, "ports": [
                {"device": self.port_name, "desc": "found by the hub"}]}), "application/json"
        if req.command != "POST" or not (path == "/control/move" or path.startswith("/setup/")):
            return None
        if req.headers.get(CONTROL_HEADER) != "1":
            return 403, f"Missing {CONTROL_HEADER} header", "text/plain; charset=utf-8"
        try:
            if path == "/control/move":
                code, msg = self._control(req)
            elif not req.local:
                code, msg = 403, "Settings can only be changed on the PC the NiteCrawler is plugged into"
            else:
                code, msg = self._setup(path, req.params)
        except AlpacaError as e:
            code, msg = 400, e.message
        return code, msg, "text/plain; charset=utf-8"

    def _control(self, req):
        """Moves from the status page, from this PC or a phone. While an Alpaca client is connected the page has to
        send override=1 (its Unlock button): moving under a running imaging sequence should be deliberate."""
        svc, p = self.svc, req.params
        op, axis_name = p.get("op") or "", p.get("axis") or ""
        axis = {"focus": P.FOCUS, "rotator": P.ROTATOR, "aux": P.AUX}.get(axis_name)
        if op not in ("halt", "haltall") and req.client and p.get("override") != "1":
            return 409, "An Alpaca client is connected: unlock the controls first"
        log.info("status page: %s %s %s", op, axis_name, p.get("value") or "")
        if op == "haltall":
            for a in P.AXES:
                svc.halt(a)
            return 200, "Stopped"
        if op == "home":
            svc.find_home(_int(p.get("value") or "3"))
            return 200, "Homing; the NiteCrawler answers nothing else until it has finished"
        if axis is None:
            return 400, "axis: focus, rotator or aux"
        if op == "halt":
            svc.halt(axis)
            return 200, "Stopped"
        try:
            v = _float(p.get("value"))
        except ValueError:
            return 400, "value: a number"
        if op == "goto":
            if axis == P.ROTATOR:
                svc.rot_move_absolute(v % 360.0)
            else:
                svc.focuser_move(axis, int(v))
            return 200, "Moving"
        if op == "step":
            if axis == P.ROTATOR:
                svc.rot_move_relative(v)
            else:
                svc.focuser_move(axis, svc.position(axis) + int(v))
            return 200, "Moving"
        if op == "setpos":  # Moonlite's SET POS: xSP n#, the motor doesn't move
            if axis == P.ROTATOR:
                svc.rot_set_steps(int(v))
            else:
                if not 0 <= int(v) <= svc.max_step(axis):
                    return 400, f"Position must be 0 to {svc.max_step(axis)}"
                svc._do(svc.link.sync, axis, int(v))
                svc.targets[axis] = None
            return 200, "Position set"
        if op == "sync":
            if axis == P.ROTATOR:
                svc.rot_sync(v % 360.0)
            else:
                svc._do(svc.link.sync, axis, int(v))
            return 200, "Synced"
        return 400, "op: goto, step, sync, halt, haltall or home"

    def _setup(self, path, p):
        svc = self.svc
        s = svc.settings
        if path == "/setup/port":
            return 400, "The hub found the NiteCrawler on this port and keeps it"
        if path == "/setup/name":
            self.names.set(p.get("name"))
            return 200, "Saved. Alpaca programs show it the next time they connect"
        if path == "/setup/motion":
            try:
                fmax, amax = _int(p.get("focus_max")), _int(p.get("aux_max"))
                rev = p.get("rot_reverse") == "1"
                aux = p.get("aux_enabled") == "1"
            except ValueError:
                return 400, "Whole numbers please"
            for n, v in (("Focus maximum", fmax), ("Aux maximum", amax)):
                if not 1 <= v <= P.POS_MAX:
                    return 400, f"{n}: 1 to {P.POS_MAX}"
            bls = {}
            for k, n in (("focus_backlash", "Focus backlash"), ("aux_backlash", "Aux backlash")):
                if p.get(k) not in (None, ""):   # optional: older pages don't send it
                    try:
                        bls[k] = _int(p.get(k))
                    except ValueError:
                        return 400, "Whole numbers please"
                    if not 0 <= bls[k] <= 100000:
                        return 400, f"{n}: 0 (off) to 100000 steps"
            s.focus_max, s.aux_max, s.aux_enabled = fmax, amax, aux
            for k, v in bls.items():
                setattr(s, k, v)
            svc.set_reverse(rev)
            svc._save()
            return 200, "Saved"
        if path == "/setup/speed":
            out = []
            for name, axis in (("focus", P.FOCUS), ("rotator", P.ROTATOR), ("aux", P.AUX)):
                v = p.get(name)
                if v:
                    try:
                        svc.set_step_delay(axis, _int(v))
                    except ValueError:
                        return 400, f"{name}: a whole number"
                    out.append(name)
            return 200, "Saved" if out else "No change"
        if path == "/setup/display":
            try:
                b, sl = _int(p.get("brightness")), _int(p.get("sleep"))
            except ValueError:
                return 400, "brightness and sleep: 0 to 255"
            dim = None
            if p.get("dim_after") not in (None, ""):   # optional: the patched firmware's PW; older pages don't send it
                try:
                    dim = _int(p.get("dim_after"))
                except ValueError:
                    dim = -1
                if not 0 <= dim <= P.DIM_DELAY_MAX:
                    return 400, f"Dim after: 0 (never) to {P.DIM_DELAY_MAX} seconds"
            svc.set_brightness(b, sl)
            if dim is not None:
                try:
                    svc.set_dim_after(dim)
                except AlpacaError as e:
                    return 400, f"{e.message}. Brightness was sent."
            return 200, "Sent to the NiteCrawler"
        if path == "/setup/encoders":
            svc.set_encoders(p.get("on") == "1")
            return 200, "Sent to the NiteCrawler"
        return 404, "Not found"

