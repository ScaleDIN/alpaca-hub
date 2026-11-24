"""PyInstaller entry point for AlpacaHub.exe (no window) and alpaca-hub.exe (console). Both are the whole hub; the hub
also starts its driver processes with the same exe (hub/supervisor.py)."""
import sys

from hub.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
