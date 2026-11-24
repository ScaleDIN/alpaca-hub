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
- **`usb` transport**, with the first ZWO driver (`zwoeaf`, see "Focuser and filter wheel"): USB ID watching, "present at this path", the maker's library
  in the driver's folder, and the `"python"` check for compiled modules.
- **`network` transport**, with the first Wi-Fi mount or focuser: the driver's own discovery, and "Add a network
  device" on Overview. The first ones planned are the ZWO AM and iOptron mounts (below), both of which also have
  Wi-Fi.
- **Mounts** (Alpaca `Telescope`), planned 2026-11-24. See "Mounts" below.
- **Cameras** (Alpaca `Camera`), planned 2026-11-24, after the `usb` transport. See "Cameras" below.
- **ZWO EAF and EFW** (Alpaca `Focuser`, `FilterWheel`), planned 2026-11-24, with the `usb` transport and before
  the cameras. See "Focuser and filter wheel" below.
- **Digital cameras** (DSLR and mirrorless, Alpaca `Camera`), a possibility after the astro cameras. See "Digital
  cameras" below.

After step 10, update `CLAUDE.md`'s "Where work goes" table, `SOURCES.md`, `README.md`, `.claude/launch.json`, the
paths `tools/firmware_watch.py` names on the hub side, and the protocol files CLAUDE.md "After firmware work" points
to.

## Mounts
Mount drivers are built only from command sets the maker has published, and the driver folder names the document
and its version. Undocumented commands, community lists of "hidden" commands, firmware images and captured traffic
are not used. Other projects' drivers (INDI, INDIGO, EQMOD) are under other licences: they may be read to check
our reading of a maker's document, but nothing is copied from them. If a mount's maker publishes nothing, the
fallback is a bridge to the maker's own ASCOM driver (`ascom` below), never a protocol worked out by us.

### First two drivers
| Driver | Mounts | Source | Transports |
|---|---|---|---|
| `zwoam` | ZWO AM3 (untested, see below), AM5, AM7 (one protocol; the model is read at connect) | ZWO's "ZWO Mount Serial Communication Protocol", the newest version ZWO issues (taken from ZWO, not from a copy in another project) | USB serial; Wi-Fi (TCP, port 4030 by default on the mount's own network) |
| `ioptron` | iOptron mounts on the "RS-232 Command Language" v3.x, starting with the HAE series | iOptron's "RS-232 Command Language" (v3.10, 2021) and its "Special RS-232 Language", both on ioptron.com | USB serial; Wi-Fi |

Both speak LX200-style framing (`:` … `#`), but the command sets differ. Each driver folder carries its own copy of
a small `lx200.py` (framing, `#`-terminated and single-character replies, timeouts, resync after junk), as every
driver folder carries its own link today. A later OnStep / Meade driver copies it as well.

### What each driver does (first version)
- **Read:** RA/Dec, Alt/Az, sidereal time, side of pier, tracking state and rate, slewing, parked, at home, the
  model, the firmware version and the alignment mode (EQ or Alt-Az, as the mount is set up).
- **Move:** `SlewToCoordinatesAsync` / `SlewToTargetAsync`, `SyncToCoordinates`, `AbortSlew`, `MoveAxis`, tracking on
  or off with sidereal, lunar and solar rates, `Park` / `Unpark`, and `FindHome` where the document has it.
  `PulseGuide` only on firmware where it's checked to work. Otherwise `CanPulseGuide` is false, and guiding goes
  through the guide port.
- **Coordinates:** report the mount's own `EquatorialSystem` (JNow / topocentric for LX200-style mounts, once the
  document confirms it), so the client converts. An astrometry library (ERFA) is added only if a client needs J2000
  from the driver.
- **Site and time:** sent only when a client sets `SiteLatitude`, `SiteLongitude`, `SiteElevation` or `UTCDate`. The
  driver never sets them on its own.
- **Never sent, as with the Optec configuration commands:** altitude and meridian limits, the park and home positions,
  EQ / Alt-Az mode, guide rate defaults, Wi-Fi settings, zero-position settings and firmware commands. Those stay
  with the maker's app or hand controller.
- **Versions:** the firmware version is read at connect. A command is used only on firmware the driver knows has it.
  Problems seen on hardware are tied to firmware versions in the simulator and in tests (as with the Gemini's late
  `END`).

### Finding the mount
The mounts' USB IDs aren't known yet, so they are read from the real mounts first.
- **Own USB IDs** that no other driver claims: normal detection. Listen first, then one read-only query (the model
  or firmware question from the document). The query is never a move.
- **A shared USB-serial chip** (CH340, which is in the box's `never_probe`; FTDI 0403:6001, which the NiteCrawler and
  Optec drivers claim; CP210x): no probing. The user adds the mount by hand on Overview with "Add a device"
  (driver + COM port), and the hub then lets that driver probe that one port only. This is a new hub feature, built
  with the first mount that needs it.
- **Wi-Fi:** "Add a network device" (driver + address) on Overview, with the `network` transport. Bluetooth is never
  used: the hub doesn't open Bluetooth COM ports.

### Safety
- **`on_lost`:** while `MoveAxis`, a pulse guide or a slew is running, the stop-all-motion command from the document
  (`:Q#` for ZWO. iOptron's document has separate stop commands for a slew and for a manual move, so the driver sends
  every one that applies; check each against the document). For Wi-Fi, the driver process stops motion in its own exit handling, and
  the contract test kills it mid-`MoveAxis`.
- **The page:** status, tracking on and off, Park, and a large **Stop** button in the first version. Manual control
  (hold-to-move, with a dead-man) follows once the first version has run on the mounts: see "The handset". Never
  goto from the page.
- **On hardware:** reading only at first. Then tracking. Then a short slew and a `MoveAxis` with someone at the mount,
  clear travel all round, and Stop within reach. Then the kill mid-`MoveAxis`. ConformU's Telescope test slews widely
  and parks: run it only after those, and with the user's OK.

### The handset (ZWO AM)
The AM mount's handset talks only to the mount (the user, 2026-11-24), so it is never a device of the hub's: there
is no driver for it and nothing to detect. It matters to the hub in two ways.
- **Living alongside it (first version).** Someone can move, stop, park or switch off tracking from the handset
  while a client is connected. The driver polls the mount, so its state always follows: `Slewing` while the mount is
  moved by hand, `Tracking`, `AtPark` and the position as the mount reports them. It never fights the handset: no
  repeating a command because the state changed, no resuming tracking on its own. If a client's slew is cut short by
  a handset move or stop, the driver reports what the mount says, and the client sees the slew end. The page shows
  "moved from the handset" when the mount moves without a command from the driver.
- **Manual control on the page (planned, the user 2026-11-24).** Four direction buttons held down to move
  (`MoveAxis` at the chosen rate; both axes at once allowed), a rate choice (the rates ZWO's document gives, from
  guide rate to the fastest), tracking on and off, Park, and a large Stop. For framing or a quick check from a phone
  at the rig. Rules:
  - **Dead-man:** while a button is held the page sends a keep-alive several times a second; the driver stops that
    axis when one is missed (about half a second), the button is released, the page is hidden or closed, or the
    connection drops. The stop is the driver's own, on a timer in the driver process, not something the page has to
    send.
  - **Who has the mount:** while a client (NINA) is connected the controls are shown but need an "I'm at the mount"
    switch first, and a client's slew or `MoveAxis` cancels a page move (and the other way round), as the handset's
    own moves would. Every page move is logged with the address it came from.
  - **Limits:** the mount's own limits apply; the page never moves the mount while it is parked (Unpark first) and
    never sends goto.
  - Built after the driver's first version has run on the mounts (reading, tracking, a short slew and `MoveAxis`,
    the mid-move kill), then checked on the mount with someone beside it and the handset's stop in reach.
- **Step 1 reads** whether ZWO's document says anything about which side wins when the handset and the PC both send.

### ZWO AM3 (supported, not tested on hardware)
The AM3 uses the same ZWO protocol as the AM5 and AM7, so `zwoam` serves it, but the user has no AM3 to check it on.
- **Built from the document only:** whatever ZWO's document says differs for the AM3 (its model reply, rates, limits,
  anything marked per model) goes into the driver and into an AM3 model in the simulator, with tests. Nothing about
  the AM3 is assumed from the AM5 beyond what the document says is shared.
- **Shown as untested:** the driver accepts an AM3 by its model reply, and the page and Overview say "AM3: not yet
  checked on a real mount" next to it. `driver.json`'s notes and the release notes say the same.
- **Nothing extra is risked:** the AM3 gets exactly the commands checked on the AM5 and AM7. A command that needed a
  firmware check on those (`PulseGuide`, say) stays off on an AM3 until it is checked on one.
- **Getting it checked:** someone with an AM3 runs the same order as under "Safety" (read-only first) and reports
  back. Their model and firmware replies go into the simulator, and the "untested" note comes off for the firmware
  they checked.

### iOptron HAE18C (`ioptron`)
The user's iOptron mount. The `ioptron` driver is built for it first, from iOptron's own documents only: the
"RS-232 Command Language" v3.x (the newest iOptron issues; v3.10 of 2021 is the one known). Not the "Special RS-232
Language": iOptron's own text says Alt-Az HAE mounts don't support it. Every command named below is checked against
the document before use; anything the document doesn't give for this model isn't used.
- **Model and firmware at connect:** the document's mount-info and firmware queries. The driver refuses a model code
  it doesn't know instead of guessing, and the simulator carries this model's replies as read in step 1.
- **EQ or Alt-Az:** the HAE mounts can be set up either way. The mode is set with iOptron's own tools and only read
  here (it is in "Never sent"). `AlignmentMode` reports it, and what follows from it:
  - **EQ:** as the general plan (RA/Dec slews, `SideOfPier`, the mount's own meridian behaviour, never set).
  - **Alt-Az:** `SideOfPier` not implemented; `CanSlewAltAz` only if the document gives Alt-Az slews for this
    model. Tracking still follows RA/Dec (the mount does it). The client knows the frame rotates; the driver doesn't
    try to correct it.
- **Status:** the document's single status query (tracking, slewing, parked, at zero, rate, time source) polled once a
  second, as the box's status line is; positions from its position query.
- **Zero position and park:** going to the zero position is `FindHome` where the document has it. Setting the zero
  position or the park position is never sent.
- **Guiding:** `PulseGuide` through the document's timed guide commands, only once checked on this mount in each
  mode it is used in; until then `CanPulseGuide` false and guiding goes through the guide port.
- **Stop:** the document's stop commands for a slew and for a manual move, both sent on Stop, `on_lost` and exit
  (see "Safety").
- **Its handset** talks directly to the mount (the user, 2026-11-24), as the AM's does: the PC's USB goes to the mount,
  and the driver follows what the handset does and never fights it ("The handset").
- **Manual control on the page:** the same as for the AM ("The handset"), with the rates the document gives.
- **Step 1 also reads:** the USB chip and IDs, the Wi-Fi address and port, the model code, both firmware versions,
  and the mode it is set up in.

**iOptron models with USB through the handset (the HAE43 without the C)** (the user, 2026-11-24; the CEM40 has its
own USB port on the mount, like the HAE18C). On these the PC's USB port is on the handset, which passes commands on
to the mount. The same `ioptron` driver can serve them later, but not before one has been checked on the real mount,
because of what changes:
- The port found is the handset's: its USB chip and IDs are the handset's, not the mount's, and may be a shared chip
  (then "Add a device" by hand, see "Finding the mount").
- The handset must be plugged in and powered, and unplugging it is the mount lost (`on_lost` can't reach the mount:
  the mount then does whatever it does when its handset goes).
- The handset may answer some commands itself, or be slower to pass replies on (longer timeouts), and its firmware
  is part of what the driver checks (the document's firmware query gives the mainboard's and the handset's).
- Someone using the handset's menus while the PC sends may get in the way; the driver reports what the mount says
  and never retries a move on its own.
Not planned until someone has such a mount to check it on.

### Steps
1. **Facts from the mounts, read-only (the user's call):** USB VID:PID and chip, COM port, the model and firmware
   replies, the Wi-Fi address and port. Get the newest protocol document from each maker, and check it matches that
   firmware.
2. **`zwoam`:** `lx200.py`, `protocol.py` and `sim.py` from ZWO's document (moving axes, tracking rates, slews,
   park, the firmware version as a parameter), plus tests. Then `driver.py` with a `Telescope` device on
   `DeviceServer`, its page, and the contract tests.
3. **`zwoam` on the mounts over USB**, in the order under "Safety". Then ConformU.
4. **`network` transport** in the hub ("Add a network device"), then `zwoam` over Wi-Fi.
5. **`ioptron`:** the same steps for the HAE18C, from iOptron's documents ("iOptron HAE18C"), in the mode it is
   set up in; the other mode only if the user uses it.
6. **"Add a device" by hand for serial**, if step 1 shows a shared chip.

### `ascom` (fallback, not planned yet)
A bridge driver that loads a maker's installed ASCOM driver by its ProgID and serves it over Alpaca, for a device
whose protocol isn't published. It needs the ASCOM Platform on that PC. That device's COM port is opened by the
maker's driver, so the hub must not claim, listen on or probe it. Built only if a device needs it.
## Cameras
Planned 2026-11-24. Alpaca `Camera` (ICameraV4). The first driver is `zwoasi` for ZWO ASI cameras, through ZWO's
own published camera SDK (`ASICamera2.dll` and its header `ASICamera2.h`). The driver calls the DLL directly with
`ctypes` from that header; no third-party wrapper. The same rules as for mounts: only what ZWO publishes (the SDK and
its documentation), nothing worked out from traffic or firmware. Then `qhy` and `playerone` for the user's QHY and
Player One guide cameras, each through its maker's own published SDK (QHYCCD's `qhyccd.dll`, Player One's
`PlayerOneCamera.dll`) on the same pattern. Other makers that publish an SDK (ToupTek and its brands) can follow
later; until then the `ascom` bridge.

**The cameras it is built and tested on:** ASI2600MC Pro, ASI2600MC Duo and ASI2600MM Pro (one sensor family,
about 26 MP; colour, colour with a built-in guide sensor, and mono). So the first version needs a full-size frame,
cooling, the anti-dew heater, Bayer and mono, and the Duo's guide sensor (it shows up as a separate device, user
2026-11-24). The simulator copies these three models'
properties and control caps as read from them in step 1, not from data sheets.

### Who controls what
A camera is used by one imaging program at a time (NINA, SharpCap, APT…). Everything that shapes a frame belongs to
that client; the hub's page doesn't compete with it.
- **The client (Alpaca):** exposures, gain, offset, binning, the frame area, readout mode, the cooler and its target.
- **The page:** shows all of it. While no client is connected it can also switch the cooler, set the target, and
  start **Warm up**. While one is connected the cooler controls are shown but locked, apart from **Warm up** and
  **Cooler off** (someone at the rig packing up). No exposures from the page in the first version.
- **The driver's own settings** (device level, kept per camera like the box's dew settings, set on the page): the
  anti-dew heater, the fan where the model has one, USB bandwidth, and the warm-up rate. These have no Alpaca
  property, so the driver applies them itself at connect, and a client can also reach them through `Action`
  (`SupportedActions` lists them).
- **Never sent:** `ASISetID` (writes an ID into the camera), anything that writes the camera's flash, and firmware
  updates. Those stay with ZWO's ASIStudio.

### Alpaca → ZWO SDK
| Alpaca | ZWO SDK | Notes |
|---|---|---|
| `Connected` | `ASIOpenCamera`, `ASIInitCamera` / `ASICloseCamera` | Fails cleanly when another program has the camera open ("in use by another program" on the page). |
| `CameraXSize`, `CameraYSize`, `PixelSizeX/Y`, `SensorType`, `BayerOffsetX/Y`, `SensorName` | `ASIGetCameraProperty` (`MaxWidth`, `MaxHeight`, `PixelSize`, `IsColorCam`, `BayerPattern`, `Name`) | Colour models: `SensorType` 2 (RGGB etc.), the offsets from `BayerPattern`. Mono: 0. |
| `MaxADU`, `ElectronsPerADU` | `BitDepth`, `ElecPerADU` | Frames are always taken as RAW16, so `MaxADU` is 65535 (the SDK scales the sensor's own bit depth up). |
| `FullWellCapacity` | — | Not in the SDK. Left as not implemented rather than guessed. |
| `StartExposure(Duration, Light)` | `ASISetControlValue(ASI_EXPOSURE, µs)`, `ASISetROIFormat`, `ASISetStartPos`, `ASIStartExposure(isDark = not Light)` | `isDark` only matters on models with a shutter. |
| `ImageReady`, `CameraState`, `PercentCompleted` | `ASIGetExpStatus` (`IDLE`, `WORKING`, `SUCCESS`, `FAILED`) | Driver states: Idle → Exposing → Reading (`ASIGetDataAfterExp`) → Idle with a frame, or Error. `PercentCompleted` from the clock. |
| `AbortExposure`, `StopExposure` | `ASIStopExposure` | `CanAbortExposure` true. `CanStopExposure` false: the SDK can't keep a partial frame. |
| `ExposureMin`, `ExposureMax`, `ExposureResolution` | `ASIGetControlCaps(ASI_EXPOSURE)` | µs in the SDK, seconds in Alpaca. |
| `LastExposureStartTime`, `LastExposureDuration` | — | Kept by the driver (UTC, ISO 8601). |
| `Gain`, `GainMin`, `GainMax` | `ASI_GAIN` caps and value | Numeric gain mode; `Gains` not implemented. |
| `Offset`, `OffsetMin`, `OffsetMax` | `ASI_OFFSET` (`ASI_BRIGHTNESS` in older headers) | Numeric offset mode; `Offsets` not implemented. |
| `BinX`, `BinY`, `MaxBinX/Y`, `CanAsymmetricBin` | `SupportedBins`, `ASISetROIFormat(bin)` | Symmetric only. `ASI_HARDWARE_BIN` and `ASI_MONO_BIN` stay at the camera's default. |
| `StartX`, `StartY`, `NumX`, `NumY` | `ASISetROIFormat`, `ASISetStartPos` | Binned pixels in both. The SDK wants the width a multiple of 8 and the height of 2 (stricter on some USB 2 models): the driver checks a client's area and returns `InvalidValue` rather than changing it quietly. |
| `ReadoutModes`, `ReadoutMode`, `CanFastReadout`, `FastReadout` | `ASI_HIGH_SPEED_MODE` | "Normal" and "High speed" where the model has it; `FastReadout` is the same control. |
| `CanSetCCDTemperature`, `SetCCDTemperature`, `CoolerOn`, `CCDTemperature`, `CoolerPower`, `CanGetCoolerPower` | `IsCoolerCam`, `ASI_TARGET_TEMP`, `ASI_COOLER_ON`, `ASI_TEMPERATURE` (tenths of °C), `ASI_COOLER_POWER_PERC` | Uncooled models still report `CCDTemperature` but can't set it. |
| `HeatSinkTemperature`, `SubExposureDuration` | — | Not in the SDK: not implemented. |
| `CanPulseGuide`, `PulseGuide`, `IsPulseGuiding` | `ST4Port`, `ASIPulseGuideOn` / `ASIPulseGuideOff` | Only on models with an ST-4 port. The driver times the pulse; the off call is always sent, also on abort and at exit. |
| `HasShutter` | `MechanicalShutter` | |
| `ImageArray`, `ImageBytes` | `ASIGetDataAfterExp` (RAW16) | Rank 2, `[x][y]` as Alpaca wants (the SDK's buffer is row by row: transposed once in the driver). |
| `Action`: anti-dew heater, fan, USB bandwidth | `ASI_ANTI_DEW_HEATER`, `ASI_FAN_ON`, `ASI_BANDWIDTHOVERLOAD` | Only those the model's `ASIGetControlCaps` lists. |

Controls for live video (white balance `ASI_WB_R/B`, gamma, `ASI_FLIP`, the RGB24 and Y8 formats) aren't used: an
imaging client gets the raw Bayer frame.

### Finding and naming the camera
- **The `usb` transport** (see "Later"): the hub watches for ZWO's USB vendor ID (03C3) and starts `zwoasi` when one
  appears. The hub never opens the device: the driver asks the SDK for its cameras (`ASIGetNumOfConnectedCameras`,
  `ASIGetCameraProperty`). ZWO's EAF focuser and EFW filter wheel share the vendor ID but use their own SDKs, so the
  driver takes only what the camera SDK lists; `zwoeaf` / `zwoefw` take theirs ("Focuser and filter wheel").
- **UniqueID:** model + `ASIGetSerialNumber` where the camera has a serial (USB 3 models). An older camera without
  one gets model + the camera ID the SDK reports, so two identical old cameras can swap settings when replugged, as
  two stock boxes can. The driver never writes an ID to tell them apart (`ASISetID` is in "Never sent").
- **Two cameras** (main and guide) appear as two Alpaca cameras from one driver process, each with its own tab.
- **The 2600MC Duo:** its built-in guide sensor shows up as a separate device (the user, 2026-11-24), so it gets its
  own tab and UniqueID. Step 1 reads what the SDK calls it, its serial (its own, or the Duo's), and whether it has a
  guide port of its own.

### Guide cameras (`qhy`, `playerone`, and the Duo's guide sensor)
A guide camera is the same Alpaca `Camera`, used differently: short exposures (a fraction of a second to a few
seconds) one after another, small frames, and no cooler. What matters:
- **Latency:** the time from `ImageReady` to the frame at the guider is part of every guide cycle. Small frames go
  through the hub as `ImageBytes` at once; the contract tests time a guide-size frame through the hub as well.
- **`PulseGuide` through the camera's ST-4 port** where it has one (the SDK's guide calls: `ASIPulseGuideOn/Off`,
  QHY's and Player One's own). Guiding through the mount instead is the guider's choice, not the driver's.
- **Each maker's SDK** is called with `ctypes` from its published header, as for ZWO. The mapping table above is
  repeated per driver from its own SDK documentation, with what each doesn't have marked "not implemented".
- **QHY:** some older QHY models get their firmware loaded by QHY's own Windows driver when plugged in. That stays
  with QHY's driver install; ours never sends firmware. The SDK is set up once per process (`InitQHYCCDResource`).
- **Finding them:** each driver claims its maker's USB vendor ID (read from the real cameras in step 1) and lists
  cameras only through its own SDK, as `zwoasi` does.

### Frames through the hub (hub work, once, with the first camera)
- `_forward` (`hub/host.py`) streams a response through in pieces instead of reading it whole. A 26-megapixel frame
  is about 52 MB as `ImageBytes` and several times that as a JSON `ImageArray`. Its 60 s timeout becomes a per-read
  timeout, so a slow network doesn't cut a long download.
- The hub passes `ImageBytes` (`Accept: application/imagebytes`) through unchanged; NINA asks for it.
- **`DeviceServer`:** `ImageBytes` (the 44-byte header, then the data, sent as UInt16 when every value fits) and a
  JSON `ImageArray` written in pieces, so a frame isn't copied more than once. Requests run on their own threads, so
  `ImageReady` and `CCDTemperature` still answer during a download.
- Frames live only in the driver process: the last frame is kept until the next exposure starts, then dropped.

### Safety
- **Cooler.** A sensor far below ambient switched off at once can mist up or ice. **Warm up** raises the target in
  steps (the warm-up rate, default a few °C a minute) to about ambient, then switches the cooler off. The driver runs
  it on a normal stop (the hub stopping, the driver turned off or updated) while the cooler is on, before it closes
  the camera; the hub waits for it, with a time limit, and the page shows its progress. A client setting `Connected`
  false doesn't touch the cooler (clients do their own warm-up). After a crash the camera does whatever its firmware
  does when the SDK lets go: check per model, read-only, in step 1.
- **Exit handling:** `ASIStopExposure`, `ASIPulseGuideOff` on every direction, the warm-up above, then
  `ASICloseCamera`. A killed process loses the exposure in progress; the client sees an error and takes it again.
- **`on_lost`:** while a pulse guide runs, its off call. Nothing else in a camera moves.

### Steps
1. **Facts from the cameras, read-only (the user's call):** for the 2600MC Pro, 2600MC Duo (both of its devices) and
   2600MM Pro, and the QHY and Player One guide cameras: models, USB IDs and serials, and
   `ASIGetCameraProperty` / `ASIGetControlCaps` for each (a small script with the SDK: no exposure, no cooler). Get
   the newest SDK from ZWO and read its licence: whether `ASICamera2.dll` may ship in our installer, or the
   installer must use the copy ZWO's own driver installs. The same for QHY's and Player One's SDKs and licences.
2. **`zwoasi`:** `asi.py` (the `ctypes` calls, from the header), `sim.py` (a simulated camera with a real model's
   property and control caps from step 1, exposures in real time, a test pattern with noise, a cooler that follows
   its target at a set rate), `driver.py` with the `Camera` device, its page, and tests.
3. **Hub and `DeviceServer`:** streaming, `ImageBytes`, and the camera contract tests (a simulated full-size frame
   through the hub as `ImageArray` and `ImageBytes`, with the hub's memory checked to stay flat).
4. **`usb` transport:** already built with `zwoeaf` ("Focuser and filter wheel"); `zwoasi` is added to it.
5. **On the cameras:** read-only, then short darks with the cap on, then the cooler to a modest target and Warm up,
   then a frame in NINA. Then ConformU's Camera test (it exposes and cools: with the user's OK).
6. **Guide cameras:** the Duo's guide sensor first (it comes with `zwoasi`): frames for PHD2 or NINA's guider, then
   `PulseGuide` through the camera if step 1 shows a guide port, with the mount tracking and someone at the rig.
7. **`playerone` and `qhy`:** steps 1, 2 and 5 again for each, from its own SDK; the hub work (steps 3 and 4) is
   already done.

## Digital cameras (DSLR and mirrorless)
Planned 2026-11-24, as a possibility for after the astro cameras: no date and no code. The same Alpaca `Camera`
(ICameraV4) for system cameras on USB, so a client without its own support for a body gets one through the hub.
Everything in "Cameras" above (streamed frames, `ImageBytes`, the `usb` transport, one driver process) is built
first and reused here unchanged.

### What it is not
Not a way to feed eclipse or other timed-sequence programs that drive cameras through a maker's SDK themselves:
those keep their own direct path. A timed shot through the hub adds a network hop and one more process that must
be running; for a sequence where a lost frame can't be retaken that is the wrong trade.

### Which bodies, from what
The rule is the same as for the astro cameras: only what the maker publishes, nothing worked out from traffic or
from another program's library.
| Maker | Through | Notes |
|---|---|---|
| Sony | Camera Remote SDK (`Cr_Core.dll`, native, 64-bit) | Licence: whether the DLLs may ship in our installer. Needs the camera's USB mode set to PC Remote (not MTP). |
| Nikon | PTP from Nikon's published USB/MTP command documents (per body, in the Nikon SDK download) | No Nikon DLL needed. |
| Fujifilm | XAPI SDK (published) | Not plain PTP: what that needs was found by probing, not published. X-Trans has no Alpaca `SensorType` (below). |
| Canon | EDSDK (from Canon's developer programme) | Licence and download terms first. |
| OM System | none published | Not planned: its tethering library is licensed to the maker's own program only. |
A driver per maker (`sonycr`, `nikonptp`, `fujixapi`, `canoneds`), so one maker's library never loads into
another's process.

### Alpaca → system camera
| Alpaca | Camera | Notes |
|---|---|---|
| `StartExposure(d)` | Nearest shutter step at or below `d`; bulb above the longest step the body allows | The camera has fixed steps. A shorter exposure is taken and logged; one 1 s or more longer than asked is refused (`InvalidValue`), never taken. |
| `LastExposureDuration`, `LastExposureStartTime` | From the frame's EXIF (exposure time; the host's release time corrected by the body's measured start delay) | Not the requested value. |
| `Gain`, `Gains` | ISO, as the list of the body's ISO steps | No `GainMin`/`GainMax`. |
| `Offset`, cooler, `BinX/Y` > 1, `PulseGuide` | not implemented | |
| `StartX/Y`, `NumX/Y` | cropped from the full frame in the driver | The camera always takes the full frame. |
| `ImageArray`, `ImageBytes` | The RAW downloaded, decoded to the sensor's values (LibRaw), unprocessed | 50 MP is about 100 MB as UInt16: the streaming of "Frames through the hub" is required. |
| `SensorType`, `BayerOffsetX/Y` | RGGB and its offsets per body | X-Trans has no value: those bodies give `Color` (a demosaiced RGB frame) or aren't offered. Decided with the first such body. |
| `ReadoutModes` | not implemented in the first version | RAW only: RAW+JPEG slows some bodies after bursts. |
| `CanAbortExposure` | true for bulb, false otherwise | |

### Lessons that apply (from driving these bodies for timed sequences)
- **USB mode decides everything.** A body in the wrong USB mode (Sony in MTP, Fuji not on fixed tether, a body
  left in a menu or playback) answers but won't shoot. The page names the mode it needs, per body, instead of a
  generic "can't connect". The hub never opens a camera while looking for devices (`never_probe` for these USB
  IDs, as for the box): a Windows program opening a body through its photo import (WPD) can knock it out of
  remote mode.
- **Download, then delete from the camera, every frame.** Some bodies stop shooting once their buffer of
  undelivered frames fills; object handles can run out after a few hundred frames in one session (reopen then).
- **Set the body up on connect, put it back on disconnect.** Manual exposure, manual focus where the driver needs
  it, file format RAW, saving to the host; the body's own settings are read first and restored when the driver
  lets go.
- **Check before every exposure** that the body is still in manual exposure and that the shutter step read back
  is the one set: a body that fell back to its own mode takes frames at its own speed without an error.
- **A body that isn't there isn't "present".** A failed open or settings read counts as not connected, so a stale
  Windows device entry never shows as a camera.
- **Measured, per body:** shortest gap between releases, bulb start delay, buffer depth. These are the body's
  calibration in the driver's settings, not data-sheet values.

### Steps
1. **Facts, read-only (the user's call):** each body's USB IDs, serial, USB modes, and the SDK or document version;
   the licence of each SDK.
2. **One maker first** (the one whose SDK licence allows shipping), with its simulator (a body with real shutter
   steps, ISO steps and a RAW-sized test frame) and the contract tests.
3. **On the body:** connect and read; then a frame in NINA with a lens cap on; then ConformU's Camera test (with the
   user's OK).
4. The other makers, each from its own SDK or document.

## Focuser and filter wheel (ZWO EAF, EFW)
Planned 2026-11-24. Two drivers, `zwoeaf` (Alpaca `Focuser`) and `zwoefw` (Alpaca `FilterWheel`), each through
ZWO's own published SDK for it (`EAF_focuser.dll` / `EAF_focuser.h`, `EFW_filter.dll` / `EFW_filter.h`), called with
`ctypes` from the header. The same rules as for the cameras: only what ZWO publishes. They come **before** `zwoasi`:
they are small and have no large frames, so the `usb` transport is built and checked on them first, and the cameras
then only add the frame work.

### Who controls what
- **The client (Alpaca):** moves (focuser position, filter slot), `Halt`.
- **The page:** position, moving, temperature (EAF), the slot and its filter name (EFW), and a **Stop** for the EAF.
  With no client connected it can also move: the EAF by a step amount or to a position, the EFW to a slot.
- **The driver's own settings** (device level, per unit by serial, set on the page): the EFW's filter names and focus
  offsets. A wheel's filters are physical, so they belong with the wheel, not with a client's profile; clients that
  keep their own names are free to.
- **Read, never sent** (they are stored in the device and set with ZWO's own app or hand controller, as with the
  Optec configuration commands): the EAF's max step, reverse, backlash, beep, setting the current position
  (`EAFResetPostion`), `EAFSetID`; the EFW's one-way rotation (`EFWSetDirection`), `EFWCalibrate`, `EFWSetID`.
  Firmware updates stay with ZWO's tools.

### EAF: Alpaca `Focuser` → ZWO EAF SDK
| Alpaca | EAF SDK | Notes |
|---|---|---|
| `Connected` | `EAFOpen` / `EAFClose` | "In use by another program" on the page when the open fails. |
| `Absolute` | — | Always true. |
| `Position` | `EAFGetPosition` | |
| `Move(position)` | `EAFMove` | Outside 0…`MaxStep` it goes to the nearest end, no error (ConformU's "Move - Below 0" / "Move - Above MaxStep" expect that). The page refuses such a position instead. |
| `IsMoving` | `EAFIsMoving` | The SDK also says whether the hand controller is moving it: the page shows "moved by hand controller". |
| `Halt` | `EAFStop` | |
| `MaxStep` | `EAFGetMaxStep` | Read at connect; never set. |
| `MaxIncrement` | the same as `MaxStep` | |
| `StepSize` | — | Not in the SDK: not implemented. |
| `Temperature` | `EAFGetTemp` | Not implemented when the SDK reports no reading. |
| `TempComp`, `TempCompAvailable` | — | `TempCompAvailable` false; clients (NINA) do their own. |
| `Action`: reverse, backlash (read) | `EAFGetReverse`, `EAFGetBacklash` | Shown on the page, read-only. |

**Seen on a real EAF (2026-11-24, the user's, on a camera lens):** USB VID 03C3 PID 1F10 (two HID entries,
"HID-compliant vendor-defined device" and "USB Input Device"); SDK 1.8.5; name "EAF", `EAFGetType` "EAF-0-0",
firmware 3.2.2, a serial number; `EAFGetErrorCode` not supported (no E5 from this firmware: the move timeout is what
catches a jam); temperature about 35 to 37 °C on the bench (inside the unit). About 370 steps/s once moving (2000
steps in about 6 s, 10000 in about 27 s). `EAFOpen` answers "invalid ID" unless `EAFGetNum`/`EAFGetID` ran first in
that process (the driver always lists first). `EAFGetProperty`'s MaxStep stayed 60000 after `EAFSetMaxStep` 19700,
which `EAFGetMaxStep` reports (the driver reads that one). With the user's OK a one-off script, not the driver, set
the position to 0 at the lens's infinity end (`EAFResetPostion`) and MaxStep to 19700 (its close-focus end), so
ConformU's moves to 0 and MaxStep stay on the lens.
On a USB hub with one transaction translator (the user's, `1A40:0101`, Terminus FE1.1) shared with an open FTDI
serial adapter (an Optec Gemini) every EAF SDK call took 0.3-0.6 s, Stop up to 4 s, with ZWO's ASCOM driver as
well; on another hub, or with that port closed, 5-8 ms. Worth a line in the EAF's help if users report a slow EAF.

### EFW: Alpaca `FilterWheel` → ZWO EFW SDK
| Alpaca | EFW SDK | Notes |
|---|---|---|
| `Connected` | `EFWOpen` / `EFWClose` | |
| `Position` (get) | `EFWGetPosition` | −1 while it turns, as in both. Slots are 0-based in both. |
| `Position` (set) | `EFWSetPosition` | Refused outside the slot count. |
| `Names` | the driver's setting | "Filter 1" … by default; the count from `EFWGetProperty` (`slotNum`). |
| `FocusOffsets` | the driver's setting | 0 for each slot by default. |

### The EAF 5V
The EAF 5V (powered from USB instead of 12 V) is served by `zwoeaf` exactly as the standard EAF: the same SDK, the
same mapping, the same rules. It needs no code of its own, only:
- **Step 1 reads it too:** its name, IDs, firmware, max step and whether it reports a temperature, so the simulator
  has a 5V model and the tests run both. Any difference the SDK shows (a name, a speed, no temperature) comes from
  the SDK, not from the driver checking for "5V".
- **Power from USB:** on a weak port or an unpowered hub it can drop off when it starts to move. The driver then sees
  it vanish from the SDK's list: the move is reported as failed (not retried), and the device comes back on the same
  tab when the SDK lists it again. The page says "lost, probably power" when that happens during a move.

### Finding and naming
- **The `usb` transport:** the hub watches for ZWO's vendor ID (03C3) and starts `zwoeaf`, `zwoefw` and `zwoasi`
  when one appears; each driver lists only what its own SDK finds (`EAFGetNum` / `EFWGetNum`, `ASIGetNumOfConnectedCameras`)
  and reports "none" otherwise, which the hub shows as nothing found, not a fault. The hub never opens a device.
- **UniqueID:** model + serial (`EAFGetSerialNumber` / `EFWGetSerialNumber`) where the unit has one. A unit without a
  serial gets model + the SDK's ID, with the same swap caveat as a stock box; the driver never writes an ID.
- **Several units** (two EAFs, say) appear as separate devices of one driver process, each with its own tab.

### Safety
- **EAF:** the exit handling sends `EAFStop`. The hub's `on_lost` can't help here (a `usb` driver's bytes don't pass
  through the hub), but every EAF move is to a position and stops there by itself, so a killed process leaves at most
  one move finishing. The contract test kills it mid-move and checks the unit stops at the target.
- **Stall or jam (checked in SDK 1.8.5's header, 2026-11-24):** the SDK reports no motor current or power, and the
  position is the step count the EAF sent (an open-loop stepper), so skipped steps don't show in `Position`.
  What it does give: `EAFGetErrorCode` with motor code **E5 "motor stall"** (`EAF_ERROR_NOT_SUPPORTED` on firmware
  without it), and calls failing with `EAF_ERROR_ERROR_STATE`. The driver reads the error code after each move and
  while moving, and the page shows "stalled" with the position it stopped at. A move still running long after the
  steps should have taken (speed measured on the unit in step 1) is reported as "not finishing", and stopped.
  Battery models report `battery_discharge_curr` (`EAFGetBatteryInfo`); whether it follows motor load is to be
  tried on such a unit, not assumed.
- **EFW:** the SDK has no stop; a wheel that is turning finishes on its own. Nothing to do at exit.

### Steps
   **SDKs fetched 2026-11-24** from zwoastro.com's Product SDK page: `EAF_SDK_EXT_V1.8.5.zip` (SHA-256
   `19b8e268…c5c8cf`) and `EFW_SDK_EXT_V1.8.6.zip` (`770d67bd…debdf1`); the page itself still says 1.8.1 / 1.8.4.
   **Licence: MIT-style** (`license.txt`, "Copyright (c) 2015, ZWO Company", the same file in both), so the x64 DLLs
   may ship in our installers with that notice beside them. The EAF SDK has `lib/Windows` and `lib/Windows7` builds.
1. **Facts from the units, read-only (the user's call):** models, USB IDs, serials, firmware versions, the EAF's max
   step, reverse and backlash, the EFW's slot count. Get the newest SDKs from ZWO and read their licences (whether
   the DLLs may ship in our installers).
2. **`usb` transport** in the hub: vendor ID watching, starting the drivers, "nothing found" as a normal state.
   **Done 2026-11-24:** `hub/usbdevs.py` reads Windows' present USB devices (SetupAPI; nothing is opened);
   `Driver.transport = "usb"`, `usb_vendors`, `find(ctx, sims)`; the scan asks a driver only while one of its vendor's
   devices is plugged in, keeps its last list when it can't answer, and drops a unit missing for `GONE_S`; a
   restarted driver process is asked once before its units start again. Tests: `tests/test_usb.py`, the contract
   tests' usb path.
3. **`zwoeaf`:** `eaf.py` (the `ctypes` calls), `sim.py` (moves at a set speed, a hand-controller move, temperature),
   `driver.py`, page, tests, contract tests. **Done 2026-11-24 (0.1.0, simulator only):** SDK 1.8.5's x64 DLL and
   ZWO's licence in `zwoeaf/sdk/`; stall (E5) and "not finishing" as under Safety; `hub-sim-zwo` in launch.json.
   The simulator's speed, max step and USB PID (1F10) are guesses until step 1.
4. **`zwoefw`:** the same, with the names and offsets on the page.
5. **On the units:** read-only, then short EAF moves with the hand controller in reach, a mid-move kill, every EFW
   slot. Then ConformU's Focuser and FilterWheel tests (they move both: with the user's OK).

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
