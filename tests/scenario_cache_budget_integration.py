"""Integration scenario for the image-cache budget over real DeckControllers.

Both per-deck caches join the budget. A binding ceiling makes an idle deck
shed to a busy one, the paint path is unaffected, and a closed deck stops.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import os
import time

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import BackgroundImage, BackgroundVideo
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses import cache_budget

KIB = 1024
CEILING_BYTES = 256 * KIB
ENTRY = 4 * KIB
# Both caches use monotonic last-use stamps and the default 2 s min-age. A real
# DeckController offers no clock seam, so the ageing here is real.
AGE_SLEEP = 2.3


def _set_ceiling(value) -> None:
    if value is None:
        os.environ.pop(cache_budget.ENV_CEILING, None)
    else:
        os.environ[cache_budget.ENV_CEILING] = str(value)


def _fill(cache, prefix: str, total_bytes: int) -> None:
    """File synthetic entries through the real put() of the cache.

    The paint path makes the same call. Each key is put twice, so the
    doorkeeper of the encode memo admits it.
    """
    for i in range(total_bytes // ENTRY):
        key = (prefix, i)
        cache.put(key, bytes(ENTRY))
        cache.put(key, bytes(ENTRY))


def _deck_bytes(controller) -> int:
    return controller.encode_memo.total_bytes + controller.native_tile_cache.total_bytes


def _present(cache, prefix: str, total_bytes: int) -> int:
    """Count how many entries _fill filed are still cached.

    Counted per key rather than by byte total, because a real repaint can file
    an entry at any moment and these assertions are about the synthetic ones.
    """
    return sum(1 for i in range(total_bytes // ENTRY) if cache.get((prefix, i)) is not None)


def _key_signature(controller, deck) -> dict:
    return {k: (deck.last_op_for(f"key:{k}") or (None,) * 5)[4]
            for k in range(controller.deck.key_count())}


def _make_test_mp4(path: str, n_frames: int, fps: int, size=(160, 120)) -> str:
    import cv2
    import numpy as np
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(n_frames):
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        writer.write(frame)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


def check_registration_and_cross_deck_eviction(busy, idle) -> None:
    busy_deck = fixtures.raw_deck(busy)

    # Both caches of both decks joined the budget, and stay attributable.
    for controller in (busy, idle):
        serial = controller.serial_number()
        assert controller.encode_memo.budget_label == f"encode_memo:{serial}", (
            f"unlabelled encode memo: {getattr(controller.encode_memo, 'budget_label', None)!r}"
        )
        assert controller.native_tile_cache.budget_label == f"native_tiles:{serial}"
        assert controller.encode_memo.budget_evictable
    groups = cache_budget.totals()
    assert "encode_memo" in groups and "native_tiles" in groups, (
        f"both cache groups must be visible to the budget: {groups!r}"
    )

    _set_ceiling(CEILING_BYTES / (1024 * 1024))
    idle_memo, idle_tiles = 160 * KIB, 96 * KIB
    busy_memo, busy_tiles = 96 * KIB, 32 * KIB

    # The working set of the idle deck goes first, so it is the older one.
    _fill(idle.encode_memo, "idle-memo", idle_memo)
    _fill(idle.native_tile_cache, "idle-tiles", idle_tiles)
    time.sleep(AGE_SLEEP)
    # The busy deck goes second, so its entries are inside min-age.
    _fill(busy.encode_memo, "busy-memo", busy_memo)
    _fill(busy.native_tile_cache, "busy-tiles", busy_tiles)

    assert _deck_bytes(idle) + _deck_bytes(busy) > CEILING_BYTES, (
        f"fixture sanity: the ceiling must actually bind "
        f"({_deck_bytes(idle) + _deck_bytes(busy)} <= {CEILING_BYTES})"
    )
    sig_before = _key_signature(busy, busy_deck)

    cache_budget._drain_once()

    # The idle deck paid, and the fresh entries of the busy deck did not.
    assert _present(busy.encode_memo, "busy-memo", busy_memo) == busy_memo // ENTRY, (
        "the busy deck's encode memo lost entries that were inside min_age_s while "
        "an older deck still had something to give"
    )
    assert _present(busy.native_tile_cache, "busy-tiles", busy_tiles) == busy_tiles // ENTRY, (
        "the busy deck's tile cache lost entries that were inside min_age_s"
    )
    assert _present(idle.encode_memo, "idle-memo", idle_memo) < idle_memo // ENTRY, (
        "the idle deck's entries must be the ones the global ceiling takes -- this "
        "cross-deck preference is exactly what per-silo caps cannot express"
    )
    assert cache_budget.evictable_bytes() <= CEILING_BYTES, (
        f"the sum must land at or under the ceiling: "
        f"{cache_budget.evictable_bytes()} > {CEILING_BYTES}"
    )
    # The sum of the default floors is 4 MiB times 4 registrants, 64 times this
    # ceiling. Only the ceiling // (2 * registrants) clamp makes any of this
    # evictable, and the clamped floor is where the idle memo comes to rest. It
    # is the oldest cache in the process, so the drain runs it down to the floor
    # and takes the rest from the next-oldest. A comparison against the nominal
    # 4 MiB floor would be vacuous, because this fixture is a quarter of a MiB.
    floor_clamp = CEILING_BYTES // (2 * 4)   # 32 KiB, 4 evictable registrants
    assert idle.encode_memo.total_bytes >= floor_clamp, (
        f"global eviction dug the idle deck's memo below its clamped floor: "
        f"{idle.encode_memo.total_bytes} < {floor_clamp}"
    )

    # Eviction happened around a live paint path, not through it. The same
    # content still reaches the device as the same bytes.
    for key in busy.inputs[Input.Key]:
        key.update(force=True)
    assert fixtures.wait_until(
        lambda: _key_signature(busy, busy_deck) == sig_before, timeout=5), (
        f"a repaint after global eviction changed what the device was handed: "
        f"{_key_signature(busy, busy_deck)} != {sig_before}"
    )

    # Once the entries of the busy deck are past min-age too, nothing is exempt
    # and a tighter ceiling is enforced against both decks.
    time.sleep(AGE_SLEEP)
    tight = 64 * KIB
    _set_ceiling(tight / (1024 * 1024))
    cache_budget._drain_once()
    assert cache_budget.evictable_bytes() <= tight, (
        f"a lowered ceiling must be enforced against every deck once entries "
        f"age past min_age_s: {cache_budget.evictable_bytes()} > {tight}"
    )

    print("PASS: both decks' caches are budgeted; the idle deck sheds first and "
          "the busy deck's device writes are unchanged")


def check_tile_min_age_tracks_video(controller) -> None:
    """Native tile entries are keyed per frame and re-touched once per loop.

    The flat 2 s default would leave the frame set of a playing video evictable
    exactly one loop before it is needed again.
    """
    assert controller.native_tile_cache.budget_min_age_s == cache_budget.DEFAULT_MIN_AGE_S, (
        "fixture sanity: a deck with no background video starts at the default min-age"
    )

    path = _make_test_mp4(
        os.path.join(fixtures.DATA_DIR, "assets", "budget_min_age.mp4"), n_frames=90, fps=15)
    video = BackgroundVideo(controller, path, loop=True, fps=30)
    video.page = None
    controller.background.set_video(video, update=False)

    # While the tile cache is still building, playback advances one frame per
    # media tick rather than at source fps, so the true loop period is longer
    # than frames over fps, and unknowably so. The conservative clamp maximum
    # holds the frame set until the build lands.
    assert not video.is_cache_complete(), "fixture sanity: the cache should still be building"
    assert controller.native_tile_cache.budget_min_age_s == cache_budget.MAX_MIN_AGE_S, (
        f"an unbuilt tile cache must be shielded by the clamp maximum, not by a "
        f"loop period that does not apply yet: "
        f"{controller.native_tile_cache.budget_min_age_s}"
    )

    # Play it through. The build completes, and the first tick past that
    # installs the real loop duration.
    for _ in range(video.n_frames * 3 + 10):
        if video.is_cache_complete():
            break
        controller.background.update_tiles()
    assert video.is_cache_complete(), "fixture sanity: the tile cache never completed"
    controller.background.update_tiles()

    assert abs(controller.native_tile_cache.budget_min_age_s - 6.0) < 0.5, (
        f"min-age must follow the loop duration (90 frames / 15 fps = 6 s) once the "
        f"cache is built and playback is real-time, got "
        f"{controller.native_tile_cache.budget_min_age_s}"
    )

    # The census entry of the reader is accounting-only, so it is visible in
    # totals() and must never count against the ceiling.
    controller.background.update_tiles()
    assert not video.budget_evictable, "video readers are census-only, never evictable"
    # Read the governed sum first. The census is a superset, so anything that
    # lands in between can only make this comparison safer.
    evictable = cache_budget.evictable_bytes()
    census = cache_budget.totals()
    assert census.get("video_readers", 0) > 0, (
        f"an open video reader must show up in the census: {census!r}"
    )
    assert evictable <= census.get("encode_memo", 0) + census.get("native_tiles", 0), (
        f"census-only registrants must not leak into the quantity the ceiling "
        f"governs: {evictable} vs {census!r}"
    )

    still = BackgroundImage(controller, Image.new("RGB", (16, 16), (10, 20, 30)))
    controller.background.set_image(still, update=False)
    assert controller.native_tile_cache.budget_min_age_s == cache_budget.DEFAULT_MIN_AGE_S, (
        "with the video gone its frame entries are stale -- min-age must revert to "
        "the default so they become immediately eligible"
    )

    print("PASS: the native tile cache's min-age tracks the background video's loop duration")


def check_gif_frames_census(controller) -> None:
    """A retained GIF frame list is the largest uncapped image holder.

    The list is the per-frame memo of the asset, so it is not evictable, and
    this census column sizes whether an aggregate cap is warranted. Only an
    alpha-carrying GIF keeps a frame list.
    """
    from src.backend.DeckManagement.DeckController import KeyGIF

    path = os.path.join(gl.DATA_PATH, "media", "budget_census.gif")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = []
    for i in range(8):
        frame = Image.new("RGBA", (240, 240), (0, 0, 0, 0))  # transparent -> frame list
        ImageDraw.Draw(frame).ellipse([20 + i, 20, 220 + i, 220],
                                      fill=(i * 20 % 256, 30, 30, 255))
        frames.append(frame)
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=80, loop=0, disposal=2)

    key = sorted(controller.inputs[Input.Key], key=lambda k: k.index)[0]
    gif = KeyGIF(controller_key=key, gif_path=path, fps=30, loop=True)
    try:
        expected = sum(f.width * f.height * len(f.getbands()) for f in gif.frames)
        assert expected > 0, "fixture sanity: the GIF decoded to nothing"
        assert not gif.budget_evictable, "GIF frames are census-only, never evictable"
        assert cache_budget.totals().get("gif_frames", 0) >= expected, (
            f"the decoded frame list must be visible in the census: "
            f"{cache_budget.totals()!r} (expected >= {expected})"
        )
        assert gif.budget_bytes() == expected
    finally:
        del gif

    print("PASS: decoded GIF frame lists are visible in the census")


def check_close_zeroes_share(controller) -> None:
    """close() step 7 clears both caches, so a torn-down deck stops counting.

    The weak registry drops it whenever GC gets to it, with no unregister call
    on the teardown path.
    """
    before = cache_budget.evictable_bytes()
    share = _deck_bytes(controller)
    assert share > 0, "fixture sanity: the deck being closed should hold cached bytes"

    fixtures.teardown(controller)

    assert _deck_bytes(controller) == 0, "close() must clear the deck's image caches"
    assert cache_budget.evictable_bytes() <= before - share, (
        f"a closed deck's share must leave the budget: "
        f"{cache_budget.evictable_bytes()} vs {before - share}"
    )

    print("PASS: closing a deck returns its whole share to the budget")


def main() -> None:
    fixtures.start_watchdog(180, label="scenario_cache_budget_integration")

    red = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "budget_red.png"), color=(220, 20, 20))
    blue = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "budget_blue.png"), color=(20, 20, 220))
    fixtures.seed_page_with_background("BudgetBusy", red)
    fixtures.seed_page_with_background("BudgetIdle", blue)

    busy = fixtures.make_headless_controller(serial="budget-busy", page_name="BudgetBusy")
    idle = fixtures.make_headless_controller(serial="budget-idle", page_name="BudgetIdle")
    assert fixtures.wait_until(lambda: busy.active_page is not None, timeout=5)
    assert fixtures.wait_until(lambda: idle.active_page is not None, timeout=5)
    time.sleep(0.5)

    try:
        check_registration_and_cross_deck_eviction(busy, idle)
        check_tile_min_age_tracks_video(busy)
        check_gif_frames_census(busy)
        check_close_zeroes_share(idle)
    finally:
        _set_ceiling(None)
        # Tear both down, always. A controller left alive keeps its non-daemon
        # threads running and the interpreter would never exit on a failing
        # assert. Teardown is bounded and safe to repeat.
        fixtures.teardown(busy)
        fixtures.teardown(idle)

    print("PASS: scenario_cache_budget_integration")


if __name__ == "__main__":
    main()
