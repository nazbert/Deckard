#!/usr/bin/env python3
"""Module-size ratchet. It caps how large a module in src/ or GtkHelper/ grows.

Run it the same way CI does, from anywhere:

    python scripts/check_module_sizes.py

Exit 0 means every module is within its cap. Exit 1 prints one message per
offending file and names the fix.

A module that nobody can hold in their head stops getting a proper review and
starts to collect behaviour that belongs elsewhere. The deck controller passed
seven thousand lines that way, one convenient addition at a time, and the split
was expensive. This check makes growth past a cap a visible edit to this file,
not a line that a diff hides.

The rules are these.

Every .py file under src/ and GtkHelper/ takes a cap of DEFAULT_CAP physical
lines.

GRANDFATHER lists the files that were already over that cap when this guard
landed, each pinned at the size it had that day. Such a file may shrink and
nothing else, and one line of growth fails the build.

A GRANDFATHER cap tightens by itself. When a listed file drops more than
TIGHTEN_SLACK lines below its cap, the check fails and asks for a cap at the new
size, so a refactor cannot bank headroom for later growth. When a listed file
falls to DEFAULT_CAP or below, its entry goes away and the default cap governs.

The deck controller shim takes a separate hard cap of SHIM_CAP lines. It holds
only re-exports, which are import statements and __all__, and the cap stops code
from collecting on the old path and undoing the package split.

A guard that fails open reads as green and covers nothing, so this check also
fails when its own footing moves. Each of these is a loud failure that names the
fix, and never a silent skip: a root in ROOTS that is not a directory, a
GRANDFATHER entry that names a file which does not exist or which sits outside
the roots, a missing shim, and a symlinked directory under a root, because the
walk does not descend into one and its contents would go uncapped.

The roots are src/ and GtkHelper/, which match the mypy files setting in
pyproject.toml, so both tools govern the same trees. The root-level modules,
such as main.py and globals.py, stay ungoverned. They are small and few. Widen
the roots when that stops being true.

To lower a GRANDFATHER number, or to delete an entry that the check calls
obsolete, edit the table in the commit that shrinks the file. To raise a number,
or to add an entry, is a decision to let a module keep growing, and the split it
needs instead is almost always the cheaper change.

This check counts physical lines the way an editor shows them, which is every
newline plus a final line without a terminator. A file whose last line has no
terminator therefore reads one higher here than in wc -l.

This module imports from the standard library only, because it runs in the bare
python:3.13-slim image of CI beside compileall, with nothing installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees that the cap governs. They match the mypy files setting in
# pyproject.toml. They hold application and helper code only, because a test is
# a fixture or a scenario, where the length comes from the case.
ROOTS = ("src", "GtkHelper")

DEFAULT_CAP = 1200

# How far a grandfathered file may sit below its cap before the cap must follow
# it down.
TIGHTEN_SLACK = 100

# The deck controller compatibility surface holds import statements and
# __all__, and nothing else. Its cap stays out of GRANDFATHER, because it is a
# permanent ceiling and not a tolerated legacy size.
SHIM_PATH = "src/backend/DeckManagement/DeckController.py"
SHIM_CAP = 100

# Files that were already over DEFAULT_CAP when this check landed, pinned at the
# size they had then. They may shrink only. See the tightening rule above.
GRANDFATHER: dict[str, int] = {
    "src/backend/DeckManagement/deck_controller/controller.py": 1667,
    "src/backend/DeckManagement/deck_controller/inputs.py": 2155,
    "src/backend/Store/StoreBackend.py": 1948,
}


def physical_lines(path: Path) -> int:
    """Count the lines of path. A last line without a terminator counts."""
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def iter_modules(failures: list[str]) -> list[Path]:
    """Every .py file under the governed roots, sorted, without the caches.

    The walk does not follow a symlink. It reports whatever makes it cover less
    than it claims, which is a root that is not a directory, or a symlinked
    directory whose contents it cannot reach.
    """
    found: list[Path] = []
    for root in ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            failures.append(
                f"{root}/: governed root is not a directory. Every module under it "
                "would go uncapped, so this check refuses to report success. Restore "
                "the tree, or update ROOTS in the same change that moves it."
            )
            continue

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            here = Path(dirpath)
            keep: list[str] = []
            for name in sorted(dirnames):
                if name == "__pycache__":
                    continue
                child = here / name
                if child.is_symlink():
                    failures.append(
                        f"{child.relative_to(REPO_ROOT).as_posix()}: symlinked directory "
                        "under a governed root. The walk does not descend into it, so "
                        "every module inside it would go uncapped. Replace it with a real "
                        "directory, or teach this check to follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place, so os.walk skips what is gone

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def check_shim(failures: list[str]) -> None:
    """Hold the deck controller shim under its hard cap."""
    shim = REPO_ROOT / SHIM_PATH
    if not shim.is_file():
        failures.append(
            f"{SHIM_PATH}: missing. The deck controller shim is the compatibility "
            "path plugins import from, and its hard cap is what keeps the package "
            "split from unwinding. Moving or deleting it needs this check updated "
            "in the same change."
        )
        return

    lines = physical_lines(shim)
    if lines > SHIM_CAP:
        failures.append(
            f"{SHIM_PATH}: {lines} lines, hard cap {SHIM_CAP} (over by {lines - SHIM_CAP}). "
            "This module re-exports the deck controller package and defines nothing: "
            "import statements and __all__. Code that belongs to a deck controller "
            "belongs in src/backend/DeckManagement/deck_controller/. The cap is what "
            "keeps the split from unwinding one convenience function at a time, so "
            "for accreted code, moving it into the package is the fix and raising "
            "the cap is not. A genuinely new compatibility name is the other case: "
            "the surface tracks what upstream binds at this path, and widening it "
            "does justify raising the cap -- deliberately, in a change that says so."
        )


def check_grandfather_table(failures: list[str]) -> None:
    """Reject a table that has drifted from the tree it describes."""
    if SHIM_PATH in GRANDFATHER:
        failures.append(
            f"{SHIM_PATH}: listed in GRANDFATHER. The shim is governed by its own "
            f"hard cap of {SHIM_CAP} lines and must not be grandfathered -- remove "
            "the entry."
        )
    for name in sorted(GRANDFATHER):
        if not any(name.startswith(f"{root}/") for root in ROOTS):
            # An entry that the walk never visits caps nothing, and it still
            # reads as a cap. That is the one way this table can lie.
            failures.append(
                f"{name}: listed in GRANDFATHER but outside the governed roots "
                f"({', '.join(ROOTS)}). Nothing enforces this entry. Delete it, or add "
                "the tree it lives in to ROOTS so the file is actually capped."
            )
        elif not (REPO_ROOT / name).is_file():
            failures.append(
                f"{name}: listed in GRANDFATHER but not present in the tree. "
                "Delete the entry, or point it at the file's new path."
            )


def check_module(path: Path, failures: list[str]) -> None:
    """Hold one module to its cap, either grandfathered or the default."""
    name = path.relative_to(REPO_ROOT).as_posix()
    if name == SHIM_PATH:
        return  # check_shim owns this one

    lines = physical_lines(path)
    cap = GRANDFATHER.get(name)

    if cap is None:
        if lines > DEFAULT_CAP:
            failures.append(
                f"{name}: {lines} lines, cap {DEFAULT_CAP} (over by {lines - DEFAULT_CAP}). "
                "Split the module along a seam that stands on its own -- a module this "
                "long stops being reviewable as a unit."
            )
        return

    if lines > cap:
        failures.append(
            f"{name}: {lines} lines, grandfathered cap {cap} (over by {lines - cap}). "
            "Grandfathered modules are allowed to shrink and nothing else. Move the "
            "addition into a module of its own rather than growing this one."
        )
    elif lines <= DEFAULT_CAP:
        failures.append(
            f"{name}: {lines} lines, now within the {DEFAULT_CAP}-line default cap. "
            "Delete its GRANDFATHER entry -- the default cap covers this module from "
            "here on."
        )
    elif lines < cap - TIGHTEN_SLACK:
        failures.append(
            f"{name}: {lines} lines, grandfathered cap {cap} -- {cap - lines} lines of "
            f"slack, more than the {TIGHTEN_SLACK} allowed. Lower the GRANDFATHER entry "
            f"to {lines} so the room this file gave back cannot be spent on new growth."
        )


def main() -> int:
    failures: list[str] = []

    check_grandfather_table(failures)
    check_shim(failures)

    modules = iter_modules(failures)
    for path in modules:
        check_module(path, failures)

    if failures:
        print(
            f"Module-size ratchet: {len(failures)} problem(s) "
            f"(default cap {DEFAULT_CAP} lines; see scripts/check_module_sizes.py).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"Module-size ratchet: {len(modules)} modules within cap "
        f"({DEFAULT_CAP} lines default, {len(GRANDFATHER)} grandfathered, "
        f"shim hard cap {SHIM_CAP})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
