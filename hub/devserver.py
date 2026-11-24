"""A small Alpaca server for drivers written for the hub (rather than wrapping a standalone service).

A driver gives it AlpacaDevice objects (one per Alpaca device it serves) and an App (its status page). The server
answers the common ASCOM members, the management API and the page endpoints; the hub forwards to it.
"""
import io
import json
import logging
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler

from .core.net import make_http_server as _make_http_server
from .core.net import plain_ip as _plain_ip
from .core.page import Page

log = logging.getLogger("hub.devserver")

# ASCOM error numbers
NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
VALUE_NOT_SET = 0x402
NOT_CONNECTED = 0x407
INVALID_OPERATION = 0x40B
ACTION_NOT_IMPLEMENTED = 0x40C
DRIVER_ERROR = 0x500


# The Alpaca API's parameter names. In a PUT body they are case sensitive: one of these sent with other casing counts
# as missing (ConformU's protocol check sends e.g. "position" and expects 400). Query (GET) names are not.
ALPACA_PARAMS = (
    "ClientID ClientTransactionID Connected Action Parameters Command Raw Position TempComp Reverse Brightness "
    "Id State Value Name AveragePeriod SensorName Azimuth Altitude Slaved Axis Rate Duration Direction "
    "DeclinationRate RightAscensionRate DoesRefraction GuideRateDeclination GuideRateRightAscension SideOfPier "
    "SiteElevation SiteLatitude SiteLongitude SlewSettleTime TargetDeclination TargetRightAscension Tracking "
    "TrackingRate UTCDate RightAscension Declination BinX BinY CCDTemperature CoolerOn FastReadout Gain NumX NumY "
    "Offset ReadoutMode StartX StartY SubExposureDuration Light").split()
_CANON = {n.lower(): n for n in ALPACA_PARAMS}


def put_params(body):
    """A PUT body -> {lower-case name: value}, leaving out a standard Alpaca name sent with the wrong casing."""
    out = {}
    for k, v in urllib.parse.parse_qsl(body, keep_blank_values=True):
        c = _CANON.get(k.lower())
        if c is not None and k != c:
            continue
        out[k.lower()] = v
    return out


class AlpacaError(Exception):
    def __init__(self, number, message):
        super().__init__(message)
        self.number, self.message = number, message


class BadRequest(Exception):
    pass


class AlpacaDevice:
    """One Alpaca device. Subclasses set the class attributes and implement get/put for their own members."""
    type = "device"            # lower case Alpaca type: "covercalibrator"
    type_name = "Device"       # "CoverCalibrator"
    number = 0
    interface_version = 1
    driver_info = ""
    driver_version = ""

    actions = ()               # SupportedActions

    def __init__(self, uid, name, description):
        self.uid, self.name, self.description = uid, name, description
        self.connected = False

    def present(self):
        """False hides the device (management list and API answer as if it didn't exist), e.g. an optional motor."""
        return True

    def set_connected(self, on):
        """Connected = on. Raise AlpacaError to refuse (e.g. the device doesn't answer)."""
        self.connected = on

    def action(self, name, parameters):
        """Action(name, parameters) -> str."""
        raise AlpacaError(ACTION_NOT_IMPLEMENTED, f"Unknown action {name!r}")

    def get(self, member, param):
        """Value of a GET member (lower case). Raise KeyError for an unknown member."""
        raise KeyError(member)

    def put(self, member, param):
        """Carry out a PUT member; return a value or None. Raise KeyError for an unknown member."""
        raise KeyError(member)


STEP_S = 10            # history.json?res=m: averages over this many seconds (1 minute before 2026-11-24)
KEEP_S = 24 * 3600     # how long they are kept


class History:
    """1 s samples for 10 minutes and 10-second averages for 24 hours, in memory. history.json?res=m gives the
    averages (named when they were 1-minute ones; the reply's "step" says how long they are, each dated at its
    start), and after the keys each one's lowest and highest 1 s sample in the step (<key>_min, then <key>_max), so
    a spike the average hides still shows. The hub's day files make 1-minute rows of them (hub/recorder.py)."""

    def __init__(self, keys, step=STEP_S):
        self.keys = list(keys)
        self.step = step
        self._lock = threading.Lock()
        self.fine = deque(maxlen=600)
        self.coarse = deque(maxlen=KEEP_S // step)
        self._minute = None    # the step being averaged (named when it was a minute)
        self._acc = []
        self._last_t = 0

    def add(self, t, values):
        t = int(t)
        with self._lock:
            if t == self._last_t:
                return
            self._last_t = t
            row = [values.get(k) for k in self.keys]
            self.fine.append([t] + row)
            m = t // self.step
            if self._minute is not None and m != self._minute and self._acc:
                n = range(len(self.keys))
                self.coarse.append([self._minute * self.step] + [self._avg(i) for i in n]
                                   + [self._ext(i, min) for i in n] + [self._ext(i, max) for i in n])
                self._acc = []
            self._minute = m
            self._acc.append(row)

    def _avg(self, i):
        vals = [r[i] for r in self._acc if r[i] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _ext(self, i, fn):
        vals = [r[i] for r in self._acc if r[i] is not None]
        return round(fn(vals), 2) if vals else None

    def get(self, res, since=None):
        with self._lock:
            rows = list(self.fine if res == "s" else self.coarse)
        if since is not None:
            rows = [r for r in rows if r[0] > since]
        if res == "s":
            return {"keys": self.keys, "rows": rows, "step": 1}
        return {"keys": self.keys + [k + "_min" for k in self.keys] + [k + "_max" for k in self.keys], "rows": rows,
                "step": self.step}


class App:
    """The device's status page side. Subclasses implement status() and control()."""
    ui_dir = None              # folder with body.html, device.js (the kit is the hub's: hub/ui/kit)
    history = None             # History, or None for no chart

    def status(self):
        return {}

    def control(self, form):
        """A page control request (already allowed: local or unlocked). Returns (http code, message)."""
        return 404, "Unknown control"

    def route(self, req):
        """Any other request to the device's page (its own endpoints): None if it isn't one, else
        (http code, body str or bytes, content type[, {extra header: value}]). req has command, path, params
        (lower-case keys), pairs (every (name, value) of the body or query, as sent), headers, local (the caller is
        on this PC) and client (an Alpaca client is connected)."""
        return None


class DeviceServer:
    def __init__(self, devices, app, version, public_urls=None, simulated=False, proxied=True, is_local=None):
        self.devices = {(d.type, d.number): d for d in devices}
        self.app = app
        self.version = version
        self.public_urls = public_urls or (lambda: [])
        self.simulated = simulated
        self.proxied = proxied
        self.is_local = is_local or (lambda ip: _plain_ip(ip).startswith("127.") or _plain_ip(ip) == "::1")
        self.page = Page(app.ui_dir) if app.ui_dir else None
        self.started = time.time()
        self.httpd = None
        self.port = None
        self._txn = 0
        self._lock = threading.Lock()

    def client_connected(self):
        return any(d.connected for d in self.devices.values() if d.present())

    def next_txn(self):
        with self._lock:
            self._txn += 1
            return self._txn

    def start(self):
        server = self

        class Handler(_Handler):
            srv = server

        self.httpd = _make_http_server("127.0.0.1", 0, Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="devserver", daemon=True).start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class _Request:
    """What App.route sees of a request."""

    def __init__(self, command, path, params, headers, local, client, pairs=()):
        self.command, self.path, self.params, self.headers = command, path, params, headers
        self.local, self.client, self.pairs = local, client, list(pairs)


class _Handler(BaseHTTPRequestHandler):
    srv: DeviceServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    @property
    def peer(self):
        ip = self.client_address[0]
        if self.srv.proxied and _plain_ip(ip).startswith("127."):
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd
        return ip

    def local(self):
        return self.srv.is_local(self.peer)

    # ---------------------------------------------------------------- plumbing
    def _bytes(self, data, ctype, code=200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _plain(self, code, text):
        self._bytes(text.encode("utf-8"), "text/plain; charset=utf-8", code)

    def _json(self, obj, code=200):
        self._bytes(json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json", code)

    def _params(self):
        u = urllib.parse.urlsplit(self.path)
        p = {k.lower(): v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        self._body = ""
        if self.command in ("PUT", "POST"):
            n = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            p.update({k.lower(): v[-1] for k, v in urllib.parse.parse_qs(self._body, keep_blank_values=True).items()})
        return u.path, p

    def _alpaca(self, p, value=None, err=0, msg=""):
        try:
            ctid = int(p.get("clienttransactionid", "0"))
        except ValueError:
            ctid = 0
        d = {"ClientTransactionID": ctid, "ServerTransactionID": self.srv.next_txn(), "ErrorNumber": err,
             "ErrorMessage": msg}
        if value is not None:
            d["Value"] = value
        self._json(d)

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_POST(self):
        self._route()

    # ---------------------------------------------------------------- routes
    def _route(self):
        try:
            path, p = self._params()
            parts = [x for x in path.split("/") if x]
            if parts[:1] == ["management"]:
                return self._management(path, p)
            if len(parts) == 5 and parts[:2] == ["api", "v1"]:
                return self._device(parts, p)
            if parts[:1] == ["setup"] and len(parts) >= 2 and parts[1] == "v1":
                return self._bytes(b"<p>Settings are on the status page.</p>", "text/html", 200)
            if path in ("/", "/index.html") and self.srv.page:
                html, gz = self.srv.page.get()
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    return self._bytes(gz, "text/html; charset=utf-8", extra=(("Content-Encoding", "gzip"),))
                return self._bytes(html, "text/html; charset=utf-8")
            if path == "/status.json":
                d = self.srv.app.status()
                d.update({"build": self.srv.version, "uptime": round(time.time() - self.srv.started),
                          "simulated": self.srv.simulated, "client": self.srv.client_connected(),
                          "local": self.local(), "urls": self.srv.public_urls()})
                return self._json(d)
            if path == "/history.json" and self.srv.app.history:
                try:
                    since = float(p["since"]) if p.get("since") else None
                except ValueError:
                    return self._plain(400, "since: unix seconds")
                return self._json(self.srv.app.history.get(p.get("res") or "m", since))
            if path == "/qr.svg":
                return self._qr(p)
            if path == "/control" and self.command == "POST":
                if self.headers.get("X-Hub-Page") != "1":
                    return self._plain(403, "Page requests only")
                if self.srv.client_connected() and p.get("override") != "1":
                    return self._plain(409, "An Alpaca client is connected: unlock the controls first")
                code, msg = self.srv.app.control(p)
                return self._plain(code, msg)
            pairs = urllib.parse.parse_qsl(self._body if self.command in ("PUT", "POST")
                                           else urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            r = self.srv.app.route(_Request(self.command, path, p, self.headers, self.local(),
                                            self.srv.client_connected(), pairs))
            if r is not None:
                code, body, ctype = r[:3]
                extra = tuple(r[3].items()) if len(r) > 3 and r[3] else ()
                return self._bytes(body if isinstance(body, bytes) else body.encode("utf-8"), ctype, code, extra)
            return self._plain(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("request failed: %s %s", self.command, self.path)
            try:
                self._plain(500, "Internal error")
            except Exception:
                pass

    def _management(self, path, p):
        if path == "/management/apiversions":
            return self._alpaca(p, [1])
        if path == "/management/v1/description":
            return self._alpaca(p, {"ServerName": "alpaca-hub device", "Manufacturer": "alpaca-hub",
                                    "ManufacturerVersion": self.srv.version, "Location": ""})
        if path == "/management/v1/configureddevices":
            return self._alpaca(p, [{"DeviceName": d.name, "DeviceType": d.type_name, "DeviceNumber": d.number,
                                     "UniqueID": d.uid} for d in self.srv.devices.values() if d.present()])
        return self._plain(404, "Not found")

    def _device(self, parts, p):
        if parts[2] != parts[2].lower():
            return self._plain(400, f"Device types are lower case in Alpaca URLs: {parts[2]!r}")
        try:
            num = int(parts[3])
        except ValueError:
            return self._plain(400, "Invalid device number")
        if self.command == "PUT":
            p = put_params(self._body)      # PUT names are case sensitive (the page's POSTs aren't Alpaca)
        dev = self.srv.devices.get((parts[2].lower(), num))
        if dev is None or not dev.present():
            return self._plain(404, "No such device")
        for k, name in (("clientid", "ClientID"), ("clienttransactionid", "ClientTransactionID")):
            v = p.get(k)
            if v is not None and not (v.isdigit() and int(v) <= 0xFFFFFFFF):
                return self._plain(400, f"Invalid {name}")      # Alpaca: unsigned 32-bit
        m, put = parts[4].lower(), self.command == "PUT"
        try:
            if m == "connected":
                if put:
                    v = (p.get("connected") or "").strip().lower()
                    if v not in ("true", "false"):
                        raise BadRequest("Connected must be true or false")
                    dev.set_connected(v == "true")
                    return self._alpaca(p)
                return self._alpaca(p, dev.connected)
            if m in ("connect", "disconnect") and put:
                dev.set_connected(m == "connect")
                return self._alpaca(p)
            if not put:
                common = {"connecting": False, "name": dev.name, "description": dev.description,
                          "driverinfo": dev.driver_info, "driverversion": dev.driver_version,
                          "interfaceversion": dev.interface_version, "supportedactions": list(dev.actions)}
                if m in common:
                    return self._alpaca(p, common[m])
            if put and m == "action":
                if p.get("action") is None:
                    raise BadRequest("Missing Action")
                if not dev.connected:
                    raise AlpacaError(NOT_CONNECTED, "Not connected")
                return self._alpaca(p, dev.action(p.get("action"), p.get("parameters")))
            if put and m in ("commandblind", "commandbool", "commandstring"):
                raise AlpacaError(NOT_IMPLEMENTED, "Raw commands are not supported")
            value = dev.put(m, p) if put else dev.get(m, p)
            return self._alpaca(p, value)
        except KeyError:
            return self._plain(400, f"Unknown {parts[2]} member {parts[4]!r}")
        except BadRequest as e:
            return self._plain(400, str(e))
        except AlpacaError as e:
            return self._alpaca(p, err=e.number, msg=e.message)

    def _qr(self, p):
        urls = self.srv.public_urls()
        try:
            url = urls[int(p.get("i") or 0)]
        except (ValueError, IndexError):
            return self._plain(404, "No such address")
        try:
            import segno
        except ImportError:
            return self._plain(501, "QR codes need the segno package")
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=5, border=3, dark="#000", light="#fff")
        self._bytes(buf.getvalue(), "image/svg+xml")
