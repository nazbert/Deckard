"""Unit-tier scenario for three HelperMethods regressions.

get_sys_args_without_param returns a new list and never pops past the end.
color_values_to_gdk accepts any sequence and scales alpha into the 0-1 range.
"""
import sys

import fixtures

from src.backend.DeckManagement import HelperMethods

WATCHDOG_SECONDS = 30


def check_get_sys_args_without_param() -> None:
    original = ["prog", "--data", "/tmp/x", "--devel"]
    saved_argv = sys.argv
    sys.argv = list(original)
    try:
        # A param with a value drops both the param and its value.
        args = HelperMethods.get_sys_args_without_param("--data")
        assert args == ["prog", "--devel"], args
        assert sys.argv == original, "sys.argv must not be mutated in place"

        # A param as the last argv element has no value to drop, and must not
        # raise.
        args = HelperMethods.get_sys_args_without_param("--devel")
        assert args == ["prog", "--data", "/tmp/x"], args
        assert sys.argv == original, "sys.argv must not be mutated in place"

        # With no match everything is returned, still as a copy.
        args = HelperMethods.get_sys_args_without_param("--missing")
        assert args == original, args
        assert args is not sys.argv, "must return a new list, not sys.argv itself"
    finally:
        sys.argv = saved_argv

    print("PASS: get_sys_args_without_param is bounds-safe and leaves sys.argv alone")


def check_color_values_to_gdk() -> None:
    # A tuple must not crash on a missing append attribute.
    rgba = HelperMethods.color_values_to_gdk((255, 0, 0))
    assert round(rgba.red, 2) == 1.0, rgba
    assert rgba.alpha == 1.0, "3-element input must default to fully opaque"

    # A list must not gain a fourth element in the caller's own object.
    values = [0, 128, 255]
    HelperMethods.color_values_to_gdk(values)
    assert values == [0, 128, 255], f"argument must not be mutated, got {values}"

    # A 4-element input is still accepted unchanged.
    values4 = (10, 20, 30, 255)
    rgba4 = HelperMethods.color_values_to_gdk(values4)
    assert rgba4 is not None

    print("PASS: color_values_to_gdk accepts tuples and never mutates its argument")


def check_alpha_round_trip() -> None:
    """Alpha arrives as 0-255, like the other three channels, and CSS wants 0-1.

    Feeding the raw value into the CSS string clamps every alpha of 1 or more
    to fully opaque, so only 0 and 255 survive the round trip.
    """
    for alpha in (0, 1, 64, 128, 200, 254, 255):
        rgba = HelperMethods.color_values_to_gdk((10, 20, 30, alpha))
        assert abs(rgba.alpha - alpha / 255) < 0.01, (
            f"alpha {alpha}/255 became {rgba.alpha} on the Gdk side -- the "
            f"0-255 value is being handed to CSS rgba(), which reads 0-1"
        )
        got = HelperMethods.gdk_color_to_values(rgba)
        assert got == (10, 20, 30, alpha), (
            f"alpha {alpha} round-tripped to {got} -- the chooser would show "
            f"(and then persist) a different transparency than the label has"
        )

    # The rgb channels stay 0-255, because CSS takes those unscaled, and a
    # 3-element input is still fully opaque.
    assert HelperMethods.gdk_color_to_values(
        HelperMethods.color_values_to_gdk((10, 20, 30))) == (10, 20, 30, 255)

    # The built-in outline-colour default must survive the fixed scale. A
    # default of (0,0,0,1) only looks opaque while the clamp rounds any alpha
    # of 1 or more up, so the default and the conversion are pinned together.
    from src.backend.SettingsManager import FONT_DEFAULTS
    default_outline = FONT_DEFAULTS["outline-color"]
    rgba = HelperMethods.color_values_to_gdk(default_outline)
    assert rgba.alpha > 0.99, (
        f"the built-in outline-colour default {default_outline} renders a "
        f"transparent swatch (alpha {rgba.alpha:.3f}) under the 0-255 scale"
    )

    print("PASS: color_values_to_gdk round-trips every alpha, not just 0 and 255")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_helper_methods")

    check_get_sys_args_without_param()
    check_color_values_to_gdk()
    check_alpha_round_trip()

    print("PASS: scenario_helper_methods")


if __name__ == "__main__":
    main()
