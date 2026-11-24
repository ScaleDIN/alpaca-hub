"""A simulated NiteCrawler for tests and --simulate: answers the stock command set the way firmware 1.6 does."""
import random
import threading
import time

from . import protocol as P

SPEED = {P.FOCUS: 3000.0, P.ROTATOR: 12000.0, P.AUX: 3000.0}  # steps per second at the default step delay
DEFAULT_DELAY = 10


class _Axis:
    def __init__(self, pos=0):
        self.pos = float(pos)
        self.target = int(pos)
        self.new = int(pos)
        self.moving = False
        self.delay = DEFAULT_DELAY


class SimNiteCrawler:
    def __init__(self, model="3.5 NC", fw="1.6", speed=1.0, clock=time.monotonic, focus_max=120000, serial="10001"):
        self.model, self.fw, self.serial = model, fw, serial
        self.speed = speed
        self.clock = clock
        self.focus_max = focus_max
        self.axes = {P.FOCUS: _Axis(50000), P.ROTATOR: _Axis(0), P.AUX: _Axis(0)}
        self.temp_tenths = 125
        self.volt_tenths = 123
        self.homing_mask = 0
        self.encoders = True
        self.brightness = 150
        self.sleep_brightness = 16
        self.dim_delay = 30      # PW: patched firmware only
        self.knows_pw = True     # False: stock firmware 1.6, which never answers PW
        self.temp_offset = 0
        self.log = []          # every command received, for tests
        self.silent = False    # True: answers nothing (cable pulled, wrong port)
        self.drop_next = 0     # swallow the answers to this many commands (a busy moment)
        self._t = clock()
        self._temp_t = self._t
        self._lock = threading.Lock()
        self._out = bytearray()

    # ---------------------------------------------------------------- physics
    def _advance(self):
        now = self.clock()
        dt = (now - self._t) * self.speed
        self._t = now
        for a, ax in self.axes.items():
            if not ax.moving:
                continue
            v = SPEED[a] * DEFAULT_DELAY / max(ax.delay, 1)
            d = ax.target - ax.pos
            step = v * dt
            if abs(d) <= step:
                ax.pos = float(ax.target)
                ax.moving = False
            else:
                ax.pos += step if d > 0 else -step
            if a == P.FOCUS:
                ax.pos = min(max(ax.pos, 0.0), float(self.focus_max))
                if ax.pos in (0.0, float(self.focus_max)):
                    ax.moving = False
        if self.homing_mask and not any(self.axes[a].moving for a in self._homed_axes()):
            for a in self._homed_axes():
                self.axes[a].pos = 0.0
                self.axes[a].target = self.axes[a].new = 0
            self.homing_mask = 0
            self._out += b"OK#"
        if now - self._temp_t > 5.0:   # a slow random walk, one step per 5 s at most
            self._temp_t = now
            self.temp_tenths += random.choice((0, 0, 1, -1))

    def _homed_axes(self):
        return [a for bit, a in ((P.HOME_FOCUS, P.FOCUS), (P.HOME_ROTATOR, P.ROTATOR), (P.HOME_AUX, P.AUX))
                if self.homing_mask & bit]

    def limits(self):
        f = self.axes[P.FOCUS].pos
        r = self.axes[P.ROTATOR].pos
        return ((P.LIMIT_ROT_HOME if abs(r) < 200 else 0) | (P.LIMIT_OUT if f >= self.focus_max else 0) |
                (P.LIMIT_IN if f <= 0 else 0))

    # ---------------------------------------------------------------- commands
    def handle(self, cmd):
        """One command without its '#'. Returns the bytes the firmware would send."""
        self.log.append(cmd)
        self._advance()
        if len(cmd) >= 3 and cmd[0] in "123":
            ax = self.axes[int(cmd[0])]
            op, arg = cmd[1:3], cmd[3:].strip()
            if op == "GP":
                return f"{int(round(ax.pos)):08d}#".encode()
            if op == "GM":
                return f"{1 if ax.moving else 0:02x}#".encode()
            if op == "SN":
                ax.new = int(arg)
                return b"#"
            if op == "SM":
                ax.target = ax.new
                ax.moving = ax.target != int(round(ax.pos))
                return b"#"
            if op == "SQ":
                ax.moving = False
                ax.target = ax.new = int(round(ax.pos))
                if self.homing_mask:
                    self.homing_mask = 0
                    for a in self.axes.values():
                        a.moving = False
                return b"#"
            if op == "SP":
                ax.pos = float(int(arg))
                ax.target = ax.new = int(arg)
                return b"#"
            if op == "SR":   # firmware 1.6: "xSR#" (no value) gets no answer
                if arg:
                    ax.delay = int(arg)
                    return b"#"
                return b""
            if op == "GR":   # read the step rate (Moonlite's NC Remote)
                return f"{ax.delay:03d}#".encode()
            return b""
        if cmd == "GT":
            return f"{self.temp_tenths}#".encode()
        if cmd == "GV":
            return f"{self.volt_tenths}#".encode()
        if cmd == "GS":
            return f"{self.limits():02d}#".encode()
        if cmd == "PV":
            return f"{self.fw}#".encode()
        if cmd == "PF":
            return f"{self.model}#".encode()
        if cmd == "PS":
            return f"{self.serial}#".encode()
        if cmd == "PU":
            return b"-------- UNREGISTERED --------#"
        if cmd.startswith("SH"):
            self.homing_mask = int(cmd[2:].strip() or "0") & 7
            for a in self._homed_axes():
                ax = self.axes[a]
                ax.target = 0
                ax.moving = int(round(ax.pos)) != 0
            return b"#"
        if cmd.startswith("PE"):
            self.encoders = cmd[2:].strip() == "01"
            return b"#"
        if cmd.startswith("PD"):
            self.brightness = int(cmd[2:])
            return b"#"
        if cmd.startswith("PL"):
            self.sleep_brightness = int(cmd[2:])
            return b"#"
        if cmd.startswith("PW") and self.knows_pw:
            arg = cmd[2:].strip()
            if not arg:
                return f"{self.dim_delay:05d}#".encode()
            if arg.isdigit() and int(arg) <= 65535:
                self.dim_delay = int(arg)
                return b"#"
            return b""   # malformed: no answer
        if cmd.startswith("Pt"):
            self.temp_offset = int(cmd[2:])
            return b""
        return b""

    # ---------------------------------------------------------------- serial port
    def opener(self, port):
        return _SimPort(self)


class _SimPort:
    def __init__(self, nc):
        self.nc = nc
        self._in = b""
        self.is_open = True

    @property
    def in_waiting(self):
        with self.nc._lock:
            self.nc._advance()
            return len(self.nc._out)

    def write(self, data):
        if not self.is_open:
            raise OSError("port closed")
        with self.nc._lock:
            self._in += bytes(data)
            while b"#" in self._in:
                cmd, self._in = self._in.split(b"#", 1)
                ans = self.nc.handle(cmd.decode("ascii", "replace"))
                if self.nc.drop_next > 0:
                    self.nc.drop_next -= 1
                elif not self.nc.silent:
                    self.nc._out += ans
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self.nc._lock:
            self.nc._out.clear()

    def read(self, n=1):
        if not self.is_open:
            raise OSError("port closed")
        with self.nc._lock:
            self.nc._advance()
            if self.nc._out:
                out = bytes(self.nc._out[:n])
                del self.nc._out[:n]
                return out
        time.sleep(0.005)
        return b""

    def close(self):
        self.is_open = False
