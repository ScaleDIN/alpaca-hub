"""Serial link to the NiteCrawler: one background thread owns the COM port.

The NiteCrawler only speaks when spoken to, so the thread polls it (positions and motion twice a second, limit
switches too, temperature and voltage every 2 s) and runs other threads' commands in between, one at a time:
flush, write, read the answer. Callers block in call() until their answer is in.

Homing ("SH nn#") is special: the firmware answers '#' at once and "OK#" when it has finished, and INDI's driver
sends nothing in between, so neither does this one. Commands that arrive meanwhile are refused (Busy), except stop.
"""
import logging
import queue
import re
import threading
import time

from . import protocol as P

log = logging.getLogger("nitecrawler.link")

ANSWER_S = 1.0         # wait this long for an answer
POLL_S = 0.5           # positions / motion / limits
SLOW_POLL_S = 2.0      # temperature / voltage
FAILS_TO_DROP = 3      # consecutive unanswered commands before the port is closed and opened again
REOPEN_S = 3.0
SCAN_S = 20.0          # auto mode: wait between scans while no NiteCrawler is found
HOMING_MAX_S = 600.0   # give up waiting for "OK#" after this long
_MODEL = re.compile(r"^\d\.\d NC$")


def _is_stop(cmd):
    return re.match(rb"^\dSQ#$", cmd) is not None


class Busy(RuntimeError):
    pass


class NoAnswer(RuntimeError):
    pass


class _Call:
    __slots__ = ("cmd", "kind", "done", "answer", "error", "answer_s", "quiet")

    def __init__(self, cmd, kind, answer_s=ANSWER_S, quiet=False):
        self.cmd, self.kind = cmd, kind
        self.answer_s = answer_s   # how long to wait for the answer
        self.quiet = quiet         # True: no answer is expected from every firmware; don't count it against the link
        self.done = threading.Event()
        self.answer = None
        self.error = None


def open_serial(port):
    """A driver never opens a port: the hub holds it and gives the link a RelaySerial (HubContext.opener_for)."""
    raise OSError(f"no port for {port}: the NiteCrawler's port comes from the hub")


def list_ports():
    """[(device, description, is_usb)]: none. The hub finds the NiteCrawler; the link never looks for it."""
    return []


def list_port_names():
    return []


class Transport:
    """Question and answer on an open serial port."""

    def __init__(self, ser, clock=time.monotonic):
        self.ser = ser
        self.clock = clock
        self.buf = b""
        self.late_ok = False   # an "OK" (homing finished) arrived in the middle of another answer

    def ask(self, cmd, kind, timeout=ANSWER_S):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.buf = b""
        self.ser.write(cmd)
        self.ser.flush()
        if kind is None:
            return None
        return self.read_answer(kind, timeout, cmd)

    def read_answer(self, kind, timeout, cmd=b""):
        end = self.clock() + timeout
        while True:
            answers, self.buf = P.split_answers(self.buf)
            for a in answers:
                if a == "OK" and kind != "ok":
                    self.late_ok = True
                    continue
                if kind == "ack":
                    if a:
                        log.debug("%r: unexpected answer %r instead of '#'", cmd, a)
                    return ""
                if kind == "ok":
                    if a == "OK":
                        return a
                    continue
                return a
            if self.clock() >= end:
                raise NoAnswer(f"no answer to {cmd.decode(errors='replace').strip() or kind}")
            chunk = self.ser.read(max(1, getattr(self.ser, "in_waiting", 0) or 1))
            if chunk:
                self.buf += chunk
                if len(self.buf) > 256:
                    self.buf = self.buf[-64:]


def probe(ser):
    """(firmware, model) if a NiteCrawler answers on this open port, else None."""
    t = Transport(ser)
    try:
        model = t.ask(*P.get_model(), timeout=0.6)
        if not _MODEL.match(model or ""):
            return None
        fw = t.ask(*P.get_firmware(), timeout=0.6)
        return fw, model
    except Exception:
        return None


class NCLink:
    def __init__(self, port="auto", opener=open_serial, clock=time.monotonic, preferred=None,
                 on_port_found=None, on_status=None):
        self.port_setting = port
        self.preferred = preferred
        self._opener = opener
        self._clock = clock
        self._on_port_found = on_port_found or (lambda port: None)
        self._on_status = on_status or (lambda st: None)
        self._calls = queue.Queue()
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self._thread = None
        self._ser = None
        self._t = None
        self.port = None
        self.firmware = None
        self.model = None
        self.status = None
        self.status_t = None
        self.homing_mask = 0
        self.homing_since = None
        self.last_error = ""
        self._fails = 0
        self._next_slow = 0.0
        self.stats = {"polls": 0, "commands": 0, "timeouts": 0, "opens": 0}

    # ---------------------------------------------------------------- public
    def start(self):
        self._thread = threading.Thread(target=self._run, name="nclink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._reopen.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close()

    def up(self):
        return self.status_t is not None and self._clock() - self.status_t < 5.0

    def homing(self):
        return self.homing_mask != 0

    def set_port(self, port):
        self.port_setting = port
        self._reopen.set()

    def auto(self):
        return not self.port_setting or self.port_setting.lower() == "auto"

    @property
    def steps_per_rev(self):
        return P.STEPS_PER_REV.get(self.model or "", P.STEPS_PER_REV[P.DEFAULT_MODEL])

    def call(self, cmd_kind, timeout=5.0, answer_s=ANSWER_S, quiet=False):
        """Runs one command on the link thread and returns its answer text. Raises Busy, NoAnswer or
        ConnectionError. quiet: a command some firmware never answers (PW on stock 1.6); a missing answer then
        raises NoAnswer without counting towards dropping the link."""
        cmd, kind = cmd_kind
        if self.homing() and not _is_stop(cmd):
            raise Busy("The NiteCrawler is homing; wait until it has finished")
        if self._ser is None:
            raise ConnectionError(self.last_error or "NiteCrawler not connected")
        c = _Call(cmd, kind, answer_s, quiet)
        self._calls.put(c)
        if not c.done.wait(timeout):
            raise NoAnswer("the link thread did not get to the command in time")
        if c.error:
            raise c.error
        return c.answer

    # convenience wrappers
    def goto(self, axis, pos):
        self.call(P.set_target(axis, pos))
        self.call(P.start_move(axis))
        self._mark_moving(axis)

    def halt(self, axis):
        self.call(P.stop(axis))

    def sync(self, axis, pos):
        self.call(P.sync(axis, pos))

    def find_home(self, mask):
        # Marked as homing before the command goes out, so the link thread can't slip a poll in between.
        if self._ser is None:
            raise ConnectionError(self.last_error or "NiteCrawler not connected")
        if self.homing():
            raise Busy("The NiteCrawler is already homing")
        self.homing_mask = int(mask)
        self.homing_since = self._clock()
        c = _Call(*P.find_home(mask))
        self._calls.put(c)
        if not c.done.wait(5.0) or c.error:
            self.homing_mask = 0
            raise c.error or NoAnswer("the link thread did not get to the command in time")
        log.info("homing started (mask %d)", mask)

    def _mark_moving(self, axis):
        st = self.status
        if st is not None:
            st.axis(axis).moving = True   # until the next poll says otherwise: IsMoving must be true right away

    # ---------------------------------------------------------------- thread
    def _close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._t = None

    def _candidates(self):
        if not self.auto():
            return [self.port_setting]
        ports = list_ports()
        names = [d for d, _, usb in ports if usb] or [d for d, _, _ in ports]
        if self.preferred in names:
            names.remove(self.preferred)
            names.insert(0, self.preferred)
        return names

    def _open(self):
        for name in self._candidates():
            try:
                ser = self._opener(name)
            except Exception as e:
                self.last_error = f"cannot open {name}: {e}"
                continue
            found = probe(ser)
            if not found:
                try:
                    ser.close()
                except Exception:
                    pass
                if not self.auto():
                    self.last_error = f"no NiteCrawler answers on {name}"
                continue
            self._ser, self._t = ser, Transport(ser, self._clock)
            self.firmware, self.model = found
            self.port = name
            self.preferred = name
            self.stats["opens"] += 1
            self.last_error = ""
            self._fails = 0
            log.info("NiteCrawler %s, firmware %s on %s", self.model, self.firmware, name)
            try:
                self._on_port_found(name)
            except Exception:
                log.exception("port handler failed")
            return True
        if self.auto():
            self.last_error = "no NiteCrawler found on any serial port (looking again every 20 s)"
        return False

    def _run(self):
        while not self._stop.is_set():
            if self._reopen.is_set():
                self._reopen.clear()
                self._close()
                self.port = None
                self.status_t = None
            if self._ser is None:
                self._fail_waiting(ConnectionError(self.last_error or "NiteCrawler not connected"))
                if not self._open():
                    self._reopen.wait(SCAN_S if self.auto() else REOPEN_S)
                    continue
                self._init_axes()
            try:
                if self.homing():
                    self._wait_homing()
                else:
                    self._serve_calls()
                    self._poll()
                    self._serve_calls(until=self._clock() + POLL_S)
            except NoAnswer as e:
                # One missed answer (seen once on the WR35 right after homing) is not a dead link: _ask closes the
                # port itself after FAILS_TO_DROP misses in a row. Until then, skip this poll and carry on.
                if self._ser is not None:
                    log.info("%s on %s; carrying on", e, self.port)
                    continue
                self._reopen.wait(REOPEN_S)
            except Exception as e:  # port vanished (USB unplugged) and the like
                log.warning("link error on %s: %s", self.port, e)
                self.last_error = f"{self.port}: {e}"
                self._close()
                self._reopen.wait(REOPEN_S)

    def _ask(self, cmd_kind, timeout=ANSWER_S):
        try:
            a = self._t.ask(*cmd_kind, timeout=timeout)
        except NoAnswer:
            self.stats["timeouts"] += 1
            self._fails += 1
            if self._fails >= FAILS_TO_DROP:
                self.last_error = "the NiteCrawler stopped answering"
                log.warning("%s on %s; reopening", self.last_error, self.port)
                self._close()
            raise
        self._fails = 0
        return a

    def _serve_calls(self, until=None):
        """Runs queued commands: all that are waiting (until=None), or as they come until the clock says until."""
        while self._ser is not None:
            if self.homing():
                return   # _wait_homing takes over (it also serves stop commands)
            left = 0.0 if until is None else until - self._clock()
            try:
                c = self._calls.get(timeout=left) if left > 0 else self._calls.get_nowait()
            except queue.Empty:
                return
            try:
                if c.quiet:
                    c.answer = self._t.ask(c.cmd, c.kind, timeout=c.answer_s)
                else:
                    c.answer = self._ask((c.cmd, c.kind), timeout=c.answer_s)
                self.stats["commands"] += 1
                log.debug("sent %r -> %r", c.cmd, c.answer)
            except Exception as e:
                c.error = e
            c.done.set()

    def _init_axes(self):
        # Step rates are read with "xGR#", as Moonlite's NC Remote does (firmware 1.6 answers "007#"; INDI's "xSR#"
        # gets nothing). A missing answer just leaves the rate unknown: it isn't counted against the link.
        self.status = P.Status()
        for a in P.AXES:
            try:
                self.status.axis(a).step_delay = P.parse_int(self._t.ask(*P.get_step_delay(a), timeout=0.5))
            except (NoAnswer, ValueError, TypeError):
                pass

    def _poll(self):
        st = P.Status() if self.status is None else self.status
        new = P.Status(temperature=st.temperature, voltage=st.voltage)
        for a in P.AXES:
            ax = new.axis(a)
            ax.step_delay = st.axis(a).step_delay
            # Moving first, then the position: once the axis says it stopped, the position read after that is final.
            # The other order let a move that ended between the two leave (an old position, stopped) until the next
            # poll, and a client reading IsMoving then Position got the old one (hardware run 2026-11-22: 488 of 500).
            ax.moving = P.parse_moving(self._ask(P.is_moving(a)))
            ax.position = P.parse_int(self._ask(P.get_position(a)))
        new.limits = P.Limits.parse(self._ask(P.get_limits()))
        now = self._clock()
        if now >= self._next_slow:
            self._next_slow = now + SLOW_POLL_S
            new.temperature = P.parse_tenths(self._ask(P.get_temperature()))
            new.voltage = P.parse_tenths(self._ask(P.get_voltage()))
        self.status, self.status_t = new, now
        self.stats["polls"] += 1
        try:
            self._on_status(new)
        except Exception:
            log.exception("status handler failed")

    def _wait_homing(self):
        """Nothing but "OK#" (or a stop command) until homing has finished."""
        if self.status is not None:
            self.status.homing = True
            self.status_t = self._clock()  # still alive
        done = False
        try:
            self._t.read_answer("ok", 0.5)
            done = True
        except NoAnswer:
            pass
        while not self._calls.empty():   # stop commands only; call() refuses everything else while homing
            c = self._calls.get_nowait()
            try:
                c.answer = self._t.ask(c.cmd, c.kind)
            except Exception as e:
                c.error = e
            c.done.set()
            if _is_stop(c.cmd):
                log.info("homing stopped by request")
                done = True
        if not done and self._clock() - (self.homing_since or 0) > HOMING_MAX_S:
            log.warning("no OK# after %d s of homing; carrying on", HOMING_MAX_S)
            done = True
        if done:
            log.info("homing finished")
            self.homing_mask = 0
            self.homing_since = None
            if self.status is not None:
                self.status.homing = False

    def _fail_waiting(self, err):
        while True:
            try:
                c = self._calls.get_nowait()
            except queue.Empty:
                return
            c.error = err
            c.done.set()
