"""Line-based request/answer over an open serial port, and the polling unit both Optec hubs build on.

Optec hubs never talk unasked: every exchange is one framed command, then reply lines ending in "\\n".
"""
import logging
import threading
import time

log = logging.getLogger("optec")

BAUD = 115200            # both FocusLynx/ThirdLynx and Gemini/Hercules, 8N1


class OptecError(Exception):
    """The hub answered with an error (ER=… or ERROR ID = …)."""

    def __init__(self, code, text):
        super().__init__(f"{text} (error {code})" if code is not None else text)
        self.code, self.text = code, text


class NoAnswer(OSError):
    pass


class NotReady(Exception):
    """The command can't run in the device's present state (not homed, no focuser type set)."""


def raise_lines(ser):
    """Turn DTR and RTS on for an already open port, as Optec's .NET drivers, Empire and INDI all open ports.
    USB CDC firmware (the ThirdLynx) may not answer until DTR is on. Works on pyserial and on the hub's
    PolledSerial wrapper; simulators without the attributes are left alone."""
    raw = getattr(ser, "_ser", ser)
    for name in ("dtr", "rts"):
        try:
            if hasattr(raw, name):
                setattr(raw, name, True)
        except Exception as e:      # a port that can't do it still works for everything else
            log.debug("could not set %s: %s", name, e)


class Wire:
    """Writes commands and reads whole lines, with a deadline. Reads only through ser.readline(), which on the
    hub's PolledSerial returns what has arrived within its short timeout, so no read waits inside the driver."""

    def __init__(self, ser, clock=time.monotonic):
        self.ser = ser
        self.clock = clock
        self._buf = b""

    def clear(self):
        self._buf = b""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def send(self, text):
        self.ser.write(text.encode("ascii"))
        try:
            self.ser.flush()
        except Exception:
            pass

    def line(self, deadline):
        """The next non-empty line (stripped), or None at the deadline."""
        while True:
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                text = raw.decode("ascii", "replace").strip()
                if text:
                    return text
            if self.clock() >= deadline:
                return None
            chunk = self.ser.readline()
            if chunk:
                self._buf += chunk
            elif self.clock() < deadline:
                time.sleep(0.005)


def parse_block(lines):
    """'Curr Pos = 001234' lines -> {'Curr Pos': '001234'}. Lines without '=' (headers) are skipped."""
    out = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def as_int(block, key, default=None):
    try:
        return int(str(block.get(key, "")).strip().lstrip("+"))
    except ValueError:
        return default


def as_bool(block, key):
    return as_int(block, key, 0) == 1


def as_temp(block, key):
    """Degrees C, or None when no probe is attached ("NP", "No Probe", empty)."""
    try:
        return float(str(block.get(key, "")).strip())
    except ValueError:
        return None


class Unit:
    """Owns one serial port: polls the hub's status in a thread and runs commands between polls, never two at
    once. Subclasses implement hello() (once per (re)connect) and poll().

    After FAILS_TO_DROP failed exchanges in a row the unit counts as down, closes the port and reopens it through
    opener(port name) every REOPEN_S until the hub answers again."""

    POLL_S = 0.5
    FAILS_TO_DROP = 3
    REOPEN_S = 5.0

    def __init__(self, port_name, opener, clock=time.monotonic):
        self.port_name = port_name
        self.opener = opener
        self.clock = clock
        self.lock = threading.RLock()
        self.wire = None
        self.last_error = None
        self._fails = 0
        self._up = False
        self._stop = threading.Event()
        self._thread = None

    # -- subclass hooks
    def hello(self):
        """Read what doesn't change (configuration). Called with the lock held after each (re)connect."""

    def poll(self):
        """Read the status. Called with the lock held."""

    def prepare(self, ser):
        """Called once on each newly opened port, before anything is sent."""

    # -- running
    def start(self, connect_now=False):
        """connect_now: open the port and read the configuration in the calling thread first (the hub creates
        instances while holding its lock, which the held port is taken under, so a poll thread would wait)."""
        if connect_now:
            try:
                with self.lock:
                    self._connect()
            except Exception as e:
                self.failed(e)
        self._thread = threading.Thread(target=self._run, name=f"optec-{self.port_name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self.lock:
            self._close()

    def up(self):
        return self._up

    def _close(self):
        if self.wire is not None:
            try:
                self.wire.ser.close()
            except Exception:
                pass
        self.wire = None
        self._up = False

    def _connect(self):
        ser = self.opener(self.port_name)
        self.wire = Wire(ser, self.clock)
        try:
            self.prepare(ser)
            self.hello()
        except Exception:
            self._close()
            raise
        self._fails = 0
        self._up = True
        self.last_error = None

    def _run(self):
        while not self._stop.is_set():
            try:
                with self.lock:
                    if self.wire is None:
                        self._connect()
                    self.poll()
                self._fails = 0
                self._up = True
            except Exception as e:
                self.failed(e)
                if self.wire is None:
                    self._stop.wait(self.REOPEN_S)
                    continue
            self._stop.wait(self.POLL_S)

    def failed(self, e):
        self.last_error = str(e)
        self._fails += 1
        log.debug("%s: %s", self.port_name, e)
        if self._fails >= self.FAILS_TO_DROP or self.wire is None:
            if self._up:
                log.warning("%s: Optec hub stopped answering (%s)", self.port_name, e)
            with self.lock:
                self._close()

    def run(self, fn, *args):
        """Run a command now (between polls). Errors from the hub (OptecError) pass through; a link failure
        counts towards dropping the link and is raised as RuntimeError."""
        with self.lock:
            if self.wire is None:
                raise RuntimeError("Not connected to the Optec hub")
            try:
                return fn(*args)
            except OptecError:
                raise
            except (OSError, NoAnswer) as e:
                self.failed(e)
                raise RuntimeError(f"Optec hub did not answer: {e}") from e
