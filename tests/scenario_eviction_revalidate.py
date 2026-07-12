"""
Scenario: page-cache eviction must not gut a live page (issue #4 / B-04).

clear_old_cached_pages snapshotted evictable pages under _pages_lock, then
ran clear_action_objects() + pop OUTSIDE it, with three windows:

  1. A controller's screensaver-pending page (non-active, ready_to_clear,
     held for the whole screensaver duration) was invisible to the guards:
     evicted, ScreenSaver.hide() then loaded a page whose every action was
     dead.
  2. Activation TOCTOU: a page activated between the snapshot and the
     out-of-lock gutting got its actions torn down while ACTIVE (made
     deterministic here: the first eviction's clear_action_objects hook
     activates the second candidate).
  3. Gut-then-pop meant a concurrent get_page() could still be handed the
     gutted object before the pop. Post-fix the pop is INSIDE the lock and
     BEFORE the teardown, so a get_page() during the teardown gap mints a
     fresh Page (via #28's single-flight builder) instead of the corpse.

Post-fix: per-item re-validation under _pages_lock (skip if replaced,
re-marked, active anywhere, or screensaver-pending) and pop-before-teardown.
Plus a non-vacuous check: genuinely stale pages still get evicted.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog


class StubController:
    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def fill_cache(controller, n: int, prefix: str):
    pages = []
    for i in range(n):
        path = seed_page(f"{prefix}{i}")
        page = gl.page_manager.get_page(path, controller)
        page.ready_to_clear = True
        pages.append(page)
    return pages


def actions_alive(page) -> bool:
    # clear_action_objects() empties every state dict; our seeded pages have
    # no actions, so use a sentinel injected into action_objects instead.
    return bool(page.action_objects.get("sentinel"))


def arm(page):
    # Real schema depth: type -> json_identifier -> state -> {index: action}.
    page.action_objects["sentinel"] = {"0x0": {0: {0: object()}}}


def main() -> int:
    start_watchdog(30, "eviction_revalidate")
    fixtures._install_integration_globals()

    controller = StubController("evict-1")
    gl.deck_manager.deck_controller.append(controller)
    gl.page_manager.max_pages = 3

    # --- 1) screensaver-pending page survives eviction pressure ---
    pages = fill_cache(controller, 6, "Evict")
    pending = pages[0]  # oldest -> first eviction candidate
    arm(pending)
    controller._screensaver_pending_page = pending
    controller.active_page = pages[-1]

    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(pending):
        print("FAIL(1): the screensaver-pending page was gutted -- hide() "
              "would load a page whose every action is dead")
        return 1
    print("PASS: screensaver-pending page survives eviction")

    # Non-vacuous: pressure was real -- some page DID get evicted.
    cached = gl.page_manager.pages[controller]
    if len(cached) > gl.page_manager.max_pages + 1:  # +1: pending kept
        print(f"FAIL: eviction did nothing ({len(cached)} cached, "
              f"max {gl.page_manager.max_pages}) -- guard is vacuous")
        return 1
    print("PASS: stale pages still get evicted under pressure")

    # --- 2) activation between snapshot and teardown (deterministic) ---
    controller2 = StubController("evict-2")
    gl.deck_manager.deck_controller.append(controller2)
    pages2 = fill_cache(controller2, 6, "Toctou")
    victim_a, victim_b = pages2[0], pages2[1]
    arm(victim_a)
    arm(victim_b)
    controller2.active_page = pages2[-1]

    real_clear = victim_a.clear_action_objects

    def clear_and_activate():
        # Runs during the eviction loop, outside the lock: the page-switch
        # that the snapshot could not see.
        controller2.active_page = victim_b
        real_clear()

    victim_a.clear_action_objects = clear_and_activate

    gl.page_manager.clear_old_cached_pages()

    if not actions_alive(victim_b):
        print("FAIL(2): a page activated mid-eviction was gutted while "
              "ACTIVE (snapshot TOCTOU)")
        return 1
    print("PASS: page activated mid-eviction is skipped by re-validation")

    # --- 3) pop-before-teardown: a get_page() during the teardown gap gets
    # a FRESH Page, not the gutted corpse (window 3, deterministic) ---
    # The pop happens INSIDE the lock BEFORE clear_action_objects(), so while
    # the corpse is being torn down (outside the lock) the cache slot is
    # already empty -- a concurrent get_page() must mint a new Page via #28's
    # single-flight builder rather than hand back the object being gutted.
    # Made deterministic by having the victim's teardown itself perform that
    # get_page() and capture what it receives.
    controller3 = StubController("evict-3")
    gl.deck_manager.deck_controller.append(controller3)
    pages3 = fill_cache(controller3, 6, "Refetch")
    victim = pages3[0]  # oldest -> first eviction candidate
    victim_path = victim.json_path
    controller3.active_page = pages3[-1]

    captured = {}
    real_clear3 = victim.clear_action_objects

    def clear_and_refetch():
        # Runs during the eviction loop, outside the lock, AFTER the pop: a
        # concurrent get_page() for the same (controller, path) lands here.
        captured["page"] = gl.page_manager.get_page(victim_path, controller3)
        real_clear3()

    victim.clear_action_objects = clear_and_refetch

    gl.page_manager.clear_old_cached_pages()

    refetched = captured.get("page")
    if refetched is None:
        print("FAIL(3): the teardown-gap get_page() never ran")
        return 1
    if refetched is victim:
        print("FAIL(3): a get_page() during the teardown gap was handed the "
              "gutted corpse -- pop must precede clear_action_objects()")
        return 1
    # And the fresh object is usable (not itself gutted): a newly minted Page
    # has its own action_objects dict, untouched by the victim's teardown.
    arm(refetched)
    if not actions_alive(refetched):
        print("FAIL(3): the freshly minted Page is not usable")
        return 1
    print("PASS: get_page() during the teardown gap mints a fresh Page, "
          "not the gutted corpse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
