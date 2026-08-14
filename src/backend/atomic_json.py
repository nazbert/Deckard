"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Shared durable-JSON writer. Stdlib only, because the migrators run before
# SettingsManager and the globals consumers exist, and must still import this.
import json
import os
import stat
import tempfile
import time

# A hard kill between the write and the rename orphans a temp file. The next
# write for the same target removes such a temp once it is older than this
# (seconds).
STALE_TMP_MAX_AGE = 60 * 60

# How many .corrupt sidecars to keep per primary file. They hold forensic
# copies rather than a version history. Without the cap, a file that corrupts
# again and again fills the config directory; three copies are enough for
# post-mortem.
CORRUPT_SIDECAR_KEEP = 3


def _process_umask() -> int:
    """Read the process umask and leave other threads alone.

    os.umask() reads only by setting, which zeroes the mask for the whole
    process, so a concurrent open() in another thread creates an unmasked
    file. Linux exposes the mask read-only in /proc/self/status. Elsewhere
    this falls back to the set-and-restore round trip.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _reap_stale_tmp_siblings(dir_path: str, target_basename: str) -> None:
    """Remove old orphaned temp files for this one target.

    A SIGKILL between the write and the rename leaks a temp file, and nothing
    else cleans the config directories. This removes only temps older than
    STALE_TMP_MAX_AGE, so a concurrent writer keeps its live temp. An unlink
    race with another reaper is harmless.
    """
    prefix = f".save-{target_basename}."
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if not (entry.startswith(prefix) and entry.endswith(".tmp")):
            continue
        path = os.path.join(dir_path, entry)
        try:
            if now - os.stat(path).st_mtime > STALE_TMP_MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def quarantine_corrupt_file(file_path: str) -> tuple[bool, str]:
    """Move a corrupt file aside, so the next save cannot overwrite the only
    surviving copy of the user's data.

    Returns (moved, dest). moved is False when the rename fails, after a
    read-only filesystem error, a permission error, or a concurrent
    quarantine that moved the file first. The primary is corrupt either way,
    so a caller must not gate its recovery on moved. dest is the sidecar on
    success and the untouched original path on failure.

    This takes the first free .corrupt, .corrupt.1, .corrupt.2 and so on, and
    keeps an earlier sidecar, so a second corruption cannot destroy the first
    forensic copy. os.replace of the chosen name stays atomic. Two threads can
    pick one free slot; the loser then overwrites an equally corrupt file, or
    finds the source gone and reports not-moved.
    """
    candidate = file_path + ".corrupt"
    n = 0
    # Bounded probe for a free sidecar name. If every slot is taken, keep the
    # plain name, where os.replace overwrites the oldest sidecar.
    while os.path.exists(candidate) and n < 10000:
        n += 1
        candidate = f"{file_path}.corrupt.{n}"
    try:
        os.replace(file_path, candidate)
        return True, candidate
    except OSError:
        # The rename failed, or a concurrent quarantine took the source. The
        # caller recovers from a backup either way.
        return False, file_path


def prune_corrupt_sidecars(primary_path: str, keep: int = CORRUPT_SIDECAR_KEEP,
                           protect: "str | list[str] | tuple[str, ...] | None" = None) -> list[str]:
    """Prune <primary_path>.corrupt sidecars, oldest first.

    A loader calls this immediately after it quarantines a file, never as a
    startup-wide filesystem walk. The scope is one primary file's own
    sidecars, so how often that file corrupts bounds the work. Only names the
    quarantine primitive writes count: <name>.corrupt, and <name>.corrupt.<n>
    with a purely numeric suffix. A user's own settings.json.corrupt.bak, and
    a directory of a matching name, stay. A user file named exactly
    <name>.corrupt reads as a sidecar and is prunable. Only this module writes
    that name next to a config file, so this documents the case instead of
    defending against it.

    protect names sidecars that survive whatever their age. Pass the sidecar
    the caller just created. Age is the sidecar's mtime, which os.replace
    carries over from the corrupt primary. That is the time the corrupt
    content was written, and not the time the sidecar appeared. A backup tool that restores a
    corrupt primary keeps its old mtime, so the fresh forensic copy sorts
    older than the sidecars already on disk and prunes first without protect.
    A protected entry still counts toward keep, so the surviving total holds
    and the next-oldest unprotected sidecar goes instead.

    Names hold no age order. quarantine_corrupt_file takes the first free slot,
    so the next corruption recycles a pruned .corrupt. The name breaks a tie
    between equal mtimes only, and on a coarse-granularity filesystem that
    tie-break can invert the true age. It gives determinism within one mtime.

    Returns the paths this call removed. It swallows every filesystem error,
    because a failed tidy-up must not break the load that triggered it.
    """
    keep = max(keep, 0)
    if protect is None:
        protected = set()
    elif isinstance(protect, str):
        protected = {os.path.abspath(protect)}
    else:
        protected = {os.path.abspath(p) for p in protect}
    dir_path = os.path.dirname(primary_path) or "."
    plain = os.path.basename(primary_path) + ".corrupt"
    numbered = plain + "."

    try:
        entries = os.listdir(dir_path)
    except OSError:
        return []

    sidecars: list[tuple[float, str, str]] = []
    for entry in entries:
        if entry != plain:
            suffix = entry[len(numbered):] if entry.startswith(numbered) else ""
            if not (suffix.isascii() and suffix.isdigit()):
                continue
        path = os.path.join(dir_path, entry)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        # The name is a stable tie-break for equal mtimes. The docstring
        # explains why it is no age order.
        sidecars.append((st.st_mtime, entry, path))

    n_remove = len(sidecars) - keep
    if n_remove <= 0:
        return []

    sidecars.sort()
    removed: list[str] = []
    for _mtime, _entry, path in sidecars:
        if n_remove <= 0:
            break
        if os.path.abspath(path) in protected:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
        # Count it either way. A concurrent pruner removes a sidecar just as
        # this call does.
        n_remove -= 1
    return removed


def atomic_write_json(file_path: str, data, indent: int | None = 4) -> None:
    """Write data as JSON to file_path atomically and durably.

    This serializes the payload into a temp file in the target's real
    directory, fsyncs it, chmods it (an existing file keeps its mode, a new
    file follows the process umask), and moves it in with os.replace(). It
    then fsyncs the directory, so the rename survives a crash. An interrupted
    write leaves no partial file at file_path, and a reader sees the old
    content or the new content.

    This follows a symlinked target. The write lands in the link's real file
    and the link stays a link. os.replace on the link path detaches a managed
    config such as stow or chezmoi. Resolving up front also keeps the temp
    file and the rename on one filesystem.
    """
    file_path = os.path.realpath(file_path)
    dir_path = os.path.dirname(file_path) or "."
    os.makedirs(dir_path, exist_ok=True)

    basename = os.path.basename(file_path)
    _reap_stale_tmp_siblings(dir_path, basename)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=f".save-{basename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600. Keep the existing file's mode, or apply the
        # umask-derived default for a new file. A hardcoded 0644 leaks plugin
        # settings, which hold API tokens, under a restrictive umask like 077.
        try:
            mode = os.stat(file_path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o666 & ~_process_umask()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)
        # fsync the directory so the rename itself becomes durable.
        try:
            dir_fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
