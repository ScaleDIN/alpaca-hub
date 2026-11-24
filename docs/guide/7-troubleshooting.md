# When something doesn't work

[Guide](README.md) › When something doesn't work

**A device isn't found**
- Is another program using its port (an older service, the vendor's tool)? Close it and press *Scan now*.
- Is its driver installed and running? Look under *Drivers* on the Overview.
- Open *Serial ports* on the Overview: "no driver recognised it" or "no installed driver uses its USB chip" tells you
  the hub saw the port but not your device.
- Is the device powered? Some only answer once their 12 V supply is on.

**A port is "stuck in Windows' serial driver", or every scan is slow**
Some USB serial chips hang when opened. The hub carries on with the other ports. Unplugging the device's USB cable
and plugging it back in frees it. If it's a port you don't need the hub for, *Ignore* it under *Serial ports*.

**Phones or other computers can't reach the hub**
- Is the hub's PC on a Private network (Windows *Settings › Network & internet*)?
- If you pressed *Cancel* when Windows Firewall asked, or left the firewall option unticked when installing: Start
  menu › *Allow network access (firewall)*.

**A driver keeps restarting, or failed**
The Overview shows why and its last log lines. The full log is `logs\<driver>.log` in the settings folder (Start menu
› *Settings and log folder*).

**My imaging program lost a device after an update**
A driver update waits for the program to disconnect; *Switch now* forces it. The program gets the device back with
the same number. If it doesn't reconnect by itself, connect it again in the program.

**Two power boxes or flat panels**
They have no serial number, so the hub tells them apart by their USB socket. Keep each on its own socket.

**Still stuck?** Open an [issue](https://github.com/ScaleDIN/alpaca-hub/issues) with the hub's version, the device,
and what the Overview shows. The logs in the settings folder help.

[Back to the guide](README.md)
