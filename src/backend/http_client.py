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

The shared HTTP client is one process-wide requests.Session behind a retrying
adapter. Every outbound fetch uses it, which covers the store catalog, the
install archives, the asset URL imports, and the contributor list of the About
dialog.

The session gives connection reuse. A store page load issues about 150 small
requests to raw.githubusercontent.com. The top-level requests.get() builds a
throwaway Session per call, which costs a fresh TCP and TLS handshake per
fetch. A pooled Session spreads one handshake across the catalog load.

The session also holds the one retry policy. GitHub rate-limits by IP with a
429, and the store answers that with the stale-cache fallback in
StoreBackend.get_remote_file.

This module builds the session once and never mutates it, so the store
prepare pool, the UI install threads and the asset-manager worker thread can
share it. The connection pools of urllib3 and the cookie jar of requests are
each thread-safe; a concurrent reconfiguration of a Session is not.
"""
import os
import threading

import requests
from requests.adapters import HTTPAdapter, Retry

# Connection-pool size for the shared session. It must stay at or above the
# store's concurrent-fetch cap, so every in-flight fetch holds a live
# keep-alive connection. StoreBackend.MAX_CONCURRENT_REQUESTS aliases this
# value, so the two cannot drift apart.
POOL_MAXSIZE = 10

_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """The process-wide session, built on first use.

    The retry policy is 2 retries (3 attempts) on 429, 502 and 503, with a
    0.5s backoff factor, and it obeys a server-sent Retry-After. raise_on_status
    stays off, so an exhausted retry budget returns the final response instead
    of raising. Each call site then keeps its own status handling, and the
    store keeps its stale-cache fallback.

    connect=0 takes connect errors out of the budget that total covers. This
    policy retries a status. A retry of a failed CONNECT gains nothing,
    because a black-holed or down host stays down for those seconds, and it
    costs three times the wall clock on every offline failure, so a 10s asset
    download becomes a 31s block. KeyGrid's GTK drop handler calls
    HelperMethods.download_file synchronously on the main thread. Read errors
    stay retryable, because those are transient.
    """
    global _session
    with _session_lock:
        if _session is None:
            retry = Retry(
                total=2,
                connect=0,
                backoff_factor=0.5,
                status_forcelist=(429, 502, 503),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=POOL_MAXSIZE)
            session = requests.Session()
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _session = session
    return _session


def get(url: str, *, timeout: float, stream: bool = False) -> requests.Response:
    """GET url through the shared session.

    timeout is keyword-only and required, so every HTTP call passes an
    explicit timeout. A request without a timeout parks its worker thread on a
    black-holed connection, and a required argument holds that rule for a new
    call site too.
    """
    return get_session().get(url, timeout=timeout, stream=stream)


def download_to_file(url: str, target_path: str, *, timeout: float = 30, chunk_size: int = 8192) -> None:
    """Stream url into target_path through the shared session.

    Raises the usual requests exceptions on a network error and on an HTTP
    error status, so an error page never lands on disk as the requested file.
    It leaves no partial or zero-byte file, because a failure mid-download
    removes the target again.
    """
    directory = os.path.dirname(target_path)
    if directory != "":
        os.makedirs(directory, exist_ok=True)

    try:
        # Call the module-level get(), so a test that patches http_client.get
        # covers this path too.
        with get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(target_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
    except BaseException:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise
