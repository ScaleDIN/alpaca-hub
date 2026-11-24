# Changes

What changed in each release of the Alpaca Hub and its drivers. Newest first. Each heading is
`## <Hub | driver key> <version> (<date>)`: `packaging/build.py release` takes each new version's notes from here
for the hub's update check, and refuses a release whose new versions have no entry.

## Hub 0.4.0 (2026-11-24)
- The device tabs keep the same order, by device type and unit, whatever COM port each device is on and whichever
  is found first. Drag a tab to move it, or press Ctrl+Shift+← / → on it: the hub keeps that order for every
  browser, and an unplugged device's tab comes back in its place. Reset order on the Overview goes back to the usual
  order.
- The hub can run drivers for USB devices that are reached through their maker's own software rather than a serial
  port (for the coming ZWO EAF driver).

## zwoeaf 0.1.0 (not yet released)
- New driver: the ZWO EAF focuser (the standard EAF and the EAF 5V) as an Alpaca Focuser, through ZWO's own EAF SDK.
  Not yet tried on a real focuser.
- Its page shows the position, temperature and the settings stored in the focuser (read-only), with Go to, Step and
  Halt, locked while an imaging program is connected.
- A motor stall reported by the focuser (E5), and a move that doesn't finish in time, are shown, and the second is
  stopped.
- Needs the hub's support for USB devices opened through a maker's SDK (hub 0.4.0).

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
