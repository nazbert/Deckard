#!/usr/bin/env python3
"""Slot freeze for globals.py: the `gl` inventory may not grow unnoticed.

Run it locally the same way CI does, from anywhere:

    python scripts/check_gl_slots.py

Exit 0 means globals.py declares exactly the names the tables below list, and
every `gl.<name> = ...` in the governed trees targets a declared slot. Exit 1
prints one message per problem and names the fix.

WHAT THIS PROTECTS

`globals` is a module every part of the app imports, so any module can mint a
new process-wide slot with a single assignment and nothing notices -- two slots
reached the tree that way and stayed undeclared for months, one of them read by
nobody at all. A shared namespace that grows by accident is the substrate every
other coupling problem here grows on: an implicit dependency edge no reader can
see, no rename can follow, and no test can stub.

This check is the brake. It does not shrink the inventory and it does not stop
anyone from adding to it -- it makes adding a *decision*: two edits in one
change, visible in the diff, with a reviewer looking at the alternative.

THE ALTERNATIVE, WHICH IS THE DEFAULT

A new service does not need a slot. Build it as a local in
`main.create_global_objects` and pass it to whatever needs it -- the way the
page manager backend already takes the settings manager as a constructor
argument. The dependency then shows up in a signature, where it can be read,
renamed and substituted. A slot is for state that genuinely has no owner.

THE RULES

* DECLARATION PIN. The set of names globals.py binds at module scope must equal
  FROZEN_SLOTS + INCIDENTAL + IMPORTED + MACHINERY exactly. Both directions
  bite: a new declaration fails until a table lists it, and a table entry whose
  declaration is gone fails until the entry goes too.
* ASSIGNMENT PIN. In every governed file, each attribute store on the module
  alias (`gl.X = ...`, augmented, `for gl.X in ...`, `with ... as gl.X`) must
  name a FROZEN_SLOTS entry. Stores to IMPORTED/INCIDENTAL/MACHINERY names are
  as much a failure as stores to undeclared ones: those are not API. The one
  exception is MODULE_TYPE_STORES, granted per file and per attribute, for
  stores that reach the module object's own machinery rather than its
  inventory.
* `setattr(gl, ...)` and `delattr(gl, ...)` fail outright -- a computed slot
  name is invisible to this check, which is the whole reason to reject it. So
  does `del gl.X`: it makes the frozen inventory false at runtime.
* Attribute *reads* are not policed. What may be read from `gl`, and by which
  layer, is a separate question with a separate guard; this one is only about
  what exists and who creates it.
* Mutating a slot's contents (`gl.loggers[...] = ...`, appending to a queue) is
  not a store on `gl` and is not policed, deliberately: that is those slots'
  semantics, and the queues exist to be appended to. `gl.__dict__[...] = ...`
  rides the same allowance -- it reaches the module namespace rather than a
  slot's contents, but it is a subscript store like any other and this check
  does not see it. Nothing in the tree writes that way.

WHAT COUNTS AS A DECLARATION

Targets of assignments, annotated assignments, augmented assignments, `for`,
`with ... as`, walrus expressions and `type` aliases, plus imports and
module-scope `def`/`class` names -- collected over the module body at any
nesting depth, without descending into function, class or lambda bodies (names
bound in there are locals, not module attributes). The parts of a `def` that do
evaluate where the `def` sits -- decorators, default arguments, annotations,
base classes -- are still walked, because a walrus in one of them binds module
scope. Names a module-scope `del` removes are subtracted, which is why the
version-reading helper globals.py deletes after use is in no table.
`except ... as` names are deliberately not collected: Python deletes the handler
name when the handler exits, so such a name is never a module attribute.

`fallback_font` is declared the one way that has no name in the source:
globals.py's module `__getattr__` resolves it on first read and caches it with
`globals()["fallback_font"] = value`. A write into the namespace dict under a
literal key is collected like any other binding, so that slot is pinned in both
directions -- deleting the caching line fails the check until the table entry
goes too.

A guard that fails open is worse than no guard, because it reads as green while
covering nothing. So the check also fails when its own footing moves: a missing
or empty globals.py, a governed root that is not a directory (or a governed
file that is not a file), a symlinked directory under a root (the walk does not
descend into one, so every store inside it would go unseen), a file that will
not parse, a name listed in two tables at once, and any `import` or
`from ... import` STATEMENT naming globals that this check cannot resolve to an
alias. Each is a loud failure naming what to fix, never a silent skip.

WHAT THIS DOES NOT SEE, AND WHY THAT IS THE RIGHT LINE

The module can also be reached without an import statement --
`importlib.import_module("globals")`, `__import__("globals")`,
`sys.modules["globals"]` -- and an alias can be laundered through another name
(`x = gl`, `vars(gl)`, a helper that takes the module as a parameter). None of
those are detected. The threat model here is the well-meaning change, not the
determined one: a slot arrives because somebody needed one and reached for the
nearest surface, and every such arrival in this tree's history has been a plain
`gl.name = value`. A check that chased every indirection would have to
approximate the interpreter, and would still lose. This one makes the ordinary
path visible and leaves the exotic ones to review.

Nor does it see what a class installed through a MODULE_TYPE_STORES exemption
goes on to do. A swapped `__class__` decides how every read of the module
behaves, and a `__getattr__` on it that caches its answer into the module
namespace mints a name that outlives the restore -- a slot arriving by
behaviour rather than by assignment. That is why the exemption names one file
and one attribute at a time: the trust is granted in review, to a specific
class somebody has read, not to a shape.

Stdlib only, no imports beyond it: this runs in CI's bare python:3.13-slim
image alongside compileall, with nothing installed.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GLOBALS_MODULE = "globals.py"
THIS_SCRIPT = "scripts/check_gl_slots.py"

# What the assignment pin walks. Wider than mypy's roots on purpose: main.py
# does most of the assigning, and the tests are included because a scenario that
# invents a slot to stand something up is inventing a slot.
#
# What stays ungoverned, and knowingly: the remaining root-level modules
# (appinfo.py, rebrand_migration.py, globals.py itself), which import before
# globals exists or are globals; scripts/, which is tooling that never runs in
# the app process; and third-party plugins, which live outside the tree
# entirely and are a compatibility surface rather than something to police.
# Widening these tuples is the fix if any of that stops being true.
GOVERNED_FILES = ("main.py", "autostart.py", "cli_args.py")
GOVERNED_TREES = ("src", "GtkHelper", "locales", "tests")

# ─────────────────────────────────────────────────────────────────────────────
# THE FROZEN INVENTORY
#
# Every process-wide slot, and nothing else. This is the only table the
# assignment pin accepts as a store target. The note on each entry says what
# lives there, not who happens to read it.
# ─────────────────────────────────────────────────────────────────────────────
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

    # Flags and queues: shared state with no owning object.
    "threads_running": "cleared on shutdown so worker loops exit",
    "screen_locked": "current lock state, written by the lock screen detector",
    "showed_donate_window": "one-shot latch for the donation prompt",
    "loggers": "named logger registry, keyed by plugin",
    "app_loading_finished_tasks": "zero-arg deliveries queued until App activates",
    "api_page_requests": "page changes parked by the CLI until a deck appears",
    "api_state_requests": "state changes parked by the CLI until a deck appears",
}

# Names that leak out of a block in globals.py's body and become module
# attributes by accident: Python does not scope a `with` or an `if`. They are
# not API, nothing may assign them, and they exist here only so the declaration
# pin balances. Deleting the leak is a fine change -- delete the entry with it.
INCIDENTAL: frozenset[str] = frozenset({
    "settings",           # static settings dict, from the `with open(...)` block
    "f",                  # the file handle that block opened
    "top_level_folder",   # parent of PLUGIN_DIR, bound only on the nix path
})

# Modules and type names globals.py imports. Runtime imports are ordinary
# attributes of the module (`gl.os` resolves); the rest are bound only under
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

# Module machinery: not state, not imported, not assignable.
MACHINERY: frozenset[str] = frozenset({
    "__getattr__",   # serves and caches fallback_font on first read
})

# Attribute stores that reach the module OBJECT's own machinery rather than the
# inventory globals.py declares, listed per file that makes one. Rebinding
# `__class__` to a ModuleType subclass is the documented way to give a module
# custom attribute behaviour, and the store does not by itself add a name to
# globals.py's declared inventory -- the declaration pin reads that source and
# is unaffected. What the installed class then DOES is outside this check's
# reach; see WHAT THIS DOES NOT SEE. Which is why the exemption is granted by
# file and by attribute, in review, rather than to a shape: `gl.__dict__ = {}`
# in the same file would still fail, and so would `gl.__class__` anywhere else.
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
    """Parse `path`, or record why it could not be parsed. Never silently skips."""
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


# ── declaration pin ──────────────────────────────────────────────────────────

def declared_names(tree: ast.Module) -> set[str]:
    """Names `tree` binds at module scope, minus the ones it deletes again.

    Descends through every compound statement (`if`, `with`, `try`, `match`,
    loops) because a binding inside one is still module scope, and stops at
    `def`/`class`/`lambda` boundaries because a binding inside one of those is
    not -- except for a `global` declaration, which reaches back out to module
    scope from wherever it sits, and for a `globals()["name"] = ...` write,
    which is how the lazy slot is cached.
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
            # `globals()["name"] = value` -- a module attribute written through
            # the namespace dict, the one binding with no name in the source.
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
        """Collect `:=` targets from expressions that evaluate in module scope.

        A walrus binds where its expression is evaluated, which for one inside a
        comprehension is the scope *around* the comprehension -- so a module-scope
        comprehension can mint a module attribute. A lambda body is its own scope,
        and is the one expression this does not enter.
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
            # The body binds locals, not module attributes -- but decorators,
            # default arguments, annotations and base classes are evaluated
            # where the statement sits, so a walrus in one binds module scope.
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
            target(node.name)   # `type X = ...` binds X like an assignment
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    # capture patterns bind through `name`, mapping rests through `rest`
                    captured = getattr(pattern, "name", None)
                    captured = captured or getattr(pattern, "rest", None)
                    if isinstance(captured, str):
                        bound.add(captured)
        elif isinstance(node, ast.Delete):
            for element in node.targets:
                if isinstance(element, ast.Name):
                    deleted.add(element.id)

        # Generic descent, so a statement shape this check has never seen still
        # gets walked. `except ... as` names are not collected: Python deletes
        # the handler name when the handler exits.
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
    """Reject tables that overlap or that have emptied out, and hold
    MODULE_TYPE_STORES to the two properties that keep it from becoming a way
    to exempt slots.

    MODULE_TYPE_STORES is not one of TABLES: those answer what a name globals.py
    declares IS, and this one names attributes of the module object, which
    globals.py does not declare at all. So the overlap check above never reaches
    it, and without the two below, widening it by one plain name would quietly
    excuse every store of a real slot.
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
    """Pin globals.py's module-scope names to the tables. Returns what it found."""
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


# ── assignment pin ───────────────────────────────────────────────────────────

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
            dirnames[:] = keep   # prune in place: os.walk descends into what is left

            found.extend(here / name for name in filenames if name.endswith(".py"))

    return sorted(found)


def module_aliases(
    tree: ast.Module, where: str, problems: list[tuple[int, str]]
) -> set[str]:
    """Names this file binds the `globals` module to.

    The house form is `import globals as gl`, and it is the only form that lets
    the assignment pin see a store. The other two forms are reported rather than
    skipped: skipping is how a guard silently stops covering a file.
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
    """Pin every `gl` attribute store in one file to FROZEN_SLOTS. Returns the
    count, and records into `type_stores_used` which MODULE_TYPE_STORES
    exemptions this file actually needed."""
    where = relative(path)
    type_stores = MODULE_TYPE_STORES.get(where, frozenset())
    tree = parse(path, failures)
    if tree is None:
        return 0

    # Collected with their line numbers and reported in source order: the walk
    # visits by node shape, and findings that read out of order read as noise.
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
    """Drop a MODULE_TYPE_STORES entry once the store it excuses is gone.

    An exemption that outlives its store is a standing permission nobody is
    using, and a standing permission is how the next one arrives unremarked --
    the same staleness pressure the read-surface scenario puts on its own
    per-file exemption.
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
