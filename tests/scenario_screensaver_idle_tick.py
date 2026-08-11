"""
An idle deck showing a static screensaver must cost nothing per tick -- and
must not depend on that per-tick cost to stay visible.

While ScreenSaver.show() owns the deck, `deck_controller.inputs` is the
freshly built set init_inputs() installed -- no action, no media and no
label on any of it, and ActionCore.get_is_present() refuses plugin writes
for the duration -- while the screensaver's own imagery lives in
`background`. So the action-tick loop has no per-input work: no actions to
tick, and nothing whose composite could have changed since the last frame.

Pinned here over a REAL DeckController with its REAL tick thread:

  1. the tick loop keeps running while the screensaver shows, so 2. is not
     vacuously satisfied by a dead thread;
  2. across several complete iterations it calls update() on no input at
     all. THIS is the discriminating check -- it is what fails against a
     loop that repaints the whole input set once a second;
  3. the device stays untouched for that same window. Corroboration only:
     it also held before this change, because the dedup guard swallowed the
     steady-state repaints -- it is the composite and hash cost upstream of
     the guard that 2. is about;
  4. hide() still repaints every key, so the wake path is untouched;
  5. and -- the reason 2. is safe to assert at all -- a Clear that executes
     AFTER the screensaver paints it was ordered before leaves the deck
     showing the SCREENSAVER, not blank and not the page it replaced. That
     interleave used to be cured by the per-second repaint (_exec_clear
     nulls the dedup hashes, so the next composite was NOT discarded and did
     reach the device); recovery now belongs to the repaint retry
     _exec_clear arms. Checked against the screensaver's own measured
     signature because the recovery repaint runs on the media thread while
     show() is still painting on its own, and the way that race loses is
     the previous page's imagery sticking -- indistinguishable from success
     to a bare "not blank" assertion.

update() calls are attributed by CALLING THREAD, not counted globally: the
media thread legitimately calls the same method (on_media_player_tick, the
pending-full-repaint retry), and conflating the two would pin the wrong
invariant.
"""
import os
import threading
import time

import fixtures
import globals as gl

# Complete tick iterations to observe. TICK_DELAY is 1s, so this is also
# roughly the observation window's length in seconds.
OBSERVED_WINDOWS = 2


def wait_until_quiet(deck, quiet_for: float = 0.5, timeout: float = 15.0) -> bool:
    """Waits until no device write has landed for `quiet_for` seconds, so
    what the window below observes is the tick loop's doing and not the tail
    of show()'s own repaint. Not a fixed sleep: how fast show()'s bulk batch
    drains is not a constant on a loaded machine."""
    deadline = time.monotonic() + timeout
    seen = len(deck.journal())
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = len(deck.journal())
        if now != seen:
            seen = now
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= quiet_for:
            return True
    return False


def all_keys_painted_after(deck, key_count: int, seq: int) -> bool:
    return all(
        (e := deck.last_op_for(f"key:{k}")) is not None and e[1] > seq
        for k in range(key_count)
    )


def idle_tick_costs_nothing(controller, deck, key_count) -> None:
    """1., 2. and 3.: a still screensaver draws no per-input update() from
    the tick thread."""
    lock = threading.Lock()
    updates: dict[str, int] = {}
    marks: dict[str, int] = {}

    def count_updates(controller_input) -> None:
        original = controller_input.update

        def counting(*args, **kwargs):
            with lock:
                name = threading.current_thread().name
                updates[name] = updates.get(name, 0) + 1
            return original(*args, **kwargs)

        controller_input.update = counting

    for input_list in controller.inputs.values():
        for controller_input in input_list:
            count_updates(controller_input)

    # Liveness probe: tick_actions brackets each iteration between a
    # False-call and a True-call of this, so two marks is one iteration.
    # Attributed per thread because this counts iterations of the tick loop
    # specifically; nothing else on this controller drives this method (the
    # key handler holds its page through page_pins.holding instead), so the
    # attribution is what keeps the count a statement about the tick thread
    # rather than about brackets in general.
    original_mark = controller.mark_page_ready_to_clear

    def counting_mark(*args, **kwargs):
        with lock:
            name = threading.current_thread().name
            marks[name] = marks.get(name, 0) + 1
        return original_mark(*args, **kwargs)

    controller.mark_page_ready_to_clear = counting_mark

    def observed_windows() -> int:
        # Minus one: the probe can be installed midway through an iteration,
        # so the first mark seen may be a lone True-call whose body ran
        # before counting started. Discounting it keeps this a lower bound on
        # iterations whose body was FULLY observed -- which is what the
        # update() count below is a statement about.
        with lock:
            return max(0, marks.get("tick_actions", 0) // 2 - 1)

    def tick_updates() -> int:
        with lock:
            return updates.get("tick_actions", 0)

    journal_at_start = len(deck.journal())
    repaint_ts_at_start = controller._last_full_repaint_ts

    assert fixtures.wait_until(
        lambda: observed_windows() >= OBSERVED_WINDOWS,
        timeout=controller.TICK_DELAY * (OBSERVED_WINDOWS + 2) + 20,
    ), (
        f"the action tick loop completed only {observed_windows()} fully observed "
        f"iterations -- the observation window never happened, so nothing below "
        f"is pinned"
    )

    observed = observed_windows()
    assert tick_updates() == 0, (
        f"the action tick repainted inputs {tick_updates()} times across "
        f"{observed} iterations while a static screensaver was showing -- an "
        f"idle deck is re-compositing and re-hashing every input every second"
    )

    if controller._last_full_repaint_ts != repaint_ts_at_start:
        # A >=5s gap between media-loop iterations reads as a suspend/resume
        # and arms a full repaint. Under a loaded test runner that is a
        # scheduling artifact, not the defect this scenario is about, so the
        # corroborating device-silence check is reported and skipped rather
        # than failed. The update() count above is unaffected -- it is
        # attributed to the tick thread by name.
        print("NOTE: a full repaint fired during the window (loaded machine); "
              "skipping the device-silence check")
    else:
        assert len(deck.journal()) == journal_at_start, (
            f"the deck was written to {len(deck.journal()) - journal_at_start} "
            f"times while a static screensaver sat idle"
        )
    print(f"PASS: {observed} fully observed tick iterations under a static "
          f"screensaver, 0 input repaints from the tick thread")


def late_clear_does_not_strand_blank(controller, deck, key_count, blank_hash,
                                     page_sig, ss_sig) -> None:
    """5.: the screensaver-entry interleave the per-second repaint used to
    cure.

    show() submits its Clear on the CONTROL queue and only afterwards
    enqueues the screensaver's paints onto the task slots. A tick that has
    already drained control therefore writes the screensaver first and pops
    the Clear on its NEXT pass -- blanking a deck whose slots are now empty.
    The seq filter cannot save it (those tasks already ran), a still
    screensaver animates nothing, and no other producer is left.

    Reproduced deterministically, with no source edits, by parking the
    writer between its control drain and its task drain and running show()
    inside that gap.
    """
    media_player = controller.media_player
    arm = threading.Event()
    parked = threading.Event()
    release = threading.Event()
    original_check = media_player.check_resume_gap

    def parking_check(now=None):
        # check_resume_gap runs after drain_control_queue() and before
        # perform_media_player_tasks() -- exactly the gap this needs. Parks
        # once, then behaves normally (including for the very call that
        # parked, so no resume-gap bookkeeping is skipped).
        if arm.is_set() and not parked.is_set():
            parked.set()
            release.wait(timeout=20)
        return original_check(now)

    media_player.check_resume_gap = parking_check
    seq_before_show = deck.current_seq()
    try:
        arm.set()
        assert parked.wait(timeout=15), "the media writer never reached the park point"
        # Runs to completion while the writer is parked: the ClearMsg lands
        # on an already-drained control queue, the paints on slots the
        # parked tick is about to drain.
        controller.screen_saver.show()
        assert controller.screen_saver.showing is True
    finally:
        release.set()
        media_player.check_resume_gap = original_check

    # The interleave must actually have happened, or 5. proves nothing: a
    # blank has to land AFTER the screensaver content it overwrote.
    def blanked_after_content() -> bool:
        painted = False
        for entry in deck.journal():
            if entry[1] <= seq_before_show or entry[3] != "key:0":
                continue
            if entry[4] != blank_hash:
                painted = True
            elif painted:
                return True
        return False

    assert fixtures.wait_until(blanked_after_content, timeout=15), (
        "the late-Clear interleave did not reproduce -- the parking probe no "
        "longer lands between the control drain and the task drain, so the "
        "recovery assertion below would be vacuous"
    )

    # ... and the deck must come back to the SCREENSAVER's content, not
    # merely to something non-blank. The repaint races show()'s own
    # update_all_inputs, and the way that race loses is the previous page's
    # imagery sticking -- which a bare "not blank" check would wave through.
    def recovered() -> bool:
        return all(
            (e := deck.last_op_for(f"key:{k}")) is not None and e[4] == ss_sig[k]
            for k in range(key_count)
        )

    ok = fixtures.wait_until(recovered, timeout=15)
    if not ok:
        stuck = {
            k: deck.last_op_for(f"key:{k}")[4]
            for k in range(key_count)
            if deck.last_op_for(f"key:{k}")[4] != ss_sig[k]
        }
        blank_keys = [k for k, h in stuck.items() if h == blank_hash]
        page_keys = [k for k, h in stuck.items() if h == page_sig[k]]
        raise AssertionError(
            "the deck did not come back to the screensaver's content behind a "
            f"showing screensaver. Blank keys {blank_keys} (a Clear that executed "
            "after the screensaver's paints wiped them with nothing left to "
            f"restore the picture); previous-page keys {page_keys} (a recovery "
            "repaint raced show()'s own update_all_inputs and the pre-swap "
            f"composite landed last); other {sorted(set(stuck) - set(blank_keys) - set(page_keys))}"
        )
    print("PASS: a late-executing Clear recovers to the screensaver's own content")


def main() -> None:
    fixtures.start_watchdog(180, label="scenario_screensaver_idle_tick")
    controller = fixtures.make_headless_controller(serial="ss-idle-1")
    try:
        deck = fixtures.raw_deck(controller)
        key_count = controller.deck.key_count()

        # DeckController.__init__'s bootstrap clear() is a deterministic
        # blank written through the same _write_blank_frames() every later
        # Clear uses -- capture its hash as the blank reference before
        # anything else touches the deck.
        blank_hash = next(e[4] for e in deck.journal() if e[3] == "key:0")

        screensaver_png = fixtures.make_test_png(
            os.path.join(gl.DATA_PATH, "media", "idle_saver.png"), color=(0, 160, 220)
        )
        controller.screen_saver.set_media_path(screensaver_png)

        # Let the default page land before the transition under test.
        assert fixtures.wait_until(
            lambda: deck.last_op_for("key:0") is not None, timeout=10
        ), "fixture sanity: the default page never painted"

        assert wait_until_quiet(deck), "the default page never settled"
        page_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

        seq_before_show = deck.current_seq()
        controller.screen_saver.show()
        assert controller.screen_saver.showing is True

        # EVERY key, not just key:0: show()'s repaint is a bulk batch that
        # writes keys one at a time, and a mid-flight batch would otherwise
        # bleed into the observation window.
        assert fixtures.wait_until(
            lambda: all_keys_painted_after(deck, key_count, seq_before_show), timeout=15
        ), "the screensaver never painted every key"
        assert wait_until_quiet(deck), (
            "a STATIC screensaver never stopped writing to the deck -- the window "
            "below cannot attribute anything"
        )

        # Signatures for the recovery check in phase 5. Captured from a
        # CLEAN entry, so "the screensaver's own content" is a measured
        # value rather than an assumption -- and asserted distinct from
        # both the page's and blank, or the check could not tell the three
        # outcomes apart.
        ss_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}
        for k in range(key_count):
            assert ss_sig[k] not in (blank_hash, page_sig[k]), (
                f"fixture sanity: key {k}'s screensaver content is "
                f"indistinguishable from {'blank' if ss_sig[k] == blank_hash else 'the page'}"
            )

        idle_tick_costs_nothing(controller, deck, key_count)

        # --- the wake path must be untouched -------------------------------
        seq_before_hide = deck.current_seq()
        controller.screen_saver.hide()
        assert controller.screen_saver.showing is False
        assert fixtures.wait_until(
            lambda: all_keys_painted_after(deck, key_count, seq_before_hide), timeout=15
        ), "hiding the screensaver no longer repaints every key"
        print("PASS: hide() still repaints every key")

        # hide()'s phase 3 is a load_page(), and load_screensaver() re-reads
        # the media path from the page -- which never persisted one. Re-set
        # it, or the entry below would show a blank background and the blank
        # reference would stop discriminating (see
        # fixtures.seed_page_with_background_and_screensaver's docstring).
        assert wait_until_quiet(deck), "the restored page never settled"
        controller.screen_saver.set_media_path(screensaver_png)
        late_clear_does_not_strand_blank(
            controller, deck, key_count, blank_hash, page_sig, ss_sig
        )
    finally:
        fixtures.teardown(controller)

    print("\nALL PASS: scenario_screensaver_idle_tick")


if __name__ == "__main__":
    main()
