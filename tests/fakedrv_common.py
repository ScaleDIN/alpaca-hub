"""A fake driver for the supervisor tests (tests/test_supervisor.py): a 'thermometer' that says "THERMO <serial> <t>"
on its own, found by listening. Its instance reads the port through the relay and serves a tiny Alpaca-like HTTP
server. GET /do?what=... makes it misbehave on purpose: crash, exit, hang, flood, bad (a message the hub must
refuse), big (one over the size limit)."""
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hub.api import Driver, Instance, unit_of

HANG = threading.Event()


def make(key, usb):
    class FakeDriver(Driver):
        pass
    FakeDriver.key = key
    FakeDriver.title = f"Fake {key}"
    FakeDriver.usb_ids = frozenset({usb})
    FakeDriver.listen_baud = 9600

    def recognise(self, data):
        for line in data.decode("ascii", "replace").splitlines():
            if line.startswith("THERMO "):
                return {"serial": line.split()[1]}
        return None

    def start(self, ctx, port, ident):
        return FakeInstance(self, ctx, port, ident)
    FakeDriver.recognise = recognise
    FakeDriver.start = start
    return FakeDriver


class FakeInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        n = ctx.unit_number()
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        super().__init__(driver, port, ident, slug, f"{driver.title} #{n + 1}")
        self.uid = ctx.uid(unit_of(ident, port), "switch")
        self.temp = None
        self.lines = 0
        self.opener = ctx.opener_for(port, 9600)
        self.ser = self.opener(port.device)
        self._stop = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        inst = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                inst._http(self)

            do_PUT = do_GET
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.local_port = self.httpd.server_address[1]

    def _read(self):
        while not self._stop.is_set():
            try:
                line = self.ser.readline()
            except Exception:
                time.sleep(0.2)
                try:
                    self.ser.close()
                    self.ser = self.opener(self.port.device)     # a link's reopen: through the hub again
                except Exception:
                    pass
                continue
            if line.startswith(b"THERMO "):
                self.temp = float(line.split()[2])
                self.lines += 1

    def _http(self, h):
        u = urllib.parse.urlsplit(h.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        n = int(h.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(h.rfile.read(n).decode())) if n else {}
        if u.path.endswith("/connected"):
            if h.command == "PUT":
                self.connected = form.get("Connected", "").lower() == "true"
            body = {"Value": bool(getattr(self, "connected", False)), "ErrorNumber": 0, "ErrorMessage": "",
                    "ClientTransactionID": 0, "ServerTransactionID": 0}
        elif u.path == "/do":
            self._do(q.get("what", ""))
            body = {"ok": True}
        elif u.path.startswith("/management/v1/configureddevices"):
            body = {"Value": [{"DeviceName": self.title, "DeviceType": "Switch", "DeviceNumber": 0,
                               "UniqueID": self.uid}], "ErrorNumber": 0, "ErrorMessage": ""}
        elif u.path == "/status.json":
            body = {"temp": self.temp, "lines": self.lines, "pid": os.getpid()}
        else:
            body = {"Value": True, "ErrorNumber": 0, "ErrorMessage": "", "ClientTransactionID": 0,
                    "ServerTransactionID": 0}
        data = json.dumps(body).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _do(self, what):
        host = _host()
        if what == "crash":
            threading.Timer(0.1, os._exit, (3,)).start()
        elif what == "exit":
            threading.Timer(0.1, os._exit, (0,)).start()
        elif what == "hang":
            HANG.set()
        elif what == "flood":
            def flood():
                chunk = b"x" * 60000
                while not self._stop.is_set():
                    try:
                        self.ser.write(chunk)
                    except Exception:
                        time.sleep(0.01)
            threading.Thread(target=flood, daemon=True).start()
        elif what == "bad":
            threading.Timer(0.1, host.channel._conn.send_bytes, (b"\xffthis is not a message",)).start()
        elif what == "badrelay":
            threading.Timer(0.1, host.channel.send, (["r", 1, ["set", "parity", "E"]],)).start()
        elif what == "big":
            threading.Timer(0.1, host.channel._conn.send_bytes, (b'["x","' + b"y" * (2 << 20) + b'"]',)).start()

    def up(self):
        return self.temp is not None

    def summary(self):
        if HANG.is_set():
            time.sleep(3600)       # every ping from now on waits here: the hub must end this process
        return {"text": f"{self.temp} °C", "state": "ok"}

    def stop(self):
        self._stop.set()
        self.httpd.shutdown()
        try:
            self.ser.close()
        except Exception:
            pass


def _host():
    import gc
    from hub.driverhost import DriverHost
    return next(o for o in gc.get_objects() if isinstance(o, DriverHost))
