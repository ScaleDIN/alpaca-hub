"""The serial link: one thread reads the thermometer's lines and keeps the latest.

The port comes from the hub (HubContext.opener_for): a RelaySerial, which reads like pyserial with a short timeout.
The link never opens a port itself. If the port fails (the device was unplugged, a USB reset) a read raises; the link
closes it and calls the opener again, which reattaches to the hub's port (the hub reopens the real one if needed)."""
import logging
import threading
import time

from .protocol import parse_line

log = logging.getLogger("thermo.link")

STALE_S = 5.0        # no line for this long: the thermometer counts as not answering


class ThermoLink:
    def __init__(self, port_name, opener, clock=time.monotonic):
        self.port_name = port_name
        self._opener = opener
        self._clock = clock
        self._ser = None
        self._stop = threading.Event()
        self._thread = None
        self.temperature = None     # °C, as the device reports it
        self.serial = None
        self.last_t = None          # clock() of the last good line
        self.last_error = ""
        self.lines = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="thermo-link", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close()

    def up(self):
        return self.last_t is not None and self._clock() - self.last_t < STALE_S

    def age(self):
        return None if self.last_t is None else self._clock() - self.last_t

    def _close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _run(self):
        while not self._stop.is_set():
            if self._ser is None:
                try:
                    self._ser = self._opener(self.port_name)
                    self.last_error = ""
                except Exception as e:
                    self.last_error = f"cannot open {self.port_name}: {e}"
                    self._stop.wait(2.0)
                    continue
            try:
                line = self._ser.readline()
            except Exception as e:
                self.last_error = f"read failed: {e}"
                log.warning("%s: %s", self.port_name, self.last_error)
                self._close()
                self._stop.wait(1.0)
                continue
            got = parse_line(line) if line else None
            if got:
                self.serial, self.temperature = got
                self.last_t = self._clock()
                self.lines += 1
