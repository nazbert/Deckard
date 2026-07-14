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
pre-rename instance); it imports only `appinfo`, which is itself stdlib-only.

Design (docs/rename-deckard-plan.md, Phase 2):

- The WHOLE ~/.var/app/<id> dir moves (data/ + static/ + flatpak-era cache/
  and config/): static/settings.json can carry a custom data-path pointer
  and must relocate with the data.
- A compat symlink is left at the old root: live JSON (deck settings, pages
  incl. backups) embeds absolute paths into the old tree.
- rename+symlink cannot be atomic together, so a marker file tracks the
  state instead. The pending marker is written -- durably (fsync) -- into
  the OLD root immediately before the rename and therefore travels with it:
  any crash after the rename leaves a "symlink-pending" marker in the new
  tree, which the next start finishes. The durable write matters: a
  non-fsync'd marker can reach disk as a zero-length file after power loss
  (the rename metadata commits before the marker's data blocks), which reads
  as neither state and would be mistaken for a fresh install.
- Never merge, never delete user data: if both roots contain files beyond
  the known import-time skeleton, refuse loudly and let the user resolve it.

Stale pre-rename autostart entries are NOT handled here -- autostart.py owns
autostart filenames and removes the legacy ones on every launch (self-
healing), which this one-shot path could not be.
"""

import os
import shutil
import sys

import appinfo

OLD_ID = appinfo.OLD_APP_ID
NEW_ID = appinfo.APP_ID
OLD_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", OLD_ID))
NEW_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", NEW_ID))

MARKER_NAME = ".migrated-from-" + OLD_ID
_STATE_PENDING = "symlink-pending"
_STATE_COMPLETE = "complete"


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


def _write_marker(marker_path: str, state: str) -> bool:
    """Durably write the marker (fsync file + fsync dir + atomic replace).

    Returns True on success. Durability is load-bearing: a truncated or
    zero-length marker surviving a crash reads as neither state and would be
    treated as a fresh install, stranding the migrated data with no compat
    symlink.
    """
    tmp = marker_path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, (state + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, marker_path)
        dir_fd = os.open(os.path.dirname(marker_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except OSError as e:
        _log(f"could not durably write marker {marker_path} ({e})")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _old_instance_running() -> bool:
    try:
        import dbus
        return bool(dbus.SessionBus().name_has_owner(OLD_ID))
    except Exception as e:
        _log(f"could not probe the session bus for a pre-rename instance ({e}); assuming none")
        return False


def _data_override_active(argv: list[str]) -> bool:
    """True if a --data override is present, including the argparse
    abbreviations globals.py accepts (--dat, --data=..., etc.).

    We match any `--data`-prefixed flag of at least 5 chars ("--dat"). Very
    short prefixes ("--da") are treated as NOT an override on purpose: a
    false positive here would skip the migration and strand the real data
    unmigrated (the app boots factory-fresh), which is worse than the
    false-negative case, where the migration merely runs early during a
    genuinely custom-data session and is harmless to the moved tree.
    """
    for arg in argv[1:]:
        head = arg.split("=", 1)[0]
        if len(head) >= 5 and "--data".startswith(head):
            return True
    return False


def _is_skeleton(root: str) -> bool:
    """True only if `root` is a tree of empty directories -- the import-time
    makedirs residue globals.py / mp4_tile_cache.py leave behind.

    Any regular file, or ANY symlink anywhere below (a directory-symlink is
    reported by os.walk in dirnames, unfollowed; a file-symlink in
    filenames), means the tree holds real user state -- e.g. a data-
    relocation symlink -- and is not ours to delete.
    """
    if os.path.islink(root):
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        if filenames:
            return False
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                return False
    return True


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
    # A failed complete-marker write is self-healing: the marker that
    # travelled with the rename still reads "pending", so the next start
    # re-enters _finish_symlink via the pending branch and retries.
    _write_marker(marker_path, _STATE_COMPLETE)


def migrate(old_root: str = OLD_ROOT, new_root: str = NEW_ROOT,
            argv: list[str] | None = None, require_pre_globals: bool = True) -> None:
    if require_pre_globals and "globals" in sys.modules:
        raise AssertionError(
            "rebrand_migration.migrate() must run before `import globals` -- "
            "globals creates the data dir at import time and poisons the checks below"
        )

    argv = sys.argv if argv is None else argv
    if _data_override_active(argv):
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
        if os.path.islink(new_root):
            _abort(
                f"{new_root} is a symlink; the migration expects to create it as a "
                f"real directory. Resolve it manually, then restart."
            )
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # The pending marker travels with the rename (see module docstring). A
    # non-durable marker here is the one unrecoverable state, so refuse to
    # rename without it -- old_root is still intact, so this is a clean retry.
    if not _write_marker(os.path.join(old_root, MARKER_NAME), _STATE_PENDING):
        _abort(
            f"could not durably write the migration marker into {old_root}; refusing "
            f"to rename without it. Fix permissions/space on that path and restart."
        )
    try:
        os.rename(old_root, new_root)
    except OSError as e:
        _abort(f"could not move {old_root} -> {new_root}: {e}")
    _log(f"moved {old_root} -> {new_root}")
    _finish_symlink(old_root, new_root, marker_path)
