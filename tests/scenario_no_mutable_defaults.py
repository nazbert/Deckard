"""
Regression test for mutable default arguments.

Python evaluates a list or dict default once, at definition time, and shares
it with every caller that omits the argument. The AST scan covers src/,
GtkHelper/ and main.py. Media uses eq=False, so two instances must hold
separate layers lists and keep identity == and hashability.
"""
import ast
import os

import fixtures  # must be first, to isolate DATA_PATH

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SCAN_ROOTS = ["src", "GtkHelper"]
SCAN_FILES = ["main.py"]

# Pruned from the walk below. Directory names only, never the absolute path,
# because the checkout path itself can contain ".claude" inside a worktree.
SKIP_PARTS = (".venv", ".claude", "__pycache__")

MUTABLE_NODES = (
    ast.List,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


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


def find_offenders() -> tuple[list[tuple[str, int, str]], int]:
    offenders: list[tuple[str, int, str]] = []
    scanned = 0

    for path in sorted(iter_python_files()):
        scanned += 1
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as e:
            raise AssertionError(f"could not parse {path}: {e}") from e

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            defaults = list(node.args.defaults) + list(node.args.kw_defaults)
            for default in defaults:
                if default is None:  # kw-only arg with no default
                    continue
                if isinstance(default, MUTABLE_NODES):
                    offenders.append(
                        (os.path.relpath(path, REPO_ROOT), default.lineno, node.name)
                    )

    return offenders, scanned


# Tripwire against a vacuous pass. Path filtering that matches a segment of
# the checkout's own absolute path degrades the scan to main.py alone. The
# tree holds about 220 scannable files; a much lower count means the walk
# broke.
MIN_SCANNED = 100


def check_ast_scan() -> bool:
    offenders, scanned = find_offenders()
    if offenders:
        print(f"FAIL: {len(offenders)} mutable default argument(s) found:")
        for rel_path, lineno, func_name in offenders:
            print(f"  {rel_path}:{lineno} {func_name}")
        return False

    if scanned < MIN_SCANNED:
        print(
            f"FAIL: only {scanned} files scanned (expected >= {MIN_SCANNED}) "
            f"-- the walk is broken, a clean result would be vacuous"
        )
        return False

    print(f"PASS: no mutable default arguments in {scanned} files "
          f"(src/, GtkHelper/, main.py)")
    return True


def check_media_defaults() -> bool:
    from src.backend.DeckManagement.Media.Media import Media

    a = Media()
    b = Media()

    if a.layers is b.layers:
        print("FAIL: two default-constructed Media objects share one layers list")
        return False

    sentinel = object()
    a.layers.append(sentinel)

    if b.layers:
        print(
            f"FAIL: appending to one Media's layers leaked into another: "
            f"{b.layers!r}"
        )
        return False

    if Media() == Media():
        print(
            "FAIL: Media gained field-equality -- the dataclass must use "
            "eq=False to keep identity semantics for plugins comparing with =="
        )
        return False

    try:
        hash(Media())
    except TypeError as e:
        print(
            f"FAIL: Media became unhashable ({e}) -- plugins storing Media in "
            f"a set/dict would break"
        )
        return False

    if (a.size, a.halign, a.valign) != (1.0, 0.0, 0.0):
        print(f"FAIL: Media field defaults changed: {a}")
        return False

    print("PASS: Media() instances get independent layers lists, == is identity, hashable")
    return True


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_no_mutable_defaults")

    ok = True
    ok &= check_ast_scan()
    ok &= check_media_defaults()

    if not ok:
        return 1

    print("PASS: scenario_no_mutable_defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
