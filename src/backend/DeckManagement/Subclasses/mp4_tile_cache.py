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
from collections import OrderedDict

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses import cache_budget

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)


# --------------------------------------------------------------------- #
# Source-hash memo
# --------------------------------------------------------------------- #

_md5_memo_lock = threading.Lock()
# Small LRU: every edit of a source video mints a new (path, size, mtime)
# key, so an unbounded dict grows by one small entry per file version
# forever. 256 keys is far beyond any realistic working set of distinct
# videos; eviction only costs a re-hash.
_MD5_MEMO_MAX = 256
_md5_memo: "OrderedDict[tuple[str, int, float], str]" = OrderedDict()


def get_video_md5(path: str) -> str:
    """(path, size, mtime) -> md5, memoized (bounded LRU).

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
    """Saturation factor in integer hundredths -- THE single rounding for
    everything saturation-derived. The registry key, the cache-file suffix,
    and the factor baked into frames must all fall into the same bucket for
    a given raw float; two independent roundings here (`round(sat, 2)` for
    the key vs `int(round(sat * 100))` for the path) disagreed around the
    *.**5 boundaries, leaving a reader polling forever for a file that its
    entry's builder writes under a different name."""
    return int(round(float(saturation) * 100))


def canonical_saturation(saturation: float) -> float:
    """Raw factor -> the canonical two-decimal value every saturation
    consumer (registry key, file suffix, bake-in enhance) derives from."""
    return _sat_centi(saturation) / 100.0


def sat_suffix(saturation: float) -> str:
    """Two-decimal fixed encoding (e.g. 1.30 -> ".sat130"); empty at the
    default factor so plain "{md5}.mp4" caches stay valid and no
    enhance/mode-conversion work happens at 1.0. Derived from the same
    canonical rounding as the registry key (see _sat_centi).

    No-op band note: `centi == 100` treats [0.995, 1.005) as the default
    (the centi rounding this whole module shares), whereas the still-image /
    GIF bake elsewhere gates on `abs(sat - 1.0) > 0.001`. The (1.001, 1.005)
    gap is intentional and UI-unreachable -- the saturation slider steps by
    0.05 and rounds to two decimals, so only exact 0.05 multiples reach here,
    none of which fall in the gap. A single shared centi rounding for every
    saturation-derived name is worth more than matching that epsilon."""
    centi = _sat_centi(saturation)
    return "" if centi == 100 else f".sat{centi}"


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

    # Registry bookkeeping, attached from outside by the acquire() /
    # attach_promoted() entry points below on the readers they hand out.
    # Declaration only (no class-level value): a directly-constructed
    # instance genuinely has neither attribute, which is why every read of
    # them goes through getattr(..., None).
    _registry_key: "tuple | None"
    _registry_entry: "_TileCacheEntry | None"

    def __init__(self, source_path: str, out_size: tuple[int, int], saturation: float = 1.0,
                 cache_path: str = None, is_builder: bool = True) -> None:
        self.lock = threading.Lock()

        self.source_path = source_path
        self.out_size = out_size
        self.saturation = canonical_saturation(saturation)
        self._sat_suffix = sat_suffix(self.saturation)
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
        self._cache_cap: cv2.VideoCapture | None = None
        self._cache_pos = 0  # index of the next frame _cache_cap will return
        self._last_entry: tuple[int, object] | None = None
        self.last_payload = None  # last good decode, served over a transient failure
        self.last_payload_index: int | None = None  # source frame `last_payload` holds (see get_frame_and_index)
        self._adopt_failures = 0  # failed shared-cache adoptions (see _maybe_adopt_shared_cache)

        self.cap: cv2.VideoCapture | None = None
        self._writer: cv2.VideoWriter | None = None
        self._frames_written = 0
        self.last_frame_index = -1  # source decode position while building/reading

        self.n_frames = 0
        self._source_fps: float | None = None

        if not self._open_existing_cache():
            self._open_source()

        # #142 census, accounting-only: these hold real image RAM but are
        # never evictable -- dropping the one-frame memo would force a
        # re-decode every media tick, and the decoder buffers are not ours
        # to free. Registered last so budget_bytes() can never see a
        # half-constructed instance.
        cache_budget.register(
            self,
            label=f"video_readers:{self.video_md5[:8]}@{self.out_size[0]}x{self.out_size[1]}",
            evictable=False,
        )

    # Flat allowance per open cv2.VideoCapture. FFmpeg's decoder internals
    # (packet buffers, reference frames, swscale contexts) are opaque to
    # Python, so this is an honest constant rather than a measurement -- the
    # census's job is to show that video readers hold SOMETHING and how their
    # count moves, not to price libavcodec.
    CAPTURE_OVERHEAD_BYTES = 2 * 1024 * 1024

    def budget_bytes(self) -> int:
        """Estimated image RAM held by this reader (#142 census).

        LOCK-FREE ON PURPOSE. `self.lock` is held across whole decode, seek
        and build-frame operations, so taking it here would let one slow
        source stall the budget daemon (and, through it, every deck's
        eviction). Every read below is a single attribute load -- atomic
        under the GIL -- snapshotted into a local so a concurrent close()
        can null the original without this raising. A torn read costs one
        diagnostic sample a stale number, which is the correct trade for a
        census."""
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
        """Native fps of the source video; None if unknown. The cache mp4 is
        written at the source's fps, so whichever capture is open (source or
        cache) can answer."""
        if self._source_fps is None:
            with self.lock:
                cap = self._cache_cap if self._cache_cap is not None else self.cap
                if cap is not None:
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    if fps and fps > 0:
                        self._source_fps = float(fps)
        return self._source_fps

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
        writer = cv2.VideoWriter(self._writer_tmp_path, cv2.VideoWriter.fourcc(*"mp4v"), fps, self.out_size)
        if writer.isOpened():
            self._writer = writer
        else:
            log.warning(f"Could not open tile cache writer for {self.source_path}; playing uncached")

    # --- frame access ----------------------------------------------------

    def get_frame(self, n: int):
        return self.get_frame_and_index(n)[0]

    def get_frame_and_index(self, n: int):
        """(payload, source frame index of that payload) -- the index is what
        the payload actually IS, not what was asked for: requests are clamped
        to the readable range, and a transient decode failure repeats the
        last good frame. The index is None when the payload's provenance is
        unknown (fallback frames, or a repeat served before any index was
        established), so a caller keying a cache off it can never file one
        frame's pixels under another frame's identity (#163)."""
        if not self._complete:
            self._maybe_adopt_shared_cache()
        with self.lock:
            if self._complete:
                payload = self._get_cached_frame(n)
            else:
                payload = self._decode_source_frame(n)
            # Published under the lock: close() clears last_payload under
            # this same lock, so a teardown racing an in-flight decode can
            # no longer be overtaken by this write and left retaining one
            # frame on a closed instance.
            if payload is not None:
                self.last_payload = payload
                # _last_entry is the (clamped index, payload) pair whichever
                # decode path just produced or replayed; identity is only
                # claimed when it demonstrably describes THIS payload.
                if self._last_entry is not None and self._last_entry[1] is payload:
                    self.last_payload_index = self._last_entry[0]
                else:
                    self.last_payload_index = None
                return payload, self.last_payload_index
            # Keep showing the last good frame over a transient decode failure
            # -- last_payload_index still describes it, so it stays valid.
            if self.last_payload is not None:
                return self.last_payload, self.last_payload_index
        return self._fallback_payload(), None

    # After this many failed adoptions of a cache file the registry claims is
    # ready, give up on it: invalidate the entry so a future acquire() starts
    # a fresh builder, and detach so playback stops paying a per-frame stat
    # on a file that is never going to appear.
    MAX_ADOPT_FAILURES = 3

    def _maybe_adopt_shared_cache(self) -> None:
        """Registry consumers only (see KeyVideoCache/acquire() below): if
        this instance is a non-builder reader still decoding the source, and
        the registry reports the shared cache file was promoted by someone
        else's builder, switch over -- closing the now-unneeded source
        capture. No-op for BackgroundVideoCache (never sets
        `_registry_entry`) and for the builder instance itself.

        Bounded: if the registry says ready but the file cannot be opened
        (deleted or corrupted behind the registry's back -- e.g. an external
        cleanup of the cache dir), this degrades instead of retrying forever:
        after MAX_ADOPT_FAILURES attempts the entry is invalidated (self-heal
        -- the next acquire() sees ready=False/no builder and rebuilds) and
        this reader detaches, keeping its own source decode."""
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
            # Not reachable: the seek above pins _cache_pos to n whenever it
            # ran ahead, so the loop always reads at least once. Kept as a
            # cheap guard rather than an assert -- this is the media thread's
            # per-frame path.
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

    def is_build_terminal(self) -> bool:
        """True once the source capture has been released without the cache
        completing (VideoWriter open failure, os.replace failure, cache
        reopen failure, or a truncated source whose metadata promised more
        frames): no further get_frame() can make progress. Only meaningful
        after at least one get_frame() call. The constructor opens the source
        eagerly (_open_source), so a fresh builder's cap is already set -- the
        "trivially terminal before any decode" case (a source that would not
        open at all) is instead screened by _run_builder's `n_frames <= 0`
        guard, which returns before the first terminal check."""
        return self.cap is None and not self._complete

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
        # Out of the census the moment it is closed, rather than whenever GC
        # gets to the weak registry. A closed reader would report only its
        # (now released) captures anyway; this just keeps the reader COUNT
        # honest between teardown and collection.
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
    return gl.settings_manager.app().cache_videos


class _TileCacheEntry:
    __slots__ = ("path", "refcount", "ready", "builder_thread", "stop_event")

    def __init__(self, path: str):
        self.path = path
        self.refcount = 0
        # A previous run may have already built (and left on disk) this
        # exact cache -- no builder needed, first acquire() just reads it.
        self.ready = os.path.isfile(path)
        self.builder_thread: threading.Thread | None = None
        self.stop_event = threading.Event()


_registry_lock = threading.Lock()
_registry: dict[tuple[str, tuple[int, int], float], _TileCacheEntry] = {}


def _registry_key(source_path: str, out_size: tuple[int, int], saturation: float,
                  variant: str = "") -> tuple:
    # canonical_saturation is the same rounding sat_suffix() uses, so a key
    # and the file path derived from it can never disagree. `variant` names a
    # second, DIFFERENT rendering of the same source at the same size (see
    # acquire_from_frames): it must be part of the key, or the two would
    # share a file and each would serve the other's pixels.
    return (get_video_md5(source_path), tuple(out_size), canonical_saturation(saturation), variant)


def _cache_file_path(md5: str, out_size: tuple[int, int], saturation: float,
                     variant: str = "") -> str:
    size_str = f"{out_size[0]}x{out_size[1]}"
    return os.path.join(VID_CACHE, f"keys_{size_str}",
                        f"{md5}{sat_suffix(saturation)}{variant}.mp4")


def acquire(source_path: str, out_size: tuple[int, int], saturation: float = 1.0) -> KeyVideoCache:
    """Attach a new consumer to the shared tile-cache file for
    (source, out_size, saturation). Starts exactly one detached builder
    thread the first time a given key has no promoted cache on disk yet
    (and only while `performance.cache-videos` is enabled). Returns a fresh
    `KeyVideoCache` reader that owns its own `cv2.VideoCapture` and decode
    state -- release it with `release()` (InputVideo.close() does this).

    The builder DEMUXES the source with FFmpeg, so this entry point is for
    real video only: see `acquire_from_frames` for sources (GIFs) whose
    frames must come from a different compositor."""
    key = _registry_key(source_path, out_size, saturation)
    path = _cache_file_path(key[0], out_size, saturation)

    # The thread to start is carried out of the lock directly rather than as
    # a flag plus a re-read of entry.builder_thread (which another acquire()
    # could have replaced by then).
    start_builder: threading.Thread | None = None
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
            start_builder = entry.builder_thread

    if start_builder is not None:
        start_builder.start()

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
    _detach_entry(key, entry)


def _detach_entry(key: tuple, entry: "_TileCacheEntry") -> None:
    """Drop one reference to `entry`. Shared by release() and by the
    build-from-frames path's failure exit, so a consumer that never got a
    usable reader still balances its refcount."""
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


# --------------------------------------------------------------------- #
# Externally composited sources (GIF keys)
# --------------------------------------------------------------------- #
#
# A GIF's pixels may NEVER come from FFmpeg. FFmpeg and PIL disagree about
# GIF disposal and partial-extent frames -- measured on a stock 15-frame
# file, 7 frames differed structurally (~48% of pixels off by >32/255,
# disposed regions black on one side and olive on the other) -- so demuxing
# a GIF here would silently change what a key looks like. The two entry
# points below let a caller that HAS composited the frames itself (PIL, the
# same compositor the retained frame list uses) put them into the shared
# tile-cache file and read them back, without this module ever opening the
# GIF as video.

# Container timestamps only. Every consumer of these caches picks frames by
# INDEX off its own timeline (KeyGIF's per-frame delay walk), so the encoded
# frame rate is never played back at; it exists because mp4 needs one.
EXTERNAL_TILE_FPS = 15.0


def attach_promoted(source_path: str, out_size: tuple[int, int],
                    saturation: float = 1.0, variant: str = "") -> KeyVideoCache | None:
    """Attach a reader to an ALREADY-BUILT tile cache for this key, or
    return None when there is nothing built to attach to.

    Never starts a builder, and never hands back a reader that fell through
    to decoding the source -- the two things acquire() does that an
    externally-composited source must not get. A returned reader is always
    `is_cache_complete()`, i.e. serving the promoted mp4.

    Callers use this as the WARM path: the artifact's existence is itself
    the proof that the source was classified as buildable, so nothing has to
    be re-derived from pixels to route (issue #201). That inference is only
    sound per `variant` -- see acquire_from_frames."""
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
            # A build is in flight (or was abandoned): the caller does its
            # own cold pass rather than waiting on someone else's.
            return None
        entry.refcount += 1
    return _attach_promoted_reader(source_path, out_size, saturation, key, entry, path)


def acquire_from_frames(source_path: str, out_size: tuple[int, int], saturation: float,
                        frames, fps: float = EXTERNAL_TILE_FPS,
                        variant: str = "") -> KeyVideoCache | None:
    """Write the shared tile cache for this key from CALLER-SUPPLIED frames,
    then attach a reader to it. Returns None if the cache could not be
    written or read back (the caller keeps whatever it already has -- it
    never gets a reader that would decode the source instead).

    `frames` is any iterable of PIL images, consumed lazily and exactly
    once: a caller holding the whole animation passes its list, one that
    cannot afford to passes a generator and stays O(1). Images are resized
    to `out_size` only if they are not already there (the even-dimension
    clamp mp4v needs can leave a fitted frame a pixel off).

    Refcounting, sharing and release() are exactly acquire()'s: two keys
    showing the same GIF share one file and one entry, and the write is
    skipped entirely if the artifact already exists.

    `variant` separates renderings that are NOT interchangeable even though
    they come from the same source at the same size -- KeyGIF uses it to
    keep its alpha-dropping over-budget artifact away from the lossless one,
    so a later load cannot mistake the degraded file for proof that the GIF
    was opaque all along."""
    key = _registry_key(source_path, out_size, saturation, variant)
    path = _cache_file_path(key[0], out_size, saturation, variant)

    with _registry_lock:
        entry = _registry.get(key)
        if entry is None:
            entry = _TileCacheEntry(path)
            _registry[key] = entry
        entry.refcount += 1
        needs_build = not entry.ready

    if needs_build:
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
    """A reader on this entry, or None -- the caller's refcount is dropped
    on the None path. Rejects a reader that did not open the promoted cache:
    `Mp4FrameCache.__init__` falls back to opening the SOURCE when the cache
    file is missing or unreadable, which is exactly the FFmpeg-demuxes-the-
    GIF outcome these entry points exist to make impossible."""
    reader = KeyVideoCache(source_path, out_size, saturation, cache_path=path, is_builder=False)
    reader._registry_key = key
    reader._registry_entry = entry
    if not reader.is_cache_complete():
        release(reader)
        return None
    return reader


def _write_tile_mp4(path: str, out_size: tuple[int, int], frames, fps: float) -> int:
    """Encode `frames` into the tile cache at `path`, atomically (write to a
    per-writer temp file, `os.replace` on success -- the same promote
    discipline `_end_of_source` uses). Returns the number of frames written,
    0 on any failure. Never raises: a failed cache write must cost playback
    quality, never the key."""
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
    """Cache-file paths of every live registry entry -- consulted by the
    startup sweep (video_cache_sweeper.py) so it never deletes a file an
    attached reader/builder is using, even when the reference scan cannot
    see the source (e.g. the source file was deleted after acquire(), so
    its hash can no longer be recomputed)."""
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
                # Source released without completion. get_frame() returns
                # instantly in this state, so looping again would busy-spin
                # a full core for as long as the key stays on screen (B-02).
                #
                # This logs once per builder and exits. A permanently
                # unbuildable source therefore re-attempts (and re-logs once)
                # each time the entry is recreated by a fresh acquire() after
                # its refcount hit zero -- accepted: one bounded decode pass +
                # one log per acquire cycle is a strict improvement over the
                # pre-fix core-spin, and playback degrades to uncached either
                # way.
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
