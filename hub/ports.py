"""Serial ports with their USB IDs, and a way to open them with DTR/RTS held at a fixed level (off unless a driver asks
for on), so opening never pulses them."""
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PortInfo:
    device: str            # "COM10"
    vid: int = None        # USB vendor ID, None if not a USB port
    pid: int = None
    serial: str = ""       # USB serial number (FTDI chips have one; CH340s usually don't)
    description: str = ""

    @property
    def usb(self):
        return None if self.vid is None else (self.vid, self.pid)

    def label(self):
        ids = f" ({self.vid:04X}:{self.pid:04X})" if self.vid is not None else ""
        return f"{self.device} {self.description}{ids}".strip()


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    return [PortInfo(p.device, p.vid, p.pid, p.serial_number or "", p.description or "") for p in lp.comports()]


# One serial open at a time across the whole hub (scanner and every instance's reconnects). On 2026-11-20 opens sat
# in SetCommState for minutes (see PolledSerial); serialising them is cheap. The wait is bounded: if an open is stuck
# in the driver while holding the lock, the others go ahead after OPEN_WAIT_S instead of queueing behind it forever.
_OPEN_LOCK = threading.Lock()
OPEN_WAIT_S = 10.0


def open_port(device, baud, timeout=0.05, lines=False):
    """An open port, wrapped so that no read ever waits inside the driver (PolledSerial).
    lines: DTR and RTS on (as Wanderer Empire and INDI open the Wanderer devices) or off, set before the open."""
    import serial  # pyserial
    s = serial.Serial()
    s.port = device
    s.baudrate = baud
    s.timeout = 0            # Windows: ReadIntervalTimeout=MAXDWORD, i.e. ReadFile returns at once with what is there
    s.write_timeout = 1.0
    s.dtr = bool(lines)
    s.rts = bool(lines)
    got = _OPEN_LOCK.acquire(timeout=OPEN_WAIT_S)
    try:
        s.open()
    finally:
        if got:
            _OPEN_LOCK.release()
    return PolledSerial(s, timeout)


class PolledSerial:
    """A pyserial port whose reads never wait inside the driver: it waits in Python and only reads bytes that
    have already arrived, as .NET's SerialPort (and so Wanderer Empire) does.

    Why (2026-11-20): with pyserial's own read timeout, every read is a ReadFile left pending in the driver until
    bytes or the timeout come. The WCH CH340 driver 3.9.2024.9 seemed to ignore read timeouts (a read(256) only
    ever returned full 256-byte buffers) and never finished a cancelled read: processes then couldn't exit until
    the device was unplugged, and opening another CH340 port sat in SetCommState for minutes. Reading only
    what is already there leaves nothing pending to cancel.

    Same interface as pyserial for what the links use: read, readline, write, flush, close, in_waiting,
    reset_input_buffer, timeout (seconds; None waits for ever)."""

    POLL_S = 0.01

    def __init__(self, ser, timeout=0.05):
        self._ser = ser
        self._buf = bytearray()
        self.timeout = timeout

    def __getattr__(self, name):              # write, flush, close, is_open, port, baudrate, dtr, rts …
        return getattr(self._ser, name)

    @property
    def in_waiting(self):
        return len(self._buf) + self._ser.in_waiting

    def _fill(self):
        n = self._ser.in_waiting
        if n:
            self._buf += self._ser.read(n)
        return n

    def _wait(self, done):
        """Pull arrived bytes into the buffer until done() or the timeout."""
        end = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            self._fill()
            if done():
                return
            if end is not None and time.monotonic() >= end:
                return
            time.sleep(self.POLL_S)

    def read(self, size=1):
        if size <= 0:
            return b""
        self._wait(lambda: len(self._buf) >= size)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def read_until(self, expected=b"\n", size=None):
        def found():
            return expected in self._buf or (size is not None and len(self._buf) >= size)
        self._wait(found)
        i = self._buf.find(expected)
        end = i + len(expected) if i >= 0 else len(self._buf)
        if size is not None:
            end = min(end, size)
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out

    def readline(self, size=-1):
        return self.read_until(b"\n", None if size is None or size < 0 else size)

    def reset_input_buffer(self):
        self._buf.clear()
        self._ser.reset_input_buffer()

    def close(self):
        self._buf.clear()
        self._ser.close()
