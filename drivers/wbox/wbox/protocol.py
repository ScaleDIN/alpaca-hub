"""Wanderer's stock WandererBox Plus V3 serial protocol (firmware 20240216 and 20250411).

See WandererBoxPlusV3-hardware-map.md for where this comes from. In short:

* 19200 baud. The box reads one integer per loop with Serial.parseInt(SKIP_NONE): a number must be the very
  first byte in the receive buffer, and anything else in front of it (a newline left over from an earlier
  command, say) makes that loop read 0 and the command is lost. So commands are sent as bare digits with no
  terminator: parseInt ends the number by its 1 s timeout. Measured on a real box (2026-11-17): with "\\n"
  every second command was dropped, and without it every command took. One loop takes about a second
  (blocking DS18B20 conversion), so the box takes at most one command per status line.
* Once per loop it prints a status line with fields separated by "A":
  ZXWBPlusV3 A fw A probeC A hum% A ambC A currentA A volts A usb A dc2 A dc3pwm A dc46 A dc2V*10 A \\r\\n
* Every command is written to EEPROM unconditionally, so the host must not send needless commands.
"""
import math
from dataclasses import dataclass, field

BAUD = 19200
HEADER = "ZXWBPlusV3"

DC2_MIN_V = 5.0       # lower bound we offer (the firmware accepts 0.1 V up, but nothing useful runs below 5 V)
DC2_MAX_V = 13.2      # firmware ceiling (PWM 255)
PWM_MAX = 255

# Firmware versions whose behaviour has been checked against the disassembly.
KNOWN_FIRMWARE = ("20240216", "20250411")
# 66300744 is a real zero-current calibration only from this version on. On 20240216 the same number
# switches every output ON with the dew heater at full power, so it must never be sent there.
CALIBRATION_SAFE_FIRMWARE = ("20250411",)
# The open replacement firmware for the box: "WSM" + version digits, e.g. WSM020. It speaks the
# 20250411 protocol (66300744 included: all outputs off, zero measured). Its regular status line is stock-length
# (WandererEmpire won't connect otherwise); the X1 extension fields after field 12 come only in the reply to an
# extension command (40000 and up) or while streaming is on (40004). parse_status keeps them in Status.ext
# (that firmware's PROTOCOL.md, 2.3 and 5).
OPEN_FIRMWARE_PREFIX = "WSM"


def is_open_firmware(fw):
    return fw.startswith(OPEN_FIRMWARE_PREFIX) and fw[len(OPEN_FIRMWARE_PREFIX):].isdigit()


def known_firmware(fw):
    return fw in KNOWN_FIRMWARE or is_open_firmware(fw)


def calibration_safe(fw):
    return fw in CALIBRATION_SAFE_FIRMWARE or is_open_firmware(fw)
CAL_COMMAND = 66300744


@dataclass
class Status:
    fw: str = ""
    lens: float | None = None      # DS18B20 probe, C (None = missing)
    humidity: float | None = None  # DHT22, %
    ambient: float | None = None   # DHT22, C
    current: float | None = None   # A
    voltage: float | None = None   # V
    usb: bool = False
    dc2: bool = False
    dew_pwm: int = 0               # DC3, 0..255
    dc46: bool = False
    dc2_volts: float = 0.0         # setpoint, V
    ext: dict = field(default_factory=dict)   # WSM X1 fields (key -> text); empty on a stock-length line
    raw: str = field(default="", repr=False)

    @property
    def ambient_ok(self):
        return self.ambient is not None and self.humidity is not None

    @property
    def dewpoint(self):
        if not self.ambient_ok:
            return None
        return dew_point(self.ambient, self.humidity)

    @property
    def power(self):
        if self.voltage is None or self.current is None:
            return None
        return self.voltage * self.current


def _num(tok):
    """Float from a status token, or None for nan/inf/ovf/garbage."""
    try:
        v = float(tok)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _flag(tok):
    v = _num(tok)
    if v not in (0.0, 1.0):
        raise ValueError(f"bad on/off field {tok!r}")
    return v == 1.0


def parse_status(line):
    """Parses one status line. Returns Status, or None if the line isn't a complete status line
    (partial line after opening the port, noise, or another device)."""
    line = line.strip()
    if not line.startswith(HEADER + "A"):
        return None
    t = line.split("A")
    # header, fw, 10 values, and the empty string after the trailing "A"
    if len(t) < 12:
        return None
    try:
        fw = t[1].strip()
        lens = _num(t[2])
        hum = _num(t[3])
        amb = _num(t[4])
        cur = _num(t[5])
        volts = _num(t[6])
        usb = _flag(t[7])
        dc2 = _flag(t[8])
        pwm = _num(t[9])
        dc46 = _flag(t[10])
        dc2v = _num(t[11])
    except ValueError:
        return None
    if pwm is None or not 0 <= pwm <= PWM_MAX or dc2v is None or not 0 <= dc2v <= 200:
        return None
    # DallasTemperature reports -127 for "no probe" (85 is the power-on value of a probe that hasn't converted).
    if lens is not None and (lens <= -100 or lens == 85.0 or lens > 125):
        lens = None
    # DHT22 gives nan when missing; also reject impossible values.
    if hum is not None and not 0 <= hum <= 100:
        hum = None
    if amb is not None and not -40 <= amb <= 80:
        amb = None
    if hum is None or amb is None:
        hum = amb = None
    if volts is not None and not 0 <= volts <= 20:
        volts = None
    if cur is not None and not 0 <= cur <= 30:
        cur = None
    ext = {}
    if "X1" in t[12:]:
        for f in t[t.index("X1", 12) + 1:]:
            k, eq, v = f.partition("=")
            if eq:
                ext[k] = v
    return Status(fw=fw, lens=lens, humidity=hum, ambient=amb, current=cur, voltage=volts, usb=usb, dc2=dc2,
                  dew_pwm=int(round(pwm)), dc46=dc46, dc2_volts=round(dc2v / 10.0, 1), ext=ext, raw=line)


def dew_point(t, rh):
    """Magnus formula (Sonntag 1990 constants), C."""
    rh = max(rh, 0.1)
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * t / (b + t)
    return b * g / (a - g)


# ---------------------------------------------------------------- commands (integers, sent as bare digits)

def cmd_usb(on):
    return 111 if on else 110


def cmd_dc2(on):
    return 121 if on else 120


def cmd_dc46(on):
    return 101 if on else 100


def cmd_dew_pwm(pwm):
    pwm = int(pwm)
    if not 0 <= pwm <= PWM_MAX:
        raise ValueError("dew PWM out of range")
    return 3000 + pwm


def cmd_dc2_volts(volts):
    dv = int(round(volts * 10))
    if not 1 <= dv <= int(round(DC2_MAX_V * 10)):
        raise ValueError("DC2 voltage out of range")
    return 20000 + dv


# ---------------------------------------------------------------- WSM config keys (PROTOCOL.md 5.4)
# Only for a box whose firmware is_open_firmware(): stock ignores these numbers, but nothing should rely on that.
KEY_DC2_SLEW = 11                 # DC2 ramp rate, mV/s
DC2_SLEW_MV = (100, 20000)        # the firmware's range for key 11 (0.1 to 20 V/s)
DC2_SLEW_DEFAULT_MV = 5000
KEY_DC2_MAX = 10                  # DC2 ceiling, 0.1 V: the box refuses any setpoint above it (why=2)
DC2_MAX_DV = (50, 132)            # the firmware's range for key 10 (5.0 to 13.2 V)

CMD_CLEAR_FAULTS = 40002          # clear latched faults and info bits
CMD_HEARTBEAT = 40003             # harmless; its reply carries the X1 fields
CMD_STREAM_ON = 40004             # X1 fields in every line until 40005 or the next box start (never stored)

# X1 field meanings (PROTOCOL.md 2.3, 2.4)
OUTPUT_BITS = ((1, "USB"), (2, "DC2"), (4, "DC4-6"), (8, "dew heater"))
FAULT_BITS = ((1, "over-current (latched)"), (2, "low voltage: heater off"), (4, "low voltage: all off"),
              (8, "over-voltage: DC2 off (latched)"), (16, "heater over-temperature (latched)"),
              (32, "current sensor implausible"), (64, "Vin too low for the DC2 setpoint"),
              (128, "DC2 off: not enough headroom"), (256, "host lost"), (512, "settings reset to defaults"),
              (1024, "restarted warm"), (2048, "Vin measurement implausible"))
FLT_LATCHED = 1 | 8 | 16
RESET_BITS = ((1, "power-on"), (2, "reset pin"), (4, "brown-out"), (8, "watchdog"))


def _names(mask, table):
    return [n for b, n in table if mask & b]


def output_names(mask):
    return _names(mask, OUTPUT_BITS)


def fault_names(mask):
    return _names(mask, FAULT_BITS)


def reset_cause(mcusr):
    if mcusr is None:
        return "?"
    return " + ".join(_names(mcusr, RESET_BITS)) or "none recorded"


def ext_int(st, key):
    try:
        return int(st.ext[key])
    except (KeyError, ValueError):
        return None


def cmd_cfg_read(key):
    """60000 + key: the reply line carries cfg=key,value."""
    if not 1 <= int(key) <= 99:
        raise ValueError("config key out of range")
    return 60000 + int(key)


def cmd_cfg_write(key, value):
    """7 KK S VVVVV: key, sign (1 = negative), value 0-99999."""
    key, value = int(key), int(value)
    if not 1 <= key <= 99 or not -99999 <= value <= 99999:
        raise ValueError("config key or value out of range")
    return 700000000 + key * 1000000 + (100000 if value < 0 else 0) + abs(value)


def cfg_value(st, key):
    """The value from a cfg=key,value reply in this status line, or None."""
    k, _, v = st.ext.get("cfg", "").partition(",")
    try:
        return int(v) if k and int(k) == key else None
    except ValueError:
        return None


def encode(cmd, terminate=False):
    # Stock: no terminator (see the module docstring); one command per status line keeps numbers from running
    # together. WSM ends a number at any non-digit, so terminate=True adds "\n" and it acts at once (PROTOCOL.md 3).
    return f"{int(cmd)}".encode("ascii") + (b"\n" if terminate else b"")


def pct_to_pwm(pct):
    return int(round(max(0.0, min(100.0, pct)) * PWM_MAX / 100.0))


def pwm_to_pct(pwm):
    return round(pwm * 100.0 / PWM_MAX, 1)
