"""A malformed env var must not abort deck init.

MediaPlayerThread.__init__ reads DECKARD_VIDEO_WRITE_HZ and
DECKARD_WRITE_YIELD_MS. A bad value logs a warning and falls back.
"""
import os

# Poison the environment before the thread class ever reads it.
os.environ["DECKARD_VIDEO_WRITE_HZ"] = "fast"
os.environ["DECKARD_WRITE_YIELD_MS"] = "1.5ms"

import fixtures


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_env_var_resilience")

    # Without the fallback this raises ValueError out of __init__.
    controller, media_player, _ = fixtures.make_stub_controller(serial="envvar-1")

    assert media_player._video_write_hz == 30.0, (
        f"malformed DECKARD_VIDEO_WRITE_HZ should fall back to the "
        f"default 30.0, got {media_player._video_write_hz!r}"
    )
    assert media_player._inter_write_yield == 0.0, (
        f"malformed DECKARD_WRITE_YIELD_MS should fall back to the "
        f"default 0, got {media_player._inter_write_yield!r}"
    )
    print("PASS: malformed env vars fall back to defaults without aborting init")

    # Sanity. Well-formed overrides must still take effect.
    os.environ["DECKARD_VIDEO_WRITE_HZ"] = "10"
    os.environ["DECKARD_WRITE_YIELD_MS"] = "3"
    from src.backend.DeckManagement.DeckController import MediaPlayerThread
    tuned = MediaPlayerThread(deck_controller=controller)
    assert tuned._video_write_hz == 10.0, tuned._video_write_hz
    assert abs(tuned._inter_write_yield - 0.003) < 1e-12, tuned._inter_write_yield
    print("PASS: well-formed env overrides still apply")

    print("PASS: scenario_env_var_resilience")


if __name__ == "__main__":
    main()
