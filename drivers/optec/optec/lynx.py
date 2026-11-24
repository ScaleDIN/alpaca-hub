"""Optec FocusLynx and ThirdLynx focuser hubs: up to two focusers, F1 and F2.

Wire format (115200 8N1):
- command: "<" target verb [argument] ">"; target F1, F2 or FH (the hub itself). Example: <F1MA012345>
- the hub first answers "!" (accepted) or "ER=<n> <text>" (refused, then lines up to END)
- then, for GET… commands, a block: a header line (STATUS1, CONFIG1, HUB INFO …), "Key      = value" lines, END
- other commands answer one word: MA -> "M", HOME -> "H", HALT -> "HALTED", SCCP/REVERSE -> "SET",
  HELLO -> the focuser's nickname

Status keys (ThirdLynx fw 3.0.4): Temp(C) ("NP" without a probe), Curr Pos, Targ Pos, IsMoving, IsHoming, IsHomed,
FFDetect, TmpProbe, RemoteIO, Hnd Ctlr, Reverse.
Config keys: Nickname, Max Pos, Dev Typ ("ZZ" = no focuser configured), TComp ON, TempCo A–E, TC Mode, BLC En,
BLC Stps, LED Brt, TC@Start.
"""
import time
from dataclasses import dataclass, field

from .wire import BAUD, NoAnswer, NotReady, OptecError, Unit, as_bool, as_int, as_temp, log, parse_block, raise_lines

USB_THIRDLYNX = (0x04D8, 0xED77)   # in the ThirdLynx firmware's USB descriptor (fw 3.0.4); Optec's driver looks for it
USB_OPTEC_FTDI = (0x0403, 0x6001)  # Optec's serial-to-USB cable for the older FocusLynx and the Gemini
NO_DEVICE = "ZZ"
ANSWER_S = 1.5
MAX_LIMIT = 999999                 # six digits in MA/SCCP


@dataclass
class FocusStatus:
    position: int
    target: int
    moving: bool
    homing: bool
    homed: bool
    temperature: float | None     # None: no probe
    reverse: bool
    t: float = 0.0                # clock time the status was read


@dataclass
class FocusConfig:
    nickname: str
    max_pos: int
    dev_type: str
    raw: dict = field(default_factory=dict)   # every GETCONFIG key, for the page's read-only settings


def frame(target, verb, arg=""):
    return f"<{target}{verb}{arg}>"


def exchange(wire, target, verb, arg="", timeout=ANSWER_S):
    """Send one command and return its answer lines (after the "!"). Raises OptecError when the hub refuses it,
    NoAnswer when nothing comes."""
    wire.clear()
    wire.send(frame(target, verb, arg))
    deadline = wire.clock() + timeout
    first = wire.line(deadline)
    if first is None:
        raise NoAnswer(f"no answer to {frame(target, verb, arg)}")
    if first.startswith("ER=") or first.startswith("ERROR"):
        rest = []
        while True:                                   # the explanation may follow, up to END
            ln = wire.line(min(deadline, wire.clock() + 0.3))
            if ln is None or ln == "END":
                break
            rest.append(ln)
        code_text = first.split("=", 1)[1].strip()
        code, _, text = code_text.partition(" ")
        text = " ".join([text.strip()] + rest).strip() or "refused"
        try:
            code = int(code)
        except ValueError:
            code, text = None, code_text
        raise OptecError(code, text)
    if first != "!":
        raise NoAnswer(f"unexpected answer {first!r} to {frame(target, verb, arg)}")
    if verb.startswith("GET"):
        lines = []
        while True:
            ln = wire.line(deadline)
            if ln is None:
                raise NoAnswer(f"{frame(target, verb, arg)}: no END after {len(lines)} lines")
            if ln == "END":
                return lines
            lines.append(ln)
    ln = wire.line(deadline)
    if ln is None:
        raise NoAnswer(f"{frame(target, verb, arg)}: accepted but no reply word")
    return [ln]


def parse_status(lines, t=0.0):
    b = parse_block(lines)
    if "Curr Pos" not in b:
        raise NoAnswer(f"status without Curr Pos: {lines!r}")
    return FocusStatus(position=as_int(b, "Curr Pos", 0), target=as_int(b, "Targ Pos", 0),
                       moving=as_bool(b, "IsMoving"), homing=as_bool(b, "IsHoming"), homed=as_bool(b, "IsHomed"),
                       temperature=as_temp(b, "Temp(C)"), reverse=as_bool(b, "Reverse"), t=t)


def parse_config(lines):
    b = parse_block(lines)
    return FocusConfig(nickname=b.get("Nickname", ""), max_pos=as_int(b, "Max Pos", 0) or 0,
                       dev_type=b.get("Dev Typ", NO_DEVICE) or NO_DEVICE, raw=b)


def probe(ser, clock=time.monotonic):
    """Identity of a FocusLynx/ThirdLynx on this open port, or None. Asks only for the hub's info (read only)."""
    from .wire import Wire
    raise_lines(ser)
    time.sleep(0.1)
    w = Wire(ser, clock)
    try:
        info = parse_block(exchange(w, "FH", "GETHUBINFO", timeout=1.0))
    except (OptecError, OSError, NoAnswer):
        return None
    fw = info.get("Hub FVer")
    if not fw:
        return None
    ident = {"model": "FocusLynx", "firmware": fw}
    try:                                             # only the ThirdLynx knows GETVER; older hubs refuse it
        lines = exchange(w, "FH", "GETVER", timeout=0.8)
        v = parse_block(lines)
        if any(ln.strip() == "ThirdLynx" for ln in lines) or "OneLynx" in v:
            ident["model"] = "ThirdLynx"
        if v.get("Board ID"):
            ident["serial"] = v["Board ID"]
        if v.get("Main FWV"):
            ident["firmware"] = v["Main FWV"]
    except (OptecError, OSError, NoAnswer):
        pass
    return ident


class LynxUnit(Unit):
    """One FocusLynx/ThirdLynx hub. channels: the focuser targets in use ("F1", "F2")."""

    TARGETS = ("F1", "F2")
    SETTLE_S = 2.0           # how long a move may take to show in the status before it counts as done

    def __init__(self, port_name, opener, clock=time.monotonic):
        super().__init__(port_name, opener, clock)
        self.channels = []
        self.config = {}           # target -> FocusConfig
        self.status = {}           # target -> FocusStatus
        self.hub_info = {}
        self._pending = {}         # target -> (requested position, clock time)

    def prepare(self, ser):
        raise_lines(ser)

    def hello(self):
        w = self.wire
        try:
            self.hub_info = parse_block(exchange(w, "FH", "GETHUBINFO"))
        except OptecError:
            self.hub_info = {}
        found = []
        for t in self.TARGETS:
            try:
                cfg = parse_config(exchange(w, t, "GETCONFIG"))
            except OptecError as e:          # e.g. "invalid target device" on a one-focuser hub
                log.info("%s %s: %s", self.port_name, t, e)
                continue
            self.config[t] = cfg
            if cfg.dev_type != NO_DEVICE:
                found.append(t)
        if not found and "F1" in self.config:
            found = ["F1"]                   # shown, with a note that no focuser type is set
        if not self.channels:
            self.channels = found
        self.poll()

    def poll(self):
        for t in self.channels:
            self.status[t] = parse_status(exchange(self.wire, t, "GETSTATUS"), self.clock())

    # -- what the Alpaca devices and the page use
    def snapshot(self, t):
        return self.status.get(t)

    def max_pos(self, t):
        cfg = self.config.get(t)
        return cfg.max_pos if cfg and cfg.max_pos > 0 else MAX_LIMIT

    def configured(self, t):
        cfg = self.config.get(t)
        return bool(cfg) and cfg.dev_type != NO_DEVICE

    def is_moving(self, t):
        st = self.status.get(t)
        if st is None:
            return False
        if st.moving or st.homing:
            return True
        req = self._pending.get(t)
        if req is None:
            return False
        pos, since = req
        # Until a status read after the command shows the new target (and no motion), the move counts as running,
        # so a client polling IsMoving right after Move never sees "done" before the motor has started.
        if (st.t >= since and st.target == pos and st.position == pos) or self.clock() - since > self.SETTLE_S:
            del self._pending[t]
            return False
        return True

    def move(self, t, pos):
        if not self.configured(t):
            raise NotReady("No focuser type is set for this port; set it in Optec's FocusLynx Commander first")
        if not 0 <= pos <= self.max_pos(t):
            raise ValueError(f"Position {pos} is outside 0..{self.max_pos(t)}")

        def go():
            exchange(self.wire, t, "MA", f"{pos:06d}")
            self._pending[t] = (pos, self.clock())
            self.status[t] = parse_status(exchange(self.wire, t, "GETSTATUS"), self.clock())
        self.run(go)

    def halt(self, t):
        def go():
            exchange(self.wire, t, "HALT")
            self._pending.pop(t, None)
            self.status[t] = parse_status(exchange(self.wire, t, "GETSTATUS"), self.clock())
        self.run(go)

    def home(self, t):
        if not self.configured(t):
            raise NotReady("No focuser type is set for this port")
        self.run(lambda: exchange(self.wire, t, "HOME"))

    def sync(self, t, pos):
        """Tell the hub the focuser is at pos (SCCP: the motor doesn't move)."""
        if not 0 <= pos <= self.max_pos(t):
            raise ValueError(f"Position {pos} is outside 0..{self.max_pos(t)}")
        self.run(lambda: exchange(self.wire, t, "SCCP", f"{pos:06d}"))


__all__ = ["BAUD", "USB_THIRDLYNX", "USB_OPTEC_FTDI", "LynxUnit", "FocusStatus", "FocusConfig", "probe",
           "exchange", "parse_status", "parse_config", "OptecError"]
