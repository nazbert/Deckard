"""Shared build, promote and decode-ahead discipline for video-backed tile
caches.

Mp4FrameCache decodes the source video once, through a cv2.VideoWriter mp4v
encode that os.replace promotes atomically. Every frame after that is a cheap
decode out of the small canvas or tile-resolution mp4, and no raw frame data
sits in RAM. BackgroundVideoCache (background_video_cache.py) subclasses this
as a single instance that is both the builder and the only consumer, with the
build interleaved with playback ticks.

KeyVideoCache below uses the same discipline differently. The module-level
registry shares the cache file across consumers. Exactly one detached builder
thread per md5, size and saturation decodes the source and encodes the tile
mp4 independently of playback ticks. Every consumer of acquire() gets its own
KeyVideoCache reader with its own cv2.VideoCapture and its own last-frame
memo. A reader decodes straight from the source until the builder promotes,
then switches over.
"""
import hashlib
import os
import threading
from collections import OrderedDict

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses import cache_budget

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)


# Source-hash memo.

_md5_memo_lock = threading.Lock()
# A small LRU. Every edit of a source video mints a new (path, size, mtime)
# key, so an unbounded dict grows by one small entry per file version forever.
# 256 keys is far beyond any realistic working set of distinct videos, and an
# eviction only costs a re-hash.
_MD5_MEMO_MAX = 256
_md5_memo: "OrderedDict[tuple[str, int, float], str]" = OrderedDict()


def get_video_md5(path: str) -> str:
    """Maps (path, size, mtime) to an md5, memoized in a bounded LRU.

    Both cache classes hash the whole source file in their constructor, once
    per page switch and per InputVideo construction. That is cheap once and
    expensive when repeated. The registry key below hashes on every acquire(),
    which without the memo multiplies the cost across every consumer of a
    shared video.
    """
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    with _md5_memo_lock:
        cached = _md5_memo.get(key)
        if cached is not None:
            _md5_memo.move_to_end(key)
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
        _md5_memo.move_to_end(key)
        while len(_md5_memo) > _MD5_MEMO_MAX:
            _md5_memo.popitem(last=False)
    return digest


def _sat_centi(saturation: float) -> int:
    """Saturation factor in integer hundredths, the single rounding for
    everything saturation-derived.

    The registry key, the cache-file suffix and the factor baked into frames
    must fall into the same bucket for a given raw float. Two independent
    roundings, round(sat, 2) for the key against int(round(sat * 100)) for the
    path, disagree at the half-hundredth boundaries, and a reader then polls
    forever for a file its entry's builder writes under a different name.
    """
    return int(round(float(saturation) * 100))


def canonical_saturation(saturation: float) -> float:
    """Maps a raw factor to the canonical two-decimal value. Every saturation
    consumer derives from it, that is the registry key, the file suffix and
    the bake-in enhance."""
    return _sat_centi(saturation) / 100.0


def sat_suffix(saturation: float) -> str:
    """Two-decimal fixed encoding, e.g. 1.30 becomes ".sat130".

    It is empty at the default factor, so plain "{md5}.mp4" caches stay valid
    and no enhance or mode conversion runs at 1.0. It derives from the same
    canonical rounding as the registry key (see _sat_centi).
    """
    centi = _sat_centi(saturation)
    # centi == 100 treats [0.995, 1.005) as the default, the centi rounding
    # this whole module shares. The still-image and GIF bake elsewhere gates on
    # abs(sat - 1.0) > 0.001 instead. The UI cannot reach the (1.001, 1.005)
    # gap, because the saturation slider steps by 0.05 and rounds to two
    # decimals, so only exact 0.05 multiples arrive here.
    return "" if centi == 100 else f".sat{centi}"


# Mp4FrameCache.

class Mp4FrameCache:
    """Builds or reuses an mp4 per source, out_size and saturation. It decodes
    faster than the source and holds no per-frame data in RAM.

    A builder (is_builder=True, the default) decodes the source and writes
    every frame to a tmp mp4 that a promote makes atomic on completion. A
    reader (is_builder=False) never writes, and it decodes whichever of the
    promoted cache and the source is available.
    """

    # A forward jump of up to this many frames is bridged by a decode and
    # discard, which is cheaper than a container seek at tile or canvas
    # resolution. Anything larger, or backward, is a real seek.
    MAX_DECODE_AHEAD = 30

    # Registry bookkeeping. The acquire() and attach_promoted() entry points
    # below attach it from outside on the readers they hand out. This is a
    # declaration and not a class-level value, because a directly-constructed
    # instance has neither attribute. That is why every read of them goes
    # through getattr(..., None).
    _registry_key: "tuple | None"
    _registry_entry: "_TileCacheEntry | None"

    def __init__(self, source_path: str, out_size: tuple[int, int], saturation: float = 1.0,
                 cache_path: str = None, is_builder: bool = True) -> None:
        self.lock = threading.Lock()

        self.source_path = source_path
        self.out_size = out_size
        self.saturation = canonical_saturation(saturation)
        self._sat_suffix = sat_suffix(self.saturation)
        # BackgroundVideoCache uses one instance as both roles, a single
        # consumer with the build interleaved with playback ticks. The
        # KeyVideoCache registry splits them into one detached builder thread
        # and N per-consumer readers.
        self.is_builder = is_builder

        self.video_md5 = get_video_md5(source_path)

        self.cache_path = cache_path or self._default_cache_path()
        cache_dir = os.path.dirname(self.cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        # Unique per instance. Two builders for the same key that write at the
        # same time must not collide on one temp file. os.replace keeps a
        # collision last-wins safe.
        self._writer_tmp_path = os.path.join(
            cache_dir,
            f"{os.path.basename(self.cache_path)}.{os.getpid()}-{id(self):x}.tmp.mp4",
        )

        self._complete = False
        self._cache_cap: cv2.VideoCapture | None = None
        self._cache_pos = 0  # index of the next frame _cache_cap will return
        self._last_entry: tuple[int, object] | None = None
        self.last_payload = None  # last good decode, served over a transient failure
        self.last_payload_index: int | None = None  # source frame last_payload holds (see get_frame_and_index)
        self._adopt_failures = 0  # failed shared-cache adoptions (see _maybe_adopt_shared_cache)

        self.cap: cv2.VideoCapture | None = None
        self._writer: cv2.VideoWriter | None = None
        self._frames_written = 0
        self.last_frame_index = -1  # source decode position while building/reading

        self.n_frames = 0
        self._source_fps: float | None = None

        if not self._open_existing_cache():
            self._open_source()

        # Image-cache census, for accounting only. These hold real image RAM
        # and are never evictable. A drop of the one-frame memo forces a
        # re-decode every media tick, and the decoder buffers belong to
        # FFmpeg. Register last, so budget_bytes() never sees a
        # half-constructed instance.
        cache_budget.register(
            self,
            label=f"video_readers:{self.video_md5[:8]}@{self.out_size[0]}x{self.out_size[1]}",
            evictable=False,
        )

    # Flat allowance per open cv2.VideoCapture. FFmpeg's decoder internals,
    # the packet buffers, reference frames and swscale contexts, are opaque to
    # Python, so this is an honest constant and not a measurement. The census
    # shows that video readers hold memory and how their count moves. It does
    # not price libavcodec.
    CAPTURE_OVERHEAD_BYTES = 2 * 1024 * 1024

    def budget_bytes(self) -> int:
        """Estimated image RAM held by this reader, for the image-cache census.

        This method takes no lock. self.lock is held across whole decode, seek
        and build-frame operations, so a lock here lets one slow source stall
        the budget daemon and, through it, every deck's eviction. Every read
        below is a single attribute load, which the GIL makes atomic, and it
        goes into a local so a concurrent close() can null the original
        without a raise here. A torn read costs one diagnostic sample a stale
        number, which is the right trade for a census.
        """
        payload = self.last_payload
        total = 0
        if payload is not None:
            frames = payload if isinstance(payload, (list, tuple)) else (payload,)
            for frame in frames:
                try:
                    total += frame.width * frame.height * len(frame.getbands())
                except Exception:
                    continue
        for cap in (self.cap, self._cache_cap):
            if cap is not None:
                total += self.CAPTURE_OVERHEAD_BYTES
        return total

    def get_source_fps(self) -> float | None:
        """Native fps of the source video, or None when unknown. The cache mp4
        is written at the source fps, so whichever capture is open, source or
        cache, can answer."""
        if self._source_fps is None:
            with self.lock:
                cap = self._cache_cap if self._cache_cap is not None else self.cap
                if cap is not None:
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    if fps and fps > 0:
                        self._source_fps = float(fps)
        return self._source_fps

    # Overridable hooks.

    def _default_cache_path(self) -> str:
        raise NotImplementedError

    def _payload_from_bgr(self, frame_bgr: np.ndarray):
        """Convert one target-resolution BGR frame into what get_frame()
        returns. The default is a single RGB PIL image, which suits key and
        dial tiles decoded at tile resolution. BackgroundVideoCache overrides
        it to crop the canvas into per-key tiles and the strip."""
        return Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))

    def _fallback_payload(self):
        """Used when no decoded frame exists yet, on the first request during
        a build or after an unrecoverable early failure, and when there is no
        previous payload to repeat."""
        return None

    def _on_promoted(self) -> None:
        """Hook fired whenever this instance becomes _complete, either from an
        existing cache found at startup or from a fresh build just promoted.
        It does nothing by default. BackgroundVideoCache uses it to purge the
        legacy pickle cache format."""
        pass

    def _writer_enabled(self) -> bool:
        """Whether a builder instance opens a VideoWriter.

        The default is True. KeyVideoCache's registry gates
        performance.cache-videos once at acquire() time, before any builder
        exists, so its builder instances need no re-check.
        BackgroundVideoCache is a single self-contained instance that decides
        for itself, so it overrides this to read the live setting.
        """
        return True

    # Setup.

    def _open_cache_capture(self) -> cv2.VideoCapture:
        # A tile or canvas-resolution stream decodes at thousands of fps on
        # one thread. The default lets FFmpeg spawn a 16-thread frame pool per
        # capture, which wastes threads at this resolution.
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
        # The builder decodes as fast as it can on its own thread. A plain
        # reader is cheap at tile size and must not spin up extra threads per
        # consumer.
        threads = 4 if self.is_builder else 1
        self.cap = cv2.VideoCapture(self.source_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, threads])
        self.n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not self.is_builder or not self._writer_enabled():
            return
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        writer = cv2.VideoWriter(self._writer_tmp_path, cv2.VideoWriter.fourcc(*"mp4v"), fps, self.out_size)
        if writer.isOpened():
            self._writer = writer
        else:
            log.warning(f"Could not open tile cache writer for {self.source_path}; playing uncached")

    # Frame access.

    def get_frame(self, n: int):
        return self.get_frame_and_index(n)[0]

    def get_frame_and_index(self, n: int):
        """Returns the payload and the source frame index of that payload.

        The index names what the payload is and not what the caller asked
        for. This clamps a request to the readable range, and a transient
        decode failure repeats the last good frame. The index is None when the
        payload provenance is unknown, that is for a fallback frame or a
        repeat served before any index existed, so a caller that keys a cache
        off it never files one frame's pixels under another frame's identity.
        """
        if not self._complete:
            self._maybe_adopt_shared_cache()
        with self.lock:
            if self._complete:
                payload = self._get_cached_frame(n)
            else:
                payload = self._decode_source_frame(n)
            # Publish under the lock. close() clears last_payload under this
            # same lock, so this write cannot overtake a teardown that races
            # a decode in flight and leave one frame retained on a closed
            # instance.
            if payload is not None:
                self.last_payload = payload
                # _last_entry is the (clamped index, payload) pair that
                # whichever decode path just produced or replayed. Claim the
                # identity only when the pair describes this payload.
                if self._last_entry is not None and self._last_entry[1] is payload:
                    self.last_payload_index = self._last_entry[0]
                else:
                    self.last_payload_index = None
                return payload, self.last_payload_index
            # Keep showing the last good frame over a transient decode
            # failure. last_payload_index still describes it, so it stays
            # valid.
            if self.last_payload is not None:
                return self.last_payload, self.last_payload_index
        return self._fallback_payload(), None

    # Give up after this many failed adoptions of a cache file the registry
    # claims is ready. Invalidate the entry, so a future acquire() starts a
    # fresh builder, and detach, so playback stops paying a per-frame stat on
    # a file that never appears.
    MAX_ADOPT_FAILURES = 3

    def _maybe_adopt_shared_cache(self) -> None:
        """Switches a registry consumer over to a promoted shared cache file.

        This applies to registry consumers only (see KeyVideoCache and
        acquire() below). When this instance is a non-builder reader still
        decoding the source, and the registry reports that another builder
        promoted the shared cache file, it switches over and closes the source
        capture. It does nothing for BackgroundVideoCache, which never sets
        _registry_entry, and nothing for the builder instance itself.
        """
        entry = getattr(self, "_registry_entry", None)
        if entry is None or not entry.ready:
            return
        with self.lock:
            if self._complete:
                return
            if self._open_existing_cache():
                if self.cap is not None:
                    self.cap.release()
                    self.cap = None
                return
            # The registry says ready but the file will not open, e.g. an
            # external cleanup of the cache dir deleted or corrupted it behind
            # the registry's back. Bound the retry. After MAX_ADOPT_FAILURES
            # attempts, invalidate the entry so the next acquire() rebuilds,
            # and detach this reader onto its own source decode.
            self._adopt_failures += 1
            give_up = self._adopt_failures >= self.MAX_ADOPT_FAILURES
        if not give_up:
            return
        log.warning(
            f"Shared tile cache {self.cache_path} is marked ready but cannot be "
            f"opened; invalidating its registry entry and continuing uncached "
            f"from {self.source_path}"
        )
        with _registry_lock:
            entry.ready = False
            entry.builder_thread = None
        self._registry_entry = None

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
        if frame is None:
            # Not reachable. The seek above pins _cache_pos to n whenever it
            # ran ahead, so the loop always reads at least once. This stays a
            # cheap guard and not an assert, because it sits on the media
            # thread's per-frame path.
            return None
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

        # A backward request during a build appends frames out of order, so
        # this drops the partial cache and the builder plays uncached from
        # here. A fresh builder restarts from scratch. A plain reader only
        # re-seeks, and it has nothing to abort.
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

        # The frame-count metadata is usually exact, so the last read succeeds
        # and never trips the end-of-stream branch above. Promote the cache as
        # soon as the writer writes every promised frame.
        if self.n_frames > 0 and self.last_frame_index >= self.n_frames - 1:
            self._end_of_source()

        if payload is not None:
            self._last_entry = (n, payload)
        return payload

    def _fit_to_target(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Fit a source BGR frame to out_size, keep the aspect ratio and bake
        in the saturation boost. This runs once per source frame during a
        cache build, and never again once the cache is complete."""
        pil_image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        canvas = ImageOps.fit(pil_image, self.out_size, Image.Resampling.HAMMING)
        # canvas is always mode "RGB" here, because pil_image came from a
        # 3-channel BGR to RGB conversion, so ImageEnhance.Color needs no mode
        # check and no conversion. The default factor skips this entirely.
        if self._sat_suffix:
            canvas = ImageEnhance.Color(canvas).enhance(self.saturation)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)

    def _end_of_source(self) -> None:
        """Handles an exhausted source, at EOF or after a decode failure
        partway through.

        It promotes whatever the writer produced, or it clamps n_frames when
        the source metadata promised more frames than it delivered. It always
        releases the source capture. A decode failure that wrote and decoded
        no frame must not leak self.cap, which is what a break out of a decode
        loop with no release does.
        """
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

    def is_build_terminal(self) -> bool:
        """True once the source capture is released and the cache is not
        complete, so no further get_frame() can make progress.

        That state follows a VideoWriter open failure, an os.replace failure,
        a cache reopen failure, or a truncated source whose metadata promised
        more frames. It is only meaningful after at least one get_frame()
        call. The constructor opens the source eagerly through _open_source,
        so a fresh builder already has its cap. _run_builder's n_frames <= 0
        guard screens the case of a source that does not open at all, and it
        returns before the first terminal check.
        """
        return self.cap is None and not self._complete

    # Teardown.

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
        # Leave the census the moment this closes, rather than when GC reaches
        # the weak registry. A closed reader reports only its released
        # captures anyway. This keeps the reader count honest between teardown
        # and collection.
        cache_budget.unregister(self)
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
            self.last_payload_index = None


# KeyVideoCache.

class KeyVideoCache(Mp4FrameCache):
    """Per-key and per-dial tile video.

    out_size is the tile size, the key width by height or the dial area size.
    Each frame is one PIL image decoded at that resolution, with no crop,
    unlike BackgroundVideoCache's canvas and crop.

    It serves both as the registry's detached builder (is_builder=True) and as
    each consumer's own reader (is_builder=False, see acquire() below).
    """

    def _default_cache_path(self) -> str:
        size_str = f"{self.out_size[0]}x{self.out_size[1]}"
        cache_dir = os.path.join(VID_CACHE, f"keys_{size_str}")
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}.mp4")


# File-level registry. It shares the cache file and not the instance. Many
# InputVideo instances can reference the same source, tile size and
# saturation, and they must not share one cache instance. The build loop
# requires monotonically increasing frame requests, so interleaved consumers
# abort the writer, and after the build their independent wall-clock timelines
# seek-thrash the shared capture, measured at 0.05 ms to 0.92 ms per frame.

def cache_videos_enabled() -> bool:
    return gl.settings_manager.app().cache_videos


class _TileCacheEntry:
    __slots__ = ("path", "refcount", "ready", "builder_thread", "stop_event")

    def __init__(self, path: str):
        self.path = path
        self.refcount = 0
        # A previous run can have built this exact cache and left it on disk.
        # No builder is needed then, and the first acquire() only reads it.
        self.ready = os.path.isfile(path)
        self.builder_thread: threading.Thread | None = None
        self.stop_event = threading.Event()


_registry_lock = threading.Lock()
_registry: dict[tuple[str, tuple[int, int], float], _TileCacheEntry] = {}


def _registry_key(source_path: str, out_size: tuple[int, int], saturation: float,
                  variant: str = "") -> tuple:
    # canonical_saturation is the same rounding sat_suffix() uses, so a key
    # and the file path derived from it can never disagree. variant names a
    # second and different rendering of the same source at the same size (see
    # acquire_from_frames). It must be part of the key, or the two share a
    # file and each serves the other's pixels.
    return (get_video_md5(source_path), tuple(out_size), canonical_saturation(saturation), variant)


def _cache_file_path(md5: str, out_size: tuple[int, int], saturation: float,
                     variant: str = "") -> str:
    size_str = f"{out_size[0]}x{out_size[1]}"
    return os.path.join(VID_CACHE, f"keys_{size_str}",
                        f"{md5}{sat_suffix(saturation)}{variant}.mp4")


def acquire(source_path: str, out_size: tuple[int, int], saturation: float = 1.0) -> KeyVideoCache:
    """Attach a new consumer to the shared tile-cache file for one source,
    out_size and saturation.

    It returns a fresh KeyVideoCache reader that owns its own cv2.VideoCapture
    and decode state. Release it with release(), which InputVideo.close() does.

    The builder demuxes the source with FFmpeg, so this entry point takes real
    video only. See acquire_from_frames for a source such as a GIF whose
    frames must come from a different compositor.
    """
    key = _registry_key(source_path, out_size, saturation)
    path = _cache_file_path(key[0], out_size, saturation)

    # Carry the thread to start out of the lock directly, rather than as a
    # flag plus a re-read of entry.builder_thread, which another acquire() can
    # replace by then.
    start_builder: threading.Thread | None = None
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        # Start exactly one detached builder the first time a key has no
        # promoted cache on disk, and only while performance.cache-videos is
        # enabled.
        if not entry.ready and entry.builder_thread is None and cache_videos_enabled():
            entry.builder_thread = threading.Thread(
                target=_run_builder,
                args=(entry, source_path, out_size, saturation),
                name="tile-cache-builder",
                daemon=True,
            )
            start_builder = entry.builder_thread

    if start_builder is not None:
        start_builder.start()

    reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    reader._registry_key = key
    reader._registry_entry = entry
    return reader


def release(reader: KeyVideoCache) -> None:
    """Detach a consumer that acquire() returned.

    It always closes the reader's own capture. At refcount zero it also
    signals a builder in flight to abort, because nothing needs its output,
    and it drops the registry bookkeeping entry. A future acquire() then
    re-discovers the file from disk, if the builder promoted it, or starts a
    fresh builder.
    """
    reader.close()

    key = getattr(reader, "_registry_key", None)
    entry = getattr(reader, "_registry_entry", None)
    if key is None or entry is None:
        return
    _detach_entry(key, entry)


def _detach_entry(key: tuple, entry: "_TileCacheEntry") -> None:
    """Drop one reference to entry. release() and the failure exit of the
    build-from-frames path share it, so a consumer that never got a usable
    reader still balances its refcount."""
    with _registry_lock:
        # Compare identity. A late release must not evict a newer entry for
        # the same key, e.g. when this entry was already dropped and a fresh
        # acquire() replaced it.
        if _registry.get(key) is not entry:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            entry.stop_event.set()
            del _registry[key]


# Externally composited sources (GIF keys).
#
# A GIF's pixels must never come from FFmpeg. FFmpeg and PIL disagree about
# GIF disposal and partial-extent frames. On a stock 15-frame file, 7 frames
# differed structurally, with about 48% of pixels off by more than 32/255 and
# disposed regions black on one side and olive on the other. A demux of a GIF
# here therefore changes what a key looks like, silently. The two entry points
# below let a caller that composited the frames itself, in PIL, the same
# compositor the retained frame list uses, put them into the shared tile-cache
# file and read them back. This module never opens the GIF as video.

# Container timestamps only. Every consumer of these caches picks frames by
# index off its own timeline, such as KeyGIF's per-frame delay walk, so
# nothing plays back at the encoded frame rate. It exists because mp4 needs
# one.
EXTERNAL_TILE_FPS = 15.0


def attach_promoted(source_path: str, out_size: tuple[int, int],
                    saturation: float = 1.0, variant: str = "") -> KeyVideoCache | None:
    """Attach a reader to an already-built tile cache for this key, or return
    None when nothing built exists to attach to.

    It never starts a builder, and it never hands back a reader that fell
    through to decoding the source. Those are the two things acquire() does
    that an externally-composited source must not get. A returned reader is
    always is_cache_complete(), that is it serves the promoted mp4.

    Callers use this as the warm path. The artifact's existence proves that
    something classified the source as buildable, so nothing needs re-deriving
    from pixels to route. That inference holds per variant only, see
    acquire_from_frames.
    """
    key = _registry_key(source_path, out_size, saturation, variant)
    path = _cache_file_path(key[0], out_size, saturation, variant)
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            if not os.path.isfile(path):
                return None
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        elif not entry.ready:
            # A build is in flight, or something abandoned it. The caller does
            # its own cold pass rather than wait on another one.
            return None
        entry.refcount += 1
    return _attach_promoted_reader(source_path, out_size, saturation, key, entry, path)


def acquire_from_frames(source_path: str, out_size: tuple[int, int], saturation: float,
                        frames, fps: float = EXTERNAL_TILE_FPS,
                        variant: str = "") -> KeyVideoCache | None:
    """Write the shared tile cache for this key from caller-supplied frames,
    then attach a reader to it.

    Returns None when the write or the read back fails. The caller then keeps
    whatever it already has, and it never gets a reader that decodes the
    source instead. Refcounting, sharing and release() match acquire().
    """
    # variant separates renderings that are not interchangeable even though
    # they come from the same source at the same size. KeyGIF uses it to keep
    # its alpha-dropping over-budget artifact away from the lossless one, so a
    # later load cannot read the degraded file as proof the GIF was opaque.
    key = _registry_key(source_path, out_size, saturation, variant)
    path = _cache_file_path(key[0], out_size, saturation, variant)

    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        # Two keys showing the same GIF share one file and one entry, so skip
        # the write when the artifact already exists.
        needs_build = not entry.ready

    if needs_build:
        # frames is any iterable of PIL images, consumed lazily and exactly
        # once. A caller holding the whole animation passes its list, and one
        # that cannot afford to passes a generator and stays O(1).
        if _write_tile_mp4(path, out_size, frames, fps) <= 0:
            _detach_entry(key, entry)
            return None
        with _registry_lock:
            if _registry.get(key) is entry:
                entry.ready = True

    reader = _attach_promoted_reader(source_path, out_size, saturation, key, entry, path)
    if reader is None:
        log.warning(f"Tile cache written for {source_path} but not readable back")
    return reader


def _attach_promoted_reader(source_path: str, out_size: tuple[int, int], saturation: float,
                            key: tuple, entry: "_TileCacheEntry", path: str) -> KeyVideoCache | None:
    """A reader on this entry, or None. The None path drops the caller's
    refcount.

    It rejects a reader that did not open the promoted cache.
    Mp4FrameCache.__init__ falls back to opening the source when the cache
    file is missing or unreadable, and that is the FFmpeg demux of a GIF these
    entry points exist to make impossible.
    """
    reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    reader._registry_key = key
    reader._registry_entry = entry
    if not reader.is_cache_complete():
        release(reader)
        return None
    return reader


def _write_tile_mp4(path: str, out_size: tuple[int, int], frames, fps: float) -> int:
    """Encode frames into the tile cache at path, atomically.

    It writes to a per-writer temp file and calls os.replace on success, the
    same promote discipline _end_of_source uses. It returns the number of
    frames written, and 0 on any failure. It never raises, because a failed
    cache write must cost playback quality and never the key.
    """
    written = 0
    tmp_path = f"{path}.{os.getpid()}-{threading.get_ident():x}.tmp.mp4"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writer = cv2.VideoWriter(tmp_path, cv2.VideoWriter.fourcc(*"mp4v"), fps, out_size)
        if not writer.isOpened():
            log.warning(f"Could not open tile cache writer for {path}; playing uncached")
            return 0
        try:
            for frame in frames:
                # Resize only off-size frames. The even-dimension clamp mp4v
                # needs can leave an already-fitted frame a pixel off.
                if frame.size != out_size:
                    frame = frame.resize(out_size, Image.Resampling.LANCZOS)
                if frame.mode != "RGB":
                    frame = frame.convert("RGB")
                writer.write(cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR))
                written += 1
        finally:
            writer.release()
        if written == 0:
            return 0
        os.replace(tmp_path, path)
        log.info(f"Cached tile video ({written} frames, "
                 f"{os.path.getsize(path) / 1e6:.1f} MB): {path}")
        return written
    except Exception:
        log.opt(exception=True).warning(f"Failed to write tile video cache {path}")
        return 0
    finally:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def registry_cache_paths() -> set[str]:
    """Cache-file paths of every live registry entry.

    The startup sweep (video_cache_sweeper.py) reads them, so it never deletes
    a file an attached reader or builder uses. That holds even when the
    reference scan cannot see the source, e.g. after a delete of the source
    file that followed acquire(), which makes its hash unrecomputable.
    """
    with _registry_lock:
        return {entry.path for entry in _registry.values()}


def _run_builder(entry: _TileCacheEntry, source_path: str, out_size: tuple[int, int], saturation: float) -> None:
    builder = KeyVideoCache(source_path, out_size, saturation, cache_path=entry.path, is_builder=True)
    try:
        while not builder.is_cache_complete():
            if entry.stop_event.is_set():
                return
            if builder.n_frames <= 0:
                return
            builder.get_frame(builder.last_frame_index + 1)
            if builder.is_build_terminal():
                # The source is released and the build did not complete.
                # get_frame() returns instantly in this state, so another loop
                # busy-spins a full core for as long as the key stays on
                # screen.
                #
                # This logs once per builder and exits. A permanently
                # unbuildable source therefore re-attempts, and re-logs once,
                # each time a fresh acquire() recreates the entry after its
                # refcount hit zero. One bounded decode pass and one log per
                # acquire cycle is acceptable, and playback degrades to
                # uncached either way.
                log.error(
                    f"Tile cache build cannot complete for {source_path} -- "
                    f"leaving uncached playback"
                )
                return
        entry.ready = True
    except Exception:
        log.opt(exception=True).error(f"Tile cache builder failed for {source_path}")
    finally:
        builder.close()
