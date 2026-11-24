"""A simulated WandererBox Plus V3 running stock firmware, behind a pyserial-like object.

Behaves like the disassembled firmware: one status line per loop, at most one parseInt() number per loop,
every command counted as an EEPROM write, and 66300744 doing different things on 20240216 and 20250411.
Adds a simple thermal model so the dew controller has something to regulate.
"""
import math
import re
import threading
import time

from . import protocol as P

_INT = re.compile(rb"-?\d+")


class SimBox:
    def __init__(self, fw="20250411", loop_s=1.0, ambient=8.0, humidity=85.0, lens_missing=False, dht_missing=False,
                 clock=time.monotonic, sleep=time.sleep):
        self.fw = fw
        self.loop_s = loop_s
        self.ambient = ambient
        self.humidity = humidity
        self.lens_missing = lens_missing
        self.dht_missing = dht_missing
        self.lens = ambient - 2.0        # radiative cooling pulls an unheated lens below ambient
        self.voltage = 12.3
        self.usb, self.dc2, self.dc46 = True, False, True
        self.pwm = 0
        self.dc2v = 120
        self.eeprom_writes = 0
        self.commands = []
        self.dropped = 0                 # loops whose parseInt hit a non-digit first (command lost)
        self.garble_writes = 0           # tests: put a stray "\n" in front of this many writes (loses them)
        self._rx = b""
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._next = None
        self._last_t = None
        # WSM firmware (PROTOCOL.md 2.3, 5): config keys, ack/rej, X1 only in replies or while streaming.
        self.cfg = {P.KEY_DC2_SLEW: P.DC2_SLEW_DEFAULT_MV, P.KEY_DC2_MAX: 132}
        self.cfg_range = {P.KEY_DC2_SLEW: P.DC2_SLEW_MV, P.KEY_DC2_MAX: P.DC2_MAX_DV}
        self.flt = 0          # tests set fault bits; 40002 clears them
        self.shed = 0         # tests set outputs held off by protection
        self.rst = 1          # MCUSR at the last start (1 = power-on)
        self.up = 0.0         # seconds since the last start
        self.ack = self.rej = self.why = 0
        self.cfg_read = 0
        self.x1_once = self.x1_stream = False
        self._replies = []    # WSM: status lines owed for commands it already acted on (sent before the next loop)

    # ---------------------------------------------------------------- firmware behaviour
    def _execute(self, n):
        self.commands.append(n)
        self.eeprom_writes += 1
        if n in (100, 101):
            self.dc46 = n == 101
        elif n in (110, 111):
            self.usb = n == 111
        elif n in (120, 121):
            self.dc2 = n == 121
        elif 3000 <= n <= 3255:
            self.pwm = n - 3000
        elif 20001 <= n <= 20132 and P.is_open_firmware(self.fw) and n - 20000 > self.cfg[P.KEY_DC2_MAX]:
            self.eeprom_writes -= 1
            self.x1_once, self.rej, self.why = True, n, 2   # WSM: above the ceiling, refused (never capped)
        elif 20001 <= n <= 20132:
            self.dc2v = n - 20000
        elif P.is_open_firmware(self.fw) and n >= 40000 and n != P.CAL_COMMAND:
            self.eeprom_writes -= 1   # WSM saves config 5 s later, not per command
            self._wsm(n)
        elif n == P.CAL_COMMAND:
            if self.fw == "20240216":
                self.usb = self.dc2 = self.dc46 = True
                self.pwm = 255
            else:
                self.usb = self.dc2 = self.dc46 = False
                self.pwm = 0
        else:
            self.eeprom_writes -= 1  # unknown numbers are ignored

    def _wsm(self, n):
        self.x1_once = True
        if n in (40000, 40003):
            self.ack = n
        elif n == 40002:
            self.flt, self.ack = 0, n
        elif n in (40004, 40005):
            self.x1_stream, self.ack = n == 40004, n
        elif 60000 <= n <= 60099:
            if n - 60000 in self.cfg:
                self.cfg_read, self.ack = n - 60000, n
            else:
                self.rej, self.why = n, 4
        elif 700000000 <= n <= 799999999:
            key, neg, val = (n // 1000000) % 100, (n // 100000) % 10, n % 100000
            val = -val if neg else val
            lo, hi = self.cfg_range.get(key, (None, None))
            if lo is None:
                self.rej, self.why = n, 4
            elif not lo <= val <= hi:
                self.rej, self.why = n, 2
            else:
                self.cfg[key], self.ack = val, n
                self.eeprom_writes += 1
                if key == P.KEY_DC2_MAX and self.dc2v > val:
                    self.dc2v = val   # the firmware pulls the setpoint down to a lower ceiling
        else:
            self.rej, self.why = n, 1

    def restart(self, mcusr):
        """Tests: the box starts again (streaming off, uptime from 0)."""
        self.rst, self.up, self.x1_stream, self.x1_once = mcusr, 0.0, False, False

    def _physics(self, dt):
        self.up += dt
        heat = 6.0 * self.pwm / 255.0            # C/min of heating at full power
        loss = (self.lens - (self.ambient - 2.0)) / 4.0   # relaxes toward ambient-2 with a 4 min time constant
        self.lens += (heat - loss) * dt / 60.0

    def _current(self):
        i = 0.15 + (0.4 if self.usb else 0) + (1.2 if self.dc46 else 0) + 2.0 * self.pwm / 255.0
        if self.dc2:
            i += (self.dc2v / 10.0) / 8.0 * (self.dc2v / 10.0) / max(self.voltage, 1)
        return i

    def status_line(self):
        lens = -127.0 if self.lens_missing else self.lens
        if self.dht_missing:
            hum = amb = "nan"
        else:
            hum, amb = f"{self.humidity:.2f}", f"{self.ambient:.2f}"
        return (f"{P.HEADER}A{self.fw}A{lens:.2f}A{hum}A{amb}A{self._current():.2f}A{self.voltage:.2f}A"
                f"{int(self.usb)}A{int(self.dc2)}A{self.pwm}A{int(self.dc46)}A{self.dc2v}A{self._x1()}\r\n").encode()

    def _x1(self):
        if not P.is_open_firmware(self.fw) or not (self.x1_once or self.x1_stream):
            return ""
        self.x1_once = False
        req = (1 if self.usb else 0) | (2 if self.dc2 else 0) | (4 if self.dc46 else 0) | (8 if self.pwm else 0)
        x = (f"X1Aact={req & ~self.shed}Ashed={self.shed}Aflt={self.flt}Aup={int(self.up)}Arst={self.rst}A"
             f"ack={self.ack}Arej={self.rej}Awhy={self.why}A")
        if self.cfg_read:
            x += f"cfg={self.cfg_read},{self.cfg[self.cfg_read]}A"
            self.cfg_read = 0
        return x

    def loop_once(self, dt=None):
        """One firmware loop: physics, one parseInt(), status line."""
        if dt is None:
            dt = self.loop_s
        self._physics(dt)
        with self._lock:
            # parseInt(SKIP_NONE): the number has to start at the first byte. Anything else in front makes
            # this loop read nothing useful; the real box then loses the command, modelled here by dropping
            # what's buffered (on the real box, newline-terminated commands lost every second one).
            n = None
            if self._rx:
                m = _INT.match(self._rx)
                if m:
                    self._rx = self._rx[m.end():]
                    n = int(m.group())
                else:
                    self.dropped += 1
                    self._rx = b""
        if n is not None:
            self._execute(n)
        return self.status_line()

    # ---------------------------------------------------------------- serial side
    def opener(self, port=None):
        return _SimSerial(self)


class _SimSerial:
    """Enough of serial.Serial for BoxLink."""

    def __init__(self, box):
        self.box = box
        self.timeout = 0.2
        self.is_open = True

    def readline(self):
        b = self.box
        with b._lock:
            if b._replies:
                return b._replies.pop(0)
        now = b._clock()
        if b._next is None:
            b._next = now + b.loop_s
        wait = b._next - now
        if wait > self.timeout:
            b._sleep(self.timeout)
            return b""
        if wait > 0:
            b._sleep(wait)
        b._next += b.loop_s
        if b._next < b._clock():       # fell behind (debugger, slow test): don't burst
            b._next = b._clock() + b.loop_s
        return b.loop_once()

    def write(self, data):
        if not self.is_open:
            raise OSError("port closed")
        b = self.box
        with b._lock:
            if b.garble_writes > 0:
                b.garble_writes -= 1
                data = b"\n" + data
            b._rx += data
            if not P.is_open_firmware(b.fw):
                return len(data)
            # WSM: a number ended by a non-digit is acted on at once, with a status line straight after it.
            done = []
            while True:
                m = re.match(rb"\D*(\d+)(?=\D)", b._rx)
                if not m:
                    break
                b._rx = b._rx[m.end():]
                done.append(int(m.group(1)))
            if not re.search(rb"\d", b._rx):
                b._rx = b""
        for n in done:
            b._execute(n)
            with b._lock:
                b._replies.append(b.status_line())
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False
