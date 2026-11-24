"""Installed drivers (REFACTOR.md step 6): the hub notices a driver installed, updated or uninstalled while it runs,
and needs no restart. An update waits while a client is connected to the driver's devices, unless "Switch now".
Turned off on Overview, a driver stops and stays off. A driver for another hub API or Python is refused."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from hub import host as H  # noqa: E402
from hub import supervisor as S  # noqa: E402
from hub.catalog import Catalog  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
from test_supervisor import ThermoPort, wait_for  # noqa: E402

USB = (0x1234, 0x0003)


def install(root, version, key="thermo", api=1, python=None, json_last=True, title=None, min_hub=None):
    """What a driver installer does: the package first, driver.json last."""
    vdir = os.path.join(root, key, version)
    pkg = os.path.join(vdir, key)
    os.makedirs(pkg, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fakedrv_common.py"), os.path.join(pkg, "common.py"))
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(pkg, "driver.py"), "w") as f:
        f.write(f"from .common import make\n\nd = make({key!r}, {USB!r})\nd.title = 'Thermometer v{version}'\n"
                f"DRIVERS = [d]\n")
    meta = {"key": key, "title": title or "Thermometer", "version": version, "api": api}
    if python:
        meta["python"] = python
    if min_hub:
        meta["min_hub"] = min_hub
    if json_last:
        with open(os.path.join(vdir, "driver.json"), "w") as f:
            json.dump(meta, f)
    return vdir


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (S.PING_S, S.T_PING, S.WATCH_S)
        S.PING_S, S.T_PING, S.WATCH_S = 0.5, 0.5, 0.3

    @classmethod
    def tearDownClass(cls):
        S.PING_S, S.T_PING, S.WATCH_S = cls.saved

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "drivers")
        os.makedirs(self.root)
        self.opened = []
        self.handles = {}
        self.ports = {"COM71": PortInfo("COM71", *USB, "", "thermometer")}
        self.sup = S.Supervisor(config_dir=self.dir, simulated=True,
                                catalog=Catalog(installed=self.root, source="", builtin=False))
        self.hub = H.Hub(self.sup.drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0,
                         port_lister=lambda: list(self.ports.values()), opener=self.opener, simulated=True,
                         use_standalone_settings=False, listen_s=0.3, supervisor=self.sup)
        self.hub.start(scan=False)

    def tearDown(self):
        self.hub.stop()

    def opener(self, device, baud, lines=False):
        self.opened.append(device)
        p = self.handles[device] = ThermoPort(device[-2:])
        return p

    def drivers(self):
        return {d["name"]: d for d in self.get("/hub/drivers")}

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.hub.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode())

    def post(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}{path}", method="POST", data=b"",
                                     headers={"X-Hub-Page": "1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def put(self, path, **form):
        req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}{path}", method="PUT",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def installed_and_found(self, version="1.0.0"):
        install(self.root, version)
        self.assertTrue(wait_for(lambda: self.drivers().get("thermo", {}).get("state") == "running", 20),
                        self.drivers())
        self.hub.scan_once()
        self.assertIn("thermo", self.hub.instances)
        inst = self.hub.instances["thermo"]
        self.assertTrue(wait_for(inst.up))
        self.assertEqual(self.hub.instances["thermo"].title, f"Thermometer v{version} #1")
        return inst

    def num(self):
        self.hub._table = (0.0, [])
        return next(r["number"] for r in self.hub.device_table() if r["slug"] == "thermo")

    # ---------------------------------------------------------------- install, update, uninstall
    def test_half_written_is_ignored_then_installed_while_running(self):
        install(self.root, "1.0.0", json_last=False)
        time.sleep(1.0)
        self.assertEqual(self.sup.procs, [], "no driver.json yet: not started")
        self.assertEqual(self.get("/hub/drivers"), [])
        with open(os.path.join(self.root, "thermo", "1.0.0", "driver.json"), "w") as f:
            json.dump({"key": "thermo", "title": "Thermometer", "version": "1.0.0", "api": 1}, f)
        self.assertTrue(wait_for(lambda: self.drivers().get("thermo", {}).get("state") == "running", 20))
        d = self.drivers()["thermo"]
        self.assertEqual((d["version"], d["source"], d["api"], d["enabled"]), ("1.0.0", "installed", 1, True))
        self.hub.scan_once()
        self.assertIn("thermo", self.hub.instances)

    def test_update_without_clients_switches_on_the_same_port_and_cleans_up(self):
        inst = self.installed_and_found()
        old_pid = self.sup.find("thermo").pid
        uid = next(r["uid"] for r in self.hub.device_table() if r["slug"] == "thermo")
        install(self.root, "1.1.0")
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["version"] == "1.1.0"
                                 and self.drivers()["thermo"]["state"] == "running" and inst.up(), 30),
                        self.drivers())
        self.assertIs(self.hub.instances["thermo"], inst, "same tab")
        self.assertEqual(inst.title, "Thermometer v1.1.0 #1")
        self.assertNotEqual(self.sup.find("thermo").pid, old_pid)
        self.assertEqual(self.opened, ["COM71"], "the port was never reopened")
        self.hub._table = (0.0, [])
        self.assertEqual(next(r["uid"] for r in self.hub.device_table() if r["slug"] == "thermo"), uid)
        self.assertTrue(wait_for(lambda: not os.path.exists(os.path.join(self.root, "thermo", "1.0.0")), 10),
                        "the old version's folder is deleted once nothing uses it")

    def test_update_waits_for_a_connected_client_or_switch_now(self):
        inst = self.installed_and_found()
        n = self.num()
        r = self.put(f"/api/v1/switch/{n}/connected", Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertEqual(r["ErrorNumber"], 0)
        install(self.root, "2.0.0")
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["update"] == "2.0.0", 15), self.drivers())
        time.sleep(1.0)
        self.assertEqual(self.drivers()["thermo"]["version"], "1.0.0", "not while a client is connected")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "thermo", "1.0.0")), "the running version stays")
        # the client disconnects: the update goes through by itself
        self.put(f"/api/v1/switch/{n}/connected", Connected="false", ClientID="1", ClientTransactionID="2")
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["version"] == "2.0.0" and inst.up(), 30))
        self.assertIsNone(self.drivers()["thermo"]["update"])
        # again, with "Switch now" while connected
        self.put(f"/api/v1/switch/{n}/connected", Connected="true", ClientID="1", ClientTransactionID="3")
        install(self.root, "2.1.0")
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["update"] == "2.1.0", 15))
        self.assertTrue(self.post("/hub/drivers/thermo/switch")["ok"])
        self.assertEqual(self.drivers()["thermo"]["version"], "2.1.0")
        self.assertTrue(wait_for(inst.up, 20))
        self.assertEqual(self.opened, ["COM71"])

    def test_uninstall_stops_it_and_frees_the_port(self):
        self.installed_and_found()
        proc = self.sup.find("thermo").popen
        os.remove(os.path.join(self.root, "thermo", "1.0.0", "driver.json"))     # what the uninstaller does first
        self.assertTrue(wait_for(lambda: "thermo" not in self.drivers(), 15), "gone from /hub/drivers")
        self.assertIsNotNone(proc.wait(10), "its process ended")
        self.assertNotIn("thermo", self.hub.instances)
        self.assertNotIn("COM71", self.hub._relays, "its port is released")
        self.assertTrue(self.handles["COM71"].closed)
        shutil.rmtree(os.path.join(self.root, "thermo"))                         # then the rest
        install(self.root, "1.0.0")                                              # installed again: found again
        self.assertTrue(wait_for(lambda: self.drivers().get("thermo", {}).get("state") == "running", 20))
        self.hub.scan_once()
        self.assertIn("thermo", self.hub.instances)

    # ---------------------------------------------------------------- refused, off/on
    def test_wrong_api_or_python_is_refused(self):
        install(self.root, "1.0.0", api=2)
        install(self.root, "1.0.0", key="otherpy", python="2.7")
        self.assertTrue(wait_for(lambda: len(self.drivers()) == 2, 15))
        d = self.drivers()
        self.assertEqual(d["thermo"]["state"], "refused")
        self.assertIn("hub API 2", d["thermo"]["why"])
        self.assertEqual(d["otherpy"]["state"], "refused")
        self.assertIn("Python 2.7", d["otherpy"]["why"])
        self.assertEqual(self.sup.procs, [])
        # a good version next to the refused one is the one used
        install(self.root, "0.9.0")
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["state"] == "running", 20), self.drivers())
        self.assertEqual(self.drivers()["thermo"]["version"], "0.9.0")

    def test_driver_for_a_newer_hub_is_refused(self):
        from hub import VERSION
        install(self.root, "2.0.0", min_hub="99.0")
        install(self.root, "1.0.0", key="badmin", min_hub="soon")
        self.assertTrue(wait_for(lambda: len(self.drivers()) == 2, 15))
        d = self.drivers()
        self.assertEqual(d["thermo"]["state"], "refused")
        self.assertIn(f"needs Alpaca Hub 99.0 or newer, this is {VERSION}", d["thermo"]["why"])
        self.assertEqual(d["badmin"]["state"], "refused")
        self.assertIn("min_hub", d["badmin"]["why"])
        # an older version that this hub can run is used instead, and one asking for this very hub runs
        install(self.root, "1.5.0", min_hub=VERSION)
        self.assertTrue(wait_for(lambda: self.drivers()["thermo"]["state"] == "running", 20), self.drivers())
        self.assertEqual(self.drivers()["thermo"]["version"], "1.5.0")

    def test_turned_off_stays_off_and_on_again(self):
        self.installed_and_found()
        self.assertTrue(self.post("/hub/drivers/thermo/off")["ok"])
        d = self.drivers()["thermo"]
        self.assertEqual((d["state"], d["enabled"]), ("off", False))
        self.assertNotIn("thermo", self.hub.instances)
        self.assertNotIn("COM71", self.hub._relays)
        self.assertEqual(json.load(open(os.path.join(self.dir, "hub.json")))["drivers_off"], ["thermo"], "saved")
        time.sleep(1.0)
        self.assertEqual(self.drivers()["thermo"]["state"], "off", "the catalog watch doesn't start it again")
        self.hub.scan_once()
        self.assertNotIn("thermo", self.hub.instances)
        self.assertTrue(self.post("/hub/drivers/thermo/on")["ok"])
        self.assertEqual(self.drivers()["thermo"]["state"], "running")
        self.hub.scan_once()
        self.assertIn("thermo", self.hub.instances)

    def test_driver_buttons_need_the_hub_page(self):
        self.installed_and_found()
        req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}/hub/drivers/thermo/off", method="POST",
                                     data=b"")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.drivers()["thermo"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
