# Plan: a fixed hub, drivers installed separately

Written 2026-11-22, decisions settled with the user the same day. Status: **steps 0–11 done** (branch
`refactor-drivers`). Step 0: nothing to carry over, and both originals are frozen (`SOURCES.md`). Step 1: the hub
imports nothing from `ncalpaca`. Step 2: one kit, `hub/ui/kit/` (v11); the `device-web-ui` skill says so. Step 3:
`hub/api.py`; drivers are found (every `hub/drivers` module with `DRIVERS`), not listed, and declare their
simulators; old settings files are copied to the new paths once, and the Alpaca numbers saved for the old UniqueIDs
are cleared once (`hub.json` `"api": 1`). Unit numbers (tab names) are handed out by the hub: the lowest free one.
Step 4: `hub/relay.py`; every link and probe gets a `RelaySerial`, the hub holds each port (`PortRelay`) from
detection until the device is dropped, caps writes and drops a probe's writes to a `never_probe` port. The two ends
only exchange plain-tuple messages (listed at the top of `hub/relay.py`), joined in-process by `LocalLink`; step 5
puts the control pipe there. Step 5: `hub/channel.py` (JSON over a named pipe, never pickle), `hub/driverhost.py`,
`hub/supervisor.py`; every driver module runs in its own process, the hub's process imports no driver code (a
test checks), simulators run in their drivers' processes. `--in-process` keeps the old way for debugging.
`on_lost` waits for the first driver that needs it (a mount). Step 6: `hub/catalog.py` finds installed versions
(`%ProgramData%\AlpacaHub\drivers\<key>\<version>\driver.json`), source folders (`drivers\<key>\`) and the
built-in `hub/drivers` modules; the supervisor syncs every 2 s (install, update with the client rule and "Switch
now", uninstall, old versions deleted), Overview has the Drivers list with on/off (`hub.json` `drivers_off`), and
`GET /hub/drivers` is what an uninstaller waits on. A client counts as connected from the Connected calls the hub
forwards, until it makes no call for 60 s. Step 7: the panel is `drivers/wcover/` (key kept as `wcover`, not
`wpanel`, so its UniqueID, settings and Alpaca number didn't change), with its own copy of the link and its tests;
`tests/test_contract.py` runs the contract tests on every driver folder. On the real panel: a client stays
connected across a driver restart (the hub connects again what it had connected), and a restart mid-move reads
Unknown until the move ends. Step 8: the NiteCrawler is `drivers/nitecrawler/` on `DeviceServer` (`devices.py`;
generic hooks added: refuse a connect, absent devices, Actions, ClientID checks, an App's own page endpoints), and
`ncalpaca/` is deleted. ConformU's protocol check then found `DeviceServer` too lenient (PUT names, upper-case
device type); fixed, and it passes for every device. Step 9: the box is `drivers/wbox/` on `DeviceServer`, and
`wbstock/` is deleted; a device in a driver process now reads "starting" until that driver's first ping. Next:
step 10, **postponed by the user (2026-11-22) until the Optec hardware is connected**; step 11 done
first: `packaging/` (hub.spec, hub.iss, driver.iss, build.py), `docs/USER-GUIDE.html`, `docs/WRITING-DRIVERS.md` and
the example driver `docs/example-driver/thermo` (in the contract tests); the hub got `--stop`, `--open`,
`--open-folder`, `--driver-folder` and `%ProgramData%\AlpacaHub\running.json`. The installers are built and the
frozen exe smoke-tested; installing them on a PC is the user's test (HANDOFF.md). Next: step 10 (with the Optec
hardware) or step 12. Step 10 then done too (user, 2026-11-22: move now, hardware check later): `drivers/optec`
(both Optec drivers, one process), `hub/drivers` empty, so the hub's exe carries no driver; Optec gets its own
installer. ConformU on the simulator: see HANDOFF.md. Still to do: Optec on real hardware, and step 12.

## Goal
The hub stays the same when a driver is added, updated or removed. The hub covers finding devices, owning the
ports, tabs, Overview, Alpaca forwarding and discovery, and the UI kit. It's updated only when the hub itself
changes. Each driver has **its own installer and uninstaller (a setup .exe)**. The hub notices a driver being
installed, updated or uninstalled while it runs, and needs no restart. **A driver that crashes or hangs can't take
the hub or the other devices down.** So far every driver stage (`38a3263`, `881d3cb`) has edited `host.py`,
`ports.py`, `shell.py` and `__main__.py`. Once this is done, the test is simple: a new driver changes no file under
`hub/`.

## Decisions (2026-11-22)
1. **One service.** Once the originals are frozen (step 0), the standalone programs (`python -m ncalpaca`,
   `python -m wbstock`) are dropped and the hub is the only way to run any device.
2. **One Alpaca server.** Every driver uses the hub's `DeviceServer`. `ncalpaca/alpaca.py` and `wbstock/alpaca.py`
   go. Order: **the panel first** (the pilot), then the NiteCrawler, then the box. The Optec drivers go last,
   because they haven't been used on hardware yet.
3. **One ID and settings rule, fixed now.** NINA isn't used with the hub yet, so nothing needs the old IDs.
   - `UniqueID = uuid5(NAMESPACE_URL, "alpaca-hub:<driver key>:<unit>:<device>")`. `<unit>` is the device's own
     serial number when it reports one (NiteCrawler, Optec), else the USB chip's serial number, else the COM port
     (box and panel: their CH340 has none).
   - Settings: `%LOCALAPPDATA%\alpaca-hub\drivers\<driver key>\<unit>.json`.
   - The "first box / NiteCrawler / panel keeps the standalone ID and settings" cases go.
4. **Each driver runs in its own process, and the hub relays its port.** No driver code runs in the hub's process.

## Overview
```
hub.exe                  owns every serial port, finds devices, tabs, Overview, Alpaca forwarding and discovery
 ├─ hub.exe --driver wpanel         one child process per installed driver (the same exe, so the same Python)
 ├─ hub.exe --driver nitecrawler    runs its devices' links and its DeviceServer on a private 127.0.0.1 port
 └─ hub.exe --driver wbox           talks to its device only through the hub's port relay
```
From source it's the same: `python -m hub` starts `python -m hub --driver <folder>` with its own interpreter.

### What stays in the hub
- **Ports and the rules for finding devices.** The hub opens every port, listens first, and only lets a probe
  through to a silent port whose USB IDs the driver claims. A driver never opens a port: it gets a relayed port from
  the hub. This is what protects the box, which stores every byte it receives.
- **Ports stay open** from detection for as long as the device is in use, including across a driver process
  restart. The device never sees a reopen.
- **One copy of the UI kit**, the page builder, `DeviceServer` (the library drivers use), Alpaca discovery, the
  management API, and the Python runtime with `pyserial`.

### Talking to a driver process
- **A control channel** (`multiprocessing.connection` over a named pipe, with a random key per process). The hub
  sends:
  - `hello` (config dir, public URLs, simulated)
  - `recognise(driver, bytes)`
  - `probe(driver, relay)`
  - `start(driver, port, relay, identity, unit number)`, which returns the instance's slug, title and local port
  - `summary(instance)`
  - `stop(instance)`
  - `ping`

  Every call has a time limit (`recognise`/`summary` 2 s, `probe`/`start` 10 s).
- **The port relay**: one channel per port. The driver side is `RelaySerial`, a pyserial subset:
  - `read`/`readline` with time limits, `write`, `in_waiting`, `reset_input_buffer`/`reset_output_buffer`,
    `dtr`/`rts`, `baudrate`, `close`, `is_open`
  - `opener_for` returns one, so the links keep their existing reopen logic

  The hub side reads the real port continuously into a capped buffer. Writes are capped too, so a runaway driver
  can't flood a device.
- **`never_probe` is enforced on the bytes.** During detection, the hub drops any write through a relay to a port
  whose USB IDs another driver lists in `never_probe`, and marks the driver that tried as refused.
- **HTTP is unchanged.** Each instance's `DeviceServer` listens on 127.0.0.1 inside its driver process, and the hub
  forwards to it as it does now.
- **Simulators run in the driver's process too.** In `--simulate`, the hub asks the driver to create its simulator.
  The hub sees it as a port whose bytes go to the driver process over the same relay.

### Supervision
- The hub starts a process for every enabled driver when it starts, and **pings each one every 2 s**. After 3 missed
  pings it ends the process (`TerminateProcess`).
- **When a driver process ends** (a crash, a kill, or a driver calling `exit()`):
  - The ports it had stay open, and the tabs show "driver restarting". Alpaca calls get an ASCOM "not connected"
    error.
  - The hub restarts the process, waiting 1, 2, 4… up to 60 s, and gives it the same ports. The UniqueIDs are the
    same, so NINA reconnects.
  - After 5 ends in 10 minutes the driver shows as **failed** on Overview, with its last log lines and a
    "Try again" button. Its ports stay held, and no other driver probes them.
- **Logging:** each driver process logs to its own file in `%LOCALAPPDATA%\alpaca-hub\logs\<key>.log`. Its last
  lines reach the hub log when it ends abnormally.
- **Second layer, in the hub:** every message from a driver is checked for size and shape, and a bad message counts
  as a driver failure, not a hub one. Since the hub imports no driver code, `sys.exit`, monkey-patching and memory
  leaks in a driver can't reach it.

### Devices that aren't a serial port, and devices that move
Every device so far is a USB serial port. Future drivers need three kinds of connection (`transport` in the
contract):

| Transport | Examples | Who opens it | How it's found |
|---|---|---|---|
| `serial` | NiteCrawler, box, panel, Optec; most mounts over USB (FTDI, CP210x, PL2303, CH340 or the mount's own USB serial) | the hub, relayed to the driver as above | the hub listens, then probes; all the rules above |
| `usb` | ZWO EAF focuser, EFW filter wheel, cameras: USB HID or vendor USB, opened only through the maker's SDK (a DLL) | the driver, through the maker's SDK | the hub watches USB IDs (ZWO: VID `03C3`) and tells the driver "one of yours is present, at this path". The driver matches it to an SDK device |
| `network` | mounts and focusers on Wi-Fi or Ethernet (OnStep, LX200 over TCP, maker Wi-Fi boxes) | the driver, to an address | the driver's own discovery, or an address the user enters on Overview ("Add a network device") |

- **The SDK is why `usb` drivers need their own process.** A maker's DLL that crashes or hangs now takes only its
  own driver process with it. Driver folders may carry such DLLs and compiled Python modules. A compiled module
  must be built for the hub's Python version, and the driver's `driver.json` says which one (`"python": "3.14"`).
  The hub refuses a mismatch.
- **`never_probe` can't be enforced for `usb` and `network`**: the hub doesn't see the bytes. It still holds them
  to their USB IDs or the address the user gave.
- **Devices that move: `on_lost`.** A focuser or a mount goto stops by itself at its target. But some moves only
  stop when told to: a mount's `MoveAxis`, a guide pulse, a focuser "move in" held from a page. A driver that dies
  during one of those leaves the device moving. So a `serial` driver may declare **`on_lost` bytes** (a mount's
  "stop all motion" command). While the driver says something is moving, the hub sends those bytes itself if the
  driver process ends or stops answering. A `usb` or `network` driver can't use this. Its process must stop motion
  in its own exit handling, and the contract tests kill it mid-move to check what the device does.
- **A mount driver** serves the Alpaca `Telescope` interface. That's much larger than a focuser: asynchronous
  slews, tracking rates, pulse guiding, parking, side of pier, and coordinate conversion (J2000 to the mount's
  epoch, which needs an astrometry library such as ERFA, carried in the driver's folder). The hub needs nothing new
  for it: forwarding already works for any device type and number.
- **Cameras** come later: see "Later" below.

## The driver contract (hub API version 1)
A driver is a folder with `driver.json` and a package:

```
<drivers folder>\wpanel\1.2.0\
  driver.json     {"key": "wpanel", "title": "WandererCover V4-EC", "version": "1.2.0", "api": 1}
  wpanel\         driver.py (exports DRIVERS), link/protocol code, sim.py, ui\ (body.html, device.js, device.css)
```

Each `Driver` declares (the hub gets these from the driver process's `hello` reply):

| Field | Meaning |
|---|---|
| `key`, `title`, `version`, `api` | as in `driver.json`. The hub refuses a driver whose `api` it doesn't support, and says why on Overview |
| `transport` | `serial`, `usb` or `network` (see "Devices that aren't a serial port") |
| `usb_ids`, `listen_baud`, `probe_baud`, `lines` | as now (`hub/drivers/base.py`); the baud rates only for `serial` |
| `on_lost` | `serial` only: bytes the hub sends if the driver process dies while it says something is moving |
| `probe_priority` | replaces the order of the hard-coded list (NiteCrawler before Optec on FTDI) |
| `never_probe` | a listening driver sets it for USB IDs where a byte can do harm (the box: CH340). Enforced by the relay |
| `simulators` | `{name: factory}`, used by `--simulate` and `--drivers` |
| `ui` | its page folder, with no `kit/`: the hub supplies it |

`start(ctx, port, ident)` gets a **`HubContext`** in the driver process. It has `opener_for` (returns a
`RelaySerial`), `settings_path(unit)`, `unit_number`, `uid(unit, device)`, `public_urls`, `simulated`, `is_local`
and `log`. Drivers use `hub.claim_primary`, `use_standalone_settings`, `count_instances` and `config_file` today. The
first two go with decision 3, and the other two become `unit_number` and `settings_path`. `Instance` stays as it is
(`up()`, `summary()`, `stop()`, `local_port`).

What a driver may use: the standard library, `serial` (only for its constants and exceptions, since the port is a
`RelaySerial`), `hub.api` / `hub.core`, and its own package. The hub's exe build includes the **whole standard
library**, since driver processes run on it. A driver that needs another library brings it inside its own folder.

## Where drivers live and how the hub finds them
- **Installed drivers:** `%ProgramData%\AlpacaHub\drivers\<key>\<version>\`, outside the hub's own folder, so a hub
  update or reinstall leaves them alone. Each version goes in its own folder, so an update never overwrites files a
  running driver process has open.
- **From source:** the repository's `drivers\<key>\` folders are read too (no version folder), so development
  needs no installer.
- **Finding changes:** the hub checks the drivers folder every 2 s and uses the highest version whose `driver.json`
  is present. A driver installer writes `driver.json` **last**, so the hub never starts half a driver.
  - **Install:** a new driver starts its process and its ports go into the next scan.
  - **Update:** a higher version appears. If no client is connected to that driver's devices, the hub stops the old
    process and starts the new one on the same held ports (a few seconds, same tabs and UniqueIDs). If a client is
    connected, Overview shows "update ready" and the switch happens when the client disconnects or the user presses
    "Switch now". The hub deletes an old version's folder once nothing uses it.
  - **Uninstall:** the uninstaller deletes `driver.json` first, waits up to 10 s for the hub's `/hub/drivers` page
    to show the driver stopped, then deletes the rest. The ports are released and go back to scanning.
- **Overview's "Drivers" list:** each driver's version and API, its state (running / restarting / failed / off /
  refused / update ready, and why), restarts in the last hour, and an on/off switch saved in the hub settings.

## Installers (Inno Setup, as wbstock's `packaging/installer.iss` already does)
- **The hub:** a PyInstaller one-folder build with the full standard library, in Program Files.
  - Autostart at logon and a firewall rule for private networks, as wbstock's installer does.
  - No drivers inside.
  - Its own AppId and uninstall entry.
- **Each driver:** `AlpacaHub-<Driver>-<version>-Setup.exe`, built from one template by `packaging/build.py
  <key>`, which reads `driver.json`.
  - Its own AppId and uninstall entry ("Alpaca Hub driver: WandererCover V4-EC").
  - It checks that the hub is installed. If the hub is too old for the driver's `api`, it says which hub version is
    needed.
  - It installs into a new version folder, and no files are locked.
  - Driver processes run on the hub's exe, so a driver installer carries no Python and stays small.
- **Uninstalling the hub** leaves the drivers installed, and their uninstall entries keep working.

## Layout after the refactor
```
hub/                    never device-specific, never imports a driver
  api.py                Driver, Instance, HubContext, API_VERSION (what driver processes import)
  driverhost.py         the --driver side: loads one driver folder, answers the control channel
  supervisor.py         starts, pings, restarts and stops driver processes; finds installs and updates
  relay.py              the hub's side of the port relay, and RelaySerial
  core/net.py           HTTP server helpers, own addresses, discovery (out of ncalpaca/alpaca.py)
  core/devserver.py     today's hub/devserver.py
  core/page.py          one page builder (ncalpaca/webpage.py and wbstock/webpage.py are the same file)
  ui/kit/               the only kit copy (today: 5 copies, all v11)
  ui/overview/, shell.py, host.py, ports.py
drivers/                one folder per installer
  wpanel/  wbox/  nitecrawler/  optec/       each: driver.json, package, sim, ui, tests
packaging/              hub.spec, hub.iss, driver.iss (template), build.py
tests/                  hub tests with a fake driver; contract tests run against every driver folder
```
The box and the panel become separate drivers, each with its own installer. The panel package takes its serial link
from `wbstock/cover.py`. `optec/` becomes one driver with two `Driver`s (FocusLynx, Gemini).

## Steps
Each step leaves the devices working as before, ends with `python -m unittest discover -s tests` passing and a page
check with `hub-sim`, and is one commit. Steps run in order, in one session (they touch the same files).

0. **Freeze the originals.** Here: compare `WR35/nc-alpaca` and `wanderer/stock-alpaca` with the commits in
   `SOURCES.md` (reading only) and carry anything newer over. Then, in sessions in those two repositories, a short
   "frozen, see alpaca-hub" note in each. This comes first because later steps move and then delete the copies.
1. **Hub core out of `ncalpaca`.** `hub/core/net.py` and `hub/core/page.py`; the hub stops importing `ncalpaca`.
2. **One UI kit**, in `hub/ui/kit/` (the `device-web-ui` skill syncs to it). Check every page in the browser.
3. **API v1 and the ID rule.** `hub/api.py`, `HubContext`, `probe_priority`, `never_probe`, `simulators`. The
   current drivers move onto it where they are, still in the hub's process, with the new IDs and settings paths.
   `all_drivers()`, `simulated_ports()` and `DRIVER_NAMES` go.
4. **Port relay.** `hub/relay.py`: `RelaySerial` and the hub side, with `never_probe` enforced on writes. Tests:
   every driver's link runs on its simulator through a relay (timing, reopen, DTR/RTS, input clearing), not only
   on a direct port.
5. **Driver processes.** `hub/driverhost.py` and `hub/supervisor.py`: the control channel, start, ping, restart
   with back-off, failed state, held ports across restarts, simulators in the driver process. Every current driver
   now runs in its own process. Tests: a fake driver that crashes, hangs, calls `exit()`, floods writes and sends
   bad messages. The hub and the other drivers keep running each time.
6. **Install detection and the Drivers list.** Version folders, `driver.json` last/first, update when no client is
   connected or on "Switch now", on/off. Test it by copying folders in and out while the hub runs.
7. **Pilot: the panel.** It becomes `drivers/wcover/` (planned as `wpanel`), already on `DeviceServer`, with its link code out of
   `wbstock`. Check it on the real panel, including killing its process mid-move.
8. **NiteCrawler:** onto `DeviceServer`, as `drivers/nitecrawler/`. Delete `ncalpaca/` and its standalone program.
   Check it on the real NiteCrawler and with ConformU.
9. **Box:** onto `DeviceServer`, as `drivers/wbox/`. Delete `wbstock/` and its standalone program. Check it on the
   real box (see Hardware) and with ConformU. Its dew target's minimum is −20, so ConformU always raises two issues
   that aren't real faults (CLAUDE.md "Checks").
10. **Optec:** `drivers/optec/`, moved only (it's already on `DeviceServer`).
11. **Installers:** `packaging/` for the hub and the driver template. On this PC: install the hub, then install,
    update and uninstall the panel driver while the hub runs.
12. **Proof:** a dummy `serial` driver (a simulated thermometer) is built and installed with its own setup .exe without
    touching `hub/`. It appears, turns off and on, updates, is killed and comes back, uninstalls, and is refused
    when its `api` is wrong. Then it's deleted.

## Later (kept in the plan, built with the first driver that needs it)
- **`usb` transport**, with the first ZWO EAF driver: USB ID watching, "present at this path", the maker's library
  in the driver's folder, and the `"python"` check for compiled modules.
- **`network` transport**, with the first Wi-Fi mount or focuser: the driver's own discovery, and "Add a network
  device" on Overview.
- **Cameras** (after the `usb` transport, since ZWO ASI cameras use ZWO's library too):
  - **Hub, once:** `_forward` (`hub/host.py`) streams responses through in pieces instead of holding a whole one.
    `ImageArray` for a large sensor is tens of MB per frame. The hub also passes on `ImageBytes` (the Alpaca binary
    image format, `Accept: application/imagebytes`) unchanged, since NINA asks for it and it's far smaller than
    JSON.
  - **`DeviceServer`:** support for `ImageBytes` and a streamed `ImageArray`, so a driver hands over a frame
    without the server making copies of it.
  - **A driver that dies:** an exposure in progress is lost. The client sees an error and takes the frame again.
    The process's exit handling aborts the exposure through the library. The cooler needs care: a sensor cooled
    far below ambient and switched off at once can mist up, so the driver warms it up in steps on a normal stop.
    After a crash the camera's own firmware decides what happens; check that per camera model.
  - **Memory:** frames live only in the camera's driver process; the hub only passes them through.
  - **Contract tests for cameras:** a simulated camera through the hub with a full-size frame, in both
    `ImageArray` and `ImageBytes`, with the hub's own memory checked to stay flat.

After step 10, update `CLAUDE.md`'s "Where work goes" table, `SOURCES.md`, `README.md`, `.claude/launch.json`, the
paths `tools/firmware_watch.py` names on the hub side, and the protocol files CLAUDE.md "After firmware work" points
to.

## Contract tests (every driver folder, every run)
- `driver.json` matches what its process says in `hello`; it only imports what's allowed.
- It's found from each of its simulators, through the relay, with nothing sent while listening.
- A probing driver never gets a byte to a port whose USB IDs are in another driver's `never_probe`.
- `summary()` has the right shape and answers within its time limit; `stop()` releases the port; its page builds
  with the hub's kit.
- Killing its process mid-run, turning it off and on, and updating it all bring its device back on the same tab
  with the same UniqueID, without the port being reopened.

## Hardware
None of this should change anything sent to a device. Still, the relay sits between every driver and its device,
so at steps 7–9 each device is run on the real hardware once, and its process is killed once mid-run. The box runs
on scratch settings with the dew state adopted (CLAUDE.md "After firmware work", point 4), unless the user decides
otherwise.
