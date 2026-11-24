"""Serial-level simulators the hub finds exactly as it finds real hardware (for --simulate and the tests).

The NiteCrawler and the WandererBox already have serial simulators in their packages; the WandererCover V4-EC gets
one here, modelled on the captures from the real panel (fw 20250703, 2026-11-19, wanderer/cover-capture):
- a status line every 0.33 s: WandererCoverV4A<fw>A<close°>A<open°>A<position°>A<V>A<brightness>A<heater>A<asiair>A
- open (1001) / close (1000): silence for the move, then "done", then status lines again; a repeat of the last move
  doesn't move the cap (checked on the panel 2026-11-21)
- brightness 1–255 shows in the next line; 9999 is off; the light goes off by itself while the cover is open
- heater 2000 / 2050 / 2100 / 2150
- `block_at`: an angle the cap can't pass, like the real cap caught on something (2026-11-23): the move stops
  there and still ends with "done"
"""
import threading
import time


class SimV4ec:
    def __init__(self, move_s=3.0, line_s=0.33, clock=time.monotonic, sleep=time.sleep):
        self.close_set, self.open_set = 15.0, 295.0
        self.position = 15.0
        self.brightness = 0
        self.heater = 50
        self.move_s, self.line_s = move_s, line_s
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._move = None            # (target position, end time)
        self._last_move = 1000       # it starts closed
        self.block_at = None         # an angle the cap can't move past (None: nothing in the way)
        self._rx = b""
        self._reply = b""            # answers to commands, sent before the next status line
        self._next = None
        self.log = []                # every number received, for tests

    def line(self):
        return (f"WandererCoverV4A20250703A{self.close_set:.2f}A{self.open_set:.2f}A{self.position:.2f}A"
                f"{12.04 if self.heater else 12.55:.2f}A{self.brightness}A{self.heater}A0A\r\n").encode()

    def _command(self, n):
        self.log.append(n)
        if n in (1000, 1001):
            target = self.close_set if n == 1000 else self.open_set
            if n == self._last_move:              # the real panel ignores a repeat: "done" after ~1.3 s, no motion
                target = self.position
            b = self.block_at
            if b is not None and min(self.position, target) < b < max(self.position, target):
                target = b                        # caught on something: stops there, says "done" all the same
            self._last_move = n
            self._move = (target, self.clock() + self.move_s)
            if n == 1001:
                self.brightness = 0
        elif 1 <= n <= 255:
            if abs(self.position - self.close_set) < 3:
                self.brightness = n
        elif n == 9999:
            self.brightness = 0
        elif n in (2000, 2050, 2100, 2150):
            self.heater = n - 2000
        elif 10000 <= n < 40000:              # closed angle; the real panel answers ClosePositionSet15.00 (no A)
            self.close_set = (n - 10000) / 100  # and takes any angle, e.g. 39000 = 290° (2026-11-21)
            self._reply += f"ClosePositionSet{self.close_set:.2f}\r\n".encode()
        elif 40000 <= n < 80000:              # open angle
            self.open_set = (n - 40000) / 100
            self._reply += f"OpenPositionSet{self.open_set:.2f}\r\n".encode()

    def _take_commands(self):
        while b"\n" in self._rx:
            raw, self._rx = self._rx.split(b"\n", 1)
            try:
                self._command(int(raw.strip() or b"0"))
            except ValueError:
                pass

    def readline(self, timeout=0.2):
        with self._lock:
            self._take_commands()
            now = self.clock()
            if self._reply and self._move is None:
                r, self._reply = self._reply.split(b"\n", 1)
                return r + b"\n"
            if self._move is not None:
                target, end = self._move
                if now < end:
                    pass
                else:
                    self.position = target
                    self._move = None
                    self._next = now + self.line_s
                    return b"done\r\n"
            else:
                if self._next is None or now >= self._next:
                    self._next = now + self.line_s
                    return self.line()
        self.sleep(min(timeout, 0.05))
        return b""

    def opener(self, device=None, baud=None):
        return _SimV4ecPort(self)


class _SimV4ecPort:
    def __init__(self, panel):
        self.panel = panel
        self.timeout = 0.2
        self.is_open = True

    def readline(self):
        if not self.is_open:
            raise OSError("port closed")
        return self.panel.readline(self.timeout)

    read = lambda self, n=1: self.readline()   # noqa: E731  (the hub listens with read())

    def write(self, data):
        if not self.is_open:
            raise OSError("port closed")
        with self.panel._lock:
            self.panel._rx += bytes(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False
