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
# Shared durable-JSON writer. Deliberately dependency-free (stdlib only):
# the Migrators run before SettingsManager/globals consumers are ready and
# must still be able to import this.
import json
import os
import stat
import tempfile
import time

# Temps orphaned by a hard kill between write and rename are reaped on the
# next write for the same target once they're older than this (seconds).
STALE_TMP_MAX_AGE = 60 * 60

# How many ``.corrupt*`` sidecars to keep per primary file. They exist for
# post-mortem, not as a version history: a file that keeps getting corrupted
# would otherwise fill the config dir forever (issue #152), and three
# generations is already more than anyone reads.
CORRUPT_SIDECAR_KEEP = 3


def _process_umask() -> int:
    """Read the process umask without racing other threads where possible.

    os.umask() can only read by setting, which briefly zeroes the mask for
    the whole process -- a concurrent open() in another thread would then
    create files unmasked. On Linux, /proc/self/status exposes the mask
    read-only; fall back to the set/restore round-trip elsewhere.
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
    """Best-effort removal of old orphaned temp files for the SAME target
    (SIGKILL between write and rename leaks them; nothing else ever cleans
    the config dirs). Only temps older than STALE_TMP_MAX_AGE are touched,
    so a concurrent writer's live temp is never at risk; unlink races with
    other reapers are ignorable."""
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
    """Move a corrupt file aside instead of leaving it where the next save
    would overwrite the only surviving copy of the user's data.

    Returns ``(moved, dest)``:
      * ``moved`` -- True if the file was renamed aside, False if the rename
        failed (read-only fs, permissions, a concurrent quarantine that
        already moved it, ...). Callers must NOT gate recovery on this: a
        corrupt primary is corrupt whether or not the rename succeeded.
      * ``dest`` -- where the file now lives (the ``.corrupt*`` sidecar on
        success, the untouched original path on failure).

    A pre-existing ``<path>.corrupt`` is never clobbered -- a second
    corruption would otherwise destroy the first forensic copy. The first
    free ``.corrupt`` / ``.corrupt.1`` / ``.corrupt.2`` ... slot is used.
    os.replace of the chosen name is still atomic; the only race is two
    threads picking the same free slot, in which case the loser's replace
    overwrites an identical-fate corrupt file (harmless) or finds the source
    already gone (reported as not-moved).
    """
    candidate = file_path + ".corrupt"
    n = 0
    # Bounded probe for a free sidecar name; fall back to the plain name if
    # somehow every slot is taken (then os.replace overwrites the oldest).
    while os.path.exists(candidate) and n < 10000:
        n += 1
        candidate = f"{file_path}.corrupt.{n}"
    try:
        os.replace(file_path, candidate)
        return True, candidate
    except OSError:
        # Rename failed (or the source vanished under a concurrent
        # quarantine). Leave the caller to recover from a backup regardless.
        return False, file_path


def prune_corrupt_sidecars(primary_path: str, keep: int = CORRUPT_SIDECAR_KEEP,
                           protect: "str | list[str] | tuple[str, ...] | None" = None) -> list[str]:
    """Best-effort, oldest-first pruning of ``<primary_path>.corrupt*``.

    Call this from a loader immediately after it quarantined something --
    never as a startup-wide filesystem walk. The scope is exactly one primary
    file's own sidecars, so the work is bounded by how often that one file has
    been corrupt, and only names the quarantine primitive itself can produce
    are considered: ``<name>.corrupt`` and ``<name>.corrupt.<n>`` with a
    purely numeric suffix. A user's own ``settings.json.corrupt.bak`` (or a
    directory that happens to match) is never touched. A user-created regular
    file named exactly ``<name>.corrupt`` IS indistinguishable from our own
    sidecar and would be prunable -- unreachable in the layouts we own
    (nothing but this module writes that name next to a config file), so it is
    documented rather than defended against.

    ``protect`` names sidecars that must survive regardless of age -- pass the
    sidecar the caller just created. Age is the sidecar's mtime, which
    os.replace carries over from the corrupt PRIMARY, i.e. when that corrupt
    content was written. That is the interesting timestamp, but it is not the
    same as when the sidecar appeared: a corrupt primary restored with its
    mtime preserved (backup tools, ``os.rename`` of an old settings dir) is
    born older than sidecars already on disk, so without ``protect`` the
    freshly written forensic copy is the first thing pruned -- one line after
    the loader logged "preserved at". Protected entries still count toward
    ``keep``, so the surviving total is unchanged; the next-oldest unprotected
    sidecar goes instead.

    Names are NOT an age order: quarantine_corrupt_file takes the first free
    slot, so a pruned ``.corrupt`` is recycled by the next corruption. The
    name is used only as a tie-break for equal mtimes, and on a
    coarse-granularity filesystem that tie-break can therefore invert the true
    age (recycled ``.corrupt`` sorts before ``.corrupt.1``). It buys
    determinism, not correctness, and only within the same mtime.

    Returns the paths actually removed. Every filesystem error is swallowed:
    failing to tidy up must never break the load that triggered it.
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
        # Name as a stable tie-break for equal mtimes (see the docstring: it
        # buys determinism, not age correctness).
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
        # Counted either way: a sidecar that vanished under a concurrent
        # pruner is just as gone as one this call removed.
        n_remove -= 1
    return removed


def atomic_write_json(file_path: str, data, indent: int | None = 4) -> None:
    """
    Write ``data`` as JSON to ``file_path`` atomically and durably.

    The payload is serialized into a temp file in the destination's REAL
    directory, fsync'd, chmod'd (existing files keep their mode; new files
    honor the process umask like a plain open("w") would), and moved into
    place with os.replace(); the directory is then fsync'd so the rename
    itself survives a crash. An interrupted write therefore can never leave
    a truncated/partial file at ``file_path`` -- readers see either the old
    content or the new content, nothing in between.

    Symlinked targets are followed: the write lands in the link's real file
    and the link stays a link (os.replace on the link path itself would
    silently detach stow/chezmoi-style managed configs). Resolving up front
    also keeps the temp file and the rename on one filesystem.
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
        # mkstemp creates 0600; keep the existing file's mode, or apply the
        # umask-derived default for new files (hardcoding 0644 would leak
        # secret-bearing files -- plugin settings hold API tokens -- under a
        # restrictive umask such as 077).
        try:
            mode = os.stat(file_path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o666 & ~_process_umask()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)
        # fsync the directory so the rename itself is durable, not just data.
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
