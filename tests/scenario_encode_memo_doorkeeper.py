"""
Unit + integration scenario (docs/memory-footprint-impl-plan.md P2.5):
EncodedImageCache's doorkeeper second-hit admission, and Background.
set_image()/set_video() clearing the encode memo on a content change.

Covers:
  (a) a key's first put() is recorded in the doorkeeper only -- NOT cached
      yet; its second put() is admitted into the real cache. An unrelated
      key's first sighting doesn't ride on another key's admission.
  (b) the doorkeeper ring is bounded (DOORKEEPER_SIZE): high-entropy content
      that keeps presenting brand-new keys can't grow it unboundedly -- a
      key that scrolled out of the ring is treated as a fresh first sighting
      again, not spuriously admitted.
  (c) clear() resets BOTH the cached entries and the doorkeeper's "seen"
      bookkeeping -- a key cached before a clear() must need two fresh puts
      again afterward, not skip straight back in.
  (d) Background.set_image() (DeckController.py) clears the deck's
      encode_memo -- a background content change orphans every entry keyed
      against the OLD background's composited pixels/hashes (design doc
      P2.5): left uncleared, a full memo would just sit there dead until
      LRU eviction eventually churned through it.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image

from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.DeckController import BackgroundImage, BackgroundVideo


def check_doorkeeper_admission() -> None:
    cache = EncodedImageCache(max_bytes=1024 * 1024)

    key = ("frame-key", 0)
    data = b"x" * 100

    # First sighting: recorded in the doorkeeper only, not cached yet.
    cache.put(key, data)
    assert cache.get(key) is None, "a key's first put() must not be cached yet (doorkeeper first sighting)"

    # Second sighting: now admitted into the real cache.
    cache.put(key, data)
    assert cache.get(key) == data, "a key's second put() must be cached (doorkeeper second sighting)"

    # A different key, seen once, still isn't cached -- admission is
    # per-key, not a global "warm" flag.
    other_key = ("other-frame", 1)
    cache.put(other_key, data)
    assert cache.get(other_key) is None, "an unrelated key's first sighting must not ride on another key's admission"

    print("PASS: doorkeeper second-hit admission")


def check_doorkeeper_ring_is_bounded() -> None:
    cache = EncodedImageCache(max_bytes=1024 * 1024)
    data = b"y" * 10

    # Fill the doorkeeper ring past capacity with distinct first sightings
    # (simulates high-entropy content: every key is brand new).
    for i in range(cache.DOORKEEPER_SIZE + 10):
        cache.put(("noise", i), data)

    # The very first key sighted has fallen out of the bounded ring by now
    # -- its next sighting is treated as a fresh first sighting again (still
    # not cached), proving noise can't grow the ring or the cache unbounded.
    cache.put(("noise", 0), data)
    assert cache.get(("noise", 0)) is None, (
        "a key that fell out of the bounded doorkeeper ring must be treated "
        "as a first sighting again, not spuriously admitted"
    )

    # A key still within the ring's recent window IS admitted normally.
    recent_key = ("noise", cache.DOORKEEPER_SIZE + 9)
    cache.put(recent_key, data)
    assert cache.get(recent_key) == data, "a key still within the doorkeeper ring must be admitted on its second sighting"

    print("PASS: doorkeeper ring is bounded (high-entropy content can't grow it unboundedly)")


def check_clear_resets_doorkeeper_and_entries() -> None:
    cache = EncodedImageCache(max_bytes=1024 * 1024)
    key = ("k", 0)
    data = b"z" * 10
    cache.put(key, data)
    cache.put(key, data)
    assert cache.get(key) == data, "fixture sanity: key should be cached before clear()"

    cache.clear()
    assert cache.get(key) is None, "clear() must drop cached entries"

    # clear() must also reset the doorkeeper: the same key must need TWO
    # fresh puts again, not be treated as already-seen from before the clear.
    cache.put(key, data)
    assert cache.get(key) is None, "clear() must reset the doorkeeper -- a post-clear first sighting must not be pre-admitted"
    cache.put(key, data)
    assert cache.get(key) == data

    print("PASS: clear() resets both cached entries and doorkeeper state")


def check_background_set_image_clears_encode_memo() -> None:
    controller = fixtures.make_headless_controller(serial="encode-memo-clear-image-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)

    memo_key = ("probe-image", 0)
    probe_data = b"probe-bytes"
    # Warm it past the doorkeeper directly (two puts), independent of
    # whatever real composite traffic the fixture's own page load produced.
    controller.encode_memo.put(memo_key, probe_data)
    controller.encode_memo.put(memo_key, probe_data)
    assert controller.encode_memo.get(memo_key) == probe_data, "fixture sanity: probe key should be cached before the background change"

    new_bg_image = BackgroundImage(controller, Image.new("RGB", (16, 16), (10, 20, 30)))
    controller.background.set_image(new_bg_image, update=False)

    assert controller.encode_memo.get(memo_key) is None, (
        "Background.set_image() must clear the encode memo (mem-plan P2.5) -- "
        "a content change orphans every entry keyed against the OLD background"
    )

    fixtures.teardown(controller)
    print("PASS: Background.set_image() clears the encode memo")


def check_background_set_video_clears_encode_memo() -> None:
    import cv2
    import numpy as np
    import os
    import globals as gl

    controller = fixtures.make_headless_controller(serial="encode-memo-clear-video-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)

    video_path = os.path.join(gl.DATA_PATH, "media", "encode_memo_probe.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (32, 32))
    assert writer.isOpened(), "could not open test video writer"
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    for _ in range(3):
        writer.write(frame)
    writer.release()

    memo_key = ("probe-video", 0)
    probe_data = b"probe-bytes-2"
    controller.encode_memo.put(memo_key, probe_data)
    controller.encode_memo.put(memo_key, probe_data)
    assert controller.encode_memo.get(memo_key) == probe_data, "fixture sanity: probe key should be cached before the background change"

    new_bg_video = BackgroundVideo(controller, video_path, loop=True, fps=10)
    try:
        controller.background.set_video(new_bg_video, update=False)

        assert controller.encode_memo.get(memo_key) is None, (
            "Background.set_video() must clear the encode memo (mem-plan P2.5)"
        )
    finally:
        fixtures.teardown(controller)

    print("PASS: Background.set_video() clears the encode memo")


def check_byte_cap_lru_eviction() -> None:
    """#71 (c): the byte-size cap and its LRU eviction order were unexercised.
    Admit several entries past the cap and prove: (1) total bytes stay <=
    max_bytes, (2) the LEAST-recently-used entry is the one evicted (a get()
    on an older key promotes it, sparing it), (3) a fresh key is never
    admitted on its first put() even under memory pressure (the doorkeeper
    still gates admission)."""
    # Each admitted value is 100 bytes; cap holds 3 of them (300 B). Admission
    # needs two puts per key (doorkeeper), so warm each key with two puts.
    cache = EncodedImageCache(max_bytes=300)
    val = b"v" * 100

    def admit(k):
        cache.put(k, val)  # doorkeeper first sighting
        cache.put(k, val)  # admitted

    admit("a")
    admit("b")
    admit("c")
    assert cache.get("a") == val and cache.get("b") == val and cache.get("c") == val, "a,b,c should all be cached at the cap"
    assert cache._total_bytes == 300, f"total bytes should be exactly the cap (300), got {cache._total_bytes}"

    # Touch "a" so it becomes most-recently-used; "b" is now the LRU victim.
    assert cache.get("a") == val
    admit("d")  # over the cap -> exactly one eviction, and it must be "b"

    assert cache._total_bytes <= 300, f"total bytes must stay within the cap after eviction, got {cache._total_bytes}"
    assert cache.get("b") is None, "the least-recently-used entry ('b') must be the one evicted"
    assert cache.get("a") == val, "a recently-touched entry must survive eviction"
    assert cache.get("c") == val, "'c' (more recently used than 'b') must survive"
    assert cache.get("d") == val, "the newly-admitted entry must be present"

    # Memory pressure must not let a brand-new key skip the doorkeeper.
    cache.put("e", val)
    assert cache.get("e") is None, "a first-sighting key must not be admitted even under cap pressure"

    print("PASS: byte-cap holds and evicts the least-recently-used entry, doorkeeper still gates")


def check_memo_consulted_on_real_encode_path() -> None:
    """#71 (c): the scenario tested EncodedImageCache in isolation but never
    proved the memo is CONSULTED on the real encode path -- unplugging it
    entirely still passed. Drive a real ControllerKey.update() on a headless
    controller and prove the second identical paint is a memo HIT: the
    expensive encode_native_key() is NOT called again, and the same
    already-cached native-image object is what gets enqueued.

    Red proof (documented in the campaign notes): bypass the memo (make
    encode_memo.get() always return None) and this leg FAILS -- exactly the
    'unplugging it still passes' gap it closes."""
    import time
    from src.backend.DeckManagement.InputIdentifier import Input
    import src.backend.DeckManagement.DeckController as DC

    controller = fixtures.make_headless_controller(serial="encode-memo-realpath-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)
    assert controller.is_visual(), "fixture sanity: the encode path only runs on a visual deck"

    # Count real encodes so a memo hit is observable as "encode_native_key
    # was NOT called again".
    encode_calls = {"n": 0}
    real_encode = DC.encode_native_key

    def counting_encode(deck, img):
        encode_calls["n"] += 1
        return real_encode(deck, img)

    DC.encode_native_key = counting_encode
    try:
        key = controller.inputs[Input.Key][0]

        # Let any startup paint settle, then take a clean baseline.
        time.sleep(0.1)
        controller.encode_memo.clear()
        # Warm the memo for this key's content: because put()'s doorkeeper
        # admits on the SECOND sighting, two identical forced paints are what
        # actually populate the real cache entry (mirrors looping content
        # warming by its second wrap).
        key.update(force=True)
        time.sleep(0.05)
        key.update(force=True)
        time.sleep(0.05)

        # There must now be a cached native image for this content.
        assert len(controller.encode_memo._entries) >= 1, (
            "two identical paints must have admitted a native-image entry into "
            "the real encode memo"
        )
        cached_before = dict(controller.encode_memo._entries)
        calls_before = encode_calls["n"]

        # A THIRD identical paint must consult the memo and hit -- no new
        # encode, and the cache contents are unchanged (same objects).
        key.update(force=True)
        time.sleep(0.05)

        assert encode_calls["n"] == calls_before, (
            "an identical repaint must be served from the encode memo -- "
            "encode_native_key must NOT be called again (memo hit)"
        )
        # Same cached object identities: the hit returned the stored native
        # image, it did not re-encode and re-put a fresh one.
        after = controller.encode_memo._entries
        assert after.keys() == cached_before.keys(), "a memo hit must not change the cached key set"
        for k in cached_before:
            assert after[k] is cached_before[k], (
                "a memo hit must return the SAME stored native image object, "
                "proving .get() was consulted rather than re-encoding"
            )
    finally:
        DC.encode_native_key = real_encode
        fixtures.teardown(controller)

    print("PASS: the encode memo is consulted (and hits) on the real ControllerKey.update() path")


def check_put_vs_clear_race() -> None:
    """#71 (c): a put() racing a clear() (a background content change or a
    close() firing while a paint is mid-put) must never corrupt the cache --
    it must leave the memo in a consistent state (total_bytes matching the
    entries actually held, never negative, never over the cap), regardless of
    which of the two won the lock. Both operations take the same _lock, so
    the invariant is that neither can observe or leave a torn intermediate.

    Deterministic interleave: two barrier-synchronized threads hammer put()
    and clear() on the same keys; after they join, assert the bookkeeping
    invariant holds and the cache is still usable."""
    import threading

    cache = EncodedImageCache(max_bytes=10 * 1024)
    val = b"z" * 128
    keys = [("race", i) for i in range(32)]

    start = threading.Barrier(2)
    stop = threading.Event()
    errors = []

    def putter():
        start.wait()
        try:
            while not stop.is_set():
                for k in keys:
                    cache.put(k, val)  # first sighting
                    cache.put(k, val)  # admit
        except Exception as e:  # a torn state would surface as an exception
            errors.append(e)

    def clearer():
        start.wait()
        try:
            for _ in range(2000):
                cache.clear()
        except Exception as e:
            errors.append(e)
        finally:
            stop.set()

    tp = threading.Thread(target=putter, name="race-putter")
    tc = threading.Thread(target=clearer, name="race-clearer")
    tp.start()
    tc.start()
    tc.join(timeout=15)
    stop.set()
    tp.join(timeout=15)
    assert not tp.is_alive() and not tc.is_alive(), "race threads wedged"
    assert not errors, f"put/clear race raised: {errors!r}"

    # Invariant: total_bytes must equal the sum of the bytes actually held,
    # be non-negative, and never exceed the cap -- no torn accounting left by
    # whichever op won the lock last during the storm.
    with cache._lock:
        held = sum(len(v) for v in cache._entries.values())
        assert cache._total_bytes == held, (
            f"total_bytes ({cache._total_bytes}) must match the bytes actually "
            f"held ({held}) after a put/clear race"
        )
        assert cache._total_bytes >= 0, "total_bytes must never go negative"
        assert cache._total_bytes <= cache._max_bytes, "total_bytes must never exceed the cap"

    # Deterministic post-storm check: a clear() after puts drove the counter
    # up must reset the byte accounting to exactly zero. This holds regardless
    # of race timing (a fresh clear over a non-empty cache), and is the direct
    # accounting invariant a clear() that emptied _entries without resetting
    # _total_bytes would violate.
    cache.put(("settle", 0), val)
    cache.put(("settle", 0), val)  # admit, so _entries + _total_bytes are non-zero
    assert cache._total_bytes > 0, "fixture sanity: cache should hold bytes before the final clear"
    cache.clear()
    assert cache._total_bytes == 0, (
        "clear() must reset total_bytes to zero -- a clear() that empties the "
        "entries without resetting the byte counter leaves torn accounting"
    )
    assert len(cache._entries) == 0, "clear() must empty the entries"

    # Still usable: a fresh two-put admit works after the storm.
    cache.put(("after", 0), val)
    cache.put(("after", 0), val)
    assert cache.get(("after", 0)) == val, "cache must remain usable after the race"

    print("PASS: put/clear race leaves consistent byte accounting and a usable cache")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_encode_memo_doorkeeper")
    check_doorkeeper_admission()
    check_doorkeeper_ring_is_bounded()
    check_clear_resets_doorkeeper_and_entries()
    check_byte_cap_lru_eviction()
    check_put_vs_clear_race()
    check_memo_consulted_on_real_encode_path()
    check_background_set_image_clears_encode_memo()
    check_background_set_video_clears_encode_memo()
    print("PASS: scenario_encode_memo_doorkeeper")


if __name__ == "__main__":
    main()
