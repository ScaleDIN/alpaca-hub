"""Dew heater control on the PC, since the stock firmware only knows a raw PWM value.

Modes, as in WandererEmpire:
  0 off
  1 manual               fixed power
  2 constant temperature holds the lens probe at a set temperature
  3 dew point offset     holds the lens probe a set number of degrees above the dew point
  4 DC3 (12 V output)    not a heater: the DC3 jack as a plain switched 12 V output, fully on or off (dc3_on).
                         No power limit, no probes; stopping the service leaves it as it is.

Modes 2 and 3 use a PI controller on the lens probe temperature. Every command the stock firmware receives
is written to its EEPROM, so the controller sends a new PWM value only when it differs enough from what the
box has now, and not more often than min_interval_s (default 60 s, about 600 writes in a 10-hour night).

If a sensor the mode needs is missing (lens probe, or the DHT22 in mode 3), the heater runs at a fixed
fallback power until it comes back.
"""
from dataclasses import dataclass

from . import protocol as P

OFF, MANUAL, CONSTANT, DEWPOINT, DC3OUT = 0, 1, 2, 3, 4
MODE_NAMES = {OFF: "off", MANUAL: "manual", CONSTANT: "constant temperature", DEWPOINT: "dew point offset",
              DC3OUT: "DC3 (12 V output)"}
FIXED_MODES = (OFF, MANUAL, DC3OUT)   # no regulation: the PWM follows the settings


@dataclass
class DewSettings:
    mode: int = OFF
    manual_pct: float = 50.0
    target_c: float = 5.0          # mode 2
    offset_c: float = 5.0          # mode 3: lens target = dew point + offset
    max_pct: float = 100.0         # ceiling for every mode
    fallback_pct: float = 40.0     # modes 2/3 when a needed sensor is missing
    kp: float = 20.0               # % per C of error
    ki: float = 0.05               # % per C per second
    min_step_pct: float = 5.0      # smallest change worth an EEPROM write
    min_interval_s: float = 60.0   # at most one automatic write per interval
    period_s: float = 10.0         # control loop period
    dc3_on: bool = True            # mode 4: the 12 V output on (PWM 255) or off


class DewController:
    def __init__(self, settings, clock):
        self.s = settings
        self._clock = clock
        self._integral = 0.0
        self._last_run = None
        self._last_auto_send = None
        self.desired_pwm = None
        self.reason = "idle"
        self.target = None

    def reset(self):
        self._integral = 0.0
        self._last_run = None
        self._last_auto_send = None

    def retune(self):
        """Tuning changed: start the PI controller afresh at the next status line. The command interval keeps
        running, so saving the tuning never causes an extra EEPROM write."""
        self._integral = 0.0
        self._last_run = None

    def _cap(self):
        return P.pct_to_pwm(self.s.max_pct)

    def fixed_pwm(self):
        """PWM for the modes that don't regulate (off, manual)."""
        if self.s.mode == OFF:
            return 0
        if self.s.mode == DC3OUT:
            return 255 if self.s.dc3_on else 0   # a plain output: the heater's power limit doesn't apply
        return min(P.pct_to_pwm(self.s.manual_pct), self._cap())

    def step(self, st):
        """Called on every status line. Returns a PWM value to send now, or None."""
        now = self._clock()
        mode = self.s.mode
        if mode in FIXED_MODES:
            self.target = None
            self.reason = ("on" if self.s.dc3_on else "off") if mode == DC3OUT else MODE_NAMES[mode]
            self.desired_pwm = self.fixed_pwm()
            # Keep the box in line if its PWM was changed elsewhere (e.g. it restarted with an older EEPROM value).
            if st.dew_pwm != self.desired_pwm and self._may_auto_send(now):
                self._last_auto_send = now
                return self.desired_pwm
            return None

        if self._last_run is not None and now - self._last_run < self.s.period_s:
            return None
        dt = 0.0 if self._last_run is None else now - self._last_run
        self._last_run = now

        target = self.s.target_c if mode == CONSTANT else (None if st.dewpoint is None else st.dewpoint + self.s.offset_c)
        self.target = target
        cap_pct = max(0.0, min(100.0, self.s.max_pct))
        if st.lens is None or target is None:
            self.reason = "lens probe missing: fallback power" if st.lens is None else "ambient sensor missing: fallback power"
            self._integral = 0.0
            out = min(self.s.fallback_pct, cap_pct)
        else:
            err = target - st.lens
            p = self.s.kp * err
            out_unclamped = p + self._integral
            # Anti-windup: integrate only when it wouldn't push further into saturation.
            if not ((out_unclamped >= cap_pct and err > 0) or (out_unclamped <= 0 and err < 0)):
                self._integral += self.s.ki * err * dt
                self._integral = max(0.0, min(cap_pct, self._integral))
            out = max(0.0, min(cap_pct, p + self._integral))
            self.reason = f"regulating to {target:.1f} C"
        want = P.pct_to_pwm(out)
        self.desired_pwm = want

        have = st.dew_pwm
        if want == have:
            return None
        step = P.pct_to_pwm(self.s.min_step_pct)
        meaningful = abs(want - have) >= step or want == 0 or want == P.pct_to_pwm(cap_pct)
        if meaningful and self._may_auto_send(now):
            self._last_auto_send = now
            return want
        return None

    def _may_auto_send(self, now):
        return self._last_auto_send is None or now - self._last_auto_send >= self.s.min_interval_s

    def mark_user_send(self):
        """A user changed mode/power: the next automatic write waits a full interval."""
        self._last_auto_send = self._clock()
