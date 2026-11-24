"""Optec focuser hubs over their serial command sets.

- lynx: FocusLynx and ThirdLynx (one or two focusers, "<F1GETSTATUS>" style commands).
- gemini: Gemini and Hercules focusing rotators (one focuser and one rotator, "<F101GETSTA>" style commands).

Where the formats come from (2026-11-20): the reply formats are the printf strings in Optec's own firmware
(ThirdLynxMainBoardFW_V3.0.4.bin, GeminiMainBoardFW_V2.0.1.hex), the command names are in
Optec's drivers (Optec.FocusLynx.dll 3.3.5, Optec.Gemini.dll 1.4.5) and their framing matches INDI's focuslynx and
gemini drivers. Not yet run against real hardware.

Safety: these modules never send a configuration command on their own (device type, max position, temperature
compensation, network settings). Optec warns that a wrong device type can damage a focuser. Only status queries,
moves, halt, home, sync and the rotator's reverse (which Alpaca asks for) are ever sent.
"""

VERSION = "1.0.3"   # keep equal to ../driver.json (the contract tests check it)
