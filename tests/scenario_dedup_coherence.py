"""
Integration scenario (docs/presenter-migration-plan.md §7 "Suspend/resume
repaint" sibling / §3 dedup coherence, M2): DeckController.clear() must
reset dedup state on every current input before writing the blanks, so a
repaint of visually-IDENTICAL content after a clear is never wrongly
hash-skipped (the "blank-after-clear" bug the plan fixes in M2 -- pre-fix,
the key's cached _last_img_hash/_last_enqueued_hash would still match the
unchanged content and the device would stay permanently blank).

Also covers the touchscreen dedup added in M2 (mirrors ControllerKey.
update's existing dual-hash guard): two identical composites without a
clear in between must produce exactly one device write.
"""
import time

import fixtures


def main() -> None:
    controller = fixtures.make_headless_controller(serial="dedup-1")
    deck = fixtures.raw_deck(controller)

    # DeckController.__init__'s bootstrap clear() is a deterministic
    # blank/alpha image -- capture its hash as the "blank" reference (same
    # technique as scenario_screensaver_entry.py).
    blank_hash = next(e[4] for e in deck.journal() if e[3] == "key:0")

    # Let the default page's real (static) content land.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    content_hash = deck.last_op_for("key:0")[4]
    assert content_hash != blank_hash, "fixture sanity: default page content should not be blank"

    key0 = controller.get_key_by_index(0)

    # --- Sanity: without a clear, repainting identical content is hash-skipped
    # (existing dual-hash behavior, untouched by this plan). ---
    seq_before_noop = deck.current_seq()
    key0.update()
    time.sleep(0.2)
    assert deck.current_seq() == seq_before_noop, (
        "fixture sanity: identical repaint without a clear should hash-skip"
    )

    # --- The dedup-coherence fix: clear() then repaint IDENTICAL content
    # must actually be written, not hash-skipped. ---
    controller.clear()

    def blank_landed():
        e = deck.last_op_for("key:0")
        return e is not None and e[4] == blank_hash and e[1] > seq_before_noop

    ok = fixtures.wait_until(blank_landed, timeout=3)
    assert ok, "clear() must blank key 0"

    seq_after_blank = deck.current_seq()
    key0.update()  # identical content to content_hash

    def repainted_with_same_content():
        e = deck.last_op_for("key:0")
        return e is not None and e[1] > seq_after_blank and e[4] == content_hash

    ok = fixtures.wait_until(repainted_with_same_content, timeout=3)
    assert ok, (
        "identical content must repaint after a clear -- dedup state must be "
        "reset by Clear (plan §3), otherwise the device is stuck on blank"
    )

    # --- Touchscreen dedup (plan §3): two identical composites without a
    # clear in between must produce exactly one device write. ---
    if controller.deck.is_touch():
        from src.backend.DeckManagement.InputIdentifier import Input

        touchscreen = controller.inputs[Input.Touchscreen][0]
        # Force a clean slate so the first update() below is guaranteed to
        # land, decoupling this from whatever the boot/dial-tick machinery
        # already painted.
        touchscreen._last_img_hash = None
        touchscreen._last_enqueued_hash = None

        seq_before_ts = deck.current_seq()
        touchscreen.update()

        def first_ts_landed():
            return len([e for e in deck.ops_after(seq_before_ts) if e[2] == "set_touchscreen_image"]) == 1

        ok = fixtures.wait_until(first_ts_landed, timeout=3)
        assert ok, "first touchscreen paint should land"

        seq_after_first_ts = deck.current_seq()
        touchscreen.update()  # identical composite -- must be a no-op
        time.sleep(0.3)  # let a would-be regression write land

        extra_ts_writes = [e for e in deck.ops_after(seq_after_first_ts) if e[2] == "set_touchscreen_image"]
        assert len(extra_ts_writes) == 0, (
            f"identical touchscreen composite must be hash-skipped, got "
            f"{len(extra_ts_writes)} additional write(s)"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_dedup_coherence")


if __name__ == "__main__":
    main()
