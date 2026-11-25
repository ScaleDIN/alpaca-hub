"""DeviceServer's Alpaca protocol details, as ConformU's protocol check wants them (2026-11-22: it found PUT names
read case-insensitively and an upper-case device type accepted, after the NiteCrawler moved onto DeviceServer)."""
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hub.devserver import (NOT_CONNECTED, AlpacaDevice, AlpacaError, App, BadRequest, DeviceServer,  # noqa: E402
                           put_params)


class Thing(AlpacaDevice):
    type, type_name = "focuser", "Focuser"
    actions = ("Ping",)

    def __init__(self):
        super().__init__("uid-1", "thing", "a test device")
        self.moved = []
        self.up = True
        self.shown = True

    def present(self):
        return self.shown

    def set_connected(self, on):
        if on and not self.up:
            raise AlpacaError(0x500, "not answering")
        self.connected = on

    def action(self, name, parameters):
        return f"pong {parameters}"

    def put(self, m, p):
        if m != "move":
            raise KeyError(m)
        if "position" not in p:
            raise BadRequest("Missing Position")
        self.moved.append(int(p["position"]))


class PageApp(App):
    def route(self, req):
        if req.path == "/extra":
            return 200, f"{req.command} {req.params.get('x')} local={req.local}", "text/plain"
        return None


class DeviceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev = Thing()
        cls.srv = DeviceServer([cls.dev], PageApp(), "1.0")
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def req(self, method, path, body=None):
        r = urllib.request.Request(self.base + path, method=method, data=body.encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_put_names_are_case_sensitive(self):
        self.dev.connected = True
        code, _ = self.req("PUT", "/api/v1/focuser/0/move", "position=100&ClientTransactionID=3")
        self.assertEqual(code, 400, "a wrongly cased Position is missing")
        code, body = self.req("PUT", "/api/v1/focuser/0/move", "Position=200&clienttransactionid=4")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["ClientTransactionID"], 0, "a wrongly cased ClientTransactionID is ignored")
        self.assertEqual(self.dev.moved[-1:], [200])
        self.assertNotIn(100, self.dev.moved)
        self.assertEqual(put_params("Position=1&position=2&Custom=x"), {"position": "1", "custom": "x"})

    def test_get_names_are_not(self):
        code, body = self.req("GET", "/api/v1/focuser/0/name?clienttransactionid=9")
        self.assertEqual((code, json.loads(body)["ClientTransactionID"]), (200, 9))

    def test_device_type_must_be_lower_case(self):
        self.assertEqual(self.req("GET", "/api/v1/FOCUSER/0/name")[0], 400)

    def test_bad_client_ids(self):
        self.assertEqual(self.req("GET", "/api/v1/focuser/0/name?ClientID=-1")[0], 400)
        self.assertEqual(self.req("GET", "/api/v1/focuser/0/name?ClientTransactionID=x")[0], 400)

    def test_connect_can_be_refused_and_absent_devices_vanish(self):
        self.dev.up = False
        try:
            r = json.loads(self.req("PUT", "/api/v1/focuser/0/connected", "Connected=True")[1])
            self.assertEqual(r["ErrorNumber"], 0x500)
        finally:
            self.dev.up = True
        self.dev.shown = False
        try:
            self.assertEqual(self.req("GET", "/api/v1/focuser/0/name")[0], 404)
            devs = json.loads(self.req("GET", "/management/v1/configureddevices")[1])["Value"]
            self.assertEqual(devs, [])
        finally:
            self.dev.shown = True

    def test_actions(self):
        self.dev.connected = False
        r = json.loads(self.req("PUT", "/api/v1/focuser/0/action", "Action=Ping&Parameters=1")[1])
        self.assertEqual(r["ErrorNumber"], NOT_CONNECTED)
        self.dev.connected = True
        r = json.loads(self.req("PUT", "/api/v1/focuser/0/action", "Action=Ping&Parameters=1")[1])
        self.assertEqual(r["Value"], "pong 1")
        self.assertEqual(json.loads(self.req("GET", "/api/v1/focuser/0/supportedactions")[1])["Value"], ["Ping"])

    def test_app_routes(self):
        self.assertEqual(self.req("POST", "/extra", "x=5"), (200, "POST 5 local=True"))
        self.assertEqual(self.req("GET", "/nothing")[0], 404)


class HistoryTests(unittest.TestCase):
    def test_10_second_steps_with_lowest_and_highest(self):
        from hub.devserver import History
        h = History(["v", "w"])
        t0 = 1795800000                                   # a whole 10 s
        for i in range(10):
            h.add(t0 + i, {"v": 12.0 if i != 4 else 10.8, "w": None})   # a 1 s dip the average hides
        h.add(t0 + 10, {"v": 12.5, "w": 3.0})
        h.add(t0 + 20, {"v": 12.5, "w": 3.0})
        d = h.get("m")
        self.assertEqual(d["step"], 10)
        self.assertEqual(d["keys"], ["v", "w", "v_min", "w_min", "v_max", "w_max"])
        self.assertEqual(d["rows"], [[t0, 11.88, None, 10.8, None, 12.0, None], [t0 + 10, 12.5, 3.0, 12.5, 3.0, 12.5, 3.0]])
        self.assertEqual(h.get("m", since=t0)["rows"], [d["rows"][1]])
        s = h.get("s")
        self.assertEqual((s["keys"], s["step"], len(s["rows"])), (["v", "w"], 1, 12))

    def test_held_keys_last_reading_and_exact_changes(self):
        from hub.devserver import History
        h = History(["pos", "temp"], held=("pos", "nope"))
        t0 = 1795800000
        for i in range(10):                               # 5600, a move to 5800 and back inside one step
            h.add(t0 + i, {"pos": 5800 if 3 <= i < 5 else 5600, "temp": 20.0 + i})
        for i in range(10, 21):
            h.add(t0 + i, {"pos": 5700 if i < 15 else None, "temp": 21.0})
        d = h.get("m")
        self.assertEqual(d["held"], ["pos"])
        self.assertEqual(d["rows"][0][:3], [t0, 5600, 24.5])           # the last reading, never 5640
        self.assertEqual(d["rows"][0][3], 5600)                        # pos_min
        self.assertEqual(d["rows"][0][5], 5800)                        # pos_max: the move still shows
        self.assertEqual(d["rows"][1][1], 5700)                        # the last reading the step had
        c = h.get("c")
        self.assertEqual(c["keys"], ["pos"])
        self.assertEqual(c["rows"], [[t0, "pos", 5600], [t0 + 3, "pos", 5800], [t0 + 5, "pos", 5600],
                                     [t0 + 10, "pos", 5700], [t0 + 15, "pos", None]])
        self.assertEqual(h.get("c", since=t0 + 5)["rows"], c["rows"][3:])
        self.assertEqual(History(["v"]).get("m")["held"], [])

    def test_changes_kept_for_a_day(self):
        from hub.devserver import History, KEEP_S
        h = History(["pos"], held=("pos",))
        h.add(1000, {"pos": 1})
        h.add(1001, {"pos": 2})
        h.add(1001 + KEEP_S, {"pos": 3})
        self.assertEqual(h.get("c")["rows"], [[1001, "pos", 2], [1001 + KEEP_S, "pos", 3]])


if __name__ == "__main__":
    unittest.main()
