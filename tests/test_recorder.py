"""hub/recorder.py: the day files the hub writes for every device's history."""
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub import recorder as R  # noqa: E402
from hub.ports import PortInfo  # noqa: E402

# A driver's 10 s steps (hub/devserver.py History): temp, volt, then each one's lowest, then highest.
KEYS = ["temp", "volt", "temp_min", "volt_min", "temp_max", "volt_max"]
HEAD = ["time_local", "unix_s", "temp", "temp_min", "temp_max", "volt", "volt_min", "volt_max"]


def steps(t, temp, volt, n=6, spike=None):
    """n 10 s steps from t with these averages (lowest = highest = the average); spike: (i, volt) in step i's max."""
    out = []
    for i in range(n):
        tv = temp if not callable(temp) else temp(i)
        hi = spike[1] if spike and spike[0] == i else volt
        out.append([t + 10 * i, tv, volt, tv, volt, tv, hi])
    return out


T0 = int(time.mktime(time.strptime("2026-11-20 12:00", "%Y-%m-%d %H:%M")))   # local noon: minutes stay on that day
DAY = time.strftime("%Y-%m-%d", time.localtime(T0))


class FakeDevice:
    """A device's private server: /history.json?res=m answers with self.reply, rows after since."""

    def __init__(self):
        self.reply = {"keys": KEYS, "rows": [], "step": 10}
        self.box_days = 7                     # a box: its status.json history.days, set by POST /setup/history
        self.posts = []
        dev = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                from urllib.parse import parse_qs
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode())
                dev.posts.append((self.path, self.headers.get("X-WBStock"), form))
                if self.path == "/setup/history" and self.headers.get("X-WBStock") == "1":
                    dev.box_days = int(form["days"][0])
                    code = 200
                else:
                    code = 403
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                if self.path == "/status.json":
                    body = json.dumps({"history": {"days": dev.box_days}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                q = parse_qs(urlsplit(self.path).query)
                since = float(q["since"][0]) if "since" in q else None
                d = dict(dev.reply)
                d["rows"] = [r for r in dev.reply["rows"] if since is None or r[0] > since]
                body = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Driver:
    def __init__(self, key):
        self.key = key


class Inst:
    def __init__(self, key, port, serial="A1B2"):
        self.driver = Driver(key)
        self.port = PortInfo("COM7", 0x0403, 0x6001, serial, "test")
        self.ident = {}
        self.slug = key
        self.local_port = port


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dev = FakeDevice()
        self.days = 7
        self.insts = [Inst("nitecrawler", self.dev.port)]
        self.now = T0 + 3600
        self.rec = self.new()

    def tearDown(self):
        self.dev.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def new(self):
        return R.Recorder(self.dir, lambda: self.insts, days=lambda: self.days, clock=lambda: self.now,
                          set_days=self.set_days)

    def set_days(self, n):
        self.days = n

    def folder(self, key="nitecrawler", unit="A1B2"):
        return os.path.join(self.dir, "drivers", key, unit + "-history")

    def rows(self, name):
        with open(os.path.join(self.folder(), name), newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def test_writes_finished_minutes_once(self):
        # minute 0: temp 5.0/5.5 in turns, a 1 s spike to 13.4 V; minute 1: no temp (probe out); minute 2 still open
        self.dev.reply["rows"] = (steps(T0, lambda i: 5.0 if i % 2 else 5.5, 12.0, spike=(3, 13.4))
                                  + steps(T0 + 60, None, 12.1) + steps(T0 + 120, 5.5, 12.2, n=2))
        self.assertEqual(self.rec.record(self.insts[0]), 2)
        name = f"nitecrawler-history-{DAY}.csv"
        got = self.rows(name)
        self.assertEqual(got[0], HEAD)
        self.assertEqual(got[1][1:], [str(T0), "5.25", "5", "5.5", "12", "12", "13.4"])
        self.assertEqual(got[2][1:], [str(T0 + 60), "", "", "", "12.1", "12.1", "12.1"])   # missing stays empty
        self.assertEqual(got[1][0], time.strftime("%Y-%m-%d %H:%M", time.localtime(T0)))
        self.assertEqual(self.rec.record(self.insts[0]), 0)              # the open minute waits
        self.dev.reply["rows"] += steps(T0 + 140, 5.5, 12.2, n=4) + steps(T0 + 180, 5.6, 12.2, n=1)
        self.assertEqual(self.rec.record(self.insts[0]), 1)
        self.assertEqual(self.rows(name)[3][1:4], [str(T0 + 120), "5.5", "5.5"])
        # a restarted hub carries on after the last minute in the files; a device leaving writes its open minute
        self.assertEqual(self.new().record(self.insts[0], final=True), 1)
        self.assertEqual([r[1] for r in self.rows(name)[1:]], [str(T0 + 60 * i) for i in range(4)])

    def test_leaves_the_device_name_beside_its_files(self):
        self.insts[0].title = "NiteCrawler 3.5 NC #2"
        self.dev.reply["rows"] = steps(T0, 5.0, 12.0) + steps(T0 + 60, 5.0, 12.0, n=1)
        self.assertEqual(self.rec.record(self.insts[0]), 1)
        self.assertEqual(R.read_title(self.folder()), "NiteCrawler 3.5 NC #2")
        self.assertEqual(sorted(os.listdir(self.folder())), ["device.json", f"nitecrawler-history-{DAY}.csv"])
        self.assertIsNone(R.read_title(os.path.join(self.dir, "nowhere")))

    def test_older_drivers_one_minute_rows(self):
        # a driver from before 10 s steps: one row a minute, no min/max -> its average is also its min and max
        self.dev.reply = {"keys": ["temp", "volt"], "rows": [[T0, 5.25, 12.0], [T0 + 60, 5.3, 12.1]]}
        self.assertEqual(self.rec.record(self.insts[0], final=True), 2)
        self.assertEqual(self.rows(f"nitecrawler-history-{DAY}.csv")[1][1:], [str(T0), "5.25", "5.25", "5.25", "12", "12", "12"])

    def test_held_key_gets_the_minutes_last_reading(self):
        from hub.recorder import minute_rows
        keys = ["pos", "temp", "pos_min", "temp_min", "pos_max", "temp_max"]
        rows = [[T0, 5600, 5.0, 5600, 5.0, 5600, 5.0], [T0 + 10, 5800, 6.0, 5600, 6.0, 5800, 6.0],
                [T0 + 20, 5700, 7.0, 5700, 7.0, 5800, 7.0]]
        self.assertEqual(minute_rows(keys, rows, ("pos",)), (["pos", "temp"], [[T0, 5700, 5600, 5800, 6.0, 5.0, 7.0]]))
        self.assertEqual(minute_rows(keys, rows)[1][0][1], 5700.0)       # not held: the average (here the same)
        self.assertEqual(minute_rows(keys, rows[:2], ("pos",))[1][0][1], 5800)

    def test_box_and_drivers_with_their_own_files_are_left_out(self):
        self.dev.reply["rows"] = steps(T0, 1.0, 2.0)
        self.assertEqual(self.rec.record(Inst("wbox", self.dev.port), final=True), 0)
        self.dev.reply["files"] = True
        self.assertEqual(self.rec.record(self.insts[0], final=True), 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "drivers")))

    def test_one_days_setting_for_the_box_too(self):
        box = Inst("wbox", self.dev.port)
        # first sight: both take the larger, so nothing is deleted to match
        self.dev.box_days, self.days = 30, 7
        self.assertEqual(self.rec.sync_days(box), 30)
        self.assertEqual((self.dev.box_days, self.days), (30, 30))
        self.assertEqual(self.dev.posts, [])
        self.dev.box_days, self.days = 7, 14
        self.assertEqual(self.new().sync_days(box), 14)          # a new hub: the hub's larger value goes to the box
        self.assertEqual(self.dev.box_days, 14)
        self.assertEqual(self.dev.posts[-1][:2], ("/setup/history", "1"))
        # changed on the Overview: the box follows
        self.rec.sync_days(box)                                  # agreed on 14
        self.days = 3
        self.assertEqual(self.rec.sync_days(box), 3)
        self.assertEqual(self.dev.box_days, 3)
        # changed on the box's tab: the hub follows
        self.dev.box_days = 60
        self.assertEqual(self.rec.sync_days(box), 60)
        self.assertEqual(self.days, 60)
        n = len(self.dev.posts)
        self.assertEqual(self.rec.sync_days(box), 60)            # nothing changed: nothing sent
        self.assertEqual(len(self.dev.posts), n)
        # the minute's pass syncs the box and writes no files for it
        self.dev.reply["rows"] = [[T0, 1.0, 2.0]]
        self.days = 5
        self.insts = [box]
        self.rec.record_all()
        self.assertEqual(self.dev.box_days, 5)
        self.assertFalse(os.path.exists(self.folder("wbox", "A1B2")))

    def test_changed_columns_start_a_new_file(self):
        self.dev.reply["rows"] = steps(T0, 5.0, 12.0)
        self.rec.record(self.insts[0], final=True)
        self.dev.reply = {"keys": ["temp", "volt", "focus"], "rows": [[T0 + 60, 5.0, 12.0, 1000]]}
        self.assertEqual(self.rec.record(self.insts[0], final=True), 1)
        self.assertEqual(self.rows(f"nitecrawler-history-{DAY}-2.csv")[0][-3:], ["focus", "focus_min", "focus_max"])
        self.assertEqual(len(self.rows(f"nitecrawler-history-{DAY}.csv")), 2)

    def test_unit_folder_follows_the_device_not_the_port(self):
        self.dev.reply["rows"] = steps(T0, 5.0, 12.0)
        inst = Inst("wcover", self.dev.port, serial="")
        self.rec.record(inst, final=True)
        self.assertTrue(os.listdir(self.folder("wcover", "COM7")))       # no serial number: the COM port

    def test_purge_keeps_the_days_setting_and_others_files(self):
        old = time.strftime("%Y-%m-%d", time.localtime(T0 - 10 * 86400))
        f = self.folder()
        os.makedirs(f)
        box = self.folder("wbox", "COM5")
        os.makedirs(box)
        for folder, name in ((f, f"nitecrawler-history-{old}.csv"), (f, f"nitecrawler-history-{old}-2.csv"),
                             (f, f"nitecrawler-history-{DAY}.csv"), (f, "notes.csv"),
                             (box, f"wbstock-history-{old}.csv")):
            open(os.path.join(folder, name), "w").close()
        self.now = T0
        self.assertEqual(self.rec.purge(), 2)
        self.assertEqual(sorted(os.listdir(f)), sorted([f"nitecrawler-history-{DAY}.csv", "notes.csv"]))
        self.assertEqual(os.listdir(box), [f"wbstock-history-{old}.csv"])    # the box's own setting decides
        self.assertEqual(R.clamp_days("x"), 7)
        self.assertEqual(R.clamp_days(0), 1)
        self.assertEqual(R.clamp_days(9999), 365)


if __name__ == "__main__":
    unittest.main()
