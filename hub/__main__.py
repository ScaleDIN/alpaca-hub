"""python -m hub [--simulate nitecrawler,nitecrawler] [--http-port 11111] ..."""
import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading

from . import NAME, VERSION
from .host import Hub, HubSettings, default_config_dir
from .ports import PortInfo
from .usbdevs import UsbDevice


def all_drivers():
    from .drivers import all_drivers as _all     # imports every driver: only for --in-process and the tests
    return _all()


def driver_modules():
    """The driver modules, by name, without importing them (the hub process never runs driver code)."""
    import pkgutil
    from . import drivers
    return [f"{drivers.__name__}.{m.name}" for m in sorted(pkgutil.iter_modules(drivers.__path__), key=lambda m: m.name)
            if not m.ispkg and not m.name.startswith("_")]


def _setup_logging(verbose, path):
    # the windowless exe (AlpacaHub.exe) has no console: sys.stderr is None there, so only the file
    handlers = [logging.StreamHandler()] if sys.stderr is not None else []
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def _simulators(drivers):
    """{simulator name: (driver, factory)} from what each driver declares."""
    out = {}
    for d in drivers:
        for name, factory in d.simulators.items():
            out[name] = (d, factory)
    return out


def simulated_ports(spec, drivers=None):
    """'nitecrawler,nitecrawler' -> (port lister, opener) serving simulated devices on SIM1, SIM2 …
    The names are the drivers' own (Driver.simulators)."""
    known = _simulators(drivers if drivers is not None else all_drivers())
    sims, ports, usb = {}, [], []
    for i, kind in enumerate(k.strip() for k in spec.split(",") if k.strip()):
        if kind not in known:
            raise SystemExit(f"--simulate: unknown device {kind!r} (known: {', '.join(sorted(known))})")
        name = f"SIM{i + 1}"
        sim, (vid, pid), serial, desc = known[kind][1](i)
        if getattr(known[kind][0], "transport", "serial") == "usb":
            usb.append(UsbDevice(vid, pid, name, desc, sim=sim))     # an SDK device: no serial port
            continue
        sims[name] = sim
        ports.append(PortInfo(name, vid, pid, serial, desc))

    def opener(device, baud, lines=False):
        if device not in sims:
            raise OSError(f"{device} not found")
        return sims[device].opener(device)

    def lister():
        return list(ports)
    lister.usb = lambda: list(usb)                # the hub's USB device list (Hub: usb_lister)
    return lister, opener


def pick_drivers(spec, drivers=None):
    """--drivers: driver keys, or any of a driver's simulator names ("box" for wbox, "panel" for wcover)."""
    drivers = drivers if drivers is not None else all_drivers()
    if not spec:
        return drivers
    names = {d.key: d.key for d in drivers}
    names.update({n: d.key for n, (d, _) in _simulators(drivers).items()})
    want = set()
    for name in (k.strip() for k in spec.split(",") if k.strip()):
        if name not in names:
            raise SystemExit(f"--drivers: unknown kind {name!r} (known: {', '.join(sorted(names))})")
        want.add(names[name])
    return [d for d in drivers if d.key in want]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--driver" in argv:                         # a driver process, started by the hub (hub/supervisor.py)
        dp = argparse.ArgumentParser(prog="hub --driver")
        dp.add_argument("--driver", required=True)
        dp.add_argument("--pipe", required=True)
        dp.add_argument("--config-dir", required=True)
        dp.add_argument("--driver-path")
        dp.add_argument("--log-name")
        d = dp.parse_args(argv)
        from .driverhost import main as driver_main
        return driver_main(d.driver, d.pipe, d.config_dir, d.driver_path, d.log_name)
    ap = argparse.ArgumentParser(prog="hub", description=f"{NAME}: one Alpaca service for every device on this PC")
    ap.add_argument("--http-port", type=int, help="Alpaca HTTP port (default 11111, saved)")
    ap.add_argument("--bind", help="address to listen on (default 0.0.0.0, saved)")
    ap.add_argument("--config-dir", help=f"settings folder (default {default_config_dir()})")
    ap.add_argument("--simulate", metavar="DEVICES",
                    help="simulated devices instead of the serial ports, e.g. nitecrawler,box,panel,lynx,gemini "
                         "(the drivers' simulator names)")
    ap.add_argument("--drivers", metavar="KINDS",
                    help="only these kinds of device, e.g. nitecrawler,panel (default: all; driver keys or the names "
                         "above). Ports of the others aren't opened at all")
    ap.add_argument("--driver-folder", action="append", default=[], metavar="FOLDER",
                    help="also run the driver in FOLDER (a folder with driver.json), or every driver folder in it: "
                         "for writing a driver (docs/WRITING-DRIVERS.md). May be given more than once")
    ap.add_argument("--in-process", action="store_true",
                    help="run the drivers inside the hub's process, as before step 5 (for debugging only)")
    ap.add_argument("--log", help="log file (default hub.log in the settings folder; 'none' to disable)")
    ap.add_argument("--open", action="store_true",
                    help="open the status page in the browser once the hub listens (the Start-menu shortcut)")
    ap.add_argument("--stop", action="store_true", help="stop the hub running on this PC, cleanly, and exit")
    ap.add_argument("--wait", action="store_true", help="with --stop: wait until it has stopped")
    ap.add_argument("--open-folder", action="store_true", help="open the settings and log folder, and exit")
    ap.add_argument("--firewall", action="store_true",
                    help="installed hub: let other computers on private networks reach it again (asks for "
                         "administrator rights; removes Windows' block rules for it), and exit")
    ap.add_argument("--update-feed", metavar="URL",
                    help="read the update list from this address instead of the built-in one (trying a release)")
    ap.add_argument("--version", action="version", version=f"{NAME} {VERSION}")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    if a.firewall:
        from . import firewall
        return firewall.run()
    config_dir = a.config_dir or (os.path.join(default_config_dir(), "sim") if a.simulate else default_config_dir())
    saved_port = a.http_port or HubSettings.load(os.path.join(config_dir, "hub.json")).http_port
    if a.open_folder:
        os.makedirs(config_dir, exist_ok=True)
        os.startfile(config_dir)
        return 0
    if a.stop:
        return _stop(saved_port, a.wait)
    if _running(saved_port):
        # Started again (Start menu, a second click): show the one that runs instead of failing on the busy port.
        print(f"{NAME} already runs: http://localhost:{saved_port}/")
        if a.open or sys.stdout is None:
            _open_page(saved_port)
        return 0
    log_path = a.log or os.path.join(config_dir, "hub.log")
    _setup_logging(a.verbose, None if log_path.lower() == "none" else log_path)

    kw = {}
    sup = None
    if a.in_process:
        from .catalog import Catalog, load_in_process
        drivers = load_in_process(Catalog(installed="", extra=a.driver_folder))
        if a.simulate:
            lister, opener = simulated_ports(a.simulate, drivers)
            kw = dict(port_lister=lister, opener=opener, simulated=True, use_standalone_settings=False, scan_s=3.0)
        chosen = pick_drivers(a.drivers, drivers)
    else:
        from .supervisor import Supervisor
        from .catalog import Catalog
        saved = HubSettings.load(os.path.join(config_dir, "hub.json"))
        sup = Supervisor(config_dir=config_dir, simulated=bool(a.simulate), primary=saved.primary,
                         use_standalone_settings=not a.simulate, verbose=a.verbose,
                         catalog=Catalog(extra=a.driver_folder),
                         off=saved.drivers_off)
        if a.simulate:
            lister, opener = sup.simulated_ports(a.simulate)
            kw = dict(port_lister=lister, opener=opener, simulated=True, use_standalone_settings=False, scan_s=3.0)
        chosen = pick_drivers(a.drivers, sup.drivers())
        kw["supervisor"] = sup
    done = threading.Event()
    running = None if a.simulate else os.path.join(programdata_dir(), "running.json")
    hub = Hub(chosen, config_dir=config_dir, bind=a.bind, http_port=a.http_port, on_shutdown=done.set,
              running_file=running, update_feed=a.update_feed, **kw)
    try:
        hub.start()
    except OSError as e:
        logging.getLogger("hub").error("cannot listen on port %s: %s", hub.settings.http_port, e)
        if sup is not None:
            sup.stop()
        return 1
    hub._save()
    print(f"{NAME}: http://localhost:{hub.port}/")
    if a.open:
        _open_page(hub.port)

    signal.signal(signal.SIGINT, lambda *_: done.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    while not done.wait(0.5):
        pass
    hub.stop()
    return 0


def programdata_dir():
    """%ProgramData%\\AlpacaHub: installed drivers (drivers\\) and running.json. The hub's installer makes it."""
    return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "AlpacaHub")


def _hub_request(port, path, method="GET", timeout=3):
    import json
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None, headers={"X-Hub-Page": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _running(port):
    try:
        return _hub_request(port, "/hub.json", timeout=2).get("name") == NAME
    except Exception:
        return False


def _open_page(port):
    import webbrowser
    webbrowser.open(f"http://localhost:{port}/")


def _stop(port, wait):
    """hub --stop: asks the running hub to stop cleanly (its drivers stop their devices, e.g. the box's dew heater
    when it was set to switch off on exit). Exit code 0 if it stopped or wasn't running."""
    import time
    if not _running(port):
        print(f"{NAME} isn't running on port {port}")
        return 0
    try:
        _hub_request(port, "/hub/shutdown", method="POST")
    except Exception as e:
        print(f"could not ask it to stop: {e}")
        return 1
    if wait:
        end = time.monotonic() + 30
        while _running(port) and time.monotonic() < end:
            time.sleep(0.3)
        if _running(port):
            print("it is still running after 30 s")
            return 1
    print(f"{NAME} stopped" if wait else f"{NAME} is stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
