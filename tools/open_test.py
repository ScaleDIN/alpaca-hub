"""Open serial ports one after another, keep them all open, and time each open. Never writes a byte.

Made 2026-11-20 to test why a second CH340 port (box COM8 / panel COM11) hangs in SetCommState while the other is
open. Empire (.NET SerialPort) and INDI both open these devices with DTR and RTS ON; the hub opens with both OFF.

usage:
    python tools/open_test.py --lines on  COM8 COM11      # like Empire / INDI
    python tools/open_test.py --lines off COM8 COM11      # like the hub (hub/ports.py open_port)

Options:
    --lines on|off   DTR and RTS state handed to SetCommState at open (default: on)
    --baud N         default 19200 (what Empire and INDI use for the box and the panel)
    --hold S         after the last open, keep everything open S seconds and count received bytes (default 15)

Reading is passive (only bytes the devices send by themselves), so it is safe for the box, which takes every byte
it receives as a stored command. With DTR on, a board may restart when its port opens; the byte counts show when it
starts talking again.

If an open hangs, the script says so every 5 s. A process stuck in the driver may not exit even when killed, and
keeps its ports until the device is unplugged and plugged back in. Use the full-path Python, not the `python` alias:
    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe tools\\open_test.py --lines on COM8 COM11
"""
import argparse
import os
import sys
import threading
import time

import serial  # pyserial


def say(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def open_one(device, baud, lines_on):
    s = serial.Serial()
    s.port = device
    s.baudrate = baud
    s.timeout = 0           # reads return at once with what has arrived (same as the hub)
    s.write_timeout = 1.0
    s.dtr = lines_on        # set before open(), so they go into the DCB of the first SetCommState
    s.rts = lines_on
    done = threading.Event()

    def watchdog():
        t0 = time.monotonic()
        while not done.wait(5):
            say(f"  ... {device} open still waiting after {time.monotonic() - t0:.0f} s (stuck in the driver?)")

    threading.Thread(target=watchdog, daemon=True).start()
    t0 = time.monotonic()
    try:
        s.open()
    finally:
        done.set()
    return s, time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ports", nargs="+")
    ap.add_argument("--lines", choices=["on", "off"], default="on")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--hold", type=float, default=15.0)
    a = ap.parse_args()
    lines_on = a.lines == "on"
    say(f"pyserial {serial.__version__}, Python {sys.version.split()[0]}, DTR/RTS {a.lines.upper()}, {a.baud} baud")

    held = []
    for dev in a.ports:
        others = ", ".join(s.port for s in held) or "nothing"
        say(f"opening {dev} (holding {others}) ...")
        try:
            s, dt = open_one(dev, a.baud, lines_on)
        except serial.SerialException as e:
            say(f"  {dev} FAILED: {e}")
            continue
        say(f"  {dev} open in {dt:.2f} s  (DTR={s.dtr} RTS={s.rts})")
        held.append(s)

    if held and a.hold > 0:
        say(f"holding {len(held)} port(s) {a.hold:.0f} s, counting bytes the devices send by themselves")
        counts = {s.port: 0 for s in held}
        first = {}
        t0 = time.monotonic()
        while time.monotonic() - t0 < a.hold:
            for s in held:
                n = s.in_waiting
                if n:
                    s.read(n)
                    counts[s.port] += n
                    first.setdefault(s.port, time.monotonic() - t0)
            time.sleep(0.05)
        for p, n in counts.items():
            start = f", first after {first[p]:.1f} s" if p in first else ""
            say(f"  {p}: {n} bytes{start}")

    for s in held:
        t0 = time.monotonic()
        s.close()
        say(f"closed {s.port} in {time.monotonic() - t0:.2f} s")
    say("done")
    sys.stdout.flush()
    os._exit(0)  # don't wait on a watchdog or anything left in the driver


if __name__ == "__main__":
    main()
