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

---

Shared HTTP client (issue #168): one process-wide requests.Session behind a
retrying adapter, used by every outbound fetch (store catalog, install
archives, asset URL imports, the About dialog's contributor list).

Two reasons it exists:

  * Connection reuse -- a store page load issues ~150 small requests to
    raw.githubusercontent.com. Top-level requests.get() builds a throwaway
    Session per call, i.e. a fresh TCP + TLS handshake per fetch; a pooled
    Session amortizes that across the whole catalog load.
  * A single place to configure retries. GitHub rate-limits by IP (429),
    and the store's only previous answer to that was the stale-cache
    fallback in StoreBackend.get_remote_file.

The session is created once and never mutated afterwards, which is what
makes sharing it across the store prepare pool, UI install threads and the
asset-manager worker thread safe (urllib3's connection pools and requests'
cookie jar are individually thread-safe; concurrent *reconfiguration* of a
Session is what isn't).
"""
import os
import threading

import requests
from requests.adapters import HTTPAdapter, Retry

# Connection-pool size for the shared session. Must stay >= the store's
# concurrent-fetch cap so all of its in-flight fetches can hold a live
# keep-alive connection; StoreBackend.MAX_CONCURRENT_REQUESTS is an alias of
# this value so the two cannot drift apart.
POOL_MAXSIZE = 10

_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """The process-wide session, built on first use.

    Retry policy: 2 retries (3 attempts total) on 429/502/503 with a 0.5s
    backoff factor, honoring a server-sent Retry-After. `raise_on_status`
    stays off deliberately -- after the retries are exhausted the FINAL
    response is handed back rather than raising, so every call site keeps the
    status handling (and, for the store, the stale-cache fallback) it had
    before it was routed through here.
    """
    global _session
    with _session_lock:
        if _session is None:
            retry = Retry(
                total=2,
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
    """GET `url` through the shared session.

    `timeout` is keyword-only and required on purpose: every HTTP call in
    this codebase passes an explicit timeout (a timeout-less request can hang
    its worker thread forever on a black-holed connection), and a required
    argument is the only way that invariant survives new call sites.
    """
    return get_session().get(url, timeout=timeout, stream=stream)


def download_to_file(url: str, target_path: str, *, timeout: float = 30, chunk_size: int = 8192) -> None:
    """Stream `url` into `target_path` through the shared session.

    Raises the usual requests exceptions on network errors AND on HTTP error
    statuses (an error page is never persisted as if it were the requested
    file), and never leaves a partial or zero-byte file behind: the target is
    removed again if anything fails mid-download.
    """
    directory = os.path.dirname(target_path)
    if directory != "":
        os.makedirs(directory, exist_ok=True)

    try:
        # Module-level get(), not _session.get(): tests that patch
        # http_client.get then cover this path too.
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
