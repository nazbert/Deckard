"""
Scenario: page-cache eviction BUDGET arithmetic and the active_page=None
budget-distortion strand (issue #60 / B-04, audit table row 5).

The evict-vs-activate interleave, screensaver-pending survival, the
gut-then-pop window and the ready_to_clear re-point/strand all have
deterministic coverage already (scenario_eviction_revalidate.py legs 1-3,
scenario_small_guards.py ready_to_clear_*). What had ZERO direct coverage is
the plain arithmetic of clear_old_cached_pages:

  * how many pages `excess = total - max_pages` actually removes,
  * that it removes the OLDEST (lowest page_number) first,
  * that set_pages_to_cache(n) shrinking the budget triggers a pass, and
  * the row-5 distortion: a controller with active_page is None has its
    cached pages counted toward `total` (:227) but skipped from the
    evictable list (:236), so it inflates `excess` for OTHER controllers --
    over-evicting live controllers while its own pages are never reclaimed
    (tracked fix: issue #81, pin-count page-cache ownership; leg 4 is its
    tripwire).

Unit tier: lightweight stub controllers + the REAL PageManagerBackend over
gl (same pattern as scenario_eviction_revalidate.py). Pages are seeded
action-free; get_page mints one distinct Page per (controller, path).
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog


class StubController:
    """The minimal surface clear_old_cached_pages dereferences: a serial,
    an active_page, and a _screensaver_pending_page slot (via getattr)."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self._screensaver_pending_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def reset_world() -> None:
    """Isolate a leg: clear the shared controller list and the page cache so
    `total` (summed across ALL controllers in gl.page_manager.pages) reflects
    only this leg's controllers. Legs share the singleton gl.page_manager and
    gl.deck_manager, so without this a prior leg's cached pages inflate the
    budget and displace evictions."""
    gl.deck_manager.deck_controller.clear()
    gl.page_manager.pages.clear()
    gl.page_manager._loads_in_flight.clear()


def fresh_controller(serial: str) -> StubController:
    c = StubController(serial)
    gl.deck_manager.deck_controller.append(c)
    return c


def cache_page(controller, name: str):
    """Load a fresh page into the cache for `controller`. Every seeded page
    is born ready_to_clear=True (Page.__init__), so all are evictable unless
    active. Returns the Page object."""
    return gl.page_manager.get_page(seed_page(name), controller)


def cached_paths(controller):
    return set(gl.page_manager.pages.get(controller, {}).keys())


def cached_count(controller):
    return len(gl.page_manager.pages.get(controller, {}))


# ---------------------------------------------------------------------------
# Leg 1: excess arithmetic -- clear_old_cached_pages removes exactly
# (total - max_pages) pages, no more, no fewer.
# ---------------------------------------------------------------------------
def leg_excess_count() -> int:
    reset_world()
    controller = fresh_controller("budget-excess")
    # Enormous budget during setup so get_page's own internal
    # clear_old_cached_pages (fired after every load) never evicts our
    # candidates before we've built the full set.
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Excess{i}") for i in range(8)]
    controller.active_page = pages[-1]  # one page is active -> never evictable

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


# ---------------------------------------------------------------------------
# Leg 2: oldest-first -- eviction removes the lowest page_number entries.
# get_page bumps page_number on every access, so a page re-touched after
# loading becomes "newer" and must survive over an untouched older sibling.
# ---------------------------------------------------------------------------
def leg_oldest_first() -> int:
    reset_world()
    controller = fresh_controller("budget-oldest")
    gl.page_manager.max_pages = 100

    # Load in order A, B, C, D. page_number ascends A<B<C<D.
    paths = {name: seed_page(f"Order{name}") for name in ("A", "B", "C", "D")}
    for name in ("A", "B", "C", "D"):
        gl.page_manager.get_page(paths[name], controller)

    # Re-touch A: get_page bumps its page_number to the newest. Now the
    # oldest-by-page_number order is B < C < D < A.
    gl.page_manager.get_page(paths["A"], controller)

    # Make D active so it is exempt regardless of number; the eviction
    # decision among the rest must be purely oldest-first.
    controller.active_page = gl.page_manager.pages[controller][paths["D"]]["page"]

    # Budget 2: total 4, excess 2. Oldest two evictable (B, C) must go;
    # A (re-touched -> newest) and D (active) must survive.
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


# ---------------------------------------------------------------------------
# Leg 3: set_pages_to_cache(n) shrinking the budget triggers an eviction
# pass; growing it does NOT evict.
# ---------------------------------------------------------------------------
def leg_set_pages_to_cache_shrink() -> int:
    reset_world()
    controller = fresh_controller("budget-shrink")
    gl.page_manager.max_pages = 100

    pages = [cache_page(controller, f"Shrink{i}") for i in range(6)]
    controller.active_page = pages[-1]
    if cached_count(controller) != 6:
        print(f"FAIL(3-setup): expected 6 cached, got {cached_count(controller)}")
        return 1

    # Growing the budget must NOT evict (old_max_pages <= new max_pages).
    gl.page_manager.set_pages_to_cache(200)
    if cached_count(controller) != 6:
        print(f"FAIL(3): growing the cache budget evicted pages "
              f"({cached_count(controller)} left, expected 6)")
        return 1

    # Shrinking must trigger clear_old_cached_pages. set_pages_to_cache(n)
    # sets max_pages = n + 1, so n=1 -> max_pages 2 -> total 6, excess 4.
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


# ---------------------------------------------------------------------------
# Leg 4: active_page=None controller distorts the budget (audit row 5).
# Its cached pages count toward `total` (:227) but are skipped from the
# evictable list (:236). So they inflate `excess` -- over-evicting a LIVE
# controller -- while never being reclaimed themselves.
#
# The tracked fix is issue #81 (pin-count page-cache ownership redesign);
# this leg asserts CURRENT behavior and is a deliberate tripwire: when #81
# (or any change to the total/:236 contract) lands, it fails loudly so the
# leg gets rewritten to the new contract instead of silently passing.
# ---------------------------------------------------------------------------
def leg_active_none_distorts_budget() -> int:
    reset_world()
    # A controller mid-init / torn-down but not yet discarded: active_page
    # is None, yet it holds cached pages.
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

    # total = 8. Budget 5 -> excess 3. WITHOUT the ghost's 4 pages the live
    # controller (4 cached, 1 active -> 3 evictable) would sit comfortably
    # within a 5-page budget for its own pages. But the ghost's 4 pages count
    # toward total, inflating excess to 3, and since the ghost's pages are
    # never in the evictable list, all 3 evictions land on the live
    # controller instead. This documents the distortion: it is the CURRENT
    # behavior, and the point the audit flags.
    gl.page_manager.max_pages = 5
    gl.page_manager.clear_old_cached_pages()

    ghost_left = cached_count(ghost)
    live_left = cached_count(live)

    # The distortion, asserted precisely as current behavior:
    #  - the ghost's pages are NEVER reclaimed (active_page None skips them),
    if ghost_left != 4:
        print(f"FAIL(4): an active_page=None controller's pages were evicted "
              f"({ghost_left}/4 left) -- if this changed, the :236 guard was "
              f"altered (issue #81 pin-count redesign landing?); rewrite this "
              f"leg to the new budget contract")
        return 1
    #  - and the live controller is over-evicted BECAUSE the ghost's dead
    #    weight inflated `total`. excess=3 all lands on live (4 -> 1).
    if live_left != 1:
        print(f"FAIL(4): expected the live controller over-evicted to 1 page "
              f"(all 3 excess evictions displaced onto it by the ghost's "
              f"budget distortion), got {live_left} left -- if the distortion "
              f"was fixed (issue #81 pin-count redesign), rewrite this leg to "
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
