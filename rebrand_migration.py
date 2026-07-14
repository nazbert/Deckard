"""
One-time whole-directory migration of the app's var-app tree from the
pre-rename id (com.core447.StreamController) to the Deckard id.

Hard constraint: migrate() must run before `import globals` (and before
anything that imports it). globals.py resolves DATA_PATH and os.makedirs()
it at *import* time -- on every invocation, including CLI early-return runs
-- which would create an empty skeleton under the NEW id and poison the
"does the new tree exist" check below. main.py calls migrate() right after
the patcher, before its main import block. This module is stdlib-only for
the same reason (dbus is imported lazily, and only to probe for a live
pre-rename instance).

Design (docs/rename-deckard-plan.md, Phase 2):

- The WHOLE ~/.var/app/<id> dir moves (data/ + static/ + flatpak-era cache/
  and config/): static/settings.json can carry a custom data-path pointer
  and must relocate with the data.
- A compat symlink is left at the old root: live JSON (deck settings, pages
  incl. backups) embeds absolute paths into the old tree.
- rename+symlink cannot be atomic together, so a marker file tracks the
  state instead. The pending marker is written into the OLD root immediately
  before the rename and therefore travels with it: any crash after the
  rename leaves a "symlink-pending" marker in the new tree, which the next
  start finishes. (The only unrecoverable window is a crash between the
  marker write and the rename itself -- two adjacent syscalls -- which
  degrades to a plain unmigrated state and is retried in full.)
- Never merge, never delete user data: if both roots contain files beyond
  the known import-time skeleton, refuse loudly and let the user resolve it.
"""

import os
import shutil
import sys

OLD_ID = "com.core447.StreamController"
NEW_ID = "io.github.nazbert.Deckard"
OLD_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", OLD_ID))
NEW_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", NEW_ID))

MARKER_NAME = ".migrated-from-" + OLD_ID
_STATE_PENDING = "symlink-pending"
_STATE_COMPLETE = "complete"

# Autostart entries written under the old identity. The app-written one
# ("StreamController.desktop", autostart.py pre-rename) would relaunch an
# old-identity build at every login; the portal-era one is a flatpak
# remnant no code path ever removed on native installs.
_STALE_AUTOSTART_NAMES = ("StreamController.desktop", OLD_ID + ".desktop")


def _log(msg: str) -> None:
    # The logger is not configured yet at this point in startup (and must not
    # be: its sinks would open files inside the tree being renamed).
    print(f"[rebrand-migration] {msg}", file=sys.stderr)


def _abort(msg: str) -> None:
    _log("FATAL: " + msg)
    raise SystemExit(1)


def _read_marker(marker_path: str) -> str | None:
    try:
        with open(marker_path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_marker(marker_path: str, state: str) -> None:
    try:
        with open(marker_path, "w") as f:
            f.write(state + "\n")
    except OSError as e:
        _log(f"could not write marker {marker_path} ({e}); state will be re-derived next start")


def _old_instance_running() -> bool:
    try:
        import dbus
        return bool(dbus.SessionBus().name_has_owner(OLD_ID))
    except Exception as e:
        _log(f"could not probe the session bus for a pre-rename instance ({e}); assuming none")
        return False


def _is_skeleton(root: str) -> bool:
    """True if `root` holds only (nested) empty directories.

    globals.py and mp4_tile_cache.py os.makedirs() the data tree at import
    time on every invocation, so a new-id root consisting purely of empty
    dirs is machine residue, not user state. Any regular file or symlink
    anywhere below means it is not ours to delete.
    """
    for _dirpath, _dirnames, filenames in os.walk(root):
        if filenames:
            return False
    return True


def _cleanup_stale_autostart() -> None:
    autostart_dir = os.path.expanduser(os.path.join("~", ".config", "autostart"))
    for name in _STALE_AUTOSTART_NAMES:
        path = os.path.join(autostart_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                _log(f"removed stale autostart entry {path}")
            except OSError as e:
                _log(f"could not remove stale autostart entry {path}: {e}")


def _finish_symlink(old_root: str, new_root: str, marker_path: str) -> None:
    """Create (or verify) the compat symlink, then mark the migration done."""
    if os.path.lexists(old_root):
        if os.path.islink(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            pass  # already in place
        else:
            _log(
                f"{old_root} reappeared as a real directory or foreign link while the "
                f"compat symlink was pending; leaving migration pending. Remove it "
                f"manually so the symlink to {new_root} can be created."
            )
            return
    else:
        try:
            os.symlink(new_root, old_root)
            _log(f"compat symlink {old_root} -> {new_root}")
        except OSError as e:
            _log(f"could not create compat symlink ({e}); will retry next start")
            return
    _write_marker(marker_path, _STATE_COMPLETE)
    _cleanup_stale_autostart()


def migrate(old_root: str = OLD_ROOT, new_root: str = NEW_ROOT,
            argv: list[str] | None = None, require_pre_globals: bool = True) -> None:
    if require_pre_globals and "globals" in sys.modules:
        raise AssertionError(
            "rebrand_migration.migrate() must run before `import globals` -- "
            "globals creates the data dir at import time and poisons the checks below"
        )

    argv = sys.argv if argv is None else argv
    if "--data" in argv or any(a.startswith("--data=") for a in argv):
        _log("--data override active; skipping data-dir migration")
        return

    marker_path = os.path.join(new_root, MARKER_NAME)
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # Crashed (or failed) between rename and symlink on a previous start.
        _finish_symlink(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return  # fresh install -- nothing to migrate

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            # Our compat link, but the marker is missing (e.g. marker write
            # failed earlier): data is already at the new root.
            _write_marker(marker_path, _STATE_COMPLETE)
            _cleanup_stale_autostart()
            return
        _abort(
            f"{old_root} is a symlink but does not resolve to {new_root} (broken or "
            f"foreign). Refusing to touch it -- resolve it manually, then restart."
        )

    # old_root is a real directory holding the pre-rename data.
    if _old_instance_running():
        _abort(
            f"a pre-rename instance still owns {OLD_ID} on the session bus. Quit the "
            f"running StreamController first, then start Deckard again. Renaming the "
            f"data dir under a live instance would split its writes across two trees."
        )

    if os.path.lexists(new_root):
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # The pending marker travels with the rename (see module docstring).
    _write_marker(os.path.join(old_root, MARKER_NAME), _STATE_PENDING)
    try:
        os.rename(old_root, new_root)
    except OSError as e:
        _abort(f"could not move {old_root} -> {new_root}: {e}")
    _log(f"moved {old_root} -> {new_root}")
    _finish_symlink(old_root, new_root, marker_path)
