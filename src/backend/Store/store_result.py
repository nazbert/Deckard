"""The store's one typed error channel.

The fetch layer (request_from_url, get_remote_file, get_last_commit,
get_manifest, get_official_authors) RAISES ``StoreFetchError`` when a fetch
cannot be satisfied. The read boundary above it (get_all_*, the private
_get_assets_to_update) RETURNS a ``StoreResult`` -- an ``Ok`` carrying the
payload, or an ``Err`` naming why.

The payload lives INSIDE ``Ok``: a caller cannot iterate or index a
``StoreResult`` without narrowing it first, so a missed failure branch is a
mypy error the gate catches -- and, at runtime, an un-narrowed ``Err`` raises
``TypeError`` at first use instead of masquerading as an empty list. Neither
arm defines ``__bool__``: truthiness is not the protocol, narrowing is.

Stdlib only -- no globals, no GTK -- so any layer can name these types.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class StoreFetchError(Exception):
    """A fetch could not be satisfied: offline, rate-limited, or a non-200
    answer with no fresh cache to fall back to. Carries the url it was
    fetching and a short human-readable detail."""

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
    """A successful result carrying its payload. Read it via ``.value`` after
    narrowing away the ``Err`` arm."""

    value: T


@dataclass(frozen=True)
class Err:
    """A failed result naming the reason. Never carries a payload -- an
    un-narrowed use is meant to fail loudly, not read as empty data."""

    reason: ErrReason
    detail: str = ""


type StoreResult[T] = Ok[T] | Err
