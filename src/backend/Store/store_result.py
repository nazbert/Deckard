"""The store's one typed error channel.

The fetch layer (request_from_url, get_remote_file, get_last_commit,
get_manifest, get_official_authors) raises StoreFetchError when it cannot
satisfy a fetch. The read boundary above it (get_all_*, the private
_get_assets_to_update) returns a StoreResult: an Ok that carries the payload,
or an Err that names the reason.

Ok holds the payload, so a caller must narrow the result before it iterates or
indexes it. A missed failure branch is then a mypy error, and an un-narrowed
Err raises TypeError at first use instead of reading as an empty list. Neither
arm defines __bool__. This module imports stdlib only, so any layer can name
these types.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class StoreFetchError(Exception):
    """A fetch failed, from no connection, a rate limit, or a non-200 answer
    with no fresh cache to fall back to. Carries the url and a short
    detail."""

    def __init__(self, url: str, detail: str) -> None:
        super().__init__(f"{detail} ({url})")
        self.url = url
        self.detail = detail


class ErrReason(enum.Enum):
    """Why a read-boundary call failed, at the granularity a caller acts on."""

    NO_CONNECTION = "no_connection"   # no store reachable / catalog fetch failed
    INVALID_ASSET = "invalid_asset"   # unsafe id, missing url, staged-manifest mismatch
    INSTALL_FAILED = "install_failed"  # git / archive / ref hard failure


@dataclass(frozen=True)
class Ok[T]:
    """A successful result that carries its payload. Read .value after you
    narrow the Err arm away."""

    value: T


@dataclass(frozen=True)
class Err:
    """A failed result that names the reason. It carries no payload, so an
    un-narrowed use fails loudly."""

    reason: ErrReason
    detail: str = ""


type StoreResult[T] = Ok[T] | Err
