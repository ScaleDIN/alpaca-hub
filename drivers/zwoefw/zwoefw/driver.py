"""ZWO EFW filter wheels as Alpaca FilterWheels, with a status page (tab) per wheel.

Transport "usb": the hub calls find() while a ZWO device (USB vendor 03C3) is plugged in; this driver lists the EFWs
ZWO's SDK sees and the hub starts an EfwInstance for each new one. Several wheels are several devices of one process.

What the hub never does here (REFACTOR.md "Focuser and filter wheel"): set anything stored in the wheel (one-way
rotation, calibration, alias). The one-way setting is shown read-only; ZWO's own software sets it.

The filter names and focus offsets are this driver's settings, per wheel (by serial), set on the page: the filters
are in the wheel, so they belong with it, not with a client's profile.

The SDK has no stop: a turn finishes on its own. A turn still running long after it should have ended is reported
("not finishing"), not stopped. Another program can open the wheel while the hub holds it (the EAF does that, seen
2026-11-24), so a turn the hub didn't start is shown as "moved by another program".
"""
import json
import logging
import os
import re
import threading
import time

from hub.api import Driver, Instance, unit_of
from hub.devserver import (DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE, NOT_CONNECTED, AlpacaDevice, AlpacaError,
                           App, BadRequest, DeviceServer, History)

from . import VERSION
from . import sim as _sim
from .efw import ERROR_STATE, INVALID_ID, MOVING, REMOVED, VID, EfwError, Sdk

log = logging.getLogger("zwoefw")
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

POLL_MOVING_S = 0.25
POLL_IDLE_S = 1.0
HW_EVERY_S = 5.0            # the hardware error code is read this often while idle
UP_S = 5.0                  # up while a read succeeded this recently
RELOCATE_S = 2.0            # a lost wheel is looked for this often
SLOT_LIMIT_S = 3.0          # a turn longer than this per slot of the whole wheel, plus LIMIT_EXTRA_S, is "not finishing"
LIMIT_EXTRA_S = 15.0
OURS_S = 2.0                # after a turn of ours ends, a change this soon still counts as ours
FAILS_TO_REOPEN = 3         # SDK calls failing this many polls in a row (an error that doesn't say "gone"): the unit is
                            # closed and opened again, as after an unplug. On 2026-11-25 an EFW answered every call
                            # with an undocumented error 11 after the driver's update restart, until it was reopened.
START_GRACE_S = 1.5         # after EFWSetPosition returns, a wheel still reading its old slot hasn't started yet
NAME_MAX = 32


class ZwoEfwDriver(Driver):
    key = "zwoefw"
    title = "ZWO EFW filter wheel"
    transport = "usb"
    usb_vendors = frozenset({VID})
    simulators = {
        "efw": lambda i: (_sim.seven(i), (VID, _sim.PID), "", "Simulated ZWO EFW (7 slots)"),
        "efw5": lambda i: (_sim.five(i), (VID, _sim.PID), "", "Simulated ZWO EFW (5 slots)"),
        "efw8": lambda i: (_sim.eight(i), (VID, _sim.PID), "", "Simulated ZWO EFW (8 slots)"),
    }

    def __init__(self):
        self._real = None           # efw.Sdk, loaded the first time a ZWO device is plugged in
        self._simsdk = None
        self.sdk = None             # the one find() last used: instances use the same
        self._lock = threading.RLock()
        self.held = {}              # unit -> EfwInstance (its wheel is open)
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
                    log.error("ZWO's EFW SDK did not load: %s", e)
                self._load_error = str(e)
                return None
        return self._real

    def _identify(self, sdk, id_):
        """Open, read and close one wheel nobody here holds -> identity, or None (in use elsewhere, gone, still
        counting its slots after being plugged in)."""
        try:
            sdk.open(id_)
        except EfwError as e:
            if id_ not in self._refused:
                log.info("EFW with SDK ID %d not opened (%s): in use by another program?", id_, e)
                self._refused.add(id_)
            return None
        try:
            self._refused.discard(id_)
            info = sdk.info(id_)
            serial = sdk.serial(id_)
            ident = {"serial": serial or f"{info['name']}-ID{id_}", "name": info["name"], "id": id_,
                     "has_serial": bool(serial), "firmware": sdk.firmware(id_), "slots": info["slots"]}
            alias = alias_of(info["name"])
            if alias:
                ident["alias"] = alias        # the hub shows it when the user hasn't named the device
            return ident
        except EfwError as e:
            if e.code != MOVING:
                log.info("EFW with SDK ID %d: reading it failed (%s)", id_, e)
            return None
        finally:
            try:
                sdk.close(id_)
            except EfwError:
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
            return out

    def locate(self, unit):
        """The SDK ID of unit if it is plugged in and nobody here holds that ID (a replugged wheel), else None."""
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
        return EfwInstance(self, ctx, port, ident)


def alias_of(name):
    """The custom ID (alias) ZWO's software stores in the wheel, which the SDK gives inside the model name:
    "EFW(Main scope)" -> "Main scope"; None without one."""
    m = re.fullmatch(r"\s*[^()]*\((.+)\)\s*", name or "")
    return m.group(1).strip() or None if m else None


def default_name(slot):
    return f"Filter {slot + 1}"


class EfwInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        self.unit = unit_of(ident, port)
        n = ctx.unit_number()
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        super().__init__(driver, port, ident, slug, "ZWO EFW" + ("" if n == 0 else f" #{n + 1}"))
        self.sdk = driver.sdk
        self.id = None
        self._lock = threading.Lock()
        self.slots = ident.get("slots") or None
        self.position = None        # the slot, or None while unknown; -1 never stored (see moving)
        self.moving = False
        self.one_way = None
        self.hw_error = None
        self.error_state = False
        self.firmware = ident.get("firmware")
        self.move = None            # {"from", "target", "t0", "by"} for our own turns
        self.other = None           # {"from"}: a turn the hub didn't start, while it runs
        self._ours_until = 0.0
        self.last_move = None       # {"text", "state"}: how the last turn ended, for the page
        self.slot_time = None       # seconds per slot, from timed turns
        self.lost = None
        self.last_error = None
        self._ok_at = 0.0
        self._hw_at = 0.0
        self._lost_at = 0.0
        self._fails = 0             # polls failing in a row (FAILS_TO_REOPEN)
        self.settings_path = ctx.settings_path(self.unit)
        self._names, self._offsets = self._load()
        self._take(ident.get("id"))
        name = "ZWO EFW" + ("" if n == 0 else f" #{n + 1}")
        self.wheel = EfwFilterWheel(self, ctx.uid(self.unit, "filterwheel"), name)
        self.app = EfwApp(self)
        self.srv = DeviceServer([self.wheel], self.app, VERSION, public_urls=ctx.public_urls,
                                simulated=ctx.simulated, is_local=ctx.is_local)
        self.srv.start()
        self.local_port = self.srv.port
        self._stop = threading.Event()
        threading.Thread(target=self._run, name=f"{slug}-poll", daemon=True).start()
        threading.Thread(target=self._sample, name=f"{slug}-history", daemon=True).start()
        log.info("%s: %s, serial %s, SDK ID %s, %s slots", slug, ident.get("name"), self.unit, self.id, self.slots)

    # ---------------------------------------------------------------- names and offsets (this driver's settings)
    def _load(self):
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        names = [str(x)[:NAME_MAX] for x in d.get("names", []) if isinstance(x, str)]
        offsets = [int(x) for x in d.get("offsets", []) if isinstance(x, int) and not isinstance(x, bool)]
        return names, offsets

    def names(self):
        """One name per slot: the saved ones, "Filter n" for the rest (a wheel with more slots than names saved)."""
        n = self.slots or 0
        return [(self._names[k] if k < len(self._names) and self._names[k].strip() else default_name(k))
                for k in range(n)]

    def offsets(self):
        n = self.slots or 0
        return [(self._offsets[k] if k < len(self._offsets) else 0) for k in range(n)]

    def save_filters(self, names, offsets):
        """From the page: a name (up to 32 characters; empty: "Filter n") and a whole-step focus offset per slot."""
        n = self.slots or 0
        if len(names) != n or len(offsets) != n:
            raise ValueError(f"this wheel has {n} slots")
        clean_names = [str(x).strip()[:NAME_MAX] or default_name(k) for k, x in enumerate(names)]
        clean_offsets = []
        for k, x in enumerate(offsets):
            try:
                clean_offsets.append(int(str(x).strip() or "0"))
            except ValueError:
                raise ValueError(f"slot {k + 1}: the focus offset must be a whole number of steps")
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"names": clean_names, "offsets": clean_offsets}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.settings_path)
        self._names, self._offsets = clean_names, clean_offsets
        log.info("%s: filters saved: %s", self.slug, ", ".join(f"{a} ({b:+d})" for a, b in zip(clean_names, clean_offsets)))

    # ---------------------------------------------------------------- holding the wheel
    def _take(self, id_):
        if id_ is None or self.sdk is None:
            self.lost = self.lost or "not found yet"
            return False
        try:
            self.sdk.open(id_)
            self.one_way = self.sdk.one_way(id_)
            try:
                slots = self.sdk.info(id_)["slots"]
                if slots:
                    self.slots = slots
            except EfwError as e:
                if e.code != MOVING:            # MOVING: still counting its slots; poll() asks again
                    raise
            pos = self.sdk.position(id_)
        except EfwError as e:
            self.lost = f"cannot open it ({e}): in use by another program?"
            log.warning("%s: %s", self.slug, self.lost)
            try:
                self.sdk.close(id_)
            except EfwError:
                pass
            return False
        self.moving = pos < 0
        self.position = None if pos < 0 else pos
        self.id = id_
        self.lost = None
        self._ok_at = time.monotonic()
        with self.driver._lock:
            self.driver.held[self.unit] = self
        return True

    def _lose(self, why):
        with self._lock:
            if self.move is not None:
                self.last_move = {"text": f"lost while turning to {self.label(self.move['target'])}: {why}",
                                  "state": "bad"}
                self.move = None
            self.lost = why
            old, self.id = self.id, None
            self.moving = False
        with self.driver._lock:
            self.driver.held.pop(self.unit, None)
        if old is not None:
            try:
                self.sdk.close(old)
            except EfwError:
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
            self._stop.wait(POLL_MOVING_S if self.moving else POLL_IDLE_S)

    def poll(self):
        id_ = self.id
        if id_ is None:
            return
        try:
            if not self.slots:
                try:
                    self.slots = self.sdk.info(id_)["slots"] or None
                except EfwError as e:
                    if e.code != MOVING:
                        raise
            pos = self.sdk.position(id_)
            hw = self.hw_error
            now = time.monotonic()
            if pos >= 0 and now - self._hw_at >= HW_EVERY_S:
                self._hw_at = now
                hw = self.sdk.hw_error(id_)
        except EfwError as e:
            if e.code in (REMOVED, INVALID_ID):
                self._lose("not found: unplugged, or no power?")
            elif e.code == ERROR_STATE:
                with self._lock:
                    self._ok_at = time.monotonic()
                    if not self.error_state:
                        log.warning("%s: the wheel reports an error state", self.slug)
                    self.error_state, self.moving = True, False
                    if self.move is not None:
                        self.last_move = {"text": f"error while turning to {self.label(self.move['target'])}",
                                          "state": "bad"}
                        self.move = None
                try:
                    self.hw_error = self.sdk.hw_error(id_)
                except EfwError:
                    pass
            else:
                self.last_error = str(e)
                self._fails += 1
                if self._fails >= FAILS_TO_REOPEN:
                    self._fails = 0
                    self._lose(f"not answering ({e}): opening it again")
            return
        self._fails = 0
        with self._lock:
            self._ok_at = time.monotonic()
            self.error_state = False
            self.hw_error = hw
            prev, was_moving = self.position, self.moving
            moving = pos < 0
            mv = self.move
            if mv is not None and not moving and pos != mv["target"] and (
                    not mv.get("sent") or (pos == mv["from"] and time.monotonic() - mv["sent"] < START_GRACE_S)):
                # A turn of ours not started yet: its SDK call is still running (it can take a second on a busy USB
                # hub), or the wheel hasn't begun to move. Still "turning" (-1) for the client, not "stopped short".
                return
            self.moving = moving
            if not moving:
                self.position = pos
            if mv is not None:
                took = time.monotonic() - mv["t0"]
                if not moving:
                    self.move = None
                    self._ours_until = time.monotonic() + OURS_S
                    if pos == mv["target"]:
                        steps = self.steps(mv["from"], pos)
                        if steps and took > 0.2:
                            v = took / steps
                            self.slot_time = v if self.slot_time is None else 0.7 * self.slot_time + 0.3 * v
                        self.last_move = {"text": f"reached {self.label(pos)}", "state": "ok"}
                    else:
                        self.last_move = {"text": f"stopped at {self.label(pos)}, not {self.label(mv['target'])}",
                                          "state": "warn"}
                elif took > self.limit() and not mv.get("late"):
                    mv["late"] = True
                    self.last_move = {"text": f"not finishing: still turning to {self.label(mv['target'])} after "
                                              f"{took:.0f} s (the SDK can't stop it). Jammed?", "state": "bad"}
                    log.warning("%s: turn to slot %d not finishing after %.0f s", self.slug, mv["target"] + 1, took)
            elif time.monotonic() >= self._ours_until:
                self._note_other(prev, pos, moving, was_moving)

    def _note_other(self, prev, pos, moving, was_moving):
        """A turn the hub didn't start: another program has the wheel open too. Called with the lock held."""
        if moving:
            if self.other is None:
                self.other = {"from": prev}
                self.last_move = {"text": "being turned by another program", "state": "warn"}
                log.info("%s: being turned by another program, from slot %s", self.slug,
                         "?" if prev is None else prev + 1)
            return
        if self.other is not None:
            start = self.other["from"]
            self.other = None
        elif prev is not None and pos != prev:
            start = prev
        else:
            return
        self.last_move = {"text": f"turned by another program: {self.label(start)} → {self.label(pos)}",
                          "state": "warn"}
        log.info("%s: turned by another program to slot %d", self.slug, pos + 1)

    def steps(self, a, b):
        """Slots passed between a and b: the shorter way round, or forward only when the wheel is one-way."""
        n = self.slots or 0
        if a is None or b is None or not n:
            return 0
        fwd = (b - a) % n
        return fwd if self.one_way else min(fwd, n - fwd)

    def limit(self):
        return SLOT_LIMIT_S * (self.slots or 8) + LIMIT_EXTRA_S

    def label(self, slot):
        if slot is None:
            return "?"
        names = self.names()
        name = names[slot] if 0 <= slot < len(names) else ""
        return f"slot {slot + 1}" + (f" ({name})" if name and name != default_name(slot) else "")

    # ---------------------------------------------------------------- commands (Alpaca and the page)
    def ready(self):
        if self.id is None:
            raise AlpacaError(DRIVER_ERROR, f"The filter wheel is {self.lost or 'not available'}")
        if not self.slots:
            raise AlpacaError(DRIVER_ERROR, "The filter wheel is still counting its slots")

    def goto(self, slot, by):
        self.ready()
        if not 0 <= slot < self.slots:
            raise AlpacaError(INVALID_VALUE, f"Slot {slot} is outside 0 to {self.slots - 1}")
        if self.moving:
            raise AlpacaError(INVALID_OPERATION, "The filter wheel is turning: wait until it stops")
        if self.error_state:
            raise AlpacaError(DRIVER_ERROR, "The filter wheel reports an error state")
        pos = self.position
        with self._lock:
            self.other = None
            if pos == slot:
                # Already there: nothing to turn, and no SDK call (one can take over a second on a busy USB hub,
                # which ConformU took for a synchronous Position set, 2026-11-25).
                self.last_move = {"text": f"already at {self.label(slot)}", "state": "ok"}
                log.info("%s: already at slot %d (%s)", self.slug, slot + 1, by)
                return
            # Answer at once, as an Alpaca filter wheel should: Position reads -1 from now on, and the SDK call
            # (0.2 s on a quiet USB hub, up to 1.6 s on one shared with an FTDI adapter) runs in the background.
            mv = self.move = {"from": pos, "target": slot, "t0": time.monotonic(), "by": by, "sent": None}
            self.moving = True
            self.last_move = {"text": f"turning to {self.label(slot)}", "state": "warn"}
        log.info("%s: slot %s -> %d (%s)", self.slug, "?" if pos is None else pos + 1, slot + 1, by)
        threading.Thread(target=self._send, args=(self.id, mv), name=f"{self.slug}-turn", daemon=True).start()

    def _send(self, id_, mv):
        """EFWSetPosition for a turn goto() has already answered. A refusal ends the turn with the reason on the
        page and in the log (the client sees the wheel stop where it is)."""
        try:
            self.sdk.set_position(id_, mv["target"])
        except EfwError as e:
            with self._lock:
                if self.move is mv:
                    self.move = None
                    self.moving = False
                    why = "it was already turning" if e.code == MOVING else str(e)
                    self.last_move = {"text": f"turn to {self.label(mv['target'])} refused: {why}", "state": "bad"}
            log.warning("%s: turn to slot %d refused: %s", self.slug, mv["target"] + 1, e)
            return
        with self._lock:
            mv["sent"] = time.monotonic()

    # ---------------------------------------------------------------- what the hub asks
    def up(self):
        return self.id is not None and time.monotonic() - self._ok_at < UP_S

    def state(self):
        """(text, ok|warn|bad) for Overview and the page."""
        if self.id is None:
            return self.lost or "not available", "bad"
        if self.error_state:
            return "the wheel reports an error state" + (f" (code {self.hw_error})" if self.hw_error else ""), "bad"
        if self.last_move and self.last_move["state"] == "bad":
            return self.last_move["text"], "bad"
        if not self.slots:
            return "counting its slots", "warn"
        if self.moving:
            return (f"turning to {self.label(self.move['target'])}" if self.move
                    else "being turned by another program"), "warn"
        return "idle", "ok"

    def summary(self):
        text, st = self.state()
        if st == "ok":
            return {"text": self.label(self.position), "state": "ok"}
        return {"text": text, "state": st}

    def _sample(self):
        while not self._stop.wait(1.0):
            if self.up():
                # The slot in place: while turning it stays the old one (position isn't updated until the turn
                # ends), so the chart is continuous and jumps when the new filter is there.
                self.app.history.add(time.time(), {"pos": None if self.position is None else self.position + 1})

    def stop(self):
        """Lets go of the wheel (a turn finishes on its own: the SDK has no stop)."""
        self._stop.set()
        self.srv.stop()
        id_ = self.id
        if id_ is not None:
            try:
                self.sdk.close(id_)
            except EfwError:
                pass
        with self.driver._lock:
            if self.driver.held.get(self.unit) is self:
                self.driver.held.pop(self.unit, None)
        self.id = None


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# ---------------------------------------------------------------- Alpaca FilterWheel
class EfwFilterWheel(AlpacaDevice):
    type, type_name, interface_version = "filterwheel", "FilterWheel", 3
    driver_version = VERSION
    driver_info = "ZWO EFW through ZWO's EFW SDK, alpaca-hub"
    GET = {"names", "focusoffsets", "position", "devicestate"}
    PUT = {"position"}

    def __init__(self, inst, uid, name):
        super().__init__(uid, name, "ZWO EFW filter wheel")
        self.inst = inst

    def set_connected(self, on):
        if on and self.inst.id is None:
            raise AlpacaError(0x500, f"The filter wheel is {self.inst.lost or 'not available'}")
        self.connected = on

    def _position(self):
        i = self.inst
        return -1 if i.moving or i.position is None else i.position

    def get(self, m, p):
        if m not in self.GET:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        i = self.inst
        i.ready()
        if m == "names":
            return i.names()
        if m == "focusoffsets":
            return i.offsets()
        if m == "position":
            return self._position()
        return [{"Name": "Position", "Value": self._position()}, {"Name": "TimeStamp", "Value": _now_iso()}]

    def put(self, m, p):
        if m not in self.PUT:
            raise KeyError(m)
        if not self.connected:
            raise AlpacaError(NOT_CONNECTED, "Not connected")
        try:
            slot = int(str(p.get("position", "")).strip())
        except ValueError:
            raise BadRequest("Missing or invalid Position")
        self.inst.goto(slot, "Alpaca")
        return None


# ---------------------------------------------------------------- the page
class EfwApp(App):
    ui_dir = UI

    def __init__(self, inst):
        self.inst = inst
        self.history = History(["pos"], held=("pos",))      # a slot: exact changes, never an average

    def status(self):
        i = self.inst
        text, st = i.state()
        return {"name": i.title, "model": i.ident.get("name"), "serial": i.unit,
                "has_serial": i.ident.get("has_serial", True), "firmware": i.firmware, "sdk_id": i.id,
                "sdk": _sdk_version(i.sdk), "up": i.up(), "lost": i.lost, "state": text, "state_class": st,
                "slots": i.slots, "position": None if i.moving else i.position, "moving": bool(i.moving),
                "target": i.move["target"] if i.move else None, "names": i.names(), "offsets": i.offsets(),
                "one_way": i.one_way, "hw_error": i.hw_error, "error_state": i.error_state,
                "last_move": i.last_move, "slot_time": None if i.slot_time is None else round(i.slot_time, 2),
                "error": i.last_error, "connected": i.wheel.connected, "uid": i.wheel.uid}

    def control(self, form):
        op = form.get("op", "")
        log.info("%s page: %s %s", self.inst.slug, op, form.get("value", ""))
        try:
            if op == "goto":
                try:
                    slot = int(str(form.get("value", "")).strip())
                except ValueError:
                    return 400, "Choose a slot"
                self.inst.goto(slot, "page")
                return 200, f"Turning to {self.inst.label(slot)}"
            if op == "filters":
                n = self.inst.slots or 0
                names = [form.get(f"name{k}", "") for k in range(n)]
                offsets = [form.get(f"offset{k}", "0") for k in range(n)]
                try:
                    self.inst.save_filters(names, offsets)
                except ValueError as e:
                    return 400, str(e)
                return 200, "Filters saved"
        except AlpacaError as e:
            return 409, e.message
        return 400, "op: goto or filters"


def _sdk_version(sdk):
    try:
        return sdk.sdk_version() if sdk is not None else None
    except Exception:
        return None


DRIVERS = [ZwoEfwDriver]
