"""The example thermometer driver's own tests: its protocol, its link on the simulator, and its Alpaca device and page on
a DeviceServer. The hub's contract tests (tests/test_contract.py) also run it in its own process through the hub."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                              # the driver folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))  # the repository (hub)

from hub.api import HubContext  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
from thermo.driver import ThermoDriver  # noqa: E402
from thermo.link import ThermoLink  # noqa: E402
from thermo.protocol import parse_line  # noqa: E402
from thermo.sim import SimThermo  # noqa: E402


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class ProtocolTests(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(parse_line(b"THERMO T1 21.4\r\n"), ("T1", 21.4))
        self.assertIsNone(parse_line(b"THERMO T1 warm\r\n"))
        self.assertIsNone(parse_line("SOMETHING ELSE 1 2"))

    def test_recognised_by_listening(self):
        d = ThermoDriver()
        self.assertEqual(d.recognise(b"noise\r\nTHERMO T7 20.0\r\n"), {"serial": "T7"})
        self.assertIsNone(d.recognise(b"WandererCoverV4A2025\r\n"))


class LinkTests(unittest.TestCase):
    def test_reads_the_simulator_and_sends_nothing(self):
        sim = SimThermo(serial="T2", period_s=0.1)
        link = ThermoLink("SIM", sim.opener)
        link.start()
        try:
            self.assertTrue(wait_for(lambda: link.up() and link.lines >= 2))
            self.assertEqual(link.serial, "T2")
            self.assertAlmostEqual(link.temperature, 15.0, delta=2.5)
            self.assertEqual(bytes(sim.written), b"", "a listening driver never writes")
        finally:
            link.stop()


class FakeHub:
    """Just what HubContext needs, so the instance can run here without a hub."""
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

    def opener_for(self, port, baud):
        return self.sim.opener

    def free_number(self, key):
        return 0


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub(tempfile.mkdtemp())
        self.hub.sim = SimThermo(serial="T3", period_s=0.1)
        d = ThermoDriver()
        self.inst = d.start(HubContext(self.hub, d), PortInfo("SIM1", 0x1209, 0x0001, "", "sim"), {"serial": "T3"})
        self.base = f"http://127.0.0.1:{self.inst.local_port}"
        self.assertTrue(wait_for(self.inst.up))

    def tearDown(self):
        self.inst.stop()

    def call(self, method, path, **form):
        data = urllib.parse.urlencode(form).encode() if method in ("PUT", "POST") else None
        url = self.base + path + ("" if data is not None or not form else "?" + urllib.parse.urlencode(form))
        req = urllib.request.Request(url, data=data, method=method, headers={"X-Hub-Page": "1"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_alpaca_temperature(self):
        code, body = self.call("GET", "/api/v1/observingconditions/0/temperature")
        self.assertEqual(json.loads(body)["ErrorNumber"], 0x407, "not connected yet")
        self.call("PUT", "/api/v1/observingconditions/0/connected", Connected="True")
        t = json.loads(self.call("GET", "/api/v1/observingconditions/0/temperature")[1])["Value"]
        self.assertAlmostEqual(t, 15.0, delta=2.5)
        r = json.loads(self.call("GET", "/api/v1/observingconditions/0/humidity")[1])
        self.assertEqual(r["ErrorNumber"], 0x400, "a sensor it hasn't: not implemented")

    def test_offset_from_the_page_is_saved(self):
        self.assertEqual(self.call("POST", "/control", op="offset", value="1.5")[0], 200)
        self.assertEqual(self.inst.settings.values["offset_c"], 1.5)
        with open(self.inst.settings.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["offset_c"], 1.5)
        self.assertEqual(self.call("POST", "/control", op="offset", value="99")[0], 400)
        s = json.loads(self.call("GET", "/status.json")[1])
        self.assertAlmostEqual(s["temperature"], s["raw"] + 1.5, places=2)

    def test_page(self):
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Calibration", page)


if __name__ == "__main__":
    unittest.main()
