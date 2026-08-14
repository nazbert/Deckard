"""Pins the doorkeeper admission of EncodedImageCache and the memo clear.

A key is cached on its second put, the doorkeeper ring is bounded, clear()
resets both, and a background content change clears the deck encode memo.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image

from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.DeckController import BackgroundImage, BackgroundVideo


def check_doorkeeper_admission() -> None:
    cache = EncodedImageCache(max_bytes=1024 * 1024)

    key = ("frame-key", 0)
    data = b"x" * 100

    # The first sighting is recorded in the doorkeeper only, not cached yet.
    cache.put(key, data)
    assert cache.get(key) is None, "a key's first put() must not be cached yet (doorkeeper first sighting)"

    # The second sighting is admitted into the real cache.
    cache.put(key, data)
    assert cache.get(key) == data, "a key's second put() must be cached (doorkeeper second sighting)"

    # A different key seen once is still not cached, because admission is
    # per key rather than a global warm flag.
    other_key = ("other-frame", 1)
    cache.put(other_key, data)
    assert cache.get(other_key) is None, "an unrelated key's first sighting must not ride on another key's admission"

    print("PASS: doorkeeper second-hit admission")


def check_doorkeeper_ring_is_bounded() -> None:
    cache = EncodedImageCache(max_bytes=1024 * 1024)
    data = b"y" * 10

    # Fill the doorkeeper ring past capacity with distinct first sightings,
    # which models high-entropy content where every key is brand new.
    for i in range(cache.DOORKEEPER_SIZE + 10):
        cache.put(("noise", i), data)

    # The first key sighted has fallen out of the bounded ring by now, so its
    # next sighting counts as a fresh first sighting and stays uncached. Noise
    # cannot grow the ring or the cache without bound.
    cache.put(("noise", 0), data)
    assert cache.get(("noise", 0)) is None, (
        "a key that fell out of the bounded doorkeeper ring must be treated "
        "as a first sighting again, not spuriously admitted"
    )

    # A key still inside the recent window of the ring is admitted normally.
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

    # clear() must also reset the doorkeeper. The same key must need two fresh
    # puts again, not count as already seen from before the clear.
    cache.put(key, data)
    assert cache.get(key) is None, "clear() must reset the doorkeeper -- a post-clear first sighting must not be pre-admitted"
    cache.put(key, data)
    assert cache.get(key) == data

    print("PASS: clear() resets both cached entries and doorkeeper state")


def check_set_image_clears_memo() -> None:
    controller = fixtures.make_headless_controller(serial="encode-memo-clear-image-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)

    memo_key = ("probe-image", 0)
    probe_data = b"probe-bytes"
    # Warm it past the doorkeeper directly with two puts, independent of
    # whatever real composite traffic the page load of the fixture produced.
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


def check_set_video_clears_memo() -> None:
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
    """Pins the byte-size cap and its LRU eviction order.

    Total bytes stay at or under max_bytes, the least recently used entry is
    the one evicted, a get() promotes an older key, and a fresh key is never
    admitted on its first put even under memory pressure.
    """
    # Each admitted value is 100 bytes and the cap holds three of them.
    # Admission needs two puts per key, so warm each key with two puts.
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

    # Touch a, so it becomes most recently used and b is the LRU victim.
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


def check_memo_used_on_encode_path() -> None:
    """The memo must be consulted on the real encode path.

    Drive a real ControllerKey.update() on a headless controller and prove the
    second identical paint hits the memo. encode_native_key() must not run
    again, and the enqueued native image must be the already cached object.
    """
    import time
    from src.backend.DeckManagement.InputIdentifier import Input
    import src.backend.DeckManagement.deck_controller.inputs as inputs_mod

    controller = fixtures.make_headless_controller(serial="encode-memo-realpath-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)
    assert controller.is_visual(), "fixture sanity: the encode path only runs on a visual deck"

    # Count real encodes, so a memo hit shows up as encode_native_key not being
    # called again. ControllerKey.update resolves the name from its own module,
    # so that is where the counter has to be installed.
    encode_calls = {"n": 0}
    real_encode = inputs_mod.encode_native_key

    def counting_encode(deck, img):
        encode_calls["n"] += 1
        return real_encode(deck, img)

    inputs_mod.encode_native_key = counting_encode
    try:
        key = controller.inputs[Input.Key][0]

        # Let any startup paint settle, then take a clean baseline.
        time.sleep(0.1)
        controller.encode_memo.clear()
        # Warm the memo for this key content. put() admits on the second
        # sighting, so two identical forced paints populate the real cache
        # entry, which mirrors looping content warming on its second wrap.
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
        # A standing guard, not a fixture detail. The patch below bites only if
        # it replaces the name in the module the paint path resolves it from,
        # and a counter stuck at zero makes every no-new-encode assertion
        # compare 0 to 0, which proves nothing.
        assert calls_before > 0, (
            "the encode counter never fired: encode_native_key is not being "
            "intercepted on the paint path, so the memo assertions below are "
            "vacuous"
        )

        # A third identical paint must consult the memo and hit, with no new
        # encode and unchanged cache contents.
        key.update(force=True)
        time.sleep(0.05)

        assert encode_calls["n"] == calls_before, (
            "an identical repaint must be served from the encode memo -- "
            "encode_native_key must NOT be called again (memo hit)"
        )
        # The same cached object identities. The hit returned the stored native
        # image and did not re-encode and re-put a fresh one.
        after = controller.encode_memo._entries
        assert after.keys() == cached_before.keys(), "a memo hit must not change the cached key set"
        for k in cached_before:
            assert after[k] is cached_before[k], (
                "a memo hit must return the SAME stored native image object, "
                "proving .get() was consulted rather than re-encoding"
            )
    finally:
        inputs_mod.encode_native_key = real_encode
        fixtures.teardown(controller)

    print("PASS: the encode memo is consulted (and hits) on the real ControllerKey.update() path")


def check_put_vs_clear_race() -> None:
    """A put() racing a clear() must never corrupt the cache.

    Both operations take the same lock, so neither may observe or leave a torn
    intermediate. Two barrier-synchronized threads hammer put() and clear() on
    the same keys, then the bookkeeping invariant must hold.
    """
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

    # The invariant. total_bytes must equal the sum of the bytes actually held,
    # stay non-negative and never exceed the cap, whichever operation won the
    # lock last during the storm.
    with cache._lock:
        held = sum(len(v) for v in cache._entries.values())
        assert cache._total_bytes == held, (
            f"total_bytes ({cache._total_bytes}) must match the bytes actually "
            f"held ({held}) after a put/clear race"
        )
        assert cache._total_bytes >= 0, "total_bytes must never go negative"
        assert cache._total_bytes <= cache._max_bytes, "total_bytes must never exceed the cap"

    # A deterministic post-storm check. A clear() after puts drove the counter
    # up must reset the byte accounting to exactly zero. This holds whatever the
    # race timing was, and a clear() that emptied _entries without resetting
    # _total_bytes would violate it.
    cache.put(("settle", 0), val)
    cache.put(("settle", 0), val)  # admit, so _entries + _total_bytes are non-zero
    assert cache._total_bytes > 0, "fixture sanity: cache should hold bytes before the final clear"
    cache.clear()
    assert cache._total_bytes == 0, (
        "clear() must reset total_bytes to zero -- a clear() that empties the "
        "entries without resetting the byte counter leaves torn accounting"
    )
    assert len(cache._entries) == 0, "clear() must empty the entries"

    # Still usable. A fresh two-put admit works after the storm.
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
    check_memo_used_on_encode_path()
    check_set_image_clears_memo()
    check_set_video_clears_memo()
    print("PASS: scenario_encode_memo_doorkeeper")


if __name__ == "__main__":
    main()
