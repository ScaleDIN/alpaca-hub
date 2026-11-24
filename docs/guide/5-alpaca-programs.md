# Imaging programs

[Guide](README.md) › Imaging programs

## Connecting

Alpaca Hub speaks ASCOM Alpaca, which imaging, guiding and planetarium programs use to reach devices over the
network, also on the same PC.

1. Start the hub, with the devices plugged in.
2. In your imaging program, open the equipment list for a focuser, rotator, switch, cover, or weather readings.
3. Choose the device from the Alpaca devices it discovered. If your program doesn't discover them, add the hub by
   hand: the PC's address (`localhost` on the same PC) and port **11111**.

Every device the hub found is there, whatever its brand. The Overview shows which numbers each device has.

## Device numbers stay put

Alpaca numbers each type of device: Focuser #0, Focuser #1 and so on. The hub counts them across all brands and
remembers each device by its UniqueID, so:

- a focuser keeps its number from night to night, and your program's profile stays valid;
- a device that isn't plugged in one night leaves a gap rather than shifting the others;
- two units of the same model get their own numbers, settings and tabs.

A device without a serial number of its own (the WandererBox, the WandererCover) is told apart by its USB socket:
keep each one on the same socket.

## While a program is connected

- The device's page locks its controls, so a click on the page can't interfere. *Unlock* lets you change something
  deliberately.
- A driver update waits until the program disconnects (or *Switch now* on the Overview).
- If a driver restarts, the hub connects the device again for the program, which carries on.

## Programs that only know classic ASCOM

Programs that speak Alpaca need nothing else. A program that only knows classic ASCOM drivers needs the ASCOM
Platform (6.5 or later) on the PC where it runs: in the ASCOM Chooser, turn on Alpaca discovery and pick the hub's
device. The Platform then makes it look like an ordinary ASCOM driver to the program.

## From another PC or a tablet

Programs on another computer on your home network can use the devices too, through
`http://<the hub PC's address>:11111/`, once the hub's PC is on a Private network
([Installing](2-install.md#phones-and-other-computers)).

Next: [Updates](6-updates.md)
