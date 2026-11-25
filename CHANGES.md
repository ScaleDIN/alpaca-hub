# Changes

What changed in each release of the Alpaca Hub and its drivers. Newest first. Each heading is
`## <Hub | driver key> <version> (<date>)`: `packaging/build.py release` takes each new version's notes from here
for the hub's update check, and refuses a release whose new versions have no entry.

## zwoefw 0.1.2 (2026-11-25)
- A filter wheel that keeps answering with an error (one ZWO's software doesn't explain, seen after the driver was
  updated) is closed and opened again by itself after three tries, as after an unplug, instead of staying stuck
  until the driver is turned off and on.

## zwoeaf 0.1.4 (2026-11-25)
- The same for the focuser: one that keeps answering with an error is closed and opened again by itself.

## zwoefw 0.1.1 (2026-11-25)
- Choosing a filter answers at once, as imaging programs expect, while the wheel turns: on a USB hub shared with a
  busy USB serial adapter, ZWO's call to start a turn can take over a second, and the program had to wait for it.
- Choosing the filter that is already in place answers at once without asking the wheel.

## Hub 0.4.2 (2026-11-25)
- With two or more units of a kind (two filter wheels, two focusers), each keeps the tab name it got the first time:
  "ZWO EFW" and "ZWO EFW #2" no longer swap depending on which one the hub finds first.
- A unit that is gone keeps its number for when it comes back, so a new one gets the next free number. Renumber tabs
  on the driver's card on the Overview forgets the units that aren't plugged in and numbers the rest 1, 2, 3 again
  (not while an imaging program is connected to them). Alpaca numbers and settings don't change.
- Renumber Alpaca numbers on the Overview does the same for the Alpaca numbers (Focuser #0, #1 …), with a warning
  first: the devices here are numbered 0, 1, 2 per kind and numbers kept for devices that are gone are forgotten.
  Imaging programs that chose a device by its number must choose it again.
- Rename on a device's card on the Overview gives it a name of your own, shown on its tab, its card and in Saved
  history; empty brings back its usual name. Without one, a name stored in the device itself is shown (the custom
  ID set with ZWO's software, for the ZWO focuser and filter wheel).
- Saved history lists each device by its name ("ZWO EFW #2", or the one you gave it), also once it's unplugged.
- Charts draw positions and set levels (a focuser's position, a filter slot, a panel's brightness) as the values the
  device really had, in steps, instead of averages it never had; the saved day files take each minute's last value
  for them. The drivers use this from their next versions.

## nitecrawler 1.0.4 (2026-11-25)
- The chart draws the focuser, rotator and aux positions as the steps they really took, not averages. Needs Alpaca
  Hub 0.4.2 or newer.

## optec 1.0.4 (2026-11-25)
- The chart draws the focusers' and rotator's positions as the steps they really took, not averages. Needs Alpaca
  Hub 0.4.2 or newer.

## wcover 1.0.5 (2026-11-25)
- The chart draws the light's brightness and the heater level as the values they were set to, not averages (the
  cover's angle, a sensor reading, is still averaged). Needs Alpaca Hub 0.4.2 or newer.

## zwoeaf 0.1.3 (2026-11-25)
- The focuser's custom ID (set with ZWO's software) is shown as its name when you haven't named it in the hub
  (needs Alpaca Hub 0.4.2 to show).
- The page says the custom ID, like maximum step and backlash, is set with ZWO's own drivers or tools.
- Its chart draws the position as the steps it really took, not averages. Needs Alpaca Hub 0.4.2 or newer.

## zwoefw 0.1.0 (2026-11-25)
- New driver: the ZWO EFW filter wheel as an Alpaca FilterWheel, through ZWO's own EFW SDK (1.8.6). Tried on two
  7-slot EFWs at once: ConformU's FilterWheel test passes on each.
- Its page shows the slot and the filter in it, with a button per slot (locked while an imaging program is
  connected), and the filters' names and focus offsets, kept in the hub for that wheel so every program sees the
  same ones.
- A turn the hub didn't start (another program with the wheel open) and a turn that doesn't finish are shown; the
  SDK can't stop a turn.
- Needs Alpaca Hub 0.4.2 or newer.

## zwoeaf 0.1.2 (2026-11-24)
- The page says when the focuser was moved by another program (or the hand controller), and from where to where:
  the EAF can be opened by two programs at once, for example ZWO's own ASCOM driver while the hub holds it.

## zwoeaf 0.1.1 (2026-11-24)
- At the end of a move an imaging program could briefly read a position short of the target (for example 7506 for
  7570) while the focuser was already reported stopped, when the focuser answers slowly. It now always reads the
  final position.
- A move starts sooner: one call to the focuser instead of two.
- If the EAF answers slowly (hundreds of milliseconds a call), check that it isn't on the same USB hub as a USB
  serial adapter (FTDI) that is in use: moving it to another port fixed that on a test setup.

## Hub 0.4.1 (2026-11-24)
- A hub started for testing with only some drivers (`--drivers`) now keeps to them when one of its driver
  programs restarts; before, it took every other device it could find too.
- A request the hub refused (for example one not sent from its own page) could leave the connection unusable for
  the next request from the same program; the hub now reads it to the end first.

## zwoeaf 0.1.0 (2026-11-24)
- New driver: the ZWO EAF focuser as an Alpaca Focuser, through ZWO's own EAF SDK (1.8.5). Tried on a standard EAF
  (firmware 3.2.2): ConformU's Focuser test passes. The EAF 5V uses the same SDK but hasn't been tried yet.
- Its page shows the position, temperature (the probe when plugged in, else the EAF's own) and the settings stored
  in the focuser (read-only, set with ZWO's software), with Go to, Step and Halt, locked while an imaging program is
  connected.
- An imaging program asking for a position beyond either end gets that end.
- A motor stall reported by the focuser (E5, where its firmware has it) and a move that doesn't finish in time are
  shown, and the second is stopped.
- Needs Alpaca Hub 0.4.0 or newer.

## Hub 0.4.0 (2026-11-24)
- The device tabs keep the same order, by device type and unit, whatever COM port each device is on and whichever
  is found first. Drag a tab to move it, or press Ctrl+Shift+← / → on it: the hub keeps that order for every
  browser, and an unplugged device's tab comes back in its place. Reset order on the Overview goes back to the usual
  order.
- The hub can run drivers for USB devices that are reached through their maker's own software rather than a serial
  port (for the coming ZWO EAF driver).

## nitecrawler 1.0.3 (2026-11-24)
- The page names the focuser, rotator and aux motor by the numbers your imaging program sees (Focuser #2 …), not
  always #0 and #1.

## optec 1.0.3 (2026-11-24)
- The page's Hub card names each focuser and rotator by the number your imaging program sees (Focuser #1 …), not
  the driver's own 0.

## Hub 0.3.1 (2026-11-24)
- The hub looks for new versions online once a day and shows them on the Overview, with what's new. It sends nothing
  but its own version number; the Overview's Updates setting turns it off (then it sends nothing at all).
- On the hub's PC, Install downloads an update, checks it against the published list and runs its installer (Windows
  asks for administrator rights). After a hub update the hub starts again by itself.
- Drivers can state the oldest hub they work with; the hub and the driver installers refuse a driver for a newer hub.
- First public release of the source (MIT licence).

## optec 1.0.2 (2026-11-24)
- Needs Alpaca Hub 0.3.0 or newer, and says so (its installer refuses an older hub).

## wcover 1.0.4 (2026-11-24)
- Needs Alpaca Hub 0.3.0 or newer, and says so (its installer refuses an older hub).

## wbox 1.0.2 (2026-11-24)
- Needs Alpaca Hub 0.3.0 or newer, and says so (its installer refuses an older hub).

## nitecrawler 1.0.2 (2026-11-24)
- Needs Alpaca Hub 0.3.0 or newer, and says so (its installer refuses an older hub).

## Hub 0.3.0 (2026-11-24)
- Saved history: every device's 1-minute averages, with their lowest and highest readings, go to one file per day.
  One setting on the Overview keeps them for 1 to 365 days, and Open folder shows them on the hub's PC.
- The Overview has a chart of every device on one time axis. It reads the day files back up to 7 days, and can plot
  a CSV file saved earlier.
- Charts keep 10-second steps with each value's lowest and highest reading, drawn as a band around the line when
  one or two values are shown (the Range button switches it). A picker grouped by device chooses the values.
- Install this hub before this release's drivers: their pages need its new charts.

## optec 1.0.1 (2026-11-24)
- Gemini / Hercules: a late "END" left over from setting the reverse direction no longer upsets the next command
  (firmware 1.3.1 and 2.0.1).
- The page has Step (in/out, −/+) and Center for every focuser and rotator, and shows the values stored in the
  device, read-only, under Hub.
- The new charts and saved history. Needs Alpaca Hub 0.3.0 or newer.

## wcover 1.0.3 (2026-11-24)
- The new charts and saved history. Needs Alpaca Hub 0.3.0 or newer.

## wbox 1.0.1 (2026-11-24)
- The box's tab starts with an empty chart and keeps 24 hours, like the other devices; its history files follow
  the Overview's one setting.
- The new charts and saved history. Needs Alpaca Hub 0.3.0 or newer.

## nitecrawler 1.0.1 (2026-11-24)
- The new charts and saved history. Needs Alpaca Hub 0.3.0 or newer.

## wcover 1.0.2 (2026-11-23)
- A cover move that stops short of open or closed (something in the way) now reports Error, with the angle it
  stopped at, instead of Open or Closed.

## Hub 0.2.2 (2026-11-23)
- Just after a driver stops unexpectedly, Alpaca calls get "not connected" instead of an HTTP 502 error.
- A longer listen queue, so many programs connecting at once aren't refused.
