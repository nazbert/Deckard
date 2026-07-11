#!/usr/bin/env python3
"""
soak_driver.py -- drive a running StreamController over its DBus API
(src/api.py) for a memory soak run (docs/memory-footprint-plan.md Phase 0
P0.6; see tests/soak/README.md for the full manual soak procedure this
script is one part of).

Cycles every connected controller through the configured pages and drops a
marker line into mem_telemetry.csv (if SC_MEM_TELEMETRY=1 was set for the
run) so the switches line up against the RSS/thread/fd timeline the
sampler is recording.

Degrades gracefully: if the app isn't running (no DBus service reachable),
this prints why and exits 1 instead of raising.

Usage:
    .venv/bin/python tests/soak/soak_driver.py [--cycles N] [--interval SECONDS]

Note: brightness and screensaver-force cycling (mentioned alongside page
switches in the plan) aren't exposed on the DBus API yet -- src/api.py
currently has only page/icon-pack/window methods. Only page switches are
driven for now; this script is the obvious place to add the others once
they land.
"""
import argparse
import os
import re
import sys
import time

SERVICE = "com.core447.StreamController"
TOP_PATH = "/com/core447/StreamController"
TOP_IFACE = "com.core447.StreamController"
CTRL_IFACE = "com.core447.StreamController.Controller"
CONTROLLER_BASE_PATH = TOP_PATH + "/controllers"


def _serial_to_dbus_path(serial: str) -> str:
    """Mirrors src/api.py's _serial_to_dbus_path -- DBus paths only allow [A-Za-z0-9_]."""
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
        # get_proxy() does no I/O by itself -- touch a property to confirm
        # the service is actually up before committing to a run.
        _ = top.Controllers
    except Exception as e:
        print(f"StreamController DBus API not reachable ({e}). Is the app running?", file=sys.stderr)
        return None, None
    return bus, top


def write_marker(data_path: str, text: str) -> None:
    """Append a '#'-prefixed marker line to mem_telemetry.csv. A no-op if
    telemetry wasn't enabled for this run (no CSV to mark) -- markers are a
    correlation aid, not something the rest of this script depends on."""
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
