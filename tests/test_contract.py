"""Contract tests, run against every driver folder in drivers/ (REFACTOR.md "Contract tests").

For each folder: driver.json matches the package and this hub's API; the package imports only what a driver may;
then, in its own process on each of its simulators: found through the relay with nothing sent while listening, a
summary of the right shape, a page built with the hub's kit, and its device back on the same tab with the same
UniqueID, without its port being reopened, after its process is killed, after off and on, and after an update."""
import ast
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
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from hub import host as H  # noqa: E402
from hub import supervisor as S  # noqa: E402
from hub.api import API_VERSION  # noqa: E402
from hub import VERSION as HUB_VERSION  # noqa: E402
from hub.catalog import parse_version  # noqa: E402
from hub.catalog import SOURCE_ROOT, Catalog, load_in_process  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
from hub.usbdevs import UsbDevice  # noqa: E402

ALLOWED_HUB = ("hub.api", "hub.devserver", "hub.core")     # what a driver may use from the hub


EXAMPLES = os.path.join(ROOT, "docs", "example-driver")     # the guide's example driver (docs/WRITING-DRIVERS.md)


def _folders():
    out = {}
    for root in (SOURCE_ROOT, EXAMPLES):
        for k in os.listdir(root):
            if os.path.isfile(os.path.join(root, k, "driver.json")):
                out[k] = os.path.join(root, k)
    return out


FOLDERS = _folders()


def folders():
    return sorted(FOLDERS)


def folder_of(key):
    return FOLDERS[key]


def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return cond()


class FolderTests(unittest.TestCase):
    def test_there_is_at_least_one_driver_folder(self):
        self.assertIn("wcover", folders())
        self.assertIn("thermo", folders(), "the guide's example driver is checked too")

    def test_driver_json_and_package(self):
        for key in folders():
            with self.subTest(key=key):
                d = json.load(open(os.path.join(folder_of(key), "driver.json"), encoding="utf-8"))
                self.assertEqual(d["key"], key)
                self.assertEqual(d["api"], API_VERSION)
                if "min_hub" in d:   # the oldest hub it works with: a version, and not newer than this hub
                    self.assertIsNotNone(parse_version(d["min_hub"]), d["min_hub"])
                    self.assertLessEqual(parse_version(d["min_hub"]), parse_version(HUB_VERSION))
                pkg = (d.get("module") or f"{key}.driver").split(".")[0]
                init = os.path.join(folder_of(key), pkg, "__init__.py")
                self.assertTrue(os.path.isfile(init), "the package is there")
                tree = ast.parse(open(init, encoding="utf-8").read())
                versions = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
                            and any(getattr(t, "id", "") == "VERSION" for t in n.targets)]
                if versions:
                    self.assertEqual(versions[0], d["version"], "the package's VERSION is driver.json's")

    def test_only_allowed_imports(self):
        stdlib = set(sys.stdlib_module_names)
        for key in folders():
            d = json.load(open(os.path.join(folder_of(key), "driver.json"), encoding="utf-8"))
            pkg = (d.get("module") or f"{key}.driver").split(".")[0]
            base = os.path.join(folder_of(key), pkg)
            for dirpath, _, files in os.walk(base):
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, f)
                    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
                        names = []
                        if isinstance(node, ast.Import):
                            names = [a.name for a in node.names]
                        elif isinstance(node, ast.ImportFrom) and node.level == 0:
                            names = [node.module]
                        for n in names:
                            top = n.split(".")[0]
                            with self.subTest(file=os.path.relpath(path, ROOT), module=n):
                                if top == "hub":
                                    self.assertTrue(n.startswith(ALLOWED_HUB), f"{n} is hub-internal")
                                else:
                                    self.assertTrue(top in stdlib or top in ("serial", pkg),
                                                    f"{n}: not the standard library, serial or the driver's own")


class InProcessTests(unittest.TestCase):
    """Every simulator of every driver folder, in the driver's own process, through the hub."""

    @classmethod
    def setUpClass(cls):
        cls.saved = (S.PING_S, S.T_PING, S.WATCH_S)
        S.PING_S, S.T_PING, S.WATCH_S = 0.5, 0.5, 0.3

    @classmethod
    def tearDownClass(cls):
        S.PING_S, S.T_PING, S.WATCH_S = cls.saved

    def start(self, key, sim):
        self.dir = tempfile.mkdtemp()
        self.inst_root = os.path.join(self.dir, "installed")
        os.makedirs(self.inst_root)
        self.src = os.path.join(self.dir, "source")
        shutil.copytree(os.path.join(folder_of(key)), os.path.join(self.src, key),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.sup = S.Supervisor(config_dir=self.dir, simulated=True, use_standalone_settings=False,
                                catalog=Catalog(installed=self.inst_root, source=self.src, builtin=False))
        self.assertEqual([p.state for p in self.sup.procs], ["running"], self.sup.info())
        # The simulator runs here, standing in for the device: killing the driver's process leaves it alone, as it
        # leaves a real device alone (with --simulate it would live, and die, in the driver's process).
        simobj, usb, serial, desc = self.factory(key, sim)(0)
        self.usb = self.transport(key, sim) == "usb"
        if self.usb:
            # An SDK device: no port, and its simulator lives in the driver's process (the SDK runs there). A
            # killed process starts a fresh one, as a real focuser would be found again.
            ports, usb_devs = [], [UsbDevice(usb[0], usb[1], "SIM1", desc, sim_name=sim, sim_index=0)]
        else:
            ports, usb_devs = [PortInfo("SIM1", usb[0], usb[1], serial, desc)], []
        self.opens, self.written = opens, written = [], []      # this run's own (an old hub may still write)

        def lister():
            return list(ports)
        lister.usb = lambda: list(usb_devs)

        def counting(device, baud, lines=False):
            opens.append(device)
            try:
                port = simobj.opener(device, baud)
            except TypeError:
                port = simobj.opener(device)
            real = port.write

            def write(data):
                written.append((time.monotonic(), bytes(data)))
                return real(data)
            port.write = write
            return port
        self.hub = H.Hub(self.sup.drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0,
                         port_lister=lister, opener=counting, simulated=True, use_standalone_settings=False,
                         listen_s=1.5, supervisor=self.sup)
        self.hub.start(scan=False)
        return self.sup.procs[0]

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.hub.port}{path}", timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def put(self, path, **form):
        req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}{path}", method="PUT",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def uid(self, slug):
        self.hub._table = (0.0, [])
        return sorted(r["uid"] for r in self.hub.device_table() if r["slug"] == slug)

    def same_device_back(self, inst, uid, opens, why):
        self.assertTrue(wait_for(lambda: inst.up() and self.hub.instances.get(inst.slug) is inst, 30),
                        f"{why}: {inst.slug} back on its tab ({self.sup.info()})")
        self.assertEqual(self.uid(inst.slug), uid, f"{why}: same UniqueIDs")
        self.assertEqual(self.opens, opens, f"{why}: its port was not opened again")

    def test_contract(self):
        for key in folders():
            d = json.load(open(os.path.join(folder_of(key), "driver.json"), encoding="utf-8"))
            names = None
            for sim in self._simulators(key):
                names = names or sim
                with self.subTest(key=key, simulator=sim):
                    self._one(key, d, sim)
            self.assertIsNotNone(names, f"{key} declares no simulator")

    @staticmethod
    def _local(key):
        """The folder's drivers, imported into this test process (only for their simulators)."""
        meta = json.load(open(os.path.join(folder_of(key), "driver.json"), encoding="utf-8"))
        pkg = (meta.get("module") or f"{key}.driver").split(".")[0]
        return [d for d in load_in_process(Catalog(installed="", source=os.path.dirname(folder_of(key)), builtin=False))
                if type(d).__module__.split(".")[0] == pkg]

    def _simulators(self, key):
        return sorted({n for d in self._local(key) for n in d.simulators})

    def transport(self, key, sim):
        return next(getattr(d, "transport", "serial") for d in self._local(key) if sim in d.simulators)

    def factory(self, key, sim):
        return next(d.simulators[sim] for d in self._local(key) if sim in d.simulators)

    def _one(self, key, meta, sim):
        proc = self.start(key, sim)
        try:
            self._check(key, meta, sim, proc)
        finally:
            self.hub.stop()                        # before the next driver's run, not at the end of the test

    def _check(self, key, meta, sim, proc):
        self.assertEqual(proc.entry.version, meta["version"])
        if self.usb:
            # found through its SDK while a device of its vendor is plugged in; never through a port
            units = self.hub.scan_usb()
            self.assertEqual(len(units), 1, f"{sim} found")
            port = next(iter(units.values()))[0]
            self.assertEqual(self.opens, [], "no port opened")
        else:
            port = self.hub.port_lister()[0]
            # found, and a listening driver sends nothing while it is looked for
            found = self.hub.identify(port)
            self.assertIsNotNone(found, f"{sim} found")
            driver, ident = found
            if driver.listen_baud and not driver.probe_baud:
                self.assertEqual(self.written, [], "nothing sent while listening")
            self.hub._release(port.device)
        self.opens.clear()
        self.hub.scan_once()
        self.assertEqual(len(self.hub.instances), 1)
        inst = next(iter(self.hub.instances.values()))
        self.assertTrue(wait_for(inst.up, 20), f"{inst.slug} up")
        s = inst.summary()
        self.assertTrue(isinstance(s, dict) and isinstance(s.get("text"), str) and s.get("state") in ("ok", "warn", "bad"))
        code, page = self.get(f"/{inst.slug}/")
        self.assertEqual(code, 200)
        self.assertIn("<html", page.lower())
        uid = self.uid(inst.slug)
        self.assertTrue(uid, "it serves Alpaca devices")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "logs", f"{key}.log")), "it logs to logs/<key>.log")
        opens = list(self.opens)
        # a client connects to its first Alpaca device, then the process is killed: back by itself, still connected
        self.hub._table = (0.0, [])
        row = next(r for r in self.hub.device_table() if r["slug"] == inst.slug)
        base = f"/api/v1/{row['type']}/{row['number']}/connected"
        r = self.put(base, Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertEqual(r["ErrorNumber"], 0, r)
        old = proc.pid
        proc.popen.kill()
        self.assertTrue(wait_for(lambda: proc.state == "running" and proc.pid != old, 20))
        self.same_device_back(inst, uid, opens, "killed")
        self.assertTrue(wait_for(lambda: json.loads(self.get(base + "?ClientID=1&ClientTransactionID=2")[1])["Value"],
                                 20), "its client's connection was restored")
        self.put(base, Connected="false", ClientID="1", ClientTransactionID="3")
        # off and on: a new instance, but the same tab and UniqueIDs (the port is released, then opened again)
        self.sup.set_enabled(key, False)
        self.assertEqual(self.hub.instances, {})
        self.assertNotIn(port.device, self.hub._relays, "off releases the port")
        self.sup.set_enabled(key, True)
        self.hub.scan_once()
        self.assertTrue(wait_for(lambda: inst.slug in self.hub.instances and self.hub.instances[inst.slug].up(), 20))
        self.assertEqual(self.uid(inst.slug), uid, "on again: same UniqueIDs")
        inst = self.hub.instances[inst.slug]
        opens = list(self.opens)
        # updated (a higher version installed): same tab and UniqueIDs, port kept
        vdir = os.path.join(self.inst_root, key, "999.0.0")
        shutil.copytree(os.path.join(self.src, key), vdir, ignore=shutil.ignore_patterns("driver.json"))
        meta2 = dict(meta, version="999.0.0")
        with open(os.path.join(vdir, "driver.json"), "w", encoding="utf-8") as f:
            json.dump(meta2, f)
        self.assertTrue(wait_for(lambda: self.sup.find(key).entry.version == "999.0.0", 20))
        self.same_device_back(inst, uid, opens, "updated")


if __name__ == "__main__":
    unittest.main()
