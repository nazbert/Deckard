"""Scenario: the fetched-not-yet-activated window in the page cache.

get_page() hands back a Page that nothing references yet: it is not any
controller's active_page, not a screensaver-pending page, and the caller has
not reached its load_page() call. Cache pressure in that window used to tear
the page down anyway -- its action objects gutted and its cache slot popped --
so the caller activated a corpse and the next fetch minted a duplicate Page
for the same (controller, path).

An ownership pin closes it: every fetch reserves what it returns, eviction
refuses to tear down anything reserved, and the reservation is retired when
the page is installed on its deck. The reservation is deliberately bounded to
ONE per deck: a caller can abandon a fetched page or raise before activating
it, and a bound is the only release that does not depend on such a caller
behaving. So an abandoned fetch costs one unevictable page on that deck until
its next fetch or load, never an accumulating leak.

Legs:
  1. fetch-then-pressure: the fetched page survives, is not gutted, and a
     re-fetch returns the SAME object (no twin mint).
  2. abandoned fetches do not accumulate: N fetches abandoned by an exception
     leave at most one page unevictable, and eviction reclaims the rest.
  3. installing the page retires its reservation (the release itself, pinned
     against being dropped).
  4. a page deferred by the screensaver keeps its reservation across the
     stash, because the hand-off back is a window of its own.
  5. bracketed work is counted, not flagged: overlapping brackets on one page
     cannot end each other's protection early, unmatched releases are clamped
     rather than driving the count negative, and a bracket whose body raises
     still releases.
  6. the screensaver hand-off end to end: the deferred page survives cache
     pressure in the gap between hide() popping it and the load installing
     it, with the stash's own reservation already retired by a later fetch.
  7. the tick loop's bracket releases its page even when the tick body
     raises -- the second bracket site, which cannot use the context manager.

Legs 1-2 run on lightweight stub controllers plus the REAL
PageManagerBackend over gl, the pattern scenario_eviction_revalidate.py uses.
Legs 3-5 need a real DeckController: the releases live on the load path.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog
from src.backend.PageManagement import page_pins


class StubController:
    """The minimal surface clear_old_cached_pages dereferences."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def reset_world() -> None:
    """Isolate a leg: legs share the singleton page manager, so a prior leg's
    controllers and cached pages would otherwise inflate `total` and displace
    this leg's evictions."""
    gl.deck_manager.deck_controller.clear()
    gl.page_manager.pages.clear()
    gl.page_manager._loads_in_flight.clear()


def fresh_controller(serial: str) -> StubController:
    c = StubController(serial)
    gl.deck_manager.deck_controller.append(c)
    return c


def arm(page) -> None:
    """Inject a sentinel action object: the seeded pages carry no actions, so
    this is what makes a teardown observable (clear_action_objects empties
    every state dict)."""
    page.action_objects["sentinel"] = {"0x0": {0: {0: object()}}}


def actions_alive(page) -> bool:
    return bool(page.action_objects.get("sentinel"))


# ---------------------------------------------------------------------------
# Leg 1: a page fetched but not yet activated survives eviction pressure, is
# not gutted, and is still the cache's page for its key.
# ---------------------------------------------------------------------------
def leg_fetched_page_survives_pressure() -> int:
    reset_world()
    controller = fresh_controller("pin-fetch")
    # Roomy budget during setup so get_page's own eviction pass does not
    # reclaim the fillers before the leg has built its state.
    gl.page_manager.max_pages = 100

    active = gl.page_manager.get_page(seed_page("PinActive"), controller)
    controller.active_page = active
    fillers = [gl.page_manager.get_page(seed_page(f"PinFiller{i}"), controller)
               for i in range(2)]

    # The page under test: fetched LAST, so this is exactly the state a caller
    # is in between `page = get_page(...)` and `controller.load_page(page)`.
    target_path = seed_page("PinTarget")
    target = gl.page_manager.get_page(target_path, controller)
    arm(target)
    if target is None or len(fillers) != 2:
        print("FAIL(1-setup): the cache was not seeded")
        return 1

    # total = 4, budget 1 -> excess 3, and exactly 3 entries are evictable
    # (everything but the active page). Without a pin the fetched page is one
    # of them: born evictable, referenced by nobody the cache can see.
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(target):
        print("FAIL(1): the page fetched but not yet activated was gutted -- "
              "its caller is about to hand a corpse to load_page")
        return 1
    cached = gl.page_manager.pages.get(controller, {})
    if target_path not in cached:
        print("FAIL(1): the page fetched but not yet activated was evicted "
              "from the cache")
        return 1

    # And the consequence the caller sees: a re-fetch must be the SAME object.
    # A twin Page for one (controller, path) means two sets of action objects
    # registering live event handlers for one key.
    again = gl.page_manager.get_page(target_path, controller)
    if again is not target:
        print("FAIL(1): re-fetching the evicted page minted a twin Page for "
              "one (controller, path)")
        return 1

    # Non-vacuous: the pressure was real -- the unreserved fillers went.
    survivors = set(gl.page_manager.pages.get(controller, {}))
    if any(f.json_path in survivors for f in fillers):
        print("FAIL(1): no eviction happened at all -- the guard is vacuous")
        return 1
    print("PASS(1): a fetched-but-not-yet-activated page survives eviction "
          "pressure intact, and re-fetching it returns the same Page")
    return 0


# ---------------------------------------------------------------------------
# Leg 2: abandoned fetches are bounded, not accumulated. A caller that raises
# between the fetch and the load never runs a release, so the bound is the
# only thing standing between this and a cache that fills with unevictable
# pages: one reservation per deck, retired by that deck's next fetch.
# ---------------------------------------------------------------------------
def leg_abandoned_fetches_do_not_accumulate() -> int:
    reset_world()
    controller = fresh_controller("pin-abandon")
    gl.page_manager.max_pages = 100

    controller.active_page = gl.page_manager.get_page(seed_page("AbandonHome"),
                                                      controller)

    abandoned = []
    for i in range(3):
        try:
            abandoned.append(
                gl.page_manager.get_page(seed_page(f"PinAbandon{i}"), controller))
            raise RuntimeError("the caller blew up before activating the page")
        except RuntimeError:
            pass

    # total = 4, budget 1 -> excess 3. Only the newest fetch is still
    # reserved, so two of the three abandoned pages must go.
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    survivors = set(gl.page_manager.pages.get(controller, {}))
    still_here = [p for p in abandoned if p.json_path in survivors]
    if len(still_here) != 1 or still_here[0] is not abandoned[-1]:
        print(f"FAIL(2): abandoned fetches accumulated -- "
              f"{len(still_here)} of 3 survived eviction (expected only the "
              f"newest). A dropped release must cost one page per deck, not "
              f"one per fetch")
        return 1

    # And the last one is not special either: the deck's next fetch retires
    # its reservation, so it becomes evictable like the rest.
    gl.page_manager.max_pages = 100
    gl.page_manager.get_page(seed_page("AbandonNext"), controller)
    gl.page_manager.max_pages = 1
    gl.page_manager.clear_old_cached_pages()

    if abandoned[-1].json_path in set(gl.page_manager.pages.get(controller, {})):
        print("FAIL(2): the deck's next fetch did not retire the previous "
              "reservation -- the bound leaks one page per deck forever")
        return 1
    print("PASS(2): an abandoned fetch costs at most one unevictable page per "
          "deck, retired by that deck's next fetch")
    return 0


# ---------------------------------------------------------------------------
# Leg 3: installing the page releases the reservation. This is the release
# itself under test, so nothing else may retire the reservation in between --
# no second fetch, which would retire it by the bound instead.
# ---------------------------------------------------------------------------
def leg_install_releases_reservation(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinInstall"), controller)
    if not pins.is_pinned(page):
        print("FAIL(3): get_page did not reserve the page it returned")
        return 1

    controller.load_page(page)
    if not fixtures.wait_until(lambda: not pins.is_pinned(page)):
        print("FAIL(3): installing the page left its fetch reservation in "
              "place -- the deck now carries a page it can never evict")
        return 1
    print("PASS(3): installing a page on its deck releases the fetch "
          "reservation")
    return 0


# ---------------------------------------------------------------------------
# Leg 4: the screensaver hand-off. A page change while the screensaver shows
# is stashed, not installed, so its reservation must NOT be released there --
# the stash and the reservation together are what carry it until hide() gets
# to install it for real.
# ---------------------------------------------------------------------------
def leg_screensaver_pending_keeps_reservation(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinPending"), controller)

    controller.screen_saver.showing = True
    try:
        controller.load_page(page)
        if controller._screensaver_pending_page is not page:
            print("FAIL(4-setup): the page change was not deferred")
            return 1
        if not pins.is_pinned(page):
            print("FAIL(4): a deferred page change dropped its reservation -- "
                  "nothing carries the page across the gap between hide() "
                  "taking it and the load that installs it")
            return 1
    finally:
        controller.screen_saver.showing = False
        controller._screensaver_pending_page = None

    controller.load_page(page)
    if not fixtures.wait_until(lambda: not pins.is_pinned(page)):
        print("FAIL(4): the deferred page kept its reservation after it was "
              "finally installed")
        return 1
    print("PASS(4): a screensaver-deferred page keeps its reservation until "
          "the load that installs it")
    return 0


# ---------------------------------------------------------------------------
# Leg 5: bracketed work is counted, not flagged. The tick loop and a key
# gesture bracket the same page routinely; with a flag the first release ends
# the protection the second is still relying on.
# ---------------------------------------------------------------------------
def leg_brackets_are_counted(controller) -> int:
    pins = gl.page_manager.pins
    page = gl.page_manager.get_page(seed_page("PinBracket"), controller)
    base = pins.count(page)

    controller.mark_page_ready_to_clear(False, page)
    controller.mark_page_ready_to_clear(False, page)
    if pins.count(page) != base + 2:
        print(f"FAIL(5): two brackets on one page counted as "
              f"{pins.count(page) - base}, not 2")
        return 1

    controller.mark_page_ready_to_clear(True, page)
    if not pins.is_pinned(page):
        print("FAIL(5): one bracket ending released the page while the other "
              "was still working on it")
        return 1

    controller.mark_page_ready_to_clear(True, page)
    if pins.count(page) != base:
        print(f"FAIL(5): the brackets did not balance -- {pins.count(page) - base} "
              f"holders left over")
        return 1

    # Unmatched releases are clamped: they must not drive the count negative,
    # where a later real holder's pin would read as already released.
    controller.mark_page_ready_to_clear(True, page)
    controller.mark_page_ready_to_clear(True, page)
    controller.mark_page_ready_to_clear(False, page)
    if not pins.is_pinned(page):
        print("FAIL(5): unmatched releases drove the count below zero -- a "
              "real holder's pin no longer protects its page")
        return 1
    controller.mark_page_ready_to_clear(True, page)

    # A bracket whose body raises must still release. A count, unlike the flag
    # it replaced, does not heal on the next bracket: one skipped release pins
    # the page for the life of the process, and a caller that swallows the
    # traceback (an emulated press dispatched on the GTK loop) repeats it
    # every time.
    base = pins.count(page)
    for _ in range(3):
        try:
            with page_pins.holding(page):
                raise RuntimeError("the bracketed work blew up")
        except RuntimeError:
            pass
    if pins.count(page) != base:
        print(f"FAIL(5): a bracket whose body raised kept "
              f"{pins.count(page) - base} holder(s) -- permanently, and once "
              f"per raising call")
        return 1
    print("PASS(5): overlapping brackets count, unmatched releases are "
          "clamped at zero, and a raising bracket still releases")
    return 0


# ---------------------------------------------------------------------------
# Leg 6: the screensaver hand-off, driven end to end. hide() pops the pending
# page under the load lock and installs it after releasing that lock, so in
# between the page is neither pending nor active. The stash's own reservation
# does not cover it either: this deck fetched again while the screensaver was
# up (window cycling does exactly that), which retires the older reservation.
# The re-reservation hide() takes at the pop is all that is left.
# ---------------------------------------------------------------------------
def leg_screensaver_handoff_survives_pressure(controller) -> int:
    saver = controller.screen_saver
    deferred = gl.page_manager.get_page(seed_page("PinHandoff"), controller)
    arm(deferred)

    saver.show()
    try:
        controller.load_page(deferred)
        if controller._screensaver_pending_page is not deferred:
            print("FAIL(6-setup): the page change was not deferred")
            return 1
        # Another fetch on this deck, exactly as window cycling produces:
        # the deferred page's own reservation is retired by it.
        gl.page_manager.get_page(seed_page("PinHandoffOther"), controller)

        # Squeeze the cache in the hand-off gap: after hide() has popped the
        # pending page, before the follow-up installs it.
        real_followup = saver._hide_followup

        def pressure_then_install(*args, **kwargs):
            gl.page_manager.max_pages = 1
            gl.page_manager.clear_old_cached_pages()
            return real_followup(*args, **kwargs)

        saver._hide_followup = pressure_then_install
        try:
            saver.hide()
        finally:
            saver._hide_followup = real_followup
            gl.page_manager.max_pages = 100
    finally:
        saver.showing = False
        controller._screensaver_pending_page = None

    if not actions_alive(deferred):
        print("FAIL(6): the page the screensaver was holding was gutted in "
              "the hand-off gap -- dismissing the screensaver restores a page "
              "whose every action is dead")
        return 1
    if controller.active_page is not deferred:
        print("FAIL(6): dismissing the screensaver did not install the "
              "deferred page")
        return 1
    print("PASS(6): the page deferred by a screensaver survives cache "
          "pressure in the gap between the hand-off and its load")
    return 0


# ---------------------------------------------------------------------------
# Leg 7: the tick loop's bracket is a hand-written pair rather than a `with`,
# because the liveness probe other scenarios hang off it counts both calls. So
# its release lives in a `finally`: a tick body that raises would otherwise
# leave the page it marked pinned for good. Driven on a throwaway deck,
# because the raise ends that deck's ticking.
# ---------------------------------------------------------------------------
def leg_tick_bracket_releases_on_error() -> int:
    pins = gl.page_manager.pins
    controller = fixtures.make_headless_controller(serial="pin-tick",
                                                   page_name="PinTickHome")
    try:
        page = controller.active_page
        # A clean baseline: the active page carries no reservation once it is
        # installed, but the tick loop brackets it on its own clock.
        if page is None or not fixtures.wait_until(lambda: pins.count(page) == 0):
            print("FAIL(7-setup): the deck's page never settled unpinned")
            return 1

        def boom():
            raise RuntimeError("a tick body blew up")

        # Restored as soon as the tick thread is gone: the media thread reads
        # the same states, and its log.catch would swallow this on every
        # frame for as long as the injection stands.
        patched = [i for input_list in controller.inputs.values()
                   for i in input_list]
        originals = [i.get_active_state for i in patched]
        for controller_input in patched:
            controller_input.get_active_state = boom
        try:
            died = fixtures.wait_until(
                lambda: not controller.tick_thread.is_alive(),
                timeout=controller.TICK_DELAY * 4 + 5)
        finally:
            for controller_input, original in zip(patched, originals):
                controller_input.get_active_state = original

        if not died:
            print("FAIL(7-setup): the tick body never raised")
            return 1
        if pins.count(page) != 0:
            print(f"FAIL(7): a tick body that raised left the page it marked "
                  f"pinned ({pins.count(page)} holder(s)) -- unevictable for "
                  f"the life of the process")
            return 1
    finally:
        fixtures.teardown(controller)
    print("PASS(7): a tick body that raises still releases the page its "
          "bracket marked")
    return 0


def main() -> int:
    start_watchdog(60, "page_pin_counts")
    fixtures._install_integration_globals()

    rc = 0
    rc |= leg_fetched_page_survives_pressure()
    rc |= leg_abandoned_fetches_do_not_accumulate()

    # The remaining legs drive the real load path, so they need a real
    # controller rather than the stubs above.
    reset_world()
    gl.page_manager.max_pages = 100
    controller = fixtures.make_headless_controller(serial="pin-load",
                                                   page_name="PinLoadHome")
    try:
        rc |= leg_install_releases_reservation(controller)
        rc |= leg_screensaver_pending_keeps_reservation(controller)
        rc |= leg_brackets_are_counted(controller)
        rc |= leg_screensaver_handoff_survives_pressure(controller)
    finally:
        fixtures.teardown(controller)
    rc |= leg_tick_bracket_releases_on_error()

    if rc == 0:
        print("PASS: scenario_page_pin_counts")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
