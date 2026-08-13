#!/usr/bin/env python3
"""Module-size ratchet: caps how large any module in src/ or GtkHelper/ may grow.

Run it locally the same way CI does, from anywhere:

    python scripts/check_module_sizes.py

Exit 0 means every module is within its cap. Exit 1 prints one message per
offending file and names the fix.

WHAT THIS PROTECTS

A module nobody can hold in their head stops getting reviewed properly and
starts collecting behaviour that belongs elsewhere -- the deck controller
passed seven thousand lines exactly that way, one convenient addition at a
time, and unpicking it was expensive. This check is the brake: growth past a
cap has to be a deliberate, visible edit to this file rather than a line
nobody notices in a diff.

THE RULES

* Every .py file under src/ and GtkHelper/ is capped at DEFAULT_CAP physical
  lines.
* Files already over that cap when the guard landed are listed in GRANDFATHER,
  each pinned at the size it had that day. They are allowed to shrink and
  nothing else -- one line of growth fails the build.
* GRANDFATHER caps tighten automatically. When a listed file drops more than
  TIGHTEN_SLACK lines below its cap, the check fails and asks for the cap to be
  lowered to the new size, so the headroom a refactor wins can never be spent
  on fresh growth. When a listed file falls to DEFAULT_CAP or below, its entry
  goes away entirely and the default cap takes over.
* The deck controller shim is capped separately and hard at SHIM_CAP lines. It
  is a re-export surface -- imports and __all__ -- and the cap is what stops
  code re-accreting on the old path and quietly unwinding the package split.

A guard that fails open is worse than no guard, because it reads as green while
covering nothing. So the check also fails when its own footing moves: a root in
ROOTS that is not a directory, a GRANDFATHER entry naming a file that does not
exist or that sits outside the roots, a missing shim, and a symlinked directory
under a root (the walk does not descend into one, so its contents would be
uncapped). Each of those is a loud failure naming what to fix, never a silent
skip.

The roots are src/ and GtkHelper/, matching mypy's `files` in pyproject.toml so
both tools govern the same trees. Root-level modules -- main.py, globals.py and
their siblings -- are consequently ungoverned; they are small and few, and
widening the roots is the fix if that stops being true.

CHANGING A CAP

Lowering a GRANDFATHER number, or deleting an entry the check says is obsolete,
is ordinary housekeeping: edit the table in the same commit that shrinks the
file. Raising a number, or adding an entry, is the opposite -- it is a decision
to let a module keep growing, and the split it should have had instead is
almost always the cheaper change.

Physical lines are counted the way an editor shows them: every newline, plus a
final line that lacks its terminator. Files whose last line is unterminated
therefore read one higher here than in `wc -l`.

Stdlib only, no imports beyond it: this runs in CI's bare python:3.13-slim
image alongside compileall, with nothing installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees the cap governs, matching mypy's `files` in pyproject.toml. Application
# and helper code only -- tests are fixtures and scenarios, where length is
# inherent rather than a design smell.
ROOTS = ("src", "GtkHelper")

DEFAULT_CAP = 1200

# How far a grandfathered file may sit below its cap before the cap must follow
# it down.
TIGHTEN_SLACK = 100

# The deck controller compatibility surface: import statements and __all__,
# nothing else. Its cap is separate from GRANDFATHER because it is not a
# tolerated legacy size -- it is a ceiling this module must stay under forever.
SHIM_PATH = "src/backend/DeckManagement/DeckController.py"
SHIM_CAP = 100

# Files that were already over DEFAULT_CAP when this check landed, pinned at the
# size they had then. Shrink-only; see the tightening rule above.
GRANDFATHER: dict[str, int] = {
    "src/backend/DeckManagement/deck_controller/controller.py": 1667,
    "src/backend/DeckManagement/deck_controller/inputs.py": 2155,
    "src/backend/DeckManagement/deck_controller/media_writer.py": 1212,
    "src/backend/Store/StoreBackend.py": 2065,
}


def physical_lines(path: Path) -> int:
    """Count the lines of `path`, counting an unterminated last line as a line."""
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def iter_modules(failures: list[str]) -> list[Path]:
    """Every .py file under the governed roots, sorted, caches excluded.

    Walks without following symlinks, and reports anything that would make the
    walk cover less than it claims to: a root that is not a directory, or a
    symlinked directory whose contents the walk cannot reach.
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
            dirnames[:] = keep   # prune in place: os.walk descends into what is left

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
            # An entry the walk never visits caps nothing, and reads as though it
            # does -- the one way this table can lie.
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
    """Hold one module to its cap: grandfathered, or the default."""
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
