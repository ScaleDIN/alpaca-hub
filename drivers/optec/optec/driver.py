"""Optec focuser hubs: FocusLynx / ThirdLynx (one or two Alpaca Focusers) and Gemini / Hercules (a Focuser and a
Rotator), with the optec package's links, and a status page (tab) per hub.

A driver folder (drivers/optec, REFACTOR.md step 10), one process for both drivers. Written for the hub (2026-11-20)
from Optec's firmware and drivers; see __init__.py. Not yet run against real hardware. The hub never sends Optec
configuration commands: device type, max position, temperature compensation and backlash stay as Optec's FocusLynx
Commander / Gemini Commander set them.
"""
import json
import logging
import os
import threading
import time

from hub.api import Driver, Instance, unit_of
from hub.devserver import (DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED,
                           AlpacaDevice, AlpacaError, App, BadRequest, DeviceServer, History)

from . import VERSION
from . import gemini as G
from . import lynx as L
from .wire import NotReady, OptecError, as_bool, as_int

log = logging.getLogger("optec.driver")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _int_param(p, name):
    try:
        return int(str(p.get(name.lower(), "")).strip())
    except ValueError:
        raise BadRequest(f"Missing or invalid {name}")


def _float_param(p, name):
    try:
        v = float(str(p.get(name.lower(), "")).strip())
    except ValueError:
        raise BadRequest(f"Missing or invalid {name}")
    if v != v or v in (float("inf"), float("-inf")):
        raise BadRequest(f"Missing or invalid {name}")
    return v


def _bool_param(p, name):
    v = str(p.get(name.lower(), "")).strip().lower()
    if v not in ("true", "false"):
        raise BadRequest(f"Missing or invalid {name}")
    return v == "true"


def _guard(fn):
    """Run a device command, turning the optec errors into Alpaca errors."""
    try:
        return fn()
    except NotReady as e:
        raise AlpacaError(INVALID_OPERATION, str(e))
    except ValueError as e:
        raise AlpacaError(INVALID_VALUE, str(e))
    except OptecError as e:
        busy = any(w in e.text for w in ("Moving", "Homing", "Homed", "homing", "Sleeping"))
        raise AlpacaError(INVALID_OPERATION if busy else DRIVER_ERROR, str(e))
    except (RuntimeError, OSError) as e:
        raise AlpacaError(DRIVER_ERROR, str(e))


# ---------------------------------------------------------------- one focus axis, whichever hub it is on
class FocusAxis:
    """What an Alpaca Focuser needs from a focus motor."""

    def __init__(self, unit, kind, key):
        self.unit, self.kind, self.key = unit, kind, key      # kind "lynx" (key F1/F2) or "gemini" (key "F")

    def status(self):
        return self.unit.snapshot(self.key) if self.kind == "lynx" else self.unit.focus

    def max_step(self):
        return self.unit.max_pos(self.key) if self.kind == "lynx" else (self.unit.focus_max() or L.MAX_LIMIT)

    def moving(self):
        return self.unit.is_moving(self.key)

    def move(self, pos):
        return self.unit.move(self.key, pos) if self.kind == "lynx" else self.unit.focus_move(pos)

    def halt(self):
        self.unit.halt(self.key)

    def home(self):
        self.unit.home(self.key)


class Focuser(AlpacaDevice):
    type, type_name, interface_version = "focuser", "Focuser", 4
    driver_version = VERSION
    GET = {"absolute", "ismoving", "maxincrement", "maxstep", "position", "stepsize", "tempcomp",
           "tempcompavailable", "temperature", "devicestate"}
    PUT = {"halt", "move", "tempcomp"}

    def __init__(self, axis, uid, name, description, number=0):
        super().__init__(uid, name, description)
        self.axis = axis
        self.number = number
        self.driver_info = f"{description}, alpaca-hub (Optec serial command set)"

    def _status(self):
        st = self.axis.status()
        if st is None or not self.axis.unit.up():
            raise AlpacaError(DRIVER_ERROR, "The Optec hub is not answering")
        return st

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "absolute":
            return True
        if m in ("maxstep", "maxincrement"):
            return self.axis.max_step()
        if m in ("tempcomp", "tempcompavailable"):
            return False
        if m == "stepsize":
            raise AlpacaError(NOT_IMPLEMENTED, "Step size is not known")
        st = self._status()
        if m == "position":
            return st.position
        if m == "ismoving":
            return self.axis.moving()
        if m == "temperature":
            if st.temperature is None:
                raise AlpacaError(NOT_IMPLEMENTED, "No temperature probe attached")
            return st.temperature
        out = [{"Name": "IsMoving", "Value": self.axis.moving()}, {"Name": "Position", "Value": st.position}]
        if st.temperature is not None:
            out.append({"Name": "Temperature", "Value": st.temperature})
        out.append({"Name": "TimeStamp", "Value": _now_iso()})
        return out

    def put(self, m, p):
        if m not in self.PUT:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "tempcomp":
            _bool_param(p, "TempComp")
            raise AlpacaError(NOT_IMPLEMENTED, "Temperature compensation is not offered through this driver")
        if m == "halt":
            _guard(self.axis.halt)
            return None
        pos = _int_param(p, "Position")
        _guard(lambda: self.axis.move(pos))
        return None


# ---------------------------------------------------------------- the Gemini/Hercules rotator
class RotatorSettings:
    """The rotator's sky offset (Alpaca Sync), kept by the hub, per unit."""

    def __init__(self, path):
        self.path = path
        self.sky_offset = 0.0
        try:
            with open(path, encoding="utf-8") as f:
                self.sky_offset = float(json.load(f).get("sky_offset", 0.0))
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sky_offset": self.sky_offset}, f, indent=2)
        os.replace(tmp, self.path)


class Rotator(AlpacaDevice):
    type, type_name, interface_version = "rotator", "Rotator", 4
    driver_version = VERSION
    GET = {"canreverse", "ismoving", "mechanicalposition", "position", "reverse", "stepsize", "targetposition",
           "devicestate"}
    PUT = {"halt", "move", "moveabsolute", "movemechanical", "reverse", "sync"}

    def __init__(self, unit, settings, uid, name, description, number=0):
        super().__init__(uid, name, description)
        self.unit, self.settings = unit, settings
        self.number = number
        self.driver_info = f"{description}, alpaca-hub (Optec serial command set)"
        self._target_sky = None

    def _status(self):
        st = self.unit.rotator
        if st is None or st.pa is None or not self.unit.up():
            raise AlpacaError(DRIVER_ERROR, "The Optec hub is not answering")
        return st

    def mech(self):
        return self._status().pa % 360.0

    def sky(self, mech=None):
        return ((self.mech() if mech is None else mech) + self.settings.sky_offset) % 360.0

    def target_sky(self):
        return self._target_sky if self._target_sky is not None else self.sky()

    def move_mech(self, mech):
        _guard(lambda: self.unit.rotator_move(mech))
        self._target_sky = self.sky(mech % 360.0)

    def move_sky(self, sky):
        self.move_mech((sky - self.settings.sky_offset) % 360.0)
        self._target_sky = sky % 360.0

    def sync(self, sky):
        self.settings.sky_offset = (sky - self.mech()) % 360.0
        self.settings.save()
        self._target_sky = sky % 360.0

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "canreverse":
            return True
        if m == "reverse":
            return self.unit.rotator_reverse()
        if m == "stepsize":
            raise AlpacaError(NOT_IMPLEMENTED, "Step size is not known")
        if m == "ismoving":
            self._status()
            return self.unit.is_moving("R")
        if m == "position":
            return round(self.sky(), 4)
        if m == "mechanicalposition":
            return round(self.mech(), 4)
        if m == "targetposition":
            return round(self.target_sky(), 4)
        return [{"Name": "IsMoving", "Value": self.unit.is_moving("R")},
                {"Name": "MechanicalPosition", "Value": round(self.mech(), 4)},
                {"Name": "Position", "Value": round(self.sky(), 4)},
                {"Name": "TimeStamp", "Value": _now_iso()}]

    def put(self, m, p):
        if m not in self.PUT:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "halt":
            _guard(lambda: self.unit.halt("R"))
            return None
        if m == "reverse":
            on = _bool_param(p, "Reverse")
            _guard(lambda: self.unit.set_rotator_reverse(on))
            return None
        v = _float_param(p, "Position")
        if m in ("moveabsolute", "movemechanical", "sync") and not 0.0 <= v < 360.0:
            raise AlpacaError(INVALID_VALUE, f"Position {v} is outside 0..360")
        if m == "move":
            self._status()
            self.move_sky((self.target_sky() if self.unit.is_moving("R") else self.sky()) + v)
        elif m == "moveabsolute":
            self.move_sky(v)
        elif m == "movemechanical":
            self.move_mech(v)
        else:
            self._status()
            self.sync(v)
        return None


# ---------------------------------------------------------------- the status page
class OptecApp(App):
    """One page for either hub: a card per axis (focusers, rotator)."""
    ui_dir = UI

    def __init__(self, inst):
        self.inst = inst
        self.history = History(["pos1", "pos2", "rot", "temp"])   # the same keys for both hubs: one page, one chart

    def status(self):
        i = self.inst
        return {"name": i.title, "kind": i.driver.key, "port": i.port.device, "model": i.ident.get("model"),
                "firmware": i.ident.get("firmware"), "serial": i.ident.get("serial"), "up": i.unit.up(),
                "error": i.unit.last_error, "connected": any(d.connected for d in i.devices),
                "axes": i.axes_status()}

    def control(self, form):
        op, axis = form.get("op", ""), form.get("axis", "")
        log.info("%s page: %s %s %s", self.inst.slug, op, axis, form.get("value", ""))
        try:
            return self.inst.control(op, axis, form.get("value"))
        except NotReady as e:
            return 409, str(e)
        except (ValueError, OptecError) as e:
            return 409, str(e)
        except AlpacaError as e:
            return 409, e.message
        except (RuntimeError, OSError) as e:
            return 503, str(e)


class _OptecInstance(Instance):
    """What both hubs share: the unit, the device server, the page, history samples."""

    def _serve(self, ctx, started=False):
        self.app = OptecApp(self)
        self.srv = DeviceServer(self.devices, self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self._stop = threading.Event()
        if not started:
            self.unit.start()
        self.srv.start()
        self.local_port = self.srv.port
        threading.Thread(target=self._sample, name=f"{self.slug}-history", daemon=True).start()

    def _sample(self):
        while not self._stop.wait(1.0):
            if self.unit.up():
                self.app.history.add(time.time(), self.history_values())

    def stop(self):
        self._stop.set()
        self.srv.stop()
        self.unit.stop()

    def up(self):
        return self.unit.up()


def _parse_value(value, kind=int):
    try:
        return kind(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Enter a number")


def _on(block, key):
    return None if key not in block else ("on" if as_bool(block, key) else "off")


def _settings(block, keys):
    """The page's read-only settings: [label, value] for each key the device reported. keys: (label, key, steps key)
    with steps key None for a plain on/off or text value. The hub never changes these (Optec's software does)."""
    out = []
    for label, key, steps_key in keys:
        if key not in block:
            continue
        if key == "Nickname":
            v = str(block[key]).strip()
            if v:
                out.append([label, v])
            continue
        v = _on(block, key)
        n = as_int(block, steps_key) if steps_key else None
        out.append([label, v + (f", {n} steps" if n is not None else "")])
    return out


LYNX_SETTINGS = (("Nickname", "Nickname", None), ("Backlash compensation", "BLC En", "BLC Stps"),
                 ("Temperature compensation", "TComp ON", None), ("Temp. compensation at power-up", "TC@Start", None))
GEMINI_FOCUS_SETTINGS = (("Nickname", "Nickname", None), ("Backlash compensation", "BLCompOn", "BLCSteps"),
                         ("Home at power-up", "HOnStart", None), ("Temperature compensation", "TComp On", None),
                         ("Temp. compensation at power-up", "TC Start", None))
GEMINI_ROTATOR_SETTINGS = (("Nickname", "Nickname", None), ("Backlash compensation", "BLCompOn", "BLCSteps"),
                           ("Home at power-up", "HOnStart", None), ("Reverse", "iReverse", None))


# ---------------------------------------------------------------- FocusLynx / ThirdLynx
class FocusLynxDriver(Driver):
    key = "focuslynx"
    title = "Optec FocusLynx"
    usb_ids = frozenset({L.USB_THIRDLYNX, L.USB_OPTEC_FTDI})
    probe_baud = L.BAUD          # the hub is silent until asked; the probe asks only for its info
    simulators = {"lynx": lambda i: (_sim("SimLynx"), L.USB_THIRDLYNX, f"SIMLYNX{i + 1}", "Simulated Optec ThirdLynx")}

    def probe(self, ser):
        return L.probe(ser)

    def start(self, ctx, port, ident):
        return FocusLynxInstance(self, ctx, port, ident)


def _sim(name, **kw):
    from . import sim
    return getattr(sim, name)(**kw)


class FocusLynxInstance(_OptecInstance):
    def __init__(self, driver, ctx, port, ident, unit=None):
        n = ctx.unit_number()
        model = ident.get("model") or "FocusLynx"
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        title = f"Optec {model}" + ("" if n == 0 else f" #{n + 1}")
        super().__init__(driver, port, ident, slug, title)
        self.unit_id = unit_of(ident, port)
        self.unit = unit or L.LynxUnit(port.device, ctx.opener_for(port, L.BAUD))
        # The channels (F1, F2) are only known once the hub has answered, so connect before making the devices.
        self.unit.start(connect_now=True)
        chans = self.unit.channels or ["F1"]
        self.devices = []
        for i, ch in enumerate(chans):
            nick = (self.unit.config.get(ch).nickname if self.unit.config.get(ch) else "") or ch
            name = f"{title} {ch}" + (f" ({nick})" if nick and nick != ch else "")
            self.devices.append(Focuser(FocusAxis(self.unit, "lynx", ch), ctx.uid(self.unit_id, ch), name,
                                        f"Optec {model} focuser {ch}", number=i))
        self._serve(ctx, started=True)
        log.info("%s on %s (%s), focusers %s", title, port.device, ident, ", ".join(chans))

    def history_values(self):
        vals, temp = {}, None
        for ch in self.unit.channels:
            st = self.unit.snapshot(ch)
            if st:
                vals["pos1" if ch == "F1" else "pos2"] = st.position
                temp = st.temperature if st.temperature is not None else temp
        vals["temp"] = temp
        return vals

    def axes_status(self):
        out = []
        for dev in self.devices:
            ch = dev.axis.key
            st = self.unit.snapshot(ch)
            cfg = self.unit.config.get(ch)
            out.append({"id": ch, "kind": "focuser", "name": dev.name, "alpaca": "Focuser",
                        "configured": self.unit.configured(ch), "dev_type": cfg.dev_type if cfg else None,
                        "position": st.position if st else None, "target": st.target if st else None,
                        "moving": self.unit.is_moving(ch), "homing": st.homing if st else None,
                        "homed": st.homed if st else None, "max": self.unit.max_pos(ch),
                        "temperature": st.temperature if st else None, "unit": "steps",
                        "settings": _settings(cfg.raw, LYNX_SETTINGS) if cfg else []})
        return out

    def control(self, op, axis, value):
        if axis not in self.unit.channels:
            return 400, "Unknown focuser"
        if op == "goto":
            self.unit.move(axis, _parse_value(value))
            return 200, f"{axis} moving to {value}"
        if op in ("step", "center"):       # page only, like Optec's own pages: plain moves, nothing stored
            st = self.unit.snapshot(axis)
            if st is None:
                raise NotReady("No position yet")
            pos = self.unit.max_pos(axis) // 2 if op == "center" else st.position + _parse_value(value)
            self.unit.move(axis, pos)
            return 200, f"{axis} moving to {pos}"
        if op == "halt":
            self.unit.halt(axis)
            return 200, f"{axis} halted"
        if op == "home":
            self.unit.home(axis)
            return 200, f"{axis} homing"
        if op == "sync":
            self.unit.sync(axis, _parse_value(value))
            return 200, f"{axis} position set to {value} (the motor did not move)"
        return 400, "op: goto, step, center, halt, home or sync"

    def summary(self):
        if not self.up():
            return {"text": "no answer", "state": "bad"}
        parts, moving = [], False
        for ch in self.unit.channels:
            st = self.unit.snapshot(ch)
            if st:
                parts.append(f"{ch} {st.position:,}".replace(",", " "))
                moving = moving or self.unit.is_moving(ch)
        temp = next((self.unit.snapshot(ch).temperature for ch in self.unit.channels
                     if self.unit.snapshot(ch) and self.unit.snapshot(ch).temperature is not None), None)
        if temp is not None:
            parts.append(f"{temp:.1f} °C")
        return {"text": " · ".join(parts), "state": "warn" if moving else "ok"}


# ---------------------------------------------------------------- Gemini / Hercules
class GeminiDriver(Driver):
    key = "gemini"
    title = "Optec Gemini / Hercules"
    usb_ids = frozenset({G.USB_OPTEC_FTDI})
    probe_baud = G.BAUD
    simulators = {"gemini": lambda i: (_sim("SimGemini", serial=str(2001 + i)), G.USB_OPTEC_FTDI, f"SIMGEM{i + 1}",
                                       "Simulated Optec Gemini / Hercules")}

    def probe(self, ser):
        return G.probe(ser)

    def start(self, ctx, port, ident):
        return GeminiInstance(self, ctx, port, ident)


class GeminiInstance(_OptecInstance):
    def __init__(self, driver, ctx, port, ident, unit=None):
        n = ctx.unit_number()
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        title = "Optec Gemini / Hercules" + ("" if n == 0 else f" #{n + 1}")
        super().__init__(driver, port, ident, slug, title)
        self.unit_id = unit_of(ident, port)
        self.unit = unit or G.GeminiUnit(port.device, ctx.opener_for(port, G.BAUD))
        self.rot_settings = RotatorSettings(ctx.settings_path(
            self.unit_id, [ctx.legacy_file(f"optec-gemini-{self.unit_id}.json")]))
        suffix = "" if n == 0 else f" #{n + 1}"
        self.focuser = Focuser(FocusAxis(self.unit, "gemini", "F"), ctx.uid(self.unit_id, "focuser"),
                               f"Optec Gemini focuser{suffix}", "Optec Gemini / Hercules focuser")
        self.rotator = Rotator(self.unit, self.rot_settings, ctx.uid(self.unit_id, "rotator"),
                               f"Optec Gemini rotator{suffix}", "Optec Gemini / Hercules rotator")
        self.devices = [self.focuser, self.rotator]
        self.unit.start(connect_now=True)
        self._serve(ctx, started=True)
        log.info("%s on %s (%s)", title, port.device, ident)

    def history_values(self):
        f, r = self.unit.focus, self.unit.rotator
        return {"pos1": f.position if f else None,
                "rot": round(self.rotator.sky(r.pa % 360.0), 2) if r and r.pa is not None else None,
                "temp": f.temperature if f else None}

    def axes_status(self):
        f, r = self.unit.focus, self.unit.rotator
        out = [{"id": "F", "kind": "focuser", "name": self.focuser.name, "alpaca": "Focuser", "configured": True,
                "position": f.position if f else None, "target": f.target if f else None,
                "moving": self.unit.is_moving("F"), "homing": f.homing if f else None,
                "homed": f.homed if f else None, "max": self.unit.focus_max() or None,
                "temperature": f.temperature if f else None, "unit": "steps",
                "settings": _settings(self.unit.focus_cfg, GEMINI_FOCUS_SETTINGS)}]
        rot = {"id": "R", "kind": "rotator", "name": self.rotator.name, "alpaca": "Rotator", "configured": True,
               "moving": self.unit.is_moving("R"), "homing": r.homing if r else None,
               "homed": r.homed if r else None, "reverse": self.unit.rotator_reverse(),
               "sky_offset": round(self.rot_settings.sky_offset, 3), "unit": "°",
               "settings": _settings(self.unit.rotator_cfg, GEMINI_ROTATOR_SETTINGS)}
        if r and r.pa is not None:
            rot.update(mechanical=round(r.pa % 360.0, 3), position=round(self.rotator.sky(r.pa % 360.0), 3),
                       target=round(self.rotator.target_sky(), 3))
        out.append(rot)
        return out

    def control(self, op, axis, value):
        if axis == "F":
            if op == "goto":
                self.unit.focus_move(_parse_value(value))
                return 200, f"Focuser moving to {value}"
            if op in ("step", "center"):   # page only, like Optec's own pages: plain moves, nothing stored
                f = self.unit.focus
                if f is None:
                    raise NotReady("No focuser position yet")
                pos = (self.unit.focus_max() or 0) // 2 if op == "center" else f.position + _parse_value(value)
                self.unit.focus_move(pos)
                return 200, f"Focuser moving to {pos}"
            if op in ("halt", "home"):
                getattr(self.unit, op)("F")
                return 200, "Focuser " + ("halted" if op == "halt" else "homing")
            return 400, "op: goto, step, center, halt or home"
        if axis == "R":
            if op == "goto":
                self.rotator.move_sky(_parse_value(value, float))
                return 200, f"Rotator turning to {value}°"
            if op == "step":               # by a signed angle from where it is, the short way
                to = (self.rotator.sky() + _parse_value(value, float)) % 360.0
                self.rotator.move_sky(to)
                return 200, f"Rotator turning to {to:.2f}°"
            if op == "center":             # Optec's Center: mechanical 0° (what the sky angle is there depends on Sync)
                self.rotator.move_mech(0.0)
                return 200, f"Rotator turning to mechanical 0° (sky {self.rotator.sky(0.0):.2f}°)"
            if op in ("halt", "home"):
                getattr(self.unit, op)("R")
                return 200, "Rotator " + ("halted" if op == "halt" else "homing")
            if op == "sync":
                self.rotator.sync(_parse_value(value, float))
                return 200, f"Rotator now reads {value}° (it did not move)"
            return 400, "op: goto, step, center, halt, home or sync"
        return 400, "Unknown axis"

    def summary(self):
        if not self.up():
            return {"text": "no answer", "state": "bad"}
        f, r = self.unit.focus, self.unit.rotator
        parts = []
        if f:
            parts.append(f"focus {f.position:,}".replace(",", " "))
        if r and r.pa is not None:
            parts.append(f"rotator {self.rotator.sky(r.pa % 360.0):.2f}°")
            if not r.homed:
                parts.append("rotator not homed")
        moving = self.unit.is_moving("F") or self.unit.is_moving("R")
        return {"text": " · ".join(parts), "state": "warn" if moving or (r and not r.homed) else "ok"}


DRIVERS = [FocusLynxDriver, GeminiDriver]
