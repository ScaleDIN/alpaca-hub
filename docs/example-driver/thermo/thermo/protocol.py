"""The thermometer's serial protocol: 9600 baud, and once a second, unasked, a line

    THERMO <serial number> <temperature in °C>

for example "THERMO T1 21.4". It takes no commands. Keep the parsing here, free of I/O, so it is easy to test."""

BAUD = 9600


def parse_line(line):
    """b"THERMO T1 21.4\\r\\n" (or str) -> ("T1", 21.4), or None if it isn't one of its lines."""
    if isinstance(line, bytes):
        line = line.decode("ascii", "replace")
    parts = line.split()
    if len(parts) != 3 or parts[0] != "THERMO":
        return None
    try:
        return parts[1], float(parts[2])
    except ValueError:
        return None
