"""Serial link to the box: one background thread owns the COM port.

* Opens the port with DTR and RTS held low, so (if the board resets on DTR like an Arduino) opening the port
  doesn't reboot the box. Board checklist step 2 tells whether the box resets anyway.
* Reads status lines and keeps the latest one.
* Sends queued commands one per status line, because the stock firmware reads one number per loop. A WSM box
  (the user's open firmware) gets each one at once, ended by a newline, as soon as the one before is confirmed.
  A newer command for the same output replaces one still waiting in the queue.
* Callers can wait until a status line confirms the command took effect. A command with a confirm check that
  doesn't show up is sent once more (commands set absolute values, so repeating one is harmless), unless a newer
  command for the same output is already waiting.
"""
import logging
import threading
import time
from collections import OrderedDict

from . import protocol as P

log = logging.getLogger("wbox.link")

STALE_S = 5.0          # no status line for this long = box down
REOPEN_S = 3.0         # retry interval after the port failed or vanished
SCAN_S = 30.0          # auto mode: wait between scans of every port while no box is found
CONFIRM_LINES = 4      # status lines to wait for a command to show up before giving up


class _Pending:
    __slots__ = ("cmd", "confirm", "events", "result", "sent_at_line", "resent")

    def __init__(self, cmd, confirm):
        self.cmd = cmd
        self.confirm = confirm
        self.events = [threading.Event()]
        self.result = False
        self.sent_at_line = None
        self.resent = False


def open_serial(port):
    """A driver never opens a port: the hub holds it and gives the link a RelaySerial (HubContext.opener_for)."""
    raise OSError(f"no port for {port}: the box's port comes from the hub")


def list_port_names():
    """None: the hub finds the box (by listening, never by writing); the link never looks for it."""
    return []


def find_port(opener=open_serial, listen_s=3.0, preferred=None, ports=None):
    """Looks for a box on the serial ports (the preferred one first) by listening for a status line.
    Read-only: nothing is written to ports that aren't the box. Returns the port name or None."""
    names = list_port_names() if ports is None else list(ports)
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    for name in names:
        try:
            s = opener(name)
        except Exception:
            continue
        try:
            end = time.monotonic() + listen_s
            while time.monotonic() < end:
                line = s.readline().decode("ascii", "replace")
                if P.parse_status(line):
                    log.info("found a WandererBox Plus V3 on %s", name)
                    return name
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return None


def _channel(cmd):
    if cmd in (110, 111):
        return "usb"
    if cmd in (120, 121):
        return "dc2"
    if cmd in (100, 101):
        return "dc46"
    if 3000 <= cmd <= 3255:
        return "dew"
    if 20000 <= cmd <= 20132:
        return "dc2v"
    return f"cmd{cmd}"


class BoxLink:
    def __init__(self, port="auto", opener=open_serial, on_status=None, on_write=None, clock=time.monotonic,
                 preferred=None, on_port_found=None):
        self.port_setting = port
        self.preferred = preferred            # auto mode: port to try first (where the box was last time)
        self._on_port_found = on_port_found or (lambda port: None)
        self._reopen = threading.Event()
        self.port = None
        self._opener = opener
        self._on_status = on_status or (lambda st: None)
        self._on_write = on_write or (lambda cmd: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._queue = OrderedDict()   # channel -> _Pending (not yet written)
        self._inflight = []           # written, waiting for confirmation
        self._status = None
        self._status_t = None
        self._lines = 0
        self.stats = {"lines": 0, "bad": 0, "writes": 0, "opens": 0, "errors": 0, "resends": 0}
        self._stop = threading.Event()
        self._thread = None
        self._ser = None
        self.last_error = ""

    # ---------------------------------------------------------------- public
    def start(self):
        self._thread = threading.Thread(target=self._run, name="boxlink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._reopen.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close()

    @property
    def status(self):
        return self._status

    def up(self):
        return self._status_t is not None and self._clock() - self._status_t < STALE_S

    def status_age(self):
        return None if self._status_t is None else self._clock() - self._status_t

    def send(self, cmd, confirm=None, wait=False, timeout=8.0):
        """Queues a command. With wait=True, blocks until a status line satisfies confirm(status)
        (or, without confirm, until the command has been written). Returns True on success."""
        p = _Pending(int(cmd), confirm)
        ch = _channel(p.cmd)
        with self._lock:
            old = self._queue.pop(ch, None)
            if old is not None:
                p.events.extend(old.events)  # whoever waited on the replaced command now waits on this one
            self._queue[ch] = p
        if not wait:
            return True
        ok = p.events[0].wait(timeout)
        return ok and p.result

    def set_port(self, port):
        """Switches to another port setting (a COM port or 'auto') at run time."""
        self.port_setting = port
        self._reopen.set()

    def auto(self):
        return not self.port_setting or self.port_setting.lower() == "auto"

    def queued(self):
        with self._lock:
            return [p.cmd for p in self._queue.values()]

    # ---------------------------------------------------------------- thread
    def _close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _open(self):
        port = self.port_setting
        if self.auto():
            port = find_port(self._opener, preferred=self.preferred)
            if not port:
                self.last_error = "no WandererBox Plus V3 found on any serial port (looking again every 30 s)"
                return False
            self.preferred = port
            try:
                self._on_port_found(port)
            except Exception:
                log.exception("port handler failed")
        try:
            self._ser = self._opener(port)
        except Exception as e:
            self.last_error = f"cannot open {port}: {e}"
            return False
        self.port = port
        self.stats["opens"] += 1
        self.last_error = ""
        log.info("opened %s", port)
        return True

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            if self._reopen.is_set():
                self._reopen.clear()
                self._close()
                self.port = None
                self._status_t = None
            if self._ser is None:
                if not self._open():
                    self._fail_all_waiting()
                    self._reopen.wait(SCAN_S if self.auto() else REOPEN_S)
                    continue
                buf = b""
            if self._fast() and not self._inflight:
                self._write_next()   # WSM answers each command at once: no need to wait for the next status line
            try:
                chunk = self._ser.readline()
            except Exception as e:
                self._error(f"read failed on {self.port}: {e}")
                continue
            if not chunk:
                continue
            buf += chunk
            if not buf.endswith(b"\n"):
                if len(buf) > 512:
                    buf = b""
                continue
            line, buf = buf.decode("ascii", "replace"), b""
            st = P.parse_status(line)
            if st is None:
                if line.strip():
                    self.stats["bad"] += 1
                continue
            self._got_status(st)

    def _error(self, msg):
        log.warning(msg)
        self.last_error = msg
        self.stats["errors"] += 1
        self._close()
        self._reopen.wait(REOPEN_S)

    def _got_status(self, st):
        if self._status is None or st.fw != self._status.fw:
            log.info("box firmware %s", st.fw or "?")
        self._status = st
        self._status_t = self._clock()
        self._lines += 1
        self.stats["lines"] += 1
        try:
            self._on_status(st)
        except Exception:
            log.exception("status handler failed")
        self._check_inflight(st)
        self._write_next()

    def _check_inflight(self, st):
        keep = []
        for p in self._inflight:
            ok = p.confirm is None or p.confirm(st)
            if ok or self._lines - p.sent_at_line >= CONFIRM_LINES:
                if not ok and not p.resent and self._requeue(p):
                    log.info("command %d not confirmed by the box, sending it once more", p.cmd)
                    continue
                p.result = ok
                if not ok:
                    log.warning("command %d not confirmed by the box", p.cmd)
                for e in p.events:
                    e.set()
            else:
                keep.append(p)
        self._inflight = keep

    def _requeue(self, p):
        """Queues an unconfirmed command again, unless a newer one for the same output is waiting."""
        with self._lock:
            ch = _channel(p.cmd)
            if ch in self._queue:
                return False
            p.resent = True
            p.sent_at_line = None
            self._queue[ch] = p
        self.stats["resends"] += 1
        return True

    def _fast(self):
        """A WSM box reads a number as soon as a non-digit ends it and answers with a status line at once
        (PROTOCOL.md 2.6, 3), so commands go out one after another, each confirmed by its reply."""
        return self._status is not None and P.is_open_firmware(self._status.fw)

    def _write_next(self):
        with self._lock:
            if not self._queue:
                return
            _, p = self._queue.popitem(last=False)
        try:
            self._ser.write(P.encode(p.cmd, terminate=self._fast()))
            self._ser.flush()
        except Exception as e:
            p.result = False
            for ev in p.events:
                ev.set()
            self._error(f"write failed on {self.port}: {e}")
            return
        self.stats["writes"] += 1
        log.debug("sent %d", p.cmd)
        try:
            self._on_write(p.cmd)
        except Exception:
            log.exception("write handler failed")
        p.sent_at_line = self._lines
        self._inflight.append(p)

    def _fail_all_waiting(self):
        with self._lock:
            pend = list(self._queue.values()) + self._inflight
            self._queue.clear()
            self._inflight = []
        for p in pend:
            p.result = False
            for e in p.events:
                e.set()
