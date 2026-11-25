"""zwoefw's own tests, on the simulated SDK: finding wheels, the Alpaca FilterWheel, the filters' names and offsets,
and what happens on a turn by another program, an error state, a turn that never finishes and an unplug. The hub's
contract tests (tests/test_contract.py) also run it in its own process through the hub."""
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
from zwoefw import driver as D  # noqa: E402
from zwoefw.sim import SimEfw  # noqa: E402

FAST = 0.05             # seconds per slot in these tests


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

    def public_urls(self):
        return []

    def is_local(self, ip):
        return True

    def free_number(self, key):
        return 0


class FindTests(unittest.TestCase):
    def test_lists_wheels_with_their_serials_and_slots(self):
        a, b = SimEfw(serial="AAAA", sdk_id=0, slots=7), SimEfw(serial="BBBB", sdk_id=1, slots=5)
        units = D.ZwoEfwDriver().find(None, [a, b])
        self.assertEqual([(u["serial"], u["slots"]) for u in units], [("AAAA", 7), ("BBBB", 5)])
        self.assertFalse(a.is_open or b.is_open, "find closes what it opened")
        json.dumps(units)                                   # plain values: they cross a pipe

    def test_left_out_while_in_use_elsewhere_or_counting(self):
        a, b = SimEfw(serial="AAAA", sdk_id=0), SimEfw(serial="BBBB", sdk_id=1)
        a.in_use, b.counting = True, True
        d = D.ZwoEfwDriver()
        self.assertEqual(d.find(None, [a, b]), [])
        b.counting = False                                  # done counting: found on the next scan
        self.assertEqual([u["serial"] for u in d.find(None, [a, b])], ["BBBB"])

    def test_no_serial_in_the_firmware_and_unplugged(self):
        a = SimEfw(serial=None, sdk_id=3)
        u = D.ZwoEfwDriver().find(None, [a])[0]
        self.assertEqual((u["serial"], u["has_serial"]), ("EFW-ID3", False))
        a.unplug()
        self.assertEqual(D.ZwoEfwDriver().find(None, [a]), [])

    def test_the_alias_stored_in_the_wheel(self):
        self.assertEqual(D.alias_of("EFW(Main scope)"), "Main scope")
        self.assertIsNone(D.alias_of("EFW"))
        self.assertIsNone(D.alias_of("EFW( )"))
        u = D.ZwoEfwDriver().find(None, [SimEfw(serial="AAAA", name="EFW(Main scope)")])[0]
        self.assertEqual(u["alias"], "Main scope")
        self.assertNotIn("alias", D.ZwoEfwDriver().find(None, [SimEfw(serial="BBBB")])[0])

    def test_simulators(self):
        for name, factory in D.ZwoEfwDriver.simulators.items():
            sim, usb, serial, desc = factory(0)
            self.assertEqual(usb, (0x03C3, 0x1F01), name)
            self.assertIn("EFW", desc)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.saved = (D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S, D.SLOT_LIMIT_S, D.LIMIT_EXTRA_S, D.OURS_S)
        D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S, D.OURS_S = 0.02, 0.05, 0.1, 0.2
        self.dir = tempfile.mkdtemp()
        self.hub = FakeHub(self.dir)
        self.sim = SimEfw(serial="EFW1", sdk_id=0, slots=7, slot_s=FAST, position=0)
        self.start()

    def start(self):
        self.driver = D.ZwoEfwDriver()
        ident = self.driver.find(None, [self.sim])[0]
        self.ctx = HubContext(self.hub, self.driver)
        self.inst = self.driver.start(self.ctx, PortInfo("USB-zwoefw-EFW1", 0x03C3, 0x1F01, "EFW1", "EFW"), ident)
        self.base = f"http://127.0.0.1:{self.inst.local_port}"
        self.assertTrue(wait_for(self.inst.up))

    def tearDown(self):
        self.inst.stop()
        (D.POLL_MOVING_S, D.POLL_IDLE_S, D.RELOCATE_S, D.SLOT_LIMIT_S, D.LIMIT_EXTRA_S, D.OURS_S) = self.saved

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
        return json.loads(self.call(method, f"/api/v1/filterwheel/0/{member}", **form)[1])

    def connect(self):
        self.assertEqual(self.alpaca("PUT", "connected", Connected="True")["ErrorNumber"], 0)

    def status(self):
        return json.loads(self.call("GET", "/status.json")[1])

    def idle(self):
        return wait_for(lambda: self.alpaca("GET", "position")["Value"] >= 0)

    def test_alpaca_filterwheel(self):
        self.assertEqual(self.alpaca("GET", "position")["ErrorNumber"], 0x407, "not connected yet")
        self.connect()
        self.assertEqual(self.alpaca("GET", "names")["Value"], [f"Filter {k}" for k in range(1, 8)])
        self.assertEqual(self.alpaca("GET", "focusoffsets")["Value"], [0] * 7)
        self.assertEqual(self.alpaca("GET", "position")["Value"], 0)
        self.sim.slot_s = 0.3
        self.assertEqual(self.alpaca("PUT", "position", Position="3")["ErrorNumber"], 0)
        self.assertEqual(self.alpaca("GET", "position")["Value"], -1, "-1 while turning, at once")
        self.assertEqual(self.alpaca("PUT", "position", Position="2")["ErrorNumber"], 0x40B, "not while turning")
        self.assertTrue(self.idle())
        self.assertEqual(self.alpaca("GET", "position")["Value"], 3)
        self.assertTrue(wait_for(lambda: self.status()["last_move"]["text"] == "reached slot 4"))
        self.assertIsNotNone(self.status()["slot_time"])
        state = {d["Name"]: d["Value"] for d in self.alpaca("GET", "devicestate")["Value"]}
        self.assertEqual(state["Position"], 3)
        for bad in ("7", "-1"):
            self.assertEqual(self.alpaca("PUT", "position", Position=bad)["ErrorNumber"], 0x401, bad)
        self.assertEqual(self.call("PUT", "/api/v1/filterwheel/0/position")[0], 400, "no Position")
        self.assertEqual(self.alpaca("PUT", "position", Position="3")["ErrorNumber"], 0, "the slot it's at")
        self.assertEqual(self.status()["last_move"]["text"], "already at slot 4")

    def test_the_shorter_way_round_unless_one_way(self):
        self.assertEqual(self.inst.steps(0, 6), 1)
        self.inst.one_way = True
        self.assertEqual(self.inst.steps(0, 6), 6)
        self.assertEqual(self.inst.steps(6, 0), 1)

    def test_filters_names_and_offsets(self):
        n = {f"name{k}": f"F{k}" for k in range(7)}
        n.update({f"offset{k}": str(k * 10) for k in range(7)})
        n["name1"], n["name2"], n["offset6"] = "  Ha 7nm ", "", "-25"
        self.assertEqual(self.call("POST", "/control", op="filters", **n)[0], 200)
        self.connect()
        names = ["F0", "Ha 7nm", "Filter 3", "F3", "F4", "F5", "F6"]
        self.assertEqual(self.alpaca("GET", "names")["Value"], names)
        self.assertEqual(self.alpaca("GET", "focusoffsets")["Value"], [0, 10, 20, 30, 40, 50, -25])
        self.assertEqual(self.call("POST", "/control", op="filters", **dict(n, offset3="x"))[0], 409,
                         "locked while a client is connected, unless the page says override (it does for names)")
        self.assertEqual(self.call("POST", "/control", op="filters", override="1", **dict(n, offset3="x"))[0], 400)
        self.assertEqual(self.call("POST", "/control", op="goto", value="1", override="1")[0], 200)
        self.assertTrue(wait_for(lambda: self.status()["last_move"]["text"] == "reached slot 2 (Ha 7nm)"))
        # kept by serial: a new run reads them back
        self.inst.stop()
        self.start()
        self.assertEqual(self.inst.names(), names)
        self.assertEqual(self.inst.offsets()[6], -25)

    def test_page_controls(self):
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Filters", page)
        self.assertEqual(self.call("POST", "/control", op="goto", value="5")[0], 200)
        self.assertTrue(wait_for(lambda: self.inst.position == 5 and not self.inst.moving))
        self.assertEqual(self.call("POST", "/control", op="goto", value="9")[0], 409)
        self.assertEqual(self.call("POST", "/control", op="goto", value="x")[0], 400)
        self.connect()
        self.assertNotEqual(self.call("POST", "/control", op="goto", value="1")[0], 200,
                            "locked while a client is connected")
        s = self.status()
        for k in ("position", "slots", "names", "offsets", "one_way", "serial", "firmware", "uid"):
            self.assertIn(k, s)
        # the slot is a held value: its exact changes (1-based), never an average, and no gap while turning: the old
        # slot holds until the new one is in place
        self.assertEqual(json.loads(self.call("GET", "/history.json", res="m")[1])["held"], ["pos"])
        changes = lambda: [r[2] for r in json.loads(self.call("GET", "/history.json", res="c")[1])["rows"]]
        self.assertTrue(wait_for(lambda: changes()[-1:] == [6], 5))
        self.assertNotIn(None, changes())

    def test_turned_by_another_program(self):
        time.sleep(1.2)                                     # a history sample at slot 1 first
        self.sim.slot_s = 0.6
        self.sim.turn(3)
        self.assertTrue(wait_for(lambda: self.inst.moving))
        self.assertEqual(self.status()["state"], "being turned by another program")
        self.assertTrue(wait_for(lambda: not self.inst.moving))
        self.assertEqual(self.status()["last_move"], {"text": "turned by another program: slot 1 → slot 4",
                                                      "state": "warn"})
        # a turn of about 1.8 s, sampled every second: the chart's slot goes 1 -> 4 with no gap
        self.assertTrue(wait_for(lambda: [r[2] for r in json.loads(self.call("GET", "/history.json", res="c")[1])["rows"]] == [1, 4], 3))

    def test_error_state_and_a_turn_that_never_finishes(self):
        D.SLOT_LIMIT_S, D.LIMIT_EXTRA_S = 0.01, 0.2
        self.sim.hang()
        self.assertEqual(self.call("POST", "/control", op="goto", value="2")[0], 200)
        self.assertTrue(wait_for(lambda: "not finishing" in self.status()["state"]))
        self.assertEqual(self.status()["state_class"], "bad")
        self.sim.error(code=7)
        self.assertTrue(wait_for(lambda: self.inst.error_state))
        self.assertIn("error state (code 7)", self.status()["state"])
        self.assertEqual(self.inst.summary()["state"], "bad")

    def test_unplugged_then_back(self):
        self.sim.unplug()
        self.assertTrue(wait_for(lambda: not self.inst.up()))
        self.assertIn("not found", self.status()["state"])
        self.sim.plug()
        self.assertTrue(wait_for(self.inst.up))
        self.assertEqual(self.inst.position, 0)


if __name__ == "__main__":
    unittest.main()
