#!/usr/bin/env python3
"""Settings-JSON ratchet: no module opens a settings file with a bare json.load.

Run it locally the same way CI does, from anywhere:

    python scripts/check_settings_json.py

Exit 0 means every ``json.load``/``json.dump`` call in the governed trees sits
in a file the allowlist below sanctions. Exit 1 prints one message per problem
and names the fix. ``--self-test`` runs the check against a throwaway tree with
a planted bare reader and proves it goes red; it prints PASS/FAIL and is not
part of the ordinary run.

WHAT THIS PROTECTS

The app owns a set of JSON settings files -- deck settings, app settings, the
page-manager bookkeeping, the asset library index, plugin settings, the asset
chooser's UI state. Every one of them needs the same three answers: where it
lives, what happens when it is corrupt, and who may write it. Those answers
live in one place, ``src/backend/settings_store.py``, and the way a surface
gets them is by being read and written through the store. A module that opens
one of these files with a bare ``json.load`` instead has none of them: it does
not heal a corrupt file (one such reader took the whole app down at boot), it
does not share a cache with other readers, and its write can land straight past
the atomic writer. Each of those was a real defect this wave closed.

This check is the brake. It does not stop anyone from adding a raw reader -- it
makes adding one a *decision*: a ``json.load`` in a file the allowlist does not
name fails the build, so the choice between "route it through the store" and
"this is a sanctioned exception" is made in review, in a two-line diff, rather
than in a line nobody notices.

THE STRING FORMS ARE NOT POLICED, ON PURPOSE

``json.loads`` and ``json.dumps`` parse an in-memory string -- a subprocess's
stdout, a DBus reply, an HTTP body -- and never touch a settings file. Policing
them would turn every window-grabber integration and every store payload into a
false positive for a concern that does not apply to them. A settings file is
*opened* and read or written, which is ``json.load`` / ``json.dump``; that is
the line, and it is the same one the store draws.

THE RULES

* Every ``json.load``/``json.dump`` call in a governed file must sit in a file
  the ALLOWLIST names. A call anywhere else fails.
* The allowlist is per file: naming a file sanctions the raw JSON access in it.
  A file the allowlist names that no longer makes any such call fails too -- a
  standing exemption for a read nobody performs is how the next one arrives
  unremarked, so the exemption is dropped the moment its reader is.
* ``from json import load`` / ``from json import dump`` fail outright: they bind
  the file entry points under bare names this check would not see at the call
  site, which is the one way a governed reader could hide from it. Go through
  ``json.load`` -- or, for a settings file, through the store.

WHAT COUNTS AS A CALL

Any ``<json>.load(...)`` or ``<json>.dump(...)`` where ``<json>`` is the name
``json`` or a module alias bound by ``import json as ...`` -- collected over the
whole file at any nesting depth, because ``main.py`` imports json inside the
function that uses it. The threat model is the well-meaning change, not the
determined one: a raw reader arrives because somebody reached for ``json.load``,
and every such arrival in this tree has been exactly that. An alias laundered
through another name, or ``getattr(json, "load")``, is left to review, the same
line the gl-slot freeze draws.

A guard that fails open is worse than no guard, because it reads as green while
covering nothing. So the check also fails when its own footing moves: a
governed root that is not a directory, a governed file that is missing, a
symlinked directory under a root (the walk does not descend into one, so every
call inside it would go unseen), a file that will not parse, and an allowlist
entry naming a file that does not exist or sits outside the governed set. Each
is a loud failure naming what to fix, never a silent skip.

The trees are src/ and GtkHelper/, matching mypy and the module-size ratchet.
globals.py and main.py are governed by name as well -- the way the gl-slot
freeze names main.py -- because the allowlist sanctions a raw read in each and
an allowlist entry the walk never visits sanctions nothing.

Stdlib only, no imports beyond it: this runs in CI's bare python:3.13-slim
image alongside compileall, with nothing installed.
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_SCRIPT = "scripts/check_settings_json.py"

# Root-level modules that read a settings file raw and live outside the trees
# below. Named individually for the reason the gl-slot freeze names main.py:
# the allowlist sanctions a raw read in each, and an allowlist entry the walk
# never visits sanctions nothing.
GOVERNED_FILES = ("globals.py", "main.py")
GOVERNED_TREES = ("src", "GtkHelper")

# The two file-level json entry points this check governs (see the docstring:
# the string forms json.loads/json.dumps are deliberately out of scope).
FILE_JSON_FUNCS = frozenset({"load", "dump"})

# The `from json import <name>` names that would hide a governed call.
HIDDEN_IMPORT_NAMES = frozenset({"load", "dump"})

# Files sanctioned to read or write JSON files directly. Everything else routes
# through the settings store, whose own read-with-heal loader and the atomic
# writer it wraps are the first two entries. One file, one reason, per line.
ALLOWLIST: dict[str, str] = {
    # The store itself: its loader IS the read-with-heal every settings read
    # reaches instead of opening a file, and the atomic writer IS the one
    # json.dump every settings write funnels through.
    "src/backend/settings_store.py": "the settings store's read-with-heal loader",
    "src/backend/atomic_json.py": "the atomic settings writer -- the one json.dump every write funnels through",
    # The data-path bootstrap: runs before the store (or anything) is importable
    # and is the read that DEFINES the data path the store resolves against.
    "globals.py": "the data-path bootstrap read, which runs before the store exists",
    # A dev-only page dump printed by the CLI, reading page files directly.
    "main.py": "the CLI page-listing dump, a dev listing that never runs in the app proper",
    # Migrators read old on-disk formats once, to rewrite them into the current
    # shape -- before the store's surfaces describe the tree.
    "src/backend/Migration/Migrator.py": "a migrator reading a legacy file to rewrite it",
    "src/backend/Migration/Migrators/Migrator_1_5_0.py": "a migrator reading legacy deck/page files",
    "src/backend/Migration/Migrators/Migrator_1_5_0_beta_5.py": "a migrator reading legacy page/settings files",
    # Pack manifests are read-only SOURCE files shipped inside a pack, not app
    # settings: the app never writes them, so nothing overwrites a corrupt one.
    "src/backend/IconPackManagement/IconPack.py": "an icon pack's read-only manifest",
    "src/backend/WallpaperPackManagement/WallpaperPack.py": "a wallpaper pack's read-only manifest",
    "src/backend/SDPlusBarWallpaperPackManagement/SDPlusBarWallpaperPack.py": "an SD+ wallpaper pack's read-only manifest",
    # Plugin manifest.json / about.json are read-only source files the app never
    # writes -- the log-and-leave policy the store deliberately does not own.
    "src/backend/PluginManager/PluginBase.py": "a plugin's read-only manifest/about source files",
    # The plugin store's own on-disk state: a cache index with its own locked
    # flush protocol, and the id read out of a downloaded asset's manifest.
    "src/backend/Store/StoreCache.py": "the plugin store's cache index, on its own flush protocol",
    "src/backend/Store/StoreBackend.py": "reading a downloaded asset's id out of its manifest",
    # A page backup is parsed to validate it before it is trusted as a heal
    # source; the page machinery owns page files end to end.
    "src/backend/PageManagement/page_document.py": "validating a page backup before healing from it",
    # Importers read a FOREIGN export to translate it into pages -- a one-shot
    # read of a file the app does not own and never writes back.
    "src/windows/PageManager/Importer/Importer.py": "probing a foreign export's shape before importing it",
    "src/windows/PageManager/Importer/StreamController/StreamController.py": "reading a StreamController export to import it",
    "src/windows/PageManager/Importer/StreamDeckUI/StreamDeckUI.py": "reading a StreamDeck UI export to import it",
    "src/windows/PageManager/elements/MenuButton.py": "reading a page file chosen for import",
}


def relative(path: Path, root: Path) -> str:
    """Repo-relative posix path, or the absolute one if it sits outside the repo."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def parse(path: Path, root: Path, failures: list[str]) -> ast.Module | None:
    """Parse `path`, or record why it could not be parsed. Never silently skips."""
    try:
        source = path.read_bytes()
    except OSError as error:
        failures.append(
            f"{relative(path, root)}: cannot be read ({error}). This check covers every "
            "file under the governed roots, so one it cannot read is one it cannot report on."
        )
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        failures.append(
            f"{relative(path, root)}:{error.lineno}: does not parse ({error.msg}). Fix the "
            "syntax -- an unparseable file is unchecked, not clean."
        )
        return None


def collect_governed_files(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...], failures: list[str]
) -> list[Path]:
    """Every governed file, sorted. Reports anything that shrinks the sweep."""
    found: list[Path] = []

    for name in governed_files:
        path = root / name
        if not path.is_file():
            failures.append(
                f"{name}: governed file is missing. It is named individually because the "
                f"allowlist sanctions a raw read in it, so an unswept one is a hole in the "
                f"check. Restore it, or update GOVERNED_FILES in {THIS_SCRIPT} in the same "
                "change that moves it."
            )
            continue
        found.append(path)

    for tree in governed_trees:
        base = root / tree
        if not base.is_dir():
            failures.append(
                f"{tree}/: governed root is not a directory. Every json call under it would "
                f"go unchecked, so this check refuses to report success. Restore the tree, or "
                f"update GOVERNED_TREES in {THIS_SCRIPT} in the same change that moves it."
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
                        f"{relative(child, root)}: symlinked directory under a governed root. "
                        "The walk does not descend into it, so every json call inside it would "
                        "go unseen. Replace it with a real directory, or teach this check to "
                        "follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place: os.walk descends into what is left

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def json_aliases(tree: ast.Module) -> set[str]:
    """Names the file uses for the json module.

    Always includes ``json`` (the house form), plus any ``import json as X`` --
    collected at any depth, because main.py imports json inside a function.
    """
    aliases = {"json"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json" and alias.asname:
                    aliases.add(alias.asname)
    return aliases


def check_file(
    path: Path, root: Path, allowlist: dict[str, str], used: set[str], failures: list[str]
) -> int:
    """Pin every file-level json call in one file. Returns the count found.

    Records into `used` whether an allowlisted file actually made a call, so a
    stale exemption can be dropped.
    """
    where = relative(path, root)
    tree = parse(path, root, failures)
    if tree is None:
        return 0

    aliases = json_aliases(tree)
    sanctioned = where in allowlist

    problems: list[tuple[int, str]] = []
    calls = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "json":
            for alias in node.names:
                if alias.name in HIDDEN_IMPORT_NAMES:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `from json import {alias.name}` binds a file "
                        "entry point under a bare name this check cannot see at the call site. "
                        f"Write `json.{alias.name}` -- or, for a settings file, go through the "
                        "settings store.",
                    ))
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in FILE_JSON_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            calls += 1
            if not sanctioned:
                problems.append((
                    node.lineno,
                    f"{where}:{node.lineno}: opens a JSON file with `json.{func.attr}`. A "
                    "settings file is read and written through the settings store "
                    "(src/backend/settings_store.py), whose loader heals a corrupt one and "
                    "whose writer is atomic. If this genuinely is not a settings file, add "
                    f"{where!r} to the ALLOWLIST in {THIS_SCRIPT}, with the reason -- a "
                    "two-line, reviewable decision.",
                ))

    if sanctioned and calls:
        used.add(where)

    failures.extend(message for _, message in sorted(problems))
    return calls


def check_allowlist_table(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...],
    allowlist: dict[str, str], failures: list[str],
) -> None:
    """Reject an allowlist entry the walk will never visit -- it caps nothing and
    reads as though it does."""
    for name in sorted(allowlist):
        under_governed = name in governed_files or any(
            name.startswith(f"{tree}/") for tree in governed_trees
        )
        if not under_governed:
            failures.append(
                f"{name}: listed in the ALLOWLIST but outside the governed set "
                f"({', '.join(governed_files + governed_trees)}). Nothing enforces this entry. "
                "Delete it, or add the tree/file it lives in to the governed set so its calls "
                "are actually checked."
            )
        elif not (root / name).is_file():
            failures.append(
                f"{name}: listed in the ALLOWLIST but not present in the tree. Delete the "
                "entry, or point it at the file's new path."
            )


def check_allowlist_use(allowlist: dict[str, str], used: set[str], failures: list[str]) -> None:
    """Drop an allowlist entry once the reader it excuses is gone.

    An exemption that outlives its json call is a standing permission for a read
    nobody makes, and a standing permission is how the next one arrives
    unremarked -- the same staleness pressure the gl-slot freeze puts on its own
    per-file exemptions.
    """
    for name in sorted(allowlist):
        if name not in used:
            failures.append(
                f"{name}: listed in the ALLOWLIST but calls no json.load/json.dump (or is no "
                "longer parseable/present). Drop the entry rather than leaving a raw-JSON "
                "exemption standing for a read nothing performs."
            )


def run_check(
    root: Path, governed_files: tuple[str, ...], governed_trees: tuple[str, ...],
    allowlist: dict[str, str],
) -> tuple[list[str], int, int]:
    """The whole check against one tree. Returns (failures, files, calls)."""
    failures: list[str] = []
    check_allowlist_table(root, governed_files, governed_trees, allowlist, failures)
    files = collect_governed_files(root, governed_files, governed_trees, failures)
    used: set[str] = set()
    calls = sum(check_file(path, root, allowlist, used, failures) for path in files)
    check_allowlist_use(allowlist, used, failures)
    return failures, len(files), calls


def self_test() -> int:
    """Prove the check goes red on a planted bare reader and a stale entry."""
    problems: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="settings-json-selftest-"))
    try:
        backend = tmp / "src" / "backend"
        backend.mkdir(parents=True)
        (tmp / "GtkHelper").mkdir()
        (tmp / "globals.py").write_text("x = 1\n")
        (tmp / "main.py").write_text("y = 2\n")
        (backend / "settings_store.py").write_text(
            "import json\n\n\ndef load(p):\n    with open(p) as h:\n        return json.load(h)\n"
        )
        (backend / "clean.py").write_text("z = 3\n")
        allow = {"src/backend/settings_store.py": "the loader"}
        gfiles = ("globals.py", "main.py")
        gtrees = ("src", "GtkHelper")

        failures, _, _ = run_check(tmp, gfiles, gtrees, allow)
        if failures:
            problems.append(f"a clean tree was reported red: {failures}")

        rogue = backend / "rogue.py"
        rogue.write_text("import json\n\n\ndef g(p):\n    return json.load(open(p))\n")
        failures, _, _ = run_check(tmp, gfiles, gtrees, allow)
        if not any("rogue.py" in f for f in failures):
            problems.append(f"a planted bare json.load was NOT caught red: {failures}")
        rogue.unlink()

        # A `from json import load` also has to be caught.
        hidden = backend / "hidden.py"
        hidden.write_text("from json import load\n\n\ndef g(p):\n    return load(open(p))\n")
        failures, _, _ = run_check(tmp, gfiles, gtrees, allow)
        if not any("hidden.py" in f for f in failures):
            problems.append(f"a `from json import load` was NOT caught red: {failures}")
        hidden.unlink()

        # A stale allowlist entry -- the file no longer reads json -- is red.
        (backend / "settings_store.py").write_text("still_no_json = True\n")
        failures, _, _ = run_check(tmp, gfiles, gtrees, allow)
        if not any("settings_store.py" in f for f in failures):
            problems.append(f"a stale allowlist entry was NOT caught red: {failures}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print("settings-json ratchet self-test: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("settings-json ratchet self-test: PASS (planted reader, hidden import and stale entry all caught)")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv[1:]:
        return self_test()

    failures, files, calls = run_check(REPO_ROOT, GOVERNED_FILES, GOVERNED_TREES, ALLOWLIST)

    if failures:
        print(
            f"settings-json ratchet: {len(failures)} problem(s) "
            f"(the allowlist and the rules live in {THIS_SCRIPT}).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"settings-json ratchet: {calls} json.load/json.dump calls across {files} governed "
        f"files, all within the {len(ALLOWLIST)}-entry allowlist."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
