"""The hub's "usb" transport (REFACTOR.md "Devices that aren't a serial port"): a driver is asked for its units only
while a device of its vendor is plugged in, each unit gets its own device and tab, and a unit gone for GONE_S is
dropped. The hub never opens a USB device itself."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hub import host as H  # noqa: E402
from hub.__main__ import simulated_ports  # noqa: E402
from hub.api import Driver, Instance  # noqa: E402
from hub.usbdevs import UsbDevice, parse_instance  # noqa: E402


class FakeUsbDriver(Driver):
    key, title, transport = "fakeusb", "Fake USB", "usb"
    usb_vendors = frozenset({0x03C3})

    def __init__(self):
        self.units = [{"serial": "A1", "name": "Fake"}]
        self.finds = []
        self.started = []

    def find(self, ctx, sims):
        self.finds.append(sims)
        return list(self.units)

    def start(self, ctx, port, ident):
        n = ctx.unit_number()
        inst = Instance(self, port, ident, self.key if n == 0 else f"{self.key}-{n + 1}", "Fake")
        inst.local_port = 1
        self.started.append(port)
        return inst


class UsbTransportTests(unittest.TestCase):
    def setUp(self):
        self.usb = []
        self.drv = FakeUsbDriver()
        self.hub = H.Hub([self.drv], config_dir=tempfile.mkdtemp(), bind="127.0.0.1", http_port=0,
                         port_lister=lambda: [], usb_lister=lambda: list(self.usb), simulated=True)

    def test_asked_only_while_its_vendor_is_plugged_in(self):
        self.usb = [UsbDevice(0x1A86, 0x7523, "USB\\VID_1A86&PID_7523\\1")]       # someone else's device
        self.hub.scan_once()
        self.assertEqual(self.drv.finds, [])
        self.assertEqual(self.hub.instances, {})
        self.usb.append(UsbDevice(0x03C3, 0x1F10, "USB\\VID_03C3&PID_1F10\\X"))
        self.hub.scan_once()
        self.assertEqual(len(self.drv.finds), 1)
        self.assertEqual(list(self.hub.instances), ["fakeusb"])
        port = self.drv.started[0]
        self.assertEqual(port.device, "USB-fakeusb-A1")
        self.assertEqual((port.vid, port.pid, port.serial), (0x03C3, 0x1F10, "A1"))
        self.assertEqual(self.hub.ports_seen, {}, "not a serial port: not in Overview's port list")
        self.assertEqual(self.drv.finds[0], [], "a simulated hub never lets a driver use its real SDK")

    def test_each_unit_once_and_a_second_unit_gets_its_own_tab(self):
        self.usb = [UsbDevice(0x03C3, 0x1F10, "X")]
        self.hub.scan_once()
        self.hub.scan_once()
        self.assertEqual(len(self.drv.started), 1, "a running unit isn't started again")
        self.drv.units.append({"serial": "B2", "name": "Fake"})
        self.hub.scan_once()
        self.assertEqual(sorted(self.hub.instances), ["fakeusb", "fakeusb-2"])

    def test_a_unit_gone_for_a_while_is_dropped(self):
        saved = H.GONE_S
        H.GONE_S = 0.0
        try:
            self.usb = [UsbDevice(0x03C3, 0x1F10, "X")]
            self.hub.scan_once()
            self.assertIn("fakeusb", self.hub.instances)
            self.drv.units = []
            self.hub.scan_once()
            self.assertEqual(self.hub.instances, {})
        finally:
            H.GONE_S = saved

    def test_unplugged_vendor_device_drops_its_units_after_gone(self):
        saved = H.GONE_S
        H.GONE_S = 3600.0
        try:
            self.usb = [UsbDevice(0x03C3, 0x1F10, "X")]
            self.hub.scan_once()
            self.usb = []
            self.hub.scan_once()
            self.assertIn("fakeusb", self.hub.instances, "kept for GONE_S, as a serial device's port is")
            self.assertIn("fakeusb", self.hub._missing)
        finally:
            H.GONE_S = saved

    def test_a_driver_that_cannot_answer_keeps_its_units(self):
        self.usb = [UsbDevice(0x03C3, 0x1F10, "X")]
        self.hub.scan_once()
        self.drv.find = lambda ctx, sims: (_ for _ in ()).throw(RuntimeError("SDK hung"))
        self.hub.scan_once()
        self.assertNotIn("fakeusb", self.hub._missing)

    def test_bad_identities_are_ignored(self):
        self.usb = [UsbDevice(0x03C3, 0x1F10, "X")]
        self.drv.units = [{"name": "no serial"}, {"serial": ""}, "junk", {"serial": "OK"}]
        self.hub.scan_once()
        self.assertEqual([p.serial for p in self.drv.started], ["OK"])

    def test_simulated_devices_reach_the_driver(self):
        sim = object()
        self.usb = [UsbDevice(0x03C3, 0x1F10, "SIM1", sim=sim)]
        self.hub.scan_once()
        self.assertEqual(self.drv.finds, [[sim]])

    def test_a_simulated_hub_never_sees_this_pcs_devices(self):
        h = H.Hub([self.drv], config_dir=tempfile.mkdtemp(), bind="127.0.0.1", http_port=0,
                  port_lister=lambda: [], simulated=True)
        self.assertEqual(h.usb_lister(), [])


class ListingTests(unittest.TestCase):
    def test_instance_ids(self):
        self.assertEqual(parse_instance("USB\\VID_03C3&PID_1F10\\5&2B3&0&1"), (0x03C3, 0x1F10))
        self.assertEqual(parse_instance("USB\\VID_13D3&PID_52BA&MI_00\\7&30E5"), (0x13D3, 0x52BA))
        self.assertIsNone(parse_instance("HID\\VID_03C3&PID_1F10\\1"))
        self.assertIsNone(parse_instance(""))

    def test_simulate_puts_sdk_devices_on_the_usb_list(self):
        lister, opener = simulated_ports("box,eaf")
        self.assertEqual([p.device for p in lister()], ["SIM1"], "only the box is a serial port")
        usb = lister.usb()
        self.assertEqual(len(usb), 1)
        self.assertEqual((usb[0].vid, usb[0].instance), (0x03C3, "SIM2"))
        self.assertIsNotNone(usb[0].sim)

    def test_simulated_eaf_through_the_hub(self):
        lister, opener = simulated_ports("eaf,eaf5v")
        from hub.__main__ import all_drivers
        hub = H.Hub(all_drivers(), config_dir=tempfile.mkdtemp(), bind="127.0.0.1", http_port=0,
                    port_lister=lister, opener=opener, simulated=True)
        try:
            hub.scan_once()
            self.assertEqual(sorted(hub.instances), ["zwoeaf", "zwoeaf-2"])
            self.assertTrue(all(i.up() for i in hub.instances.values()))
        finally:
            for slug in list(hub.instances):
                hub._remove(slug)


if __name__ == "__main__":
    unittest.main()
