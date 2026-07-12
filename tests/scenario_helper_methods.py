"""
Unit-tier scenario for HelperMethods regressions (issue #53 items 5 and 6):

  (a) get_sys_args_without_param must not pop past the end of argv when the
      matched parameter is the last element, and must return a NEW list --
      it used to filter sys.argv in place, corrupting it for every later
      reader.
  (b) color_values_to_gdk must accept any 3- or 4-element sequence (it used
      to call .append on its argument -- crashing on tuples -- and mutate
      the caller's list when given one).
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
        # Param with a value: both the param and its value are dropped.
        args = HelperMethods.get_sys_args_without_param("--data")
        assert args == ["prog", "--devel"], args
        assert sys.argv == original, "sys.argv must not be mutated in place"

        # Param as the LAST argv element: no value to drop, must not raise.
        args = HelperMethods.get_sys_args_without_param("--devel")
        assert args == ["prog", "--data", "/tmp/x"], args
        assert sys.argv == original, "sys.argv must not be mutated in place"

        # No match: everything returned, still a copy.
        args = HelperMethods.get_sys_args_without_param("--missing")
        assert args == original, args
        assert args is not sys.argv, "must return a new list, not sys.argv itself"
    finally:
        sys.argv = saved_argv

    print("PASS: get_sys_args_without_param is bounds-safe and leaves sys.argv alone")


def check_color_values_to_gdk() -> None:
    # Tuples used to crash ('tuple' object has no attribute 'append').
    rgba = HelperMethods.color_values_to_gdk((255, 0, 0))
    assert round(rgba.red, 2) == 1.0, rgba
    assert rgba.alpha == 1.0, "3-element input must default to fully opaque"

    # Lists used to gain a 4th element in the caller's own object.
    values = [0, 128, 255]
    HelperMethods.color_values_to_gdk(values)
    assert values == [0, 128, 255], f"argument must not be mutated, got {values}"

    # 4-element input still accepted unchanged.
    values4 = (10, 20, 30, 255)
    rgba4 = HelperMethods.color_values_to_gdk(values4)
    assert rgba4 is not None

    print("PASS: color_values_to_gdk accepts tuples and never mutates its argument")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_helper_methods")

    check_get_sys_args_without_param()
    check_color_values_to_gdk()

    print("PASS: scenario_helper_methods")


if __name__ == "__main__":
    main()
