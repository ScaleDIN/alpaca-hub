"""The port relay (REFACTOR.md step 4): drivers never hold a serial port, the hub does.

Hub side, PortRelay: owns one open port from detection until the device is dropped, reads it all the time and hands
what arrives to the driver's end. A driver end that closes (a link's reopen) never closes the real port: the next
attach gets the same handle, so the device never sees a reopen (a reopen can reset it, and USB selective suspend).
Only when the port fails (a read or write raises) does the hub close it; the next attach opens it again.

Driver side, RelaySerial: the subset of pyserial the links use (read, read_until, readline with time limits, write,
flush, in_waiting, reset_input_buffer / reset_output_buffer, dtr, rts, baudrate, close, is_open, timeout).

The two ends only exchange messages (tuples of plain values), so that in step 5 they can sit in different processes:
  driver -> hub   ("w", bytes)  ("flush",)  ("reset_in", seq)  ("reset_out",)  ("set", name, value)  ("detach",)
  hub -> driver   ("d", bytes)  ("ack", seq)  ("error", text)
Here they are joined directly (LocalLink); the order of messages is kept either way.

Safety, in the hub (a driver can't get around it):
- writes are capped: at most WRITE_MAX bytes per message, and about twice what the line can carry per second;
  anything over is dropped and logged, so a runaway driver can't flood a device.
- a relay made with refuse=callback (a probe on a port whose USB IDs another driver lists in never_probe) passes no
  byte to the device: each write is dropped and the callback is told.
"""
import logging
import threading
import time

log = logging.getLogger("hub.relay")

HOLD_MAX = 64 * 1024       # bytes kept for a driver end that isn't attached (oldest dropped first)
RX_MAX = 256 * 1024        # bytes a driver end keeps unread (oldest dropped first, like a UART overrun)
WRITE_MAX = 4096           # largest write the hub passes on in one go
POLL_S = 0.01              # how often the hub looks for bytes on a pyserial port
ACK_S = 2.0                # how long reset_input_buffer waits for the hub


try:  # pyserial's exception, so links that catch it keep working; OSError otherwise (it is one)
    from serial import SerialException as RelayError
except Exception:  # pragma: no cover
    class RelayError(OSError):
        pass


class PortRelay:
    """The hub's side of one port. reopen() -> a newly opened port, for when the old one failed."""

    def __init__(self, ser, device, baud, reopen=None, refuse=None, name=None):
        self.device = device
        self.baud = baud
        self._ser = ser
        self._reopen = reopen
        self._refuse = refuse
        self._lock = threading.RLock()        # the real port and the attached end
        self._end = None                      # the attached driver end's deliver(msg)
        self._hold = bytearray()              # bytes read while no end is attached
        self._attached_once = False
        self._closed = threading.Event()
        self._kick = threading.Event()        # set after a write: look for the answer at once, not after POLL_S
        self._error = None                    # why the port was closed by the hub (it failed)
        self._tokens = float(self._rate())
        self._tokens_t = time.monotonic()
        self.dropped = 0                      # bytes not passed on (cap or refusal)
        self.refused = 0                      # writes dropped because of refuse
        self._dropped_logged = 0.0
        self._pump = threading.Thread(target=self._run, name=f"relay-{name or device}", daemon=True)
        self._pump.start()

    # ---------------------------------------------------------------- the real port
    def _rate(self):
        """Bytes per second a driver may write: about twice what the line carries (10 bits a byte), at least 1 KB."""
        return max(1024, int(self.baud or 0) // 5)

    def _read_some(self, ser):
        if hasattr(type(ser), "in_waiting"):    # pyserial and PolledSerial: only what has already arrived
            n = ser.in_waiting
            if n:
                return ser.read(n)
            self._kick.wait(POLL_S)
            self._kick.clear()
            return b""
        read = getattr(ser, "read", None)       # simulators: they wait for their own timeout
        return (read(256) if read else ser.readline()) or b""

    def _run(self):
        while not self._closed.is_set():
            ser = self._ser
            if ser is None:
                self._kick.wait(0.05)
                self._kick.clear()
                continue
            try:
                data = self._read_some(ser)
            except Exception as e:
                self._fail(f"read failed on {self.device}: {e}", ser)
                continue
            if data:
                self._deliver_data(bytes(data))

    def _deliver_data(self, data):
        # Under the lock, so data and acks reach the end in the order the hub saw them (the lock order is always
        # relay, then the end's own lock).
        with self._lock:
            end = self._end
            if end is None:
                self._hold += data
                if len(self._hold) > HOLD_MAX:
                    del self._hold[:len(self._hold) - HOLD_MAX]
                return
            end(("d", data))

    def _fail(self, why, ser):
        """The port failed: close it, tell the driver's end. The next attach opens it again."""
        with self._lock:
            if self._ser is not ser:
                return
            self._ser = None
            self._error = why
            end = self._end
        log.warning("%s", why)
        _close(ser)
        if end is not None:
            end(("error", why))

    def _ensure_open(self):
        with self._lock:
            if self._ser is not None:
                return
            if self._reopen is None:
                raise RelayError(self._error or f"{self.device} is closed")
            ser = self._reopen()
            self._ser, self._error = ser, None
            self._kick.set()
            if self.baud and getattr(ser, "baudrate", self.baud) != self.baud:
                ser.baudrate = self.baud

    # ---------------------------------------------------------------- driver ends
    def detach(self, end):
        """Lets go of end if it is the attached one (its driver process ended): what arrives is held again."""
        with self._lock:
            if self._end is end:
                self._end = None
                self._hold.clear()

    def attach(self, end):
        """Connects a driver end (its deliver(msg)); a previous one is detached. Bytes that arrived before the
        first attach are passed on (the status line detection heard); after a detach they are dropped, as a real
        reopen would."""
        if self._closed.is_set():
            raise RelayError(f"{self.device} is no longer held")
        self._ensure_open()
        with self._lock:
            old, self._end = self._end, end
            held = bytes(self._hold) if not self._attached_once else b""
            self._hold.clear()
            self._attached_once = True
            if held:
                end(("d", held))
        if old is not None:
            old(("error", "another end attached"))

    def handle(self, end, msg):
        """A message from a driver end. Returns nothing: answers go back through deliver."""
        with self._lock:
            if end is not self._end:
                if msg[0] != "detach":
                    end(("error", f"{self.device}: this end was detached"))
                return
            op = msg[0]
            if op == "detach":
                self._end = None
                self._hold.clear()
                return
            ser = self._ser
        if ser is None:
            end(("error", self._error or f"{self.device} is closed"))
            return
        try:
            if op == "w":
                self._write(ser, msg[1])
            elif op == "flush":
                f = getattr(ser, "flush", None)
                if f:
                    f()
            elif op == "reset_in":
                with self._lock:              # no data delivered between the reset and its ack
                    self._hold.clear()
                    r = getattr(ser, "reset_input_buffer", None)
                    if r:
                        r()
                    end(("ack", msg[1]))
            elif op == "reset_out":
                r = getattr(ser, "reset_output_buffer", None)
                if r:
                    r()
            elif op == "set":
                name, value = msg[1], msg[2]
                if name not in ("dtr", "rts", "baudrate"):
                    raise ValueError(f"cannot set {name}")
                if name == "baudrate":
                    value = int(value)
                    self.baud = value
                if hasattr(ser, name) or name == "baudrate":
                    setattr(ser, name, value)
            else:
                raise ValueError(f"unknown message {op!r}")
        except ValueError as e:
            end(("error", str(e)))
        except Exception as e:
            self._fail(f"write failed on {self.device}: {e}", ser)

    def _write(self, ser, data):
        data = bytes(data)
        if self._refuse is not None:
            self.refused += 1
            self.dropped += len(data)
            self._refuse(self.device, data)
            return
        now = time.monotonic()
        rate = self._rate()
        self._tokens = min(rate, self._tokens + (now - self._tokens_t) * rate)
        self._tokens_t = now
        allowed = int(min(len(data), WRITE_MAX, self._tokens))
        if allowed < len(data):
            self.dropped += len(data) - allowed
            if now - self._dropped_logged > 10:
                self._dropped_logged = now
                log.warning("%s: a driver wrote more than the port may take; %d bytes dropped so far",
                            self.device, self.dropped)
            data = data[:allowed]
        if data:
            self._tokens -= len(data)
            ser.write(data)
            self._kick.set()

    def close(self):
        """Stops relaying and closes the real port (the device was dropped, or the hub stops)."""
        self._closed.set()
        self._kick.set()
        with self._lock:
            ser, self._ser = self._ser, None
            end, self._end = self._end, None
        if end is not None:
            end(("error", f"{self.device} was released by the hub"))
        if ser is not None:
            _close(ser)
        if self._pump is not threading.current_thread():
            self._pump.join(timeout=1.0)

    @property
    def closed(self):
        return self._closed.is_set()


def _close(ser):
    try:
        ser.close()
    except Exception:
        pass


def check_driver_msg(msg):
    """A relay message from a driver process, checked before the hub acts on it. Raises ValueError."""
    if not isinstance(msg, list) or not msg or not isinstance(msg[0], str):
        raise ValueError("not a relay message")
    op, n = msg[0], len(msg)
    ok = ((op == "w" and n == 2 and isinstance(msg[1], bytes) and len(msg[1]) <= 65536)
          or (op in ("flush", "reset_out", "detach") and n == 1)
          or (op == "reset_in" and n == 2 and type(msg[1]) is int)
          or (op == "set" and n == 3 and msg[1] in ("dtr", "rts") and isinstance(msg[2], bool))
          or (op == "set" and n == 3 and msg[1] == "baudrate" and type(msg[2]) is int and 50 <= msg[2] <= 4000000))
    if not ok:
        raise ValueError(f"bad relay message {op!r}")


class LocalLink:
    """Joins a RelaySerial to a PortRelay in the same process (step 5 puts a pipe here instead)."""

    def __init__(self, relay):
        self.relay = relay
        self._deliver = None

    def connect(self, deliver):
        self._deliver = deliver
        self.relay.attach(deliver)

    def send(self, msg):
        self.relay.handle(self._deliver, msg)


class RelaySerial:
    """The driver's port: what a link gets from HubContext.opener_for. Behaves like a pyserial port opened with a
    short timeout (reads return what there is when the timeout passes)."""

    def __init__(self, link, device, baud, timeout=0.05, lines=False):
        self.port = self.name = device
        self.timeout = timeout
        self.write_timeout = 1.0
        self._link = link
        self._baud = baud
        self._dtr = self._rts = bool(lines)
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._error = None
        self._open = True
        self._seq = 0
        self._acked = 0
        link.connect(self._deliver)
        if getattr(link, "lines", None) is not None:      # a driver process learns the level from the hub
            self._dtr = self._rts = bool(link.lines)
        if getattr(link, "baud", None):
            self._baud = link.baud

    # ---------------------------------------------------------------- messages from the hub
    def _deliver(self, msg):
        with self._cv:
            op = msg[0]
            if op == "d":
                self._buf += msg[1]
                if len(self._buf) > RX_MAX:
                    del self._buf[:len(self._buf) - RX_MAX]
            elif op == "ack":
                self._acked = max(self._acked, msg[1])
                if self._acked >= self._seq:
                    self._buf.clear()        # what came before the reset (in order, so all of it) is stale
            elif op == "error":
                self._error = msg[1]
            self._cv.notify_all()

    def _check(self):
        if not self._open:
            raise RelayError(f"{self.port} is closed")
        if self._error:
            raise RelayError(self._error)

    def _send(self, msg):
        self._check()
        self._link.send(msg)
        if self._error:
            raise RelayError(self._error)

    # ---------------------------------------------------------------- pyserial
    @property
    def is_open(self):
        return self._open

    @property
    def in_waiting(self):
        self._check()
        with self._cv:
            return len(self._buf)

    def _wait(self, done):
        end = None if self.timeout is None else time.monotonic() + self.timeout
        with self._cv:
            while True:
                if done():
                    return
                self._check()
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return
                self._cv.wait(left)

    def read(self, size=1):
        if size <= 0:
            return b""
        self._check()
        self._wait(lambda: len(self._buf) >= size)
        with self._cv:
            out = bytes(self._buf[:size])
            del self._buf[:size]
            return out

    def read_until(self, expected=b"\n", size=None):
        self._check()

        def found():
            return expected in self._buf or (size is not None and len(self._buf) >= size)
        self._wait(found)
        with self._cv:
            i = self._buf.find(expected)
            end = i + len(expected) if i >= 0 else len(self._buf)
            if size is not None:
                end = min(end, size)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def readline(self, size=-1):
        return self.read_until(b"\n", None if size is None or size < 0 else size)

    def write(self, data):
        data = bytes(data)
        self._send(("w", data))
        return len(data)

    def flush(self):
        self._send(("flush",))

    def reset_input_buffer(self):
        with self._cv:
            self._seq += 1
            seq = self._seq
            self._buf.clear()
        self._send(("reset_in", seq))
        end = time.monotonic() + ACK_S
        with self._cv:
            while self._acked < seq and not self._error:
                left = end - time.monotonic()
                if left <= 0:
                    raise RelayError(f"{self.port}: the hub did not answer")
                self._cv.wait(left)
        self._check()

    def reset_output_buffer(self):
        self._send(("reset_out",))

    def _set(self, name, value):
        self._send(("set", name, value))

    @property
    def baudrate(self):
        return self._baud

    @baudrate.setter
    def baudrate(self, value):
        self._set("baudrate", int(value))
        self._baud = int(value)

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._set("dtr", bool(value))
        self._dtr = bool(value)

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._set("rts", bool(value))
        self._rts = bool(value)

    def close(self):
        """Lets go of the port. The hub keeps the real one open (see PortRelay)."""
        if not self._open:
            return
        self._open = False
        try:
            self._link.send(("detach",))
        except Exception:
            pass
        with self._cv:
            self._buf.clear()
            self._cv.notify_all()
