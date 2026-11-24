"""Driver processes (REFACTOR.md step 5): a driver that crashes, hangs, exits, floods its port or sends bad messages
takes only its own process down. The hub and the other driver keep running, the device comes back on the same tab
with the same UniqueID, and its port is never reopened."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from hub import host as H  # noqa: E402
from hub import supervisor as S  # noqa: E402
from hub.ports import PortInfo  # noqa: E402


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


class ThermoPort:
    """A hub-side port with a 'thermometer' on it: it says a line every 0.1 s and counts what it is sent."""

    def __init__(self, serial):
        self.serial = serial
        self.rx = bytearray()
        self.written = 0
        self.first_write = None
        self.lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._talk, daemon=True).start()

    def _talk(self):
        t = 20.0
        while not self.closed:
            t += 0.1
            with self.lock:
                self.rx += f"THERMO {self.serial} {t:.1f}\n".encode()
            time.sleep(0.1)

    @property
    def in_waiting(self):
        if self.closed:
            raise OSError("closed")
        with self.lock:
            return len(self.rx)

    def read(self, n=1):
        with self.lock:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def write(self, data):
        with self.lock:
            if self.first_write is None:
                self.first_write = time.monotonic()
            self.written += len(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self.lock:
            self.rx.clear()

    def close(self):
        self.closed = True


class Bench:
    def __init__(self):
        self.ports = {"COM61": PortInfo("COM61", 0x1234, 0x0001, "", "fake a"),
                      "COM62": PortInfo("COM62", 0x1234, 0x0002, "", "fake b")}
        self.opened = []
        self.handles = {}

    def lister(self):
        return list(self.ports.values())

    def opener(self, device, baud, lines=False):
        self.opened.append(device)
        p = self.handles[device] = ThermoPort(device[-2:])
        return p


class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (S.PING_S, S.T_PING, S.BACKOFF_MAX_S)
        S.PING_S, S.T_PING = 0.5, 0.5          # a hang is noticed in about 1.5 s

    @classmethod
    def tearDownClass(cls):
        S.PING_S, S.T_PING, S.BACKOFF_MAX_S = cls.saved

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bench = Bench()
        self.sup = S.Supervisor(["fakedrv_a", "fakedrv_b"], self.dir, simulated=True, extra_path=HERE)
        self.assertEqual([p.state for p in self.sup.procs], ["running", "running"],
                         [p.why for p in self.sup.procs])
        self.hub = H.Hub(self.sup.drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0,
                         port_lister=self.bench.lister, opener=self.bench.opener, simulated=True,
                         use_standalone_settings=False, listen_s=0.3, supervisor=self.sup)
        self.hub.start(scan=False)
        self.hub.scan_once()
        self.assertEqual(sorted(self.hub.instances), ["fakea", "fakeb"])
        for inst in self.hub.instances.values():
            self.assertTrue(wait_for(inst.up), f"{inst.slug} up")
        self.assertEqual(len(self.hub.device_table()), 2)     # as Overview's polling does all the time

    def tearDown(self):
        self.hub.stop()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.hub.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode())

    def put(self, path, **form):
        req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}{path}", method="PUT",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def proc(self, name):
        return self.sup.find(name)

    def misbehave(self, slug, what):
        self.get(f"/{slug}/do?what={what}")

    def check_back(self, slug, name, old_pid, other_pid):
        """slug's driver process was replaced, its device is back on the same tab and UniqueID, and nothing else
        was touched."""
        a = self.hub.instances[slug]
        uid = next(r["uid"] for r in self.hub.device_table() if r["slug"] == slug)
        self.assertTrue(wait_for(lambda: self.proc(name).state == "running" and self.proc(name).pid != old_pid
                                 and a.up(), 30), f"{name} came back: {self.proc(name).info()}")
        self.assertIs(self.hub.instances[slug], a, "same tab")
        st = self.get(f"/{slug}/status.json")
        self.assertNotEqual(st["pid"], old_pid)
        self.assertTrue(wait_for(lambda: self.get(f"/{slug}/status.json")["lines"] > 2), "reads its port again")
        self.hub._table = (0.0, [])
        self.assertEqual(next(r["uid"] for r in self.hub.device_table() if r["slug"] == slug), uid, "same UniqueID")
        self.assertEqual(sorted(self.bench.opened), ["COM61", "COM62"], "no port was opened again")
        b = self.proc("fakedrv_b" if name == "fakedrv_a" else "fakedrv_a")
        self.assertEqual(b.pid, other_pid, "the other driver's process was not touched")
        self.assertEqual(b.state, "running")

    def pids(self):
        return self.proc("fakedrv_a").pid, self.proc("fakedrv_b").pid

    # ---------------------------------------------------------------- failures
    def test_a_crash_restarts_only_that_driver(self):
        a_pid, b_pid = self.pids()
        b_lines = self.get("/fakeb/status.json")["lines"]
        num = next(r["number"] for r in self.hub.device_table() if r["slug"] == "fakea")
        old, S.BACKOFF_FIRST_S = S.BACKOFF_FIRST_S, 3.0      # long enough to look at it while it restarts
        try:
            self.misbehave("fakea", "crash")
            self.assertTrue(wait_for(lambda: self.proc("fakedrv_a").state == "restarting", 10))
            # while it restarts: the tab says so, Alpaca says "not connected", the other device carries on
            inst = self.hub.instances["fakea"]
            self.assertIsNone(inst.local_port)
            self.assertFalse(inst.up())
            self.assertIn("restarting", inst.summary()["text"])
            self.hub._table = (0.0, [])
            r = self.put(f"/api/v1/switch/{num}/connected", Connected="true", ClientID="1", ClientTransactionID="5")
            self.assertEqual((r["ErrorNumber"], r["ClientTransactionID"]), (H.NOT_CONNECTED, 5))
            self.assertIn("code 3", self.proc("fakedrv_a").why)
            self.assertGreater(self.get("/fakeb/status.json")["lines"], b_lines)
            self.assertIn("restarting", [p["state"] for p in self.get("/hub.json")["drivers"]])
        finally:
            S.BACKOFF_FIRST_S = old
        self.check_back("fakea", "fakedrv_a", a_pid, b_pid)

    def test_a_driver_gone_before_the_hub_notices_answers_not_connected(self):
        # tools/soak.py, 2026-11-23: a request in the instant after a driver process died (the hub still had its
        # port) got HTTP 502, which NINA and the ASCOM library take as a failed device. Now it is an Alpaca "not
        # connected", as while the driver restarts; the driver's own pages still get 502.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()                                   # nothing listens there now
        inst = self.hub.instances["fakea"]
        num = next(r["number"] for r in self.hub.device_table() if r["slug"] == "fakea")
        live, inst.local_port = inst.local_port, dead
        try:
            r = self.get(f"/api/v1/switch/{num}/connected?ClientID=1&ClientTransactionID=7")
            self.assertEqual((r["ErrorNumber"], r["ClientTransactionID"]), (H.NOT_CONNECTED, 7))
            r = self.put(f"/api/v1/switch/{num}/connected", Connected="true", ClientID="1", ClientTransactionID="8")
            self.assertEqual((r["ErrorNumber"], r["ClientTransactionID"]), (H.NOT_CONNECTED, 8))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/fakea/status.json")
            self.assertEqual(cm.exception.code, 502)
        finally:
            inst.local_port = live

    def test_exit_is_restarted(self):
        a_pid, b_pid = self.pids()
        self.misbehave("fakeb", "exit")
        self.check_back("fakeb", "fakedrv_b", b_pid, a_pid)

    def test_a_connected_client_stays_connected_across_a_restart(self):
        # Real panel, 2026-11-22: after its driver process was killed, CoverState answered "not connected" until
        # the client connected again. NINA doesn't; the hub now connects again what a client had connected.
        a_pid, b_pid = self.pids()
        num = next(r["number"] for r in self.hub.device_table() if r["slug"] == "fakea")
        r = self.put(f"/api/v1/switch/{num}/connected", Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertEqual(r["ErrorNumber"], 0)
        self.misbehave("fakea", "crash")
        self.check_back("fakea", "fakedrv_a", a_pid, b_pid)
        conn = lambda: self.get(f"/api/v1/switch/{num}/connected?ClientID=1&ClientTransactionID=2")["Value"]  # noqa
        self.assertTrue(wait_for(conn, 15), "connected again in the new process")
        # a device the client had disconnected stays disconnected
        self.put(f"/api/v1/switch/{num}/connected", Connected="false", ClientID="1", ClientTransactionID="3")
        a_pid = self.proc("fakedrv_a").pid
        self.misbehave("fakea", "crash")
        self.check_back("fakea", "fakedrv_a", a_pid, b_pid)
        time.sleep(1.5)
        self.assertFalse(conn())

    def test_a_hang_is_ended_and_restarted(self):
        a_pid, b_pid = self.pids()
        self.misbehave("fakea", "hang")
        self.assertTrue(wait_for(lambda: "stopped answering" in (self.proc("fakedrv_a").why or ""), 15),
                        self.proc("fakedrv_a").info())
        self.check_back("fakea", "fakedrv_a", a_pid, b_pid)

    def test_bad_messages_end_the_process(self):
        for what in ("bad", "badrelay", "big"):
            a_pid, b_pid = self.pids()
            self.misbehave("fakea", what)
            self.assertTrue(wait_for(lambda: self.proc("fakedrv_a").pid != a_pid, 15), what)
            self.check_back("fakea", "fakedrv_a", a_pid, b_pid)
        self.assertGreaterEqual(len(self.proc("fakedrv_a").restarts), 3)

    def test_a_flood_is_capped_and_harms_nothing_else(self):
        b_lines = self.get("/fakeb/status.json")["lines"]
        self.misbehave("fakea", "flood")
        time.sleep(2.0)
        port = self.bench.handles["COM61"]
        relay = self.hub._relays["COM61"]
        took = time.monotonic() - port.first_write
        self.assertLessEqual(port.written, relay._rate() * (took + 1) + 4096, "no more than the line rate")
        self.assertGreater(relay.dropped, 0)
        self.assertGreater(self.get("/fakeb/status.json")["lines"], b_lines + 5, "the other device carried on")
        self.assertEqual(self.get("/hub.json")["name"], "Alpaca Hub")

    def test_five_ends_make_it_failed_until_try_again(self):
        old = S.FAILS
        S.FAILS = 2
        try:
            a_pid, b_pid = self.pids()
            self.misbehave("fakea", "crash")
            self.assertTrue(wait_for(lambda: self.proc("fakedrv_a").state == "running"
                                     and self.proc("fakedrv_a").pid != a_pid and self.hub.instances["fakea"].up(), 30))
            self.misbehave("fakea", "crash")
            self.assertTrue(wait_for(lambda: self.proc("fakedrv_a").state == "failed", 15))
            time.sleep(2.5)
            self.assertEqual(self.proc("fakedrv_a").state, "failed", "not started again by itself")
            j = self.get("/hub.json")
            d = next(p for p in j["drivers"] if p["name"] == "fakedrv_a")
            self.assertEqual(d["state"], "failed")
            self.assertTrue(d["last_lines"], "its last log lines are on Overview")
            dev = next(x for x in j["devices"] if x["slug"] == "fakea")
            self.assertFalse(dev["up"])
            self.assertIn("failed", dev["summary"]["text"])
            self.assertIn("COM61", self.hub._relays, "its port stays held")
            req = urllib.request.Request(f"http://127.0.0.1:{self.hub.port}/hub/drivers/fakedrv_a/retry",
                                         method="POST", data=b"", headers={"X-Hub-Page": "1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertTrue(json.loads(r.read())["ok"])
            self.assertTrue(wait_for(lambda: self.proc("fakedrv_a").state == "running"
                                     and self.hub.instances["fakea"].up(), 30))
            self.assertEqual(self.proc("fakedrv_b").pid, b_pid)
        finally:
            S.FAILS = old

    def test_stopping_the_hub_ends_the_driver_processes(self):
        procs = [p.popen for p in self.sup.procs]
        self.hub.stop()
        for p in procs:
            self.assertIsNotNone(p.wait(10))


class RealDriversInProcessesTests(unittest.TestCase):
    def test_hub_process_runs_no_driver_code_and_a_killed_driver_comes_back(self):
        # In a fresh interpreter, so the modules other tests imported don't count.
        code = r'''
import json, sys, tempfile, time
sys.path.insert(0, sys.argv[1])
from hub.catalog import Catalog
from hub.supervisor import Supervisor
from hub.host import Hub
d = tempfile.mkdtemp()
sup = Supervisor(config_dir=d, simulated=True, use_standalone_settings=False, catalog=Catalog(installed=""))
lister, opener = sup.simulated_ports("nitecrawler,box,panel,lynx,gemini")
hub = Hub(sup.drivers(), config_dir=d, bind="127.0.0.1", http_port=0, port_lister=lister, opener=opener,
          simulated=True, use_standalone_settings=False, listen_s=1.3, supervisor=sup)
hub.start(scan=False)
hub.scan_once()
def up():
    return all(i.up() for i in hub.instances.values())
end = time.monotonic() + 30
while not up() and time.monotonic() < end:
    time.sleep(0.2)
out = {"slugs": sorted(hub.instances), "up": up(),
       "driver_code": sorted(m for m in sys.modules if m.split(".")[0] in ("nitecrawler", "wbox", "optec", "wcover")
                             or m.startswith("hub.drivers."))}
panel = hub.instances["panel"]
cover = sup.find("wcover")
others = {p.name: p.pid for p in sup.procs if p is not cover}
old = cover.pid
cover.popen.kill()
end = time.monotonic() + 40
while not (cover.state == "running" and cover.pid != old and panel.up()) and time.monotonic() < end:
    time.sleep(0.2)
out["panel_back"] = cover.state == "running" and cover.pid != old and panel.up() and hub.instances["panel"] is panel
out["others_same"] = others == {p.name: p.pid for p in sup.procs if p is not cover}
out["opens"] = None
hub.stop()
print(json.dumps(out))
'''
        r = subprocess.run([sys.executable, "-c", code, ROOT], capture_output=True, text=True, timeout=180, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["slugs"], ["box", "focuslynx", "gemini", "nitecrawler", "panel"])
        self.assertTrue(out["up"], "every simulated device came up, its simulator in its driver's process")
        self.assertEqual(out["driver_code"], [], "the hub's process imported no driver code")
        self.assertTrue(out["panel_back"], "the panel came back on its tab after its driver process was killed")
        self.assertTrue(out["others_same"], "the other driver processes were not touched")


if __name__ == "__main__":
    unittest.main()
