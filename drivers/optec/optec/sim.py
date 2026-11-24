"""Serial-level simulators of the ThirdLynx/FocusLynx and the Gemini/Hercules, for --simulate and the tests.

They answer with the reply formats printed by Optec's own firmware (ThirdLynx 3.0.4, Gemini 2.0.1), ignore bytes
that aren't one of their frames (such as another driver's probe), and log every command they act on.
"""
import re
import threading
import time

_LYNX = re.compile(rb"<(F1|F2|FH)([A-Z]+)([^>]*)>")
_GEM = re.compile(rb"<([FRH])1(\d\d)([A-Z]{6})([^>]*)>")


class _Axis:
    def __init__(self, pos=0, speed=2000.0, max_pos=50000, homed=True):
        self.pos, self.target = float(pos), int(pos)
        self.speed = speed
        self.max_pos = max_pos
        self.homing = False
        self.homed = homed

    def step(self, dt):
        if self.homing:
            self.target = 0
        d = self.target - self.pos
        if abs(d) <= self.speed * dt:
            self.pos = float(self.target)
            if self.homing:
                self.homing, self.homed = False, True
        else:
            self.pos += self.speed * dt * (1 if d > 0 else -1)

    @property
    def moving(self):
        return int(round(self.pos)) != self.target or self.homing


class _SimBase:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._rx = b""
        self._tx = b""
        self._last = clock()
        self.log = []                      # verbs acted on, e.g. ("F1", "MA", "012345")

    def _tick(self):
        now = self.clock()
        dt, self._last = now - self._last, now
        for ax in self.axes():
            ax.step(dt)

    def _say(self, *lines):
        self._tx += b"".join(ln.encode() + b"\n" for ln in lines)

    def feed(self, data):
        with self._lock:
            self._rx += bytes(data)
            self._tick()
            self._parse()

    def take(self, timeout):
        with self._lock:
            self._tick()
            if b"\n" in self._tx:
                line, self._tx = self._tx.split(b"\n", 1)
                return line + b"\n"
        self.sleep(min(timeout, 0.01))
        return b""

    def opener(self, device=None, baud=None):
        return _SimPort(self)


class _SimPort:
    def __init__(self, sim):
        self.sim = sim
        self.timeout = 0.05
        self.is_open = True
        self.dtr = self.rts = False

    def _check(self):
        if not self.is_open:
            raise OSError("port closed")

    def readline(self):
        self._check()
        return self.sim.take(self.timeout)

    def read(self, n=1):
        self._check()
        return self.sim.take(self.timeout)

    @property
    def in_waiting(self):
        return len(self.sim._tx)

    def write(self, data):
        self._check()
        self.sim.feed(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self.sim._lock:
            self.sim._tx = b""

    def close(self):
        self.is_open = False


class SimLynx(_SimBase):
    """A ThirdLynx (model="ThirdLynx") or an older FocusLynx (model="FocusLynx", which refuses GETVER), with one
    or two focusers. dev_types: the configured device type per port ("ZZ" = none)."""

    VERBS = ["GETHUBINFO", "GETSTATUS", "GETCONFIG", "GETVER", "REVERSE", "SETMAX", "HELLO", "HALT", "HOME",
             "SCCP", "SCDT", "MA"]

    def __init__(self, model="ThirdLynx", channels=2, dev_types=("FA", "ZZ"), speed=2000.0, temperature=11.6,
                 board_id="E6614C311B5C7A2F", **kw):
        super().__init__(**kw)
        self.model = model
        self.focusers = {f"F{i + 1}": _Axis(pos=25000 if i == 0 else 0, speed=speed) for i in range(channels)}
        self.dev_types = {f"F{i + 1}": dev_types[i] if i < len(dev_types) else "ZZ" for i in range(channels)}
        self.temperature = temperature
        self.board_id = board_id
        self.reverse = {k: 0 for k in self.focusers}

    def axes(self):
        return self.focusers.values()

    def _err(self, code, text):
        self._say(f"ER={code} {text}", "END")

    def _parse(self):
        while True:
            m = _LYNX.search(self._rx)
            if not m:
                self._rx = self._rx[-64:]      # junk that isn't a frame (another driver's probe) is dropped
                return
            self._rx = self._rx[m.end():]
            target, body = m.group(1).decode(), (m.group(2) + m.group(3)).decode()
            verb = next((v for v in self.VERBS if body.startswith(v)), None)
            if verb is None:
                self._err(3, "Unknown Command Received")
                continue
            arg = body[len(verb):]
            self.log.append((target, verb, arg))
            self._do(target, verb, arg)

    def _do(self, t, verb, arg):
        if t == "FH":
            if verb == "GETHUBINFO":
                self._say("!", "HUB INFO", "Hub FVer = 3.0.4", "Sleeping = 0", "Wired IP = 0.0.0.0",
                          "DHCPisOn = 1", "WF Atchd = 0", "END")
            elif verb == "GETVER" and self.model == "ThirdLynx":
                self._say("!", "VERSIONS", "ThirdLynx", f"Board ID = {self.board_id}", "Main FWV = 3.0.4",
                          "OneLynx  = 1.2.0", "END")
            else:
                self._err(3, "Unknown Command Received")
            return
        ax = self.focusers.get(t)
        if ax is None:
            self._err(1, "The command received was for an invalid target device")
            return
        n = t[1]
        configured = self.dev_types[t] != "ZZ"
        if verb == "GETSTATUS":
            temp = "NP" if self.temperature is None else f"{self.temperature:.1f}"
            self._say("!", f"STATUS{n}", f"Temp(C)  = {temp}", f"Curr Pos = {int(round(ax.pos)):06d}",
                      f"Targ Pos = {ax.target:06d}", f"IsMoving = {int(ax.moving)}", f"IsHoming = {int(ax.homing)}",
                      f"IsHomed  = {int(ax.homed)}", "FFDetect = 0", f"TmpProbe = {int(self.temperature is not None)}",
                      "RemoteIO = 0", "Hnd Ctlr = 0", f"Reverse  = {self.reverse[t]}", "END")
        elif verb == "GETCONFIG":
            self._say("!", f"CONFIG{n}", f"Nickname = Focuser {n}", f"Max Pos  = {ax.max_pos:06d}",
                      f"Dev Typ  = {self.dev_types[t]}", "TComp ON = 0", "TempCo A = +0086", "TC Mode  = A",
                      "BLC En   = 0", "BLC Stps = 40", "LED Brt  = 75", "TC@Start = 0", "END")
        elif verb == "HELLO":
            self._say("!", f"Focuser {n}")
        elif verb == "MA":
            if not configured:
                self._err(6, "Device Type Is Set Incorrectly")
            elif ax.moving:
                self._err(11, "Cannot Execute Command While Device Is Moving")
            else:
                pos = int(arg)
                if pos > ax.max_pos:
                    self._err(14, "Unacceptable Parameter Received In Command")
                else:
                    ax.target = pos
                    self._say("!", "M")
        elif verb == "HALT":
            ax.target = int(round(ax.pos))
            ax.homing = False
            self._say("!", "HALTED")
        elif verb == "HOME":
            ax.homing = True
            self._say("!", "H")
        elif verb == "SCCP":
            ax.pos = float(int(arg))
            ax.target = int(arg)
            self._say("!", "SET")
        elif verb == "REVERSE":
            self.reverse[t] = int(arg or 0)
            self._say("!", "SET")
        else:                                     # SCDT / SETMAX: configuration, never sent by the hub
            self._say("!", "SET")


class SimGemini(_SimBase):
    """A Gemini or Hercules: focuser F1, rotator R1, hub H1. Rotator speed in degrees per second."""

    def __init__(self, focus_speed=2000.0, rot_speed=30.0, rot_homed=True, serial="1234", temperature=8.4,
                 firmware="2.0.1", **kw):
        super().__init__(**kw)
        self.focus = _Axis(pos=12000, speed=focus_speed, max_pos=26000)
        self.rot = _Axis(pos=45000, speed=rot_speed * 1000.0, max_pos=359999, homed=rot_homed)  # milli-degrees
        self.temperature = temperature
        self.serial = serial
        self.reverse = 0
        self.firmware = firmware
        self._late = []      # lines sent only when the next command comes in (a reply's late tail)

    def axes(self):
        return (self.focus, self.rot)

    def _error(self, n, text):
        self._say(f"ERROR ID = {n}", f"ERROR TEXT = {text}", "END")

    def _parse(self):
        while True:
            m = _GEM.search(self._rx)
            if not m:
                self._rx = self._rx[-64:]
                return
            self._rx = self._rx[m.end():]
            dev, tid, verb, payload = (g.decode() for g in m.groups())
            self.log.append((dev, verb, payload))
            self._do(dev, tid, verb, payload)

    def _do(self, dev, tid, verb, p):
        ack = f"!{tid}"
        if self._late:
            self._say(*self._late)
            self._late = []
        if dev == "H":
            if verb == "GETCFG":
                self._say(ack, f"Firmware = {self.firmware}", "LEDBrite = 50", "HandCtrl = 0", "Wired IP = 0.0.0.0", "END")
            elif verb == "GETSER":
                self._say(ack, f"Serial Number = {self.serial}", "END")
            else:
                self._error(3, "Unknown Command Received")
            return
        if dev == "F":
            ax = self.focus
            if verb == "GETSTA":
                temp = "No Probe" if self.temperature is None else f"{self.temperature:.1f}"
                self._say(ack, f"CurrTemp = {temp}", f"CurrStep = {int(round(ax.pos)):06d}",
                          f"TargStep = {ax.target:06d}", f"IsMoving = {int(ax.moving)}", f"IsHoming = {int(ax.homing)}",
                          f"Is Homed = {int(ax.homed)}", f"TempProb = {int(self.temperature is not None)}",
                          "RemoteIO = 0", "HCStatus = 0", "Home Swi = 0", "END")
            elif verb == "GETCFG":
                self._say(ack, "Nickname = Gemini Focuser", f"MaxSteps = {ax.max_pos}", "Dev Type = A",
                          "TComp On = 0", "TCMode A = 86", "CurrenTC = A", "BLCompOn = 0", "BLCSteps = 40",
                          "TC Start = 0", "HOnStart = 0", "END")
            elif verb == "MOVABS":
                pos = int(p)
                if ax.moving:
                    self._error(11, "Cannot Execute Command While Device Is Moving")
                elif pos > ax.max_pos:
                    self._error(14, "Unacceptable Parameter Received In Command")
                else:
                    ax.target = pos
                    self._say(ack)
            else:
                self._common(ax, ack, verb)
            return
        ax = self.rot
        if verb == "GETSTA":
            pa = int(round(ax.pos)) % 360000
            self._say(ack, f"CurrStep = {pa // 10:06d}", f"TargStep = {ax.target // 10:06d}",
                      f"LastStep = {pa // 10:06d}", f"CurentPA = {pa:06d}", f"TargetPA = {ax.target % 360000:06d}",
                      f"IsMoving = {int(ax.moving)}", f"IsHoming = {int(ax.homing)}", f"Is Homed = {int(ax.homed)}",
                      "END")
        elif verb == "GETCFG":
            self._say(ack, "Nickname = Gemini Rotator", "MaxSteps = 36000", "Dev Type = A", "BLCompOn = 0",
                      "BLCSteps = 0", "HOnStart = 0", f"iReverse = {self.reverse}", "MaxSpeed = 800", "END")
        elif verb == "MOVEPA":
            if not ax.homed:
                self._error(10, "Cannot Execute Command - Device Is Not Homed")
            elif ax.moving:
                self._error(11, "Cannot Execute Command While Device Is Moving")
            else:
                ax.target = int(p) % 360000
                self._say(ack)
        elif verb == "SETREV":
            self.reverse = int(p or 0)
            self._say(ack)
            if self.firmware.startswith("1."):
                self._late.append("END")   # a real Hercules on 1.3.1 (2026-11-23): "END" a moment after the "!"
        else:
            self._common(ax, ack, verb)

    def _common(self, ax, ack, verb):
        if verb == "DOHALT":
            ax.target = int(round(ax.pos))
            ax.homing = False
            self._say(ack)
        elif verb == "DOHOME":
            ax.homing = True
            self._say(ack)
        else:
            self._error(3, "Unknown Command Received")
