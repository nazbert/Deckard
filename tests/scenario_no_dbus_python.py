"""
Regression fence: no dbus-python anywhere in the tree (gl#195).

The #168-#172/#175 dependency swap removed dbus-python entirely (Gio for
bus work, dasbus where a proxy layer is wanted); dbus-python's implicit
main-loop integration (its GLib mainloop glue class) and abandoned upstream
are exactly what those issues paid to leave behind. Upstream's logind
detector still imports it -- a verbatim port is the likeliest way it comes
back, and this fence is what would have caught one.

Scans every tracked *.py (`git ls-files`) for the module's import forms and
its GLib mainloop glue class. Word-boundary matching, so `dasbus` and
`dbus_next`-style names pass. The needles are assembled at runtime from
fragments so this file never matches itself.
"""
import os
import re
import subprocess

import fixtures  # must be first: isolates DATA_PATH

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Assembled, never written literally, so the fence's own (tracked) source
# does not trip the scan.
MODULE = "d" + "bus"
PATTERNS = [
    re.compile(r"\bimport\s+" + MODULE + r"\b"),
    re.compile(r"\bfrom\s+" + MODULE + r"\b"),
    re.compile("DBusG" + "MainLoop"),
]

# Tripwire against a vacuous pass (same rationale as
# scenario_no_mutable_defaults): the tree has ~380 tracked *.py files; well
# under that means the listing broke, not that the tree shrank.
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
        f"{MODULE}-python usage found (the dep swap of #168-#172/#175 "
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
