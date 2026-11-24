"""The hub's update check (hub/updates.py): reading the published list, what it offers, the Overview's setting."""
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from hub import VERSION  # noqa: E402
from hub import host as H  # noqa: E402
from hub import updates as U  # noqa: E402


def bump(v, i=-1):
    t = list(U.vt(v))
    t[i] += 1
    return ".".join(map(str, t))


NEWER_HUB = bump(VERSION)
FEED = {"feed": 1, "released": "2026-12-01",
        "hub": {"version": NEWER_HUB, "api": 1, "url": "https://example.org/r/AlpacaHub-x-Setup.exe", "date": "2026-12-01",
                "notes": "- Hub thing.", "sha256": "0" * 64, "size": 1, "file": "x"},
        "drivers": {
            "optec": {"version": "1.0.2", "title": "Optec", "api": 1, "min_hub": VERSION, "url": "https://e/o.exe",
                      "notes": "- Optec thing.", "date": "2026-12-01"},
            "wcover": {"version": "1.0.3", "title": "Cover", "api": 1, "url": "https://e/c.exe", "notes": ""},
            "wbox": {"version": "1.1.0", "title": "Box", "api": 1, "min_hub": NEWER_HUB, "url": "https://e/b.exe"},
            "nitecrawler": {"version": "2.0.0", "title": "NC", "api": 1, "min_hub": bump(VERSION, 0)},
            "thermo": {"version": "9.0.0", "title": "Not installed here", "api": 1}}}
HERE_DRIVERS = {"optec": ("1.0.1", "Optec"), "wcover": ("1.0.3", "Cover"), "wbox": ("1.0.1", "Box"),
                "nitecrawler": ("1.0.1", "NC")}


class CompareTest(unittest.TestCase):
    def test_what_is_offered(self):
        found = {a["key"]: a for a in U.compare(FEED, VERSION, HERE_DRIVERS)}
        self.assertEqual(set(found), {"hub", "optec", "wbox", "nitecrawler"}, "wcover is current, thermo isn't here")
        self.assertEqual((found["hub"]["version"], found["hub"]["installed"], found["hub"]["blocked"]),
                         (NEWER_HUB, VERSION, ""))
        self.assertEqual(found["optec"]["blocked"], "")
        self.assertEqual(found["optec"]["notes"], "- Optec thing.")
        self.assertIn("update the hub first", found["wbox"]["blocked"])
        self.assertIn(f"needs Alpaca Hub {bump(VERSION, 0)} or newer", found["nitecrawler"]["blocked"])
        self.assertNotIn("update the hub first", found["nitecrawler"]["blocked"], "no listed hub is new enough")

    def test_versions_compare_as_numbers_and_bad_entries_are_skipped(self):
        feed = {"feed": 1, "drivers": {"optec": {"version": "1.0.10"}, "wcover": {"version": "soon"},
                                       "wbox": "not an entry"}}
        found = U.compare(feed, VERSION, {"optec": ("1.0.9", "O"), "wcover": ("1.0.0", "C"), "wbox": ("1.0.0", "B")})
        self.assertEqual([a["key"] for a in found], ["optec"])
        self.assertEqual(U.compare({"feed": 1}, VERSION, HERE_DRIVERS), [])

    def test_other_api_is_blocked(self):
        feed = {"feed": 1, "drivers": {"optec": {"version": "2.0.0", "api": 2}}}
        self.assertIn("hub API 2", U.compare(feed, VERSION, HERE_DRIVERS)[0]["blocked"])


class Server:
    """A local list server that remembers each request's headers."""

    def __init__(self, body):
        self.body, self.requests, self.files = body, [], {}     # files: {"/name.exe": bytes}
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                srv.requests.append(dict(self.headers, path=self.path))
                if self.path in srv.files:
                    data = srv.files[self.path]
                else:
                    data = srv.body if isinstance(srv.body, bytes) else json.dumps(srv.body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/releases.json"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.srv = Server(FEED)
        self.addCleanup(self.srv.close)

    def test_sends_only_the_hubs_version(self):
        self.assertEqual(U.fetch(self.srv.url)["hub"]["version"], NEWER_HUB)
        h = {k.lower(): v for k, v in self.srv.requests[0].items()}
        self.assertEqual(h["user-agent"], f"AlpacaHub/{VERSION}")
        self.assertLessEqual(set(h), {"host", "user-agent", "accept", "accept-encoding", "connection", "path"})

    def test_refusals(self):
        with self.assertRaises(ValueError):
            U.fetch("http://example.org/releases.json")          # not https, not this PC
        self.srv.body = {"feed": 2}
        with self.assertRaisesRegex(ValueError, "feed 2"):
            U.fetch(self.srv.url)
        self.srv.body = b"x" * (U.MAX_BYTES + 10)
        with self.assertRaisesRegex(ValueError, "too big"):
            U.fetch(self.srv.url)


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        self.on = True

    def make(self, fetcher=None, url="https://example.org/releases.json"):
        def f(u):
            self.calls.append(u)
            return FEED
        return U.Updates(self.dir, enabled=lambda: self.on, installed=lambda: HERE_DRIVERS, url=url,
                         fetcher=fetcher or f)

    def test_check_keeps_the_result_across_a_restart(self):
        u = self.make()
        self.assertEqual(u.check(), "")
        self.assertEqual(len(u.status()["available"]), 4)
        again = self.make()
        self.assertEqual(again.last_check, u.last_check)
        self.assertEqual(len(again.status()["available"]), 4)

    def test_a_failed_check_keeps_the_last_list(self):
        u = self.make()
        u.check()
        def broken(url):
            raise OSError("offline")
        u.fetcher = broken
        self.assertIn("offline", u.check())
        st = u.status()
        self.assertIn("offline", st["error"])
        self.assertEqual(len(st["available"]), 4)
        self.assertLess(st["last_ok"], st["last_check"] + 1)

    def test_thread_checks_once_then_waits_a_day_and_never_when_off(self):
        stop = threading.Event()
        u = self.make()
        t = threading.Thread(target=u.run, args=(stop,), kwargs={"first_s": 0.2}, daemon=True)
        t.start()
        deadline = time.time() + 5
        while not self.calls and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        self.assertEqual(len(self.calls), 1)
        u.check_now()
        deadline = time.time() + 5
        while len(self.calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.calls), 2, "Check now")
        stop.set()
        u._wake.set()
        t.join(5)
        # off: the thread sends nothing, and nothing is offered
        self.on = False
        stop2 = threading.Event()
        u2 = U.Updates(tempfile.mkdtemp(dir=self.dir), enabled=lambda: self.on, installed=lambda: HERE_DRIVERS,
                       url="https://example.org/r.json", fetcher=lambda url: self.calls.append(url) or FEED)
        t2 = threading.Thread(target=u2.run, args=(stop2,), kwargs={"first_s": 0.1}, daemon=True)
        t2.start()
        time.sleep(0.6)
        stop2.set()
        u2._wake.set()
        t2.join(5)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(u.status()["available"], [])

    def test_no_address_checks_nothing(self):
        u = self.make(url="")
        self.assertIn("no update address", u.check())
        self.assertEqual(self.calls, [])
        self.assertFalse(u.status()["address"])


class InstallTest(unittest.TestCase):
    """Install: download from the list's host, check size and SHA-256, run the installer (a stand-in here)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = Server(None)
        self.addCleanup(self.srv.close)
        self.ran = []
        base = self.srv.url.rsplit("/", 1)[0]
        self.hub_exe, self.drv_exe = b"MZ new hub" * 1000, b"MZ new optec"
        self.srv.files = {"/AlpacaHub-9.9.9-Setup.exe": self.hub_exe, "/AlpacaHub-optec-1.0.2-Setup.exe": self.drv_exe}
        sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
        self.srv.body = {"feed": 1,
                         "hub": {"version": "9.9.9", "api": 1, "file": "AlpacaHub-9.9.9-Setup.exe",
                                 "url": base + "/AlpacaHub-9.9.9-Setup.exe", "sha256": sha(self.hub_exe),
                                 "size": len(self.hub_exe)},
                         "drivers": {"optec": {"version": "1.0.2", "api": 1, "file": "AlpacaHub-optec-1.0.2-Setup.exe",
                                               "url": base + "/AlpacaHub-optec-1.0.2-Setup.exe",
                                               "sha256": sha(self.drv_exe), "size": len(self.drv_exe)},
                                     "wbox": {"version": "1.1.0", "api": 1, "min_hub": "99.0",
                                              "file": "AlpacaHub-wbox-1.1.0-Setup.exe", "url": base + "/w.exe",
                                              "sha256": "0" * 64, "size": 5}}}
        self.u = U.Updates(self.dir, enabled=lambda: True, installed=lambda: HERE_DRIVERS, url=self.srv.url,
                           runner=self.record)
        self.assertEqual(self.u.check(), "")

    def record(self, path, args):
        with open(path, "rb") as f:
            self.ran.append((path, args, f.read()))

    def wait_state(self, key, states=("started", "failed")):
        end = time.time() + 10
        while time.time() < end:
            st = self.u.status()["installs"].get(key)
            if st and st["state"] in states:
                return st
            time.sleep(0.05)
        self.fail(f"{key}: {self.u.status()['installs']}")

    def test_driver_and_hub_installers_run_with_their_arguments(self):
        self.assertEqual(self.u.install("optec"), "")
        st = self.wait_state("optec")
        self.assertEqual(st["state"], "started", st)
        self.assertEqual(st["done"], len(self.drv_exe))
        path, args, data = self.ran[0]
        self.assertEqual(os.path.basename(path), "AlpacaHub-optec-1.0.2-Setup.exe")
        self.assertEqual(args, "/SILENT /SP- /NORESTART")
        self.assertEqual(data, self.drv_exe)
        self.assertEqual(self.u.install("hub"), "")
        self.assertEqual(self.wait_state("hub")["state"], "started")
        self.assertIn("/RESTARTHUB", self.ran[1][1])
        self.assertEqual(os.listdir(os.path.join(self.dir, "updates")), ["AlpacaHub-9.9.9-Setup.exe"],
                         "the earlier download was cleaned up")
        self.u.clean()
        self.assertEqual(os.listdir(os.path.join(self.dir, "updates")), [])

    def test_a_download_that_isnt_the_listed_one_is_not_run(self):
        self.srv.files["/AlpacaHub-optec-1.0.2-Setup.exe"] = b"MZ tampered!"      # same size, other bytes
        self.u.install("optec")
        st = self.wait_state("optec")
        self.assertEqual(st["state"], "failed")
        self.assertIn("SHA-256", st["error"])
        self.srv.files["/AlpacaHub-optec-1.0.2-Setup.exe"] = self.drv_exe + b"more"
        self.u.install("optec")
        self.assertIn("bigger than the list says", self.wait_state("optec")["error"])
        self.assertEqual(self.ran, [])
        self.assertEqual(os.listdir(os.path.join(self.dir, "updates")), [], "nothing left behind")

    def test_refusals(self):
        self.assertIn("needs Alpaca Hub 99.0", self.u.install("wbox"))
        self.assertIn("no update for", self.u.install("wcover"))
        self.u.feed["drivers"]["optec"]["url"] = "https://example.org/AlpacaHub-optec-1.0.2-Setup.exe"
        self.assertIn("list's own https host", self.u.install("optec"))
        self.u.feed["drivers"]["optec"]["url"] = self.u.feed["hub"]["url"].replace("9.9.9", "optec-1.0.2")
        del self.u.feed["drivers"]["optec"]["sha256"]
        self.assertIn("no file, size or SHA-256", self.u.install("optec"))
        self.assertEqual(self.ran, [])

    def test_one_at_a_time_and_a_cancelled_prompt(self):
        gate = threading.Event()

        def slow(path, args):
            gate.wait(5)
            raise OSError(None, "cancelled", None, 1223)

        self.u.runner = slow
        self.assertEqual(self.u.install("optec"), "")
        self.wait_state("optec", ("starting",))
        self.assertIn("wait for it", self.u.install("hub"))
        gate.set()
        st = self.wait_state("optec")
        self.assertEqual(st["error"], "Windows' administrator prompt was cancelled")

    def test_off_installs_nothing(self):
        self.u.enabled = lambda: False
        self.assertIn("off", self.u.install("optec"))


class HubTest(unittest.TestCase):
    """Through the hub: hub.json's "updates", the Overview's setting and Check now."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.srv = Server(FEED)
        self.addCleanup(self.srv.close)

    def start(self, **kw):
        h = H.Hub([], config_dir=self.dir, bind="127.0.0.1", http_port=0, port_lister=lambda: [], simulated=True,
                  use_standalone_settings=False, **kw)
        h.updates.installed = lambda: HERE_DRIVERS
        h.start(scan=False)
        self.addCleanup(h.stop)
        self.base = f"http://127.0.0.1:{h.port}"
        return h

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def post(self, path, body=b"", page=True):
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"X-Hub-Page": "1"} if page else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def wait(self, cond, s=5):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_check_now_turn_off_and_on(self):
        self.start(update_feed=self.srv.url)
        u = self.get("/hub.json")["updates"]
        self.assertTrue(u["on"] and u["enabled"] and u["address"])
        self.assertEqual(u["last_check"], 0, "not straight after start")
        self.post("/hub/updates/check")
        self.assertTrue(self.wait(lambda: len(self.get("/hub.json")["updates"]["available"]) == 4))
        n = len(self.srv.requests)
        # off: nothing offered, Check now refused, and nothing sent
        u = self.post("/hub/updates", b"on=0")
        self.assertFalse(u["enabled"])
        self.assertEqual(self.get("/hub.json")["updates"]["available"], [])
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/hub/updates/check")
        self.assertEqual(e.exception.code, 409)
        with open(os.path.join(self.dir, "hub.json"), encoding="utf-8") as f:
            self.assertIs(json.load(f)["update_check"], False)
        time.sleep(0.5)
        self.assertEqual(len(self.srv.requests), n)
        # on again: it checks at once
        self.post("/hub/updates", b"on=1")
        self.assertTrue(self.wait(lambda: len(self.srv.requests) == n + 1))
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/hub/updates", b"on=0", page=False)
        self.assertEqual(e.exception.code, 403)

    def test_install_through_the_hub(self):
        self.start(update_feed=self.srv.url)
        self.post("/hub/updates/check")
        self.assertTrue(self.wait(lambda: self.get("/hub.json")["updates"]["available"]))
        d = self.get("/hub.json")
        self.assertTrue(d["local"])
        self.assertIs(d["updates"]["available"][0]["in_use"], False)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/hub/updates/install", b"key=hub", page=False)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as e:     # FEED has no SHA-256 for its installers
            self.post("/hub/updates/install", b"key=optec")
        self.assertEqual(e.exception.code, 409)
        self.assertIn("SHA-256", json.loads(e.exception.read())["error"])
        e.exception.close()

    def test_simulated_hub_without_an_address_doesnt_check(self):
        h = self.start()
        u = self.get("/hub.json")["updates"]
        self.assertTrue(u["on"])
        self.assertFalse(u["enabled"])
        self.assertEqual(h.updates.url, os.environ.get("ALPACA_HUB_UPDATE_FEED", U.FEED_URL))


if __name__ == "__main__":
    unittest.main()
