"""The box driver against its simulator: protocol, dew controller, service, history, and its Alpaca devices and
page on the hub's DeviceServer. Moved from tests/test_wbstock.py with the code (REFACTOR.md step 9); the flat
panel's tests went with it to drivers/wcover in step 7."""
import csv
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                   # the driver folder (its package)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))  # the repository (hub)

from hub.core.net import DiscoveryDedupe  # noqa: E402
from hub.devserver import DeviceServer  # noqa: E402
from wbox import VERSION  # noqa: E402
from wbox import dew as D  # noqa: E402
from wbox import protocol as P  # noqa: E402
from wbox.devices import BoxApp, Names, make_devices  # noqa: E402
from wbox.driver import UI  # noqa: E402
from wbox.service import (DRIVER_ERROR, INVALID_OPERATION, INVALID_VALUE, NOT_IMPLEMENTED, SW_DC2,  # noqa: E402
                          SW_AMBIENT, SW_DC2V, SW_DC46, SW_DEW_MANUAL, SW_DEW_MODE, SW_DEW_POWER, SW_DEW_TARGET,
                          SW_DEWPOINT, SW_FIXED, SW_HUMIDITY, SW_LENS, SW_USB, NOT_CONNECTED, VALUE_NOT_SET,
                          AlpacaError, Service)
from wbox.history import KEYS, History  # noqa: E402
from wbox.settings import Settings  # noqa: E402
from wbox.sim import SimBox  # noqa: E402


def box_server(svc):
    names = Names(svc)
    devices = make_devices(svc, names, lambda k: f"uid-{k}")
    return DeviceServer(devices, BoxApp(svc, names, devices, UI, "SIM"), VERSION)


LINE = "ZXWBPlusV3A20250411A3.25A81.40A7.90A2.31A12.28A1A0A128A1A120A\r\n"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ protocol
class ProtocolTest(unittest.TestCase):
    def test_parse(self):
        s = P.parse_status(LINE)
        self.assertEqual(s.fw, "20250411")
        self.assertAlmostEqual(s.lens, 3.25)
        self.assertAlmostEqual(s.humidity, 81.4)
        self.assertAlmostEqual(s.ambient, 7.9)
        self.assertAlmostEqual(s.current, 2.31)
        self.assertAlmostEqual(s.voltage, 12.28)
        self.assertEqual((s.usb, s.dc2, s.dew_pwm, s.dc46, s.dc2_volts), (True, False, 128, True, 12.0))
        self.assertAlmostEqual(s.dewpoint, 4.85, delta=0.1)

    def test_open_firmware(self):
        # WSM firmware: the stock 12 fields, then X1 extension fields that the parser ignores.
        s = P.parse_status(LINE.replace("A20250411A", "AWSM020A").rstrip() + "X1Aact=5Ashed=0Aflt=0Aack=40003A\r\n")
        self.assertEqual(s.fw, "WSM020")
        self.assertEqual((s.usb, s.dc2, s.dew_pwm, s.dc46, s.dc2_volts), (True, False, 128, True, 12.0))
        for fw in ("WSM020", "WSM1"):
            self.assertTrue(P.known_firmware(fw) and P.calibration_safe(fw), fw)
        for fw in ("WSM", "WSMx20", "OSS020", "20240101", ""):
            self.assertFalse(P.known_firmware(fw), fw)
        self.assertTrue(P.known_firmware("20240216"))
        self.assertFalse(P.calibration_safe("20240216"))

    def test_missing_sensors(self):
        s = P.parse_status("ZXWBPlusV3A20240216A-127.00AnanAnanA0.40A12.00A1A1A0A0A50A\r\n")
        self.assertIsNone(s.lens)
        self.assertIsNone(s.ambient)
        self.assertIsNone(s.dewpoint)
        self.assertFalse(s.ambient_ok)
        self.assertEqual(s.dc2_volts, 5.0)

    def test_rejects_partial_and_noise(self):
        for bad in ("", "PlusV3A20250411A3.2", "ZXWBPlusV3A20250411A3.25A81.40", "hello", LINE.replace("A1A0A128", "A7A0A128"),
                    LINE.replace("A128A", "A999A")):
            self.assertIsNone(P.parse_status(bad), bad)

    def test_commands(self):
        self.assertEqual([P.cmd_usb(True), P.cmd_usb(False), P.cmd_dc2(True), P.cmd_dc46(False)], [111, 110, 121, 100])
        self.assertEqual(P.cmd_dew_pwm(0), 3000)
        self.assertEqual(P.cmd_dew_pwm(255), 3255)
        self.assertEqual(P.cmd_dc2_volts(13.2), 20132)
        self.assertEqual(P.cmd_dc2_volts(7.55), 20076)  # 75.5 rounds to even, 76 after float error: either is fine
        with self.assertRaises(ValueError):
            P.cmd_dew_pwm(256)
        with self.assertRaises(ValueError):
            P.cmd_dc2_volts(13.3)
        self.assertEqual(P.encode(3128), b"3128")  # no terminator: the box's parseInt(SKIP_NONE) chokes on one

    def test_sim_loses_commands_behind_a_stray_byte(self):
        """Like the real box: a newline left in front of the next command loses that command."""
        box = SimBox()
        box._rx += b"121\n"
        box.loop_once()
        self.assertTrue(box.dc2)
        box._rx += b"3100\n"
        box.loop_once()
        self.assertEqual(box.pwm, 0)
        self.assertEqual(box.dropped, 1)
        box._rx += P.encode(3100)
        box.loop_once()
        self.assertEqual(box.pwm, 100)


# ------------------------------------------------------------------ dew controller
def status(lens=5.0, amb=8.0, hum=85.0, pwm=0):
    return P.Status(fw="20250411", lens=lens, ambient=amb, humidity=hum, current=1, voltage=12, dew_pwm=pwm)


class DewTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = D.DewSettings()
        self.c = D.DewController(self.s, self.clock)

    def test_off_forces_zero_once_per_interval(self):
        self.s.mode = D.OFF
        self.assertEqual(self.c.step(status(pwm=100)), 0)
        self.clock.t += 5
        self.assertIsNone(self.c.step(status(pwm=100)))  # rate limited
        self.clock.t += 60
        self.assertEqual(self.c.step(status(pwm=100)), 0)
        self.assertIsNone(self.c.step(status(pwm=0)))

    def test_manual(self):
        self.s.mode, self.s.manual_pct = D.MANUAL, 50
        self.assertEqual(self.c.step(status(pwm=0)), 128)
        self.s.max_pct = 30
        self.assertEqual(self.c.fixed_pwm(), 76)  # 76.5 rounds half to even

    def test_fallback_when_lens_missing(self):
        self.s.mode = D.CONSTANT
        self.assertEqual(self.c.step(status(lens=None)), P.pct_to_pwm(40))
        self.assertIn("lens", self.c.reason)

    def test_dewpoint_mode_needs_ambient(self):
        self.s.mode = D.DEWPOINT
        self.assertEqual(self.c.step(status(amb=None, hum=None)), P.pct_to_pwm(40))
        self.assertIn("ambient", self.c.reason)

    def test_small_changes_not_sent(self):
        self.s.mode, self.s.target_c = D.CONSTANT, 5.1
        # error 0.1 C -> 2 % wanted, box at 0: under the 5 % step, so nothing is written
        self.assertIsNone(self.c.step(status(lens=5.0, pwm=0)))

    def test_regulates_in_simulation_with_few_writes(self):
        """Three simulated hours in dew-point mode: lens ends near target, writes stay rare."""
        box = SimBox(ambient=8.0, humidity=85.0)
        self.s.mode, self.s.offset_c = D.DEWPOINT, 4.0
        writes = 0
        for _ in range(3 * 3600):
            self.clock.t += 1
            line = box.loop_once(1.0)
            pwm = self.c.step(P.parse_status(line.decode()))
            if pwm is not None:
                box._rx += P.encode(P.cmd_dew_pwm(pwm))
                writes += 1
        st = P.parse_status(box.status_line().decode())
        self.assertAlmostEqual(st.lens, st.dewpoint + 4.0, delta=0.6)
        self.assertLessEqual(writes, 3 * 60)
        self.assertGreater(box.pwm, 0)


# ------------------------------------------------------------------ service against the simulated box
class ServiceTest(unittest.TestCase):
    fw = "20250411"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(os.path.join(self.tmp.name, "s.json"))
        self.settings.port = "SIM"  # "auto" would scan the real COM ports
        self.box = SimBox(fw=self.fw, loop_s=0.02)
        self.box.pwm = 64
        self.svc = Service(self.settings, opener=self.box.opener)
        self.svc.start()
        self._wait(lambda: self.svc.link.up())

    def tearDown(self):
        self.svc.stop()
        self.tmp.cleanup()

    def _wait(self, cond, t=3.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def test_adopts_box_dew_state_on_first_run(self):
        self._wait(lambda: self.settings.dew_mode_adopted)
        self.assertEqual(self.settings.dew.mode, D.MANUAL)
        self.assertAlmostEqual(self.settings.dew.manual_pct, 25.1)
        self.assertEqual(self.box.pwm, 64)  # untouched
        self.assertEqual(self.box.eeprom_writes, 0)

    def test_switch_writes_are_confirmed_and_deduplicated(self):
        self.svc.write_switch(SW_DC2V, 9.5)
        self.svc.write_switch(SW_DC2, 1)
        self.assertTrue(self.box.dc2)
        self.assertEqual(self.box.dc2v, 95)
        self.assertEqual(self.svc.switch_value(SW_DC2V), 9.5)
        n = self.box.eeprom_writes
        self.svc.write_switch(SW_DC2, 1)       # already on: nothing sent
        self.svc.write_switch(SW_USB, 1)       # already on
        self.assertEqual(self.box.eeprom_writes, n)
        self.svc.write_switch(SW_DC46, 0)
        self.assertFalse(self.box.dc46)
        self._wait(lambda: self.settings.eeprom_writes == self.box.eeprom_writes)

    def test_lost_command_is_sent_once_more(self):
        self.box.garble_writes = 1
        self.svc.write_switch(SW_DC46, 0)
        self.assertFalse(self.box.dc46)
        self.assertEqual(self.box.dropped, 1)
        self.assertEqual(self.svc.link.stats["resends"], 1)

    def test_command_lost_twice_fails(self):
        self.box.garble_writes = 2
        with self.assertRaises(AlpacaError):
            self.svc.write_switch(SW_DC46, 0)
        self.assertTrue(self.box.dc46)
        self.assertEqual(self.svc.link.stats["resends"], 1)

    def test_range_and_readonly(self):
        with self.assertRaises(AlpacaError) as e:
            self.svc.write_switch(SW_DC2V, 13.3)
        self.assertEqual(e.exception.number, INVALID_VALUE)
        with self.assertRaises(AlpacaError) as e:
            self.svc.write_switch(SW_LENS, 1)
        self.assertEqual(e.exception.number, NOT_IMPLEMENTED)

    def test_dew_modes(self):
        self.svc.write_switch(SW_DEW_MODE, D.MANUAL)
        self.svc.write_switch(SW_DEW_MANUAL, 80)
        self.assertEqual(self.box.pwm, 204)
        self.assertEqual(self.svc.switch_value(SW_DEW_POWER), 80.0)
        self.svc.write_switch(SW_DEW_MODE, D.OFF)
        self.assertEqual(self.box.pwm, 0)
        self.assertEqual(Settings.load(self.settings.path).dew.mode, D.OFF)

    def test_auto_mode_heater_off_on_exit(self):
        self.svc.write_switch(SW_DEW_MODE, D.CONSTANT)  # target 5 C, lens at 6 C in the sim: heater stays low
        self.svc.dew.s.target_c = 30
        self.svc.dew.reset()
        self._wait(lambda: self.box.pwm > 64)
        self.svc.stop()
        self.assertEqual(self.box.pwm, 0)

    def test_calibration(self):
        before = (self.box.usb, self.box.dc2, self.box.dc46, self.box.pwm, self.box.dc2v)
        with self.assertRaises(AlpacaError):
            self.svc.action("CalibrateCurrent", "")
        self.assertEqual(self.svc.action("CalibrateCurrent", "confirm"), "Calibrated; outputs restored")
        self.assertIn(P.CAL_COMMAND, self.box.commands)
        self.assertEqual((self.box.usb, self.box.dc2, self.box.dc46, self.box.pwm, self.box.dc2v), before)

    def test_box_down(self):
        self.svc.link.stop()
        self.svc.link._status_t -= 10
        with self.assertRaises(AlpacaError) as e:
            self.svc.switch_value(SW_USB)
        self.assertEqual(e.exception.number, DRIVER_ERROR)
        self.assertEqual(self.svc.switch_value(SW_DEW_MODE), self.settings.dew.mode)  # settings still readable


    def test_default_gauges(self):
        self.assertEqual(self.svc.switch_count(), 16)
        self.assertEqual(self.svc.switch_def(SW_LENS).name, "Lens temperature")  # same id as v0.1.0
        self.assertEqual(self.svc.switch_def(SW_AMBIENT).name, "Ambient temperature")
        st = self.svc.link.status
        self.assertAlmostEqual(self.svc.switch_value(SW_AMBIENT), st.ambient)
        self.assertAlmostEqual(self.svc.switch_value(SW_HUMIDITY), st.humidity)
        self.assertAlmostEqual(self.svc.switch_value(SW_DEWPOINT), round(st.dewpoint, 2))
        self.assertFalse(self.svc.switch_def(SW_DEWPOINT).writable)

    def test_gauge_selection(self):
        self.svc.set_gauges(["dew_margin", "lens", "lens"])  # stored in the fixed order, without duplicates
        self.assertEqual(self.settings.gauges, ["lens", "dew_margin"])
        self.assertEqual(self.svc.switch_count(), SW_FIXED + 2)
        self.assertEqual(self.svc.switch_def(SW_FIXED + 1).name, "Lens above dew point")
        st = self.svc.link.status
        self.assertAlmostEqual(self.svc.switch_value(SW_FIXED + 1), round(st.lens - st.dewpoint, 2), places=1)
        self.assertEqual(self.svc.switch_def(SW_DEW_MODE).name, "Dew heater mode")  # writable ids unchanged
        with self.assertRaises(AlpacaError) as e:
            self.svc.switch_def(SW_FIXED + 2)
        self.assertEqual(e.exception.number, INVALID_VALUE)
        with self.assertRaises(AlpacaError):
            self.svc.set_gauges(["nosuch"])
        self.assertEqual(Settings.load(self.settings.path).gauges, ["lens", "dew_margin"])
        self.svc.set_gauges([])
        self.assertEqual(self.svc.switch_count(), SW_FIXED)

    def test_ambient_gauges_without_dht(self):
        """A missing probe's gauges answer (NINA aborts its connect on a failed read and freezes a failed poll):
        ': MISSING' in the name, and the value chosen in settings.missing_gauge, by default the last good one."""
        self._wait(lambda: self.svc.link.status is not None and self.svc.link.status.ambient_ok)
        self._wait(lambda: "humidity" in self.svc._last_good)
        good = self.svc.switch_value(SW_HUMIDITY)
        self.box.dht_missing = True
        self._wait(lambda: not self.svc.link.status.ambient_ok)
        for i in (SW_AMBIENT, SW_HUMIDITY, SW_DEWPOINT):
            self.assertTrue(self.svc.switch_def(i).name.endswith(": MISSING"), i)
        self.assertEqual(self.svc.switch_value(SW_HUMIDITY), good)       # hold (default)
        self.svc.set_missing("minimum")
        self.assertEqual(self.svc.switch_value(SW_AMBIENT), -40.0)
        self.svc.set_missing("value", -5)
        self.assertEqual((self.svc.switch_value(SW_AMBIENT), self.svc.switch_value(SW_HUMIDITY)), (-5.0, 0.0))  # kept in range
        with self.assertRaises(AlpacaError):
            self.svc.set_missing("guess")
        self.assertEqual(Settings.load(self.settings.path).missing_gauge, "value")
        self.box.dht_missing = False
        self._wait(lambda: self.svc.link.status.ambient_ok)
        self.assertEqual(self.svc.switch_def(SW_AMBIENT).name, "Ambient temperature")

    def test_dew_target_steps(self):
        """ConformU skips the in-between steps of a switch with a negative minimum (its tolerance for the minimum is
        relative, so it fails below 0): cover them here."""
        sd = self.svc.switch_def(SW_DEW_TARGET)
        self.assertEqual((sd.min, sd.max, sd.step), (-20, 40, 0.5))
        for v in (-20, -19.5, -7.5, -0.5, 0, 0.5, 12.5, 39.5, 40):
            self.svc.write_switch(SW_DEW_TARGET, v)
            self.assertEqual(self.svc.switch_value(SW_DEW_TARGET), v)
        for v, got in ((-19.8, -20.0), (-0.3, -0.5), (12.6, 12.5), (12.8, 13.0)):
            self.svc.write_switch(SW_DEW_TARGET, v)
            self.assertEqual(self.svc.switch_value(SW_DEW_TARGET), got)
        for bad in (-20.5, 40.5):
            with self.assertRaises(AlpacaError):
                self.svc.write_switch(SW_DEW_TARGET, bad)
        self.assertEqual(self.box.eeprom_writes, 0)   # a PC-side setting: nothing sent to the box

    def test_no_wsm_settings_on_stock(self):
        if P.is_open_firmware(self.fw):
            self.skipTest("a stock-box check")
        self.assertIsNone(self.svc.status_dict()["wsm"])
        self.assertIsNone(self.svc.dc2_slew())
        with self.assertRaises(AlpacaError) as e:
            self.svc.set_dc2_slew(10)
        self.assertEqual(e.exception.number, INVALID_OPERATION)
        for f in (lambda: self.svc.set_dc2_ceiling(8.4), self.svc.clear_faults):
            with self.assertRaises(AlpacaError):
                f()
        self.assertEqual(self.svc.switch_def(SW_DC2V).max, P.DC2_MAX_V)
        self.assertFalse([c for c in self.box.commands if c >= 40000 and c != P.CAL_COMMAND])


class DiscoveryDedupeTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.d = DiscoveryDedupe(clock=self.clock, addresses=lambda: {"192.168.1.67"})

    def test_same_pc_answered_on_loopback_only(self):
        self.assertEqual(self.d.classify("192.168.1.67"), "later")
        self.assertEqual(self.d.classify("127.0.0.1"), "now")
        self.clock.t += DiscoveryDedupe.HOLD_S
        self.assertFalse(self.d.still_needed())

    def test_same_pc_without_loopback_request(self):
        self.assertEqual(self.d.classify("192.168.1.67"), "later")
        self.clock.t += DiscoveryDedupe.HOLD_S
        self.assertTrue(self.d.still_needed())

    def test_other_pc(self):
        self.assertEqual(self.d.classify("127.0.0.1"), "now")
        self.assertEqual(self.d.classify("192.168.1.20"), "now")

    def test_later_round(self):
        self.d.classify("127.0.0.1")
        self.clock.t += 10
        self.d.classify("192.168.1.67")
        self.clock.t += DiscoveryDedupe.HOLD_S
        self.assertTrue(self.d.still_needed())


class OpenFirmwareTest(ServiceTest):
    """The user's open firmware (WSM…) speaks the 20250411 protocol: every service test runs against it too."""
    fw = "WSM020"

    def setUp(self):
        super().setUp()
        # When it first sees a WSM box the service reads keys 11 and 10 and turns X1 streaming on; let that finish.
        self._wait(lambda: self.svc.dc2_slew() is not None and self.svc.dc2_ceiling() is not None
                   and (self.svc.wsm_dict() or {}).get("streaming"))

    def test_known(self):
        self.assertTrue(self.svc.status_dict()["known_firmware"])

    def test_dc2_slew_read_at_start(self):
        self.assertEqual(self.svc.dc2_slew(), P.DC2_SLEW_DEFAULT_MV / 1000)
        self.assertIn(P.cmd_cfg_read(P.KEY_DC2_SLEW), self.box.commands)
        w = self.svc.status_dict()["wsm"]
        self.assertEqual((w["dc2_slew"], w["dc2_max"], w["streaming"]), (5.0, 13.2, True))
        self.assertEqual(self.box.eeprom_writes, 0)   # a config read isn't an EEPROM write
        self.assertEqual(self.settings.eeprom_writes, 0)

    def test_dc2_slew_write(self):
        self.assertEqual(self.svc.set_dc2_slew("20"), 20.0)
        self.assertEqual(self.box.cfg[P.KEY_DC2_SLEW], 20000)
        self.assertEqual(self.svc.dc2_slew(), 20.0)
        self.assertIn(711020000, self.box.commands)
        self.assertEqual(self.svc.set_dc2_slew(0.5), 0.5)
        self.assertEqual(self.box.cfg[P.KEY_DC2_SLEW], 500)

    def test_dc2_slew_limits(self):
        for bad in ("0.05", "21", "fast", None):
            with self.assertRaises(AlpacaError) as e:
                self.svc.set_dc2_slew(bad)
            self.assertEqual(e.exception.number, INVALID_VALUE)
            self.assertIn("0.1 to 20", e.exception.message)
        self.assertEqual(self.box.cfg[P.KEY_DC2_SLEW], 5000)

    def test_dc2_slew_refused_by_box(self):
        self.box.cfg_range[P.KEY_DC2_SLEW] = (100, 10000)   # a firmware with a lower ceiling
        with self.assertRaises(AlpacaError) as e:
            self.svc.set_dc2_slew(15)
        self.assertIn("why=2", e.exception.message)
        self.assertEqual(self.svc.dc2_slew(), 5.0)

    def test_dc2_ceiling(self):
        self.svc.write_switch(SW_DC2V, 12.0)
        self.assertEqual(self.svc.set_dc2_ceiling("8.4"), (8.4, 8.4))   # the setpoint came down with it
        self.assertEqual(self.box.cfg[P.KEY_DC2_MAX], 84)
        self.assertEqual(self.box.dc2v, 84)
        self.assertEqual(self.svc.switch_def(SW_DC2V).max, 8.4)
        with self.assertRaises(AlpacaError) as e:
            self.svc.write_switch(SW_DC2V, 9.0)
        self.assertIn("8.4 V by the DC2 ceiling", e.exception.message)
        self.assertEqual(self.box.dc2v, 84)
        self.svc.write_switch(SW_DC2V, 7.4)
        self.assertEqual(self.box.dc2v, 74)
        self.assertEqual(self.svc.set_dc2_ceiling(13.2), (13.2, None))
        for bad in ("4.9", "13.3", "x"):
            with self.assertRaises(AlpacaError):
                self.svc.set_dc2_ceiling(bad)

    def test_streaming_comes_back_after_a_restart(self):
        self.box.restart(4)   # brown-out
        self._wait(lambda: self.box.x1_stream and self.svc.wsm_dict()["last_restart"] is not None)
        r = self.svc.wsm_dict()["last_restart"]
        self.assertEqual(r["cause"], "brown-out")
        self._wait(lambda: self.svc.wsm_dict()["reset_cause"] == "brown-out")

    def test_protection_and_faults(self):
        self.box.shed, self.box.flt = 2, 1 | 4
        self._wait(lambda: self.svc.wsm_dict()["shed"] == 2)
        w = self.svc.wsm_dict()
        self.assertEqual(w["shed_names"], ["DC2"])
        self.assertEqual(w["faults"], ["over-current (latched)", "low voltage: all off"])
        self.assertTrue(w["latched"])
        self.assertFalse(w["act"] & 2)
        self.svc.clear_faults()
        self.assertEqual(self.box.flt, 0)
        self._wait(lambda: self.svc.wsm_dict()["faults"] == [])

    # WSM skips stray bytes before a number (stock loses the command), so these two differ from the stock box.
    def test_lost_command_is_sent_once_more(self):
        self.box.garble_writes = 1
        self.svc.write_switch(SW_DC46, 0)
        self.assertFalse(self.box.dc46)
        self.assertEqual(self.box.dropped, 0)
        self.assertEqual(self.svc.link.stats["resends"], 0)

    def test_command_lost_twice_fails(self):
        self.box.garble_writes = 2
        self.svc.write_switch(SW_DC46, 0)
        self.assertFalse(self.box.dc46)

    def test_switching_is_fast(self):
        """WSM answers each command at once: with a 1 s status line, four changes still take well under a second."""
        self.box.loop_s = 1.0
        t = time.monotonic()
        for i, v in ((SW_DC46, 0), (SW_DC2V, 9.5), (SW_DC2, 1), (SW_DC46, 1)):
            self.svc.write_switch(i, v)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertTrue(self.box.dc2 and self.box.dc46)
        self.assertEqual(self.box.dc2v, 95)

    def test_streaming_left_on_by_an_earlier_run(self):
        """A box still streaming for an earlier hub: lines carry X1 from the start, and the service still marks
        streaming as on (and so can tell a restart) because it asks once anyway."""
        self.svc.stop()
        box = SimBox(fw=self.fw, loop_s=0.02)
        box.x1_stream = True
        svc = Service(self.settings, opener=box.opener)
        svc.start()
        try:
            end = time.monotonic() + 3
            while time.monotonic() < end and not (svc.wsm_dict() or {}).get("streaming"):
                time.sleep(0.01)
            self.assertTrue(svc.wsm_dict()["streaming"])
            self.assertIn(P.CMD_STREAM_ON, box.commands)
        finally:
            svc.stop()
            self.svc = Service(self.settings, opener=self.box.opener)   # for tearDown
            self.svc.start()

    def test_regular_line_is_stock_length(self):
        self.box.x1_once = self.box.x1_stream = False   # what a box sends after a start, before any 40004
        self.assertEqual(self.box.status_line().decode().count("A"), 12)


class OldFirmwareTest(ServiceTest):
    fw = "20240216"

    def test_calibration(self):
        with self.assertRaises(AlpacaError) as e:
            self.svc.action("CalibrateCurrent", "confirm")
        self.assertEqual(e.exception.number, INVALID_OPERATION)
        self.assertNotIn(P.CAL_COMMAND, self.box.commands)
        self.assertNotEqual(self.box.pwm, 255)


# ------------------------------------------------------------------ port finding
# ------------------------------------------------------------------ Alpaca over HTTP
class AlpacaTest(ServiceTest):
    def setUp(self):
        super().setUp()
        self.srv = box_server(self.svc)
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.stop()
        super().tearDown()

    def call(self, method, path, **params):
        params.setdefault("ClientTransactionID", 42)
        data = urllib.parse.urlencode(params)
        if method == "GET":
            req = urllib.request.Request(f"{self.base}{path}?{data}")
        else:
            req = urllib.request.Request(self.base + path, data=data.encode(), method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if "json" in r.headers["Content-Type"] else body)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_flow(self):
        st, j = self.call("GET", "/api/v1/switch/0/maxswitch")
        self.assertEqual(j["ErrorNumber"], 0x407)
        st, j = self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.assertEqual((j["ErrorNumber"], j["ClientTransactionID"]), (0, 42))
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"], 16)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=2)[1]["Value"], "DC2 voltage")
        self.assertEqual(self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=2, Value=6.6)[1]["ErrorNumber"], 0)
        self.assertEqual(self.box.dc2v, 66)
        self.assertEqual(self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=2, Value=20)[1]["ErrorNumber"], INVALID_VALUE)
        self.assertEqual(self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=2, Value="x")[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/nosuch")[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/name", ClientTransactionID="abc")[0], 400)
        j = self.call("GET", "/api/v1/switch/0/devicestate")[1]
        self.assertTrue(any(e["Name"] == "GetSwitchValue0" for e in j["Value"]))
        self.call("PUT", "/api/v1/observingconditions/0/connected", Connected="True")
        self.assertAlmostEqual(self.call("GET", "/api/v1/observingconditions/0/humidity")[1]["Value"], 85.0)
        self.assertEqual(self.call("GET", "/api/v1/observingconditions/0/pressure")[1]["ErrorNumber"], NOT_IMPLEMENTED)
        j = self.call("PUT", "/api/v1/switch/0/action", Action="Status", Parameters="")[1]
        self.assertEqual(json.loads(j["Value"])["box"]["fw"], self.fw)
        self.assertIn("WandererBox", self.call("GET", "/")[1])
        self.assertEqual(self.call("GET", "/management/v1/configureddevices")[1]["Value"][0]["DeviceType"], "Switch")

    def test_the_port_is_the_hubs(self):
        req = urllib.request.Request(self.base + "/setup/port", data=b"port=COM9", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 400)

    def test_gauge_form(self):
        req = urllib.request.Request(self.base + "/setup/gauges", data=b"gauge=voltage&gauge=dewpoint", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn("Reset to defaults", r.read().decode())
        self.assertEqual(self.settings.gauges, ["voltage", "dewpoint"])
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"], SW_FIXED + 2)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=SW_FIXED + 1)[1]["Value"], "Dew point")
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=SW_FIXED + 2)[1]["ErrorNumber"],
                         INVALID_VALUE)
        req = urllib.request.Request(self.base + "/setup/gauges", data=b"reset=1&gauge=voltage", method="POST")
        urllib.request.urlopen(req, timeout=10).close()
        self.assertEqual(self.svc.switch_count(), 16)


# ------------------------------------------------------------------ history
class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.clock.t = time.mktime((2026, 11, 17, 21, 0, 0, 0, 0, -1))  # local 21:00:00
        self.h = History(self.tmp.name, 7, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, seconds, **v):
        for _ in range(seconds):
            self.h.add(v)
            self.clock.t += 1

    def test_minute_average_written_when_the_next_minute_starts(self):
        start = self.clock.t
        self.feed(29, vin=12.0, lens=5.0)
        self.feed(1, vin=10.8, lens=5.0)    # a 1 s dip the average hides
        self.feed(30, vin=12.2, lens=None)  # probe pulled: averaged from what there is, never zero
        self.assertEqual(self.h.files(), [])
        self.feed(1, vin=12.0)
        name, _ = self.h.files()[0]
        with open(os.path.join(self.tmp.name, name), encoding="utf-8") as f:
            r = list(csv.DictReader(f))[0]
        self.assertEqual(float(r["unix_s"]), start + 30)     # the minute's middle
        self.assertAlmostEqual(float(r["input_V"]), 12.08)
        self.assertEqual((float(r["input_V_min"]), float(r["input_V_max"])), (10.8, 12.2))
        self.assertEqual(float(r["lens_C"]), 5.0)
        self.assertEqual(r["ambient_C"], "")
        self.assertEqual(name, "wbstock-history-2026-11-17.csv")
        with open(os.path.join(self.tmp.name, name), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("time_local,unix_s,input_V,input_V_min,input_V_max,input_A"))
        self.assertTrue(lines[1].startswith("2026-11-17 21:00,"))
        self.assertIn(",,", lines[1])  # missing readings stay empty

    def test_chart_has_10_second_averages(self):
        start = self.clock.t
        self.feed(10, vin=12.0, lens=5.0)
        self.feed(9, vin=12.4, lens=None)
        self.feed(1, vin=13.4)                                # a 1 s spike in the second step
        self.assertEqual(len(self.h.coarse()["rows"]), 1)    # the second step is still open
        self.feed(1, vin=12.0)
        h = self.h.coarse()
        self.assertEqual(h["step"], 10)
        rows = [dict(zip(("t",) + tuple(h["keys"]), r)) for r in h["rows"]]
        self.assertEqual([(r["vin_min"], r["vin_max"]) for r in rows], [(12.0, 12.0), (12.4, 13.4)])
        self.assertEqual([r["t"] for r in rows], [start + 5, start + 15])    # dated in each step's middle
        self.assertEqual([r["vin"] for r in rows], [12.0, 12.5])
        self.assertEqual([r["lens"] for r in rows], [5.0, None])            # missing stays missing
        self.assertEqual(self.h.files(), [])                                # the files keep minutes

    def test_fine_keeps_ten_minutes_and_one_sample_per_second(self):
        self.h.add({"vin": 1.0})
        self.h.add({"vin": 2.0})  # same second: replaces
        self.assertEqual(self.h.fine()["rows"], [[int(self.clock.t), 2.0] + [None] * (len(KEYS) - 1)])
        self.feed(700, vin=12.0)
        rows = self.h.fine()["rows"]
        self.assertEqual(len(rows), 600)
        self.assertEqual(len(self.h.fine(since=rows[-2][0])["rows"]), 1)

    def test_chart_keeps_24_hours_and_starts_empty(self):
        # like every device tab in the hub: 24 h in memory, nothing read back at start (the Overview does that)
        self.feed(61, vin=12.0)
        self.clock.t += 2 * 86400
        self.feed(61, vin=12.5)
        self.assertEqual({r[1] for r in self.h.coarse()["rows"]}, {12.5})   # the steps 2 days ago are gone
        self.assertEqual(len(self.h.files()), 2)                           # but still in its day file
        s = Settings(os.path.join(self.tmp.name, "wbox.json"))
        s.port = "SIM"
        svc = Service(s, opener=SimBox(loop_s=0.02).opener, history_dir=self.tmp.name)
        svc.history._clock = self.clock
        svc.link.start = lambda: None                    # no link needed: only what start() does to the history
        svc.start()
        self.assertEqual(svc.history.coarse()["rows"], [])
        self.assertEqual(len(svc.history.files()), 2)

    def test_reload_after_restart(self):
        self.feed(181, vin=12.0)
        self.h.flush()
        h2 = History(self.tmp.name, 7, clock=self.clock)
        self.assertEqual(h2.load(), 4)                       # the minutes in the file (for tools: the chart starts empty)
        self.assertEqual([r[1] for r in h2.coarse()["rows"]], [12.0] * 4)

    def test_retention_and_delete(self):
        for day in range(10):
            with open(os.path.join(self.tmp.name, f"wbstock-history-2026-11-{8 + day:02d}.csv"), "w") as f:
                f.write("time_local,unix_s\n")
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as f:
            f.write("keep me")
        self.assertEqual(self.h.purge(), 3)  # keeps today and the 6 days before it
        self.assertEqual(self.h.files()[0][0], "wbstock-history-2026-11-11.csv")
        self.h.set_days(2)
        self.assertEqual([n for n, _ in self.h.files()], ["wbstock-history-2026-11-16.csv", "wbstock-history-2026-11-17.csv"])
        self.feed(61, vin=12.0)
        # today's file was written before min/max (its header without them): today goes on in a -2 file
        self.assertIn("wbstock-history-2026-11-17-2.csv", [n for n, _ in self.h.files()])
        self.assertEqual(self.h.delete_all(), (3, 0))
        self.assertEqual((self.h.files(), self.h.coarse()["rows"], self.h.fine()["rows"]), ([], [], []))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "notes.txt")))

    def test_unwritable_folder_keeps_the_chart(self):
        blocker = os.path.join(self.tmp.name, "file")
        open(blocker, "w").close()
        h = History(os.path.join(blocker, "history"), 7, clock=self.clock)
        for _ in range(61):
            h.add({"vin": 12.0})
            self.clock.t += 1
        self.assertEqual(len(h.coarse()["rows"]), 6)         # six 10 s steps
        self.assertFalse(h.info()["write_ok"])


class StatusPageTest(AlpacaTest):
    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()

    def post(self, path, data=b"", header=True):
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers={"X-WBStock": "1"} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_and_status(self):
        st, h, body = self.get("/", {"Accept-Encoding": "gzip"})
        self.assertEqual(h["Content-Encoding"], "gzip")
        import gzip
        page = gzip.decompress(body).decode()
        for s in ("K.chart", "id=\"tiles\"", "Download history", "Box serial port", "Reset to defaults"):
            self.assertIn(s, page)
        self.assertNotIn("__DEVICE_JS__", page)
        s = json.loads(self.get("/status.json")[2])
        self.assertTrue(s["local"])
        self.assertEqual(s["build"], s["service"])
        self.assertEqual(s["urls"], [])  # bound to 127.0.0.1: no address for phones
        self.assertEqual(s["history"]["days"], 7)
        c = json.loads(self.get("/setup.json")[2])
        self.assertEqual(c["ports"], ["SIM"])
        self.assertEqual(sum(g["on"] for g in c["gauges"]), 8)

    def test_history_json_gets_samples(self):
        self._wait(lambda: self.svc.history.fine()["rows"])
        j = json.loads(self.get("/history.json?res=s")[2])
        self.assertEqual(j["keys"][0], "vin")
        r = dict(zip(["t"] + j["keys"], j["rows"][-1]))
        self.assertAlmostEqual(r["vin"], self.box.voltage, delta=0.01)
        self.assertEqual(json.loads(self.get("/history.json?res=s&since=9999999999")[2])["rows"], [])
        self.assertEqual(json.loads(self.get("/history.json?res=min")[2])["rows"], [])

    def test_local_controls(self):
        self.assertEqual(self.post("/control/delete-history", header=False)[0], 403)
        self.assertEqual(self.post("/setup/history", b"days=400")[0], 400)
        self.assertEqual(self.post("/setup/history", b"days=3"), (200, "History kept for 3 days"))
        self.assertEqual(Settings.load(self.settings.path).history_days, 3)
        opened = []
        with unittest.mock.patch("os.startfile", opened.append, create=True):
            self.assertEqual(self.post("/control/open-folder")[0], 200)
        self.assertEqual(opened, [self.svc.history.folder])
        self.svc.history.add({"vin": 12.0})
        self.assertEqual(self.post("/control/delete-history")[0], 200)
        self.assertEqual(self.svc.history.fine()["rows"], [])
        self.srv.is_local = lambda ip: False  # as seen from a phone
        self.assertEqual(self.post("/control/delete-history")[0], 403)
        self.assertEqual(self.post("/setup/history", b"days=1")[0], 403)
        self.assertFalse(json.loads(self.get("/status.json")[2])["local"])
        st, h, body = self.get("/history.zip")  # downloads work from anywhere
        self.assertEqual(h["Content-Type"], "application/zip")

    def test_qr(self):
        self.srv.public_urls = lambda: ["http://192.168.1.50:11111/"]
        st, h, body = self.get("/qr.svg?i=0")
        self.assertEqual(h["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", body)
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/qr.svg?i=3")

    def test_box_controls_from_the_page(self):
        self._wait(lambda: self.svc.link.up())
        self.assertEqual(self.post("/control/box", b"id=3&value=0", header=False)[0], 403)
        self.assertEqual(self.post("/control/box", b"id=8&value=1")[0], 400)  # gauges are read-only
        self.assertEqual(self.post("/control/box", b"id=x&value=1")[0], 400)
        self.assertEqual(self.post("/control/box", b"id=3&value=0"), (200, "DC4-6 outputs: done"))
        self.assertFalse(self.box.dc46)
        self.assertEqual(self.post("/control/box", b"id=2&value=8.2")[0], 200)
        self.assertEqual(self.box.dc2v, 82)
        st, msg = self.post("/control/box", b"id=2&value=20")
        self.assertEqual(st, 400)
        self.assertIn("out of range", msg)
        self.srv.is_local = lambda ip: False  # phones may use the controls too
        self.assertEqual(self.post("/control/box", b"id=3&value=1")[0], 200)
        self.assertTrue(self.box.dc46)

    def test_box_controls_locked_while_a_client_has_the_switch(self):
        self._wait(lambda: self.svc.link.up())
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.assertTrue(json.loads(self.get("/status.json")[2])["switch_client"])
        st, msg = self.post("/control/box", b"id=3&value=0")
        self.assertEqual(st, 409)
        self.assertIn("unlock", msg)
        self.assertTrue(self.box.dc46)
        self.assertEqual(self.post("/control/box", b"id=3&value=0&override=1")[0], 200)  # the page's Unlock
        self.assertFalse(self.box.dc46)
        self.call("PUT", "/api/v1/observingconditions/0/connected", Connected="True")
        self.call("PUT", "/api/v1/switch/0/connected", Connected="False")
        self.assertEqual(self.post("/control/box", b"id=3&value=1")[0], 200)  # weather only: not locked
        self.assertTrue(self.box.dc46)

    def test_dew_tuning_from_the_page(self):
        self._wait(lambda: self.svc.link.up())
        self.assertEqual(self.post("/setup/dew", b"kp=5", header=False)[0], 403)
        self.assertEqual(self.post("/setup/dew", b"kp=999"), (400, "kp: 0 to 500"))
        self.assertEqual(self.post("/setup/dew", b"min_interval_s=5")[0], 400)  # spares the box's EEPROM
        self.assertEqual(self.post("/setup/dew", b"fallback_pct=30&kp=12.5"), (200, "Saved"))
        self.assertEqual((self.svc.dew.s.fallback_pct, self.svc.dew.s.kp), (30.0, 12.5))  # live, no restart
        saved = Settings.load(self.settings.path).dew
        self.assertEqual((saved.fallback_pct, saved.kp), (30.0, 12.5))
        self.assertEqual(json.loads(self.get("/status.json")[2])["dew"]["tuning"]["fallback_pct"], 30.0)
        self.assertEqual(self.post("/setup/dew", b"fallback_pct=30"), (200, "No change"))
        # a lower power limit caps the manual heater at once
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=1)
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=5, Value=80)
        self.assertEqual(self.box.pwm, P.pct_to_pwm(80))
        self.assertEqual(self.post("/setup/dew", b"max_pct=50")[0], 200)
        self.assertEqual(self.box.pwm, P.pct_to_pwm(50))
        self.assertEqual(self.post("/setup/dew", b"reset=1"), (200, "Back to the defaults"))
        self.assertEqual((self.svc.dew.s.fallback_pct, self.svc.dew.s.kp, self.svc.dew.s.max_pct), (40.0, 20.0, 100.0))
        c = json.loads(self.get("/setup.json")[2])
        self.assertEqual(c["tuning"]["fallback_pct"], {"min": 0, "max": 100, "default": 40.0})
        self.assertEqual([x["id"] for x in c["controls"]], list(range(8)))

    def test_dc3_as_12v_output(self):
        """Mode 4: the DC3 jack as a plain on/off 12 V output, named DC3; no power limit, no probes, left as is on stop."""
        self._wait(lambda: self.svc.link.up())
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitchvalue", Id=4)[1]["Value"], 4)
        self.post("/setup/dew", b"max_pct=40")
        self.post("/setup/probes", b"lens=0&ambient=0")  # needs no probe
        self.assertEqual(self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=4)[1]["ErrorNumber"], 0)
        self.assertEqual(self.box.pwm, 255)  # on, uncapped by the 40 % limit
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=5)[1]["Value"], "DC3 output")
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitchvalue", Id=5)[1]["Value"], 100)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchvalue", Id=5)[1]["Value"], 100.0)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=8)[1]["Value"], "DC3 output power")
        self.call("PUT", "/api/v1/switch/0/setswitch", Id=5, State="false")
        self.assertEqual((self.box.pwm, self.settings.dew.dc3_on), (0, False))
        self.assertEqual(self.post("/control/box", b"id=5&value=100&override=1")[0], 200)  # the On button (unlocked)
        self.assertEqual(self.box.pwm, 255)
        s = json.loads(self.get("/status.json")[2])["dew"]
        self.assertEqual((s["mode"], s["mode_name"], s["dc3_on"], s["state"]), (4, "DC3 (12 V output)", True, "on"))
        self.assertTrue(Settings.load(self.settings.path).dew.dc3_on is True)
        # back to a heater mode: the switch is manual power again, capped by the limit
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=1)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchname", Id=5)[1]["Value"], "Dew manual power")
        self.assertLessEqual(self.box.pwm, P.pct_to_pwm(40))
        # and into mode 4 again: stopping the service leaves a plain output as it is (automatic modes switch off)
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=4)
        self.assertEqual(self.box.pwm, 255)
        self.svc.stop()
        self.assertEqual(self.box.pwm, 255)

    def test_device_name(self):
        """A name of the user's choice for shared networks; the UniqueIDs stay."""
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        uid = self.call("GET", "/management/v1/configureddevices")[1]["Value"][0]["UniqueID"]
        self.assertEqual(self.post("/setup/name", b"name=Roof+box", header=False)[0], 403)
        st, msg = self.post("/setup/name", "name=  Roof   box  ".encode())
        self.assertEqual(st, 200)
        self.assertTrue(msg.startswith("Saved"))
        devs = self.call("GET", "/management/v1/configureddevices")[1]["Value"]
        self.assertEqual([d["DeviceName"] for d in devs], ["Roof box outputs", "Roof box sensors"])
        self.assertEqual(devs[0]["UniqueID"], uid)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/name")[1]["Value"], "Roof box outputs")
        self.assertEqual(Settings.load(self.settings.path).device_name, "Roof box")
        s = json.loads(self.get("/status.json")[2])
        self.assertEqual((s["device_name"], s["device_name_set"]), ("Roof box", "Roof box"))
        self.assertEqual(self.post("/setup/name", ("name=" + "x" * 41).encode())[0], 400)
        self.assertEqual(self.post("/setup/name", b"name=a%3Cb")[0], 400)
        self.assertEqual(self.post("/setup/name", b"name=")[0], 200)  # back to the default
        self.assertEqual(self.call("GET", "/api/v1/switch/0/name")[1]["Value"], "WandererBox Plus V3 outputs")

    def test_settings_only_from_this_pc(self):
        """No password: settings change only on the PC the box is plugged into; the Controls work from phones."""
        self._wait(lambda: self.svc.link.up())
        self.srv.is_local = lambda ip: False
        for path, body in (("/setup/name", b"name=x"), ("/setup/probes", b"lens=1&ambient=1"), ("/setup/dew", b"kp=5"),
                           ("/setup/gauges", b"gauge=lens"), ("/setup/port", b"port=SIM")):
            st, msg = self.post(path, body)
            self.assertEqual(st, 403, path)
            self.assertIn("PC the box is plugged into", msg)
        self.assertEqual(self.settings.device_name, "")
        self.assertEqual(self.post("/control/box", b"id=3&value=0")[0], 200)
        self.assertFalse(json.loads(self.get("/status.json")[2])["local"])

    def test_missing_gauge_form(self):
        self.assertEqual(self.post("/setup/gauges", b"gauge=lens&missing=minimum&missing_value=")[0], 200)
        self.assertEqual((self.settings.missing_gauge, self.settings.missing_value), ("minimum", 0.0))
        self.post("/setup/gauges", b"gauge=lens&missing=value&missing_value=-3.5")
        self.assertEqual((self.settings.missing_gauge, self.settings.missing_value), ("value", -3.5))
        self.assertEqual(json.loads(self.get("/setup.json")[2])["missing"], {"mode": "value", "value": -3.5})
        self.assertEqual(self.post("/setup/gauges", b"gauge=lens&missing=guess")[0], 400)
        self.post("/setup/gauges", b"reset=1")
        self.assertEqual(self.settings.missing_gauge, "hold")
        # NINA scans every switch when it connects and aborts on the first failed read: a probe unplugged at that
        # moment must not fail any switch
        self._wait(lambda: self.svc.link.up())
        self.box.lens_missing = self.box.dht_missing = True
        self._wait(lambda: self.svc.link.status.lens is None and not self.svc.link.status.ambient_ok)
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        names = []
        for i in range(self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"]):
            j = self.call("GET", "/api/v1/switch/0/getswitchvalue", Id=i)[1]
            self.assertEqual(j["ErrorNumber"], 0, (i, j))
            names.append(self.call("GET", "/api/v1/switch/0/getswitchname", Id=i)[1]["Value"])
        self.assertIn("Lens temperature: MISSING", names)
        self.assertIn("Dew point: MISSING", names)

    def test_power_limit_explains_itself(self):
        """Settings capped by the power limit say so, and manual power never sits above its switch maximum."""
        self._wait(lambda: self.svc.link.up())
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=5, Value=80)
        st, msg = self.post("/setup/dew", b"max_pct=40")
        self.assertEqual(st, 200)
        self.assertIn("Power limit: manual power lowered to 40 %", msg)
        self.assertTrue(msg.startswith("Saved"))
        self.assertEqual(self.call("GET", "/api/v1/switch/0/getswitchvalue", Id=5)[1]["Value"], 40.0)
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitchvalue", Id=5)[1]["Value"], 40)
        self.assertIn("power limit", self.call("GET", "/api/v1/switch/0/getswitchdescription", Id=5)[1]["Value"])
        j = self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=5, Value=60)[1]
        self.assertEqual(j["ErrorNumber"], INVALID_VALUE)
        self.assertIn("power limit", j["ErrorMessage"])
        st, msg = self.post("/setup/dew", b"fallback_pct=60")
        self.assertIn("Power limit: fallback capped at 40 %", msg)
        # raising the limit doesn't bring the old 80 % back by surprise
        self.post("/setup/dew", b"max_pct=100")
        self.assertEqual(self.settings.dew.manual_pct, 40.0)
        self.assertNotIn("power limit", self.call("GET", "/api/v1/switch/0/getswitchdescription", Id=5)[1]["Value"])

    def test_probes_not_used(self):
        """A probe the user doesn't use reads as absent everywhere, not as a fault, and nothing needs it."""
        self._wait(lambda: self.svc.link.up())
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.call("PUT", "/api/v1/observingconditions/0/connected", Connected="True")
        n_all = self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"]
        self.assertEqual(self.post("/setup/probes", b"lens=0&ambient=1", header=False)[0], 403)
        self.assertEqual(self.post("/setup/probes", b"lens=maybe&ambient=1")[0], 400)
        # refused while the dew mode needs the probe
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=2)
        st, msg = self.post("/setup/probes", b"lens=0&ambient=1")
        self.assertEqual(st, 400)
        self.assertIn("lens probe", msg)
        self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=1)
        self.assertEqual(self.post("/setup/probes", b"lens=0&ambient=0"), (200, "Saved"))
        self.assertEqual(self.post("/setup/probes", b"lens=0&ambient=0"), (200, "No change"))
        saved = Settings.load(self.settings.path)
        self.assertEqual((saved.use_lens, saved.use_ambient), (False, False))
        s = json.loads(self.get("/status.json")[2])
        self.assertEqual(s["probes"], {"lens": False, "ambient": False})
        self.assertEqual((s["box"]["lens"], s["box"]["ambient"], s["box"]["humidity"], s["box"]["dewpoint"]), (None,) * 4)
        # the modes that need a probe can't be picked
        j = self.call("PUT", "/api/v1/switch/0/setswitchvalue", Id=4, Value=3)[1]
        self.assertEqual(j["ErrorNumber"], INVALID_VALUE)
        self.assertEqual(self.settings.dew.mode, D.MANUAL)
        # gauges for unused probes are left out (the choice is kept for later); ObservingConditions has no DHT22 values
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"], n_all - 4)
        self.assertIn("lens", self.settings.gauges)
        self.assertEqual(self.call("GET", "/api/v1/observingconditions/0/temperature")[1]["ErrorNumber"], NOT_IMPLEMENTED)
        self.assertEqual(self.call("GET", "/api/v1/observingconditions/0/sensordescription", SensorName="Humidity")[1]["ErrorNumber"],
                         NOT_IMPLEMENTED)
        self.assertEqual(self.call("GET", "/api/v1/observingconditions/0/devicestate")[1]["Value"], [])
        g = {x["key"]: x for x in json.loads(self.get("/setup.json")[2])["gauges"]}
        self.assertTrue(g["lens"]["on"])
        self.assertEqual(g["dew_margin"]["needs"], ["lens", "ambient"])
        # saving the gauge form (which can't send the greyed-out ones) keeps their choice
        self.assertEqual(self.post("/setup/gauges", b"gauge=voltage")[0], 200)  # 303 to the page, followed
        self.assertEqual(set(self.settings.gauges), {"voltage", "lens", "ambient", "humidity", "dewpoint"})
        # history: no values for unused probes
        self._wait(lambda: self.svc.history.fine()["rows"])
        row = dict(zip(["t", *self.svc.history.fine()["keys"]], self.svc.history.fine()["rows"][-1]))
        self.assertEqual((row["lens"], row["amb"]), (None, None))
        # back on: everything returns
        self.assertEqual(self.post("/setup/probes", b"lens=1&ambient=1"), (200, "Saved"))
        self.assertAlmostEqual(self.call("GET", "/api/v1/observingconditions/0/humidity")[1]["Value"], 85.0)
        self.call("PUT", "/api/v1/switch/0/connected", Connected="False")
        self.call("PUT", "/api/v1/switch/0/connected", Connected="True")
        self.assertEqual(self.call("GET", "/api/v1/switch/0/maxswitch")[1]["Value"], 8 + 5)


if __name__ == "__main__":
    unittest.main()
