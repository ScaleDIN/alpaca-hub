"""USB devices that are plugged in, by vendor and product ID: what the hub watches for drivers with transport "usb"
(REFACTOR.md "Devices that aren't a serial port"). Such devices (the ZWO EAF, EFW, cameras) are opened only by their
driver, through the maker's SDK. The hub never opens them: it only reads Windows' device list, so a driver is asked to
look for its units only while one of its vendor's devices is there."""
import re
import sys
from dataclasses import dataclass

_ID = re.compile(r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&[^\\]*)?\\(.*)$", re.I)


@dataclass(frozen=True)
class UsbDevice:
    vid: int
    pid: int
    instance: str = ""        # Windows' device instance ID ("USB\VID_03C3&PID_1F10\5&2B…"), or "SIM1" when simulated
    description: str = ""
    sim: object = None        # simulated, in this process: the simulator itself
    sim_name: str = ""        # simulated, in a driver process: its simulator's name and index there
    sim_index: int = 0


def parse_instance(instance):
    """(vid, pid) from a device instance ID, or None."""
    m = _ID.match(instance or "")
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


def list_usb():
    """The USB devices Windows lists as present ([] elsewhere, or if the list can't be read)."""
    if sys.platform != "win32":
        return []
    try:
        return _setupapi_list()
    except OSError:
        return []


def _setupapi_list():
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_void_p)]

    DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
    SPDRP_DEVICEDESC = 0x0
    INVALID = ctypes.c_void_p(-1).value
    api = ctypes.WinDLL("setupapi", use_last_error=True)
    api.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    api.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    api.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    api.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
                                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA),
                                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                      ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD)]
    api.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    h = api.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not h or h == INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    try:
        i = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not api.SetupDiEnumDeviceInfo(h, i, ctypes.byref(info)):
                break
            i += 1
            buf = ctypes.create_unicode_buffer(512)
            if not api.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(info), buf, len(buf), None):
                continue
            ids = parse_instance(buf.value)
            if ids is None:
                continue
            desc = ctypes.create_unicode_buffer(256)
            if not api.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(info), SPDRP_DEVICEDESC, None, desc,
                                                         ctypes.sizeof(desc), None):
                desc.value = ""
            out.append(UsbDevice(ids[0], ids[1], buf.value, desc.value))
    finally:
        api.SetupDiDestroyDeviceInfoList(h)
    return out
