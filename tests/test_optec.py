"""Optec FocusLynx / ThirdLynx and Gemini / Hercules in the hub, next to the other devices (their wire formats and
links are tested in drivers/optec/tests).

The simulators answer with the formats printed by Optec's firmware (ThirdLynx 3.0.4, Gemini 2.0.1). None of this has
met real Optec hardware yet."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub import host as H  # noqa: E402
from hub.drivers import all_drivers  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "wcover"))
from wcover.sim import SimV4ec  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "nitecrawler"))
from nitecrawler.sim import SimNiteCrawler  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "optec"))
from optec.sim import SimGemini, SimLynx  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "wbox"))
from wbox.sim import SimBox  # noqa: E402

THIRDLYNX = (0x04D8, 0xED77)
OPTEC_FTDI = (0x0403, 0x6001)
CH340 = (0x1A86, 0x7523)

# What the hub may ever send an Optec hub: questions, moves, halt, home, sync, the rotator's reverse.
LYNX_ALLOWED = {"GETHUBINFO", "GETVER", "GETCONFIG", "GETSTATUS", "MA", "HALT", "HOME", "SCCP"}
GEMINI_ALLOWED = {"GETCFG", "GETSER", "GETSTA", "MOVABS", "MOVEPA", "DOHALT", "DOHOME", "SETREV"}


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class Bench:
    def __init__(self):
        self.ports, self.sims = {}, {}

    def add(self, name, sim, usb, serial=""):
        self.sims[name] = sim
        self.ports[name] = PortInfo(name, *usb, serial, type(sim).__name__)

    def lister(self):
        return list(self.ports.values())

    def opener(self, device, baud, lines=False):
        return self.sims[device].opener(device)


class OptecInTheHub(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bench = Bench()
        self.hubs = []

    def tearDown(self):
        for h in self.hubs:
            h.stop()

    def new_hub(self):
        h = H.Hub(all_drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=self.bench.lister,
                  opener=self.bench.opener, simulated=True, use_standalone_settings=False, listen_s=0.2)
        h.start(scan=False)
        self.hubs.append(h)
        return h

    def call(self, h, method, path, **form):
        form.setdefault("ClientID", "1")
        form.setdefault("ClientTransactionID", "1")
        data = urllib.parse.urlencode(form)
        if method == "GET":
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}{path}?{data}")
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}{path}", method="PUT", data=data.encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def value(self, h, path):
        return self.call(h, "GET", path)["Value"]

    def test_found_next_to_a_nitecrawler_on_the_same_ftdi_family(self):
        self.bench.add("COM4", SimLynx(dev_types=("FA", "FB")), THIRDLYNX, "TL1")
        self.bench.add("COM6", SimGemini(serial="501"), OPTEC_FTDI)
        self.bench.add("COM8", SimNiteCrawler(serial="31", speed=50), OPTEC_FTDI, "FT31")
        h = self.new_hub()
        h.scan_once()
        kinds = {i.port.device: i.driver.key for i in h.instances.values()}
        self.assertEqual(kinds, {"COM4": "focuslynx", "COM6": "gemini", "COM8": "nitecrawler"})
        names = [d["DeviceName"] for d in json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{h.port}/management/v1/configureddevices").read())["Value"]]
        self.assertIn("Optec ThirdLynx F1 (Focuser 1)", names)
        self.assertIn("Optec ThirdLynx F2 (Focuser 2)", names, "both ports have a focuser type: two focusers")
        self.assertIn("Optec Gemini rotator", names)
        for dev in ("COM4", "COM6"):
            self.assertFalse(any(v in ("SCDT", "SETMAX", "SCTE", "SETDEV", "SETTCE") for _, v, _ in self.bench.sims[dev].log))

    def test_page_finds_its_hub_numbers(self):
        """The page labels each axis with the hub's Alpaca number: it finds the axis in hub.json's "alpaca" rows by
        its slug, type and name (device.js alpacaName). Every axis must be there, with the hub's numbers, not the
        driver's own 0."""
        self.bench.add("COM8", SimNiteCrawler(serial="31", speed=50), OPTEC_FTDI, "FT31")
        self.bench.add("COM4", SimLynx(dev_types=("FA", "FB")), THIRDLYNX, "TL1")
        self.bench.add("COM6", SimGemini(serial="501"), OPTEC_FTDI)
        h = self.new_hub()
        h.scan_once()
        rows = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{h.port}/hub.json").read())["alpaca"]
        found = {}
        for inst in h.instances.values():
            if inst.driver.key not in ("gemini", "focuslynx"):
                continue
            for a in inst.axes_status():
                n = [r["number"] for r in rows if r["slug"] == inst.slug and r["type"] == a["kind"] and r["name"] == a["name"]]
                self.assertEqual(len(n), 1, f"{inst.slug} {a['kind']} {a['name']!r} in {rows}")
                found[(inst.driver.key, a["kind"], a["id"])] = n[0]
        focusers = sorted(v for (k, kind, _), v in found.items() if kind == "focuser")
        self.assertEqual(len(focusers), 3, found)                  # two ThirdLynx, one Gemini
        self.assertEqual(len(set(focusers)), 3, "each its own number")
        nc = [r["number"] for r in rows if r["type"] == "focuser" and r["slug"].startswith("nitecrawler")]
        self.assertEqual(len(nc), 1)
        self.assertNotIn(nc[0], focusers, "the NiteCrawler's focuser number isn't one of the Optec ones")

    def test_focuser_through_alpaca(self):
        sim = SimLynx(speed=5000)
        self.bench.add("COM4", sim, THIRDLYNX)
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/focuser/0/"
        self.assertEqual(self.call(h, "PUT", url + "connected", Connected="true")["ErrorNumber"], 0)
        self.assertEqual(self.value(h, url + "maxstep"), 50000)
        self.assertEqual(self.value(h, url + "temperature"), 11.6)
        self.assertEqual(self.call(h, "PUT", url + "move", Position="26000")["ErrorNumber"], 0)
        self.assertTrue(self.value(h, url + "ismoving"))
        self.assertTrue(wait_for(lambda: not self.value(h, url + "ismoving")))
        self.assertEqual(self.value(h, url + "position"), 26000)
        r = self.call(h, "PUT", url + "move", Position="70000")
        self.assertEqual(r["ErrorNumber"], 0x401, "beyond Max Pos is an invalid value")
        self.assertEqual(self.call(h, "PUT", url + "tempcomp", TempComp="true")["ErrorNumber"], 0x400)
        self.assertTrue({v for _, v, _ in sim.log} <= LYNX_ALLOWED, sim.log)

    def test_page_step_center_and_read_only_settings(self):
        """The page's Step and Center move the focuser with the commands Alpaca uses (nothing stored), refuse to go
        past the travel, and the page shows the stored settings without being able to change them."""
        sim = SimGemini(focus_speed=20000.0)
        self.bench.add("COM6", sim, OPTEC_FTDI, "G1")
        h = self.new_hub()
        h.scan_once()
        inst = next(iter(h.instances.values()))
        moving = lambda: inst.unit.is_moving("F")  # noqa: E731
        self.assertEqual(inst.control("step", "F", "500")[0], 200)
        self.assertTrue(wait_for(lambda: not moving()))
        self.assertEqual(inst.unit.focus.position, 12500)
        self.assertEqual(inst.control("step", "F", "-1500")[0], 200)
        self.assertTrue(wait_for(lambda: not moving()))
        self.assertEqual(inst.unit.focus.position, 11000)
        self.assertEqual(inst.control("center", "F", None)[0], 200)
        self.assertTrue(wait_for(lambda: not moving()))
        self.assertEqual(inst.unit.focus.position, 13000, "half of the simulator's 26000")
        with self.assertRaises(ValueError):
            inst.control("step", "F", "20000")     # past the maximum: refused, nothing sent
        self.assertEqual(inst.unit.focus.position, 13000)
        axes = {a["id"]: dict(a["settings"]) for a in inst.axes_status()}
        self.assertEqual(axes["F"]["Backlash compensation"], "off, 40 steps")
        self.assertEqual(axes["F"]["Home at power-up"], "off")
        self.assertEqual(axes["F"]["Temperature compensation"], "off")
        self.assertEqual(axes["R"]["Reverse"], "off")
        self.assertTrue({v for _, v, _ in sim.log} <= GEMINI_ALLOWED, sim.log)

    def test_no_probe_means_no_temperature(self):
        self.bench.add("COM4", SimLynx(temperature=None), THIRDLYNX)
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/focuser/0/"
        self.call(h, "PUT", url + "connected", Connected="true")
        self.assertEqual(self.call(h, "GET", url + "temperature")["ErrorNumber"], 0x400)

    def test_rotator_through_alpaca(self):
        sim = SimGemini(rot_speed=120.0)
        self.bench.add("COM6", sim, OPTEC_FTDI, "G1")
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/rotator/0/"
        self.call(h, "PUT", url + "connected", Connected="true")
        self.assertEqual(self.value(h, url + "mechanicalposition"), 45.0)
        self.assertEqual(self.call(h, "PUT", url + "sync", Position="100")["ErrorNumber"], 0)
        self.assertEqual(self.value(h, url + "position"), 100.0)
        self.assertEqual(self.call(h, "PUT", url + "moveabsolute", Position="130")["ErrorNumber"], 0)
        self.assertTrue(self.value(h, url + "ismoving"))
        self.assertEqual(self.value(h, url + "targetposition"), 130.0)
        self.assertTrue(wait_for(lambda: not self.value(h, url + "ismoving")))
        self.assertAlmostEqual(self.value(h, url + "mechanicalposition"), 75.0)
        self.assertAlmostEqual(self.value(h, url + "position"), 130.0)
        self.assertEqual(self.call(h, "PUT", url + "moveabsolute", Position="360")["ErrorNumber"], 0x401)
        self.assertEqual(self.call(h, "PUT", url + "reverse", Reverse="true")["ErrorNumber"], 0)
        self.assertTrue(self.value(h, url + "reverse"))
        self.assertTrue({v for _, v, _ in sim.log} <= GEMINI_ALLOWED, sim.log)
        # the page's Step and Center (2026-11-23, like Optec's own web pages): plain moves, nothing stored
        inst = next(iter(h.instances.values()))
        self.assertEqual(inst.control("step", "R", "-10")[0], 200)
        self.assertTrue(wait_for(lambda: not self.value(h, url + "ismoving")))
        self.assertAlmostEqual(self.value(h, url + "position"), 120.0)
        self.assertEqual(inst.control("center", "R", None)[0], 200)
        self.assertTrue(wait_for(lambda: not self.value(h, url + "ismoving")))
        self.assertAlmostEqual(self.value(h, url + "mechanicalposition"), 0.0)
        self.assertAlmostEqual(self.value(h, url + "position"), 55.0, msg="Center is mechanical 0°, sky 0 + offset")
        self.assertEqual(self.call(h, "PUT", url + "moveabsolute", Position="130")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: not self.value(h, url + "ismoving")))
        self.assertTrue({v for _, v, _ in sim.log} <= GEMINI_ALLOWED, sim.log)
        # the sky offset survives a restart of the hub
        h.stop()
        h2 = self.new_hub()
        h2.scan_once()
        self.call(h2, "PUT", url + "connected", Connected="true")
        self.assertAlmostEqual(self.value(h2, url + "position"), 130.0)

    def test_optec_drivers_never_claim_the_box_or_panel_chip(self):
        for d in all_drivers():
            if d.key in ("focuslynx", "gemini"):
                self.assertNotIn((0x1A86, 0x7523), d.usb_ids, "CH340: the box stores any byte it gets")

    def test_whole_setup_box_and_panel_untouched_and_working(self):
        box, panel = SimBox(loop_s=0.1), SimV4ec(move_s=0.6, line_s=0.1)
        self.bench.add("COM15", box, CH340)
        self.bench.add("COM5", panel, CH340)
        self.bench.add("COM10", SimNiteCrawler(serial="25905", speed=50), (0x0403, 0x6015), "FT25905")
        self.bench.add("COM4", SimLynx(), THIRDLYNX, "TL1")
        self.bench.add("COM6", SimGemini(), OPTEC_FTDI)
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(i.slug for i in h.instances.values()),
                         ["box", "focuslynx", "gemini", "nitecrawler", "panel"])
        self.assertEqual(box._rx, b"", "nothing may reach the box while looking")
        self.assertEqual(panel.log, [], "nothing may reach the panel while looking")
        # box and panel still work through Alpaca with everything else running
        url = "/api/v1/switch/0/"
        self.call(h, "PUT", url + "connected", Connected="true")
        self.assertEqual(self.value(h, url + "maxswitch"), 16)
        url = "/api/v1/covercalibrator/0/"
        self.call(h, "PUT", url + "connected", Connected="true")
        self.assertTrue(wait_for(lambda: self.value(h, url + "coverstate") == 1))
        self.assertEqual(self.call(h, "PUT", url + "calibratoron", Brightness="80")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: self.value(h, url + "brightness") == 80))
        self.assertEqual(panel.log, [80])
        # and an Optec move doesn't disturb them
        self.call(h, "PUT", "/api/v1/focuser/1/connected", Connected="true")
        self.assertEqual(self.call(h, "PUT", "/api/v1/focuser/1/move", Position="25100")["ErrorNumber"], 0)
        self.assertEqual(panel.log, [80])
        self.assertTrue(h.instances["box"].up() and h.instances["panel"].up())

    def test_unhomed_rotator_refuses_moves_until_homed(self):
        self.bench.add("COM6", SimGemini(rot_homed=False), OPTEC_FTDI)
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/rotator/0/"
        self.call(h, "PUT", url + "connected", Connected="true")
        r = self.call(h, "PUT", url + "moveabsolute", Position="10")
        self.assertEqual(r["ErrorNumber"], 0x40B)
        self.assertIn("home", r["ErrorMessage"])


if __name__ == "__main__":
    unittest.main()
