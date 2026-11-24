"""Optec Gemini and Hercules focusing rotators: one focuser (F1) and one rotator (R1) on one hub (H1).

The same firmware runs all four models (GeminiMainBoardFW 2.0.1 names "Gemini", "Gemini V2", "Hercules",
"Hercules V2"); Optec's one driver ("Optec Gemini & Hercules") covers them.

Wire format (115200 8N1):
- command: "<" device index tid verb [payload] ">": device F (focuser), R (rotator) or H (hub); index 1; tid a two-digit
  transaction number; verb exactly six letters. Example: <R107MOVEPA090000> (rotator to 90.000°)
- the hub answers "!" + tid (accepted), or "ERROR ID = n", "ERROR TEXT = …", END (refused)
- GET… verbs then send "Key = value" lines and END

Focuser status (GETSTA): CurrTemp, CurrStep, TargStep, IsMoving, IsHoming, Is Homed, TempProb, RemoteIO, HCStatus,
Home Swi. Focuser config (GETCFG): Nickname, MaxSteps, Dev Type, TComp On, TCMode A–E, CurrenTC, BLCompOn,
BLCSteps, TC Start, HOnStart.
Rotator status: CurrStep, TargStep, LastStep, CurentPA, TargetPA (thousandths of a degree), IsMoving, IsHoming,
Is Homed. Rotator config: Nickname, MaxSteps, Dev Type, BLCompOn, BLCSteps, HOnStart, iReverse, MaxSpeed.
"""
import re
import time
from dataclasses import dataclass

from .wire import BAUD, NoAnswer, NotReady, OptecError, Unit, Wire, as_bool, as_int, as_temp, log, parse_block, raise_lines

USB_OPTEC_FTDI = (0x0403, 0x6001)  # Optec's serial-to-USB cable
ANSWER_S = 1.5
_ACK = re.compile(r"^!(\d\d)?$")
GET_VERBS = {"GETSTA", "GETCFG", "GETDNN", "GETSER", "GETNET", "GETHIP", "GETWIP", "GETDBG"}
MILLI = 1000.0                      # rotator angles are sent and reported in thousandths of a degree


@dataclass
class AxisStatus:
    position: int                   # steps
    target: int
    moving: bool
    homing: bool
    homed: bool
    temperature: float | None = None
    pa: float | None = None         # rotator: mechanical angle, degrees
    target_pa: float | None = None
    t: float = 0.0


class Framer:
    def __init__(self):
        self._tid = 0

    def next_tid(self):
        self._tid = self._tid % 99 + 1
        return self._tid

    def frame(self, device, verb, payload="", tid=None):
        if len(verb) != 6:
            raise ValueError("Gemini verbs are six letters")
        return f"<{device}1{(tid if tid is not None else self.next_tid()):02d}{verb}{payload}>"


def exchange(wire, framer, device, verb, payload="", timeout=ANSWER_S):
    tid = framer.next_tid()
    cmd = framer.frame(device, verb, payload, tid)
    wire.clear()
    wire.send(cmd)
    deadline = wire.clock() + timeout
    first = wire.line(deadline)
    # Skip what's left of an earlier answer. A Hercules on firmware 1.3.1 (2026-11-23) answers SETREV with "!nn" and
    # then, a moment later, "END": it arrives after wire.clear(), and taken as the next command's answer it put every
    # answer one step behind until the driver gave up ("unexpected answer 'END'"). Every answer starts with "!" or
    # "ERROR", so anything else before it is a leftover.
    while first is not None and not (first.startswith("ERROR") or first.startswith("ER=") or _ACK.match(first)):
        log.debug("%s: skipped %r, left over from an earlier answer", cmd, first)
        first = wire.line(deadline)
    if first is None:
        raise NoAnswer(f"no answer to {cmd}")
    if first.startswith("ERROR") or first.startswith("ER="):
        lines = [first]
        while lines[-1] != "END":
            ln = wire.line(min(deadline, wire.clock() + 0.3))
            if ln is None:
                break
            lines.append(ln)
        b = parse_block(lines)
        code = as_int(b, "ERROR ID", as_int(b, "ER"))
        raise OptecError(code, b.get("ERROR TEXT") or first)
    m = _ACK.match(first)
    if m.group(1) and int(m.group(1)) != tid:
        log.debug("%s acknowledged with tid %s", cmd, m.group(1))
    if verb not in GET_VERBS:
        return []
    lines = []
    while True:
        ln = wire.line(deadline)
        if ln is None:
            raise NoAnswer(f"{cmd}: no END after {len(lines)} lines")
        if ln == "END":
            return lines
        lines.append(ln)


def parse_focus_status(lines, t=0.0):
    b = parse_block(lines)
    if "CurrStep" not in b:
        raise NoAnswer(f"focuser status without CurrStep: {lines!r}")
    return AxisStatus(position=as_int(b, "CurrStep", 0), target=as_int(b, "TargStep", 0),
                      moving=as_bool(b, "IsMoving"), homing=as_bool(b, "IsHoming"), homed=as_bool(b, "Is Homed"),
                      temperature=as_temp(b, "CurrTemp"), t=t)


def parse_rotator_status(lines, t=0.0):
    b = parse_block(lines)
    if "CurentPA" not in b:
        raise NoAnswer(f"rotator status without CurentPA: {lines!r}")
    tp = as_int(b, "TargetPA")
    return AxisStatus(position=as_int(b, "CurrStep", 0), target=as_int(b, "TargStep", 0),
                      moving=as_bool(b, "IsMoving"), homing=as_bool(b, "IsHoming"), homed=as_bool(b, "Is Homed"),
                      pa=as_int(b, "CurentPA", 0) / MILLI, target_pa=None if tp is None else tp / MILLI, t=t)


def probe(ser, clock=time.monotonic):
    """Identity of a Gemini/Hercules on this open port, or None. Read-only questions to the hub."""
    raise_lines(ser)
    time.sleep(0.1)
    w, f = Wire(ser, clock), Framer()
    try:
        cfg = parse_block(exchange(w, f, "H", "GETCFG", timeout=1.0))
    except (OptecError, OSError, NoAnswer):
        return None
    fw = cfg.get("Firmware")
    if not fw:
        return None
    ident = {"model": "Gemini / Hercules", "firmware": fw}
    try:
        ser_no = parse_block(exchange(w, f, "H", "GETSER", timeout=0.8)).get("Serial Number")
        if ser_no and ser_no.strip("0"):
            ident["serial"] = ser_no
    except (OptecError, OSError, NoAnswer):
        pass
    return ident


class GeminiUnit(Unit):
    SETTLE_S = 2.0

    def __init__(self, port_name, opener, clock=time.monotonic):
        super().__init__(port_name, opener, clock)
        self.framer = Framer()
        self.focus = None              # AxisStatus
        self.rotator = None
        self.focus_cfg = {}
        self.rotator_cfg = {}
        self.hub_cfg = {}
        self._pending = {}             # "F"/"R" -> (requested target, clock time, in degrees for "R")

    def _x(self, device, verb, payload=""):
        return exchange(self.wire, self.framer, device, verb, payload)

    def prepare(self, ser):
        raise_lines(ser)

    def hello(self):
        self.hub_cfg = parse_block(self._x("H", "GETCFG"))
        self.focus_cfg = parse_block(self._x("F", "GETCFG"))
        self.rotator_cfg = parse_block(self._x("R", "GETCFG"))
        self.poll()

    def poll(self):
        now = self.clock()
        self.focus = parse_focus_status(self._x("F", "GETSTA"), now)
        self.rotator = parse_rotator_status(self._x("R", "GETSTA"), self.clock())

    # -- focuser
    def focus_max(self):
        return as_int(self.focus_cfg, "MaxSteps", 0) or 0

    def is_moving(self, dev):
        st = self.focus if dev == "F" else self.rotator
        if st is None:
            return False
        if st.moving or st.homing:
            return True
        req = self._pending.get(dev)
        if req is None:
            return False
        target, since = req
        if dev == "F":
            done = st.target == target and st.position == target
        else:
            done = st.target_pa is not None and abs(_wrap(st.target_pa - target)) < 0.01 \
                and abs(_wrap(st.pa - target)) < 0.05
        if (st.t >= since and done) or self.clock() - since > self.SETTLE_S:
            del self._pending[dev]
            return False
        return True

    def focus_move(self, pos):
        mx = self.focus_max()
        if not 0 <= pos <= (mx or 999999):
            raise ValueError(f"Position {pos} is outside 0..{mx}")

        def go():
            self._x("F", "MOVABS", f"{pos:06d}")
            self._pending["F"] = (pos, self.clock())
            self.focus = parse_focus_status(self._x("F", "GETSTA"), self.clock())
        self.run(go)

    def halt(self, dev):
        def go():
            self._x(dev, "DOHALT")
            self._pending.pop(dev, None)
            if dev == "F":
                self.focus = parse_focus_status(self._x("F", "GETSTA"), self.clock())
            else:
                self.rotator = parse_rotator_status(self._x("R", "GETSTA"), self.clock())
        self.run(go)

    def home(self, dev):
        self.run(lambda: self._x(dev, "DOHOME"))

    # -- rotator (mechanical degrees; the sky offset lives in the hub driver's settings)
    def rotator_reverse(self):
        return as_bool(self.rotator_cfg, "iReverse")

    def set_rotator_reverse(self, on):
        def go():
            self._x("R", "SETREV", "1" if on else "0")
            self.rotator_cfg = parse_block(self._x("R", "GETCFG"))
        self.run(go)

    def rotator_move(self, mech_deg):
        target = round(mech_deg % 360.0, 3) % 360.0
        if self.rotator is not None and not self.rotator.homed:
            raise NotReady("The rotator is not homed yet: home it first (hub page, or Optec's software)")

        def go():
            self._x("R", "MOVEPA", f"{int(round(target * MILLI)):06d}")
            self._pending["R"] = (target, self.clock())
            self.rotator = parse_rotator_status(self._x("R", "GETSTA"), self.clock())
        self.run(go)


def _wrap(d):
    """Signed difference in degrees, -180..180."""
    return (d + 180.0) % 360.0 - 180.0


__all__ = ["BAUD", "USB_OPTEC_FTDI", "GeminiUnit", "AxisStatus", "Framer", "exchange", "probe",
           "parse_focus_status", "parse_rotator_status", "OptecError"]
