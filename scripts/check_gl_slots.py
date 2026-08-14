#!/usr/bin/env python3
"""Slot freeze for globals.py. The gl inventory must not grow unnoticed.

Run it the same way CI does, from anywhere:

    python scripts/check_gl_slots.py

Exit 0 means globals.py declares exactly the names the tables below list, and
every gl.<name> = ... in the governed trees targets a declared slot. Exit 1
prints one message per problem and names the fix.

Every part of the app imports globals, so any module can mint a process-wide
slot with one assignment and nothing reports it. Such a slot is an implicit
dependency edge that no reader sees, no rename follows, and no test stubs.
This check does not shrink the inventory and does not stop an addition. It
makes an addition two edits in one diff, with a reviewer present.

A new service needs no slot. Build it as a local in main.create_global_objects
and pass it to whatever needs it, the way the page manager backend takes the
settings manager as a constructor argument. The dependency then appears in a
signature, where a reader finds it, a rename follows it, and a test substitutes
it. A slot is for state with no owner.

The declaration pin. The set of names globals.py binds at module scope must
equal FROZEN_SLOTS plus INCIDENTAL plus IMPORTED plus MACHINERY, exactly. Both
directions fail. A new declaration fails until a table lists it, and a table
entry whose declaration is gone fails until the entry goes too.

The assignment pin. In every governed file, each attribute store on the module
alias must name a FROZEN_SLOTS entry. That covers gl.X = ..., the augmented
form, for gl.X in ..., and with ... as gl.X. A store to an IMPORTED, INCIDENTAL
or MACHINERY name fails too, because those names are not API. The one exception
is MODULE_TYPE_STORES, granted per file and per attribute, for a store that
reaches the machinery of the module object rather than its inventory.

setattr(gl, ...) and delattr(gl, ...) fail, because a computed slot name is
invisible to this check. del gl.X fails too, because it makes the frozen
inventory false at runtime.

This check does not police attribute reads. What may be read from gl, and by
which layer, is a separate question with a separate guard.

This check does not police a change to the contents of a slot, such as
gl.loggers[...] = ... or an append to a queue. Those are the semantics of those
slots. gl.__dict__[...] = ... reaches the module namespace, but it is a
subscript store as well, and this check does not see it. Nothing in the tree
writes that way.

A declaration is a target of an assignment, an annotated assignment, an
augmented assignment, a for, a with ... as, a walrus, or a type alias, plus an
import and a module-scope def or class name. The walk collects them over the
module body at any nesting depth, and it stops at a function, class or lambda
body, because a name bound in there is a local. It still walks the parts of a
def that evaluate where the def sits, which are the decorators, the default
arguments, the annotations and the base classes, because a walrus in one of
them binds module scope. It subtracts the names that a module-scope del
removes, which is why the version-reading helper that globals.py deletes after
use is in no table. It does not collect an except ... as name, because Python
deletes the handler name when the handler exits.

fallback_font has no name in the source. The module __getattr__ of globals.py
resolves it on the first read and caches it with
globals()["fallback_font"] = value. The walk collects a write into the
namespace dict under a literal key, so that slot is pinned in both directions.
A deleted caching line fails the check until the table entry goes too.

A guard that fails open reads as green and covers nothing, so this check also
fails when its own footing moves. Each of these is a loud failure that names
the fix, and never a silent skip: a missing or empty globals.py, a governed
root that is not a directory, a governed file that is not a file, a symlinked
directory under a root, because the walk does not descend into one and every
store inside it would go unseen, a file that does not parse, a name in two
tables at once, and an import statement that names globals and that this check
cannot resolve to an alias.

This check does not see the module reached without an import statement, such as
importlib.import_module("globals"), __import__("globals") or
sys.modules["globals"]. It does not see an alias laundered through another
name, such as x = gl, vars(gl), or a helper that takes the module as a
parameter. The threat model is the well-meaning change. Every slot that arrived
in this tree arrived as a plain gl.name = value. A check that chased every
indirection would approximate the interpreter and would still lose. This one
makes the ordinary path visible and leaves the rare ones to review.

This check also does not see what a class installed through a
MODULE_TYPE_STORES exemption then does. A swapped __class__ decides how every
read of the module behaves, and a __getattr__ on it that caches its answer into
the module namespace mints a name that outlives the restore. That is why the
exemption names one file and one attribute at a time, and why review grants it
to a class somebody read, not to a shape.

This module imports from the standard library only, because it runs in the bare
python:3.13-slim image of CI beside compileall, with nothing installed.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GLOBALS_MODULE = "globals.py"
THIS_SCRIPT = "scripts/check_gl_slots.py"

# What the assignment pin walks. It is wider than the mypy roots, because
# main.py does most of the assigning, and because a test scenario that invents
# a slot still invents a slot.
#
# Three groups stay ungoverned. The remaining root-level modules, appinfo.py,
# rebrand_migration.py and globals.py, import before globals exists, or are
# globals. scripts/ is tooling that never runs in the app process. Third-party
# plugins live outside the tree and form a compatibility surface. Widen these
# tuples when any of that stops being true.
GOVERNED_FILES = ("main.py", "autostart.py", "cli_args.py")
GOVERNED_TREES = ("src", "GtkHelper", "locales", "tests")

# The frozen inventory holds every process-wide slot, and nothing else. It is
# the only table the assignment pin accepts as a store target. The note on each
# entry says what lives there, not who reads it.
FROZEN_SLOTS: dict[str, str] = {
    # Import-time constants and paths, resolved in globals.py's own body.
    "MAIN_PATH": "install root, assigned by main.py before anything reads it",
    "VAR_APP_PATH": "per-app data root: flatpak's ~/.var/app/<id>, else XDG",
    "STATIC_SETTINGS_FILE_PATH": "static settings file, the data-path override",
    "DATA_PATH": "data root actually in use, after argv and static settings",
    "PLUGIN_DIR": "plugin directory, overridable by env for nix packaging",
    "top_level_dir": "directory globals.py lives in, i.e. the repo/install root",
    "video_extensions": "recognised video suffixes",
    "image_extensions": "recognised raster image suffixes",
    "svg_extensions": "recognised vector image suffixes",
    "app_version": "upstream-aligned version plugin compatibility gates compare against",
    "exact_app_version_check": "whether plugin version gates demand an exact match",
    "deckard_version": "fork release version from the VERSION file, shown to users",
    "logs": "bounded ring buffer of recent log records",
    "logs_lock": "guards the log ring against concurrent iteration",
    "release_notes": "release notes markup shown after an update",
    "fallback_font": "lazily resolved fallback font, served by the module __getattr__",

    # Services, published by main.create_global_objects unless noted.
    "lm": "locale manager",
    "media_manager": "media manager",
    "asset_manager_backend": "asset store backend",
    "asset_manager": "asset manager window, only while it is open",
    "page_manager_window": "page manager window, only while it is open",
    "page_manager": "page manager backend",
    "gnome_extensions": "GNOME extension bridge",
    "settings_manager": "settings manager",
    "app": "the App instance, absent until it activates",
    "deck_manager": "deck manager, published by main() before the loop starts",
    "plugin_manager": "plugin manager",
    "icon_pack_manager": "icon pack manager",
    "wallpaper_pack_manager": "wallpaper pack manager",
    "sd_plus_bar_wallpaper_pack_manager": "SD+ touchscreen wallpaper pack manager",
    "store_backend": "plugin store backend",
    "store": "store window, only while it is open",
    "notify": "desktop notification facade",
    "pyro_daemon": "dead slot: never set, never read",
    "signal_manager": "app-wide signal bus",
    "window_grabber": "active-window watcher",
    "wayland": "Wayland session bridge; write-only, kept for parity",
    "lock_screen_detector": "lock screen detector",
    "presence_monitor": "user presence/quiescence monitor",
    "flatpak_permission_manager": "flatpak permission manager",
    "tray_icon": "tray icon",

    # Flags and queues. This is shared state with no owning object.
    "threads_running": "cleared on shutdown so worker loops exit",
    "screen_locked": "current lock state, written by the lock screen detector",
    "showed_donate_window": "one-shot latch for the donation prompt",
    "loggers": "named logger registry, keyed by plugin",
    "app_loading_finished_tasks": "zero-arg deliveries queued until App activates",
    "api_page_requests": "page changes parked by the CLI until a deck appears",
    "api_state_requests": "state changes parked by the CLI until a deck appears",
}

# Names that leak out of a block in the globals.py body and become module
# attributes. Python does not scope a with or an if. They are not API, nothing
# may assign them, and they exist here only so the declaration pin balances.
# Delete the entry together with the leak.
INCIDENTAL: frozenset[str] = frozenset({
    "settings",           # static settings dict, from the with open(...) block
    "f",                  # the file handle that block opened
    "top_level_folder",   # parent of PLUGIN_DIR, bound only on the nix path
})

# Modules and type names that globals.py imports. A runtime import is an
# ordinary attribute of the module, so gl.os resolves. The rest bind only under
# TYPE_CHECKING and never exist at runtime. Neither kind is a slot.
IMPORTED: frozenset[str] = frozenset({
    # runtime
    "json", "os", "sys", "threading", "appinfo", "deque", "log", "argparser",
    "rebrand_migration", "Callable", "TYPE_CHECKING", "Any",
    # TYPE_CHECKING only
    "Pyro5", "App", "LocaleManager", "AssetManagerBackend", "AssetManager",
    "MediaManager", "PageManagerBackend", "SettingsManager", "DeckManager",
    "PluginManager", "IconPackManager", "WallpaperPackManager",
    "SDPlusBarWallpaperPackManager", "StoreBackend", "Notify", "SignalManager",
    "WindowGrabber", "Wayland", "GnomeExtensions", "Store",
    "FlatpakPermissionManager", "PageManager", "LockScreenManager",
    "PresenceMonitor", "TrayIcon", "Logger",
})

# Module machinery. It is not state, not imported, and not assignable.
MACHINERY: frozenset[str] = frozenset({
    "__getattr__",   # serves and caches fallback_font on first read
})

# Attribute stores that reach the machinery of the module object rather than
# the inventory that globals.py declares, listed per file that makes one. A
# rebind of __class__ to a ModuleType subclass is the documented way to give a
# module custom attribute behaviour, and the store adds no name to the declared
# inventory, so the declaration pin is unaffected. What the installed class
# then does is outside the reach of this check, so the exemption names one file
# and one attribute at a time. gl.__dict__ = {} in the same file still fails,
# and so does gl.__class__ in any other file.
MODULE_TYPE_STORES: dict[str, frozenset[str]] = {
    # Installs a recording module to pin which slots the render engine reads,
    # and restores the original class in a finally.
    "tests/scenario_engine_gl_surface.py": frozenset({"__class__"}),
}

TABLES: tuple[tuple[str, frozenset[str]], ...] = (
    ("FROZEN_SLOTS", frozenset(FROZEN_SLOTS)),
    ("INCIDENTAL", INCIDENTAL),
    ("IMPORTED", IMPORTED),
    ("MACHINERY", MACHINERY),
)

ADDING_A_SLOT = (
    f"Adding one is two edits in one change: declare it in {GLOBALS_MODULE}, and add "
    f"it to FROZEN_SLOTS in {THIS_SCRIPT}. Prefer not adding one: a new service is "
    "constructor-injected by default -- build it as a local in "
    "main.create_global_objects and pass it to whatever needs it, the way the page "
    "manager backend takes the settings manager."
)


def relative(path: Path) -> str:
    """Repo-relative posix path, or the absolute one if it sits outside the repo."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse(path: Path, failures: list[str]) -> ast.Module | None:
    """Parse path, or record why the parse failed. This never skips silently."""
    try:
        source = path.read_bytes()
    except OSError as error:
        failures.append(
            f"{relative(path)}: cannot be read ({error}). This check covers every file "
            "under the governed roots, so one it cannot read is one it cannot report on."
        )
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        failures.append(
            f"{relative(path)}:{error.lineno}: does not parse ({error.msg}). Fix the "
            "syntax -- an unparseable file is unchecked, not clean."
        )
        return None


# Declaration pin

def declared_names(tree: ast.Module) -> set[str]:
    """Names that tree binds at module scope, minus the ones it deletes.

    The walk enters every compound statement, because a binding inside one is
    still module scope. It stops at a def, class or lambda boundary. It makes
    two exceptions. A global declaration reaches module scope from anywhere,
    and a globals()["name"] = ... write caches the lazy slot.
    """
    bound: set[str] = set()
    deleted: set[str] = set()

    def target(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                target(element)
        elif isinstance(node, ast.Starred):
            target(node.value)
        elif isinstance(node, ast.Subscript):
            # globals()["name"] = value writes a module attribute through the
            # namespace dict. It is the one binding with no name in the source.
            call = node.value
            key = node.slice
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "globals"
                and isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            ):
                bound.add(key.value)

    def walrus(node: ast.AST) -> None:
        """Collect walrus targets from expressions that evaluate at module scope.

        A walrus binds where its expression evaluates. For a walrus inside a
        comprehension that is the scope around the comprehension, so a
        module-scope comprehension can mint a module attribute. A lambda body is
        its own scope, and this walk does not enter it.
        """
        for field, value in ast.iter_fields(node):
            if isinstance(node, ast.Lambda) and field == "body":
                continue
            for item in (value if isinstance(value, list) else [value]):
                if not isinstance(item, ast.AST) or isinstance(item, ast.stmt):
                    continue   # statements are descend()'s job
                if isinstance(item, ast.NamedExpr):
                    bound.add(item.target.id)
                walrus(item)

    def descend(node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            # The body binds locals, not module attributes. The decorators,
            # default arguments, annotations and base classes evaluate where the
            # statement sits, so a walrus in one binds module scope.
            walrus(node)
            return

        walrus(node)

        if isinstance(node, ast.Assign):
            for element in node.targets:
                target(element)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            target(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.TypeAlias):
            target(node.name)   # type X = ... binds X like an assignment
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    # capture patterns bind through name, mapping rests through rest
                    captured = getattr(pattern, "name", None)
                    captured = captured or getattr(pattern, "rest", None)
                    if isinstance(captured, str):
                        bound.add(captured)
        elif isinstance(node, ast.Delete):
            for element in node.targets:
                if isinstance(element, ast.Name):
                    deleted.add(element.id)

        # Generic descent, so the walk reaches a statement shape this check has
        # never seen. It does not collect an except ... as name, because Python
        # deletes the handler name when the handler exits.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                descend(child)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                for statement in child.body:
                    descend(statement)

    for statement in tree.body:
        descend(statement)

    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            # Reaches module scope from inside a def, so it is a declaration
            # wherever it sits.
            bound.update(node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for element in targets:
                if isinstance(element, ast.Subscript):
                    target(element)   # the globals()[...] form, at any depth

    return bound - deleted


def check_tables(failures: list[str]) -> None:
    """Reject a table that overlaps another one, or that is empty.

    This also holds MODULE_TYPE_STORES to the two properties that keep it from
    exempting a slot. MODULE_TYPE_STORES is not one of TABLES, because TABLES
    say what a name in globals.py is, and this one names attributes of the
    module object. The overlap check therefore misses it, and one plain name
    added to it would excuse every store of a real slot.
    """
    exempted = {name for names in MODULE_TYPE_STORES.values() for name in names}
    for name in sorted(exempted):
        if not (name.startswith("__") and name.endswith("__")):
            failures.append(
                f"{THIS_SCRIPT}: MODULE_TYPE_STORES lists `{name}`, which is not a "
                "dunder. This table is only for attributes that reach the module "
                "object's own machinery; any other name here exempts a slot store "
                "from the freeze, which is the one thing it must never do."
            )
    for table_name, entries in TABLES:
        for shared in sorted(exempted & entries):
            failures.append(
                f"{THIS_SCRIPT}: `{shared}` is listed in both MODULE_TYPE_STORES and "
                f"{table_name}. A name globals.py declares is part of the inventory, "
                "and pinning who assigns the inventory is the whole check."
            )

    for index, (name, entries) in enumerate(TABLES):
        if not entries:
            failures.append(
                f"{THIS_SCRIPT}: {name} is empty. An empty table cannot pin anything; "
                "if the population it described is really gone, delete the table and "
                "its use rather than leaving it looking like a check."
            )
        for other_name, other_entries in TABLES[index + 1:]:
            for shared in sorted(entries & other_entries):
                failures.append(
                    f"{THIS_SCRIPT}: `{shared}` is listed in both {name} and "
                    f"{other_name}. One name, one table -- the tables answer what a "
                    "name is, and two answers is no answer."
                )


def check_declarations(failures: list[str]) -> set[str]:
    """Pin the module-scope names of globals.py to the tables.

    Returns the names it found.
    """
    path = REPO_ROOT / GLOBALS_MODULE
    if not path.is_file():
        failures.append(
            f"{GLOBALS_MODULE}: missing. It is the module this check exists to freeze, "
            f"so its absence is a failure, not an empty run. If it moved, point "
            f"{THIS_SCRIPT} at the new path in the same change."
        )
        return set()

    tree = parse(path, failures)
    if tree is None:
        return set()
    if not tree.body:
        failures.append(
            f"{GLOBALS_MODULE}: parsed to an empty module. Every slot would read as "
            "deleted, so this check refuses to report on it."
        )
        return set()

    declared = declared_names(tree)
    listed = {name for _, entries in TABLES for name in entries}

    for name in sorted(declared - listed):
        failures.append(
            f"{GLOBALS_MODULE}: declares `{name}`, which no table in {THIS_SCRIPT} "
            f"lists. The `gl` inventory is frozen: it grows by deliberate edit, never "
            f"by arriving. {ADDING_A_SLOT}"
        )

    for name in sorted(listed - declared):
        table = next(table for table, entries in TABLES if name in entries)
        failures.append(
            f"{THIS_SCRIPT}: lists `{name}` in {table}, but {GLOBALS_MODULE} no longer "
            "declares it. Delete the entry in the same change that removed the "
            "declaration -- a table entry with nothing behind it makes the freeze "
            "describe a module that does not exist."
        )

    return declared


# Assignment pin

def governed_files(failures: list[str]) -> list[Path]:
    """Every governed file, sorted. Reports anything that shrinks the sweep."""
    found: list[Path] = []

    for name in GOVERNED_FILES:
        path = REPO_ROOT / name
        if not path.is_file():
            failures.append(
                f"{name}: governed file is missing. It is one of the modules that "
                f"assigns `gl` slots, so an unswept one is a hole in the freeze. "
                f"Restore it, or update GOVERNED_FILES in {THIS_SCRIPT} in the same "
                "change that moves it."
            )
            continue
        found.append(path)

    for root in GOVERNED_TREES:
        base = REPO_ROOT / root
        if not base.is_dir():
            failures.append(
                f"{root}/: governed root is not a directory. Every `gl` assignment "
                f"under it would go unchecked, so this check refuses to report success. "
                f"Restore the tree, or update GOVERNED_TREES in {THIS_SCRIPT} in the "
                "same change that moves it."
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
                        f"{relative(child)}: symlinked directory under a governed root. "
                        "The walk does not descend into it, so every `gl` assignment "
                        "inside it would go unseen. Replace it with a real directory, "
                        "or teach this check to follow symlinks."
                    )
                    continue
                keep.append(name)
            dirnames[:] = keep   # prune in place, so os.walk skips what is gone

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def module_aliases(
    tree: ast.Module, where: str, problems: list[tuple[int, str]]
) -> set[str]:
    """Names that this file binds the globals module to.

    The house form is import globals as gl, and it is the only form that lets
    the assignment pin see a store. This reports the other two forms, because a
    skip is how a guard stops covering a file without a word.
    """
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "globals" and not alias.name.startswith("globals."):
                    continue
                if alias.asname:
                    aliases.add(alias.asname)       # the house form; several per file is fine
                else:
                    aliases.add("globals")
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `import globals` binds the module under "
                        "the same name as the builtin that returns a namespace dict, so "
                        "an assignment through it reads as neither. Write "
                        "`import globals as gl`, the form every other module uses and "
                        "the one this check resolves.",
                    ))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "globals":
                names = ", ".join(alias.name for alias in node.names)
                problems.append((
                    node.lineno,
                    f"{where}:{node.lineno}: `from globals import {names}` copies slot "
                    "values into local names, so later reads see whatever the value was "
                    "at import time rather than the slot -- and no store through such a "
                    "name is visible to this check. Write `import globals as gl` and go "
                    "through the module.",
                ))

    return aliases


def check_stores(path: Path, failures: list[str], type_stores_used: set) -> int:
    """Pin every gl attribute store in one file to FROZEN_SLOTS.

    Returns the count, and records into type_stores_used which
    MODULE_TYPE_STORES exemptions this file needed.
    """
    where = relative(path)
    type_stores = MODULE_TYPE_STORES.get(where, frozenset())
    tree = parse(path, failures)
    if tree is None:
        return 0

    # Collect the problems with their line numbers and report them in source
    # order. The walk visits by node shape, and out-of-order findings read as
    # noise.
    problems: list[tuple[int, str]] = []
    aliases = module_aliases(tree, where, problems)
    if not aliases:
        failures.extend(message for _, message in sorted(problems))
        return 0

    stores = 0

    def alias_attribute(node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in aliases:
                return node.attr
        return None

    def store(node: ast.expr, line: int) -> None:
        nonlocal stores
        name = alias_attribute(node)
        if name is not None:
            if name in type_stores:
                type_stores_used.add((where, name))
                return
            stores += 1
            if name not in FROZEN_SLOTS:
                problems.append((
                    line,
                    f"{where}:{line}: assigns `gl.{name}`, which is not a frozen slot. "
                    f"{ADDING_A_SLOT}",
                ))
            return
        if isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                store(element, line)
        elif isinstance(node, ast.Starred):
            store(node.value, line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for element in node.targets:
                store(element, node.lineno)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            store(node.target, node.lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            store(node.target, node.lineno)
        elif isinstance(node, ast.comprehension):
            store(node.target, node.target.lineno)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    store(item.optional_vars, node.lineno)
        elif isinstance(node, ast.Delete):
            for element in node.targets:
                name = alias_attribute(element)
                if name is not None:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `del gl.{name}` removes a declared slot "
                        "at runtime, which makes the frozen inventory describe a module "
                        "that no longer matches it. Assign the slot back to its empty "
                        "value instead.",
                    ))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("setattr", "delattr") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in aliases:
                    problems.append((
                        node.lineno,
                        f"{where}:{node.lineno}: `{node.func.id}(gl, ...)` writes a slot "
                        "under a name this check cannot see, which is exactly what the "
                        "freeze exists to prevent. Name the slot in the source "
                        f"(`gl.thing = ...`). {ADDING_A_SLOT}",
                    ))

    failures.extend(message for _, message in sorted(problems))
    return stores


def check_type_store_use(type_stores_used: set, failures: list[str]) -> None:
    """Drop a MODULE_TYPE_STORES entry once its store is gone.

    An exemption that outlives its store is a standing permission that nobody
    uses, and a standing permission is how the next one arrives unremarked.
    """
    for where, names in sorted(MODULE_TYPE_STORES.items()):
        for name in sorted(names):
            if (where, name) not in type_stores_used:
                failures.append(
                    f"{THIS_SCRIPT}: MODULE_TYPE_STORES exempts `gl.{name}` in {where}, "
                    "which no longer stores it (or is no longer governed). Drop the "
                    "entry rather than leaving an exemption standing for a write "
                    "nothing makes."
                )


def main() -> int:
    failures: list[str] = []

    check_tables(failures)
    declared = check_declarations(failures)

    files = governed_files(failures)
    type_stores_used: set = set()
    stores = sum(check_stores(path, failures, type_stores_used) for path in files)
    check_type_store_use(type_stores_used, failures)

    if failures:
        print(
            f"gl slot freeze: {len(failures)} problem(s) "
            f"(the inventory and the rules live in {THIS_SCRIPT}).",
            file=sys.stderr,
        )
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"gl slot freeze: {len(FROZEN_SLOTS)} slots frozen, {len(declared)} declarations "
        f"pinned in {GLOBALS_MODULE}, {stores} assignments across {len(files)} governed "
        "files all within the table."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
