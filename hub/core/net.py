"""HTTP servers, this PC's addresses and Alpaca discovery, shared by the hub and every driver's server.

Taken from wbstock/alpaca.py and ncalpaca/alpaca.py (2026-11-22), which had the same code.
"""
import errno
import logging
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer

log = logging.getLogger("hub.net")

DISCOVERY_PORT = 32227


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR (Python's default for HTTPServer) lets a second process bind the same port
    silently. Use SO_EXCLUSIVEADDRUSE there instead, so a second copy of the service fails to start."""
    allow_reuse_address = os.name != "nt"
    # Connections waiting to be accepted. Python's default (5) overflows when several clients (NINA, the Overview
    # page, a phone) connect at once, and Windows then refuses the connection; the client retries 0.5 s later,
    # so a quick read takes half a second (seen in tools/soak.py runs, 2026-11-23).
    request_queue_size = 128

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DualStackHTTPServer(ExclusiveHTTPServer):
    """Listens on IPv6 and IPv4 at once. Without it, a client that asks for "localhost" tries ::1 first and
    waits about 2 s for that to fail on every request before it falls back to 127.0.0.1."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_http_server(bind, port, handler):
    """The default bind (every network) listens dual-stack where the PC has IPv6, IPv4 only otherwise."""
    if bind in ("0.0.0.0", "") and socket.has_ipv6:
        try:
            return DualStackHTTPServer(("::", port), handler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) == 10048:
                raise   # a second copy of the service: fail as before, don't fall back to IPv4
            log.info("no IPv6 here (%s), listening on IPv4 only", e)
    return ExclusiveHTTPServer((bind, port), handler)


def plain_ip(ip):
    """'::ffff:192.168.1.5' (IPv4 seen through a dual-stack socket) -> '192.168.1.5'."""
    return ip[7:] if ip.startswith("::ffff:") else ip


def route_address():
    """The address of the default route (None without a network)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent, it only picks the route
            a = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    return None if a.startswith("127.") or a == "0.0.0.0" else a


def own_addresses():
    """This PC's IPv4 addresses (without loopback)."""
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except OSError:
        pass
    a = route_address()  # which getaddrinfo sometimes leaves out
    if a:
        out.add(a)
    return {a for a in out if not a.startswith("127.") and a != "0.0.0.0"}


class DiscoveryDedupe:
    """Answer an Alpaca client on this PC once, not once per network interface.

    Discovery clients (the ASCOM library that NINA uses) broadcast on every interface and also ask 127.0.0.1.
    A client on this same PC then hears the service twice, as 127.0.0.1 and as the LAN address, and lists the
    device twice. A request from one of this PC's own LAN addresses is held briefly, and dropped if the same
    client asks on loopback too. The loopback entry is the one kept: it works without a network and survives a
    new DHCP address. A local client that never asks on loopback still gets the LAN answer.
    """
    HOLD_S = 0.3       # how long a request from our own LAN address waits for a loopback request
    WINDOW_S = 2.0     # requests this close together belong to the same discovery round
    ADDR_TTL_S = 30.0  # how long the list of own addresses is reused

    def __init__(self, clock=time.monotonic, addresses=own_addresses):
        self._clock = clock
        self._addresses = addresses
        self._own = set()
        self._own_at = None
        self._last_loopback = None
        self._lock = threading.Lock()

    def _own_now(self):
        now = self._clock()
        if self._own_at is None or now - self._own_at > self.ADDR_TTL_S:
            self._own, self._own_at = set(self._addresses()), now
        return self._own

    def classify(self, ip):
        """'now' to answer at once, 'later' to answer after HOLD_S if still_needed()."""
        with self._lock:
            if ip.startswith("127."):
                self._last_loopback = self._clock()
                return "now"
            return "later" if ip in self._own_now() else "now"

    def still_needed(self):
        with self._lock:
            return self._last_loopback is None or self._clock() - self._last_loopback > self.WINDOW_S
