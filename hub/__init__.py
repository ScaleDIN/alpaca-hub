"""alpaca-hub: one ASCOM Alpaca service for every device on this PC.

Device drivers (hub/drivers) recognise their hardware on the serial ports and run it; the host serves all of them
at one address with one discovery responder and one status page, with a tab per device that is present.
"""
NAME = "Alpaca Hub"
VERSION = "0.4.1"
