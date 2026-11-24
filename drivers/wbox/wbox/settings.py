"""Settings file (JSON). Kept small and human-editable; written atomically."""
import json
import logging
import os
import threading
from dataclasses import asdict, fields

from .dew import DewSettings

DEFAULT_GAUGES = ("dew_power", "voltage", "current", "power", "lens", "ambient", "humidity", "dewpoint")

log = logging.getLogger("wbox.settings")


def default_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "wbstock", "settings.json")


class Settings:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.port = "auto"
        self.last_port = ""             # auto mode: where the box was found last time (tried first)
        self.http_port = 11111
        self.bind = "0.0.0.0"
        self.dew = DewSettings()
        self.dew_mode_adopted = False   # False until the first run has taken over the box's dew state
        self.dew_off_on_exit = True     # automatic modes: heater off when the service stops
        self.eeprom_writes = 0          # commands sent to the box since the settings file was created
        self.location = ""
        self.device_name = ""           # base name Alpaca programs see ("" = WandererBox Plus V3); + " outputs"/" sensors"
        self.instance_id = ""           # "" = one box on this PC (UniqueIDs from the MAC, as before). Set a short
                                        # distinct id per box only when running a second instance on the SAME PC, so
                                        # their Alpaca UniqueIDs differ. Changing it re-keys the devices (re-pair in the Alpaca programs).
        self.cover_port = ""            # "" = no flat panel; "SIM" = simulated panel; else the WandererCover's COM port.
                                        # When set, the service also serves an Alpaca CoverCalibrator (#0).
        self.gauges = list(DEFAULT_GAUGES)  # read-only switches shown to Alpaca clients, see service.GAUGES
        self.history_days = 7           # days of 1-minute history kept on disk (history.py)
        self.use_lens = True            # False: the lens probe (DS18B20) isn't used; reads as "not used", not missing
        self.use_ambient = True         # False: the DHT22 isn't used (ambient, humidity, dew point)
        self.missing_gauge = "hold"     # a gauge whose probe is missing: "hold" (last value), "minimum", "value"
        self.missing_value = 0.0        # the value for "value" (and "hold" before any good reading), kept in range

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
        for k in ("port", "last_port", "http_port", "bind", "dew_mode_adopted", "dew_off_on_exit", "eeprom_writes", "location", "device_name",
                  "instance_id", "cover_port", "history_days", "use_lens", "use_ambient", "missing_gauge", "missing_value"):
            if k in d:
                setattr(s, k, type(getattr(s, k))(d[k]))
        if isinstance(d.get("gauges"), list):
            s.gauges = [str(k) for k in d["gauges"]]
        names = {f.name: f.type for f in fields(DewSettings)}
        for k, v in (d.get("dew") or {}).items():
            if k in names:
                setattr(s.dew, k, int(v) if k == "mode" else bool(v) if k == "dc3_on" else float(v))
        return s

    def save(self):
        d = {"port": self.port, "last_port": self.last_port, "http_port": self.http_port, "bind": self.bind, "location": self.location,
             "device_name": self.device_name, "instance_id": self.instance_id, "cover_port": self.cover_port,
             "dew": asdict(self.dew), "dew_mode_adopted": self.dew_mode_adopted,
             "dew_off_on_exit": self.dew_off_on_exit, "eeprom_writes": self.eeprom_writes, "gauges": list(self.gauges),
             "history_days": self.history_days, "use_lens": self.use_lens, "use_ambient": self.use_ambient,
             "missing_gauge": self.missing_gauge, "missing_value": self.missing_value}
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, self.path)
