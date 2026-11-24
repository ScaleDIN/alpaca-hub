"""The port relay (hub/relay.py): RelaySerial behaves like the ports the links had, the hub keeps the real port open,
caps writes and enforces never_probe; and every driver's link runs on its simulator through it."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub import host as H  # noqa: E402
from hub import relay as R  # noqa: E402
from hub.api import Driver  # noqa: E402
from hub.drivers import all_drivers  # noqa: E402
from hub.ports import PortInfo  # noqa: E402


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class FakePort:
    """A pyserial-like port (in_waiting is a property, as on PolledSerial). The test feeds what the device says."""

    def __init__(self):
        self.rx = bytearray()
        self.tx = bytearray()
        self.lock = threading.Lock()
        self.closed = False
        self.flushes = self.input_resets = self.output_resets = 0
        self.dtr = self.rts = False
        self.baudrate = 9600
        self.fail = None

    def arrive(self, data):
        with self.lock:
            self.rx += data

    @property
    def in_waiting(self):
        if self.fail:
            raise OSError(self.fail)
        with self.lock:
            return len(self.rx)

    def read(self, n=1):
        with self.lock:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def write(self, data):
        with self.lock:
            self.tx += data
        return len(data)

    def flush(self):
        self.flushes += 1

    def reset_input_buffer(self):
        self.input_resets += 1
        with self.lock:
            self.rx.clear()

    def reset_output_buffer(self):
        self.output_resets += 1

    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()
        self.opens = [self.port]
        self.relays = []

    def tearDown(self):
        for r in self.relays:
            r.close()

    def relay(self, **kw):
        def reopen():
            p = FakePort()
            self.opens.append(p)
            return p
        r = R.PortRelay(self.port, "COM9", 9600, reopen=reopen, **kw)
        self.relays.append(r)
        return r

    def end(self, relay, **kw):
        return R.RelaySerial(R.LocalLink(relay), "COM9", relay.baud, **kw)

    def test_reads_like_a_port_with_a_timeout(self):
        s = self.end(self.relay(), timeout=0.2)
        t = time.monotonic()
        self.assertEqual(s.read(4), b"")
        self.assertGreaterEqual(time.monotonic() - t, 0.15, "waits for the timeout")
        self.port.arrive(b"ab")
        threading.Timer(0.05, self.port.arrive, (b"c\r\nnext",)).start()
        self.assertEqual(s.readline(), b"abc\r\n", "a line across chunks")
        self.assertTrue(wait_for(lambda: s.in_waiting == 4))
        self.assertEqual(s.read(2), b"ne")
        self.assertEqual(s.read_until(b"x", size=5), b"x")
        self.assertEqual(s.read(1), b"t")
        s.timeout = 0.05
        self.assertEqual(s.readline(), b"", "nothing more: empty after the timeout")

    def test_writes_flush_and_line_settings_reach_the_port(self):
        s = self.end(self.relay(), lines=True)
        self.assertTrue(s.dtr and s.rts, "the level the hub opened the port with")
        self.assertEqual(s.write(b"PF#"), 3)
        s.flush()
        s.dtr = False
        s.rts = False
        s.baudrate = 19200
        s.reset_output_buffer()
        self.assertEqual(bytes(self.port.tx), b"PF#")
        self.assertEqual((self.port.flushes, self.port.output_resets), (1, 1))
        self.assertEqual((self.port.dtr, self.port.rts, self.port.baudrate), (False, False, 19200))
        self.assertEqual(s.baudrate, 19200)

    def test_input_clearing_drops_what_came_before(self):
        r = self.relay()
        s = self.end(r)
        self.port.arrive(b"stale\n")
        self.assertTrue(wait_for(lambda: s.in_waiting == 6))
        s.reset_input_buffer()
        self.assertEqual(s.in_waiting, 0)
        self.assertEqual(self.port.input_resets, 1)
        self.port.arrive(b"fresh\n")
        self.assertEqual(s.readline(), b"fresh\n")

    def test_close_and_reopen_keep_the_real_port_open(self):
        r = self.relay()
        self.port.arrive(b"heard while looking\n")
        time.sleep(0.05)
        s = self.end(r)
        self.assertEqual(s.readline(), b"heard while looking\n", "the first end gets what detection heard")
        s.close()
        self.assertFalse(s.is_open)
        with self.assertRaises(OSError):
            s.write(b"x")
        self.assertFalse(self.port.closed, "a link's close never closes the hub's port")
        self.port.arrive(b"while nobody listened\n")
        time.sleep(0.05)
        s2 = self.end(r)
        self.assertEqual(s2.readline(), b"", "a reopen starts empty, as a real reopen does")
        s2.write(b"again")
        self.assertEqual(bytes(self.port.tx), b"again")
        self.assertEqual(len(self.opens), 1, "never opened again")

    def test_a_second_end_detaches_the_first(self):
        r = self.relay()
        s1 = self.end(r)
        s2 = self.end(r)
        with self.assertRaises(OSError):
            s1.write(b"x")
        s2.write(b"y")
        self.assertEqual(bytes(self.port.tx), b"y")

    def test_a_failed_port_is_opened_again_on_the_next_attach(self):
        r = self.relay()
        s = self.end(r)
        self.port.fail = "device gone"
        self.assertTrue(wait_for(lambda: self.port.closed), "the hub closes a port that failed")
        with self.assertRaises(OSError):
            s.readline()
        s.close()
        s = self.end(r)                                   # what the link's reopen does
        self.assertEqual(len(self.opens), 2)
        self.opens[1].arrive(b"back\n")
        self.assertEqual(s.readline(), b"back\n")

    def test_writes_are_capped(self):
        r = self.relay()
        s = self.end(r)
        for _ in range(20):
            s.write(b"x" * 10000)
        self.assertLessEqual(len(self.port.tx), r._rate() + R.WRITE_MAX)
        self.assertGreater(r.dropped, 150000)
        s.write(b"!")                                     # still usable afterwards, at the line's pace
        time.sleep(0.1)
        s.write(b"ok")
        self.assertTrue(bytes(self.port.tx).endswith(b"ok"))

    def test_refused_writes_never_reach_the_device(self):
        told = []
        r = self.relay(refuse=lambda dev, data: told.append((dev, data)))
        s = self.end(r)
        s.write(b"1#")
        s.write(b"2#")
        self.assertEqual(bytes(self.port.tx), b"")
        self.assertEqual(told, [("COM9", b"1#"), ("COM9", b"2#")])
        self.assertEqual(r.refused, 2)

    def test_released_relay_closes_the_port_and_fails_the_end(self):
        r = self.relay()
        s = self.end(r)
        r.close()
        self.assertTrue(self.port.closed)
        with self.assertRaises(OSError):
            s.write(b"x")
        with self.assertRaises(OSError):
            self.end(r)


# ------------------------------------------------------------------------------------------------ through the hub
class SimBench:
    """Every driver's simulators on fake ports, as --simulate makes them. Ports can be made to fail once."""

    def __init__(self, names):
        self.sims, self.ports, self.opened, self.handles = {}, {}, [], {}
        drivers = {n: d for d in all_drivers() for n in d.simulators}
        for i, name in enumerate(names):
            sim, usb, serial, desc = drivers[name].simulators[name](0)
            dev = f"COM{40 + i}"
            self.sims[dev] = sim
            self.ports[dev] = PortInfo(dev, *usb, serial, desc)

    def lister(self):
        return list(self.ports.values())

    def opener(self, device, baud, lines=False):
        self.opened.append(device)
        sim = self.sims[device]
        h = sim.opener(device, baud) if _takes_baud(sim) else sim.opener(device)
        self.handles[device] = h
        return h

    def fail(self, device):
        """The port's next read raises, as a port does when its USB device resets."""
        h = self.handles[device]

        def boom(*a, **k):
            raise OSError("the device reset")
        h.read = boom
        h.readline = boom


def _takes_baud(sim):
    import inspect
    try:
        return len(inspect.signature(sim.opener).parameters) >= 2
    except (TypeError, ValueError):
        return False


class LinksThroughTheRelayTests(unittest.TestCase):
    SIMS = ["nitecrawler", "box", "panel", "lynx", "gemini"]

    def setUp(self):
        self.bench = SimBench(self.SIMS)
        self.hub = H.Hub(all_drivers(), config_dir=tempfile.mkdtemp(), bind="127.0.0.1", http_port=0,
                         port_lister=self.bench.lister, opener=self.bench.opener, simulated=True,
                         use_standalone_settings=False, listen_s=1.3)
        self.hub.start(scan=False)

    def tearDown(self):
        self.hub.stop()

    def test_every_driver_runs_reopens_and_keeps_its_line_level_through_the_relay(self):
        h = self.hub
        h.scan_once()
        detection = list(self.bench.opened)     # FTDI ports are opened once per probe baud while looking
        insts = {i.port.device: i for i in h.instances.values()}
        self.assertEqual(len(insts), len(self.SIMS), sorted(i.slug for i in h.instances.values()))
        for dev, inst in insts.items():
            self.assertTrue(wait_for(inst.up, 10), f"{inst.slug} came up through the relay")
            self.assertIsInstance(h._relays[dev], R.PortRelay)
        self.assertEqual(self.bench.opened, detection, "the links run on the ports detection opened")
        for dev, inst in insts.items():
            lines = h._lines(inst.port)
            for end_ser in _ends(inst):
                self.assertIsInstance(end_ser, R.RelaySerial, inst.slug)
                self.assertEqual((end_ser.dtr, end_ser.rts), (lines, lines), inst.slug)
        # Each port fails once (a USB reset): the hub closes it, the link reopens through the relay, and the
        # device comes back on the same tab.
        for dev in insts:
            self.bench.fail(dev)
        self.assertTrue(wait_for(lambda: all(self.bench.opened.count(d) == detection.count(d) + 1 for d in insts), 20),
                        f"each port opened again once: {sorted(self.bench.opened)}")
        for dev, inst in insts.items():
            self.assertTrue(wait_for(lambda: inst.up() and h._relays[dev]._error is None, 20),
                            f"{inst.slug} is back after its port failed")
            self.assertIs(h.instances[inst.slug], inst)
        self.assertEqual(self.bench.sims[_dev(insts, "box")]._rx if hasattr(self.bench.sims[_dev(insts, "box")], "_rx")
                         else b"", b"", "nothing was sent to the box")


def _dev(insts, slug):
    return next(d for d, i in insts.items() if i.slug == slug)


def _ends(inst):
    """The RelaySerial each instance's link holds right now."""
    out = []
    for obj in (getattr(inst, "svc", None), getattr(inst, "link", None), getattr(inst, "unit", None)):
        for holder in (obj, getattr(obj, "link", None)):
            ser = getattr(holder, "_ser", None) if holder is not None else None
            if ser is not None:
                out.append(ser)
    return out


class NeverProbeOnTheBytesTests(unittest.TestCase):
    def test_a_probe_that_writes_to_a_never_probe_port_is_dropped_and_refused(self):
        class Listener(Driver):
            key = "listener"
            usb_ids = frozenset({(0x1A86, 0x7523)})
            listen_baud = 19200
            never_probe = frozenset({(0x1A86, 0x7523)})

        class Prober(Driver):
            key = "prober"
            usb_ids = frozenset({(0x0403, 0x6015)})
            probe_baud = 9600

            def probe(self, ser):
                ser.write(b"HELLO#")
                return {"x": 1}

        port = FakePort()
        hub = H.Hub([Listener(), Prober()], config_dir=tempfile.mkdtemp(), bind="127.0.0.1", http_port=0,
                    port_lister=lambda: [PortInfo("COM5", 0x1A86, 0x7523, "", "CH340")],
                    opener=lambda dev, baud, lines=False: port, listen_s=0.1)
        # _accept already refuses a driver that would probe never_probe IDs; this one only gains them later
        # (as a driver process could claim anything): the relay is what stops its bytes.
        prober = next(d for d in hub.drivers if d.key == "prober")
        prober.usb_ids = frozenset({(0x0403, 0x6015), (0x1A86, 0x7523)})
        self.assertIsNone(hub.identify(hub.port_lister()[0]))
        self.assertEqual(bytes(port.tx), b"", "not one byte reached the device")
        self.assertIn("prober", hub.refused)
        self.assertNotIn(prober, hub.drivers)
        self.assertEqual(hub._relays, {})


if __name__ == "__main__":
    unittest.main()
