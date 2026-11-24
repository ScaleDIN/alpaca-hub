"""A simulated thermometer, for --simulate thermo and the tests. Its port behaves like the real one: a line once a
second, readline() returns b"" after its timeout when there's nothing, writes are taken and ignored (but counted, so a
test can check nothing was sent)."""
import math
import threading
import time


class SimThermo:
    def __init__(self, serial="T1", period_s=1.0, clock=time.monotonic, sleep=time.sleep):
        self.serial, self.period_s = serial, period_s
        self.clock, self.sleep = clock, sleep
        self.written = bytearray()
        self.silent = False           # True: says nothing (unplugged, a different device)
        self._t0 = clock()
        self._next = None
        self._lock = threading.Lock()

    def temperature(self):
        return round(15.0 + 2.0 * math.sin((self.clock() - self._t0) / 600.0), 2)

    def opener(self, device=None, baud=None):
        return _SimPort(self)


class _SimPort:
    def __init__(self, sim):
        self.sim = sim
        self.timeout = 0.2
        self.is_open = True

    def readline(self):
        if not self.is_open:
            raise OSError("port closed")
        s = self.sim
        with s._lock:
            now = s.clock()
            if s._next is None:
                s._next = now + s.period_s
            wait = s._next - now
            if wait <= 0 and not s.silent:
                s._next = max(s._next + s.period_s, now)
                return f"THERMO {s.serial} {s.temperature():.2f}\r\n".encode()
        s.sleep(min(max(wait, 0.01), self.timeout))
        return b""

    def write(self, data):
        if not self.is_open:
            raise OSError("port closed")
        self.sim.written += bytes(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False
