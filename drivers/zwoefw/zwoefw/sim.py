"""Simulated ZWO EFWs behind the same methods as the real SDK (efw.Sdk), for --simulate and the tests.

A move turns slot by slot at a set time per slot (the shorter way round, or always forward when the wheel is set to
one-way rotation); the position reads -1 meanwhile, as on the real wheel. Test hooks: unplug()/plug(), in_use
(another program holds it), counting (EFWGetProperty answers MOVING while it counts its slots, after a plug-in),
error() (the wheel reports an error state), hang() (a move that never finishes), turn() (another program moves it).
Read from a real 7-slot EFW (2026-11-24): USB PID 1F01, firmware 3.0.9, a serial number, the slot count given by
EFWGetProperty even before the wheel is opened (the header says 0 then). Numbers still marked "guess" weren't.
"""
import threading
import time

from .efw import CLOSED, ERROR_STATE, INVALID_ID, INVALID_VALUE, MOVING, NOT_SUPPORTED, REMOVED, EfwError

SLOT_S = 1.1             # seconds per slot: a real 7-slot EFW took 1.6-1.8 s for one slot, 3.3 s for three (2026-11-24)
PID = 0x1F01             # the EFW's USB PID, as read on a real wheel (2026-11-24)


class SimEfw:
    def __init__(self, serial="EFW0000000000001", name="EFW", slots=7, sdk_id=0, firmware="3.0.9", position=0,
                 slot_s=SLOT_S, one_way=False, pid=PID):
        self.serial, self.name, self.slots, self.sdk_id, self.fw = serial, name, slots, sdk_id, firmware
        self.slot_s, self.one_way_on, self.pid = slot_s, one_way, pid
        self.present = True
        self.in_use = False
        self.is_open = False
        self.counting = False
        self.hw_code = 0
        self._lock = threading.Lock()
        self._pos = position
        self._move = None            # (start slot, target, steps, start time) while turning
        self._hang = False
        self._error = False
        self.moves = []              # targets sent with set_position(), for the tests

    # ---------------------------------------------------------------- test hooks
    def unplug(self):
        with self._lock:
            self._settle()
            self.present = self.is_open = False
            self._move = None

    def plug(self):
        self.present = True

    def error(self, code=1):
        with self._lock:
            self._error, self.hw_code, self._move = True, code, None

    def hang(self):
        """The next moves never finish (the position stays -1)."""
        self._hang = True

    def turn(self, slot):
        """Another program (or a second hub) moves the wheel: no open needed here."""
        with self._lock:
            self._start(slot)

    # ---------------------------------------------------------------- motion
    def _steps(self, a, b):
        fwd = (b - a) % self.slots
        return fwd if self.one_way_on else min(fwd, self.slots - fwd)

    def _start(self, slot):
        self._settle()
        start = self._pos
        self._move = (start, int(slot), self._steps(start, slot), time.monotonic())

    def _settle(self):
        if self._move is None or self._hang:
            return
        start, target, steps, t0 = self._move
        if time.monotonic() - t0 >= steps * self.slot_s:
            self._pos, self._move = target, None

    def _check(self, need_open=True):
        if not self.present:
            raise EfwError(REMOVED)
        if need_open and not self.is_open:
            raise EfwError(CLOSED)

    def open(self):
        if not self.present:
            raise EfwError(REMOVED)
        if self.in_use:
            raise EfwError(INVALID_ID)
        self.is_open = True

    def close(self):
        self.is_open = False

    def info(self):
        self._check(need_open=False)
        if self.counting:
            raise EfwError(MOVING)
        return {"name": self.name, "slots": self.slots}     # a real wheel gave its count before EFWOpen too

    def position(self):
        self._check()
        if self._error:
            raise EfwError(ERROR_STATE)
        with self._lock:
            self._settle()
            return -1 if self._move is not None else self._pos

    def set_position(self, slot):
        self._check()
        if self._error:
            raise EfwError(ERROR_STATE)
        if not 0 <= slot < self.slots:
            raise EfwError(INVALID_VALUE)
        with self._lock:
            self._settle()
            if self._move is not None:
                raise EfwError(MOVING)
            self.moves.append(int(slot))
            self._start(slot)


class SimSdk:
    """efw.Sdk's methods over simulated wheels."""

    def __init__(self, units):
        self.units = list(units)

    def _u(self, id_):
        for u in self.units:
            if u.sdk_id == id_ and u.present:
                return u
        raise EfwError(INVALID_ID)

    def ids(self):
        return [u.sdk_id for u in self.units if u.present]

    def info(self, id_):
        return self._u(id_).info()

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

    def position(self, id_):
        return self._u(id_).position()

    def set_position(self, id_, slot):
        self._u(id_).set_position(slot)

    def one_way(self, id_):
        u = self._u(id_)
        u._check()
        return u.one_way_on

    def hw_error(self, id_):
        u = self._u(id_)
        u._check()
        return u.hw_code


def seven(index):
    return SimEfw(serial=f"EFW70000000000{index + 1:02X}", slots=7, sdk_id=index)


def five(index):
    return SimEfw(serial=f"EFW50000000000{index + 1:02X}", slots=5, sdk_id=index)


def eight(index):
    return SimEfw(serial=f"EFW80000000000{index + 1:02X}", slots=8, sdk_id=index)
