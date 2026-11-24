"""Hub API 1 (REFACTOR.md step 3): UniqueIDs and settings paths, settings carried over from the old hub, which
drivers the hub refuses, probe order, and the simulators drivers declare."""
import json
import os
import sys
import tempfile
import unittest
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # test_hub's bench

from hub import host as H  # noqa: E402
from hub.__main__ import pick_drivers, simulated_ports  # noqa: E402
from hub.api import API_VERSION, Driver, HubContext, unit_of  # noqa: E402
from hub.drivers import all_drivers  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
from test_hub import CH340, FTDI, Bench  # noqa: E402


class Silent:
    """A port whose device says nothing (a box that hasn't started talking yet). Records anything written."""

    def __init__(self, log):
        self.log = log

    def read(self, n=1):
        return b""

    def write(self, data):
        self.log.append(bytes(data))
        return len(data)

    def close(self):
        pass


class Prober(Driver):
    """A careless driver: it would ask anything on a CH340 port."""
    key = "prober"
    usb_ids = frozenset({CH340})
    probe_baud = 9600
    calls = []

    def probe(self, ser):
        Prober.calls.append(ser)
        ser.write(b"HELLO?\n")
        return None


class Order(Driver):
    usb_ids = frozenset({FTDI})
    probe_baud = 9600

    def __init__(self, key, prio):
        self.key, self.probe_priority = key, prio


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bench = Bench()
        self.hubs = []

    def tearDown(self):
        for h in self.hubs:
            h.stop()

    def new_hub(self, drivers=None, **kw):
        h = H.Hub(drivers if drivers is not None else all_drivers(), config_dir=self.dir, bind="127.0.0.1",
                  http_port=0, port_lister=self.bench.lister, opener=self.bench.opener, simulated=True,
                  use_standalone_settings=False, listen_s=0.2, **kw)
        h.start(scan=False)
        self.hubs.append(h)
        return h

    # ---------------------------------------------------------------- IDs and settings
    def test_uid_rule(self):
        h = self.new_hub()
        ctx = HubContext(h, next(d for d in h.drivers if d.key == "wbox"))
        self.assertEqual(ctx.uid("COM15", "switch"),
                         str(uuid.uuid5(uuid.NAMESPACE_URL, "alpaca-hub:wbox:COM15:switch")))
        self.assertNotEqual(ctx.uid("COM15", "switch"), ctx.uid("COM16", "switch"))

    def test_unit_is_device_serial_then_usb_serial_then_port(self):
        p = PortInfo("COM10", *FTDI, "FT123", "x")
        self.assertEqual(unit_of({"serial": "25905"}, p), "25905")
        self.assertEqual(unit_of({}, p), "FT123")
        self.assertEqual(unit_of({}, PortInfo("COM15", *CH340, "", "x")), "COM15")

    def test_box_settings_are_carried_over_once(self):
        """The old hub's first box kept wbox-primary.json. Its dew_mode_adopted must survive the move, or the first
        run would take over the box's dew state again."""
        with open(os.path.join(self.dir, "wbox-primary.json"), "w", encoding="utf-8") as f:
            json.dump({"dew_mode_adopted": True, "device_name": "My box"}, f)
        self.bench.add_box("COM15")
        h = self.new_hub()
        h.scan_once()
        inst = h.instances["box"]
        new = os.path.join(self.dir, "drivers", "wbox", "COM15.json")
        self.assertEqual(inst.svc.settings.path, new)
        with open(new, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["dew_mode_adopted"])
        self.assertTrue(inst.svc.settings.dew_mode_adopted)
        # The UniqueIDs follow the new rule.
        url = f"http://127.0.0.1:{inst.local_port}/management/v1/configureddevices"
        with urllib.request.urlopen(url, timeout=5) as r:
            devs = {d["DeviceType"]: d["UniqueID"] for d in json.loads(r.read())["Value"]}
        self.assertEqual(devs["Switch"], str(uuid.uuid5(uuid.NAMESPACE_URL, "alpaca-hub:wbox:COM15:switch")))

    def test_numbers_from_before_api_1_start_again(self):
        """Old UniqueIDs in hub.json would keep focuser #0 taken; they're cleared once, then kept as before."""
        with open(os.path.join(self.dir, "hub.json"), "w", encoding="utf-8") as f:
            json.dump({"numbers": {"old-nc-focus": ["focuser", 0], "old-nc-aux": ["focuser", 1]}}, f)
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        focus = [r for r in h.device_table() if r["type"] == "focuser"]
        self.assertEqual([r["number"] for r in focus], [0])
        h.stop()
        with open(os.path.join(self.dir, "hub.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["api"], API_VERSION)
        self.assertNotIn("old-nc-focus", saved["numbers"])
        h2 = self.new_hub()
        self.assertEqual(h2.settings.numbers, saved["numbers"], "cleared once only")

    def test_settings_path_copies_only_when_new_is_missing(self):
        h = self.new_hub()
        ctx = h._ctx["nitecrawler"]
        old = os.path.join(self.dir, "old.json")
        with open(old, "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        p = ctx.settings_path("25905", [old])
        self.assertEqual(p, os.path.join(self.dir, "drivers", "nitecrawler", "25905.json"))
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"a": 2}')
        ctx.settings_path("25905", [old])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 2}, "an existing new file is never overwritten")

    # ---------------------------------------------------------------- refusals and order
    def test_driver_that_would_probe_the_box_is_refused_and_never_runs(self):
        Prober.calls = []
        written = []
        self.bench.ports["COM15"] = PortInfo("COM15", *CH340, "", "a box not talking yet")
        orig = self.bench.opener
        self.bench.opener = lambda d, b, lines=False: Silent(written) if d == "COM15" else orig(d, b, lines)
        h = self.new_hub(all_drivers() + [Prober()])
        self.assertIn("prober", h.refused)
        self.assertNotIn("prober", [d.key for d in h.drivers])
        h.scan_once()
        self.assertEqual(Prober.calls, [])
        self.assertEqual(written, [], "nothing may reach a silent CH340 port")

    def test_driver_for_another_api_is_refused(self):
        d = Order("future", 50)
        d.api = API_VERSION + 1
        h = self.new_hub([d])
        self.assertIn("future", h.refused)
        self.assertEqual(h.drivers, [])

    def test_probe_order_follows_priority(self):
        h = self.new_hub([Order("b", 50), Order("a", 200), Order("c", 10)])
        self.assertEqual([d.key for d in h.drivers], ["c", "b", "a"])
        real = self.new_hub()
        probing = [d.key for d in real.drivers if d.probe_baud and FTDI in d.usb_ids]
        self.assertEqual(probing[0], "nitecrawler", "the NiteCrawler is asked first on a shared FTDI chip")

    # ---------------------------------------------------------------- simulators and --drivers
    def test_simulators_come_from_the_drivers(self):
        lister, opener = simulated_ports("nitecrawler,box,panel,lynx,gemini,box-wsm")
        self.assertEqual([p.device for p in lister()], ["SIM1", "SIM2", "SIM3", "SIM4", "SIM5", "SIM6"])
        self.assertEqual(lister()[0].serial, "SIMNC1")
        self.assertEqual([d.key for d in pick_drivers("box,panel,nitecrawler")], ["nitecrawler", "wbox", "wcover"])
        with self.assertRaises(SystemExit):
            simulated_ports("toaster")


if __name__ == "__main__":
    unittest.main()
