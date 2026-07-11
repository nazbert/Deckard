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


def atomic_write_json(file_path: str, data, indent: int | None = 4) -> None:
    """
    Write ``data`` as JSON to ``file_path`` atomically and durably.

    The payload is serialized into a temp file in the destination directory,
    fsync'd, chmod'd to the destination's existing mode (0644 for new files),
    and moved into place with os.replace(); the directory is then fsync'd so
    the rename itself survives a crash. An interrupted write therefore can
    never leave a truncated/partial file at ``file_path`` -- readers see
    either the old content or the new content, nothing in between.
    """
    dir_path = os.path.dirname(file_path) or "."
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".save-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; keep the existing file's mode
        try:
            mode = os.stat(file_path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
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
