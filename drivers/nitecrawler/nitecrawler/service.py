"""Ties the serial link and the settings together: focuser and rotator behaviour, independent of HTTP."""
import logging
import threading
import time

from hub.devserver import (ACTION_NOT_IMPLEMENTED, DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE,  # noqa: F401
                           NOT_CONNECTED, NOT_IMPLEMENTED, VALUE_NOT_SET, AlpacaError)
from hub.devserver import History as _HubHistory

from . import protocol as P
from .link import Busy, NCLink, NoAnswer

log = logging.getLogger("nitecrawler.service")

DIM_ANSWER_S = 0.4    # PW#: the patched firmware answers at once; stock 1.6 never does

CHART_KEYS = ("temp", "volt", "focus", "rot", "aux")


def wrap360(d):
    d = d % 360.0
    return 0.0 if abs(d - 360.0) < 1e-9 else d


def signed180(d):
    """Degrees to (-180, 180]."""
    d = wrap360(d)
    return d - 360.0 if d > 180.0 else d


def History():
    """The chart's history: the hub's (1 s for 10 minutes, 10-second averages for 24 hours, in memory). This driver
    had its own copy with 1-minute averages until 1.0.1."""
    return _HubHistory(CHART_KEYS)


class Service:
    ACTIONS = ("FindHome", "Encoders")

    def __init__(self, settings, opener=None, clock=time.monotonic):
        self.settings = settings
        kw = {"opener": opener} if opener else {}
        self.link = NCLink(settings.port, clock=clock, preferred=settings.last_port or None,
                           on_port_found=self._port_found, on_status=self._status, **kw)
        self.history = History()
        self.targets = {P.FOCUS: None, P.ROTATOR: None, P.AUX: None}   # rotator: sky degrees
        self._bl_gen = {P.FOCUS: 0, P.AUX: 0}      # backlash: bumped to cancel a pending return leg
        self._bl_busy = {P.FOCUS: False, P.AUX: False}
        self._last_sample = 0
        self.dim_after = None        # PW (patched firmware): seconds before the display dims; None = not known
        self._dim_read_for = None    # link.stats["opens"] the dim delay was read (or tried) for

    def start(self):
        self.link.start()

    def stop(self):
        self.link.stop()

    def _save(self):
        try:
            self.settings.save()
        except OSError as e:
            log.warning("cannot save settings: %s", e)

    def _port_found(self, port):
        if self.link.auto() and self.settings.last_port != port:
            self.settings.last_port = port
            self._save()

    def _status(self, st):
        now = time.time()
        if int(now) == self._last_sample:
            return
        self._last_sample = int(now)
        self.history.add(now, {"temp": st.temperature, "volt": st.voltage, "focus": st.focus.position,
                               "rot": round(self.rot_position(st), 2), "aux": st.aux.position})

    # ---------------------------------------------------------------- plumbing
    def status_or_raise(self):
        st = self.link.status
        if st is None or not self.link.up():
            raise AlpacaError(DRIVER_ERROR, "NiteCrawler not responding" +
                              (f" ({self.link.last_error})" if self.link.last_error else ""))
        return st

    def _do(self, fn, *a):
        try:
            return fn(*a)
        except Busy as e:
            raise AlpacaError(INVALID_OPERATION, str(e))
        except (NoAnswer, ConnectionError, OSError) as e:
            raise AlpacaError(DRIVER_ERROR, f"NiteCrawler: {e}")
        except P.ProtocolError as e:
            raise AlpacaError(INVALID_VALUE, str(e))

    def set_port(self, port):
        port = (port or "auto").strip() or "auto"
        self.settings.port = port
        self._save()
        self.link.set_port(port)

    # ---------------------------------------------------------------- focusers (focus = #0, aux = #1)
    def focuser_axis(self, n):
        if n == 0:
            return P.FOCUS
        if n == 1 and self.settings.aux_enabled:
            return P.AUX
        return None

    def max_step(self, axis):
        return int(self.settings.focus_max if axis == P.FOCUS else self.settings.aux_max)

    def position(self, axis):
        return self.status_or_raise().axis(axis).position

    def is_moving(self, axis):
        st = self.status_or_raise()
        return st.axis(axis).moving or st.homing or self._bl_busy.get(axis, False)

    def backlash(self, axis):
        return max(0, int(self.settings.focus_backlash if axis == P.FOCUS else self.settings.aux_backlash))

    def focuser_move(self, axis, pos):
        pos = int(pos)
        if not 0 <= pos <= self.max_step(axis):
            raise AlpacaError(INVALID_VALUE, f"Position must be 0 to {self.max_step(axis)}")
        st = self.status_or_raise()
        self._bl_gen[axis] += 1   # a new move replaces any unfinished backlash leg
        self._bl_busy[axis] = False
        bl, cur = self.backlash(axis), st.axis(axis).position
        first = max(0, pos - bl)
        if bl and cur is not None and pos < cur and first != pos:
            # Backlash: finish every move going outward. Overshoot inward first, then come back to pos.
            self._do(self.link.goto, axis, first)
            self._bl_busy[axis] = True
            threading.Thread(target=self._backlash_leg, args=(axis, pos, self._bl_gen[axis]),
                             name="backlash", daemon=True).start()
            log.info("%s -> %d (via %d, backlash %d)", P.AXIS_NAMES[axis], pos, first, bl)
        else:
            self._do(self.link.goto, axis, pos)
            log.info("%s -> %d", P.AXIS_NAMES[axis], pos)
        self.targets[axis] = pos

    def _backlash_leg(self, axis, pos, gen):
        try:
            while self._bl_gen[axis] == gen:
                st = self.link.status
                if st is not None and not st.axis(axis).moving and not st.homing:
                    break
                time.sleep(0.1)
            if self._bl_gen[axis] == gen:
                self.link.goto(axis, pos)
        except Exception as e:
            log.warning("%s: backlash return move failed: %s", P.AXIS_NAMES[axis], e)
        finally:
            if self._bl_gen[axis] == gen:
                self._bl_busy[axis] = False

    def _cancel_backlash(self, axis):
        self._bl_gen[axis] += 1
        self._bl_busy[axis] = False

    def halt(self, axis):
        if axis in self._bl_gen:
            self._cancel_backlash(axis)
        self._do(self.link.halt, axis)
        log.info("%s halted", P.AXIS_NAMES[axis])

    def temperature(self):
        t = self.status_or_raise().temperature
        if t is None:
            raise AlpacaError(VALUE_NOT_SET, "No temperature reading yet")
        return t

    # ---------------------------------------------------------------- rotator
    @property
    def steps_per_rev(self):
        return self.link.steps_per_rev

    def mech_from_steps(self, steps):
        d = steps * 360.0 / self.steps_per_rev
        return wrap360(-d if self.settings.rot_reverse else d)

    def steps_from_mech(self, deg):
        """Mechanical degrees -> rotator steps within half a turn either side of home (cable-safe, as INDI)."""
        d = signed180(deg)
        if self.settings.rot_reverse:
            d = -d
        return int(round(d * self.steps_per_rev / 360.0))

    def rot_mechanical(self, st=None):
        st = st or self.status_or_raise()
        return self.mech_from_steps(st.rotator.position)

    def rot_position(self, st=None):
        return wrap360(self.rot_mechanical(st) + self.settings.rot_offset)

    def rot_target(self):
        t = self.targets[P.ROTATOR]
        return self.rot_position() if t is None else t

    def _rot_unwind(self, st):
        """A position more than a turn from home (seen after many relative moves): sync it back into range
        first, so the next move doesn't unwind several turns."""
        spr = self.steps_per_rev
        p = st.rotator.position
        if -spr / 2 <= p <= spr / 2:
            return
        q = int(round(signed180(p * 360.0 / spr) * spr / 360.0))
        log.info("rotator at %d steps (beyond half a turn); syncing to %d", p, q)
        self._do(self.link.sync, P.ROTATOR, q)

    def rot_move_mechanical(self, deg):
        deg = float(deg)
        if not 0.0 <= deg < 360.0:
            raise AlpacaError(INVALID_VALUE, "Position must be 0 to less than 360")
        st = self.status_or_raise()
        self._rot_unwind(st)
        self._do(self.link.goto, P.ROTATOR, self.steps_from_mech(deg))
        self.targets[P.ROTATOR] = wrap360(deg + self.settings.rot_offset)
        log.info("rotator -> %.3f mechanical", deg)

    def rot_set_steps(self, pos):
        """Moonlite's rotation "SET POS": tells the NiteCrawler its rotator count is pos (2SP pos#). The motor
        doesn't move. Home (0) moves with it, so the sky angle is kept by adjusting the PC-side offset."""
        pos = int(pos)
        half = self.steps_per_rev // 2
        if not -half <= pos <= half:
            raise AlpacaError(INVALID_VALUE, f"Rotator position must be within half a turn of home (±{half} steps)")
        sky = self.rot_position()
        self._do(self.link.sync, P.ROTATOR, pos)
        self.settings.rot_offset = round(signed180(sky - self.mech_from_steps(pos)), 6)
        self._save()
        log.info("rotator count set to %d steps (sky angle kept at %.3f)", pos, sky)

    def rot_move_absolute(self, sky):
        sky = float(sky)
        if not 0.0 <= sky < 360.0:
            raise AlpacaError(INVALID_VALUE, "Position must be 0 to less than 360")
        self.rot_move_mechanical(wrap360(sky - self.settings.rot_offset))
        self.targets[P.ROTATOR] = sky

    def rot_move_relative(self, delta):
        delta = float(delta)
        if not -360.0 < delta < 360.0:
            raise AlpacaError(INVALID_VALUE, "Move must be between -360 and 360")
        self.rot_move_absolute(wrap360(self.rot_position() + delta))

    def rot_sync(self, sky):
        sky = float(sky)
        if not 0.0 <= sky < 360.0:
            raise AlpacaError(INVALID_VALUE, "Position must be 0 to less than 360")
        mech = self.rot_mechanical()
        self.settings.rot_offset = round(signed180(sky - mech), 6)
        self.targets[P.ROTATOR] = sky
        self._save()
        log.info("rotator synced: sky %.3f = mechanical %.3f (offset %.3f)", sky, mech, self.settings.rot_offset)

    def set_reverse(self, on):
        on = bool(on)
        if on != self.settings.rot_reverse:
            # keep the sky angle where it is: the mechanical angle flips, so does the offset
            sky = self.rot_position() if self.link.up() else None
            self.settings.rot_reverse = on
            if sky is not None:
                self.settings.rot_offset = round(signed180(sky - self.rot_mechanical()), 6)
            self._save()

    # ---------------------------------------------------------------- the rest
    def find_home(self, mask):
        self.status_or_raise()
        self._do(self.link.find_home, mask)
        for axis, bit in ((P.FOCUS, P.HOME_FOCUS), (P.ROTATOR, P.HOME_ROTATOR), (P.AUX, P.HOME_AUX)):
            if mask & bit:
                self.targets[axis] = None
                if axis in self._bl_gen:
                    self._cancel_backlash(axis)

    def set_encoders(self, on):
        self._do(self.link.call, P.set_encoders(on))

    def set_brightness(self, display=None, sleep=None):
        if display is not None:
            self._do(self.link.call, P.set_brightness(display))
            self.settings.brightness = int(display)
        if sleep is not None:
            self._do(self.link.call, P.set_sleep_brightness(sleep))
            self.settings.sleep_brightness = int(sleep)
        self._save()

    def read_dim_after(self):
        """Reads the dim delay (PW#) once per connection, off the caller's thread, so the page's poll doesn't wait.
        Stock firmware never answers PW: that costs one short wait on the link thread, and dim_after stays None."""
        opens = self.link.stats["opens"]
        if self._dim_read_for == opens or not self.link.up() or self.link.homing():
            return
        self._dim_read_for = opens
        self.dim_after = None

        def run():
            try:
                self.dim_after = P.parse_int(self.link.call(P.get_dim_delay(), answer_s=DIM_ANSWER_S, quiet=True))
            except Exception as e:
                log.info("no dim delay from the NiteCrawler (%s): stock firmware?", e)
        threading.Thread(target=run, name="dim-delay", daemon=True).start()

    def set_dim_after(self, seconds):
        try:
            self._do(lambda: self.link.call(P.set_dim_delay(seconds), answer_s=DIM_ANSWER_S, quiet=True))
        except AlpacaError as e:
            if e.number == DRIVER_ERROR and "no answer" in e.message:
                raise AlpacaError(DRIVER_ERROR, "The NiteCrawler didn't answer PW (dim delay): "
                                                "it needs the patched firmware")
            raise
        self.dim_after = int(seconds)

    def set_step_delay(self, axis, delay):
        self._do(self.link.call, P.set_step_delay(axis, delay))
        st = self.link.status
        if st is not None:
            st.axis(axis).step_delay = int(delay)

    def action(self, name, params):
        n = (name or "").lower()
        if n == "findhome":
            try:
                mask = int(params or P.HOME_FOCUS | P.HOME_ROTATOR)
            except ValueError:
                raise AlpacaError(INVALID_VALUE, "FindHome: parameter is the axis mask 1-7 (1 focus, 2 rotator, 4 aux)")
            self.find_home(mask)
            return "started"
        if n == "encoders":
            on = (params or "").strip().lower() in ("1", "true", "on")
            self.set_encoders(on)
            return "on" if on else "off"
        raise AlpacaError(ACTION_NOT_IMPLEMENTED, f"Unknown action {name!r}")

    def status_dict(self):
        lk, st = self.link, self.link.status
        d = {"up": lk.up(), "port": lk.port, "port_setting": self.settings.port, "error": lk.last_error,
             "firmware": lk.firmware, "model": lk.model, "steps_per_rev": self.steps_per_rev,
             "homing": lk.homing(), "stats": dict(lk.stats), "settings": {
                 k: getattr(self.settings, k) for k in ("focus_max", "aux_enabled", "aux_max", "focus_backlash", "aux_backlash", "rot_reverse",
                                                        "rot_offset", "brightness", "sleep_brightness")}}
        self.read_dim_after()
        d["settings"]["dim_after"] = self.dim_after
        if st is not None:
            d.update({"temperature": st.temperature, "voltage": st.voltage,
                      "limits": {"rot_home": st.limits.rot_home, "out": st.limits.out_limit,
                                 "in": st.limits.in_limit}})
            for axis in P.AXES:
                ax = st.axis(axis)
                d[P.AXIS_NAMES[axis]] = {"position": ax.position, "moving": ax.moving, "step_delay": ax.step_delay,
                                         "target": self.targets[axis]}
            d["rotator"].update({"sky": round(self.rot_position(st), 3), "mechanical": round(self.rot_mechanical(st), 3)})
        return d
