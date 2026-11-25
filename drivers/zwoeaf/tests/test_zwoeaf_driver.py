"""zwoeaf's own tests, on the simulated SDK: finding units, the Alpaca Focuser, the page, and what happens on a stall,
a move that never finishes, the hand controller, an unplug and a stop mid-move. The hub's contract tests
(tests/test_contract.py) also run it in its own process through the hub."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                              # the driver folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))           # the repository (hub)

from hub.api import HubContext  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
from zwoeaf import driver as D  # noqa: E402
from zwoeaf.sim import SimEaf, SimSdk  # noqa: E402

FAST = 4000.0           # steps/s in these tests


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class FakeHub:
    """Just what HubContext needs, so instances run here without a hub."""
    def __init__(self, folder):
        self.config_dir, self.simulated = folder, True
        self.use_standalone_settings = False

        class S:
            primary = {}
        self.settings = S()
        self.n = 0

    def public_urls(self):
        return []

    def is_local(self, ip):
        return True

    def free_number(self, key):
        return self.n


class FindTests(unittest.TestCase):
    def test_lists_units_with_their_serials(self):
        a, b = SimEaf(serial="AAAA", sdk_id=0), SimEaf(serial="BBBB", sdk_id=1)
        d = D.ZwoEafDriver()
        units = d.find(None, [a, b])
        self.assertEqual([u["serial"] for u in units], ["AAAA", "BBBB"])
        self.assertEqual(units[0]["name"], "EAF")
        self.assertFalse(a.is_open or b.is_open, "find closes what it opened")
        json.dumps(units)                                   # plain values: they cross a pipe

    def test_a_unit_in_use_elsewhere_is_left_out(self):
        a = SimEaf(serial="AAAA", sdk_id=0)
        a.in_use = True
        self.assertEqual(D.ZwoEafDriver().find(None, [a]), [])

    def test_no_serial_in_the_firmware(self):
        a = SimEaf(serial=None, sdk_id=3)
        u = D.ZwoEafDriver().find(None, [a])[0]
        self.assertEqual(u["serial"], "EAF-ID3")
        self.assertFalse(u["has_serial"])

    def test_unplugged_units_are_not_listed(self):
        a = SimEaf(serial="AAAA", sdk_id=0)
        d = D.ZwoEafDriver()
        a.unplug()
        self.assertEqual(d.find(None, [a]), [])

    def test_simulators(self):
        for name, factory in D.ZwoEafDriver.simulators.items():
            sim, usb, serial, desc = factory(0)
            self.assertEqual(usb[0], 0x03C3, name)
            self.assertIn("EAF", desc)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.saved = (D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S, D.MIN_SPEED, D.LIMIT_EXTRA_S)
        D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S = 0.05, 0.1, 0.2
        self.hub = FakeHub(tempfile.mkdtemp())
        self.sim = SimEaf(serial="EAF1", sdk_id=0, speed=FAST, max_step=20000, position=1000)
        self.driver = D.ZwoEafDriver()
        ident = self.driver.find(None, [self.sim])[0]
        self.ctx = HubContext(self.hub, self.driver)
        self.inst = self.driver.start(self.ctx, PortInfo("USB-zwoeaf-EAF1", 0x03C3, 0x1F10, "EAF1", "EAF"), ident)
        self.base = f"http://127.0.0.1:{self.inst.local_port}"
        self.assertTrue(wait_for(self.inst.up))

    def tearDown(self):
        self.inst.stop()
        D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S, D.MIN_SPEED, D.LIMIT_EXTRA_S = self.saved

    def call(self, method, path, **form):
        data = urllib.parse.urlencode(form).encode() if method in ("PUT", "POST") else None
        url = self.base + path + ("" if data is not None or not form else "?" + urllib.parse.urlencode(form))
        req = urllib.request.Request(url, data=data, method=method, headers={"X-Hub-Page": "1"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def alpaca(self, method, member, **form):
        return json.loads(self.call(method, f"/api/v1/focuser/0/{member}", **form)[1])

    def connect(self):
        self.assertEqual(self.alpaca("PUT", "connected", Connected="True")["ErrorNumber"], 0)

    def status(self):
        return json.loads(self.call("GET", "/status.json")[1])

    def test_holds_the_unit_and_find_still_lists_it(self):
        self.assertTrue(self.sim.is_open)
        self.assertEqual([u["serial"] for u in self.driver.find(None, [self.sim])], ["EAF1"])
        self.assertTrue(self.sim.is_open, "find leaves a held unit alone")

    def test_alpaca_focuser(self):
        self.assertEqual(self.alpaca("GET", "position")["ErrorNumber"], 0x407, "not connected yet")
        self.connect()
        self.assertTrue(self.alpaca("GET", "absolute")["Value"])
        self.assertEqual(self.alpaca("GET", "maxstep")["Value"], 20000)
        self.assertEqual(self.alpaca("GET", "maxincrement")["Value"], 20000)
        self.assertEqual(self.alpaca("GET", "position")["Value"], 1000)
        self.assertFalse(self.alpaca("GET", "tempcompavailable")["Value"])
        self.assertEqual(self.alpaca("GET", "stepsize")["ErrorNumber"], 0x400)
        self.assertAlmostEqual(self.alpaca("GET", "temperature")["Value"], 12.5)
        self.assertEqual(self.alpaca("PUT", "move", Position="3000")["ErrorNumber"], 0)
        self.assertTrue(self.alpaca("GET", "ismoving")["Value"])
        self.assertTrue(wait_for(lambda: not self.alpaca("GET", "ismoving")["Value"]))
        self.assertEqual(self.alpaca("GET", "position")["Value"], 3000)
        self.assertEqual(self.status()["last_move"]["text"], "reached 3000")
        # beyond either end: to that end, no error (ConformU's "Move - Above MaxStep" / "Move - Below 0")
        self.assertEqual(self.alpaca("PUT", "move", Position="20010")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: not self.alpaca("GET", "ismoving")["Value"]))
        self.assertEqual(self.alpaca("GET", "position")["Value"], 20000)
        self.assertEqual(self.alpaca("PUT", "move", Position="-10")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: not self.alpaca("GET", "ismoving")["Value"]))
        self.assertEqual(self.alpaca("GET", "position")["Value"], 0)
        self.assertEqual(self.call("PUT", "/api/v1/focuser/0/move")[0], 400, "no Position")

    def test_a_move_ending_between_the_polls_two_reads(self):
        # 2026-11-24, ConformU through the installed hub: each SDK call takes up to 1.7 s on a real EAF, and the poll
        # read the position before IsMoving, so a move ending between the two left "not moving at 7506" for 7570.
        class Finishing:
            """The move ends during whichever of the two reads comes first."""
            def __init__(self, real):
                self.real, self.done = real, False

            def is_moving(self, id_):
                self.done = True
                return False, False

            def position(self, id_):
                was, self.done = self.done, True
                return 7570 if was else 7506

            def __getattr__(self, k):
                return getattr(self.real, k)

        self.inst._stop.set()                     # no polling thread: only the poll below
        time.sleep(0.2)
        self.inst.sdk = Finishing(self.inst.sdk)
        with self.inst._lock:
            self.inst.move = {"from": 5600, "target": 7570, "t0": time.monotonic() - 5.0, "by": "Alpaca"}
            self.inst.moving_flag = True
        self.inst.poll()
        self.assertEqual((self.inst.position, self.inst.moving_flag), (7570, False))
        self.assertEqual(self.inst.last_move["text"], "reached 7570")

    def test_reopened_when_its_calls_keep_failing(self):
        # 2026-11-25: after the driver's update restart an EFW answered every call with an undocumented error (11)
        # until it was reopened; an error that doesn't say "gone" now counts too, after FAILS_TO_REOPEN polls.
        from zwoeaf.eaf import EafError

        class Broken:
            def __init__(self, real):
                self.real, self.on = real, True

            def __getattr__(self, k):
                f = getattr(self.real, k)
                if k in ("position", "is_moving") and self.on:
                    def fail(*a):
                        raise EafError(99, k)
                    return fail
                return f

        b = self.inst.sdk = Broken(self.inst.sdk)
        self.assertTrue(wait_for(lambda: "opening it again" in (self.inst.lost or ""), 5))
        self.assertIn("error 99", self.inst.lost)
        self.assertFalse(self.inst.up())
        b.on = False                                       # it answers again: back by itself, same tab
        self.assertTrue(wait_for(self.inst.up, 10))
        self.assertIsNone(self.inst.lost)

    def test_halt(self):
        self.connect()
        self.sim.speed = 500.0
        self.alpaca("PUT", "move", Position="15000")
        time.sleep(0.3)
        self.assertEqual(self.alpaca("PUT", "halt")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: not self.alpaca("GET", "ismoving")["Value"]))
        pos = self.alpaca("GET", "position")["Value"]
        self.assertLess(pos, 15000)
        self.assertTrue(wait_for(lambda: self.status()["last_move"]["text"] == f"halted at {pos}"))

    def test_stall_reported_by_the_firmware(self):
        self.connect()
        self.sim.jam(at=2000)
        self.alpaca("PUT", "move", Position="5000")
        self.assertTrue(wait_for(lambda: self.inst.stalled is not None))
        self.assertEqual(self.inst.stalled, 2000)
        s = self.status()
        self.assertEqual(s["state_class"], "bad")
        self.assertIn("stalled at 2000", s["state"])
        self.assertIn("E5", s["last_move"]["text"])
        self.assertEqual(self.inst.summary()["state"], "bad")
        # a new move clears it (here: back, away from the jam)
        self.alpaca("PUT", "move", Position="1500")
        self.assertTrue(wait_for(lambda: self.inst.position == 1500 and not self.inst.moving_flag))
        self.assertIsNone(self.inst.stalled)
        self.assertEqual(self.inst.state()[1], "ok")

    def test_a_move_that_never_finishes_is_stopped(self):
        D.MIN_SPEED, D.LIMIT_EXTRA_S = 10000.0, 0.5
        self.connect()
        self.sim.hang()
        self.alpaca("PUT", "move", Position="5000")
        self.assertTrue(wait_for(lambda: self.inst.last_move and "not finishing" in self.inst.last_move["text"]))
        self.assertGreaterEqual(self.sim.stops, 1, "EAFStop sent")
        self.assertEqual(self.inst.state()[1], "bad")

    def test_hand_controller(self):
        self.connect()
        self.sim.speed = 200.0
        self.sim.hand_move(5000)
        self.assertTrue(wait_for(lambda: self.inst.hand))
        self.assertTrue(self.alpaca("GET", "ismoving")["Value"])
        self.assertEqual(self.alpaca("PUT", "move", Position="100")["ErrorNumber"], 0x40B)
        code, text = self.call("POST", "/control", op="goto", value="100", override="1")
        self.assertEqual(code, 409)
        self.assertIn("hand controller", text)
        self.assertIn("hand controller", self.status()["state"])
        self.assertEqual(self.status()["last_move"]["text"], "being moved by the hand controller")
        self.assertTrue(wait_for(lambda: not self.inst.moving_flag, 40))
        self.assertEqual(self.status()["last_move"], {"text": "moved by the hand controller: 1000 → 5000",
                                                      "state": "ok"})

    def test_moved_by_another_program(self):
        # 2026-11-24: ZWO's ASCOM driver opened and moved the EAF while the hub held it; the page showed nothing.
        self.sim.speed = 500.0
        self.sim.move(3000)                       # straight on the focuser, as another program does
        self.assertTrue(wait_for(lambda: self.inst.moving_flag))
        self.assertEqual(self.status()["last_move"]["text"], "being moved by another program")
        self.assertEqual(self.status()["state"], "being moved by another program")
        self.assertTrue(wait_for(lambda: not self.inst.moving_flag))
        self.assertEqual(self.status()["last_move"], {"text": "moved by another program: 1000 → 3000",
                                                      "state": "warn"})
        # too quick for a poll to see it moving: still noticed from the position
        self.sim.speed = 1e6
        self.sim.move(3010)
        self.assertTrue(wait_for(lambda: self.status()["last_move"]["text"] == "moved by another program: 3000 → 3010"))
        # our own moves stay ours
        self.assertEqual(self.call("POST", "/control", op="goto", value="2000")[0], 200)
        self.assertTrue(wait_for(lambda: self.status()["last_move"]["text"] == "reached 2000"))
        time.sleep(0.5)
        self.assertEqual(self.status()["last_move"]["text"], "reached 2000")

    def test_unplugged_mid_move_then_back(self):
        self.connect()
        self.sim.speed = 500.0
        self.alpaca("PUT", "move", Position="15000")
        time.sleep(0.2)
        self.sim.unplug()
        self.assertTrue(wait_for(lambda: self.inst.id is None))
        self.assertIn("lost during the move", self.inst.last_move["text"])
        self.assertEqual(self.alpaca("GET", "position")["ErrorNumber"], 0x500)
        self.assertFalse(self.inst.up())
        self.assertEqual(self.inst.summary()["state"], "bad")
        self.assertEqual(self.sim.moves, [15000], "not retried")
        self.sim.plug()
        self.assertTrue(wait_for(lambda: self.inst.up(), 5), "back by itself")
        self.assertEqual(self.alpaca("GET", "position")["ErrorNumber"], 0)

    def test_stop_mid_move_stops_the_motor(self):
        self.connect()
        self.sim.speed = 300.0
        self.alpaca("PUT", "move", Position="15000")
        time.sleep(0.2)
        self.inst.stop()
        self.assertGreaterEqual(self.sim.stops, 1)
        self.assertIsNone(self.sim._target, "the motor stopped")
        self.assertFalse(self.sim.is_open, "let go of")

    def test_page_controls(self):
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Controls", page)
        self.assertEqual(self.call("POST", "/control", op="goto", value="2500")[0], 200)
        self.assertTrue(wait_for(lambda: self.inst.position == 2500 and not self.inst.moving_flag))
        self.assertEqual(self.call("POST", "/control", op="step", value="-500")[0], 200)
        self.assertTrue(wait_for(lambda: self.inst.position == 2000 and not self.inst.moving_flag))
        self.assertEqual(self.call("POST", "/control", op="goto", value="lots")[0], 400)
        self.assertEqual(self.call("POST", "/control", op="goto", value="99999")[0], 409)
        self.connect()
        self.assertNotEqual(self.call("POST", "/control", op="goto", value="100")[0], 200,
                            "locked while a client is connected")
        self.assertEqual(self.call("POST", "/control", op="halt", override="1")[0], 200)
        s = self.status()
        for k in ("position", "max_step", "reverse", "backlash", "serial", "firmware", "codes", "uid"):
            self.assertIn(k, s)
        self.assertEqual(s["max_step"], 20000)
        self.assertTrue(s["codes"])


class OldFirmwareTests(unittest.TestCase):
    def test_no_motor_codes(self):
        saved = D.POLL_IDLE_S
        D.POLL_IDLE_S = 0.05
        sim = SimEaf(serial="OLD", codes=False, speed=FAST)
        d = D.ZwoEafDriver()
        ident = d.find(None, [sim])[0]
        inst = d.start(HubContext(FakeHub(tempfile.mkdtemp()), d), PortInfo("USB-zwoeaf-OLD", 0x03C3, 1, "OLD", ""),
                       ident)
        try:
            self.assertTrue(wait_for(lambda: inst.codes is False))
            self.assertTrue(inst.up())
        finally:
            inst.stop()
            D.POLL_IDLE_S = saved


if __name__ == "__main__":
    unittest.main()
