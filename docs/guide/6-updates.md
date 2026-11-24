# Updates

[Guide](README.md) › Updates

## The hub tells you

Once a day (and a minute after it starts, if the last look was a day ago), the hub reads the list of published
versions from this project's [releases](https://github.com/ScaleDIN/alpaca-hub/releases). When there is a newer hub,
or a newer version of a driver you have installed, a line at the top of the Overview says so. Open it to see what's
new in each.

![The Updates setting at the bottom of the Overview](img/updates.png)

**What it sends:** one request for the list, with nothing in it but the hub's version number. No device, computer or
user details.

**Turning it off:** untick *Check for updates online* at the bottom of the Overview. The hub then sends nothing at
all. *Check now* looks straight away.

## Installing an update

On the hub's PC, each update has an **Install** button:

1. The hub downloads the installer and checks it against the published list (its size and SHA-256 checksum). A file
   that doesn't match is deleted, never run.
2. It runs the installer, and Windows asks for administrator rights.
3. **A driver** switches to the new version by itself, once no imaging program is connected to its devices (or with
   *Switch now* under Drivers).
   **The hub** stops, updates and starts again; the page reloads when it's back. Connected programs lose their
   devices for about a minute, so the button asks first.

Install the hub first when both are new. A driver that needs a newer hub says so and has no Install button until
the hub is updated.

On a phone or another PC, the Overview shows the updates but installs nothing: open the page on the hub's PC.

## Updating by hand

Download the installers from the [latest release](https://github.com/ScaleDIN/alpaca-hub/releases/latest) and run
them, hub first. That's the same as Install, and the only way to update a hub older than 0.3.1. Your settings, device
numbers and history are kept.

What changed in each version: [CHANGES.md](../../CHANGES.md).

Next: [When something doesn't work](7-troubleshooting.md)
