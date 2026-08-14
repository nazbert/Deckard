"""Pins the BetterDeck rotation mapping and the async callback setters.

reorder_physical_for_rotation must write out[logical(p)] = orig[p], checked
against get_physical_index. The three async setters call the wrapped deck.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)


from fixtures import FaultyFakeDeck, start_watchdog

from src.backend.DeckManagement.BetterDeck import BetterDeck


def check_rotation() -> int:
    deck = FaultyFakeDeck(serial_number="rot-1")
    # Force a 3x5 layout, so the literals below hold.
    deck.key_layout = lambda: (3, 5)
    better = BetterDeck(deck)

    total = 15
    physical = list(range(total))  # value == its physical index

    for rotation in (0, 90, 180, 270):
        better.set_rotation(rotation)
        out = better.reorder_physical_for_rotation(physical)

        # The result must stay a permutation, with nothing lost or duplicated.
        if sorted(out) != physical:
            print(f"FAIL(a): rotation {rotation} output is not a "
                  f"permutation: {out}")
            return 1

        # Check against the inverse formula as an oracle. The value from
        # physical slot p must sit at logical slot l where
        # get_physical_index(l) == p.
        for logical in range(total):
            p = better.get_physical_index(logical)
            if out[logical] != physical[p]:
                print(f"FAIL(a): rotation {rotation}: out[{logical}] = "
                      f"{out[logical]}, expected value from physical slot "
                      f"{p} -- the map is applied in the wrong direction")
                return 1

    # Hand-computed literal for 3 rows by 5 cols at rotation 90.
    # get_logical_index(0) = (0%5)*3 + (3-1-0//5) = 2, so orig[0] lands at out[2].
    better.set_rotation(90)
    out = better.reorder_physical_for_rotation(physical)
    if out[2] != 0:
        print(f"FAIL(a): literal check: out[2] = {out[2]}, expected 0")
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
        print("FAIL(b): async callback setter recursed into itself")
        return 1

    missing = {"key", "dial", "touch"} - set(received)
    if missing:
        print(f"FAIL(b): setters never reached the wrapped deck: {missing}")
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
