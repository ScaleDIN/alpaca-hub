"""ZWO EAF focusers as Alpaca Focusers, with a status page (tab) per focuser.

Transport "usb": the hub calls find() while a ZWO device (USB vendor 03C3) is plugged in; this driver lists the EAFs
ZWO's SDK sees and the hub starts an EafInstance for each new one. Several EAFs are several devices of one process.

What the hub never does here (REFACTOR.md "Focuser and filter wheel"): set anything stored in the focuser (max step,
reverse, backlash, beep, position, alias). Those are shown read-only; ZWO's own software sets them.

Stall and jam (REFACTOR.md, "Safety"): the SDK reports no motor current, and the position is the count of steps sent
(open loop), so a jam doesn't show in Position. What is watched: the motor error code E5 ("motor stall") on firmware
that has it, and a move still running long after its steps should have taken ("not finishing": stopped).
"""
import logging
import os
import threading
import time

from hub.api import Driver, Instance, unit_of
from hub.devserver import (DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE, NOT_CONNECTED, NOT_IMPLEMENTED,
                           AlpacaDevice, AlpacaError, App, BadRequest, DeviceServer, History)

from . import VERSION
from . import sim as _sim
from .eaf import INVALID_ID, MOTOR_CODES, REMOVED, VID, EafError, Sdk

log = logging.getLogger("zwoeaf")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

POLL_MOVING_S = 0.25
POLL_IDLE_S = 1.0
TEMP_EVERY_S = 5.0
UP_S = 5.0                  # up while a read succeeded this recently
RELOCATE_S = 2.0            # a lost focuser is looked for this often
MIN_SPEED = 50.0            # steps/s assumed before a move has been timed: a move longer than its steps at this
LIMIT_EXTRA_S = 30.0        # speed plus this is "not finishing"; once timed: twice the time plus this
SIM_PID = 0x1F10            # guess until step 1 reads a unit's USB IDs


class ZwoEafDriver(Driver):
    key = "zwoeaf"
    title = "ZWO EAF focuser"
    transport = "usb"
    usb_vendors = frozenset({VID})
    simulators = {
        "eaf": lambda i: (_sim.standard(i), (VID, SIM_PID), "", "Simulated ZWO EAF"),
        "eaf5v": lambda i: (_sim.five_volt(i), (VID, SIM_PID), "", "Simulated ZWO EAF 5V"),
    }

    def __init__(self):
        self._real = None           # eaf.Sdk, loaded the first time a ZWO device is plugged in
        self._simsdk = None
        self.sdk = None             # the one find() last used: instances use the same
        self._lock = threading.RLock()
        self.held = {}              # unit -> EafInstance (its focuser is open)
        self._refused = set()       # SDK IDs that wouldn't open (in use elsewhere): logged once
        self._load_error = None

    def _sdk_for(self, sims):
        if sims is not None:
            if self._simsdk is None:
                self._simsdk = _sim.SimSdk(sims)
            else:
                for s in sims:
                    if s not in self._simsdk.units:
                        self._simsdk.units.append(s)
            return self._simsdk
        if self._real is None:
            try:
                self._real = Sdk()
            except OSError as e:
                if self._load_error is None:
                    log.error("ZWO's EAF SDK did not load: %s", e)
                self._load_error = str(e)
                return None
        return self._real

    def _identify(self, sdk, id_):
        """Open, read and close one focuser nobody here holds -> identity, or None (in use elsewhere, gone)."""
        try:
            info = sdk.info(id_)
            sdk.open(id_)
        except EafError as e:
            if id_ not in self._refused:
                log.info("EAF with SDK ID %d not opened (%s): in use by another program?", id_, e)
                self._refused.add(id_)
            return None
        try:
            self._refused.discard(id_)
            serial = sdk.serial(id_)
            ident = {"serial": serial or f"{info['name']}-ID{id_}", "name": info["name"], "id": id_,
                     "has_serial": bool(serial), "firmware": sdk.firmware(id_), "kind": sdk.kind(id_)}
            pid = getattr(next((u for u in getattr(sdk, "units", ()) if u.sdk_id == id_), None), "pid", None)
            if pid is not None:
                ident["pid"] = pid
            return ident
        except EafError as e:
            log.info("EAF with SDK ID %d: reading it failed (%s)", id_, e)
            return None
        finally:
            try:
                sdk.close(id_)
            except EafError:
                pass

    def find(self, ctx, sims):
        sdk = self._sdk_for(sims)
        if sdk is None:
            return []
        with self._lock:
            self.sdk = sdk
            held = {i.id: i for i in self.held.values() if i.id is not None}
            out = []
            for id_ in sdk.ids():
                inst = held.get(id_)
                if inst is not None:
                    out.append(dict(inst.ident))
                    continue
                ident = self._identify(sdk, id_)
                if ident is not None:
                    out.append(ident)
            # a held unit that was unplugged and came back under a new ID: its instance takes the new ID itself
            return out

    def locate(self, unit):
        """The SDK ID of unit if it is plugged in and nobody here holds that ID (a replugged focuser), else None."""
        with self._lock:
            sdk = self.sdk
            if sdk is None:
                return None
            taken = {i.id for i in self.held.values() if i.id is not None}
            for id_ in sdk.ids():
                if id_ in taken:
                    continue
                ident = self._identify(sdk, id_)
                if ident is not None and ident["serial"] == unit:
                    return id_
        return None

    def start(self, ctx, port, ident):
        if self.sdk is None and not ctx.simulated:
            self.sdk = self._sdk_for(None)
        return EafInstance(self, ctx, port, ident)


class EafInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        self.unit = unit_of(ident, port)
        n = ctx.unit_number()
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        super().__init__(driver, port, ident, slug, "ZWO EAF" + ("" if n == 0 else f" #{n + 1}"))
        self.sdk = driver.sdk
        self.id = None
        self._lock = threading.Lock()
        self.position = self.moving_flag = self.hand = None
        self.temperature = None
        self.max_step = self.reverse = self.backlash = self.beep = None
        self.firmware, self.kind = ident.get("firmware"), ident.get("kind")
        self.codes = None           # the firmware reports motor codes: None not yet known, then True / False
        self.code = None
        self.stalled = None         # position it stalled at (E5), until the next move
        self.move = None            # {"from", "target", "t0", "by"} for our own moves
        self.last_move = None       # {"text", "state"}: how the last move ended, for the page
        self.speed = None           # steps/s, from timed moves
        self.lost = None            # why it is not usable now ("not found: unplugged or no power?")
        self.last_error = None
        self._ok_at = 0.0
        self._temp_at = 0.0
        self._lost_at = 0.0
        self._take(ident.get("id"))
        name = "ZWO EAF" + ("" if n == 0 else f" #{n + 1}")
        self.focuser = EafFocuser(self, ctx.uid(self.unit, "focuser"), name)
        self.app = EafApp(self)
        self.srv = DeviceServer([self.focuser], self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self.srv.start()
        self.local_port = self.srv.port
        self._stop = threading.Event()
        threading.Thread(target=self._run, name=f"{slug}-poll", daemon=True).start()
        threading.Thread(target=self._sample, name=f"{slug}-history", daemon=True).start()
        log.info("%s: %s, serial %s, SDK ID %s", slug, ident.get("name"), self.unit, self.id)

    # ---------------------------------------------------------------- holding the focuser
    def _take(self, id_):
        """Open the focuser at SDK ID id_ and read what is stored in it."""
        if id_ is None or self.sdk is None:
            self.lost = self.lost or "not found yet"
            return False
        try:
            self.sdk.open(id_)
            self.max_step = self.sdk.max_step(id_)
            self.reverse = self.sdk.reverse(id_)
            self.backlash = self.sdk.backlash(id_)
            try:
                self.beep = self.sdk.beep(id_)
            except EafError:
                self.beep = None
            self.position = self.sdk.position(id_)
            self.moving_flag, self.hand = self.sdk.is_moving(id_)
        except EafError as e:
            self.lost = f"cannot open it ({e}): in use by another program?"
            log.warning("%s: %s", self.slug, self.lost)
            try:
                self.sdk.close(id_)
            except EafError:
                pass
            return False
        self.id = id_
        self.lost = None
        self._ok_at = time.monotonic()
        with self.driver._lock:
            self.driver.held[self.unit] = self
        return True

    def _lose(self, why):
        with self._lock:
            if self.move is not None:
                self.last_move = {"text": f"lost during the move at {self.position}: {why}", "state": "bad"}
                self.move = None
            self.lost = why
            old, self.id = self.id, None
            self.moving_flag = self.hand = None
        with self.driver._lock:
            self.driver.held.pop(self.unit, None)
        if old is not None:
            try:
                self.sdk.close(old)
            except EafError:
                pass
        log.warning("%s: %s", self.slug, why)

    # ---------------------------------------------------------------- polling
    def _run(self):
        while not self._stop.is_set():
            if self.id is None:
                if time.monotonic() - self._lost_at >= RELOCATE_S:
                    self._lost_at = time.monotonic()
                    self.sdk = self.sdk or self.driver.sdk
                    id_ = self.driver.locate(self.unit)
                    if id_ is not None and self._take(id_):
                        log.info("%s: back (SDK ID %d)", self.slug, id_)
                self._stop.wait(0.5)
                continue
            try:
                self.poll()
            except Exception:
                log.exception("%s: poll failed", self.slug)
            self._stop.wait(POLL_MOVING_S if self.moving_flag else POLL_IDLE_S)

    def poll(self):
        id_ = self.id
        if id_ is None:
            return
        try:
            pos = self.sdk.position(id_)
            moving, hand = self.sdk.is_moving(id_)
            code = None
            if self.codes is not False:
                code = self.sdk.motor_code(id_)
                if self.codes is None:
                    self.codes = code is not None
            temp = self.temperature
            now = time.monotonic()
            if not moving and now - self._temp_at >= TEMP_EVERY_S:
                self._temp_at = now
                t = self.sdk.temperature(id_)
                temp = t if t is not None else temp
        except EafError as e:
            if e.code in (REMOVED, INVALID_ID):
                self._lose("not found: unplugged, or no power? (an EAF 5V can drop out on a weak USB port)")
            else:
                self.last_error = str(e)
            return
        stop_it = False
        with self._lock:
            self._ok_at = time.monotonic()
            self.position, self.moving_flag, self.hand, self.temperature = pos, moving, hand, temp
            self.code = code
            if code == "E5" and self.stalled is None:
                self.stalled = pos
                self.last_move = {"text": f"stalled at {pos} (the focuser reported E5, motor stall)"
                                  + (f", on the way to {self.move['target']}" if self.move else ""), "state": "bad"}
                log.warning("%s: motor stall (E5) at %d", self.slug, pos)
                self.move = None
            mv = self.move
            if mv is not None:
                took = time.monotonic() - mv["t0"]
                if not moving:
                    self.move = None
                    if pos == mv["target"]:
                        dist = abs(mv["target"] - mv["from"])
                        if dist >= 200 and took > 0.2:
                            v = dist / took
                            self.speed = v if self.speed is None else 0.7 * self.speed + 0.3 * v
                        self.last_move = {"text": f"reached {pos}", "state": "ok"}
                    elif mv.get("halted"):
                        self.last_move = {"text": f"halted at {pos}", "state": "ok"}
                    else:
                        self.last_move = {"text": f"stopped at {pos}, short of {mv['target']}", "state": "warn"}
                elif took > self.limit(mv):
                    self.move = None
                    self.last_move = {"text": f"not finishing: stopped after {took:.0f} s at {pos}, on the way to "
                                              f"{mv['target']}. Jammed?", "state": "bad"}
                    log.warning("%s: move to %d not finishing after %.0f s: stopping it", self.slug, mv["target"], took)
                    stop_it = True
        if stop_it:
            try:
                self.sdk.stop(id_)
            except EafError as e:
                self.last_error = str(e)

    def limit(self, mv):
        dist = abs(mv["target"] - mv["from"])
        if self.speed:
            return 2 * dist / self.speed + LIMIT_EXTRA_S
        return dist / MIN_SPEED + LIMIT_EXTRA_S

    # ---------------------------------------------------------------- commands (Alpaca and the page)
    def ready(self):
        if self.id is None:
            raise AlpacaError(DRIVER_ERROR, f"The EAF is {self.lost or 'not available'}")

    def goto(self, target, by, clamp=False):
        """clamp: a target outside 0 to max step goes to the nearest end (Alpaca clients: ConformU's "Move - Below 0"
        and "Move - Above MaxStep" expect that, not an error); without it (the page) such a target is refused."""
        self.ready()
        top = self.max_step or 0
        if clamp:
            target = min(max(target, 0), top)
        elif not 0 <= target <= top:
            raise AlpacaError(INVALID_VALUE, f"Position {target} is outside 0 to {self.max_step}")
        if self.hand:
            raise AlpacaError(INVALID_OPERATION, "The hand controller is moving the focuser")
        try:
            pos = self.sdk.position(self.id)
            self.sdk.move(self.id, target)
        except EafError as e:
            raise AlpacaError(DRIVER_ERROR, str(e))
        with self._lock:
            self.stalled = None
            self.move = {"from": pos, "target": target, "t0": time.monotonic(), "by": by}
            self.moving_flag = True if pos != target else self.moving_flag
            self.last_move = {"text": f"moving to {target}", "state": "warn"}
        log.info("%s: move %d -> %d (%s)", self.slug, pos, target, by)

    def halt(self, by):
        self.ready()
        try:
            self.sdk.stop(self.id)
        except EafError as e:
            raise AlpacaError(DRIVER_ERROR, str(e))
        with self._lock:
            if self.move is not None:
                self.move["halted"] = True
        log.info("%s: halt (%s)", self.slug, by)

    # ---------------------------------------------------------------- what the hub asks
    def up(self):
        return self.id is not None and time.monotonic() - self._ok_at < UP_S

    def state(self):
        """(text, ok|warn|bad) for Overview and the page."""
        if self.id is None:
            return self.lost or "not available", "bad"
        if self.stalled is not None:
            return f"stalled at {self.stalled}", "bad"
        if self.last_move and self.last_move["state"] == "bad":
            return self.last_move["text"], "bad"
        if self.hand:
            return f"moved by the hand controller, at {self.position}", "warn"
        if self.moving_flag:
            return f"moving to {self.move['target']}" if self.move else "moving", "warn"
        return "idle", "ok"

    def summary(self):
        text, st = self.state()
        if st == "ok":
            t = "" if self.temperature is None else f" · {self.temperature:.1f} °C"
            return {"text": f"{self.position} steps{t}", "state": "ok"}
        return {"text": text, "state": st}

    def _sample(self):
        while not self._stop.wait(1.0):
            if self.up():
                self.app.history.add(time.time(), {"pos": self.position, "temp": self.temperature})

    def stop(self):
        """Stops a move of ours (never leave the motor running), then lets go of the focuser."""
        self._stop.set()
        self.srv.stop()
        id_ = self.id
        if id_ is not None:
            try:
                moving, hand = self.sdk.is_moving(id_)
                if moving and not hand:
                    self.sdk.stop(id_)
                    log.info("%s: stopped its move on the way out", self.slug)
            except EafError:
                pass
            try:
                self.sdk.close(id_)
            except EafError:
                pass
        with self.driver._lock:
            if self.driver.held.get(self.unit) is self:
                self.driver.held.pop(self.unit, None)
        self.id = None


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# ---------------------------------------------------------------- Alpaca Focuser
class EafFocuser(AlpacaDevice):
    type, type_name, interface_version = "focuser", "Focuser", 4
    driver_version = VERSION
    driver_info = "ZWO EAF through ZWO's EAF SDK, alpaca-hub"
    GET = {"absolute", "ismoving", "maxincrement", "maxstep", "position", "stepsize", "tempcomp",
           "tempcompavailable", "temperature", "devicestate"}
    PUT = {"halt", "move", "tempcomp"}

    def __init__(self, inst, uid, name):
        super().__init__(uid, name, "ZWO EAF electronic focuser")
        self.inst = inst

    def set_connected(self, on):
        if on and self.inst.id is None:
            raise AlpacaError(0x500, f"The EAF is {self.inst.lost or 'not available'}")
        self.connected = on

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        i = self.inst
        if m == "absolute":
            return True
        if m in ("tempcomp", "tempcompavailable"):
            return False
        if m == "stepsize":
            raise AlpacaError(NOT_IMPLEMENTED, "The EAF SDK gives no step size")
        i.ready()
        if m in ("maxstep", "maxincrement"):
            return i.max_step
        if m == "position":
            return i.position
        if m == "ismoving":
            return bool(i.moving_flag)
        if m == "temperature":
            if i.temperature is None:
                raise AlpacaError(NOT_IMPLEMENTED, "No temperature reading from this EAF")
            return i.temperature
        out = [{"Name": "IsMoving", "Value": bool(i.moving_flag)}, {"Name": "Position", "Value": i.position}]
        if i.temperature is not None:
            out.append({"Name": "Temperature", "Value": i.temperature})
        out.append({"Name": "TimeStamp", "Value": _now_iso()})
        return out

    def put(self, m, p):
        if m not in self.PUT:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        if m == "tempcomp":
            if str(p.get("tempcomp", "")).strip().lower() not in ("true", "false"):
                raise BadRequest("Missing or invalid TempComp")
            raise AlpacaError(NOT_IMPLEMENTED, "Temperature compensation is not offered (clients do their own)")
        if m == "halt":
            self.inst.halt("Alpaca")
            return None
        try:
            pos = int(str(p.get("position", "")).strip())
        except ValueError:
            raise BadRequest("Missing or invalid Position")
        self.inst.goto(pos, "Alpaca", clamp=True)
        return None


# ---------------------------------------------------------------- the page
class EafApp(App):
    ui_dir = UI

    def __init__(self, inst):
        self.inst = inst
        self.history = History(["pos", "temp"])

    def status(self):
        i = self.inst
        text, st = i.state()
        return {"name": i.title, "model": i.ident.get("name"), "kind": i.kind, "serial": i.unit,
                "has_serial": i.ident.get("has_serial", True), "firmware": i.firmware, "sdk_id": i.id,
                "sdk": _sdk_version(i.sdk), "up": i.up(), "lost": i.lost, "state": text, "state_class": st,
                "position": i.position, "target": i.move["target"] if i.move else None,
                "moving": bool(i.moving_flag), "hand": bool(i.hand), "temperature": i.temperature,
                "max_step": i.max_step, "reverse": i.reverse, "backlash": i.backlash, "beep": i.beep,
                "codes": i.codes, "code": i.code, "code_text": MOTOR_CODES.get(i.code or "", i.code),
                "last_move": i.last_move, "speed": None if i.speed is None else round(i.speed),
                "error": i.last_error, "connected": i.focuser.connected, "uid": i.focuser.uid}

    def control(self, form):
        op = form.get("op", "")
        log.info("%s page: %s %s", self.inst.slug, op, form.get("value", ""))
        try:
            if op == "halt":
                self.inst.halt("page")
                return 200, "Halted"
            if op in ("goto", "step"):
                try:
                    v = int(str(form.get("value", "")).strip())
                except ValueError:
                    return 400, "Enter a whole number of steps"
                target = v if op == "goto" else (self.inst.position or 0) + v
                self.inst.goto(target, "page")
                return 200, f"Moving to {target}"
        except AlpacaError as e:
            return 409, e.message
        return 400, "op: goto, step or halt"


def _sdk_version(sdk):
    try:
        return sdk.sdk_version() if sdk is not None else None
    except Exception:
        return None


DRIVERS = [ZwoEafDriver]
