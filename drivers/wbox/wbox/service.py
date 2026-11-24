"""Ties the serial link, the dew controller and the settings together, and defines the Alpaca switches."""
import logging
import os
import threading
import time
import dataclasses
from dataclasses import dataclass

from hub.devserver import (ACTION_NOT_IMPLEMENTED, DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE,  # noqa: F401
                           NOT_CONNECTED, NOT_IMPLEMENTED, VALUE_NOT_SET, AlpacaError)

from . import VERSION
from . import dew as D
from . import protocol as P
from .history import History, clamp_days
from .link import BoxLink
from .settings import DEFAULT_GAUGES  # noqa: F401  (the default gauge list lives with the settings)

log = logging.getLogger("wbox.service")

OK = 0


@dataclass
class SwitchDef:
    name: str
    desc: str
    min: float
    max: float
    step: float
    writable: bool


# Writable switches: ids 0-7, always present, so sequences that set them keep working.
SW_USB, SW_DC2, SW_DC2V, SW_DC46, SW_DEW_MODE, SW_DEW_MANUAL, SW_DEW_TARGET, SW_DEW_OFFSET = range(8)
SW_FIXED = 8

# Read-only gauges follow from id 8, in this order, but only the ones chosen on the status page.
# key: (name, description, min, max, step)
GAUGES = {
    "dew_power": ("Dew heater power", "Heater power right now, percent (read-only)", 0, 100, 0.1),
    "voltage": ("Input voltage", "Supply voltage, volts (read-only)", 0, 20, 0.01),
    "current": ("Input current", "Current from the 12 V input, outputs plus the box itself, amps (read-only)", 0, 30, 0.01),
    "power": ("Input power", "Power from the 12 V input (input voltage x current), watts (read-only)", 0, 600, 0.1),
    "lens": ("Lens temperature", "Dew heater probe temperature, C (read-only)", -55, 125, 0.01),
    "ambient": ("Ambient temperature", "DHT22 air temperature, C (read-only)", -40, 80, 0.1),
    "humidity": ("Humidity", "DHT22 relative humidity, percent (read-only)", 0, 100, 0.1),
    "dewpoint": ("Dew point", "Dew point from the DHT22, C (read-only)", -60, 80, 0.01),
    "dew_margin": ("Lens above dew point", "Lens temperature minus dew point, C (read-only)", -100, 150, 0.01),
}

# Dew controller settings the status page can change: key -> (min, max). Every mode reads them live.
TUNING = {
    "max_pct": (0, 100),          # ceiling for every mode
    "fallback_pct": (0, 100),     # modes 2/3 while a sensor they need is missing
    "kp": (0, 500),               # % per C
    "ki": (0, 5),                 # % per C per second
    "min_step_pct": (1, 50),      # smallest automatic change sent
    "min_interval_s": (10, 3600),  # at most one automatic command (= EEPROM write) per interval
    "period_s": (1, 600),         # control loop period
}

# Probes each gauge reads; a gauge whose probe is set to "not used" is left out of the switch list.
GAUGE_NEEDS = {"lens": ("lens",), "ambient": ("ambient",), "humidity": ("ambient",), "dewpoint": ("ambient",),
               "dew_margin": ("lens", "ambient")}
# What a gauge reads while its probe is missing. NINA keeps a switch's old value when a read fails, and a failed
# read while it connects aborts the whole connection, so a missing probe answers with a value and ": MISSING" in the
# gauge's name instead of ASCOM's ValueNotSet.
MISSING_MODES = {"hold": "the last good value", "minimum": "the gauge's minimum", "value": "a fixed value"}
MISSING_SUFFIX = ": MISSING"

# Probes each dew mode needs.
MODE_NEEDS = {D.CONSTANT: ("lens",), D.DEWPOINT: ("lens", "ambient")}
PROBE_NAMES = {"lens": "the lens probe", "ambient": "the DHT22"}

# Ids with the default gauge selection (the same as in v0.1.0 for 8-12).
SW_DEW_POWER, SW_VIN, SW_IIN, SW_POWER, SW_LENS, SW_AMBIENT, SW_HUMIDITY, SW_DEWPOINT = range(8, 16)


def clean_gauges(keys):
    """Known keys only, without duplicates, in the fixed order."""
    want = set(keys or ())
    return [k for k in GAUGES if k in want]


CONFIRM_TIMEOUT = 12.0   # room for one resend: 4 status lines, rewrite, 4 more (about 1 s each)


class Service:
    def __init__(self, settings, opener=None, clock=time.monotonic, history_dir=None, cover=None):
        self.settings = settings
        self.cover = cover   # optional CoverDevice (flat panel), served as Alpaca CoverCalibrator #0; None = none
        self.history = History(history_dir or os.path.join(os.path.dirname(os.path.abspath(settings.path)), "history"),
                               clamp_days(settings.history_days))
        self._clock = clock
        self.dew = D.DewController(settings.dew, clock)
        kw = {} if opener is None else {"opener": opener}
        self.link = BoxLink(settings.port, on_status=self._on_status, on_write=self._on_write, clock=clock,
                            preferred=settings.last_port or None, on_port_found=self._on_port_found, **kw)
        self._wlock = threading.Lock()
        # WSM box settings read from the box itself (config keys, PROTOCOL.md 5.4): fw they belong to, key -> value.
        self._wsm_fw = None
        self._wsm_cfg = {}
        self._wsm_asked = set()
        self._wsm_x1 = {}            # latest X1 values: up, rst, flt, act, shed, gate
        self._wsm_plain = 0               # stock-length lines in a row from a WSM box
        self._wsm_gone_up = None          # uptime before streaming stopped (a box restart), until it's back
        self._wsm_streaming = False       # the box acked 40004 and hasn't restarted since
        self._wsm_restart = None     # last restart seen: when, cause, uptime before it
        self.started = time.time()
        self._last_good = {}   # gauge key -> last value read while its probe was there

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        try:
            self.history.purge()          # the chart starts empty (history.py); old day files go
        except Exception:
            log.exception("cannot tidy the history files")
        self.link.start()
        if self.cover is not None:
            self.cover.start()

    def stop(self):
        if self.settings.dew.mode in (D.CONSTANT, D.DEWPOINT) and self.settings.dew_off_on_exit and self.link.up():
            log.info("automatic dew mode: switching the heater off before exiting")
            self.link.send(P.cmd_dew_pwm(0), confirm=lambda st: st.dew_pwm == 0, wait=True, timeout=4)
        self.link.stop()
        if self.cover is not None:
            self.cover.stop()
        self.history.flush()

    # ---------------------------------------------------------------- probes in use
    def probe_used(self, probe):
        return self.settings.use_lens if probe == "lens" else self.settings.use_ambient

    def view(self, st):
        """The status line as the rest of the service sees it: a probe set to "not used" reads as absent."""
        if st is None or (self.settings.use_lens and self.settings.use_ambient):
            return st
        kw = {}
        if not self.settings.use_lens:
            kw["lens"] = None
        if not self.settings.use_ambient:
            kw["ambient"] = kw["humidity"] = None
        return dataclasses.replace(st, **kw)

    def set_probes(self, lens, ambient):
        """Which probes are used, from the status page. Refused while the dew mode needs one being switched off:
        changing the mode is a separate, deliberate step."""
        want = {"lens": bool(lens), "ambient": bool(ambient)}
        with self._wlock:
            need = [p for p in MODE_NEEDS.get(self.settings.dew.mode, ()) if not want[p]]
            if need:
                raise AlpacaError(INVALID_OPERATION, f"{D.MODE_NAMES[self.settings.dew.mode].capitalize()} mode needs "
                                  f"{PROBE_NAMES[need[0]]}. Set the heater to manual or off first.")
            changed = {k: v for k, v in want.items() if self.probe_used(k) != v}
            if not changed:
                return changed
            self.settings.use_lens, self.settings.use_ambient = want["lens"], want["ambient"]
            self.dew.reset()
            self._save()
            log.info("probes: lens probe %s, DHT22 %s", *("used" if want[k] else "not used" for k in ("lens", "ambient")))
        return changed

    # ---------------------------------------------------------------- link callbacks (link thread)
    def _on_status(self, st):
        self._wsm_poll(st)
        st = self.view(st)
        for k in GAUGES:
            v = self.gauge_value(st, k)
            if v is not None:
                self._last_good[k] = v
        if not self.settings.dew_mode_adopted:
            # First run: take over whatever the box is doing now instead of switching the heater off.
            d = self.settings.dew
            if st.dew_pwm > 0:
                d.mode, d.manual_pct = D.MANUAL, P.pwm_to_pct(st.dew_pwm)
            else:
                d.mode = D.OFF
            self.settings.dew_mode_adopted = True
            self._save()
            log.info("adopted the box's dew state: %s, %d/255", D.MODE_NAMES[d.mode], st.dew_pwm)
        pwm = self.dew.step(st)
        if pwm is not None:
            log.info("dew: %s -> PWM %d (was %d)", self.dew.reason, pwm, st.dew_pwm)
            self.link.send(P.cmd_dew_pwm(pwm))
        try:
            self.history.add(self.sample(st))
        except Exception:
            log.exception("history sample failed")

    def sample(self, st):
        """One chart sample from a status line (history.KEYS). Missing sensors stay None."""
        g = self.gauge_value
        tgt = self.dew.target
        return {"vin": st.voltage, "iin": st.current, "pwr": g(st, "power"), "lens": st.lens,
                "amb": g(st, "ambient"), "hum": g(st, "humidity"), "dp": g(st, "dewpoint"),
                "dew": g(st, "dew_power"), "dc2": st.dc2_volts if st.dc2 else 0.0,
                "tgt": None if tgt is None else round(tgt, 2)}

    def set_history_days(self, days):
        """Retention from the status page."""
        self.settings.history_days = clamp_days(days)
        self.history.set_days(self.settings.history_days)
        self._save()
        log.info("history kept for %d days", self.settings.history_days)

    def _on_port_found(self, port):
        if self.settings.last_port != port:
            self.settings.last_port = port
            self._save()

    def set_port(self, port):
        """'auto' or a COM port name, from the status page. Saved, and the link reopens."""
        port = (port or "").strip()
        if not port or len(port) > 64 or any(ord(c) < 32 for c in port):
            raise AlpacaError(INVALID_VALUE, "Invalid port")
        self.settings.port = port
        self._save()
        self.link.set_port(port)
        log.info("serial port setting -> %s", port)

    def _on_write(self, cmd):
        # Every stock command is an EEPROM write. WSM's own commands aren't, except a config write (saved 5 s later).
        if cmd < 40000 or cmd == P.CAL_COMMAND or 700000000 <= cmd <= 799999999:
            self.settings.eeprom_writes += 1
        self._save()

    def _save(self):
        try:
            self.settings.save()
        except OSError as e:
            log.warning("cannot save settings: %s", e)

    # ---------------------------------------------------------------- switches
    def gauges(self):
        """The gauges Alpaca clients see: the chosen ones whose probes are used."""
        return [k for k in clean_gauges(self.settings.gauges) if all(self.probe_used(p) for p in GAUGE_NEEDS.get(k, ()))]

    def set_gauges(self, keys):
        """Gauge selection from the status page. Clients see the new switch list when they reconnect."""
        unknown = [k for k in keys if k not in GAUGES]
        if unknown:
            raise AlpacaError(INVALID_VALUE, f"Unknown gauge {unknown[0]!r}")
        self.settings.gauges = clean_gauges(keys)
        self._save()
        log.info("gauges -> %s", ", ".join(self.settings.gauges) or "none")

    def switch_count(self):
        return SW_FIXED + len(self.gauges())

    def set_dew_tuning(self, values, reset=False):
        """Dew controller tuning from the status page; applies at once, no restart. reset: back to the defaults
        first, then apply values on top."""
        d = self.settings.dew
        new = {k: getattr(D.DewSettings(), k) for k in TUNING} if reset else {}
        for k, v in values.items():
            if k not in TUNING:
                raise AlpacaError(INVALID_VALUE, f"Unknown setting {k!r}")
            lo, hi = TUNING[k]
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise AlpacaError(INVALID_VALUE, f"{k}: not a number")
            if not (lo <= v <= hi) or v != v:
                raise AlpacaError(INVALID_VALUE, f"{k}: {lo} to {hi}")
            new[k] = v
        with self._wlock:
            changed = {k: v for k, v in new.items() if getattr(d, k) != v}
            if not changed:
                return changed
            for k, v in changed.items():
                setattr(d, k, v)
            cap = round(max(0.0, min(100.0, d.max_pct)))
            if d.manual_pct > cap:
                # the manual power switch's maximum is the limit: keep its value inside it, so a client never reads a
                # value above MaxSwitchValue, and raising the limit later doesn't bring back full power by surprise
                d.manual_pct = float(cap)
                changed["manual_pct"] = d.manual_pct
            self.dew.retune()
            self._save()
            log.info("dew tuning -> %s", ", ".join(f"{k} {v:g}" for k, v in changed.items()))
            if "max_pct" in changed and d.mode in (D.OFF, D.MANUAL):  # not DC3OUT: the limit doesn't apply there
                try:
                    self._apply_fixed_dew()  # a lower ceiling caps manual power right away
                except AlpacaError as e:
                    log.warning("dew tuning saved, heater not updated yet: %s", e.message)
        return changed

    def _gauge_key(self, i):
        g = self.gauges()
        if not SW_FIXED <= i < SW_FIXED + len(g):
            raise AlpacaError(INVALID_VALUE, "Switch Id out of range")
        return g[i - SW_FIXED]

    def switch_def(self, i):
        if i >= SW_FIXED:
            key = self._gauge_key(i)
            name, desc, lo, hi, step = GAUGES[key]
            if key == "dew_power" and self.settings.dew.mode == D.DC3OUT:
                name, desc = "DC3 output power", "DC3 as a plain 12 V output: 100 = on, 0 = off (read-only)"
            if self.gauge_missing(key):
                name += MISSING_SUFFIX
            return SwitchDef(name, desc, lo, hi, step, False)
        if i < 0:
            raise AlpacaError(INVALID_VALUE, "Switch Id out of range")
        d = self.settings.dew
        ceil = self.dc2_ceiling()
        defs = {
            SW_USB: SwitchDef("USB ports", "USB hub port power", 0, 1, 1, True),
            SW_DC2: SwitchDef("DC2 output", "Adjustable DC2 output on/off", 0, 1, 1, True),
            SW_DC2V: SwitchDef("DC2 voltage", "DC2 setpoint, volts. The stock firmware applies it in one step." if ceil is None
                               else f"DC2 setpoint, volts (at most {ceil:g} V: the DC2 ceiling stored in the box)",
                               P.DC2_MIN_V, P.DC2_MAX_V if ceil is None else max(P.DC2_MIN_V, ceil), 0.1, True),
            SW_DC46: SwitchDef("DC4-6 outputs", "12 V outputs DC4 to DC6", 0, 1, 1, True),
            SW_DEW_MODE: SwitchDef("Dew heater mode", "0 = off, 1 = manual, 2 = constant temperature, 3 = dew point "
                                   "offset, 4 = DC3 as a plain 12 V output (on/off with switch 5)", 0, 4, 1, True),
            SW_DEW_MANUAL: SwitchDef("DC3 output", "Mode 4: the DC3 jack as a plain 12 V output, 0 = off, 100 = on "
                                     "(any value above 0 switches it on)", 0, 100, 1, True) if d.mode == D.DC3OUT else
            SwitchDef("Dew manual power", "Heater power in manual mode, percent" + (
                "" if d.max_pct >= 100 else f" (at most {round(max(0.0, d.max_pct))} %: the power limit on the status page)"),
                      0, round(max(0.0, min(100.0, d.max_pct))), 1, True),
            SW_DEW_TARGET: SwitchDef("Dew target temperature", "Lens temperature held in constant-temperature mode, C",
                                     -20, 40, 0.5, True),
            SW_DEW_OFFSET: SwitchDef("Dew point offset", "Degrees above the dew point held in dew-point mode, C",
                                     0, 15, 0.5, True),
        }
        return defs[i]

    def gauge_missing(self, key):
        """True while the box is answering but this gauge's probe isn't."""
        st = self.link.status
        return st is not None and self.link.up() and self.gauge_value(self.view(st), key) is None

    def missing_reading(self, key):
        """What a gauge reads while its probe is missing (settings.missing_gauge)."""
        lo, hi = GAUGES[key][2], GAUGES[key][3]
        mode = self.settings.missing_gauge
        if mode == "minimum":
            return float(lo)
        if mode == "hold" and key in self._last_good:
            return self._last_good[key]
        return float(max(lo, min(hi, self.settings.missing_value)))

    def set_missing(self, mode, value=None):
        """From the Alpaca gauges card."""
        if mode not in MISSING_MODES:
            raise AlpacaError(INVALID_VALUE, f"Unknown choice {mode!r}")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise AlpacaError(INVALID_VALUE, "Fixed value: a number")
            if value != value or abs(value) > 1000:
                raise AlpacaError(INVALID_VALUE, "Fixed value: -1000 to 1000")
            self.settings.missing_value = value
        self.settings.missing_gauge = mode
        self._save()
        log.info("missing probe gauges -> %s (fixed value %g)", mode, self.settings.missing_value)

    def _status_or_raise(self):
        st = self.link.status
        if st is None or not self.link.up():
            raise AlpacaError(DRIVER_ERROR, "Box not responding on " + (self.link.port or "any serial port") +
                              (f" ({self.link.last_error})" if self.link.last_error else ""))
        return self.view(st)

    def switch_value(self, i):
        d = self.settings.dew
        if i == SW_DEW_MODE:
            return float(d.mode)
        if i == SW_DEW_MANUAL:
            return (100.0 if d.dc3_on else 0.0) if d.mode == D.DC3OUT else float(round(d.manual_pct))
        if i == SW_DEW_TARGET:
            return d.target_c
        if i == SW_DEW_OFFSET:
            return d.offset_c
        key = self._gauge_key(i) if i >= SW_FIXED else None
        st = self._status_or_raise()
        if key is None:
            v = {SW_USB: float(st.usb), SW_DC2: float(st.dc2), SW_DC2V: st.dc2_volts, SW_DC46: float(st.dc46)}[i]
        else:
            v = self.gauge_value(st, key)
        if v is None:
            if key is not None:
                return self.missing_reading(key)
            raise AlpacaError(VALUE_NOT_SET, "Sensor not available (missing or faulty)")
        return v

    @staticmethod
    def gauge_value(st, key):
        """A gauge's value from a status line, or None when its sensor is missing."""
        dp = st.dewpoint if st.ambient_ok else None
        if key == "dew_power":
            return P.pwm_to_pct(st.dew_pwm)
        if key == "voltage":
            return st.voltage
        if key == "current":
            return st.current
        if key == "power":
            return None if st.power is None else round(st.power, 1)
        if key == "lens":
            return st.lens
        if key == "ambient":
            return st.ambient if st.ambient_ok else None
        if key == "humidity":
            return st.humidity if st.ambient_ok else None
        if key == "dewpoint":
            return None if dp is None else round(dp, 2)
        if key == "dew_margin":
            return None if dp is None or st.lens is None else round(st.lens - dp, 2)
        raise KeyError(key)

    def write_switch(self, i, value):
        sd = self.switch_def(i)
        if not sd.writable:
            raise AlpacaError(NOT_IMPLEMENTED, "This switch is read-only")
        if i == SW_DEW_MANUAL and value > sd.max + 1e-9 and sd.max < 100 and self.settings.dew.mode != D.DC3OUT:
            raise AlpacaError(INVALID_VALUE, f"Dew manual power is limited to {sd.max:g} % by the dew heater's power limit "
                              "(status page, Dew controller card)")
        ceil = self.dc2_ceiling()
        if i == SW_DC2V and ceil is not None and value > ceil + 1e-9:
            raise AlpacaError(INVALID_VALUE, f"DC2 voltage is limited to {ceil:g} V by the DC2 ceiling stored in the box "
                              "(status page, Box and serial link card)")
        if not (sd.min - 1e-9 <= value <= sd.max + 1e-9):
            raise AlpacaError(INVALID_VALUE, f"Value out of range {sd.min}..{sd.max}")
        with self._wlock:
            if i in (SW_USB, SW_DC2, SW_DC46):
                on = value >= 0.5
                attr = {SW_USB: "usb", SW_DC2: "dc2", SW_DC46: "dc46"}[i]
                cmd = {SW_USB: P.cmd_usb, SW_DC2: P.cmd_dc2, SW_DC46: P.cmd_dc46}[i](on)
                st = self._status_or_raise()
                if getattr(st, attr) == on:
                    return
                self._send_confirmed(cmd, lambda s: getattr(s, attr) == on)
            elif i == SW_DC2V:
                v = round(value * 10) / 10.0
                st = self._status_or_raise()
                if abs(st.dc2_volts - v) < 0.05:
                    return
                self._send_confirmed(P.cmd_dc2_volts(v), lambda s: abs(s.dc2_volts - v) < 0.05)
            elif i == SW_DEW_MODE:
                mode = int(round(value))
                need = [p for p in MODE_NEEDS.get(mode, ()) if not self.probe_used(p)]
                if need:
                    raise AlpacaError(INVALID_VALUE, f"The {D.MODE_NAMES[mode]} mode needs {PROBE_NAMES[need[0]]}, "
                                      "which is set to not used on the status page")
                self.settings.dew.mode = mode
                self.settings.dew_mode_adopted = True
                self.dew.reset()
                self._save()
                if mode in D.FIXED_MODES:
                    self._apply_fixed_dew()
                log.info("dew mode -> %s", D.MODE_NAMES[mode])
            elif i == SW_DEW_MANUAL and self.settings.dew.mode == D.DC3OUT:
                self.settings.dew.dc3_on = value > 0.5
                self._save()
                self._apply_fixed_dew()
                log.info("DC3 12 V output -> %s", "on" if self.settings.dew.dc3_on else "off")
            elif i == SW_DEW_MANUAL:
                self.settings.dew.manual_pct = float(round(value))
                self._save()
                if self.settings.dew.mode == D.MANUAL:
                    self._apply_fixed_dew()
            elif i == SW_DEW_TARGET:
                self.settings.dew.target_c = round(value * 2) / 2.0
                self.dew.reset()
                self._save()
            elif i == SW_DEW_OFFSET:
                self.settings.dew.offset_c = round(value * 2) / 2.0
                self.dew.reset()
                self._save()

    def _apply_fixed_dew(self):
        st = self._status_or_raise()
        pwm = self.dew.fixed_pwm()
        self.dew.mark_user_send()
        if st.dew_pwm != pwm:
            self._send_confirmed(P.cmd_dew_pwm(pwm), lambda s: s.dew_pwm == pwm)

    def _send_confirmed(self, cmd, confirm):
        if not self.link.send(cmd, confirm=confirm, wait=True, timeout=CONFIRM_TIMEOUT):
            raise AlpacaError(DRIVER_ERROR, f"The box did not confirm command {cmd}")

    # ---------------------------------------------------------------- WSM box settings (open firmware only)
    WSM_KEYS = (P.KEY_DC2_SLEW, P.KEY_DC2_MAX)
    STREAM_RETRY_LINES = 5   # ask for streaming again after this many stock-length lines (about 5 s)

    def _wsm_poll(self, st):
        """Link thread: on a WSM box, read the settings the page shows, keep X1 streaming on (it's off after every
        box start), and log restarts and fault changes. Never blocks: replies are picked up by confirm checks."""
        if not P.is_open_firmware(st.fw):
            self._wsm_fw, self._wsm_cfg, self._wsm_asked = None, {}, set()
            self._wsm_x1 = {}
            return
        if st.fw != self._wsm_fw:
            self._wsm_fw, self._wsm_cfg, self._wsm_asked = st.fw, {}, set()
            self._wsm_x1, self._wsm_plain, self._wsm_streaming, self._wsm_gone_up = {}, 0, False, None
        for key in self.WSM_KEYS:
            if key not in self._wsm_cfg and key not in self._wsm_asked:
                self._wsm_asked.add(key)
                self.link.send(P.cmd_cfg_read(key), confirm=self._cfg_catcher(key))
        if "stream" not in self._wsm_asked:
            # Once per box: it may still be streaming for an earlier hub run (only a box start turns it off), and
            # then no plain line ever comes to prompt the request. The ack is what marks streaming as on.
            self._wsm_asked.add("stream")
            self.link.send(P.CMD_STREAM_ON)
        if not st.ext:
            # A stock-length line: streaming is off. Only a box start turns it off (the hub never sends 40005),
            # so after streaming was on this means the box restarted. Ask again, then every few lines.
            if self._wsm_streaming:
                self._wsm_streaming = False
                self._wsm_gone_up = self._wsm_x1.get("up")
            if self._wsm_plain % self.STREAM_RETRY_LINES == 0:
                self.link.send(P.CMD_STREAM_ON)
            self._wsm_plain += 1
            return
        self._wsm_plain = 0
        if st.ext.get("ack") == str(P.CMD_STREAM_ON):
            self._wsm_streaming = True   # X1 in replies alone (e.g. to a config read) doesn't mean streaming
        x = self._wsm_x1
        up, rst = P.ext_int(st, "up"), P.ext_int(st, "rst")
        dropped = up is not None and x.get("up") is not None and up < x["up"]
        if self._wsm_gone_up is not None or dropped:
            before = self._wsm_gone_up if self._wsm_gone_up is not None else x.get("up")
            self._wsm_restart = {"at": time.time(), "cause": P.reset_cause(rst), "after_s": before}
            log.warning("box restarted (%s) after %s s up", P.reset_cause(rst), before)
            self._wsm_gone_up = None
        flt = P.ext_int(st, "flt")
        if flt is not None and x.get("flt") is not None and flt != x["flt"]:
            new, gone = flt & ~x["flt"], x["flt"] & ~flt
            if new:
                log.warning("box faults: %s", ", ".join(P.fault_names(new)))
            if gone:
                log.info("box faults cleared: %s", ", ".join(P.fault_names(gone)))
        self._wsm_x1 = {"up": up, "rst": rst, "flt": flt, "act": P.ext_int(st, "act"), "shed": P.ext_int(st, "shed"),
                        "gate": P.ext_int(st, "gate") or 0}

    def _cfg_catcher(self, key):
        def confirm(s):
            v = P.cfg_value(s, key)
            if v is None:
                return False
            self._wsm_cfg[key] = v
            return True
        return confirm

    def dc2_slew(self):
        """DC2 ramp rate in V/s on a WSM box, or None (stock box, or not read yet)."""
        mv = self._wsm_cfg.get(P.KEY_DC2_SLEW) if self._wsm_fw else None
        return None if mv is None else mv / 1000.0

    def dc2_ceiling(self):
        """DC2 voltage ceiling stored in a WSM box, V, or None."""
        dv = self._wsm_cfg.get(P.KEY_DC2_MAX) if self._wsm_fw else None
        return None if dv is None else dv / 10.0

    def _write_wsm_key(self, key, value, what):
        """Writes a WSM config key and waits for the reply's ack or rej. Caller holds _wlock."""
        st = self._status_or_raise()
        if not P.is_open_firmware(st.fw):
            raise AlpacaError(INVALID_OPERATION, f"The {what} is a setting of the WSM firmware; this box runs "
                              f"{st.fw or '?'}")
        cmd = P.cmd_cfg_write(key, value)
        answer = {}

        def confirm(s):
            if s.ext.get("ack") == str(cmd):
                answer["ok"] = True
            elif s.ext.get("rej") == str(cmd):
                answer["why"] = s.ext.get("why", "?")
            else:
                return False
            return True
        self._send_confirmed(cmd, confirm)
        if "why" in answer:
            raise AlpacaError(INVALID_VALUE, f"The box refused the {what} (why={answer['why']})")
        self._wsm_cfg[key] = value

    @staticmethod
    def _number(v, what, lo, hi, unit):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise AlpacaError(INVALID_VALUE, f"{what}: a number, {lo:g} to {hi:g} {unit}")
        if not lo <= x <= hi:
            raise AlpacaError(INVALID_VALUE, f"{what}: {lo:g} to {hi:g} {unit} (the box's firmware limits)")
        return x

    def set_dc2_slew(self, v_per_s):
        """Writes the DC2 ramp rate (WSM key 11). The box saves it 5 s later."""
        lo, hi = (x / 1000.0 for x in P.DC2_SLEW_MV)
        v = self._number(v_per_s, "DC2 ramp", lo, hi, "V/s")
        with self._wlock:
            mv = int(round(v * 1000))
            self._write_wsm_key(P.KEY_DC2_SLEW, mv, f"DC2 ramp {v:g} V/s")
            log.info("DC2 ramp -> %g V/s", mv / 1000.0)
            return mv / 1000.0

    def set_dc2_ceiling(self, volts):
        """Writes the DC2 ceiling (WSM key 10). The box then refuses any DC2 voltage above it, from any program, and
        lowers a setpoint that was above it. Returns (ceiling, the setpoint after it, or None if unchanged)."""
        lo, hi = (x / 10.0 for x in P.DC2_MAX_DV)
        v = self._number(volts, "DC2 ceiling", lo, hi, "V")
        with self._wlock:
            dv = int(round(v * 10))
            before = self._status_or_raise().dc2_volts
            self._write_wsm_key(P.KEY_DC2_MAX, dv, f"DC2 ceiling {dv / 10:g} V")
            log.info("DC2 ceiling -> %g V", dv / 10.0)
            lowered = None
            if before > dv / 10.0 + 0.05:
                # The box pulls its setpoint down to the new ceiling: wait for a line that shows it.
                self.link.send(P.CMD_HEARTBEAT, confirm=lambda s: s.dc2_volts <= dv / 10.0 + 0.05, wait=True,
                               timeout=CONFIRM_TIMEOUT)
                lowered = dv / 10.0
            return dv / 10.0, lowered

    def clear_faults(self):
        """40002: clears latched faults (over-current, over-voltage, dew over-temperature) and the info bits."""
        with self._wlock:
            st = self._status_or_raise()
            if not P.is_open_firmware(st.fw):
                raise AlpacaError(INVALID_OPERATION, "Only the WSM firmware keeps faults to clear")
            self._send_confirmed(P.CMD_CLEAR_FAULTS, lambda s: s.ext.get("ack") == str(P.CMD_CLEAR_FAULTS))
            log.info("box faults cleared from the status page")

    def wsm_dict(self):
        """What the page shows about a WSM box: settings, protection, faults, restarts. None for a stock box."""
        if not self._wsm_fw:
            return None
        x = self._wsm_x1
        flt = x.get("flt")
        return {"dc2_slew": self.dc2_slew(), "dc2_max": self.dc2_ceiling(),
                "streaming": self._wsm_streaming, "act": x.get("act"), "shed": x.get("shed"), "gate": x.get("gate"),
                "shed_names": P.output_names(x.get("shed") or 0), "faults": None if flt is None else P.fault_names(flt),
                "latched": None if flt is None else bool(flt & P.FLT_LATCHED),
                "up_s": x.get("up"), "reset_cause": None if x.get("rst") is None else P.reset_cause(x["rst"]),
                "last_restart": self._wsm_restart}

    # ---------------------------------------------------------------- actions
    ACTIONS = ("CalibrateCurrent", "Status")

    def action(self, name, params):
        n = name.lower()
        if n == "status":
            import json
            return json.dumps(self.status_dict())
        if n == "calibratecurrent":
            return self.calibrate_current(params)
        raise AlpacaError(ACTION_NOT_IMPLEMENTED, f"Unknown action {name!r}")

    def calibrate_current(self, params):
        """Zero-current calibration (stock command 66300744). The box switches every output off while it
        measures; afterwards the outputs are restored. Refused on firmware where 66300744 means something else."""
        if (params or "").strip().lower() != "confirm":
            raise AlpacaError(INVALID_VALUE, "Pass Parameters=confirm: every output goes off for a few seconds")
        with self._wlock:
            st = self._status_or_raise()
            if not P.calibration_safe(st.fw):
                raise AlpacaError(INVALID_OPERATION, f"Refused on box firmware {st.fw or '?'}: there, 66300744 switches "
                                  "every output ON with the heater at full power. Calibration needs firmware 20250411 or WSM.")
            before = st
            self._send_confirmed(P.CAL_COMMAND, lambda s: not (s.usb or s.dc2 or s.dc46))
            b = before
            # (command, is it already right?, confirmation). Voltage before DC2 on, so DC2 never starts at a wrong value.
            restore = []
            if b.dc2_volts >= 0.1:
                restore.append((P.cmd_dc2_volts(b.dc2_volts), lambda s: abs(s.dc2_volts - b.dc2_volts) < 0.05))
            restore += [(P.cmd_usb(b.usb), lambda s: s.usb == b.usb),
                        (P.cmd_dc46(b.dc46), lambda s: s.dc46 == b.dc46),
                        (P.cmd_dc2(b.dc2), lambda s: s.dc2 == b.dc2),
                        (P.cmd_dew_pwm(b.dew_pwm), lambda s: s.dew_pwm == b.dew_pwm)]
            failed = []
            for cmd, good in restore:
                now = self.link.status
                if now is not None and good(now):
                    continue
                if not self.link.send(cmd, confirm=good, wait=True, timeout=CONFIRM_TIMEOUT):
                    failed.append(cmd)
            if failed:
                raise AlpacaError(DRIVER_ERROR, f"Calibrated, but restoring outputs failed for commands {failed}")
            return "Calibrated; outputs restored"

    # ---------------------------------------------------------------- status for the setup page / tools
    def status_dict(self):
        st = self.view(self.link.status)
        d = self.settings.dew
        box = None
        if st is not None:
            box = {"fw": st.fw, "lens": st.lens, "ambient": st.ambient, "humidity": st.humidity,
                   "dewpoint": None if st.dewpoint is None else round(st.dewpoint, 2),
                   "voltage": st.voltage, "current": st.current, "usb": st.usb, "dc2": st.dc2, "dc2_volts": st.dc2_volts,
                   "dc46": st.dc46, "dew_pwm": st.dew_pwm, "raw": st.raw}
        return {
            "service": VERSION, "heartbeat": False, "link": self.link.up(), "port": self.link.port, "error": self.link.last_error,
            "age": None if self.link.status_age() is None else round(self.link.status_age(), 1),
            "known_firmware": bool(st and P.known_firmware(st.fw)),
            "wsm": self.wsm_dict() if (st and P.is_open_firmware(st.fw)) else None,
            "box": box,
            "dew": {"mode": d.mode, "mode_name": D.MODE_NAMES.get(d.mode, "?"), "manual_pct": d.manual_pct,
                    "target_c": d.target_c, "offset_c": d.offset_c, "max_pct": d.max_pct, "dc3_on": d.dc3_on,
                    "tuning": {k: getattr(d, k) for k in TUNING},
                    "regulating_to": None if self.dew.target is None else round(self.dew.target, 2),
                    "state": self.dew.reason, "desired_pwm": self.dew.desired_pwm},
            "eeprom_writes": self.settings.eeprom_writes,
            "probes": {"lens": self.settings.use_lens, "ambient": self.settings.use_ambient},
            "missing_gauge": {"mode": self.settings.missing_gauge, "value": self.settings.missing_value},
            "link_stats": dict(self.link.stats), "queued": self.link.queued(),
        }
