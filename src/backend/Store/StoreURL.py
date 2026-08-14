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

from typing import NamedTuple

# Stored urls carry both spellings. The settings UI and the store catalogs
# hold github.com urls. build_url rewrites those to raw.githubusercontent.com
# before a fetch, and the rewritten urls come back through the cache keys.
_REPO_DOMAINS = ("github.com", "raw.githubusercontent.com")


class RepoRef(NamedTuple):
    """The owner and repository pair that every store url reduces to. It
    drives the API urls, the download urls, the cache keys and the displayed
    author."""
    user: str
    repo: str


def parse_repo_url(repo_url: object) -> RepoRef | None:
    """The single definition of "a usable store repository url".

    Returns None, and does not raise, for an empty field, a half-typed entry,
    a non-GitHub host, or a url with an owner but no repository. The settings
    UI refuses such an entry and the store paths skip it. Both sides must
    agree on "parseable", so neither side reimplements this function.
    """
    if not isinstance(repo_url, str):
        return None

    segments = repo_url.split("/")
    for domain in _REPO_DOMAINS:
        if domain in segments:
            index = segments.index(domain)
            break
    else:
        return None

    if index + 2 >= len(segments):
        return None

    user = segments[index + 1]
    repo = segments[index + 2]
    if not user or not repo:
        return None

    return RepoRef(user=user, repo=repo)
