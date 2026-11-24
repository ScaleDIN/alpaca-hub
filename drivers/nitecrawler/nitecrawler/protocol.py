"""Moonlite NiteCrawler serial protocol (stock firmware, checked against firmware 1.6 on a WR35).

57600 baud, 8N1. Every command ends in '#'. Axis 1 = focus, 2 = rotator, 3 = aux.
The firmware parses a command as soon as its '#' arrives (inside the receive interrupt) and answers at once:
* "get" commands answer with a value and '#' (e.g. "1GP#" -> "00012345#", negative "-0001234#").
* "set" commands that start or set something answer a single '#'.
* "SH nn#" (find home) answers '#' at once and "OK#" later, when homing has finished.
* "Pt nnn#" (temperature offset) answers nothing.

Sources: the firmware's format strings and parser (backup_x128a1_20261119/flash.bin), and the INDI NightCrawler
driver (indilib/indi drivers/rotator/nightcrawler.cpp), which was written against the real hardware.
"""
import re
from dataclasses import dataclass, field

BAUD = 57600
FOCUS, ROTATOR, AUX = 1, 2, 3
AXES = (FOCUS, ROTATOR, AUX)
AXIS_NAMES = {FOCUS: "focus", ROTATOR: "rotator", AUX: "aux"}

# Rotator steps per full turn, by model (PF# answer). From the INDI driver.
STEPS_PER_REV = {"2.5 NC": 374920, "3.0 NC": 444080, "3.5 NC": 505960}
DEFAULT_MODEL = "3.5 NC"

# GS# bit meanings
LIMIT_ROT_HOME = 0x01
LIMIT_OUT = 0x02
LIMIT_IN = 0x04

# SH nn# bit meanings (which axes to home)
HOME_FOCUS, HOME_ROTATOR, HOME_AUX = 1, 2, 4

POS_MIN, POS_MAX = -99_999_999, 99_999_999   # what fits the firmware's 8-character answer
STEP_DELAY_MAX = 999                          # xSR nnn#, in 100 us units


class ProtocolError(ValueError):
    pass


def _axis(axis):
    if axis not in AXES:
        raise ProtocolError(f"axis must be 1, 2 or 3, not {axis!r}")
    return axis


def _pos(p):
    p = int(p)
    if not POS_MIN <= p <= POS_MAX:
        raise ProtocolError(f"position {p} out of range")
    return p


def _d8(p):
    """C#'s {n:D8}: at least 8 digits, with the minus sign in front (-13043 -> "-00013043")."""
    p = _pos(p)
    return ("-" if p < 0 else "") + f"{abs(p):08d}"


def _byte(v, name):
    v = int(v)
    if not 0 <= v <= 255:
        raise ProtocolError(f"{name} must be 0-255")
    return v


# ---------------------------------------------------------------- commands
# Each returns (bytes to send, kind of answer): "value" = text up to '#', "ack" = a lone '#', None = no answer.
def get_position(axis):
    return f"{_axis(axis)}GP#".encode(), "value"


def is_moving(axis):
    return f"{_axis(axis)}GM#".encode(), "value"


def set_target(axis, pos):
    # 8-digit zero padding, as Moonlite's ASCOM driver 6.2 sends it: f"{n}SN {target:D8}#"
    return f"{_axis(axis)}SN {_d8(pos)}#".encode(), "ack"


def start_move(axis):
    return f"{_axis(axis)}SM#".encode(), "ack"


def stop(axis):
    return f"{_axis(axis)}SQ#".encode(), "ack"


def sync(axis, pos):
    return f"{_axis(axis)}SP {_d8(pos)}#".encode(), "ack"   # D8, like set_target


def get_step_delay(axis):
    """xGR# reads the step rate, as Moonlite's NC Remote 1.1.0.14 does. "xSR#" (from INDI) gets no answer from
    firmware 1.6. Not yet tried on the hardware."""
    return f"{_axis(axis)}GR#".encode(), "value"


def set_step_delay(axis, delay):
    d = int(delay)
    if not 1 <= d <= STEP_DELAY_MAX:
        raise ProtocolError(f"step delay must be 1-{STEP_DELAY_MAX}")
    return f"{_axis(axis)}SR {d}#".encode(), "ack"   # unpadded, as NC Remote sends it


def get_temperature():
    return b"GT#", "value"


def get_voltage():
    return b"GV#", "value"


def get_limits():
    return b"GS#", "value"


def get_firmware():
    return b"PV#", "value"


def get_model():
    return b"PF#", "value"


def find_home(mask):
    m = int(mask)
    if not 1 <= m <= 7:
        raise ProtocolError("home mask must be 1-7")
    return f"SH {m:02d}#".encode(), "ack"


def set_encoders(on):
    return f"PE {'1' if on else '0'}#".encode(), "value"   # exactly as Moonlite's NC Remote sends it


def set_brightness(v):
    return f"PD {_byte(v, 'brightness')}#".encode(), "ack"   # unpadded, as NC Remote


def set_sleep_brightness(v):
    return f"PL {_byte(v, 'sleep brightness')}#".encode(), "ack"   # unpadded, as NC Remote


DIM_DELAY_MAX = 65535   # PW n#: seconds, 0 = never dim


def get_dim_delay():
    """Seconds before the backlight dims ("00030", 0 = never). Only the patched firmware (WR35 menu patch) knows
    PW; stock firmware 1.6 never answers it."""
    return b"PW#", "value"


def set_dim_delay(s):
    s = int(s)
    if not 0 <= s <= DIM_DELAY_MAX:
        raise ProtocolError(f"dim delay must be 0-{DIM_DELAY_MAX} seconds")
    return f"PW {s}#".encode(), "ack"   # unpadded, like PD


def set_temp_offset(c):
    """Temperature offset in C (tenths are sent). The firmware gives no answer."""
    t = int(round(float(c) * 10))
    if not -99 <= t <= 999:
        raise ProtocolError("temperature offset out of range")
    return f"Pt {t}#".encode(), None   # tenths, unpadded: NC Remote sends (value * 10).ToString("F0")


# ---------------------------------------------------------------- answers
_INT = re.compile(r"^\s*[+-]?\d+\s*$")


def parse_int(text):
    if not _INT.match(text or ""):
        raise ProtocolError(f"expected a number, got {text!r}")
    return int(text)


def parse_moving(text):
    """GM# answers "01" while moving, "00" when stopped (printed with %02x)."""
    t = (text or "").strip()
    try:
        return int(t, 16) != 0
    except ValueError:
        raise ProtocolError(f"expected 00/01, got {text!r}")


def parse_tenths(text):
    """GT# and GV# answer tenths (degrees C, volts)."""
    return parse_int(text) / 10.0


@dataclass
class Limits:
    rot_home: bool = False
    out_limit: bool = False
    in_limit: bool = False

    @classmethod
    def parse(cls, text):
        v = parse_int(text)
        return cls(bool(v & LIMIT_ROT_HOME), bool(v & LIMIT_OUT), bool(v & LIMIT_IN))


@dataclass
class AxisState:
    position: int = 0
    moving: bool = False
    step_delay: int = None   # unknown until set: firmware 1.6 doesn't answer xSR#


@dataclass
class Status:
    """One complete poll of the NiteCrawler."""
    focus: AxisState = field(default_factory=AxisState)
    rotator: AxisState = field(default_factory=AxisState)
    aux: AxisState = field(default_factory=AxisState)
    temperature: float = None
    voltage: float = None
    limits: Limits = field(default_factory=Limits)
    homing: bool = False

    def axis(self, a):
        return {FOCUS: self.focus, ROTATOR: self.rotator, AUX: self.aux}[a]


def split_answers(buf):
    """Splits received bytes into complete '#'-terminated answers. Returns (answers, rest)."""
    parts = buf.split(b"#")
    rest = parts.pop()
    return [p.decode("ascii", "replace").strip("\r\n\x00 ") for p in parts], rest
