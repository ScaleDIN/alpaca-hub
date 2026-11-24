"""Simulated ZWO EAFs behind the same methods as the real SDK (eaf.Sdk), for --simulate and the tests.

Moves run at a set speed in real time. Test hooks: unplug()/plug(), jam() (stops the motor where it is; with firmware
that reports it, motor code E5), hang() (a move that never finishes), hand_move() (the hand controller: EAFStop can't
stop it), in_use (another program holds it). Numbers not yet read from a real unit are marked "guess": REFACTOR.md
"Focuser and filter wheel", step 1 reads them.
"""
import threading
import time

from .eaf import CLOSED, GENERAL_ERROR, INVALID_ID, INVALID_VALUE, NOT_SUPPORTED, REMOVED, EafError

SPEED = 370.0            # steps/s, as a real EAF moved (2026-11-24)
MAX_STEP = 60000         # the focuser's max step (guess)


class SimEaf:
    def __init__(self, serial="1A2B3C4D5E6F7081", name="EAF", kind=None, sdk_id=0, firmware="3.0.0",
                 max_step=MAX_STEP, position=30000, speed=SPEED, temperature=12.5, codes=True, pid=0x1F10):
        self.serial, self.name, self.kind, self.sdk_id, self.fw = serial, name, kind, sdk_id, firmware
        self.max_step, self.speed, self.temp, self.codes, self.pid = max_step, speed, temperature, codes, pid
        self.reverse, self.backlash, self.beep = False, 0, True
        self.present = True
        self.in_use = False          # another program has it open
        self.is_open = False
        self._lock = threading.Lock()
        self._pos = float(position)
        self._target = None          # (start pos, target, start time) while moving
        self._hand = False
        self._jam = None             # position the motor stops at
        self._jam_code = False
        self._hang = False
        self.code = "E0"
        self.moves = []              # targets sent with move(), for the tests
        self.stops = 0

    # ---------------------------------------------------------------- test hooks
    def unplug(self):
        with self._lock:
            self._settle()
            self.present = False
            self.is_open = False
            self._target = None

    def plug(self):
        self.present = True

    def jam(self, at=None, reported=True):
        """The focuser jams at `at` (default: where it is now). reported: the firmware raises E5 there."""
        with self._lock:
            self._settle()
            self._jam = self._pos if at is None else float(at)
            self._jam_code = reported and self.codes

    def hang(self):
        """The next moves never finish (IsMoving stays true, the position stays)."""
        self._hang = True

    def hand_move(self, target):
        with self._lock:
            self._settle()
            self._target = (self._pos, float(max(0, min(self.max_step, target))), time.monotonic())
            self._hand = True

    # ---------------------------------------------------------------- motion
    def _settle(self):
        """Bring the position up to now (call with the lock)."""
        if self._target is None:
            return
        start, target, t0 = self._target
        if self._hang:
            return
        d = self.speed * (time.monotonic() - t0)
        pos = target if d >= abs(target - start) else start + (d if target > start else -d)
        if self._jam is not None and min(start, pos) <= self._jam <= max(start, pos) and start != self._jam:
            self._pos, self._target, self._hand = self._jam, None, False
            if self._jam_code:
                self.code = "E5"
            return
        self._pos = pos
        if pos == target:
            self._target, self._hand = None, False

    def _check(self, need_open=True):
        if not self.present:
            raise EafError(REMOVED)
        if need_open and not self.is_open:
            raise EafError(CLOSED)

    def open(self):
        if not self.present:
            raise EafError(REMOVED)
        if self.in_use:
            raise EafError(GENERAL_ERROR)
        self.is_open = True

    def close(self):
        self.is_open = False

    def position(self):
        self._check()
        with self._lock:
            self._settle()
            return int(round(self._pos))

    def move(self, step):
        self._check()
        if not 0 <= step <= self.max_step:
            raise EafError(INVALID_VALUE)
        with self._lock:
            self._settle()
            self.moves.append(int(step))
            if self._jam is not None and self._jam_code and self.code == "E5":
                self.code = "E0"           # a new move clears the stall; it jams again where it's stuck
            self._target = (self._pos, float(step), time.monotonic())
            self._hand = False

    def stop(self):
        self._check()
        with self._lock:
            self._settle()
            self.stops += 1
            if not self._hand:
                self._target = None
                self._hang = False

    def is_moving(self):
        self._check()
        with self._lock:
            self._settle()
            return self._target is not None, self._hand

    def temperature(self):
        self._check()
        with self._lock:
            if self._hand or self.temp is None:
                return None
            return round(self.temp, 2)

    def motor_code(self):
        self._check()
        if not self.codes:
            raise EafError(NOT_SUPPORTED)
        with self._lock:
            self._settle()
            return self.code


class SimSdk:
    """eaf.Sdk's methods over simulated focusers."""

    def __init__(self, units):
        self.units = list(units)

    def _u(self, id_):
        for u in self.units:
            if u.sdk_id == id_ and u.present:
                return u
        raise EafError(INVALID_ID)

    def ids(self):
        return [u.sdk_id for u in self.units if u.present]

    def info(self, id_):
        u = self._u(id_)
        return {"name": u.name, "max_step": u.max_step}

    def sdk_version(self):
        return "sim"

    def open(self, id_):
        self._u(id_).open()

    def close(self, id_):
        self._u(id_).close()

    def serial(self, id_):
        u = self._u(id_)
        u._check()
        return u.serial

    def firmware(self, id_):
        u = self._u(id_)
        u._check()
        return u.fw

    def kind(self, id_):
        return self._u(id_).kind

    def position(self, id_):
        return self._u(id_).position()

    def move(self, id_, step):
        self._u(id_).move(step)

    def stop(self, id_):
        self._u(id_).stop()

    def is_moving(self, id_):
        return self._u(id_).is_moving()

    def temperature(self, id_):
        return self._u(id_).temperature()

    def max_step(self, id_):
        u = self._u(id_)
        u._check()
        return u.max_step

    def reverse(self, id_):
        return self._u(id_).reverse

    def backlash(self, id_):
        return self._u(id_).backlash

    def beep(self, id_):
        return self._u(id_).beep

    def motor_code(self, id_):
        try:
            return self._u(id_).motor_code()
        except EafError as e:
            if e.code == NOT_SUPPORTED:
                return None
            raise


def standard(index):
    return SimEaf(serial=f"EAF00000000000{index + 1:02X}", name="EAF", sdk_id=index)


def five_volt(index):
    # The EAF 5V: the same SDK. Its name and whether it reports a temperature are guesses until step 1 reads one.
    return SimEaf(serial=f"EAF5V000000000{index + 1:02X}", name="EAF", kind="EAF-5V", sdk_id=index)
