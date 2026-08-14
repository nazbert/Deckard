"""
Regression fence against dbus-python anywhere in the tree.

The tree uses Gio for bus work and dasbus where a proxy layer helps. The scan
matches the import forms and the GLib mainloop glue class, on word boundaries,
in every tracked *.py. It assembles the needles at runtime, so this file never
matches itself.
"""
import os
import re
import subprocess

import fixtures  # must be first, to isolate DATA_PATH

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Assembled, never literal, so this tracked file does not trip its own scan.
MODULE = "d" + "bus"
PATTERNS = [
    re.compile(r"\bimport\s+" + MODULE + r"\b"),
    re.compile(r"\bfrom\s+" + MODULE + r"\b"),
    re.compile("DBusG" + "MainLoop"),
]

# Tripwire against a vacuous pass. The tree holds about 380 tracked *.py
# files. A much lower count means the git listing broke.
MIN_SCANNED = 300


def tracked_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", REPO_ROOT, "ls-files", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_no_dbus_python")

    offenders: list[tuple[str, int, str]] = []
    scanned = 0

    for rel_path in tracked_python_files():
        path = os.path.join(REPO_ROOT, rel_path)
        if not os.path.isfile(path):  # tracked but deleted in the worktree
            continue
        scanned += 1
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                if any(pattern.search(line) for pattern in PATTERNS):
                    offenders.append((rel_path, lineno, line.rstrip()))

    assert not offenders, (
        f"{MODULE}-python usage found (the dep swap "
        f"removed it; use Gio or dasbus instead):\n"
        + "\n".join(f"  {p}:{n}: {text}" for p, n, text in offenders)
    )

    assert scanned >= MIN_SCANNED, (
        f"only {scanned} files scanned (expected >= {MIN_SCANNED}) -- the "
        f"git listing broke, a clean result would be vacuous"
    )

    print(f"PASS: no {MODULE}-python usage in {scanned} tracked *.py files")
    print("PASS: scenario_no_dbus_python")


if __name__ == "__main__":
    main()
