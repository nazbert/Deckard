"""
Coverage for the destructive filesystem half of the store install path
(gl#62), exercised WITHOUT network. Every existing store scenario pins
result *propagation* (gl#26 / gl#7 / gl#23); nothing exercised
download_repo's own extract/cleanup contract or install_plugin's
delete-only-after-a-good-download behavior.

download_repo is the single choke point every install_*/download_repo caller
funnels through. Its contract (StoreBackend.py ~921-1006):

  1. A network fault mid-stream removes the partial/zero-byte .zip from the
     cache instead of leaving it to poison the next run.
  2. A corrupt/truncated archive (unpack raises) returns NoConnectionError
     and never leaves the extracted temp folder behind.
  3. An archive with unsafe (path-traversal) member names is refused before
     anything is unpacked.
  4. The DESTINATION directory is reset only on the success path -- a fault
     before/at extraction leaves any pre-existing install untouched. This is
     the install_plugin-shaped safety that install_icon/wallpaper/sd_plus
     lack (that gap is B-06, pinned separately in
     scenario_store_b06_pack_survival.py).

The `requests.get` call is monkeypatched to serve bytes from a local file
(or raise) -- no socket is ever opened. Extraction/cleanup run against the
real shutil/zipfile machinery in the isolated temp DATA_PATH.
"""
import asyncio
import io
import os
import zipfile

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

import src.backend.Store.StoreBackend as store_mod
from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError


CACHE_DIR = os.path.join(gl.DATA_PATH, "cache")


def _force_release_download_path() -> None:
    """The harness runs with --devel (fixtures.py), which routes download_repo
    into the git-clone branch. The real store install path on end-user
    installs is the requests+zip branch (devel off). Pin parse_args().devel to
    False for this scenario so we exercise the real download_repo code, not the
    dev-only clone_repo shortcut."""
    real_parse = gl.argparser.parse_args

    def parse_no_devel(*args, **kwargs):
        ns = real_parse(*args, **kwargs)
        ns.devel = False
        return ns

    gl.argparser.parse_args = parse_no_devel


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


class _FakeResponse:
    """Minimal stand-in for a streaming requests.Response context manager."""

    def __init__(self, chunks, raise_on_status=False, raise_at_chunk=None):
        self._chunks = chunks
        self._raise_on_status = raise_on_status
        # Index at which iter_content raises (0 = before any bytes are
        # written, i.e. a genuine zero-byte file; 1 = a partial file).
        self._raise_at_chunk = raise_at_chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self._raise_on_status:
            raise store_mod.requests.HTTPError("404 Client Error")

    def iter_content(self, chunk_size=8192):
        for i, chunk in enumerate(self._chunks):
            if self._raise_at_chunk is not None and i == self._raise_at_chunk:
                raise ConnectionError("connection reset mid-stream")
            yield chunk


def _install_fake_get(chunks, **kwargs):
    """Point requests.get (as referenced inside StoreBackend) at an in-memory
    response. Returns the previous callable so the caller can restore it."""
    prev = store_mod.requests.get

    def fake_get(url, stream=False, timeout=None):
        return _FakeResponse(chunks, **kwargs)

    store_mod.requests.get = fake_get
    return prev


def _restore_get(prev):
    store_mod.requests.get = prev


def _good_zip_bytes(top_folder="repo-abc", files=None) -> bytes:
    """A github-shaped archive: one top-level folder, then files under it."""
    files = files or {"manifest.json": b"{}", "main.py": b"print(1)\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top_folder}/", b"")
        for name, data in files.items():
            z.writestr(f"{top_folder}/{name}", data)
    return buf.getvalue()


def _traversal_zip_bytes(top_folder="repo-abc") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top_folder}/", b"")
        z.writestr(f"{top_folder}/ok.txt", b"ok")
        z.writestr("../escape.txt", b"pwned")  # escapes the extraction root
    return buf.getvalue()


def _chunk(data: bytes, n: int = 8192):
    return [data[i:i + n] for i in range(0, len(data), n)] or [b""]


def _cache_zips() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return [f for f in os.listdir(CACHE_DIR) if f.endswith(".zip")]


def _extract_folder_left(top_folder: str) -> bool:
    """Whether download_repo left its per-archive extraction temp folder
    (named after the zip's single top-level folder) behind in the cache.
    Other unrelated cache subdirs (e.g. `videos`) are ignored -- only the
    extraction residue for THIS archive is the litter under test."""
    return os.path.isdir(os.path.join(CACHE_DIR, top_folder))


REPO_URL = "https://github.com/test/Repo"
SHA = "a" * 40


def test_successful_install_cleans_cache_and_writes_version() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_Good")

    prev = _install_fake_get(_chunk(_good_zip_bytes()))
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert result == 200, f"a well-formed install must return 200, got {result!r}"
    assert os.path.isfile(os.path.join(dest, "manifest.json")), "files not moved into destination"
    assert os.path.isfile(os.path.join(dest, "VERSION")), "VERSION file not written"
    with open(os.path.join(dest, "VERSION")) as f:
        assert f.read() == SHA
    # No temp zip / extracted folder litter left in the cache.
    assert _cache_zips() == [], f"downloaded zip left in cache: {_cache_zips()}"
    assert not _extract_folder_left("repo-abc"), "extracted temp folder left in cache"
    print("PASS: successful install writes VERSION and leaves no cache litter")


def test_network_fault_midstream_removes_partial_zip() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_Partial")

    # Two chunks; the fake raises when the second is requested -- a partial
    # .zip is already on disk at that point.
    prev = _install_fake_get(_chunk(_good_zip_bytes(), n=64), raise_at_chunk=1)
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError), (
        f"a mid-stream network fault must surface as NoConnectionError, got {result!r}"
    )
    assert _cache_zips() == [], (
        f"partial/zero-byte archive left in cache after a failed download "
        f"(would poison the next run): {_cache_zips()}"
    )
    assert not os.path.exists(dest), (
        "destination was created despite the download never completing"
    )
    print("PASS: a mid-stream network fault removes the partial archive")


def test_http_error_before_open_creates_no_archive() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_404")

    # raise_for_status fires before open("wb"): no file is created at all.
    prev = _install_fake_get(_chunk(_good_zip_bytes()), raise_on_status=True)
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError)
    assert _cache_zips() == [], f"archive created despite an HTTP error: {_cache_zips()}"
    print("PASS: an HTTP error before the body opens no archive")


def test_fault_before_first_chunk_removes_zero_byte_archive() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_ZeroByte")

    # The file is open()'d, then iter_content raises before yielding any bytes
    # -- a genuine zero-byte archive on disk that the except-branch must reap.
    prev = _install_fake_get(_chunk(_good_zip_bytes()), raise_at_chunk=0)
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError)
    assert _cache_zips() == [], (
        f"zero-byte archive left in cache after a fault before the first "
        f"chunk: {_cache_zips()}"
    )
    print("PASS: a fault before the first chunk removes the zero-byte archive")


def test_corrupt_archive_returns_error_and_cleans_up() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_Corrupt")

    # Well-formed enough for get_main_folder_of_zip to name a folder, but the
    # bytes are truncated so shutil.unpack_archive raises mid-extraction.
    good = _good_zip_bytes(top_folder="repo-abc")
    corrupt = good[: len(good) // 2]  # truncated tail

    prev = _install_fake_get([corrupt])
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError), (
        f"a corrupt archive must surface as NoConnectionError, got {result!r}"
    )
    assert _cache_zips() == [], f"corrupt zip left in cache: {_cache_zips()}"
    assert not _extract_folder_left("repo-abc"), (
        "extracted temp folder left in cache after an extraction failure"
    )
    print("PASS: a corrupt archive is cleaned up and reported as failure")


def test_traversal_member_is_refused() -> None:
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_Traversal")

    prev = _install_fake_get(_chunk(_traversal_zip_bytes()))
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError), (
        f"an archive with a traversal member must be refused, got {result!r}"
    )
    # The escaping member must not have been written outside the cache.
    assert not os.path.exists(os.path.join(gl.DATA_PATH, "escape.txt")), (
        "a path-traversal member escaped the extraction root"
    )
    assert not os.path.exists(dest), "destination touched despite refusing the archive"
    print("PASS: a path-traversal archive member is refused before extraction")


def test_download_fault_leaves_existing_install_intact() -> None:
    """download_repo resets the destination only AFTER a good download+
    extract (StoreBackend.py ~978-983). A fault before that must leave a
    pre-existing install byte-for-byte intact. This is exactly the
    install_plugin-shaped safety the icon/wallpaper/sd_plus install_* wrappers
    do NOT have (they rmtree first -- B-06)."""
    sb = _make_backend()
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_Existing")
    os.makedirs(dest, exist_ok=True)
    sentinel = os.path.join(dest, "keep.txt")
    with open(sentinel, "w") as f:
        f.write("previous good install")

    prev = _install_fake_get(_chunk(_good_zip_bytes(), n=64), raise_at_chunk=1)
    try:
        result = asyncio.run(sb.download_repo(repo_url=REPO_URL, directory=dest, commit_sha=SHA))
    finally:
        _restore_get(prev)

    assert isinstance(result, NoConnectionError)
    assert os.path.isfile(sentinel), (
        "download_repo deleted the existing install before the download "
        "succeeded -- the pack is gone on failure"
    )
    with open(sentinel) as f:
        assert f.read() == "previous good install", "existing install was corrupted"
    print("PASS: a failed download leaves the existing install intact (download_repo)")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_install_fs")
    _force_release_download_path()
    test_successful_install_cleans_cache_and_writes_version()
    test_network_fault_midstream_removes_partial_zip()
    test_http_error_before_open_creates_no_archive()
    test_fault_before_first_chunk_removes_zero_byte_archive()
    test_corrupt_archive_returns_error_and_cleans_up()
    test_traversal_member_is_refused()
    test_download_fault_leaves_existing_install_intact()
    print("PASS: scenario_store_install_fs")


if __name__ == "__main__":
    main()
