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


def main() -> None:
    check_doorkeeper_admission()
    check_doorkeeper_ring_is_bounded()
    check_clear_resets_doorkeeper_and_entries()
    check_background_set_image_clears_encode_memo()
    check_background_set_video_clears_encode_memo()
    print("PASS: scenario_encode_memo_doorkeeper")


if __name__ == "__main__":
    main()
