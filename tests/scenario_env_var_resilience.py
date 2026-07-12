"""
Regression test for issue #52 (item: malformed env vars abort deck init).

MediaPlayerThread.__init__ reads STREAMCONTROLLER_VIDEO_WRITE_HZ and
STREAMCONTROLLER_WRITE_YIELD_MS from the environment. A malformed value
(e.g. "fast") used to raise ValueError out of __init__, which
DeckManager swallows as "Failed to initialize deck" -- the deck was
silently skipped over a tuning knob typo. A bad env var must instead log
a warning and fall back to the built-in default.
"""
import os

# Poison the environment BEFORE the thread class ever reads it.
os.environ["STREAMCONTROLLER_VIDEO_WRITE_HZ"] = "fast"
os.environ["STREAMCONTROLLER_WRITE_YIELD_MS"] = "1.5ms"

import fixtures


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_env_var_resilience")

    # Pre-fix this raises ValueError out of MediaPlayerThread.__init__.
    controller, media_player, _ = fixtures.make_stub_controller(serial="envvar-1")

    assert media_player._video_write_hz == 20.0, (
        f"malformed STREAMCONTROLLER_VIDEO_WRITE_HZ should fall back to the "
        f"default 20.0, got {media_player._video_write_hz!r}"
    )
    assert abs(media_player._inter_write_yield - 0.0015) < 1e-12, (
        f"malformed STREAMCONTROLLER_WRITE_YIELD_MS should fall back to the "
        f"default 1.5ms, got {media_player._inter_write_yield!r}"
    )
    print("PASS: malformed env vars fall back to defaults without aborting init")

    # Sanity: well-formed overrides must still take effect.
    os.environ["STREAMCONTROLLER_VIDEO_WRITE_HZ"] = "10"
    os.environ["STREAMCONTROLLER_WRITE_YIELD_MS"] = "3"
    from src.backend.DeckManagement.DeckController import MediaPlayerThread
    tuned = MediaPlayerThread(deck_controller=controller)
    assert tuned._video_write_hz == 10.0, tuned._video_write_hz
    assert abs(tuned._inter_write_yield - 0.003) < 1e-12, tuned._inter_write_yield
    print("PASS: well-formed env overrides still apply")

    print("PASS: scenario_env_var_resilience")


if __name__ == "__main__":
    main()
