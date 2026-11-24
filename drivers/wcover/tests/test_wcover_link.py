"""The panel's serial link (wcover.link), fed with lines as captured on the real panel (fw 20250703, 2026-11-19).
Moved with the link from tests/test_wbstock.py (REFACTOR.md step 7)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                   # the driver folder (its package)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))  # the repository (hub)

from wcover.link import (CalibratorState, CoverBusy, CoverDevice, CoverState, V4ecCoverLink,  # noqa: E402
                         parse_cover_status)


class ParseTest(unittest.TestCase):
    def test_parse_status(self):
        st = parse_cover_status("WandererCoverV4A20250404A20.00A160.00A160.00A12.30A0A0A0A")
        self.assertEqual(st["model"], "WandererCoverV4")
        self.assertEqual((st["close_set"], st["open_set"], st["position"]), (20.0, 160.0, 160.0))
        self.assertIsNone(parse_cover_status("ZXWBPlusV3Awhatever"))


class DeviceTest(unittest.TestCase):
    def test_brightness_is_checked_before_the_link(self):
        link = V4ecCoverLink("COM5")
        dev = CoverDevice(link)
        for bad in (-1, 256):
            with self.assertRaises(ValueError):
                dev.calibrator_on(bad)
        self.assertEqual(dev.max_brightness, 255)

    def test_nothing_heard_yet_is_unknown_then_an_error(self):
        # A driver restarted while the cover moves: the panel is silent until "done". Unknown meanwhile, not an
        # error; an error only once no move could still be running; normal as soon as a line comes.
        t = [100.0]
        link = V4ecCoverLink("COM5", clock=lambda: t[0])
        snap = link.snapshot()
        self.assertEqual((snap.cover, snap.calibrator), (CoverState.UNKNOWN, CalibratorState.UNKNOWN))
        t[0] += V4ecCoverLink.MOVE_TIMEOUT_S - 1
        self.assertEqual(link.snapshot().cover, CoverState.UNKNOWN)
        t[0] += 2
        self.assertEqual(link.snapshot().cover, CoverState.ERROR)
        link2 = V4ecCoverLink("COM5", clock=lambda: t[0])
        link2.feed_line(_v4ec_line(294.5))
        self.assertEqual(link2.snapshot().cover, CoverState.OPEN)

    def test_the_port_comes_from_the_hub(self):
        with self.assertRaises(OSError):
            V4ecCoverLink("COM5")._opener("COM5")


def _v4ec_line(position, brightness=0):
    """A status line in the exact form the real panel sent on 2026-11-19 (fw 20250703)."""
    return f"WandererCoverV4A20250703A15.00A295.00A{position:.2f}A12.04A{brightness}A50A0A\r\n"


class _FakeCoverPort:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    def flush(self):
        pass

    def close(self):
        pass


class V4ecCoverLinkTest(unittest.TestCase):
    """The real link, fed with lines as captured on the real panel. No reader thread: lines go in by hand."""

    def setUp(self):
        self.t = [100.0]
        self.link = V4ecCoverLink("COM5", clock=lambda: self.t[0])
        self.port = _FakeCoverPort()
        self.link._ser = self.port
        self.link.feed_line(_v4ec_line(14.77))

    def tick(self, s):
        self.t[0] += s

    def test_closed_at_rest(self):
        self.assertTrue(self.link.up())
        snap = self.link.snapshot()
        self.assertEqual((snap.cover, snap.calibrator, snap.brightness), (CoverState.CLOSED, CalibratorState.OFF, 0))
        self.link.feed_line(_v4ec_line(13.71))  # jitter seen at rest stays CLOSED
        self.assertEqual(self.link.snapshot().cover, CoverState.CLOSED)

    def test_silent_move_then_done(self):
        """Open blocks the firmware ~18.4 s with no output, then 'done', then status lines again."""
        self.link.open()
        self.assertEqual(self.port.sent, [b"1001\n"])
        self.tick(10)
        self.assertTrue(self.link.up())  # silence during a move is not a lost panel
        self.assertEqual(self.link.snapshot().cover, CoverState.MOVING)
        self.link.open()  # same direction while moving: accepted, nothing sent
        self.assertEqual(len(self.port.sent), 1)
        for call in (self.link.close, lambda: self.link.calibrator_on(50), self.link.calibrator_off):
            with self.assertRaises(CoverBusy):
                call()
        self.assertEqual(len(self.port.sent), 1)
        self.tick(8.5)
        self.link.feed_line("done\r\n")
        self.link.feed_line(_v4ec_line(295.66))
        self.assertEqual(self.link.snapshot().cover, CoverState.OPEN)
        self.link.feed_line(_v4ec_line(293.91))  # where it settled
        self.assertEqual(self.link.snapshot().cover, CoverState.OPEN)
        self.link.open()  # already open: nothing sent
        self.assertEqual(len(self.port.sent), 1)

    def test_set_angles(self):
        """Wanderer's angle commands as checked on the panel 2026-11-19: 11500 = closed 15°, 69500 = open 295°."""
        self.link.set_angle("close", 15)
        self.link.set_angle("open", 295)
        self.link.set_angle("close", 20.55)
        self.assertEqual(self.port.sent, [b"11500\n", b"69500\n", b"12055\n"])
        self.link.feed_line("ClosePositionSet20.55\r\n")         # the panel's reply isn't a status line: ignored
        self.assertTrue(self.link.up())
        for which, deg in (("close", 9.99), ("close", 90.01), ("open", 99), ("open", 300.5), ("both", 50),
                           ("open", float("nan"))):
            with self.assertRaises(ValueError):
                self.link.set_angle(which, deg)
        self.link.open()
        with self.assertRaises(CoverBusy):                       # the firmware takes nothing while it moves
            self.link.set_angle("open", 280)
        self.assertEqual(self.port.sent[-1], b"1001\n")

    def test_jog(self):
        """The firmware ignores a repeat of its last move (panel, 2026-11-21), so a jog borrows the other end:
        store the target there, move there, give that angle back, store the target here, then this end's move."""
        panel = {"close": 15.0, "open": 295.0, "pos": 14.77, "last": 1000, "pending": 0}

        def line():
            return (f"WandererCoverV4A20250703A{panel['close']:.2f}A{panel['open']:.2f}A{panel['pos']:.2f}"
                    "A12.04A0A50A0A\r\n")

        def answer(s):              # what the panel does with each command, as captured
            self.tick(s)
            for raw in self.port.sent[panel["pending"]:]:
                n = int(raw)
                if n in (1000, 1001):
                    if n != panel["last"]:
                        panel["pos"] = panel["close"] if n == 1000 else panel["open"]
                    panel["last"] = n
                    self.tick(2)
                    self.link.feed_line("done\r\n")
                elif 10000 <= n < 40000:
                    panel["close"] = (n - 10000) / 100
                elif n >= 40000:
                    panel["open"] = (n - 40000) / 100
            panel["pending"] = len(self.port.sent)
            self.link.feed_line(line())

        self.link._sleep = answer
        # closed at 15°: "open 5°" moves the cap to 20° via the open end, then makes 20° the closed angle
        self.assertEqual(self.link.jog(5, background=False), ("close", 20.0))
        self.assertEqual(self.port.sent, [b"42000\n", b"1001\n", b"69500\n", b"12000\n", b"1000\n"])
        self.assertEqual((panel["pos"], panel["close"], panel["open"]), (20.0, 20.0, 295.0))
        self.assertEqual(self.link.snapshot().cover, CoverState.CLOSED)
        # open at 295°: "close 5°" borrows the closed angle (290°, more than the panel keeps over a restart) briefly
        self.port.sent.clear()
        panel.update(pos=294.4, last=1001, pending=0)
        self.link.feed_line(line())
        self.assertEqual(self.link.jog(-5, background=False), ("open", 290.0))
        self.assertEqual(self.port.sent, [b"39000\n", b"1000\n", b"12000\n", b"69000\n", b"1001\n"])
        self.assertEqual((panel["pos"], panel["close"], panel["open"]), (290.0, 20.0, 290.0))
        self.assertEqual(self.link.snapshot().cover, CoverState.OPEN)
        # out of range, too big a step, or nowhere near either end: refused, nothing sent
        self.port.sent.clear()
        panel["pending"] = 0
        for step in (11, 0, 25, -21, float("nan")):
            with self.assertRaises(ValueError):
                self.link.jog(step)
        panel["pos"] = 150
        self.link.feed_line(line())
        with self.assertRaises(CoverBusy):
            self.link.jog(5)
        self.assertEqual(self.port.sent, [])
        # while a jog runs the cover reads MOVING and other commands are refused
        panel["pos"] = 290
        self.link.feed_line(line())
        seen = []

        def watching(s):
            seen.append(self.link.snapshot().cover)
            with self.assertRaises(CoverBusy):
                self.link.calibrator_off()
            answer(s)
        self.link._sleep = watching
        self.link.jog(-1, background=False)
        self.assertEqual(panel["open"], 289.0)
        self.assertEqual(set(seen), {CoverState.MOVING})
        # the panel never confirms the borrowed angle: nothing moves, the jog ends with an error, the cover is free
        self.port.sent.clear()
        self.link._sleep = self.tick
        self.link.jog(-1, background=False)
        self.assertNotIn(b"1000\n", self.port.sent)
        self.assertIn("jog failed", self.link.last_error)
        self.assertNotEqual(self.link.snapshot().cover, CoverState.MOVING)

    def test_done_then_status_line_ends_the_move(self):
        """Between 'done' and the next status line (~0.33 s) the stored line still holds the old position, so the
        cover must still read MOVING. ConformU caught it reading CLOSED right after an open (hub, 2026-11-20)."""
        self.link.open()
        self.tick(18.4)
        self.link.feed_line("done\r\n")
        self.assertEqual(self.link.snapshot().cover, CoverState.MOVING)
        self.tick(0.3)
        self.link.feed_line(_v4ec_line(295.66))
        self.assertEqual(self.link.snapshot().cover, CoverState.OPEN)

    def test_done_without_a_status_line_does_not_hang_as_moving(self):
        self.link.open()
        self.tick(18.4)
        self.link.feed_line("done\r\n")
        self.tick(V4ecCoverLink.DONE_WAIT_S + 0.5)
        self.assertNotEqual(self.link.snapshot().cover, CoverState.MOVING)

    def test_missed_done(self):
        """A status line well after the command ends the move even if 'done' was lost."""
        self.link.feed_line(_v4ec_line(293.91))
        self.link.close()
        self.link.feed_line(_v4ec_line(293.91))  # one already in flight when the command went out
        self.assertEqual(self.link.snapshot().cover, CoverState.MOVING)
        self.tick(18.3)
        self.link.feed_line(_v4ec_line(15.12))
        self.assertEqual(self.link.snapshot().cover, CoverState.CLOSED)

    def test_move_timeout(self):
        self.link.feed_line(_v4ec_line(293.91))
        self.link.close()
        self.tick(V4ecCoverLink.MOVE_TIMEOUT_S + 1)
        self.assertEqual(self.link.snapshot().cover, CoverState.ERROR)
        self.assertFalse(self.link.up())
        self.assertIn("end of the move", self.link.last_error)
        self.link.feed_line(_v4ec_line(15.12))  # the panel talks again
        self.assertEqual(self.link.snapshot().cover, CoverState.CLOSED)

    def test_move_that_stops_short_is_an_error(self):
        """The real cap caught on something while opening (2026-11-23): the panel said "done" at 67.85° and reported
        nothing wrong. Error, with where it stopped, not Unknown, so a client doesn't wait for Open."""
        self.link.open()
        self.tick(18.4)
        self.link.feed_line("done\r\n")
        self.link.feed_line(_v4ec_line(67.85))
        snap = self.link.snapshot()
        self.assertEqual((snap.cover, snap.calibrator), (CoverState.ERROR, CalibratorState.OFF))
        self.assertTrue(self.link.up())
        self.assertIn("stopped at 67.8°, short of open (295°)", self.link.last_error)
        self.tick(0.33)
        self.link.feed_line(_v4ec_line(67.85))          # later status lines don't clear it
        self.assertEqual(self.link.snapshot().cover, CoverState.ERROR)
        self.link.close()                               # a new move clears it
        self.assertEqual((self.link.snapshot().cover, self.link.last_error), (CoverState.MOVING, ""))
        self.tick(5)
        self.link.feed_line("done\r\n")
        self.link.feed_line(_v4ec_line(15.12))
        self.assertEqual(self.link.snapshot().cover, CoverState.CLOSED)

    def test_move_that_arrives_is_no_error(self):
        self.link.open()
        self.tick(18.4)
        self.link.feed_line("done\r\n")
        self.link.feed_line(_v4ec_line(293.91))         # open settles up to ~1 deg short: within tolerance
        self.assertEqual((self.link.snapshot().cover, self.link.last_error), (CoverState.OPEN, ""))

    def test_close_that_stops_short_is_an_error(self):
        self.link.feed_line(_v4ec_line(293.91))
        self.link.close()
        self.tick(1.5)                                  # "done" lost; the panel talks again
        self.link.feed_line(_v4ec_line(120.0))
        self.assertEqual(self.link.snapshot().cover, CoverState.ERROR)
        self.assertIn("short of closed (15°)", self.link.last_error)
        self.assertIn("Open it, then close it again", self.link.last_error)

    def test_silence_at_rest_is_lost(self):
        self.tick(V4ecCoverLink.STALE_S + 0.1)
        self.assertFalse(self.link.up())
        self.assertEqual(self.link.snapshot().cover, CoverState.ERROR)

    def test_light_confirmed_by_status(self):
        self.link.calibrator_on(50)
        self.assertEqual(self.port.sent, [b"50\n"])
        self.assertEqual(self.link.snapshot().calibrator, CalibratorState.NOT_READY)
        self.tick(0.2)
        self.link.feed_line(_v4ec_line(14.77, brightness=50))
        snap = self.link.snapshot()
        self.assertEqual((snap.calibrator, snap.brightness), (CalibratorState.READY, 50))
        self.link.calibrator_off()
        self.assertEqual(self.port.sent[-1], b"9999\n")
        self.tick(0.43)
        self.link.feed_line(_v4ec_line(14.77, brightness=0))
        self.assertEqual(self.link.snapshot().calibrator, CalibratorState.OFF)

    def test_light_refused_while_open(self):
        self.link.feed_line(_v4ec_line(294.61))
        with self.assertRaises(CoverBusy) as e:
            self.link.calibrator_on(30)
        self.assertIn("closed", str(e.exception))
        self.assertEqual(self.port.sent, [])
        self.link.calibrator_off()  # off is always allowed at rest
        self.assertEqual(self.port.sent, [b"9999\n"])

    def test_brightness_zero_is_off(self):
        """The vendor protocol lists brightness 1-255; 0 is sent as the off command."""
        self.link.calibrator_on(0)
        self.assertEqual(self.port.sent, [b"9999\n"])

    def test_light_never_confirmed(self):
        self.link.calibrator_on(80)
        self.tick(V4ecCoverLink.CONFIRM_S + 0.5)
        self.link.feed_line(_v4ec_line(14.77, brightness=0))
        self.assertEqual(self.link.snapshot().calibrator, CalibratorState.ERROR)

    def test_send_without_port(self):
        self.link._ser = None
        with self.assertRaises(RuntimeError):
            self.link.calibrator_on(10)


if __name__ == "__main__":
    unittest.main()


class SimSnagTest(unittest.TestCase):
    """The link and the simulator together, reader thread and all: a cap caught on something reads as Error."""

    def test_snag(self):
        import time
        from wcover.sim import SimV4ec
        sim = SimV4ec(move_s=0.3, line_s=0.05)
        sim.block_at = 67.85
        link = V4ecCoverLink("COM5", opener=sim.opener)
        link.start()
        try:
            def wait_for(state):
                end = time.monotonic() + 5
                while link.snapshot().cover != state:
                    self.assertLess(time.monotonic(), end, f"never {state.name}: {link.snapshot()}")
                    time.sleep(0.02)
            wait_for(CoverState.CLOSED)
            link.open()
            wait_for(CoverState.ERROR)
            self.assertIn("stopped at 67.8", link.last_error)
            sim.block_at = None
            link.close()
            wait_for(CoverState.CLOSED)
            link.open()
            wait_for(CoverState.OPEN)
        finally:
            link.stop()
