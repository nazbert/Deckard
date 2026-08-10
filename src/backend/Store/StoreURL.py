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

# Both spellings appear in stored urls: the settings UI and the store
# catalogs carry github.com repository urls, while build_url rewrites those
# to raw.githubusercontent.com before a fetch, and rewritten urls come back
# through the cache key helpers.
_REPO_DOMAINS = ("github.com", "raw.githubusercontent.com")


class RepoRef(NamedTuple):
    """The owner/repository pair every store url is reduced to: it drives
    the API urls, the download urls, the cache keys and the displayed
    author."""
    user: str
    repo: str


def parse_repo_url(repo_url: object) -> RepoRef | None:
    """The single definition of "a usable store repository url".

    Returns None instead of raising for anything that is not one -- an
    empty field, a half-typed entry, a non-GitHub host, a url that names an
    owner but no repository. Callers decide what to do with an unusable
    entry (the settings UI refuses to store it, the store paths skip it);
    the settings UI and the store MUST agree on what "parseable" means, so
    neither side may re-implement this.
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
