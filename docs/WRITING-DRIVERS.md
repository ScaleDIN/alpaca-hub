# Writing a driver for the Alpaca Hub

The Alpaca Hub is one ASCOM Alpaca service for every astronomy device on a PC. The hub finds devices on the serial
ports, holds the ports, serves every device at one Alpaca address with one discovery responder, and shows a status
page with a tab per device. **Drivers** know the devices. Each driver is a folder that installs on its own, runs in
its own process, and can be updated or removed while the hub runs.

This guide covers writing one. A complete, working example comes with it: [`docs/example-driver/thermo`](example-driver/thermo),
a USB serial thermometer served as an Alpaca ObservingConditions device. The hub's own tests run it, so it stays
correct. Copy it to start.

Contents: [the folder](#1-the-driver-folder) · [running it while you write it](#2-running-it-while-you-write-it) ·
[the Driver](#3-the-driver-recognising-your-device) · [the Instance](#4-the-instance-running-your-device) ·
[the port](#5-the-port) · [Alpaca devices](#6-alpaca-devices) · [the status page](#7-the-status-page) ·
[rules](#8-rules) · [testing](#9-testing) · [packaging, updates](#10-packaging-installing-updating) ·
[checklist](#11-checklist)

## 1. The driver folder

```
thermo\                          the folder's name is the driver's key
  driver.json                    {"key": "thermo", "title": "Example thermometer", "version": "1.0.0", "api": 1}
  thermo\                        your package (Python)
    __init__.py                  VERSION = "1.0.0" (the same as driver.json)
    driver.py                    exports DRIVERS = [YourDriver]
    ...                          your protocol, link, simulator, settings code
    ui\body.html, device.js, device.css     its status page (the hub adds its UI kit)
  tests\test_thermo_*.py         your tests
```

`driver.json`:

| Field | Meaning |
|---|---|
| `key` | Short id: lower-case letters, digits and `_`, starting with a letter. **It must equal the folder's name, and never change once released**: settings paths and Alpaca UniqueIDs are made from it. |
| `title` | What people see (Overview's Drivers list, the uninstall entry). |
| `version` | `1.2.0` style. The hub runs the highest version installed. |
| `api` | The hub API it was written for (today `1`). A hub with another API refuses it and says why. |
| `module` | Optional: the module that exports `DRIVERS` (default `<key>.driver`). |
| `min_hub` | Optional: the oldest Alpaca Hub version it works with, e.g. `"0.3.0"` (when it relies on something a newer hub added, such as the page kit's charts). An older hub refuses it and runs the newest version it can; the driver's installer refuses to install on it. |
| `python` | Only if the folder carries compiled modules (`.pyd`): the Python version they were built for, e.g. `"3.14"`. The hub refuses a mismatch. |

## 2. Running it while you write it

With the hub from source (this repository), or with the installed hub's console program:

```
python -m hub --driver-folder D:\my-drivers\thermo --simulate thermo
alpaca-hub.exe --driver-folder D:\my-drivers\thermo --simulate thermo
```

`--driver-folder` runs your driver as if installed (repeat it for several, or give a folder of driver folders).
`--simulate thermo` uses your driver's simulator instead of real ports (section 3). The status page is at
http://localhost:11111/; your driver's log is `logs\thermo.log` in the settings folder
(`%LOCALAPPDATA%\alpaca-hub`, or `...\alpaca-hub\sim` while simulating). `--in-process` runs drivers inside the hub's
process instead, which is handy with a debugger. Drop `--simulate` to use the real device.

## 3. The Driver: recognising your device

```python
from hub.api import Driver

class ThermoDriver(Driver):
    key = "thermo"
    title = "Example thermometer"
    usb_ids = frozenset({(0x1209, 0x0001)})   # (VID, PID) of its USB serial chip
    listen_baud = 9600                         # it talks unasked: recognise() gets what it said
    simulators = {"thermo": lambda i: (SimThermo(serial=f"T{i + 1}"), (0x1209, 0x0001), "", "Simulated thermometer")}

    def recognise(self, data):
        ...                                    # bytes heard -> {"serial": ...} or None

    def start(self, ctx, port, ident):
        return ThermoInstance(self, ctx, port, ident)

DRIVERS = [ThermoDriver]
```

How the hub finds devices (it enforces this order, for every driver):

1. **Only ports with your `usb_ids`.** Other ports are never opened for you.
2. **Listening first.** If you set `listen_baud`, the hub opens the port at that baud, listens for a moment, sends
   nothing, and calls `recognise(data)` with what it heard. Return an identity dict, or `None` if it isn't yours.
   Prefer this whenever your device talks on its own: it can't upset any device.
3. **Asking, only on silent ports.** If your device only answers when asked, set `probe_baud` and write
   `probe(ser)`: it gets the open port (section 5) and may write a question and read the answer. It is called only
   for ports that stayed silent at every listening baud. Several drivers can probe the same chip (FTDI and CH340
   are everywhere): `probe_priority` orders them, lowest first, and a probe must be short and harmless to other
   devices (a read-only query, no settings).
4. **`never_probe`**: if your device acts on any byte it receives (the WandererBox stores every byte as a command),
   list its USB IDs here. The hub then refuses any driver that would probe them and drops any byte written to them
   during detection.
5. **`lines = True`** if your device needs DTR and RTS on (some only talk then). The hub sets them before the first
   open and never toggles them.

The **identity** (`recognise`/`probe`'s dict) should include `"serial"` when the device reports a serial number:
that is how two of the same device are told apart (see `unit_of` below). It crosses a pipe as JSON, so use plain
values only: strings, numbers, booleans, lists, dicts.

**Devices opened through a maker's SDK** (USB HID or vendor USB, not a COM port: ZWO's EAF, for example) set
`transport = "usb"` and `usb_vendors = frozenset({0x03C3})` instead of `usb_ids`, listening and probing. The hub never
opens them: while a USB device of one of those vendors is plugged in, it calls `find(ctx, sims)` at every scan, and
your driver returns the units its SDK lists, as identities with `"serial"` (units in use by another program left
out). Each new unit is started with `start(ctx, port, ident)`, where `port` is a stand-in named
`USB-<key>-<serial>` that is never opened; a unit missing from `find` for a minute is dropped. `sims` is `None` for
the real SDK, or the simulators that are plugged in (`--simulate`, the tests), which then stand in for the SDK. Your
process must stop any motion itself in `stop()`. See [`drivers/zwoeaf`](../drivers/zwoeaf).

**Simulators**: `simulators = {name: factory}`, `factory(index) -> (simulator, (vid, pid), usb serial, description)`.
The simulator has `opener(device, baud)` returning a port-like object (`readline`, `read`, `write`, `close`; see
[`sim.py`](example-driver/thermo/thermo/sim.py)). `--simulate name` and the tests use it. Make it behave like the real
device, timing included.

## 4. The Instance: running your device

`start(ctx, port, ident)` returns an `Instance` for one device found. It runs the device until `stop()`.

```python
from hub.api import Instance, unit_of

class ThermoInstance(Instance):
    def __init__(self, driver, ctx, port, ident):
        unit = unit_of(ident, port)       # its serial number, else the USB chip's, else the COM port
        n = ctx.unit_number()             # 0 for the first of this driver's devices, 1 for the second…
        slug = driver.key if n == 0 else f"{driver.key}-{n + 1}"
        super().__init__(driver, port, ident, slug, "Thermometer" + ("" if n == 0 else f" #{n + 1}"))
        ...
        self.local_port = self.srv.port   # your DeviceServer's port (section 6)
```

What `ctx` (a `HubContext`) gives you; it is your only way to the hub:

| | |
|---|---|
| `ctx.opener_for(port, baud)` | the opener your link uses: `opener(port.device)` returns the port (section 5) |
| `ctx.unit_number()` | this device's number among your driver's devices, handed out by the hub (use it for the slug and title) |
| `ctx.uid(unit, "sensor")` | an Alpaca UniqueID, the same on every run and every PC, for each Alpaca device of each unit |
| `ctx.settings_path(unit)` | your settings file for this unit: `%LOCALAPPDATA%\alpaca-hub\drivers\<key>\<unit>.json` |
| `ctx.public_urls()`, `ctx.is_local(ip)`, `ctx.simulated` | for your page and DeviceServer |
| `ctx.log` | a logger (`logging.getLogger(...)` works too; both go to `logs\<key>.log`) |

Your Instance also provides:

| | |
|---|---|
| `up()` | `True` while the device answers. The tab's dot and Overview use it. |
| `summary()` | `{"text": "15.0 °C", "state": "ok" | "warn" | "bad"}`: the short line on Overview. Keep it fast. |
| `stop()` | stop your threads and server and let go of the port. Called when the device goes away, when your driver is turned off, updated or uninstalled, and when the hub stops. |

The hub calls `up()` and `summary()` about every 2 s (its ping). If your process stops answering for three pings, the
hub ends it and starts it again.

## 5. The port

Your link never opens a serial port. `ctx.opener_for(port, baud)` gives an opener; calling it returns a
**`RelaySerial`**: the hub holds the real port and relays it. It behaves like a pyserial port opened with a short
timeout:

- `readline()`, `read(n)`, `read_until()` return what arrived within `timeout` (0.05 s by default; set
  `ser.timeout` if you want longer), else what there is, possibly `b""`.
- `write()`, `flush()`, `in_waiting`, `reset_input_buffer()`, `reset_output_buffer()`, `dtr`, `rts`, `baudrate`,
  `close()`, `is_open`.
- `close()` and calling the opener again **don't reopen the real port**: you get the same one back, so the device
  never sees a reopen (a link's usual "close and reopen on error" logic just works).
- If the real port fails (unplugged, a USB reset), a read or write raises (`serial.SerialException`). Close, wait a
  moment, call the opener again: the hub reopens the real port if it can.
- Writes are capped at about twice the line's speed; a runaway loop can't flood the device.

## 6. Alpaca devices

Serve your Alpaca devices with the hub's `DeviceServer` on 127.0.0.1; the hub forwards to it, gives each device its
public number (Focuser #0, #1 … across all drivers) and answers discovery.

```python
from hub.devserver import AlpacaDevice, AlpacaError, App, BadRequest, DeviceServer, NOT_CONNECTED, NOT_IMPLEMENTED

class TemperatureDevice(AlpacaDevice):
    type, type_name, interface_version = "observingconditions", "ObservingConditions", 2

    def get(self, member, p):     # member: lower case ("temperature"); p: parameters, lower-case names
        ...                       # return the value; raise KeyError for an unknown member
    def put(self, member, p):
        ...                       # do it; return None or a value

srv = DeviceServer([TemperatureDevice(...)], app, VERSION, public_urls=ctx.public_urls,
                   simulated=ctx.simulated, is_local=ctx.is_local)
srv.start()
```

`DeviceServer` answers the common members (`Connected`, `Name`, `Description`, `DriverInfo`, `DriverVersion`,
`InterfaceVersion`, `SupportedActions`) and the management API, checks the Alpaca rules (ClientID and
ClientTransactionID, case-sensitive PUT parameter names, lower-case device types) and turns errors into Alpaca answers:

- `raise AlpacaError(NOT_CONNECTED, "Not connected")` and the other ASCOM numbers (`hub/devserver.py`),
- `raise BadRequest("Missing Position")`: HTTP 400 for a missing or malformed parameter,
- `raise KeyError(member)`: an unknown member.

Optional overrides on your `AlpacaDevice`: `set_connected(on)` (raise to refuse, e.g. while the device doesn't
answer), `present()` (`False` hides an optional device), `actions` and `action(name, parameters)`. Check that the
client connected before acting (`self.connected`), as the example does.

**ConformU.** Before releasing, run ASCOM's ConformU against each device through the hub:
`conformu alpacaprotocol http://127.0.0.1:11111/api/v1/<type>/<n>` and `conformu conformance <same>`. For slow
devices (rotators, covers) give it a settings file with longer timeouts (`-s`). Its tests move and switch things:
don't run them with equipment that mustn't move or lose power.

## 7. The status page

Your folder's `ui\body.html`, `ui\device.js` and `ui\device.css` become one page, built by the hub with its UI kit
(`hub/ui/kit`: the styles, `K.tile`, `K.pill`, `K.chart`, `K.getJSON`, night mode…). Start from
`hub/ui/kit/skeleton-body.html` or the example. Give your `App` subclass `ui_dir`:

| Endpoint (relative: the page lives at `/<slug>/` behind the hub) | Your `App` |
|---|---|
| `status.json` | `status()` returns a dict; DeviceServer adds `build`, `uptime`, `simulated`, `client`, `local`, `urls` |
| `history.json` | set `self.history = History([...])` and `add()` samples (1 s for 10 min; 10 s averages with each value's lowest and highest, `<key>_min` / `<key>_max`, for 24 h; the hub writes 1-minute day files of them). Values that sit and jump (a position, a filter slot, a set level): `History([...], held=(...))` gives their last reading instead of an average and logs every change exactly (`?res=c`), drawn as steps |
| `POST control` | `control(form)` returns `(http code, message)`. The page must send the header `X-Hub-Page: 1`. While an Alpaca client is connected it is refused unless the page sends `override=1` (an Unlock button): changing things under a running imaging sequence should be deliberate |
| anything else | `route(req)` returns `(code, body, content type[, headers])` or `None` |

Settings that only make sense on the PC itself (`req.local` in `route`) can be refused from phones.

## 8. Rules

- **Never open a serial port yourself**, never list or scan ports: the hub does, by the rules in section 3.
- **Imports**: the standard library (the whole of it is in the hub's exe), `serial` (for its exceptions and constants
  only), `hub.api`, `hub.devserver`, `hub.core`, and your own package. Anything else, bring inside your folder. The
  contract tests check this.
- **Plain values across the pipe**: identities, `summary()`, anything returned by `start()` or the ping.
- **Threads**: daemon threads, stopped in `stop()`. Don't call `sys.exit()` or `os._exit()`; raise or log instead.
  A process that dies is restarted, but its device is gone meanwhile.
- **Devices that move until told to stop** (a mount's MoveAxis, a held jog): stop the motion in your `stop()`. If your
  process dies, nothing stops it.
- **Stock firmware first**: if a device has custom firmware variants, detect the version and use extra commands only
  when present.
- **Settings**: only in `ctx.settings_path(unit)`. Write them atomically (write a temporary file, then `os.replace`).

## 9. Testing

- **Your own tests**: `tests\test_<key>_*.py` in your folder. Test the protocol parsing, the link on the simulator,
  and your Alpaca devices and page on a DeviceServer. The example's
  [`test_thermo_driver.py`](example-driver/thermo/tests/test_thermo_driver.py) runs an Instance without a hub (a tiny
  stand-in for the hub that `HubContext` uses).
- **The contract tests** ([`tests/test_contract.py`](../tests/test_contract.py)) run on every folder in this
  repository's `drivers\` and on the example. They check:
  - `driver.json` against the package;
  - the imports;
  - the device is found from each of your simulators without a byte sent while listening;
  - `summary()` has the right shape, and the page builds;
  - after your process is killed, and after an update, your device comes back on the same tab with the same UniqueID
    without its port being reopened, and a connected client stays connected;
  - after your driver is turned off and on, the device comes back with the same UniqueID (turning it off releases
    the port, so that one does reopen it).

  To run them on your driver, put its folder in `drivers\` of a checkout and run
  `python -m unittest tests.test_contract`.
- On the real device: through the hub, with ConformU (section 6), and kill your driver's process once while it works
  (Task Manager: the `AlpacaHub.exe` whose command line names your driver). It should come back within seconds,
  with a connected client still connected.

## 10. Packaging, installing, updating

```
python packaging/build.py driver --folder D:\my-drivers\thermo
```

checks the folder as the hub will and builds `dist\AlpacaHub-thermo-1.0.0-Setup.exe` (Inno Setup 6 needed). The
installer:

- needs the Alpaca Hub installed (and a hub API at least the driver's), and says so otherwise;
- installs into `%ProgramData%\AlpacaHub\drivers\thermo\1.0.0\` and writes `driver.json` last;
- carries no Python: drivers run on the hub's own exe;
- adds "Alpaca Hub driver: Example thermometer" to Windows' installed apps. Uninstalling deletes `driver.json`
  first, waits until the hub has stopped the driver, then removes the rest.

**Updates**: raise `version` in `driver.json` (and `VERSION` in your package), build, install. The new version goes
into its own folder; the running hub switches to it within seconds if no Alpaca client is connected to your devices,
otherwise when they disconnect or when the user presses "Switch now" on Overview. Devices keep their tabs, Alpaca
numbers and UniqueIDs. The hub deletes the old folder once nothing uses it. Never change `key`; and keep reading
older settings files, since a unit's settings survive updates.

## 11. Checklist

- [ ] Folder name = `key` = `driver.json`'s key; `version` in `driver.json` = your package's `VERSION`; `api` 1.
- [ ] `usb_ids` lists your chip; listening if the device talks on its own, a harmless probe otherwise;
      `never_probe` if any byte can harm it.
- [ ] Identity has `"serial"` when the device has one; everything crossing the pipe is plain values.
- [ ] Slug and title from `ctx.unit_number()`; UniqueIDs from `ctx.uid()`; settings in `ctx.settings_path()`.
- [ ] No port opened, listed or scanned by your code; reopen through the opener.
- [ ] `up()`, `summary()` fast; `stop()` stops everything (and any motion).
- [ ] A simulator that behaves like the device; your tests; the contract tests pass.
- [ ] ConformU through the hub on the real device; one kill of your process while it works.
