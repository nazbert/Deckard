"""Regression scenario for four DeckManagement contract bugs.

Each one is a declaration that claimed a value could never be absent, or
claimed the wrong shape, while the code around it disagreed.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Media.Media import Media
from src.backend.DeckManagement.Subclasses.RemoteDeck import RemoteDeck
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver


def check_remote_deck_is_not_touch() -> None:
    deck = RemoteDeck(None, serial_number="remote-deck-test", deck_type="Remote Deck Test")

    touch = deck.is_touch()
    assert touch is False, (
        f"RemoteDeck.is_touch() returned {touch!r}; a bound method (truthy) here is the "
        "original bug -- it makes every is_touch() caller treat a remote deck as having "
        "a touchscreen strip"
    )
    assert deck.is_visual() is True, "fixture sanity: a remote deck is still a visual deck"
    assert deck.dial_count() == 0, "fixture sanity: a remote deck has no dials"


def check_remote_deck_key_callback_slot() -> None:
    deck = RemoteDeck(None, serial_number="remote-deck-test", deck_type="Remote Deck Test")

    assert hasattr(deck, "key_callback"), (
        "RemoteDeck must own a key_callback slot from construction: RemoteDeckManager "
        "delivers presses via `deck.key_callback(...)` without going through a hasattr"
    )
    assert deck.key_callback is None, (
        f"an unregistered key_callback must read as None, got {deck.key_callback!r}"
    )

    received = []
    deck.set_key_callback(lambda *args: received.append(args))
    deck.key_callback(deck, 3, True)
    assert received == [(deck, 3, True)], (
        f"set_key_callback must install the callable it was handed, got {received!r}"
    )


class _StubController:
    """Just enough of DeckController for ScreenSaver.__init__.

    The constructor only stores the reference.
    """


def check_screensaver_stash_is_mapping() -> None:
    screen_saver = ScreenSaver(_StubController())

    stash = screen_saver.original_inputs
    assert isinstance(stash, dict), (
        f"ScreenSaver.original_inputs must start as the empty MAPPING it later holds, "
        f"got {type(stash).__name__}"
    )
    assert stash == {}, f"expected an empty stash, got {stash!r}"
    # This is the exact comparison DeckController.close() makes, and what
    # scenario_deck_close asserts after teardown.
    assert screen_saver.original_inputs == {}
    assert screen_saver.original_background is None
    assert screen_saver.timer is None
    assert screen_saver.media_path is None


def check_unusable_path_composes_none() -> None:
    # Neither an image nor an SVG, and not on disk at all.
    media = Media.from_path("/nonexistent/not-an-image.txt")

    assert media.layers == [], (
        f"an unusable media path must produce NO layers, got {media.layers!r} "
        "(a [None] layer list is what used to crash get_final_media)"
    )
    assert media.get_final_media() is None, (
        "an empty media must compose to None rather than raising"
    )


def check_is_video_none_false() -> None:
    result = is_video(None)
    assert result is False, (
        f"is_video(None) is declared `-> bool` and must return False, got {result!r}"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_deckmanagement_none_contracts")
    check_remote_deck_is_not_touch()
    check_remote_deck_key_callback_slot()
    check_screensaver_stash_is_mapping()
    check_unusable_path_composes_none()
    check_is_video_none_false()
    print("PASS: scenario_deckmanagement_none_contracts")


if __name__ == "__main__":
    main()
