"""The WandererCover V4-EC's serial link and the device the Alpaca handler drives (the wcover driver).

Taken from wbstock/cover.py (REFACTOR.md step 7; wbstock was deleted in step 9). The
link (V4ecCoverLink) follows INDI's open driver and the captures made on the real panel on 2026-11-19
(wanderer/cover-capture), and has driven the real panel through the hub since 2026-11-20. Its port always comes
from the hub (HubContext.opener_for): it never opens one itself.
"""
import logging
import threading
import time
from dataclasses import dataclass
from enum import IntEnum

log = logging.getLogger("wcover.link")

V4EC_BAUD = 19200
# Status line fields, in order, after splitting on 'A' and dropping empties (cover-capture/v4ec.py).
_STATUS_FIELDS = ("model", "firmware", "close_set", "open_set", "position", "voltage", "brightness",
                  "heater", "asiair")


def parse_cover_status(line):
    """Decode one WandererCover status line into {field: float|str}, or None if it isn't one of theirs."""
    tokens = [t for t in line.strip().split("A") if t != ""]
    if not tokens or not tokens[0].startswith("WandererCover"):
        return None
    out = {}
    for name, value in zip(_STATUS_FIELDS, tokens):
        try:
            out[name] = float(value)
        except ValueError:
            out[name] = value
    return out


class CoverState(IntEnum):
    NOT_PRESENT = 0
    CLOSED = 1
    MOVING = 2
    OPEN = 3
    UNKNOWN = 4
    ERROR = 5


class CalibratorState(IntEnum):
    NOT_PRESENT = 0
    OFF = 1
    NOT_READY = 2
    READY = 3
    UNKNOWN = 4
    ERROR = 5


@dataclass
class CoverSnapshot:
    cover: CoverState
    calibrator: CalibratorState
    brightness: int


class CoverBusy(RuntimeError):
    """The panel can't take this command right now: it is moving the cover, or the light was asked for
    while the cover isn't closed."""


MOVE_BUSY_MSG = ("The flat panel is moving the cover and takes no commands until it finishes "
                 "(about 18 s). Try again once CoverState is no longer Moving")
LIGHT_NEEDS_CLOSED_MSG = ("The flat panel only lights while the cover is closed (it switches the light off by "
                          "itself when open). Close the cover first")


class CoverLink:
    """Interface the CoverDevice drives. up()/snapshot() report state; the rest act on the panel.
    halt() raises NotImplementedError when the panel has no halt command; open/close/calibrator calls
    raise CoverBusy while the panel can't take commands."""
    MAX_BRIGHTNESS = 255

    def start(self):
        pass

    def stop(self):
        pass

    def up(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def halt(self):
        raise NotImplementedError("The WandererCover V4-EC has no halt command")

    def calibrator_on(self, brightness):
        raise NotImplementedError

    def calibrator_off(self):
        raise NotImplementedError


def _no_opener(port):
    raise OSError(f"no port for {port}: the panel's port comes from the hub (HubContext.opener_for)")


class V4ecCoverLink(CoverLink):
    """Real serial link to a WandererCover V4-EC. One background thread owns the port, reads status lines and
    keeps the latest. Commands are written as bare numbers ending in a newline.

    Behaviour captured on the real panel (fw 20250703, 2026-11-19, cover-capture/README.md):
    - At rest it sends a status line every 0.33 s. Angles are plain degrees, stored on the panel.
    - Brightness N (0..255) and 9999 (off) show in the status line within ~0.2-0.45 s.
    - Open (1001) and close (1000) BLOCK the firmware. There is no output at all for ~18.4 s, then a line
      "done", then status lines again. So silence during a move is normal, and the panel takes no commands
      meanwhile.
    - With the cover open the panel switches the light off by itself: brightness 30 showed on one status
      line, then 0. With the cover closed, brightness 50 held.
    """

    STALE_S = 5.0           # silence longer than this means the panel is gone (outside a move)
    REOPEN_S = 3.0
    SETTLE_TOL_DEG = 3.0    # position jitters +-0.7 deg at rest; open settled 0.4-1.1 deg short of open_set
    MOVE_TIMEOUT_S = 45.0   # a full move took 18.5 s; give up waiting for "done" after this
    DONE_WAIT_S = 2.0       # after "done", wait this long at most for the status line that ends the move
    MOVE_END_AFTER_S = 1.0  # a status line this long after a move command also ends the move (missed "done")
    CONFIRM_S = 2.0         # how long the calibrator may report NOT_READY while waiting for the status line

    def __init__(self, port, opener=None, clock=time.monotonic):
        self._port = port
        self._opener = opener or _no_opener    # the hub gives the port (HubContext.opener_for)
        self._clock = clock
        self._born = clock()       # until the first status line, the panel may be in a move started before us
        self._lock = threading.RLock()
        self._status = None        # last parsed status dict
        self._status_t = None
        self._move = None          # CoverState.OPEN / CLOSED while the panel is moving
        self._move_t = 0.0
        self._done_t = None        # "done" arrived; the move ends with the next status line (its position is fresh)
        self._failed_move = False  # a move timed out without "done"; cleared by the next status line
        self._short = None         # (target, why) when a move ended away from where it was sent; cleared by a new move
        self._want = None          # brightness last commanded, until the status line confirms it
        self._want_t = 0.0
        self._on_at_zero = False   # CalibratorOn(0): the panel's light is off, but Alpaca expects Ready, not Off
        self._jog = None           # (end, angle) while a jog's command sequence runs: reads as MOVING meanwhile
        self._sleep = time.sleep
        self._stop = threading.Event()
        self._thread = None
        self._ser = None
        self.last_error = ""

    def start(self):
        self._thread = threading.Thread(target=self._run, name="coverlink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._close()

    def _moving(self):
        """True while a move is under way. Ends it, as a failure, once MOVE_TIMEOUT_S has passed."""
        if self._done_t is not None and self._clock() - self._done_t > self.DONE_WAIT_S:
            self._move, self._done_t = None, None  # "done" but no status line since: the snapshot shows it as stale
        if self._move is not None and self._clock() - self._move_t > self.MOVE_TIMEOUT_S:
            log.warning("cover: no 'done' within %.0f s of the move command", self.MOVE_TIMEOUT_S)
            self.last_error = (f"the panel didn't report the end of the move within {self.MOVE_TIMEOUT_S:.0f} s "
                               "(the cover may be stuck, or the panel reset)")
            self._move, self._failed_move = None, True
        return self._move is not None or self._jog is not None

    def up(self):
        with self._lock:
            if self._moving():
                return self._ser is not None  # the panel is silent while it moves; that's expected
            return self._status_t is not None and self._clock() - self._status_t < self.STALE_S

    def _close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            if self._ser is None:
                try:
                    self._ser = self._opener(self._port)
                    self.last_error = ""
                    log.info("opened cover on %s", self._port)
                except Exception as e:
                    self.last_error = f"cannot open {self._port}: {e}"
                    self._stop.wait(self.REOPEN_S)
                    continue
                buf = b""
            try:
                chunk = self._ser.readline()
            except Exception as e:
                self.last_error = f"read failed on {self._port}: {e}"
                self._close()
                self._stop.wait(self.REOPEN_S)
                continue
            if not chunk:
                continue
            buf += chunk
            if not buf.endswith(b"\n"):
                if len(buf) > 512:
                    buf = b""
                continue
            line, buf = buf.decode("ascii", "replace"), b""
            self.feed_line(line)

    def feed_line(self, line):
        """Handle one complete line from the panel. The reader thread calls this; tests call it directly."""
        text = line.strip()
        now = self._clock()
        with self._lock:
            if text == "done":
                if self._move is not None:
                    log.info("cover: move done after %.1f s", now - self._move_t)
                    # Keep reporting MOVING until the next status line: until then the last one still holds the
                    # position from before the move, and a client polling now would read the old state.
                    self._done_t = now
                return
            st = parse_cover_status(text)
            if st is None:
                if text:
                    log.debug("cover: unrecognised line %r", text)
                return
            self._status, self._status_t = st, now
            self._failed_move = False
            if self._move is not None and (self._done_t is not None or now - self._move_t >= self.MOVE_END_AFTER_S):
                target = self._move
                self._move = None  # "done" came, or the firmware is talking again (its "done" was lost)
                self._done_t = None
                if self._jog is None:   # a jog's moves may stop part way by design (see jog)
                    self._check_arrival(target, st)
            if self._short is not None and self._cover_state(st) == self._short[0]:
                self._short = None  # there after all (its angle was changed to where the cap is)
            if self._want is not None and st.get("brightness") == self._want:
                self._want = None

    def _send(self, number):
        if self._ser is None:
            raise RuntimeError(f"Flat panel link not open ({self.last_error or 'not started'})")
        self._ser.write(f"{int(number)}\n".encode("ascii"))
        self._ser.flush()

    def snapshot(self):
        with self._lock:
            moving = self._moving()
            st = self._status
            fresh = self._status_t is not None and self._clock() - self._status_t < self.STALE_S
            if moving:
                cover = CoverState.MOVING
            elif st is None and not self._failed_move and self._clock() - self._born < self.MOVE_TIMEOUT_S:
                # Nothing heard yet. After a driver restart the panel may be in a move started before (it is
                # silent then, for up to ~18 s): unknown, not an error, until a line comes or no move could
                # still be running (real panel, 2026-11-22: a restart mid-move read Error for a few seconds).
                return CoverSnapshot(CoverState.UNKNOWN, CalibratorState.UNKNOWN, 0)
            elif self._failed_move or not fresh or st is None:
                return CoverSnapshot(CoverState.ERROR, CalibratorState.ERROR, 0)
            elif self._short is not None:
                cover = CoverState.ERROR   # the light still works, so the calibrator reads as usual
            else:
                cover = self._cover_state(st)
            b = st.get("brightness") if st else None
            b = int(b) if isinstance(b, float) else 0
            if self._want is not None and self._clock() - self._want_t < self.CONFIRM_S:
                cal = CalibratorState.NOT_READY
            elif self._want is not None:
                cal = CalibratorState.ERROR  # commanded, never confirmed by the panel
            else:
                cal = CalibratorState.READY if b > 0 or self._on_at_zero else CalibratorState.OFF
            return CoverSnapshot(cover, cal, b)

    def _check_arrival(self, target, st):
        """A move the panel called done, but the cap isn't at that end. On the real panel (2026-11-23) the cap caught
        on something while opening: the panel stopped it at 67.9° (open is 295°), said "done", and reported no
        error. Alpaca's Unknown would leave a client waiting for Open until its own timeout, so report Error."""
        if self._cover_state(st) == target:
            return
        end = "open" if target == CoverState.OPEN else "closed"
        pos, want = st.get("position"), st.get("open_set" if target == CoverState.OPEN else "close_set")
        if not isinstance(pos, float) or not isinstance(want, float):
            return
        why = (f"the cover stopped at {pos:.1f}°, short of {end} ({want:g}°): something may be in its way. "
               + ("Close it, then open it again" if target == CoverState.OPEN else "Open it, then close it again")
               + " (the panel ignores a repeat of its last move)")
        log.warning("cover: %s", why)
        self._short, self.last_error = (target, why), why

    def _cover_state(self, st):
        """Cover state at rest, from the reported position against the angles stored on the panel."""
        pos, close_set, open_set = st.get("position"), st.get("close_set"), st.get("open_set")
        if not all(isinstance(x, float) for x in (pos, close_set, open_set)):
            return CoverState.UNKNOWN
        if abs(pos - close_set) <= self.SETTLE_TOL_DEG:
            return CoverState.CLOSED
        if abs(pos - open_set) <= self.SETTLE_TOL_DEG:
            return CoverState.OPEN
        return CoverState.UNKNOWN

    def _start_move(self, target, number):
        with self._lock:
            if self._moving():
                if self._move == target:
                    return  # already on its way there
                raise CoverBusy(MOVE_BUSY_MSG)
            st = self._status
            if st is not None and self._cover_state(st) == target:
                return  # already there; the panel would only re-run the move
            self._send(number)
            self._move, self._move_t, self._done_t = target, self._clock(), None
            self._on_at_zero = False
            if self._short is not None:
                self._short, self.last_error = None, ""

    def open(self):
        self._start_move(CoverState.OPEN, 1001)

    def close(self):
        self._start_move(CoverState.CLOSED, 1000)

    def _set_light(self, number, brightness, on_at_zero=False):
        with self._lock:
            if self._moving():
                raise CoverBusy(MOVE_BUSY_MSG)
            st = self._status
            if brightness > 0 and (st is None or self._cover_state(st) != CoverState.CLOSED):
                raise CoverBusy(LIGHT_NEEDS_CLOSED_MSG)
            self._send(number)
            self._want, self._want_t = brightness, self._clock()
            self._on_at_zero = on_at_zero

    def calibrator_on(self, brightness):
        b = int(brightness)
        # Wanderer's protocol (20250506) lists brightness as 1-255 and off as 9999, so 0 goes out as 9999.
        self._set_light(b if b > 0 else 9999, b, on_at_zero=b == 0)

    def calibrator_off(self):
        self._set_light(9999, 0)  # light off

    # ---------------------------------------------------------------- added in alpaca-hub
    HEATER_LEVELS = {0: 2000, 50: 2050, 100: 2100, 150: 2150}   # Wanderer's protocol: off / low / high / max

    def fields(self):
        """The last status line as {field: value} (angles, voltage, brightness, heater …), or {} if none yet."""
        with self._lock:
            return dict(self._status or {})

    def set_heater(self, level):
        """Dew heater level: 0 (off), 50, 100 or 150 (the panel's only four levels). Refused while moving."""
        level = int(level)
        if level not in self.HEATER_LEVELS:
            raise ValueError("Heater level must be 0 (off), 50 (low), 100 (high) or 150 (max)")
        with self._lock:
            if self._moving():
                raise CoverBusy(MOVE_BUSY_MSG)
            self._send(self.HEATER_LEVELS[level])

    # Angle commands (Wanderer's protocol): closed = 10000 + degrees x 100, open = 40000 + degrees x 100. Checked on
    # the real panel 2026-11-19 (cover-capture README): 11500 replied "ClosePositionSet15.00", 69500
    # "OpenPositionSet295.00", within 0.3 s; the cap doesn't move and the status line shows the new angle at once.
    # The ranges are the plausible ones (the panel ships at 15 / 295; open auto-detect stopped at 300).
    ANGLE_RANGES = {"close": (10.0, 90.0), "open": (100.0, 300.0)}
    ANGLE_BASE = {"close": 10000, "open": 40000}

    def set_angle(self, which, degrees):
        """Store the closed ("close") or open ("open") angle on the panel, in degrees (0.01 steps). Doesn't move the
        cap; the next open/close goes there. Refused while moving. Never runs the panel's auto-detect."""
        if which not in self.ANGLE_RANGES:
            raise ValueError("Angle must be 'close' or 'open'")
        deg = float(degrees)
        lo, hi = self.ANGLE_RANGES[which]
        if not lo <= deg <= hi:
            raise ValueError(f"The {'closed' if which == 'close' else 'open'} angle must be {lo:g} to {hi:g} degrees")
        with self._lock:
            if self._moving():
                raise CoverBusy(MOVE_BUSY_MSG)
            self._send(self.ANGLE_BASE[which] + int(round(deg * 100)))

    # Jog (added in alpaca-hub 2026-11-21). Wanderer's protocol (20250506) has no relative move, and the firmware
    # ignores a repeat of the move it made last: tried on the panel 2026-11-21, 1000 while closed (closed angle just
    # changed 15 -> 20) and 1001 while open (295 -> 290) each answered "done" after 1.3 s without moving the cap. It
    # does act on the other direction, and takes any angle for either end: with the cap open at 295, closed angle 290
    # (39000) then 1000 moved it to 290 in 2 s. So a jog borrows the other end:
    #   1. store the target as the OTHER end's angle      2. that end's move: the cap goes to the target
    #   3. give the other end its angle back               4. store the target as this end's angle
    #   5. this end's move: a no-op (~1.3 s), after which the firmware counts the cap as at this end again
    # Run on the panel 2026-11-21 (15->20->15 closed, 290->295 open): each jog took 4.3-4.8 s and ended within 1 deg
    # of the target. Which of the two moves does the work varies: a move toward the other end's side sometimes went
    # only part way (from open 290, the borrowed close to 295 reached 291), and the last move, no longer a repeat,
    # finished it. The borrowed angle is in place for about 1.5 s only: the panel seems to save the closed angle in one byte (a
    # restart with 290 stored read back 34 = 290 - 256), so a restart in that window would leave a wrong closed angle.
    JOG_MAX_DEG = 20.0
    JOG_CONFIRM_S = 2.0     # the angle reply came within 0.3 s on the panel, and status lines come every 0.33 s
    MOVE_CMD = {"open": 1001, "close": 1000}

    def jog(self, degrees, background=True):
        """Move the cap by `degrees` from the end it rests at (positive opens further, negative closes further) and
        store where it ends as that end's angle. Returns (which, new_angle) at once; the cover reads MOVING until the
        sequence ends. Refused while moving or between the ends (CoverBusy), or out of range (ValueError)."""
        d = float(degrees)
        if not (0 < abs(d) <= self.JOG_MAX_DEG):
            raise ValueError(f"Jog step must be up to {self.JOG_MAX_DEG:g}° either way")
        with self._lock:
            if self._moving():
                raise CoverBusy(MOVE_BUSY_MSG)
            st = self._status
            state = self._cover_state(st) if st else CoverState.UNKNOWN
            if state not in (CoverState.OPEN, CoverState.CLOSED):
                raise CoverBusy("Jog starts from the open or closed position: open or close the cover first")
            which = "open" if state == CoverState.OPEN else "close"
            other = "close" if which == "open" else "open"
            new = round(st[which + "_set"] + d, 2)
            lo, hi = self.ANGLE_RANGES[which]
            if not lo <= new <= hi:
                raise ValueError(f"The {'closed' if which == 'close' else 'open'} angle must be {lo:g} to {hi:g} "
                                 f"degrees; a jog would make it {new:g}")
            if self._ser is None:
                raise RuntimeError(f"Flat panel link not open ({self.last_error or 'not started'})")
            self._jog = (which, new)
            self._on_at_zero = False
        args = (which, other, new, st[other + "_set"])
        if background:
            threading.Thread(target=self._jog_run, args=args, name="cover-jog", daemon=True).start()
        else:
            self._jog_run(*args)
        return which, new

    def _jog_run(self, which, other, new, keep):
        try:
            self._jog_angle(other, new)
            self._jog_move(other)
            self._jog_angle(other, keep)       # at once: the borrowed angle mustn't outlive a restart
            self._jog_angle(which, new)
            self._jog_move(which)
            log.info("cover: jogged; %s angle now %g", which, new)
        except Exception as e:
            log.warning("cover: jog failed: %s", e)
            self.last_error = f"jog failed: {e}"
            try:
                if self.fields().get(other + "_set") != keep:
                    self._jog_angle(other, keep)
            except Exception as e2:
                self.last_error += f"; the {other} angle may still be {new:g}° instead of {keep:g}° ({e2})"
        finally:
            with self._lock:
                self._jog = None

    def _jog_angle(self, which, deg):
        with self._lock:
            self._send(self.ANGLE_BASE[which] + int(round(deg * 100)))
        deadline = self._clock() + self.JOG_CONFIRM_S
        while self.fields().get(which + "_set") != deg:
            if self._clock() >= deadline:
                raise RuntimeError(f"the panel didn't show the {which} angle {deg:g}°")
            self._sleep(0.05)

    def _jog_move(self, which):
        with self._lock:
            self._send(self.MOVE_CMD[which])
            self._move = CoverState.OPEN if which == "open" else CoverState.CLOSED
            self._move_t, self._done_t = self._clock(), None
        while True:
            with self._lock:
                self._moving()                 # applies the move timeout
                if self._failed_move:
                    raise RuntimeError("the panel didn't report the end of the move")
                if self._move is None:
                    return
            self._sleep(0.05)


class CoverDevice:
    """Thin ASCOM-facing wrapper the Alpaca handler calls. Validation lives here. It raises ValueError for a
    bad brightness, NotImplementedError when the link has no halt, and CoverBusy while the panel is moving.
    The handler maps those to ASCOM error numbers."""

    def __init__(self, link):
        self.link = link

    def start(self):
        self.link.start()

    def stop(self):
        self.link.stop()

    def up(self):
        return self.link.up()

    @property
    def max_brightness(self):
        return self.link.MAX_BRIGHTNESS

    def snapshot(self):
        return self.link.snapshot()

    def open(self):
        self.link.open()

    def close(self):
        self.link.close()

    def halt(self):
        self.link.halt()

    def calibrator_on(self, brightness):
        b = int(brightness)
        if not 0 <= b <= self.link.MAX_BRIGHTNESS:
            raise ValueError(f"Brightness 0 to {self.link.MAX_BRIGHTNESS}")
        self.link.calibrator_on(b)

    def calibrator_off(self):
        self.link.calibrator_off()
