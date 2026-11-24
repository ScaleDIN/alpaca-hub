"""Optec FocusLynx / ThirdLynx and Gemini / Hercules: the wire formats and the links, against the simulators, which
answer with the formats printed by Optec's firmware (ThirdLynx 3.0.4, Gemini 2.0.1). Moved from tests/test_optec.py
with the code (REFACTOR.md step 10). None of this has met real Optec hardware yet."""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                                   # the driver folder (its package)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))  # the repository (hub)

from optec import gemini as G  # noqa: E402
from optec import lynx as L  # noqa: E402
from optec.sim import SimGemini, SimLynx  # noqa: E402
from optec.wire import NotReady, OptecError, Wire, parse_block  # noqa: E402

THIRDLYNX = (0x04D8, 0xED77)
OPTEC_FTDI = (0x0403, 0x6001)
CH340 = (0x1A86, 0x7523)

# What the hub may ever send an Optec hub: questions, moves, halt, home, sync, the rotator's reverse.
LYNX_ALLOWED = {"GETHUBINFO", "GETVER", "GETCONFIG", "GETSTATUS", "MA", "HALT", "HOME", "SCCP"}
GEMINI_ALLOWED = {"GETCFG", "GETSER", "GETSTA", "MOVABS", "MOVEPA", "DOHALT", "DOHOME", "SETREV"}


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class Lines:
    """A port that answers every write with fixed lines, for parsing tests."""

    def __init__(self, *lines):
        self.lines = [ln.encode() + b"\n" for ln in lines]
        self.sent = []

    def write(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def reset_input_buffer(self):
        pass


class WireFormats(unittest.TestCase):
    def test_lynx_status_block_from_the_firmware(self):
        port = Lines("!", "STATUS1", "Temp(C)  = NP", "Curr Pos = 012345", "Targ Pos = 020000", "IsMoving = 1",
                     "IsHoming = 0", "IsHomed  = 1", "FFDetect = 0", "TmpProbe = 0", "RemoteIO = 0", "Hnd Ctlr = 0",
                     "Reverse  = 0", "END")
        st = L.parse_status(L.exchange(Wire(port), "F1", "GETSTATUS"))
        self.assertEqual(port.sent, [b"<F1GETSTATUS>"])
        self.assertEqual((st.position, st.target, st.moving, st.homed), (12345, 20000, True, True))
        self.assertIsNone(st.temperature, "NP means no probe, not 0 °C")

    def test_lynx_move_and_refusal(self):
        port = Lines("!", "M")
        self.assertEqual(L.exchange(Wire(port), "F2", "MA", "001500"), ["M"])
        self.assertEqual(port.sent, [b"<F2MA001500>"])
        with self.assertRaises(OptecError) as e:
            L.exchange(Wire(Lines("ER=11 Cannot Execute Command While Device Is Moving", "END")), "F1", "MA", "000010")
        self.assertEqual(e.exception.code, 11)
        self.assertIn("Moving", e.exception.text)

    def test_gemini_frames_and_acks(self):
        f = G.Framer()
        self.assertEqual(f.frame("R", "MOVEPA", "090000", tid=7), "<R107MOVEPA090000>")
        port = Lines("!01", "CurrStep = 004500", "TargStep = 004500", "LastStep = 004500", "CurentPA = 045000",
                     "TargetPA = 045000", "IsMoving = 0", "IsHoming = 0", "Is Homed = 1", "END")
        st = G.parse_rotator_status(G.exchange(Wire(port), G.Framer(), "R", "GETSTA"))
        self.assertEqual(port.sent, [b"<R101GETSTA>"])
        self.assertEqual((st.pa, st.target_pa, st.homed), (45.0, 45.0, True))
        with self.assertRaises(OptecError) as e:
            G.exchange(Wire(Lines("ERROR ID = 10", "ERROR TEXT = Cannot Execute Command - Device Is Not Homed", "END")),
                       G.Framer(), "R", "MOVEPA", "001000")
        self.assertEqual(e.exception.code, 10)

    def test_block_parsing_keeps_padded_keys_apart(self):
        b = parse_block(["IsHomed  = 1", "Is Homed = 0", "Temp(C)  = 11.6"])
        self.assertEqual(b, {"IsHomed": "1", "Is Homed": "0", "Temp(C)": "11.6"})


class Units(unittest.TestCase):
    def test_probes_tell_the_models_apart(self):
        self.assertEqual(L.probe(SimLynx().opener())["model"], "ThirdLynx")
        self.assertEqual(L.probe(SimLynx(model="FocusLynx").opener())["model"], "FocusLynx")
        self.assertIsNone(L.probe(SimGemini().opener()), "a Gemini is not a FocusLynx")
        self.assertIsNone(G.probe(SimLynx().opener()), "a FocusLynx is not a Gemini")
        self.assertEqual(G.probe(SimGemini(serial="77").opener())["serial"], "77")

    def test_lynx_unit_moves_and_reports_moving_until_there(self):
        sim = SimLynx(speed=4000)
        u = L.LynxUnit("S", lambda n: sim.opener())
        u.start()
        try:
            self.assertTrue(wait_for(u.up))
            self.assertEqual(u.channels, ["F1"], "F2 has no focuser type (ZZ)")
            u.move("F1", 27000)
            self.assertTrue(u.is_moving("F1"), "moving right after the command")
            self.assertTrue(wait_for(lambda: not u.is_moving("F1")))
            self.assertEqual(u.snapshot("F1").position, 27000)
            with self.assertRaises(ValueError):
                u.move("F1", 60000)            # beyond Max Pos
            with self.assertRaises(NotReady):
                u.move("F2", 10)
        finally:
            u.stop()
        self.assertTrue({v for _, v, _ in sim.log} <= LYNX_ALLOWED, sim.log)

    def test_gemini_rotator_needs_homing(self):
        sim = SimGemini(rot_homed=False)
        u = G.GeminiUnit("S", lambda n: sim.opener())
        u.start()
        try:
            self.assertTrue(wait_for(u.up))
            with self.assertRaises(NotReady):
                u.rotator_move(10.0)
            u.home("R")
            self.assertTrue(wait_for(lambda: u.rotator.homed))
            u.rotator_move(10.0)
            self.assertTrue(wait_for(lambda: not u.is_moving("R")))
            self.assertAlmostEqual(u.rotator.pa, 10.0)
        finally:
            u.stop()

    def test_gemini_late_end_after_setrev(self):
        """A real Hercules on firmware 1.3.1 (2026-11-23) sends "END" a moment after SETREV's "!". Taken as the next
        command's answer, it put every answer one step behind and the driver gave up; ConformU's Reverse test found
        it. The leftover is skipped, the config read right after shows the new direction, and polling goes on."""
        sim = SimGemini(firmware="1.3.1")
        u = G.GeminiUnit("S", lambda n: sim.opener())
        u.start()
        try:
            self.assertTrue(wait_for(u.up))
            for on in (True, False, True):
                u.set_rotator_reverse(on)
                self.assertEqual(u.rotator_reverse(), on)
                self.assertEqual(sim.reverse, int(on))
            u.rotator_move(20.0)
            self.assertTrue(wait_for(lambda: not u.is_moving("R")))
            self.assertAlmostEqual(u.rotator.pa, 20.0)
            self.assertTrue(u.up())
        finally:
            u.stop()


if __name__ == "__main__":
    unittest.main()
