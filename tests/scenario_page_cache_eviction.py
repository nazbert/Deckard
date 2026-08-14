"""
Page-cache eviction budget arithmetic.

clear_old_cached_pages removes exactly (total - max_pages) pages, oldest
page_number first, and a shrink through set_pages_to_cache runs a pass. A
controller with active_page None inflates total but never gives up its own
pages. This unit tier runs stub controllers over the real PageManagerBackend.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog


class StubController:
    """The minimal surface clear_old_cached_pages dereferences, namely a
    serial, an active_page and a _screensaver_pending_page slot."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def reset_world() -> None:
    """Isolate a leg by clearing the controller list and the page cache.

    total sums across every controller in gl.page_manager.pages, and the legs
    share one gl.page_manager, so a prior leg's cached pages would inflate the
    budget and displace evictions."""
    gl.deck_manager.deck_controller.clear()
    gl.page_manager.pages.clear()
    gl.page_manager._loads_in_flight.clear()


def fresh_controller(serial: str) -> StubController:
    c = StubController(serial)
    gl.deck_manager.deck_controller.append(c)
    return c


def cache_page(controller, name: str):
    """Load a fresh page into the cache for controller. Page.__init__ sets
    ready_to_clear True, so every seeded page is evictable unless it is
    active. Returns the Page object."""
    return gl.page_manager.get_page(seed_page(name), controller)


def cached_paths(controller):
    return set(gl.page_manager.pages.get(controller, {}).keys())


def cached_count(controller):
    return len(gl.page_manager.pages.get(controller, {}))


# Leg 1. clear_old_cached_pages removes exactly (total - max_pages) pages.
def leg_excess_count() -> int:
    reset_world()
    controller = fresh_controller("budget-excess")
    # A large budget during setup, so the clear_old_cached_pages that get_page
    # runs after each load evicts no candidate before the set is complete.
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Excess{i}") for i in range(8)]
    controller.active_page = pages[-1]  # one page is active, so never evictable

    if cached_count(controller) != 8:
        print(f"FAIL(1-setup): expected 8 cached, got {cached_count(controller)}")
        return 1

    # total=8, excess = 8 - 3 = 5 must be evicted, leaving 3.
    gl.page_manager.max_pages = 3
    gl.page_manager.clear_old_cached_pages()

    remaining = cached_count(controller)
    if remaining != 3:
        print(f"FAIL(1): excess arithmetic wrong -- expected 3 pages left "
              f"(total 8 - max_pages 3 = 5 evicted), got {remaining} "
              f"({8 - remaining} evicted)")
        return 1

    # The active page is one of the survivors (never evictable).
    if pages[-1].json_path not in cached_paths(controller):
        print("FAIL(1): the active page was evicted")
        return 1
    print("PASS(1): clear_old_cached_pages evicts exactly (total - max_pages)")
    return 0


# Leg 2. Eviction removes the lowest page_number entries first. get_page bumps
# page_number on every access, so a re-touched page outlives an older sibling.
def leg_oldest_first() -> int:
    reset_world()
    controller = fresh_controller("budget-oldest")
    gl.page_manager.max_pages = 100

    # Load in order A, B, C, D. page_number ascends A<B<C<D.
    paths = {name: seed_page(f"Order{name}") for name in ("A", "B", "C", "D")}
    for name in ("A", "B", "C", "D"):
        gl.page_manager.get_page(paths[name], controller)

    # Re-touch A. get_page bumps its page_number to the newest. Now the
    # oldest-by-page_number order is B < C < D < A.
    gl.page_manager.get_page(paths["A"], controller)

    # Make D active, so it is exempt whatever its number. The decision among
    # the rest must be oldest-first.
    controller.active_page = gl.page_manager.pages[controller][paths["D"]]["page"]

    # With budget 2 the total is 4 and the excess is 2. The oldest two
    # evictable pages, B and C, go. A is newest after the re-touch and D is
    # active, so both survive.
    gl.page_manager.max_pages = 2
    gl.page_manager.clear_old_cached_pages()

    survivors = cached_paths(controller)
    if paths["B"] in survivors or paths["C"] in survivors:
        print(f"FAIL(2): eviction was not oldest-first -- B/C should be gone. "
              f"survivors={sorted(p.split('/')[-1] for p in survivors)}")
        return 1
    if paths["A"] not in survivors:
        print("FAIL(2): the re-touched (newest) page A was wrongly evicted -- "
              "page_number bump on access is not respected by the ordering")
        return 1
    if paths["D"] not in survivors:
        print("FAIL(2): the active page D was evicted")
        return 1
    print("PASS(2): eviction removes the lowest-page_number (oldest-access) "
          "pages first")
    return 0


# Leg 3. A shrink through set_pages_to_cache runs an eviction pass. A grow
# evicts nothing.
def leg_set_pages_to_cache_shrink() -> int:
    reset_world()
    controller = fresh_controller("budget-shrink")
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Shrink{i}") for i in range(6)]
    controller.active_page = pages[-1]
    if cached_count(controller) != 6:
        print(f"FAIL(3-setup): expected 6 cached, got {cached_count(controller)}")
        return 1

    # Growing the budget must evict nothing.
    gl.page_manager.set_pages_to_cache(200)
    if cached_count(controller) != 6:
        print(f"FAIL(3): growing the cache budget evicted pages "
              f"({cached_count(controller)} left, expected 6)")
        return 1

    # A shrink must run clear_old_cached_pages. set_pages_to_cache(n) sets
    # max_pages to n + 1, so n=1 gives max_pages 2, total 6 and excess 4.
    gl.page_manager.set_pages_to_cache(1)
    remaining = cached_count(controller)
    if remaining != 2:
        print(f"FAIL(3): set_pages_to_cache(1) -> max_pages 2 should leave 2 "
              f"pages (6 total - 4 excess), got {remaining}")
        return 1
    if pages[-1].json_path not in cached_paths(controller):
        print("FAIL(3): shrink evicted the active page")
        return 1
    print("PASS(3): set_pages_to_cache shrinks the budget and runs an "
          "eviction pass; growing it does not evict")
    return 0


# Leg 4. A controller with active_page None distorts the budget. Its cached
# pages count toward total but never enter the evictable list, so they inflate
# excess and displace evictions onto live controllers. This leg asserts the
# current behavior and fails loudly when the ownership contract changes.
def leg_active_none_distorts_budget() -> int:
    reset_world()
    # A controller mid-init or torn down but not discarded has active_page
    # None and still holds cached pages.
    ghost = fresh_controller("budget-ghost")
    live = fresh_controller("budget-live")
    gl.page_manager.max_pages = 100

    # Ghost holds 4 cached pages but has active_page None.
    cache_page(ghost, "Ghost0")
    cache_page(ghost, "Ghost1")
    cache_page(ghost, "Ghost2")
    cache_page(ghost, "Ghost3")
    # Live controller holds 4 pages, one active.
    live_pages = [cache_page(live, f"Live{i}") for i in range(4)]
    live.active_page = live_pages[-1]
    # ghost.active_page stays None (the distortion condition).

    if cached_count(ghost) != 4 or cached_count(live) != 4:
        print(f"FAIL(4-setup): ghost={cached_count(ghost)} live={cached_count(live)}")
        return 1

    # total is 8 and the budget is 5, so excess is 3. The ghost's 4 pages
    # count toward total but never enter the evictable list, so all 3
    # evictions land on the live controller.
    gl.page_manager.max_pages = 5
    gl.page_manager.clear_old_cached_pages()

    ghost_left = cached_count(ghost)
    live_left = cached_count(live)

    # The ghost's pages are never reclaimed, because active_page None skips
    # them.
    if ghost_left != 4:
        print(f"FAIL(4): an active_page=None controller's pages were evicted "
              f"({ghost_left}/4 left) -- if this changed, the :236 guard was "
              f"altered (a pin-count redesign landing?); rewrite this "
              f"leg to the new budget contract")
        return 1
    # The live controller takes all 3 evictions, from 4 pages down to 1.
    if live_left != 1:
        print(f"FAIL(4): expected the live controller over-evicted to 1 page "
              f"(all 3 excess evictions displaced onto it by the ghost's "
              f"budget distortion), got {live_left} left -- if the distortion "
              f"was fixed (a pin-count redesign), rewrite this leg to "
              f"the new budget contract")
        return 1
    if live.active_page.json_path not in cached_paths(live):
        print("FAIL(4): the live controller's active page was evicted")
        return 1
    print("PASS(4): an active_page=None controller inflates `total` and "
          "displaces all evictions onto live controllers; its own pages are "
          "never reclaimed (audit row-5 budget distortion, documented)")
    return 0


def main() -> int:
    start_watchdog(30, "page_cache_eviction")
    fixtures._install_integration_globals()

    rc = 0
    rc |= leg_excess_count()
    rc |= leg_oldest_first()
    rc |= leg_set_pages_to_cache_shrink()
    rc |= leg_active_none_distorts_budget()
    if rc == 0:
        print("PASS: scenario_page_cache_eviction")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
