"""
Scenario: BetterDeck rotation mapping and async callback setters
(issues #17 and #18).

  #17: reorder_physical_for_rotation applied the rotation map in the WRONG
       direction (out[p] = orig[logical(p)] instead of
       out[logical(p)] = orig[p]) -- only self-inverse at 0/180, so
       key_states() was scrambled under 90/270 and ControllerKey.__init__
       read the wrong key's press state on rotated decks. Verified against
       get_physical_index as an independent oracle (a different formula)
       plus hand-computed literals.
  #18: the three async callback setters called themselves instead of the
       wrapped deck -> RecursionError for any plugin using them.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import types

from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck


def check_rotation() -> int:
    deck = FaultyFakeDeck(serial_number="rot-1")
    # Independent layout: force a 3x5 so the literals below hold.
    deck.key_layout = lambda: (3, 5)
    better = BetterDeck(deck)

    total = 15
    physical = list(range(total))  # value == its physical index

    for rotation in (0, 90, 180, 270):
        better.set_rotation(rotation)
        out = better.reorder_physical_for_rotation(physical)

        # Permutation sanity: nothing lost or duplicated.
        if sorted(out) != physical:
            print(f"FAIL(#17): rotation {rotation} output is not a "
                  f"permutation: {out}")
            return 1

        # Contract, via the INVERSE formula as oracle: the value from
        # physical slot p must sit at logical slot l where
        # get_physical_index(l) == p.
        for logical in range(total):
            p = better.get_physical_index(logical)
            if out[logical] != physical[p]:
                print(f"FAIL(#17): rotation {rotation}: out[{logical}] = "
                      f"{out[logical]}, expected value from physical slot "
                      f"{p} -- the map is applied in the wrong direction")
                return 1

    # Hand-computed literal (3 rows x 5 cols, rotation 90):
    # get_logical_index(0) = (0%5)*3 + (3-1-0//5) = 2 -> orig[0] lands at out[2].
    better.set_rotation(90)
    out = better.reorder_physical_for_rotation(physical)
    if out[2] != 0:
        print(f"FAIL(#17): literal check: out[2] = {out[2]}, expected 0")
        return 1

    print("PASS: rotation map applied in the correct direction for 0/90/180/270")
    return 0


def check_async_setters() -> int:
    deck = FaultyFakeDeck(serial_number="rot-2")
    received = {}
    deck.set_key_callback_async = lambda cb, loop=None: received.setdefault("key", (cb, loop))
    deck.set_dial_callback_async = lambda cb, loop=None: received.setdefault("dial", (cb, loop))
    deck.set_touchscreen_callback_async = lambda cb, loop=None: received.setdefault("touch", (cb, loop))
    better = BetterDeck(deck)

    async def cb(*a):
        pass

    try:
        better.set_key_callback_async(cb)
        better.set_dial_callback_async(cb)
        better.set_touchscreen_callback_async(cb)
    except RecursionError:
        print("FAIL(#18): async callback setter recursed into itself")
        return 1

    missing = {"key", "dial", "touch"} - set(received)
    if missing:
        print(f"FAIL(#18): setters never reached the wrapped deck: {missing}")
        return 1
    print("PASS: async callback setters delegate to the wrapped deck")
    return 0


def main() -> int:
    start_watchdog(30, "betterdeck_rotation")
    fixtures.install_stub_globals()
    rc = check_rotation()
    rc |= check_async_setters()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
