"""
Unit-tier scenario for SingleKeyAsset's error/fallback image (issue #53
item 7): the image was opened via the CWD-relative path
"Assets/images/error.png", so get_raw_image() raised FileNotFoundError
whenever the process was launched from anywhere but the repo root (e.g. a
desktop launcher with a different working directory). It must resolve
against the repo root regardless of CWD.
"""
import os

import fixtures  # noqa: F401  (isolated DATA_PATH + repo-root sys.path)

import globals as gl
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset

WATCHDOG_SECONDS = 30


class _StubControllerInput:
    """Exactly what SingleKeyAsset.__init__ dereferences."""
    deck_controller = None


def check_error_image_resolves_off_repo_root() -> None:
    # Make the CWD explicitly NOT the repo root (run_all.py already runs
    # scenarios from tests/, but don't depend on the runner for redness).
    os.chdir(gl.DATA_PATH)

    asset = SingleKeyAsset(_StubControllerInput())
    image = asset.get_raw_image()
    assert image is not None
    assert image.size[0] > 0 and image.size[1] > 0

    # The memoized copy must serve later callers too, still off-root.
    image2 = asset.get_raw_image()
    assert image2 is not image, "callers must get independent copies"
    assert image2.size == image.size

    print("PASS: error image loads with CWD outside the repo root")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_error_asset")

    check_error_image_resolves_off_repo_root()

    print("PASS: scenario_error_asset")


if __name__ == "__main__":
    main()
