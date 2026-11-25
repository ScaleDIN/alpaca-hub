"""The hub: safe detection, stable public device numbers, forwarding, and dropping devices that went away."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub import host as H  # noqa: E402
from hub.drivers import all_drivers  # noqa: E402
from hub.ports import PortInfo  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "wcover"))
from wcover.sim import SimV4ec  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "nitecrawler"))
from nitecrawler.sim import SimNiteCrawler  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "drivers", "wbox"))
from wbox.sim import SimBox  # noqa: E402

FTDI = (0x0403, 0x6015)
CH340 = (0x1A86, 0x7523)


class Chatty:
    """A port with some other device on it that talks on its own. Records anything written to it."""

    def __init__(self, log):
        self.log = log

    def read(self, n=1):
        time.sleep(0.01)
        return b"SOMEDEVICE,1,2,3\r\n"

    def write(self, data):
        self.log.append(bytes(data))
        return len(data)

    def close(self):
        pass


class Bench:
    """Fake serial ports: simulated NiteCrawlers, a chatty FTDI device, a CH340 device and a port nobody may open."""

    def __init__(self):
        self.ports = {}
        self.sims = {}
        self.opened = []
        self.lines = []           # (device, DTR/RTS on?) per open
        self.written = []

    def add_nc(self, name, serial):
        self.sims[name] = SimNiteCrawler(serial=serial, speed=50)
        self.ports[name] = PortInfo(name, *FTDI, f"FT{serial}", "Simulated NiteCrawler")

    def add_box(self, name):
        self.sims[name] = SimBox(loop_s=0.1)
        self.ports[name] = PortInfo(name, *CH340, "", "Simulated box")

    def add_panel(self, name):
        self.sims[name] = SimV4ec(move_s=0.6, line_s=0.1)
        self.ports[name] = PortInfo(name, *CH340, "", "Simulated panel")

    def add(self, name, usb, desc):
        self.ports[name] = PortInfo(name, *(usb or (None, None)), "", desc)

    def lister(self):
        return list(self.ports.values())

    def opener(self, device, baud, lines=False):
        self.opened.append(device)
        self.lines.append((device, lines))
        if device in self.sims:
            return self.sims[device].opener(device)
        if device == "BUSY":
            raise OSError("access denied")
        return Chatty(self.written)


class RawPort:
    """Stands in for a pyserial port opened with timeout=0. Fails the test if asked to read bytes that haven't
    arrived, because on Windows that is a read left pending inside the driver."""

    def __init__(self):
        self.rx = bytearray()
        self.lock = __import__("threading").Lock()
        self.overreads = 0
        self.closed = False

    def arrive(self, data):
        with self.lock:
            self.rx += data

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.rx)

    def read(self, n=1):
        with self.lock:
            if n > len(self.rx):
                self.overreads += 1
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def reset_input_buffer(self):
        with self.lock:
            self.rx.clear()

    def close(self):
        self.closed = True


class PolledSerialTests(unittest.TestCase):
    def setUp(self):
        from hub.ports import PolledSerial
        self.raw = RawPort()
        self.ser = PolledSerial(self.raw, timeout=0.3)

    def later(self, delay, data):
        import threading
        threading.Timer(delay, self.raw.arrive, (data,)).start()

    def test_readline_waits_for_the_line_across_chunks(self):
        self.later(0.05, b"ZXWBPlus")
        self.later(0.1, b"V3A1A\r\nnext")
        self.assertEqual(self.ser.readline(), b"ZXWBPlusV3A1A\r\n")
        self.assertEqual(self.ser.in_waiting, 4)                  # "next" kept for the following read
        self.assertEqual(self.raw.overreads, 0)

    def test_timeouts_return_what_there_is(self):
        self.raw.arrive(b"partial")
        t = time.monotonic()
        self.assertEqual(self.ser.readline(), b"partial")
        self.assertGreaterEqual(time.monotonic() - t, 0.25)
        self.assertEqual(self.ser.read(5), b"")
        self.assertEqual(self.raw.overreads, 0)

    def test_read_n(self):
        self.later(0.05, b"12")
        self.later(0.1, b"345678")
        self.assertEqual(self.ser.read(5), b"12345")
        self.assertEqual(self.ser.read(1), b"6")
        self.ser.reset_input_buffer()
        self.assertEqual(self.ser.in_waiting, 0)
        self.assertEqual(self.raw.overreads, 0)

    def test_close_and_passthrough(self):
        self.ser.close()
        self.assertTrue(self.raw.closed)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class HubTests(unittest.TestCase):
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

    def get(self, h, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{h.port}{path}", timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def put(self, h, path, **form):
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}{path}", method="PUT",
                                     data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    # ---------------------------------------------------------------- detection
    def test_detection_is_safe(self):
        self.bench.add_nc("COM10", "25905")
        self.bench.add("COM3", FTDI, "some other FTDI gadget that talks")
        self.bench.add("COM5", CH340, "CH340 device")
        self.bench.add("COM7", None, "Bluetooth link")
        self.bench.add("BUSY", FTDI, "in use by another program")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual([i.port.device for i in h.instances.values()], ["COM10"])
        self.assertEqual(self.bench.written, [], "nothing may be written to a port that talks on its own")
        self.assertNotIn("COM7", self.bench.opened, "ports with no matching USB IDs are never opened")
        self.assertNotIn("COM5", [i.port.device for i in h.instances.values()], "a CH340 that is neither box nor panel")

    def test_drivers_limit_survives_a_driver_change(self):
        # 2026-11-24: a hub started with --drivers zwoeaf took the box after its EAF driver process restarted (the
        # supervisor's drivers_changed handed it every driver) and the box driver set the dew output.
        self.bench.add_box("COM8")
        self.bench.add_nc("COM10", "25905")
        drivers = all_drivers()
        h = H.Hub([d for d in drivers if d.key == "nitecrawler"], config_dir=self.dir, bind="127.0.0.1",
                  http_port=0, port_lister=self.bench.lister, opener=self.bench.opener, simulated=True,
                  use_standalone_settings=False, listen_s=0.2, only={"nitecrawler"})
        h.start(scan=False)
        self.hubs.append(h)
        h.scan_once()
        h.set_drivers(drivers)                  # what a driver process coming back does
        h.scan_once()
        self.assertEqual([d.key for d in h.drivers], ["nitecrawler"])
        self.assertEqual([i.port.device for i in h.instances.values()], ["COM10"])
        self.assertNotIn("COM8", self.bench.opened, "the box's port is never opened")

    def test_ch340_ports_open_with_dtr_rts_on(self):
        # 2026-11-20: the panel only talked with DTR/RTS on, as Empire and INDI open it; the NiteCrawler keeps them off.
        self.bench.add_box("COM8")
        self.bench.add_panel("COM26")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(i.port.device for i in h.instances.values()), ["COM10", "COM26", "COM8"])
        on = {d: v for d, v in self.bench.lines}
        self.assertEqual(on, {"COM8": True, "COM26": True, "COM10": False})
        self.assertEqual(len({v for d, v in self.bench.lines if d == "COM8"}), 1, "one level per port, never toggled")
        before = len(self.bench.lines)
        h._release("COM8")                       # the hub lets go of the port: the next open is a real one
        h.opener_for(self.bench.ports["COM8"], 9600)("COM8")       # and it keeps the level
        self.assertTrue(self.bench.lines[before:])
        self.assertEqual(set(self.bench.lines[before:]), {("COM8", True)})
        self.assertEqual(self.bench.written, [])

    def test_a_refused_post_leaves_the_connection_clean(self):
        # A 403 sent before the body was read left the body in the kept-alive connection: the next request read it
        # as its own start, and on Windows the client now and then got WinError 10053 (the test runs' rare failures).
        import http.client
        h = self.new_hub()
        c = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
        try:
            for _ in range(3):
                c.request("POST", "/hub/tab-order", body=b"order=" + b"x," * 2000,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 403)
                c.request("GET", "/hub.json")
                r = c.getresponse()
                self.assertEqual(r.status, 200, "the same connection still works")
                json.loads(r.read())
        finally:
            c.close()

    # ---------------------------------------------------------------- the tabs' order
    def test_tab_order_is_stable_and_saved(self):
        # Found in COM order (COM10 < COM26 < COM8 as text), the tabs still go by driver and unit number.
        self.bench.add_box("COM8")
        self.bench.add_panel("COM26")
        self.bench.add_nc("COM10", "25905")
        self.bench.add_nc("COM11", "30000")
        h = self.new_hub()
        h.scan_once()
        slugs = lambda: [d["slug"] for d in json.loads(self.get(h, "/hub.json")[1])["devices"]]
        self.assertEqual(slugs(), ["nitecrawler", "nitecrawler-2", "box", "panel"])

        def post(body, page=True):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/tab-order", method="POST", data=body,
                                         headers={"X-Hub-Page": "1"} if page else {})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())["order"]
        with self.assertRaises(urllib.error.HTTPError) as e:
            post(b"order=panel", page=False)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(slugs(), ["nitecrawler", "nitecrawler-2", "box", "panel"])
        # dragged: the page sends its tabs' order; junk and repeats are dropped
        self.assertEqual(post(b"order=panel,box,nitecrawler-2,panel,..%2Fx,nitecrawler"),
                         ["panel", "box", "nitecrawler-2", "nitecrawler"])
        self.assertEqual(slugs(), ["panel", "box", "nitecrawler-2", "nitecrawler"])
        self.assertEqual(H.HubSettings.load(h.settings.path).tab_order, ["panel", "box", "nitecrawler-2", "nitecrawler"])
        # the box is unplugged and the rest reordered: it keeps its place after the panel for when it's back
        self.assertEqual(post(b"order=nitecrawler,panel,nitecrawler-2"),
                         ["nitecrawler", "panel", "box", "nitecrawler-2"])
        # a device never placed goes after the placed ones, by driver
        h.settings.tab_order = ["box"]
        self.assertEqual(slugs(), ["box", "nitecrawler", "nitecrawler-2", "panel"])
        h.settings.tab_order = "junk"            # a hand-edited settings file
        self.assertEqual(slugs(), ["nitecrawler", "nitecrawler-2", "box", "panel"])
        # Overview's Reset order: back to the default, the absent device's place forgotten too
        self.assertEqual(post(b"order=panel,box"), ["panel", "box"])
        self.assertIs(json.loads(self.get(h, "/hub.json")[1])["tab_order_saved"], True)
        with self.assertRaises(urllib.error.HTTPError) as e:
            post(b"reset=1", page=False)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(post(b"reset=1"), [])
        self.assertEqual(slugs(), ["nitecrawler", "nitecrawler-2", "box", "panel"])
        self.assertIs(json.loads(self.get(h, "/hub.json")[1])["tab_order_saved"], False)
        self.assertEqual(H.HubSettings.load(h.settings.path).tab_order, [])

    # ---------------------------------------------------------------- tab numbers
    def test_tab_numbers_are_remembered(self):
        # 2026-11-24: two EFWs swapped tabs depending on which serial sorted first. A unit keeps the tab it got first.
        self.bench.add_nc("COM11", "30000")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(h.instances), ["nitecrawler"])
        h.stop()
        self.hubs.remove(h)
        self.bench.add_nc("COM10", "25905")               # found first now (COM10 < COM11): still the second tab
        h = self.new_hub()
        h.scan_once()
        self.assertEqual({s: i.port.device for s, i in h.instances.items()},
                         {"nitecrawler": "COM11", "nitecrawler-2": "COM10"})
        self.assertEqual(h.settings.units["nitecrawler"], {"30000": 0, "25905": 1})

    def test_renumber_tabs(self):
        for com, serial in (("COM10", "25905"), ("COM11", "30000"), ("COM12", "31000")):
            self.bench.add_nc(com, serial)
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(h.instances), ["nitecrawler", "nitecrawler-2", "nitecrawler-3"])
        self.assertEqual(h.units_json()["nitecrawler"]["tidy"], True)
        h.set_tab_order(["nitecrawler-3", "box", "nitecrawler-2", "nitecrawler"])
        h.stop()
        self.hubs.remove(h)
        del self.bench.ports["COM10"]                     # the first one is gone for good
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(h.instances), ["nitecrawler-2", "nitecrawler-3"], "they keep their tabs")
        u = json.loads(self.get(h, "/hub.json")[1])["units"]["nitecrawler"]
        self.assertEqual((u["present"], u["absent"], u["tidy"]), (2, 1, False))

        def post(body, page=True):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/units/renumber", method="POST", data=body,
                                         headers={"X-Hub-Page": "1"} if page else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                text = e.read().decode()
                return e.code, json.loads(text) if text.startswith("{") else text
        self.assertEqual(post(b"key=nitecrawler", page=False)[0], 403)
        h.note_client("nitecrawler-2", "focuser", 1, "connected", "PUT", b"Connected=True")
        self.assertTrue(h.units_json()["nitecrawler"]["in_use"])
        code, r = post(b"key=nitecrawler")
        self.assertEqual(code, 409, "not while a client is connected")
        self.assertIn("disconnect it first", r["message"])
        h.note_client("nitecrawler-2", "focuser", 1, "connected", "PUT", b"Connected=False")
        code, r = post(b"key=nitecrawler")
        self.assertEqual(code, 200)
        self.assertEqual(r["message"], "Forgot 1 unit not plugged in; 2 devices starting again under a new tab")
        self.assertEqual(h.settings.units["nitecrawler"], {"30000": 0, "31000": 1})
        self.assertEqual(h.settings.tab_order, ["nitecrawler-2", "box", "nitecrawler"], "each keeps its place")
        h.scan_once()
        self.assertEqual({s: i.port.device for s, i in h.instances.items()},
                         {"nitecrawler": "COM11", "nitecrawler-2": "COM12"})
        self.assertTrue(h.units_json()["nitecrawler"]["tidy"])
        self.assertEqual(post(b"key=nitecrawler")[1]["message"], "Already in order")

    def test_device_names(self):
        self.bench.add_nc("COM11", "30000")
        h = self.new_hub()
        h.scan_once()
        dev = lambda: json.loads(self.get(h, "/hub.json")[1])["devices"][0]
        usual = dev()["title"]
        self.assertEqual((dev()["usual_title"], dev()["named"]), (usual, False))

        def post(**form):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/devices/name", method="POST",
                                         data=urllib.parse.urlencode(form).encode(), headers={"X-Hub-Page": "1"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(post(slug="nitecrawler", name="  Main   scope  focuser " + "x" * 40), 200)
        self.assertEqual(dev()["title"], ("Main scope focuser " + "x" * 40)[:40], "spaces tidied, 40 characters")
        self.assertTrue(dev()["named"])
        self.assertEqual(post(slug="nope", name="x"), 404)
        self.assertEqual(post(slug="nitecrawler", name="Main scope"), 200)
        # Saved history shows it, and the recorder leaves it beside the files
        self.assertEqual(h.display_title(h.instances["nitecrawler"]), "Main scope")
        h.stop()
        self.hubs.remove(h)
        h = self.new_hub()                                # kept by the unit (its serial), not the tab
        h.scan_once()
        self.assertEqual(dev()["title"], "Main scope")
        # the device's own stored name (alias) when the user gave none
        self.assertEqual(post(slug="nitecrawler", name=""), 200)
        self.assertEqual(dev()["title"], usual)
        h.instances["nitecrawler"].ident["alias"] = "Guide scope"
        self.assertEqual(dev()["title"], "Guide scope")
        self.assertEqual(post(slug="nitecrawler", name="Mine"), 200)
        self.assertEqual(dev()["title"], "Mine", "a name given in the hub wins over the alias")

    def test_renumber_alpaca_numbers(self):
        self.bench.add_nc("COM10", "25905")
        self.bench.add_nc("COM11", "30000")
        h = self.new_hub()
        h.scan_once()
        self.assertTrue(h.alpaca_numbers_json()["tidy"])
        h.stop()
        self.hubs.remove(h)
        del self.bench.ports["COM10"]                     # the first one is gone for good
        h = self.new_hub()
        h.scan_once()
        self.assertEqual([(r["type"], r["number"]) for r in h.device_table()], [("focuser", 1), ("rotator", 1)])
        a = json.loads(self.get(h, "/hub.json")[1])["alpaca_numbers"]
        self.assertEqual((a["tidy"], a["absent"], len(a["changes"])), (False, 2, 2))
        self.assertIn("focuser #1 → #0", a["changes"][0])
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/alpaca/renumber", method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 403)
        h.note_client("nitecrawler-2", "focuser", 1, "connected", "PUT", b"Connected=True")
        ok, msg = h.renumber_alpaca()
        self.assertFalse(ok, "not while a client is connected")
        h.note_client("nitecrawler-2", "focuser", 1, "connected", "PUT", b"Connected=False")
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/alpaca/renumber", method="POST", data=b"",
                                     headers={"X-Hub-Page": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            msg = json.loads(r.read())["message"]
        self.assertIn("2 devices renumbered", msg)
        self.assertIn("forgot the numbers of 2 devices not here", msg)
        self.assertEqual([(r["type"], r["number"]) for r in h.device_table()], [("focuser", 0), ("rotator", 0)])
        self.assertEqual(len(h.settings.numbers), 2)
        self.assertTrue(h.alpaca_numbers_json()["tidy"])
        code, body = self.get(h, "/api/v1/focuser/0/name?ClientID=1&ClientTransactionID=1")
        self.assertEqual(code, 200)
        self.assertEqual(self.get(h, "/api/v1/focuser/1/name?ClientID=1&ClientTransactionID=1")[0], 400)
        # it stays: a new hub run keeps #0
        h.stop()
        self.hubs.remove(h)
        h = self.new_hub()
        h.scan_once()
        self.assertEqual([(r["type"], r["number"]) for r in h.device_table()], [("focuser", 0), ("rotator", 0)])

    # ---------------------------------------------------------------- numbering
    def test_public_numbers_are_stable(self):
        self.bench.add_nc("COM10", "25905")
        self.bench.add_nc("COM11", "30000")
        h = self.new_hub()
        h.scan_once()
        table = {(r["slug"], r["type"]): r["number"] for r in h.device_table()}
        self.assertEqual(table[("nitecrawler", "focuser")], 0)
        self.assertEqual(table[("nitecrawler-2", "focuser")], 1)
        h.stop()
        self.hubs.remove(h)
        # next night only the second unit is plugged in: it must still be Focuser #1, and #0 is absent
        del self.bench.ports["COM10"]
        h2 = self.new_hub()
        h2.scan_once()
        rows = h2.device_table()
        self.assertEqual([(r["type"], r["number"]) for r in rows], [("focuser", 1), ("rotator", 1)])
        self.assertEqual(self.get(h2, "/api/v1/focuser/0/position?ClientID=1&ClientTransactionID=1")[0], 400)
        self.assertEqual(self.put(h2, "/api/v1/focuser/1/connected", Connected="true", ClientID="1",
                                  ClientTransactionID="2")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: json.loads(self.get(
            h2, "/api/v1/focuser/1/position?ClientID=1&ClientTransactionID=3")[1])["ErrorNumber"] == 0))

    # ---------------------------------------------------------------- forwarding
    def test_alpaca_and_page_are_forwarded(self):
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        code, body = self.get(h, "/management/v1/configureddevices?ClientID=1&ClientTransactionID=9")
        d = json.loads(body)
        self.assertEqual(d["ClientTransactionID"], 9)
        self.assertEqual({(v["DeviceType"], v["DeviceNumber"]) for v in d["Value"]}, {("Focuser", 0), ("Rotator", 0)})
        r = self.put(h, "/api/v1/focuser/0/connected", Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertEqual(r["ErrorNumber"], 0)
        self.put(h, "/api/v1/focuser/0/move", Position="52000", ClientID="1", ClientTransactionID="2")
        self.assertTrue(wait_for(lambda: "1SN 00052000" in self.bench.sims["COM10"].log))
        code, body = self.get(h, "/nitecrawler/status.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["port"], "COM10")
        self.assertEqual(self.get(h, "/")[0], 200)
        hub = json.loads(self.get(h, "/hub.json")[1])
        self.assertEqual([d["slug"] for d in hub["devices"]], ["nitecrawler"])

    def test_listen_queue_is_long_enough_for_many_clients(self):
        # tools/soak.py, 2026-11-23: with Python's default listen queue (5), several clients connecting at once
        # to a busy hub overflowed it; Windows refused the connection and the client tried again 0.5 s later.
        # (Only a busy hub overflows it, so this checks the setting; the soak run checks the effect.)
        h = self.new_hub()
        self.assertGreaterEqual(h.httpd.request_queue_size, 64)

    def test_forwarded_address_is_not_a_backdoor(self):
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        inst = h.instances["nitecrawler"]
        # a phone on the network, as the hub forwards it: settings are refused
        req = urllib.request.Request(f"http://127.0.0.1:{inst.local_port}/setup/name", method="POST",
                                     data=b"name=x", headers={"X-NCAlpaca": "1", "X-Forwarded-For": "203.0.113.5"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 403)
        # the same request through the hub from this PC is allowed
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}/nitecrawler/setup/name", method="POST",
                                     data=b"name=Main+scope", headers={"X-NCAlpaca": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    # ---------------------------------------------------------------- Wanderer box and panel
    def test_box_and_panel_found_by_listening_only(self):
        self.bench.add_box("COM15")
        self.bench.add_panel("COM5")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(i.slug for i in h.instances.values()), ["box", "nitecrawler", "panel"])
        types = sorted((r["type"], r["number"]) for r in h.device_table())
        self.assertEqual(types, [("covercalibrator", 0), ("focuser", 0), ("observingconditions", 0), ("rotator", 0),
                                 ("switch", 0)])

    def test_overview_chart_page_and_device_kinds(self):
        # The Overview's chart (/hub/chart/) reads hub.json's device kinds and each device's history.json.
        self.bench.add_box("COM15")
        self.bench.add_panel("COM5")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        code, page = self.get(h, "/hub/chart/")
        self.assertEqual(code, 200)
        self.assertIn("K.chart(", page)
        self.assertIn('id="panel"', page)
        kinds = {d["slug"]: d["kind"] for d in json.loads(self.get(h, "/hub.json")[1])["devices"]}
        self.assertEqual(kinds, {"box": "wbox", "panel": "wcover", "nitecrawler": "nitecrawler"})
        for slug, key in (("box", "vin"), ("panel", "voltage"), ("nitecrawler", "temp")):   # keys the chart page uses
            self.assertTrue(wait_for(lambda: json.loads(self.get(h, f"/{slug}/history.json?res=s")[1])["rows"], 10), slug)
            hist = json.loads(self.get(h, f"/{slug}/history.json?res=s")[1])
            self.assertIn(key, hist["keys"], slug)
            self.assertIn(key, page, f"the chart page knows {slug}'s {key}")
        self.assertIn("Overview", self.get(h, "/")[1])
        self.assertIn('src = \'/hub/chart/\'', self.get(h, "/")[1])

    def test_saved_history_without_devices(self):
        # The Overview lists the drivers' <unit>-history files with no device connected, and serves listed ones only.
        folder = os.path.join(self.dir, "drivers", "wbox", "COM15-history")
        os.makedirs(folder)
        day = "time_local,unix_s,input_V\r\n2026-11-22 21:00,1795870830,12.31\r\n"
        for name in ("wbstock-history-2026-11-21.csv", "wbstock-history-2026-11-22.csv"):
            with open(os.path.join(folder, name), "w", newline="", encoding="utf-8") as f:
                f.write(day)
        with open(os.path.join(folder, "notes.txt"), "w") as f:
            f.write("not a CSV")
        os.makedirs(os.path.join(self.dir, "drivers", "wbox", "COM16-history"))       # empty: not listed
        with open(os.path.join(self.dir, "drivers", "wbox", "COM15.json"), "w") as f:  # settings: not a file listed
            f.write("{}")
        h = self.new_hub()
        self.assertEqual(json.loads(self.get(h, "/hub.json")[1])["devices"], [])
        listing = json.loads(self.get(h, "/hub/history.json")[1])["units"]
        self.assertEqual([(u["driver"], u["unit"]) for u in listing], [("wbox", "COM15")])
        self.assertIsNone(listing[0]["title"], "no device.json: the page falls back to the kind's name")
        from hub.recorder import write_title
        self.assertTrue(write_title(folder, "Power box #2"))       # as the recorder leaves it
        listing = json.loads(self.get(h, "/hub/history.json")[1])["units"]
        self.assertEqual(listing[0]["title"], "Power box #2", "kept for a device that isn't here")
        self.assertEqual([f["name"] for f in listing[0]["files"]],
                         ["wbstock-history-2026-11-21.csv", "wbstock-history-2026-11-22.csv"], "not a day file")
        self.assertEqual([f["name"] for f in listing[0]["files"]],
                         ["wbstock-history-2026-11-21.csv", "wbstock-history-2026-11-22.csv"])
        url = f"http://127.0.0.1:{h.port}/hub/history/wbox/COM15/wbstock-history-2026-11-22.csv"
        with urllib.request.urlopen(url, timeout=10) as r:
            self.assertEqual(r.read().decode(), day)
            self.assertIn("attachment", r.headers["Content-Disposition"])
        with urllib.request.urlopen(f"http://127.0.0.1:{h.port}/hub/history/wbox/COM15.zip", timeout=10) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        self.assertEqual(sorted(z.namelist()), ["wbstock-history-2026-11-21.csv", "wbstock-history-2026-11-22.csv"])
        for bad in ("/hub/history/wbox/COM15/notes.txt", "/hub/history/wbox/COM15/..%2F..%2Fhub.json",
                    "/hub/history/wbox/COM15/..%5CCOM15.json", "/hub/history/wbox/..%2Fwbox/COM15.json",
                    "/hub/history/wbox/COM16.zip", "/hub/history/nope/COM15.zip"):
            self.assertEqual(self.get(h, bad)[0], 404, bad)
        # how long the hub keeps its files: from the page only, within 1..365
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/history/days", method="POST", data=b"days=30")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 403)
        for sent, kept in ((b"days=30", 30), (b"days=0", 1), (b"days=x", 7)):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/history/days", method="POST", data=sent,
                                         headers={"X-Hub-Page": "1"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(json.loads(r.read())["days"], kept)
            self.assertEqual(json.loads(self.get(h, "/hub/history.json")[1])["days"], kept)
        self.assertEqual(H.HubSettings.load(os.path.join(self.dir, "hub.json")).history_days, 7)
        # where the files are, and Open folder: from this PC only, listed folders or the drivers folder
        d = json.loads(self.get(h, "/hub/history.json")[1])
        self.assertEqual(d["folder"], os.path.join(self.dir, "drivers"))
        self.assertEqual(d["units"][0]["folder"], folder)
        self.assertIs(d["local"], True)
        opened = []
        h.show_folder = opened.append

        def open_(data, header=True):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/history/open", method="POST", data=data,
                                         headers={"X-Hub-Page": "1"} if header else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(open_(b"driver=wbox&unit=COM15", header=False), 403)
        self.assertEqual(open_(b"driver=wbox&unit=COM15"), 200)
        self.assertEqual(open_(b""), 200)
        self.assertEqual(open_(b"driver=wbox&unit=..%2F..%2F"), 404)
        self.assertEqual(opened, [folder, os.path.join(self.dir, "drivers")])
        # the chart page is there with no device: the saved files and Open CSV
        page = self.get(h, "/hub/chart/")[1]
        self.assertIn('id="savedList"', page)
        self.assertIn('id="openCsv"', page)

    def test_saved_history_is_where_the_box_writes(self):
        self.bench.add_box("COM15")
        h = self.new_hub()
        h.scan_once()
        folder = h.instances["box"].svc.history.folder
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "wbstock-history-2026-11-23.csv"), "w") as f:
            f.write("time_local,unix_s\r\n")
        listing = json.loads(self.get(h, "/hub/history.json")[1])["units"]
        self.assertEqual([(u["driver"], [f["name"] for f in u["files"]]) for u in listing],
                         [("wbox", ["wbstock-history-2026-11-23.csv"])])
        self.assertEqual(listing[0]["title"], "Power box", "the running device's name")
        # the Overview finds a device's day files by hub.json's unit (its chart reads them back)
        dev = json.loads(self.get(h, "/hub.json")[1])["devices"][0]
        self.assertEqual((dev["kind"], dev["unit"]), (listing[0]["driver"], listing[0]["unit"]))
        self.assertTrue(folder.endswith(os.path.join("wbox", dev["unit"] + "-history")))
        # one days setting: the Overview's reaches the box (its own setting), the box's tab's reaches the hub
        svc = h.instances["box"].svc
        self.assertTrue(wait_for(lambda: h.recorder.sync_days(h.instances["box"]) is not None, 10))
        h.set_history_days(3)
        self.assertEqual(svc.settings.history_days, 3)
        svc.set_history_days(10)
        h.recorder.record_all()
        self.assertEqual(h.settings.history_days, 10)
        self.assertEqual(json.loads(self.get(h, "/hub/history.json")[1])["days"], 10)

    def test_nothing_sent_to_box_or_panel_while_looking(self):
        self.bench.add_box("COM15")
        self.bench.add_panel("COM5")
        h = H.Hub(all_drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=self.bench.lister,
                  opener=self.bench.opener, simulated=True, use_standalone_settings=False, listen_s=0.4)
        for port in self.bench.lister():
            self.assertIsNotNone(h.identify(port), port.device)
        self.assertEqual(self.bench.sims["COM15"]._rx, b"", "the box treats any byte as a stored command")
        self.assertEqual(self.bench.sims["COM5"].log, [])

    def test_panel_through_the_hub(self):
        self.bench.add_panel("COM5")
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/covercalibrator/0/"
        self.assertEqual(self.put(h, url + "connected", Connected="true", ClientID="1", ClientTransactionID="1")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, url + "coverstate?ClientID=1&ClientTransactionID=2")[1])["Value"] == 1))
        self.assertEqual(self.put(h, url + "calibratoron", Brightness="80", ClientID="1", ClientTransactionID="3")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, url + "brightness?ClientID=1&ClientTransactionID=4")[1])["Value"] == 80))
        self.assertEqual(self.put(h, url + "opencover", ClientID="1", ClientTransactionID="5")["ErrorNumber"], 0)
        self.assertEqual(json.loads(self.get(h, url + "coverstate?ClientID=1&ClientTransactionID=6")[1])["Value"], 2)  # moving
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, url + "coverstate?ClientID=1&ClientTransactionID=7")[1])["Value"] == 3))
        r = self.put(h, url + "calibratoron", Brightness="80", ClientID="1", ClientTransactionID="8")
        self.assertEqual(r["ErrorNumber"], 0x40B, "the panel only lights while closed")
        self.assertEqual(self.bench.sims["COM5"].log, [80, 1001])

    def test_panel_calibrator_on_at_zero_is_ready(self):
        # ConformU: CalibratorOn(0) must end Ready, not Off, though the panel only knows "light off" (9999).
        self.bench.add_panel("COM5")
        h = self.new_hub()
        h.scan_once()
        url = "/api/v1/covercalibrator/0/"
        self.put(h, url + "connected", Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, url + "coverstate?ClientID=1&ClientTransactionID=2")[1])["Value"] == 1))
        state = lambda: json.loads(self.get(h, url + "calibratorstate?ClientID=1&ClientTransactionID=3")[1])["Value"]
        self.assertEqual(self.put(h, url + "calibratoron", Brightness="0", ClientID="1", ClientTransactionID="4")["ErrorNumber"], 0)
        self.assertTrue(wait_for(lambda: state() == 3), "Ready")
        self.assertEqual(self.bench.sims["COM5"].log, [9999])
        self.put(h, url + "calibratoroff", ClientID="1", ClientTransactionID="5")
        self.assertTrue(wait_for(lambda: state() == 1), "Off")

    def test_panel_page_controls_and_lock(self):
        self.bench.add_panel("COM5")
        h = self.new_hub()
        h.scan_once()

        def control(**form):
            req = urllib.request.Request(f"http://127.0.0.1:{h.port}/panel/control", method="POST",
                                         data=urllib.parse.urlencode(form).encode(), headers={"X-Hub-Page": "1"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        self.assertTrue(wait_for(lambda: h.instances["panel"].up()))
        self.assertEqual(control(op="heater", value="150")[0], 200)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["heater"] == 150))
        self.assertEqual(control(op="heater", value="70")[0], 409)          # not one of the four levels
        # angles: Wanderer's 10000 / 40000 + degrees x 100; the cap doesn't move
        sim = self.bench.sims["COM5"]
        self.assertEqual(control(op="angle", which="close", value="20.55")[0], 200)
        self.assertEqual(control(op="angle", which="open", value="270")[0], 200)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["open_set"] == 270.0))
        self.assertEqual(json.loads(self.get(h, "/panel/status.json")[1])["close_set"], 20.55)
        self.assertIn(12055, sim.log)
        self.assertIn(67000, sim.log)
        self.assertNotIn(1000, sim.log)
        self.assertNotIn(1001, sim.log)
        for bad in (dict(which="close", value="5"), dict(which="open", value="301"), dict(which="open", value="x"),
                    dict(which="middle", value="50")):
            self.assertEqual(control(op="angle", **bad)[0], 409, bad)
        self.assertFalse([n for n in sim.log if n >= 100000], "never the panel's auto-detect")
        # jog from closed (20.55°): moves to 25.55° and makes that the closed angle. The cap still sits at 15° from
        # before the angle was set, which is neither end, so a jog is refused until it is at one. A second Close
        # doesn't move it (the panel ignores a repeat of its last move), so open and close.
        self.assertEqual(control(op="jog", value="5")[0], 409)
        self.assertEqual(control(op="close")[0], 200)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["cover"] != "moving"))
        self.assertEqual(json.loads(self.get(h, "/panel/status.json")[1])["position"], 15.0)
        self.assertEqual(control(op="open")[0], 200)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["cover"] == "open"))
        self.assertEqual(control(op="close")[0], 200)
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["position"] == 20.55))
        self.assertEqual(control(op="jog", value="5")[0], 200)
        self.assertTrue(wait_for(lambda: sim.log[-2:] == [12555, 1000]), sim.log)
        self.assertEqual(control(op="jog", value="5")[0], 409)              # still moving
        self.assertTrue(wait_for(lambda: json.loads(self.get(h, "/panel/status.json")[1])["cover"] == "closed", 15))
        self.assertEqual(json.loads(self.get(h, "/panel/status.json")[1])["position"], 25.55)
        self.put(h, "/api/v1/covercalibrator/0/connected", Connected="true", ClientID="1", ClientTransactionID="1")
        self.assertEqual(control(op="angle", which="close", value="15")[0], 409)   # locked while NINA has it
        self.assertEqual(control(op="open")[0], 409)                        # NINA has it: locked
        self.assertEqual(control(op="open", override="1")[0], 200)
        self.assertTrue(wait_for(lambda: 1001 in self.bench.sims["COM5"].log))

    def test_port_stuck_in_the_driver_does_not_stop_the_scan(self):
        # 2026-11-20, real hardware: the CH340 driver never returned from SetCommState on the box's port, and the
        # scanner thread waited on it forever, so nothing after it was ever found.
        import threading
        release = threading.Event()
        self.bench.add_panel("COM11")
        self.bench.add_box("COM8")
        self.bench.add_nc("COM10", "25905")
        real = self.bench.opener

        def opener(device, baud, lines=False):
            if device == "COM8":
                release.wait(30)                     # like the driver: never comes back while plugged in
                raise OSError("device removed")
            return real(device, baud, lines)

        self.bench.opener = opener
        h = H.Hub(all_drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=self.bench.lister,
                  opener=opener, simulated=True, use_standalone_settings=False, listen_s=0.2, identify_s=1.0)
        h.start(scan=False)
        self.hubs.append(h)
        try:
            h.scan_once()
            self.assertEqual(sorted(h.instances), ["nitecrawler", "panel"])
            self.assertEqual(h.hub_json()["stuck"], ["COM8"])
            h.scan_once()                            # a stuck port isn't opened again while it is stuck
            self.assertEqual(sorted(h.instances), ["nitecrawler", "panel"])
            del self.bench.ports["COM8"]             # unplugging is what frees it
            h.scan_once()
            self.assertEqual(h.hub_json()["stuck"], [])
        finally:
            release.set()

    def test_slow_port_is_used_when_it_gets_through(self):
        # 2026-11-20: COM11's open took ~2 min each time; re-asking after it came back just started another 2 min.
        self.bench.add_panel("COM11")
        real = self.bench.opener
        opens = []

        def opener(device, baud, lines=False):
            opens.append(device)
            if device == "COM11" and len(opens) == 1:
                time.sleep(1.5)                      # only the first open is slow
            return real(device, baud, lines)

        h = H.Hub(all_drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=self.bench.lister,
                  opener=opener, simulated=True, use_standalone_settings=False, listen_s=0.2, identify_s=0.5)
        h.start(scan=False)
        self.hubs.append(h)
        h.scan_once()
        self.assertEqual(h.hub_json()["stuck"], ["COM11"])
        self.assertTrue(wait_for(lambda: not h._stuck["COM11"][0].is_alive(), 5))
        n = len(opens)
        h.scan_once()
        self.assertIn("panel", h.instances)
        self.assertEqual(h.hub_json()["stuck"], [])
        self.assertTrue(wait_for(lambda: h.instances["panel"].up()))
        self.assertEqual(opens[n:].count("COM11"), 0, "the link gets the handle identify kept open")

    def test_each_port_is_opened_once_like_empire(self):
        # USB selective suspend lets the box's USB hub sleep whenever nothing behind it is open; each close/reopen
        # cycle risked SetCommState hanging. Detection now hands the open port to the link: one open per device.
        self.bench.add_panel("COM11")
        self.bench.add_box("COM8")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        for slug in ("panel", "box", "nitecrawler"):
            self.assertTrue(wait_for(lambda: h.instances[slug].up()), slug)
        self.assertEqual(sorted(self.bench.opened), ["COM10", "COM11", "COM8"])
        self.assertEqual(sorted(h._relays), ["COM10", "COM11", "COM8"], "the hub holds each port, relayed")
        self.assertEqual(self.bench.sims["COM8"]._rx, b"", "still nothing sent to the box while looking")

    def test_every_port_is_identified_before_any_instance_starts(self):
        self.bench.add_panel("COM11")
        self.bench.add_box("COM8")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        started = []
        for d in h.drivers:
            orig = d.start

            def start(hub, port, ident, orig=orig):
                started.append((port.device, list(self.bench.opened)))
                return orig(hub, port, ident)

            d.start = start
        h.scan_once()
        self.assertEqual(len(started), 3)
        opened_before_first_start = set(started[0][1])
        self.assertEqual(opened_before_first_start, {"COM8", "COM10", "COM11"})

    # ---------------------------------------------------------------- going away
    def test_unplugged_device_is_dropped(self):
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        self.assertIn("nitecrawler", h.instances)
        del self.bench.ports["COM10"]
        old = H.GONE_S
        H.GONE_S = 0.0
        try:
            h.scan_once()
        finally:
            H.GONE_S = old
        self.assertEqual(h.instances, {})
        self.assertEqual(h.device_table(), [])

    def test_replugging_with_two_units_never_reuses_a_tab(self):
        # #1 and #2 run; #1 is unplugged and another unit plugged in elsewhere. It must not become a second #2 (that
        # replaced #2's entry and left the old #2 running, unstopped, holding its port).
        self.bench.add_box("COM15")
        self.bench.add_box("COM16")
        self.bench.add_nc("COM10", "25905")
        self.bench.add_nc("COM11", "31000")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(sorted(h.instances), ["box", "box-2", "nitecrawler", "nitecrawler-2"])
        second_box, second_nc = h.instances["box-2"], h.instances["nitecrawler-2"]
        del self.bench.ports["COM15"], self.bench.ports["COM10"]
        self.bench.add_box("COM17")
        self.bench.add_nc("COM12", "40000")
        h.scan_once()           # the old #1s are only missing so far: they keep their numbers
        self.assertEqual(sorted(h.instances),
                         ["box", "box-2", "box-3", "nitecrawler", "nitecrawler-2", "nitecrawler-3"])
        self.assertEqual(h.instances["box-3"].port.device, "COM17")
        old = H.GONE_S
        H.GONE_S = 0.0
        try:
            h.scan_once()       # now they are dropped
        finally:
            H.GONE_S = old
        self.assertEqual(sorted(h.instances), ["box-2", "box-3", "nitecrawler-2", "nitecrawler-3"])
        self.bench.add_box("COM18")
        self.bench.add_nc("COM13", "50000")
        h.scan_once()           # #1 stays the dropped units' (remembered, for when they come back): these get #4
        self.assertEqual(sorted(h.instances),
                         ["box-2", "box-3", "box-4", "nitecrawler-2", "nitecrawler-3", "nitecrawler-4"])
        self.assertEqual((h.instances["box-4"].port.device, h.instances["box-4"].title), ("COM18", "Power box #4"))
        self.assertEqual(h.instances["nitecrawler-4"].port.device, "COM13")
        self.assertIs(h.instances["box-2"], second_box)
        self.assertIs(h.instances["nitecrawler-2"], second_nc)
        self.assertEqual(h.instances["box-3"].title, "Power box #3")
        # Overview's Renumber tabs: the dropped ones are forgotten and the rest close up, in their order
        self.assertTrue(h.renumber_units("wbox")[0])
        h.scan_once()
        self.assertEqual({s: i.port.device for s, i in h.instances.items() if i.driver.key == "wbox"},
                         {"box": "COM16", "box-2": "COM17", "box-3": "COM18"})

    def test_a_clashing_slug_never_replaces_a_running_device(self):
        self.bench.add_box("COM15")
        self.bench.add_box("COM16")
        h = self.new_hub()
        drv = next(d for d in h.drivers if d.key == "wbox")
        real = drv.start
        drv.start = lambda ctx, port, ident: (lambda i: (setattr(i, "slug", "box"), i)[1])(real(ctx, port, ident))
        try:
            h.scan_once()
        finally:
            drv.start = real
        self.assertEqual(list(h.instances), ["box"])
        self.assertEqual(h.instances["box"].port.device, "COM15")

    # ---------------------------------------------------------------- ignored ports (Overview's Serial ports)
    def post_page(self, h, path, **form):
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}{path}", method="POST",
                                     data=urllib.parse.urlencode(form).encode(), headers={"X-Hub-Page": "1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_an_ignored_port_is_never_opened_again(self):
        # The Fluke meter's FTDI cable (2026-11-22): every scan opened it, the open hung, and a probe may have sent it
        # bytes. Ignored, it is left alone, after a restart too, and by its serial number on any COM port.
        self.bench.ports["COM12"] = PortInfo("COM12", *FTDI, "A9001JG8A", "Fluke cable")
        self.bench.add_nc("COM10", "25905")
        h = self.new_hub()
        h.scan_once()
        self.assertIn("COM12", self.bench.opened)
        ports = {p["device"]: p for p in json.loads(self.get(h, "/hub.json")[1])["ports"]}
        self.assertEqual(ports["COM12"]["state"], "not recognised")
        self.assertEqual(ports["COM10"]["state"], "in use")
        code, r = self.post_page(h, "/hub/ports/ignore", device="COM10")
        self.assertEqual(code, 400, "not a port a device is on")
        self.assertIn("turn its driver off", r["error"])
        self.assertEqual(self.post_page(h, "/hub/ports/ignore", device="COM12"), (200, {"ok": True, "error": ""}))
        with open(os.path.join(self.dir, "hub.json"), encoding="utf-8") as f:
            self.assertEqual([e["serial"] for e in json.load(f)["ports_ignored"]], ["A9001JG8A"], "remembered")
        self.bench.opened.clear()
        h.rescan()
        h.scan_once()
        self.assertNotIn("COM12", self.bench.opened)
        ports = {p["device"]: p for p in json.loads(self.get(h, "/hub.json")[1])["ports"]}
        self.assertEqual((ports["COM12"]["state"], ports["COM12"]["key"]), ("ignored", "A9001JG8A"))
        h.stop()
        # another day, another COM number: still the same cable
        del self.bench.ports["COM12"]
        self.bench.ports["COM14"] = PortInfo("COM14", *FTDI, "A9001JG8A", "Fluke cable")
        h2 = self.new_hub()
        h2.scan_once()
        self.assertNotIn("COM14", self.bench.opened)
        self.assertEqual(self.bench.written, [], "not one byte to it")
        # unplugged: still listed as ignored, so it can be stopped
        del self.bench.ports["COM14"]
        h2.scan_once()
        ports = json.loads(self.get(h2, "/hub.json")[1])["ports"]
        self.assertIn(("ignored, not plugged in", "A9001JG8A"), [(p["state"], p["key"]) for p in ports])
        self.bench.ports["COM14"] = PortInfo("COM14", *FTDI, "A9001JG8A", "Fluke cable")
        self.assertEqual(self.post_page(h2, "/hub/ports/unignore", key="A9001JG8A")[0], 200)
        h2.scan_once()
        self.assertIn("COM14", self.bench.opened, "looked at again")

    def test_a_port_without_a_serial_number_is_ignored_by_its_com_port(self):
        self.bench.add("COM5", CH340, "CH340 device")
        h = self.new_hub()
        h.scan_once()
        self.assertEqual(self.post_page(h, "/hub/ports/ignore", device="COM5")[0], 200)
        self.bench.opened.clear()
        h.rescan()
        h.scan_once()
        self.assertEqual(self.bench.opened, [])
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}/hub/ports/unignore", method="POST", data=b"key=COM5")
        with self.assertRaises(urllib.error.HTTPError) as e:     # not from the hub's page
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)

    # ---------------------------------------------------------------- scan now (the Overview's button)
    def test_scan_now_finds_a_device_plugged_in_later(self):
        h = H.Hub(all_drivers(), config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=self.bench.lister,
                  opener=self.bench.opener, simulated=True, use_standalone_settings=False, listen_s=0.2,
                  scan_s=3600)                  # the automatic pass would not come for an hour
        h.start(scan=True)
        self.hubs.append(h)
        self.assertTrue(wait_for(lambda: h.last_scan > 0), "the first pass runs at start")
        self.bench.add_panel("COM5")            # plugged in after that pass
        url = f"http://127.0.0.1:{h.port}/hub/rescan"
        with self.assertRaises(urllib.error.HTTPError) as e:   # not from the hub's page: refused
            urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b""), timeout=5)
        self.assertEqual(e.exception.code, 403)
        req = urllib.request.Request(url, method="POST", data=b"", headers={"X-Hub-Page": "1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            requested = json.loads(r.read())["requested"]
        done = lambda: (lambda j: not j["scanning"] and j["scan_started"] >= requested  # noqa: E731
                        and j["last_scan"] >= j["scan_started"])(json.loads(self.get(h, "/hub.json")[1]))
        self.assertTrue(wait_for(done, timeout=10), "a pass that started after the request finished")
        self.assertIn("panel", h.instances)
        self.assertEqual(self.bench.sims["COM5"].log, [], "found by listening, as always")


if __name__ == "__main__":
    unittest.main()
