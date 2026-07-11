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
import tempfile
import time

# Temps orphaned by a hard kill between write and rename are reaped on the
# next write for the same target once they're older than this (seconds).
STALE_TMP_MAX_AGE = 60 * 60


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
