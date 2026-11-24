"""The NiteCrawler driver against its simulator: protocol, link, service, and its Alpaca devices and page on the
hub's DeviceServer. Moved from tests/test_ncalpaca.py with the code (REFACTOR.md step 8)."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                   # the driver folder (its package)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))  # the repository (hub)

from hub.devserver import DeviceServer  # noqa: E402
from nitecrawler import VERSION  # noqa: E402
from nitecrawler import protocol as P  # noqa: E402
from nitecrawler.devices import NCApp, Names, make_devices  # noqa: E402
from nitecrawler.driver import UI  # noqa: E402
from nitecrawler.link import NCLink, Transport, probe  # noqa: E402
from nitecrawler.service import AlpacaError, Service, signed180, wrap360  # noqa: E402
from nitecrawler.settings import Settings  # noqa: E402
from nitecrawler.sim import SimNiteCrawler  # noqa: E402


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class ProtocolTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(P.get_position(1), (b"1GP#", "value"))
        self.assertEqual(P.set_target(2, -1234), (b"2SN -00001234#", "ack"))
        self.assertEqual(P.set_step_delay(3, 7), (b"3SR 7#", "ack"))
        self.assertEqual(P.find_home(3), (b"SH 03#", "ack"))
        self.assertEqual(P.set_brightness(5), (b"PD 5#", "ack"))
        self.assertEqual(P.set_temp_offset(1.5), (b"Pt 15#", None))
        with self.assertRaises(P.ProtocolError):
            P.get_position(4)
        with self.assertRaises(P.ProtocolError):
            P.set_brightness(256)

    def test_dim_delay(self):
        self.assertEqual(P.get_dim_delay(), (b"PW#", "value"))
        self.assertEqual(P.set_dim_delay(0), (b"PW 0#", "ack"))
        self.assertEqual(P.set_dim_delay(30), (b"PW 30#", "ack"))   # unpadded, like PD
        self.assertEqual(P.set_dim_delay(65535), (b"PW 65535#", "ack"))
        for bad in (-1, 65536):
            with self.assertRaises(P.ProtocolError):
                P.set_dim_delay(bad)

    def test_answers(self):
        self.assertEqual(P.parse_int("00012345"), 12345)
        self.assertEqual(P.parse_int("-0001234"), -1234)
        self.assertTrue(P.parse_moving("01"))
        self.assertFalse(P.parse_moving("00"))
        self.assertAlmostEqual(P.parse_tenths("125"), 12.5)
        lim = P.Limits.parse("05")
        self.assertTrue(lim.rot_home and lim.in_limit and not lim.out_limit)
        self.assertEqual(P.split_answers(b"00012#OK##12"), (["00012", "OK", ""], b"12"))
        with self.assertRaises(P.ProtocolError):
            P.parse_int("OK")


class TransportTests(unittest.TestCase):
    def test_probe_and_ask(self):
        nc = SimNiteCrawler()
        ser = nc.opener("SIM")
        self.assertEqual(probe(ser), ("1.6", "3.5 NC"))
        t = Transport(ser)
        self.assertEqual(t.ask(*P.get_position(P.FOCUS)), "00050000")
        self.assertEqual(t.ask(*P.sync(P.FOCUS, 100)), "")
        self.assertEqual(P.parse_int(t.ask(*P.get_position(P.FOCUS))), 100)

    def test_ok_in_the_middle_is_skipped(self):
        nc = SimNiteCrawler()
        ser = nc.opener("SIM")
        t = Transport(ser)
        ser.write(b"1GP#")
        nc._out[:0] = b"OK#"   # homing finished just before the answer
        self.assertEqual(t.read_answer("value", 1.0), "00050000")
        self.assertTrue(t.late_ok)

    def test_silent_port_is_not_a_nitecrawler(self):
        nc = SimNiteCrawler()
        nc.silent = True
        self.assertIsNone(probe(nc.opener("SIM")))


class MathTests(unittest.TestCase):
    def test_wrap(self):
        self.assertEqual(wrap360(-10), 350)
        self.assertEqual(wrap360(720), 0)
        self.assertEqual(signed180(190), -170)
        self.assertEqual(signed180(180), 180)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.tmp, "s.json"))
        self.settings.port = "SIM"
        self.nc = SimNiteCrawler(speed=20)
        self.svc = Service(self.settings, opener=self.nc.opener)
        self.svc.start()
        self.assertTrue(wait_for(self.svc.link.up), self.svc.link.last_error)

    def tearDown(self):
        self.svc.stop()

    def test_dim_delay(self):
        self.svc.status_dict()   # the page's poll starts the read
        self.assertTrue(wait_for(lambda: self.svc.dim_after == 30))
        self.assertIn("PW", self.nc.log)
        self.svc.set_dim_after(0)
        self.assertEqual((self.nc.dim_delay, self.svc.dim_after), (0, 0))
        self.assertIn("PW 0", self.nc.log)
        n = self.nc.log.count("PW")
        self.svc.status_dict()   # read once per connection, not every poll
        self.assertEqual(self.nc.log.count("PW"), n)
        with self.assertRaises(AlpacaError):
            self.svc.set_dim_after(70000)

    def test_dim_delay_stock_firmware(self):
        self.nc.knows_pw = False
        self.svc.status_dict()
        self.assertTrue(wait_for(lambda: "PW" in self.nc.log))
        time.sleep(0.6)
        self.assertIsNone(self.svc.status_dict()["settings"]["dim_after"])
        with self.assertRaises(AlpacaError) as cm:
            self.svc.set_dim_after(30)
        self.assertIn("patched firmware", cm.exception.message)
        for _ in range(4):   # misses don't count against the link
            with self.assertRaises(AlpacaError):
                self.svc.set_dim_after(30)
        self.assertTrue(self.svc.link.up())
        self.assertEqual(self.svc.link.stats["opens"], 1)

    def test_identity(self):
        self.assertEqual(self.svc.link.model, "3.5 NC")
        self.assertEqual(self.svc.link.firmware, "1.6")
        self.assertEqual(self.svc.steps_per_rev, 505960)

    def test_focus_move(self):
        self.svc.focuser_move(P.FOCUS, 52000)
        self.assertTrue(self.svc.is_moving(P.FOCUS))   # true at once, before the next poll
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS) and self.svc.position(P.FOCUS) == 52000))
        self.assertIn("1SN 00052000", self.nc.log)
        self.assertIn("1SM", self.nc.log)

    def test_stopped_means_the_position_is_final(self):
        # Hardware run 2026-11-22: a move to 500 read "not moving" at 488. The poll asked the position, then whether
        # it moved; a move that ended in between left the status at (488, stopped) until the next poll. NINA reads
        # IsMoving, then Position. Here the device's time runs on with every command, so moves end mid-poll.
        class Ticking(SimNiteCrawler):
            def __init__(self):
                self.now = 0.0
                super().__init__(speed=1, clock=lambda: self.now)

            def handle(self, cmd):
                self.now += 0.05          # 150 focus steps per command
                return super().handle(cmd)
        nc = Ticking()
        seen = []
        link = NCLink(port="SIM", opener=nc.opener, on_status=lambda st: seen.append(
            (st.focus.position, st.focus.moving)))
        link.start()
        try:
            self.assertTrue(wait_for(link.up))
            targets = [50000 + (1 if i % 2 else -1) * (180 + 37 * i) for i in range(10)]   # ends land mid-poll
            for target in targets:
                link.goto(P.FOCUS, target)       # sent between two polls: every poll after it sees the move
                n = len(seen)
                self.assertTrue(wait_for(lambda: len(seen) > n + 2 and not seen[-1][1]))
                stopped = [p for p, m in seen[n:] if not m]
                self.assertEqual(set(stopped), {target}, f"stopped at {stopped} on the way to {target}")
        finally:
            link.stop()

    def test_focus_range(self):
        with self.assertRaises(AlpacaError):
            self.svc.focuser_move(P.FOCUS, -1)
        with self.assertRaises(AlpacaError):
            self.svc.focuser_move(P.FOCUS, self.settings.focus_max + 1)

    def test_halt(self):
        self.nc.speed = 0.01
        self.svc.focuser_move(P.FOCUS, 100000)
        self.svc.halt(P.FOCUS)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS)))
        self.assertLess(self.svc.position(P.FOCUS), 100000)

    def test_rotator_absolute_and_sync(self):
        self.svc.rot_move_absolute(90)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.ROTATOR)))
        self.assertAlmostEqual(self.svc.rot_position(), 90, places=2)
        self.assertEqual(self.nc.axes[P.ROTATOR].target, round(505960 / 4))
        self.svc.rot_sync(100)
        self.assertAlmostEqual(self.svc.rot_position(), 100, places=3)
        self.assertAlmostEqual(self.svc.rot_mechanical(), 90, places=2)
        # sky 280 = mechanical 270 = -90: goes the short way through home, never more than half a turn
        self.svc.rot_move_absolute(280)
        self.assertEqual(self.nc.axes[P.ROTATOR].target, -round(505960 / 4))

    def test_rotator_reverse_keeps_sky_angle(self):
        self.svc.rot_move_mechanical(30)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.ROTATOR)))
        before = self.svc.rot_position()
        self.svc.set_reverse(True)
        self.assertAlmostEqual(self.svc.rot_position(), before, places=3)
        self.assertAlmostEqual(self.svc.rot_mechanical(), 330, places=2)

    def test_rotator_relative(self):
        self.svc.rot_move_absolute(10)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.ROTATOR)))
        self.svc.rot_move_relative(-20)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.ROTATOR)))
        self.assertAlmostEqual(self.svc.rot_position(), 350, places=2)

    def test_unwinds_far_rotator(self):
        self.nc.axes[P.ROTATOR].pos = 505960 * 2 + 1000.0
        self.nc.axes[P.ROTATOR].target = self.nc.axes[P.ROTATOR].new = 505960 * 2 + 1000
        self.assertTrue(wait_for(lambda: self.svc.link.status.rotator.position > 505960))
        self.svc.rot_move_mechanical(10)
        self.assertTrue(any(c.startswith("2SP 00001000") for c in self.nc.log))

    def test_homing_blocks_other_commands(self):
        self.nc.speed = 0.5
        self.svc.find_home(P.HOME_FOCUS)
        self.assertTrue(self.svc.link.homing())
        with self.assertRaises(AlpacaError):
            self.svc.focuser_move(P.FOCUS, 10)
        n = len(self.nc.log)
        time.sleep(0.6)
        self.assertEqual(len(self.nc.log), n, "nothing is sent while homing")
        self.nc.speed = 50
        self.assertTrue(wait_for(lambda: not self.svc.link.homing(), 10))
        self.assertTrue(wait_for(lambda: self.svc.position(P.FOCUS) == 0))

    def test_homing_can_be_stopped(self):
        self.nc.speed = 0.01
        self.svc.find_home(P.HOME_FOCUS)
        self.svc.halt(P.FOCUS)
        self.assertTrue(wait_for(lambda: not self.svc.link.homing()))

    def test_step_rates_read_with_gr(self):
        self.assertTrue(wait_for(lambda: self.svc.link.status.focus.step_delay is not None))
        self.assertIn("1GR", self.nc.log)
        self.assertNotIn("1SR", self.nc.log)   # "xSR#" is not a read

    def test_temperature_and_status(self):
        self.assertTrue(wait_for(lambda: self.svc.link.status.temperature is not None))
        d = self.svc.status_dict()
        self.assertTrue(d["up"])
        self.assertEqual(d["voltage"], 12.3)
        self.assertIn("sky", d["rotator"])

    def test_link_recovers_after_silence(self):
        self.nc.silent = True
        self.assertTrue(wait_for(lambda: not self.svc.link.up(), 10))
        self.nc.silent = False
        self.assertTrue(wait_for(self.svc.link.up, 10))

    def test_backlash_off_by_default(self):
        self.assertEqual(self.settings.focus_backlash, 0)
        self.svc.focuser_move(P.FOCUS, 5000)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS)))
        n = len(self.nc.log)
        self.svc.focuser_move(P.FOCUS, 3000)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS) and self.svc.position(P.FOCUS) == 3000))
        self.assertEqual([c for c in self.nc.log[n:] if c.startswith("1SN")], ["1SN 00003000"])

    def test_backlash_inward_overshoots_then_returns(self):
        self.settings.focus_backlash = 500
        self.svc.focuser_move(P.FOCUS, 5000)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS) and self.svc.position(P.FOCUS) == 5000, 10))
        n = len(self.nc.log)
        self.svc.focuser_move(P.FOCUS, 6000)          # outward: one leg
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS), 10))
        self.svc.focuser_move(P.FOCUS, 3000)          # inward: 2500, then back out to 3000
        self.assertTrue(self.svc.is_moving(P.FOCUS))
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS), 10))
        self.assertEqual(self.svc.position(P.FOCUS), 3000)
        self.assertEqual([c for c in self.nc.log[n:] if c.startswith("1SN")], ["1SN 00006000", "1SN 00002500", "1SN 00003000"])

    def test_backlash_halt_cancels_return_leg(self):
        self.settings.focus_backlash = 500
        self.nc.axes[P.FOCUS].pos = 5000.0
        self.nc.axes[P.FOCUS].target = self.nc.axes[P.FOCUS].new = 5000
        self.assertTrue(wait_for(lambda: self.svc.position(P.FOCUS) == 5000))
        self.nc.speed = 0.01
        self.svc.focuser_move(P.FOCUS, 3000)
        self.svc.halt(P.FOCUS)
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.FOCUS)))
        time.sleep(0.5)
        self.assertNotIn("1SN 00003000", self.nc.log)

    def test_one_missed_answer_keeps_the_port(self):
        opens = self.svc.link.stats["opens"]
        polls = self.svc.link.stats["polls"]
        self.nc.drop_next = 1
        self.assertTrue(wait_for(lambda: self.svc.link.stats["polls"] >= polls + 2, 10))
        self.assertEqual(self.svc.link.stats["timeouts"], 1)
        self.assertEqual(self.svc.link.stats["opens"], opens, "the port was closed after a single missed answer")
        self.assertTrue(self.svc.link.up())

    def test_settings_saved(self):
        self.svc.rot_sync(45)
        s = Settings.load(self.settings.path)
        self.assertAlmostEqual(s.rot_offset, self.settings.rot_offset)


class AlpacaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.settings = Settings(os.path.join(cls.tmp, "s.json"))
        cls.settings.port = "SIM"
        cls.nc = SimNiteCrawler(speed=20)
        cls.svc = Service(cls.settings, opener=cls.nc.opener)
        names = Names(cls.svc)
        cls.devices = make_devices(cls.svc, names, lambda k: f"uid-{k}")
        cls.srv = DeviceServer(cls.devices, NCApp(cls.svc, names, cls.devices, UI, "SIM"), VERSION)
        cls.srv.start()
        cls.svc.start()
        assert wait_for(cls.svc.link.up)
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.svc.stop()

    def get(self, path, **q):
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())

    def put(self, path, **form):
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(form).encode(), method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, headers=True, **form):
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(form).encode(), method="POST",
                                     headers={"X-NCAlpaca": "1"} if headers else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_configured_devices(self):
        devs = self.get("/management/v1/configureddevices")["Value"]
        self.assertEqual([(d["DeviceType"], d["DeviceNumber"]) for d in devs], [("Focuser", 0), ("Rotator", 0)])
        self.assertEqual(len({d["UniqueID"] for d in devs}), 2)

    def test_not_connected(self):
        self.put("/api/v1/focuser/0/connected", Connected="False")
        r = self.get("/api/v1/focuser/0/position", ClientTransactionID=7)
        self.assertEqual(r["ErrorNumber"], 0x407)
        self.assertEqual(r["ClientTransactionID"], 7)

    def test_focuser_flow(self):
        self.assertEqual(self.put("/api/v1/focuser/0/connected", Connected="True")["ErrorNumber"], 0)
        self.assertTrue(self.get("/api/v1/focuser/0/absolute")["Value"])
        self.assertEqual(self.get("/api/v1/focuser/0/interfaceversion")["Value"], 4)
        self.assertEqual(self.put("/api/v1/focuser/0/move", Position="51000")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: self.get("/api/v1/focuser/0/position")["Value"] == 51000))
        self.assertEqual(self.put("/api/v1/focuser/0/move", Position="-5")["ErrorNumber"], 0x401)
        self.assertEqual(self.put("/api/v1/focuser/0/tempcomp", TempComp="True")["ErrorNumber"], 0x400)
        self.assertEqual(self.get("/api/v1/focuser/0/temperature")["ErrorNumber"], 0)
        ds = {x["Name"]: x["Value"] for x in self.get("/api/v1/focuser/0/devicestate")["Value"]}
        self.assertIn("Position", ds)

    def test_rotator_flow(self):
        self.put("/api/v1/rotator/0/connected", Connected="True")
        self.assertEqual(self.put("/api/v1/rotator/0/moveabsolute", Position="45")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: not self.get("/api/v1/rotator/0/ismoving")["Value"]))
        self.assertAlmostEqual(self.get("/api/v1/rotator/0/position")["Value"], 45, places=2)
        self.assertEqual(self.put("/api/v1/rotator/0/moveabsolute", Position="360")["ErrorNumber"], 0x401)
        self.assertAlmostEqual(self.get("/api/v1/rotator/0/targetposition")["Value"], 45, places=2)

    def test_aux_hidden_until_enabled(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/api/v1/focuser/1/name")
        self.settings.aux_enabled = True
        try:
            self.assertIn("aux", self.get("/api/v1/focuser/1/name")["Value"])
        finally:
            self.settings.aux_enabled = False

    def test_page_and_status(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"NiteCrawler", r.read())
        d = self.get("/status.json")
        self.assertTrue(d["up"])
        self.assertEqual(d["model"], "3.5 NC")

    def test_page_controls(self):
        self.assertEqual(self.post("/control/move", headers=False, op="goto", axis="focus", value="1")[0], 403)
        self.devices[0].connected = True
        try:
            self.assertEqual(self.post("/control/move", op="goto", axis="focus", value="50500")[0], 409)
            code, _ = self.post("/control/move", op="goto", axis="focus", value="50500", override="1")
            self.assertEqual(code, 200)
            self.assertEqual(self.post("/control/move", op="haltall")[0], 200)  # stop is never locked
        finally:
            self.devices[0].connected = False

    def test_set_position_uses_moonlite_sp(self):
        code, _ = self.post("/control/move", op="setpos", axis="focus", value="1234", override="1")
        self.assertEqual(code, 200)
        self.assertIn("1SP 00001234", self.nc.log)
        self.assertTrue(wait_for(lambda: self.svc.position(P.FOCUS) == 1234))
        self.assertEqual(self.post("/control/move", op="setpos", axis="focus", value="-5", override="1")[0], 400)
        code, _ = self.post("/control/move", op="setpos", axis="aux", value="77", override="1")
        self.assertEqual(code, 200)
        self.assertIn("3SP 00000077", self.nc.log)

    def test_rotator_count_keeps_sky_angle(self):
        self.assertTrue(wait_for(lambda: not self.svc.is_moving(P.ROTATOR)))
        sky = self.svc.rot_position()
        code, _ = self.post("/control/move", op="setpos", axis="rotator", value="1000", override="1")
        self.assertEqual(code, 200)
        self.assertIn("2SP 00001000", self.nc.log)
        self.assertTrue(wait_for(lambda: self.svc.link.status.rotator.position == 1000))
        self.assertAlmostEqual(signed180(self.svc.rot_position() - sky), 0, places=3)
        self.assertEqual(self.post("/control/move", op="setpos", axis="rotator", value="400000", override="1")[0], 400)

    def test_setup_motion(self):
        code, _ = self.post("/setup/motion", focus_max="123456", aux_max="999", rot_reverse="0", aux_enabled="0")
        self.assertEqual(code, 200)
        self.assertEqual(self.settings.focus_max, 123456)
        self.assertEqual(self.post("/setup/motion", focus_max="0", aux_max="1")[0], 400)

    def test_setup_display_dim_after(self):
        self.assertTrue(wait_for(lambda: self.get("/status.json")["settings"]["dim_after"] == self.nc.dim_delay))
        n = self.nc.log.count("PW")
        self.assertEqual(self.post("/setup/display", brightness="100", sleep="10")[0], 200)
        self.assertEqual(self.nc.log.count("PW"), n)   # no dim_after: PW isn't sent
        self.assertEqual(self.post("/setup/display", brightness="100", sleep="10", dim_after="45")[0], 200)
        self.assertEqual(self.nc.dim_delay, 45)
        self.assertEqual(self.get("/status.json")["settings"]["dim_after"], 45)
        self.assertEqual(self.post("/setup/display", brightness="100", sleep="10", dim_after="65536")[0], 400)
        self.assertEqual(self.post("/setup/display", brightness="100", sleep="10", dim_after="x")[0], 400)
        self.nc.knows_pw = False
        try:
            code, msg = self.post("/setup/display", brightness="90", sleep="10", dim_after="20")
        finally:
            self.nc.knows_pw = True
        self.assertEqual(code, 400)
        self.assertIn("patched firmware", msg)
        self.assertEqual(self.nc.brightness, 90)   # brightness still went out


if __name__ == "__main__":
    unittest.main()
