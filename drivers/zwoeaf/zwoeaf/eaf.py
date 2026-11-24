"""ZWO's EAF SDK (EAF_focuser.dll, SDK 1.8.5), called with ctypes from its published header EAF_focuser.h.

Only what the driver uses, and only reads plus Open, Close, Move and Stop: the settings stored in the focuser (max
step, reverse, backlash, beep, position reset, alias, LED, Bluetooth) are never set here (REFACTOR.md "Focuser and
filter wheel"). The DLL and ZWO's licence (MIT-style) are in sdk/.

The simulator (sim.py) has the same methods, so the driver can't tell them apart.
"""
import ctypes
import os
import threading

SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdk")
DLL = os.path.join(SDK_DIR, "EAF_focuser.dll")

VID = 0x03C3            # ZWO; the header: "VID is 0x03C3 for EAF"

# EAF_ERROR_CODE
SUCCESS, INVALID_INDEX, INVALID_ID, INVALID_VALUE, REMOVED, MOVING, ERROR_STATE, GENERAL_ERROR, NOT_SUPPORTED, \
    CLOSED, BATTERY_INFO, INVALID_LENGTH = range(12)
ERROR_NAMES = {INVALID_INDEX: "invalid index", INVALID_ID: "invalid ID", INVALID_VALUE: "invalid value",
               REMOVED: "focuser removed", MOVING: "focuser is moving", ERROR_STATE: "focuser in error state",
               GENERAL_ERROR: "general error", NOT_SUPPORTED: "not supported by this focuser", CLOSED: "not open",
               BATTERY_INFO: "battery information abnormal", INVALID_LENGTH: "invalid length"}

# Motor error codes from EAFGetErrorCode (the header's list)
MOTOR_CODES = {"E0": "no error", "E5": "motor stall", "E6": "high temperature: charging stopped",
               "E7": "high temperature: discharging stopped", "E8": "low temperature: charging stopped"}


class EafError(Exception):
    def __init__(self, code, call=""):
        self.code = code
        super().__init__(f"{call}: {ERROR_NAMES.get(code, f'error {code}')}" if call else ERROR_NAMES.get(code, str(code)))


class _Info(ctypes.Structure):          # EAF_INFO
    _fields_ = [("ID", ctypes.c_int), ("Name", ctypes.c_char * 64), ("MaxStep", ctypes.c_int)]


class _Id8(ctypes.Structure):           # EAF_ID / EAF_SN
    _fields_ = [("id", ctypes.c_ubyte * 8)]


class _Type(ctypes.Structure):          # EAF_TYPE
    _fields_ = [("type", ctypes.c_char * 16)]


class _ErrorMsg(ctypes.Structure):      # EAF_ERROR_MSG
    _fields_ = [("motor_error_code", ctypes.c_char * 3), ("battery_error_code", ctypes.c_char * 3)]


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
            "EAFGetNum": ([], i), "EAFGetID": ([i, pi], i), "EAFOpen": ([i], i), "EAFClose": ([i], i),
            "EAFGetProperty": ([i, ctypes.POINTER(_Info)], i), "EAFMove": ([i, i], i), "EAFStop": ([i], i),
            "EAFIsMoving": ([i, pb, pb], i), "EAFGetPosition": ([i, pi], i),
            "EAFGetTemp": ([i, ctypes.POINTER(ctypes.c_float)], i), "EAFGetMaxStep": ([i, pi], i),
            "EAFGetReverse": ([i, pb], i), "EAFGetBacklash": ([i, pi], i), "EAFGetBeep": ([i, pb], i),
            "EAFGetSDKVersion": ([], ctypes.c_char_p),
            "EAFGetFirmwareVersion": ([i] + [ctypes.POINTER(ctypes.c_ubyte)] * 3, i),
            "EAFGetSerialNumber": ([i, ctypes.POINTER(_Id8)], i), "EAFGetType": ([i, ctypes.POINTER(_Type)], i),
            "EAFGetErrorCode": ([i, ctypes.POINTER(_ErrorMsg)], i),
        }
        for name, (args, res) in sig.items():
            f = getattr(d, name)
            f.argtypes, f.restype = args, res

    def _call(self, name, *args):
        with self._lock:
            r = getattr(self._dll, name)(*args)
        if r != SUCCESS:
            raise EafError(r, name)

    # ---------------------------------------------------------------- listing (no open needed)
    def ids(self):
        """The IDs of the focusers plugged in now (EAFGetNum refreshes the list)."""
        with self._lock:
            n = self._dll.EAFGetNum()
        out = []
        for k in range(max(0, n)):
            v = ctypes.c_int()
            try:
                self._call("EAFGetID", k, ctypes.byref(v))
            except EafError:
                continue
            out.append(v.value)
        return out

    def info(self, id_):
        """{"name", "max_step"} (EAFGetProperty)."""
        p = _Info()
        self._call("EAFGetProperty", id_, ctypes.byref(p))
        return {"name": _text(p.Name), "max_step": int(p.MaxStep)}

    def sdk_version(self):
        with self._lock:
            v = self._dll.EAFGetSDKVersion()
        return (v or b"").decode("ascii", "replace").replace(" ", "")

    # ---------------------------------------------------------------- one focuser
    def open(self, id_):
        self._call("EAFOpen", id_)

    def close(self, id_):
        self._call("EAFClose", id_)

    def serial(self, id_):
        """The serial number as hex, or None if the firmware has none."""
        sn = _Id8()
        try:
            self._call("EAFGetSerialNumber", id_, ctypes.byref(sn))
        except EafError as e:
            if e.code == NOT_SUPPORTED:
                return None
            raise
        s = bytes(sn.id).hex().upper()
        return None if s.strip("0") == "" else s

    def firmware(self, id_):
        a, b, c = ctypes.c_ubyte(), ctypes.c_ubyte(), ctypes.c_ubyte()
        self._call("EAFGetFirmwareVersion", id_, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c))
        return f"{a.value}.{b.value}.{c.value}"

    def kind(self, id_):
        """EAFGetType's text, or None where the firmware doesn't say."""
        t = _Type()
        try:
            self._call("EAFGetType", id_, ctypes.byref(t))
        except EafError:
            return None
        return _text(t.type) or None

    def position(self, id_):
        v = ctypes.c_int()
        self._call("EAFGetPosition", id_, ctypes.byref(v))
        return v.value

    def move(self, id_, step):
        self._call("EAFMove", id_, int(step))

    def stop(self, id_):
        self._call("EAFStop", id_)

    def is_moving(self, id_):
        """(moving, moved by the hand controller: EAFStop can't stop that)."""
        m, h = ctypes.c_bool(), ctypes.c_bool()
        self._call("EAFIsMoving", id_, ctypes.byref(m), ctypes.byref(h))
        return bool(m.value), bool(h.value)

    def temperature(self, id_):
        """°C, or None: the header says the value is unusable (-273, an error) while moved by hand, or without a
        sensor."""
        v = ctypes.c_float()
        try:
            self._call("EAFGetTemp", id_, ctypes.byref(v))
        except EafError as e:
            if e.code in (GENERAL_ERROR, NOT_SUPPORTED):
                return None
            raise
        return None if v.value <= -273 else round(float(v.value), 2)

    def max_step(self, id_):
        v = ctypes.c_int()
        self._call("EAFGetMaxStep", id_, ctypes.byref(v))
        return v.value

    def reverse(self, id_):
        v = ctypes.c_bool()
        self._call("EAFGetReverse", id_, ctypes.byref(v))
        return bool(v.value)

    def backlash(self, id_):
        v = ctypes.c_int()
        self._call("EAFGetBacklash", id_, ctypes.byref(v))
        return v.value

    def beep(self, id_):
        v = ctypes.c_bool()
        self._call("EAFGetBeep", id_, ctypes.byref(v))
        return bool(v.value)

    def motor_code(self, id_):
        """The motor error code ("E0" none, "E5" stall …), or None where the firmware has none."""
        m = _ErrorMsg()
        try:
            self._call("EAFGetErrorCode", id_, ctypes.byref(m))
        except EafError as e:
            if e.code == NOT_SUPPORTED:
                return None
            raise
        return _text(m.motor_error_code) or None
