#!/usr/bin/env python3
"""
Drive a running Deckard over its DBus API for a memory soak run.

Cycles every connected controller through the configured pages and writes a
marker line into mem_telemetry.csv when SC_MEM_TELEMETRY is set.

Usage:
    .venv/bin/python tests/soak/soak_driver.py [--cycles N] [--interval SECONDS]
"""

# The markers line the switches up against the timeline the sampler records.
# With no DBus service reachable, the driver prints the reason and exits 1
# rather than raising. The DBus API carries page, icon-pack and window methods
# only, so brightness and screensaver cycling are not driven from here yet.
import argparse
import os
import re
import sys
import time

SERVICE = "io.github.nazbert.Deckard"
TOP_PATH = "/io/github/nazbert/Deckard"
TOP_IFACE = "io.github.nazbert.Deckard"
CTRL_IFACE = "io.github.nazbert.Deckard.Controller"
CONTROLLER_BASE_PATH = TOP_PATH + "/controllers"


def _serial_to_dbus_path(serial: str) -> str:
    """Mirrors _serial_to_dbus_path in src/api.py. A DBus path allows only
    the characters [A-Za-z0-9_]."""
    return re.sub(r"[^A-Za-z0-9_]", "_", serial)


def connect():
    """Return (bus, top_proxy), or (None, None) if the app isn't reachable."""
    try:
        from dasbus.connection import SessionMessageBus
    except ImportError:
        print("dasbus is not importable in this interpreter -- run with the "
              "app's venv (.venv/bin/python).", file=sys.stderr)
        return None, None

    bus = SessionMessageBus()
    top = bus.get_proxy(SERVICE, TOP_PATH, TOP_IFACE)
    try:
        # get_proxy() performs no I/O of its own, so read a property to
        # confirm the service is up before committing to a run.
        _ = top.Controllers
    except Exception as e:
        print(f"Deckard DBus API not reachable ({e}). Is the app running?", file=sys.stderr)
        return None, None
    return bus, top


def write_marker(data_path: str, text: str) -> None:
    """Append a marker line, prefixed with '#', to mem_telemetry.csv.

    With telemetry off for this run there is no CSV to mark, so this does
    nothing. A marker is a correlation aid that nothing else here reads.
    """
    if not data_path:
        return
    csv_path = os.path.join(data_path, "logs", "mem_telemetry.csv")
    if not os.path.exists(csv_path):
        return
    with open(csv_path, "a") as f:
        f.write(f"# marker,{time.time():.0f},{text}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cycles", type=int, default=20, help="page-switch cycles per controller (default: 20)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between switches (default: 1.0)")
    args = parser.parse_args()

    bus, top = connect()
    if bus is None:
        return 1

    data_path = top.DataPath
    pages = top.Pages
    controllers = top.Controllers

    if not pages:
        print("No pages configured -- nothing to cycle through.", file=sys.stderr)
        return 1
    if not controllers:
        print("No controllers connected -- nothing to drive.", file=sys.stderr)
        return 1

    print(f"Driving {len(controllers)} controller(s) across {len(pages)} page(s), "
          f"{args.cycles} cycles, {args.interval}s apart.")
    write_marker(data_path, f"soak_driver start cycles={args.cycles} interval={args.interval}")

    for serial in controllers:
        ctrl_path = f"{CONTROLLER_BASE_PATH}/{_serial_to_dbus_path(serial)}"
        ctrl = bus.get_proxy(SERVICE, ctrl_path, CTRL_IFACE)
        for i in range(args.cycles):
            page = pages[i % len(pages)]
            try:
                ctrl.SetActivePage(page)
            except Exception as e:
                print(f"[{serial}] SetActivePage({page!r}) failed: {e}", file=sys.stderr)
            time.sleep(args.interval)
        print(f"[{serial}] completed {args.cycles} page switches")

    write_marker(data_path, "soak_driver done")
    print("Note: brightness and screensaver-force cycling are not yet exposed "
          "over DBus -- only page switches were driven.")

    bus.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
