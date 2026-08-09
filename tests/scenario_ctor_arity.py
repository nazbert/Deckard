"""
Regression test for constructor calls that cannot bind their class's own
`__init__` signature (gl#190).

`src/app.py`'s `show_permissions()` built
`FlatpakPermissionRequestWindow(application=..., main_window=...)` while the
window required `command` and `description` too -- a guaranteed TypeError on
the one path (flatpak) the dialog exists for, invisible to every other
environment. A window nobody can construct is exactly the class of defect
that never shows up in a headless suite and never shows up on the
maintainer's machine either, so pin it statically instead: no display, no
GTK import, no window ever opened -- just AST.

Two checks:

  1. A repo-wide scan: for every call `Foo(...)` where `Foo` is a class
     defined exactly once in the scanned tree, every parameter of `Foo`'s
     own `__init__` that has no default must be bound by the call, either
     positionally or by keyword. Calls that splat (`*args` / `**kwargs`)
     and classes whose `__init__` takes `*args` are skipped -- their
     binding is not decidable here.

  2. The specific #190 shape: `FlatpakPermissionRequestWindow` must stay
     constructible from `application` + `main_window` alone, because
     `app.show_permissions()` (the generic, command-less request) has
     nothing else to pass.

Scope note: this only sees classes defined in this repo and called by
simple name. It is a tripwire for the "signature drifted away from its
callers" defect, not a type checker.
"""
import ast
import os

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SCAN_ROOTS = ["src", "GtkHelper"]
SCAN_FILES = ["main.py", "globals.py"]

# Directory *names* pruned from the walk -- never matched against the
# absolute path, which may itself contain e.g. ".claude" when the harness
# runs inside an agent worktree.
SKIP_PARTS = (".venv", ".claude", "__pycache__")

# Tripwire: if a refactor moves the tree and the walk silently stops finding
# files, the scan would "pass" while checking nothing.
MIN_SCANNED_FILES = 150
MIN_CHECKED_CALLS = 200

PERMISSION_WINDOW = "FlatpakPermissionRequestWindow"
PERMISSION_WINDOW_FILE = os.path.join(
    "src", "windows", "Permissions", "FlatpakPermissionRequest.py")
# What app.show_permissions() has to offer -- see the module docstring.
GENERIC_REQUEST_ARGS = {"application", "main_window"}


def iter_python_files():
    for rel in SCAN_FILES:
        path = os.path.join(REPO_ROOT, rel)
        if os.path.isfile(path):
            yield path

    for root_name in SCAN_ROOTS:
        root = os.path.join(REPO_ROOT, root_name)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def get_init(class_node: ast.ClassDef):
    init = None
    for sub in class_node.body:
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == "__init__":
            init = sub  # last definition wins, like Python itself
    return init


def signature(init: ast.FunctionDef):
    """(positional param names, required param names) or None if undecidable."""
    args = init.args
    if args.vararg is not None:
        return None  # *args swallows any arity

    positional = args.posonlyargs + args.args
    positional = positional[1:]  # drop self
    n_defaults = len(args.defaults)
    required = [a.arg for a in positional[:len(positional) - n_defaults]] if n_defaults \
        else [a.arg for a in positional]
    required += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]

    return [a.arg for a in positional], required


def collect(trees: dict) -> tuple[dict, set]:
    classes: dict[str, tuple[str, ast.ClassDef]] = {}
    duplicates: set[str] = set()
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name in classes:
                    # Same name in two modules: a bare `Foo(...)` call site
                    # cannot be resolved to one of them here.
                    duplicates.add(node.name)
                classes[node.name] = (path, node)
    return classes, duplicates


def find_offenders(trees: dict, classes: dict, duplicates: set):
    offenders: list[str] = []
    checked = 0

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name in duplicates or name not in classes:
                continue

            class_path, class_node = classes[name]
            init = get_init(class_node)
            if init is None:
                continue
            sig = signature(init)
            if sig is None:
                continue
            positional, required = sig

            # Splatted calls: the bound set is unknowable statically.
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue

            checked += 1
            bound = set(positional[:len(node.args)])
            bound |= {kw.arg for kw in node.keywords}
            missing = [p for p in required if p not in bound]
            if missing:
                offenders.append(
                    f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}: "
                    f"{name}(...) never binds {missing} -- required by "
                    f"{name}.__init__ in "
                    f"{os.path.relpath(class_path, REPO_ROOT)}"
                )

    return offenders, checked


def check_repo_scan() -> None:
    trees: dict[str, ast.AST] = {}
    for path in sorted(iter_python_files()):
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            trees[path] = ast.parse(source, filename=path)
        except SyntaxError as e:
            raise AssertionError(f"{path} does not parse: {e}") from None

    assert len(trees) >= MIN_SCANNED_FILES, (
        f"only {len(trees)} python files scanned (expected >= "
        f"{MIN_SCANNED_FILES}) -- the walk is not seeing the tree, so a "
        f"clean result would prove nothing"
    )

    classes, duplicates = collect(trees)
    offenders, checked = find_offenders(trees, classes, duplicates)

    assert checked >= MIN_CHECKED_CALLS, (
        f"only {checked} constructor calls were decidable (expected >= "
        f"{MIN_CHECKED_CALLS}) -- the matcher stopped matching"
    )
    assert not offenders, (
        "constructor call(s) that cannot bind their own __init__ "
        "(TypeError at runtime):\n  " + "\n  ".join(offenders)
    )

    print(f"PASS: {checked} constructor calls across {len(trees)} files bind "
          f"every required __init__ parameter")


def check_generic_permission_request() -> None:
    path = os.path.join(REPO_ROOT, PERMISSION_WINDOW_FILE)
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    class_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == PERMISSION_WINDOW:
            class_node = node
    assert class_node is not None, f"{PERMISSION_WINDOW} not found in {PERMISSION_WINDOW_FILE}"

    init = get_init(class_node)
    assert init is not None, f"{PERMISSION_WINDOW} has no __init__"
    sig = signature(init)
    assert sig is not None, f"{PERMISSION_WINDOW}.__init__ takes *args -- rewrite this check"
    _, required = sig

    unmet = [p for p in required if p not in GENERIC_REQUEST_ARGS]
    assert not unmet, (
        f"{PERMISSION_WINDOW}.__init__ requires {unmet}, which the generic "
        f"(command-less) permission request in app.show_permissions() has no "
        f"value for -- give them defaults, or that path raises TypeError "
        f"under flatpak again (#190)"
    )

    print(f"PASS: {PERMISSION_WINDOW} stays constructible from "
          f"{sorted(GENERIC_REQUEST_ARGS)} alone")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_ctor_arity")

    check_repo_scan()
    check_generic_permission_request()

    print("PASS: scenario_ctor_arity")


if __name__ == "__main__":
    main()
