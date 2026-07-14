"""The command-line argument parser, extracted so it can be imported without
pulling in globals (which resolves and creates the data dir at import time).

Stdlib-only and side-effect-free: safe to import before `import globals`,
including from rebrand_migration.py. globals.py imports `argparser` from here,
so every existing `gl.argparser` reference keeps working, and the rebrand
migration can resolve a --data override with the exact same parser (matching
argparse abbreviations and flag/value handling instead of guessing).
"""
import argparse

argparser = argparse.ArgumentParser()
argparser.add_argument("-b", help="Open in background", action="store_true")
argparser.add_argument("--devel", help="Developer mode (disables auto update)", action="store_true")
argparser.add_argument("--skip-load-hardware-decks", help="Skips initilization/use of hardware decks", action="store_true")
argparser.add_argument("--close-running", help="Close running", action="store_true")
argparser.add_argument("--data", help="Data path", type=str)
argparser.add_argument("--change-page", action="append", nargs=2, help="Change the page for a device", metavar=("SERIAL_NUMBER", "PAGE_NAME"))
argparser.add_argument("--list-devices", help="List all connected StreamDeck devices and their properties", action="store_true")
argparser.add_argument("--list-pages", help="List all available pages", action="store_true")
argparser.add_argument("--change-state", action="append", nargs=4,
                      help="Change the state of a StreamDeck item. Format: SERIAL PAGE COORDS STATE\n"
                           "  SERIAL: Device serial number (e.g., CL123456789)\n"
                           "  PAGE: Page name (e.g., Main, Soundboard) \n"
                           "  COORDS: Position as x,y (e.g., 0,0 for top-left)\n"
                           "  STATE: State number to change to (0, 1, 2, etc.)\n"
                           "Example: --change-state CL123456789 Main 0,0 1",
                      metavar=("SERIAL", "PAGE", "COORDS", "STATE"))
argparser.add_argument("app_args", nargs="*")
