"""Settings file (JSON), small and human-editable, written atomically."""
import json
import logging
import os
import threading

log = logging.getLogger("nitecrawler.settings")

FIELDS = {
    "port": "auto",              # COM port, or "auto" (looks for a NiteCrawler on the USB serial ports)
    "last_port": "",             # auto mode: where it was found last time (tried first)
    "http_port": 11111,
    "bind": "0.0.0.0",
    "device_name": "",           # base name Alpaca programs see ("" = NiteCrawler)
    "instance_id": "",           # only for a second NiteCrawler on the SAME PC (distinct Alpaca UniqueIDs)
    "location": "",
    "focus_max": 999999,         # Focuser #0 MaxStep
    "aux_enabled": False,        # serve the aux axis as Focuser #1
    "aux_max": 999999,
    "focus_backlash": 0,         # steps; 0 = off. Inward moves overshoot by this much and come back out
    "aux_backlash": 0,           # (most users let the imaging program do backlash instead)
    "rot_reverse": False,        # Rotator Reverse
    "rot_offset": 0.0,           # Rotator sync offset, degrees (sky = mechanical + offset)
    "brightness": 150,           # last display brightness set from the page (the firmware can't be asked)
    "sleep_brightness": 16,
}


def default_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ncalpaca", "settings.json")


class Settings:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        for k, v in FIELDS.items():
            setattr(self, k, v)

    @classmethod
    def load(cls, path):
        s = cls(path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return s
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable settings file %s: %s", path, e)
            return s
        for k, default in FIELDS.items():
            if k in d:
                try:
                    setattr(s, k, type(default)(d[k]))
                except (TypeError, ValueError):
                    log.warning("settings: ignoring bad %s=%r", k, d[k])
        return s

    def save(self):
        d = {k: getattr(self, k) for k in FIELDS}
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, self.path)
