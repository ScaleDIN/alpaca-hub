"""ZWO's EFW SDK (EFW_filter.dll, SDK 1.8.6), called with ctypes from its published header EFW_filter.h.

Only what the driver uses, and only reads plus Open, Close and SetPosition: what is stored in the wheel (one-way
rotation, calibration, alias) is never set here (REFACTOR.md "Focuser and filter wheel"). The SDK has no stop: a
wheel that is turning finishes on its own. The DLL and ZWO's licence (MIT-style) are in sdk/.

The simulator (sim.py) has the same methods, so the driver can't tell them apart.
"""
import ctypes
import os
import threading

SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdk")
DLL = os.path.join(SDK_DIR, "EFW_filter.dll")

VID = 0x03C3            # ZWO; the header: "VID is 0x03C3 for EFW"

# EFW_ERROR_CODE
SUCCESS, INVALID_INDEX, INVALID_ID, INVALID_VALUE, REMOVED, MOVING, ERROR_STATE, GENERAL_ERROR, NOT_SUPPORTED, \
    INVALID_LENGTH, CLOSED = range(11)
ERROR_NAMES = {INVALID_INDEX: "invalid index", INVALID_ID: "invalid ID", INVALID_VALUE: "invalid value",
               REMOVED: "filter wheel removed", MOVING: "filter wheel is moving", ERROR_STATE: "filter wheel in error state",
               GENERAL_ERROR: "general error", NOT_SUPPORTED: "not supported by this filter wheel",
               INVALID_LENGTH: "invalid length", CLOSED: "not open"}


class EfwError(Exception):
    def __init__(self, code, call=""):
        self.code = code
        super().__init__(f"{call}: {ERROR_NAMES.get(code, f'error {code}')}" if call else ERROR_NAMES.get(code, str(code)))


class _Info(ctypes.Structure):          # EFW_INFO
    _fields_ = [("ID", ctypes.c_int), ("Name", ctypes.c_char * 64), ("slotNum", ctypes.c_int)]


class _Id8(ctypes.Structure):           # EFW_ID / EFW_SN
    _fields_ = [("id", ctypes.c_ubyte * 8)]


def _text(b):
    return bytes(b).split(b"\0", 1)[0].decode("ascii", "replace").strip()


class Sdk:
    """The real SDK. One per driver process; its calls are serialised (the header promises nothing about threads)."""

    def __init__(self, path=DLL):
        self._dll = ctypes.CDLL(path)
        self._lock = threading.Lock()
        d = self._dll
        i, pi, pb = ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_bool)
        sig = {
            "EFWGetNum": ([], i), "EFWGetID": ([i, pi], i), "EFWOpen": ([i], i), "EFWClose": ([i], i),
            "EFWGetProperty": ([i, ctypes.POINTER(_Info)], i), "EFWGetPosition": ([i, pi], i),
            "EFWSetPosition": ([i, i], i), "EFWGetDirection": ([i, pb], i),
            "EFWGetSDKVersion": ([], ctypes.c_char_p), "EFWGetHWErrorCode": ([i, pi], i),
            "EFWGetFirmwareVersion": ([i] + [ctypes.POINTER(ctypes.c_ubyte)] * 3, i),
            "EFWGetSerialNumber": ([i, ctypes.POINTER(_Id8)], i),
        }
        for name, (args, res) in sig.items():
            f = getattr(d, name)
            f.argtypes, f.restype = args, res

    def _call(self, name, *args):
        with self._lock:
            r = getattr(self._dll, name)(*args)
        if r != SUCCESS:
            raise EfwError(r, name)

    # ---------------------------------------------------------------- listing (no open needed)
    def ids(self):
        """The IDs of the wheels plugged in now (EFWGetNum refreshes the list, and must run before EFWOpen)."""
        with self._lock:
            n = self._dll.EFWGetNum()
        out = []
        for k in range(max(0, n)):
            v = ctypes.c_int()
            try:
                self._call("EFWGetID", k, ctypes.byref(v))
            except EfwError:
                continue
            out.append(v.value)
        return out

    def info(self, id_):
        """{"name", "slots"} (EFWGetProperty). slots is 0 before the wheel is opened, and the call answers MOVING
        while the wheel counts its slots, soon after it is plugged in."""
        p = _Info()
        self._call("EFWGetProperty", id_, ctypes.byref(p))
        return {"name": _text(p.Name), "slots": int(p.slotNum)}

    def sdk_version(self):
        with self._lock:
            v = self._dll.EFWGetSDKVersion()
        return (v or b"").decode("ascii", "replace").replace(" ", "")

    # ---------------------------------------------------------------- one wheel
    def open(self, id_):
        self._call("EFWOpen", id_)

    def close(self, id_):
        self._call("EFWClose", id_)

    def serial(self, id_):
        """The serial number as hex, or None if the firmware has none."""
        sn = _Id8()
        try:
            self._call("EFWGetSerialNumber", id_, ctypes.byref(sn))
        except EfwError as e:
            if e.code == NOT_SUPPORTED:
                return None
            raise
        s = bytes(sn.id).hex().upper()
        return None if s.strip("0") == "" else s

    def firmware(self, id_):
        a, b, c = ctypes.c_ubyte(), ctypes.c_ubyte(), ctypes.c_ubyte()
        self._call("EFWGetFirmwareVersion", id_, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c))
        return f"{a.value}.{b.value}.{c.value}"

    def position(self, id_):
        """The slot, 0 to slots - 1, or -1 while the wheel turns."""
        v = ctypes.c_int()
        self._call("EFWGetPosition", id_, ctypes.byref(v))
        return v.value

    def set_position(self, id_, slot):
        self._call("EFWSetPosition", id_, int(slot))

    def one_way(self, id_):
        """True when the wheel turns one way only (stored in the wheel, set with ZWO's software)."""
        v = ctypes.c_bool()
        self._call("EFWGetDirection", id_, ctypes.byref(v))
        return bool(v.value)

    def hw_error(self, id_):
        """EFWGetHWErrorCode's number (0: none), or None where the firmware doesn't say."""
        v = ctypes.c_int()
        try:
            self._call("EFWGetHWErrorCode", id_, ctypes.byref(v))
        except EfwError as e:
            if e.code == NOT_SUPPORTED:
                return None
            raise
        return v.value
