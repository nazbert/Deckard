"""Shared build/promote/decode-ahead discipline for video-backed tile
caches (docs/memory-footprint-impl-plan.md P2.1).

`Mp4FrameCache` is extracted from `background_video_cache.py`'s
`BackgroundVideoCache` -- the source video is decoded once (a `cv2.VideoWriter`
mp4v encode, atomically promoted with `os.replace`), and every frame after
that is a cheap decode out of the small canvas/tile-resolution mp4 instead of
holding raw frame data in RAM. `BackgroundVideoCache` (background_video_cache.py)
re-parents onto this class with no behavior change: it stays a single
instance that is both the builder and the only consumer, build interleaved
with playback ticks, exactly like today.

`KeyVideoCache` below is the same discipline used differently: many
InputVideo instances can reference the same (source, tile size, saturation),
so sharing one cache INSTANCE across them was tried and rejected (the build
loop requires monotonically increasing frame requests -- interleaved
consumers abort the writer -- and post-build their independent wall-clock
timelines seek-thrash the shared capture, measured 0.05->0.92 ms/frame). The
module-level registry below instead shares the cache *file*: exactly one
detached builder thread per (md5, size, saturation) decodes the source and
encodes the tile mp4 independently of playback ticks; every consumer
(`acquire()`) gets its own `KeyVideoCache` reader with its own
`cv2.VideoCapture` and its own last-frame memo, decoding straight from the
source until the builder promotes, then switching over.
"""
import hashlib
import os
import threading

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

import globals as gl

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)


# --------------------------------------------------------------------- #
# Source-hash memo
# --------------------------------------------------------------------- #

_md5_memo_lock = threading.Lock()
_md5_memo: dict[tuple[str, int, float], str] = {}


def get_video_md5(path: str) -> str:
    """(path, size, mtime) -> md5, memoized.

    Both cache classes historically hashed the whole source file in their
    constructor on every page-switch/InputVideo-construction -- cheap once,
    expensive when repeated. The registry key below hashes on every
    acquire(), which would multiply that cost across every consumer of a
    shared video if it weren't memoized.
    """
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    with _md5_memo_lock:
        cached = _md5_memo.get(key)
    if cached is not None:
        return cached

    md5 = hashlib.md5()
    with open(path, "rb") as f:
        block = f.read(2 ** 16)
        while len(block) != 0:
            md5.update(block)
            block = f.read(2 ** 16)
    digest = md5.hexdigest()

    with _md5_memo_lock:
        _md5_memo[key] = digest
    return digest


def sat_suffix(saturation: float) -> str:
    """Two-decimal fixed encoding (e.g. 1.30 -> ".sat130"); empty at the
    default factor so plain "{md5}.mp4" caches stay valid and no
    enhance/mode-conversion work happens at 1.0."""
    return (
        "" if abs(saturation - 1.0) <= 0.001
        else f".sat{int(round(saturation * 100))}"
    )


# --------------------------------------------------------------------- #
# Mp4FrameCache
# --------------------------------------------------------------------- #

class Mp4FrameCache:
    """Builds (or reuses) a per-(source, out_size, saturation) mp4 that
    decodes faster than the source and holds no per-frame data in RAM.

    An instance is either the *builder* (`is_builder=True`, the default --
    decodes the source and writes every frame to a tmp mp4 that is
    atomically promoted on completion) or a plain *reader*
    (`is_builder=False` -- never writes; decodes whichever of {promoted
    cache, source} is currently available). `BackgroundVideoCache` uses one
    instance as both (single consumer, build interleaved with playback
    ticks -- unchanged from today). The `KeyVideoCache` registry below
    splits the two roles: one detached builder thread, N per-consumer
    readers.
    """

    # Forward jumps up to this many frames are bridged by decoding and
    # discarding (cheaper than a container seek at tile/canvas resolution);
    # anything larger, or backward, is a real seek.
    MAX_DECODE_AHEAD = 30

    def __init__(self, source_path: str, out_size: tuple[int, int], saturation: float = 1.0,
                 cache_path: str = None, is_builder: bool = True) -> None:
        self.lock = threading.Lock()

        self.source_path = source_path
        self.out_size = out_size
        self.saturation = saturation
        self._sat_suffix = sat_suffix(saturation)
        self.is_builder = is_builder

        self.video_md5 = get_video_md5(source_path)

        self.cache_path = cache_path or self._default_cache_path()
        cache_dir = os.path.dirname(self.cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        # Unique per instance: two builders for the same key writing
        # concurrently must not collide on the same temp file
        # (os.replace makes last-wins safe if they ever did).
        self._writer_tmp_path = os.path.join(
            cache_dir,
            f"{os.path.basename(self.cache_path)}.{os.getpid()}-{id(self):x}.tmp.mp4",
        )

        self._complete = False
        self._cache_cap: cv2.VideoCapture = None
        self._cache_pos = 0  # index of the next frame _cache_cap will return
        self._last_entry: tuple[int, object] = None
        self.last_payload = None  # last good decode, served over a transient failure

        self.cap: cv2.VideoCapture = None
        self._writer: cv2.VideoWriter = None
        self._frames_written = 0
        self.last_frame_index = -1  # source decode position while building/reading

        self.n_frames = 0

        if not self._open_existing_cache():
            self._open_source()

    # --- overridable hooks -------------------------------------------------

    def _default_cache_path(self) -> str:
        raise NotImplementedError

    def _payload_from_bgr(self, frame_bgr: np.ndarray):
        """Convert one target-resolution BGR frame into what get_frame()
        returns. Default: a single RGB PIL image -- right for key/dial
        tiles, decoded straight at tile resolution. BackgroundVideoCache
        overrides this to crop the canvas into per-key tiles (+ strip)."""
        return Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))

    def _fallback_payload(self):
        """Used when there is no decoded frame at all yet (first request
        during build, or an unrecoverable early failure) and no previous
        payload to repeat."""
        return None

    def _on_promoted(self) -> None:
        """Hook fired whenever this instance transitions to `_complete`
        (existing cache found at startup, or a fresh build just promoted).
        No-op by default; BackgroundVideoCache uses it to purge the legacy
        pickle cache format."""
        pass

    def _writer_enabled(self) -> bool:
        """Whether a builder instance should actually open a VideoWriter.
        Default True: KeyVideoCache's registry already gates
        `performance.cache-videos` once at acquire() time, before a builder
        is ever constructed, so its builder instances don't need to re-check
        it. BackgroundVideoCache is a single self-contained instance that
        decides for itself, so it overrides this to read the live setting."""
        return True

    # --- setup ---------------------------------------------------------

    def _open_cache_capture(self) -> cv2.VideoCapture:
        # A tile/canvas-resolution stream decodes at thousands of fps
        # single-threaded; the default lets FFmpeg spawn a 16-thread frame
        # pool per capture, which is wasteful at this resolution.
        return cv2.VideoCapture(self.cache_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])

    def _open_existing_cache(self) -> bool:
        if not os.path.isfile(self.cache_path):
            return False
        cap = self._open_cache_capture()
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else 0
        if n_frames <= 0:
            cap.release()
            log.warning(f"Removing unreadable video cache {self.cache_path}")
            try:
                os.remove(self.cache_path)
            except OSError:
                pass
            return False
        self._cache_cap = cap
        self._cache_pos = 0
        self.n_frames = n_frames
        self._complete = True
        self._on_promoted()
        log.info(f"Using cached tile video ({n_frames} frames): {self.cache_path}")
        return True

    def _open_source(self) -> None:
        # The builder decodes as fast as possible on its own thread; a plain
        # reader is cheap at tile size and shouldn't spin up extra threads
        # per consumer.
        threads = 4 if self.is_builder else 1
        self.cap = cv2.VideoCapture(self.source_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, threads])
        self.n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not self.is_builder or not self._writer_enabled():
            return
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        writer = cv2.VideoWriter(self._writer_tmp_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, self.out_size)
        if writer.isOpened():
            self._writer = writer
        else:
            log.warning(f"Could not open tile cache writer for {self.source_path}; playing uncached")

    # --- frame access ----------------------------------------------------

    def get_frame(self, n: int):
        if not self._complete:
            self._maybe_adopt_shared_cache()
        with self.lock:
            if self._complete:
                payload = self._get_cached_frame(n)
            else:
                payload = self._decode_source_frame(n)
        if payload is not None:
            self.last_payload = payload
            return payload
        # Keep showing the last good frame over a transient decode failure.
        if self.last_payload is not None:
            return self.last_payload
        return self._fallback_payload()

    def _maybe_adopt_shared_cache(self) -> None:
        """Registry consumers only (see KeyVideoCache/acquire() below): if
        this instance is a non-builder reader still decoding the source, and
        the registry reports the shared cache file was promoted by someone
        else's builder, switch over -- closing the now-unneeded source
        capture. No-op for BackgroundVideoCache (never sets
        `_registry_entry`) and for the builder instance itself."""
        entry = getattr(self, "_registry_entry", None)
        if entry is None or not entry.ready:
            return
        with self.lock:
            if self._complete:
                return
            if self._open_existing_cache() and self.cap is not None:
                self.cap.release()
                self.cap = None

    def _get_cached_frame(self, n: int):
        n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]
        cap = self._cache_cap
        if cap is None:
            return None
        if n < self._cache_pos or n > self._cache_pos + self.MAX_DECODE_AHEAD:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self._cache_pos = n
        frame = None
        while self._cache_pos <= n:
            success, frame = cap.read()
            if not success:
                # Container metadata overcounted; clamp to what is readable.
                self.n_frames = max(1, self._cache_pos)
                return None
            self._cache_pos += 1
        payload = self._payload_from_bgr(frame)
        self._last_entry = (n, payload)
        return payload

    def _decode_source_frame(self, n: int):
        if self.cap is None:
            return None
        if self.n_frames > 0:
            n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]

        # A backward request while building would append frames out of
        # order, so the partial cache is dropped and the builder plays
        # uncached from here (a fresh builder restarts from scratch); a
        # plain reader just re-seeks, nothing to abort.
        if n < self.last_frame_index:
            if self.is_builder:
                self._abort_writer()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self.last_frame_index = n - 1

        payload = None
        while self.last_frame_index < n:
            success, frame = self.cap.read()
            if not success:
                self._end_of_source()
                if self._complete:
                    return self._get_cached_frame(n)
                return None
            self.last_frame_index += 1
            target_bgr = self._fit_to_target(frame)
            if self._writer is not None:
                self._writer.write(target_bgr)
                self._frames_written += 1
            if self.last_frame_index == n:
                payload = self._payload_from_bgr(target_bgr)

        # The frame-count metadata is usually exact, so the last read
        # succeeds and never trips the end-of-stream branch above; promote
        # the cache as soon as every promised frame has been written.
        if self.n_frames > 0 and self.last_frame_index >= self.n_frames - 1:
            self._end_of_source()

        if payload is not None:
            self._last_entry = (n, payload)
        return payload

    def _fit_to_target(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Fit a source frame (BGR) to `out_size`, preserving aspect ratio
        and baking in the saturation boost. Called once per *source* frame
        while a cache is being built (never again once `_complete`)."""
        pil_image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        canvas = ImageOps.fit(pil_image, self.out_size, Image.Resampling.HAMMING)
        # canvas is always mode "RGB" here (pil_image came from a 3-channel
        # BGR->RGB conversion), so no mode check/conversion is needed before
        # ImageEnhance.Color. Skipped entirely at the default factor.
        if self._sat_suffix:
            canvas = ImageEnhance.Color(canvas).enhance(self.saturation)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)

    def _end_of_source(self) -> None:
        """Source exhausted (EOF, or a decode failure partway through):
        promote whatever the writer produced, or clamp n_frames if the
        source's metadata promised more frames than it delivered. Always
        releases the source capture once reached -- a decode failure that
        never wrote/decoded a single frame must not leak `self.cap` (design
        doc bug 17: the old key_video_cache.VideoFrameCache did exactly
        that -- `break` out of its decode loop and never release)."""
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            if self._frames_written > 0:
                try:
                    os.replace(self._writer_tmp_path, self.cache_path)
                except OSError:
                    log.opt(exception=True).error("Failed to store tile video cache")
                else:
                    cap = self._open_cache_capture()
                    if cap.isOpened():
                        self._cache_cap = cap
                        self._cache_pos = 0
                        self.n_frames = self._frames_written
                        self._complete = True
                        self._on_promoted()
                        log.success(
                            f"Cached tile video ({self._frames_written} frames, "
                            f"{os.path.getsize(self.cache_path) / 1e6:.1f} MB): {self.cache_path}"
                        )
            else:
                self._remove_writer_tmp()

        if not self._complete and self.last_frame_index >= 0:
            self.n_frames = self.last_frame_index + 1

        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def is_cache_complete(self) -> bool:
        return self._complete

    # --- teardown --------------------------------------------------------

    def _abort_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        self._remove_writer_tmp()

    def _remove_writer_tmp(self) -> None:
        try:
            if os.path.isfile(self._writer_tmp_path):
                os.remove(self._writer_tmp_path)
        except OSError:
            pass

    def close(self) -> None:
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            if self._cache_cap is not None:
                self._cache_cap.release()
                self._cache_cap = None
            self._abort_writer()
            self._complete = False
            self._last_entry = None
            self.last_payload = None


# --------------------------------------------------------------------- #
# KeyVideoCache
# --------------------------------------------------------------------- #

class KeyVideoCache(Mp4FrameCache):
    """Per-key/-dial tile video. `out_size` is the tile size (key WxH or
    dial area size); one PIL image per frame, decoded straight at that
    resolution (no cropping -- unlike BackgroundVideoCache's canvas+crop).

    Used both as the registry's detached builder (`is_builder=True`) and as
    each consumer's own reader (`is_builder=False`, see `acquire()` below).
    """

    def _default_cache_path(self) -> str:
        size_str = f"{self.out_size[0]}x{self.out_size[1]}"
        cache_dir = os.path.join(VID_CACHE, f"keys_{size_str}")
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}.mp4")


# --------------------------------------------------------------------- #
# File-level registry -- shares the cache FILE, not the instance
# --------------------------------------------------------------------- #

def cache_videos_enabled() -> bool:
    return gl.settings_manager.get_app_settings().get("performance", {}).get("cache-videos", True)


class _TileCacheEntry:
    __slots__ = ("path", "refcount", "ready", "builder_thread", "stop_event")

    def __init__(self, path: str):
        self.path = path
        self.refcount = 0
        # A previous run may have already built (and left on disk) this
        # exact cache -- no builder needed, first acquire() just reads it.
        self.ready = os.path.isfile(path)
        self.builder_thread: threading.Thread = None
        self.stop_event = threading.Event()


_registry_lock = threading.Lock()
_registry: dict[tuple[str, tuple[int, int], float], _TileCacheEntry] = {}


def _registry_key(source_path: str, out_size: tuple[int, int], saturation: float) -> tuple:
    return (get_video_md5(source_path), tuple(out_size), round(float(saturation), 2))


def acquire(source_path: str, out_size: tuple[int, int], saturation: float = 1.0) -> KeyVideoCache:
    """Attach a new consumer to the shared tile-cache file for
    (source, out_size, saturation). Starts exactly one detached builder
    thread the first time a given key has no promoted cache on disk yet
    (and only while `performance.cache-videos` is enabled). Returns a fresh
    `KeyVideoCache` reader that owns its own `cv2.VideoCapture` and decode
    state -- release it with `release()` (InputVideo.close() does this)."""
    key = _registry_key(source_path, out_size, saturation)
    size_str = f"{out_size[0]}x{out_size[1]}"
    path = os.path.join(VID_CACHE, f"keys_{size_str}", f"{key[0]}{sat_suffix(saturation)}.mp4")

    start_builder = False
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        if not entry.ready and entry.builder_thread is None and cache_videos_enabled():
            entry.builder_thread = threading.Thread(
                target=_run_builder,
                args=(entry, source_path, out_size, saturation),
                name="tile-cache-builder",
                daemon=True,
            )
            start_builder = True

    if start_builder:
        entry.builder_thread.start()

    reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    reader._registry_key = key
    reader._registry_entry = entry
    return reader


def release(reader: KeyVideoCache) -> None:
    """Detach a consumer previously returned by acquire(). Closes the
    reader's own capture unconditionally; at refcount zero, also signals an
    in-flight builder to abort (nothing needs its output anymore) and drops
    the registry bookkeeping entry -- a future acquire() re-discovers the
    file from disk (if the builder had already promoted) or starts a fresh
    builder."""
    reader.close()

    key = getattr(reader, "_registry_key", None)
    entry = getattr(reader, "_registry_entry", None)
    if key is None or entry is None:
        return

    with _registry_lock:
        # Identity comparison: a late release must not evict a newer entry
        # for the same key (e.g. this entry was already dropped and
        # replaced by a fresh acquire() in between).
        if _registry.get(key) is not entry:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            entry.stop_event.set()
            del _registry[key]


def _run_builder(entry: _TileCacheEntry, source_path: str, out_size: tuple[int, int], saturation: float) -> None:
    builder = KeyVideoCache(source_path, out_size, saturation, cache_path=entry.path, is_builder=True)
    try:
        while not builder.is_cache_complete():
            if entry.stop_event.is_set():
                return
            if builder.n_frames <= 0:
                return
            builder.get_frame(builder.last_frame_index + 1)
        entry.ready = True
    except Exception:
        log.opt(exception=True).error(f"Tile cache builder failed for {source_path}")
    finally:
        builder.close()
