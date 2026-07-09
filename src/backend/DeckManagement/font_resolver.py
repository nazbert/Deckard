"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

Font resolution via fontconfig, replacing matplotlib.font_manager.

matplotlib's font machinery (font_manager) is a multi-megabyte import that
also does a synchronous system font scan the first time it's touched --
fontconfig already does this job (better, since it IS the system's font
database) and every font already goes through it once via Pango/GTK anyway.
This module talks to fontconfig directly through ctypes (falling back to the
`fc-match` binary if the shared library can't be loaded, e.g. on a minimal
container image) so nothing here imports matplotlib on Linux.

Weight scale mismatch (the #1 correctness risk here): the rest of the app
speaks Pango/CSS weights (numeric, 100-900, e.g. 400 = normal, 700 = bold).
fontconfig's own weight scale is 0-215 (regular=80, bold=200) and does NOT
accept raw OpenType/CSS values -- `fc-match "DejaVu Sans:weight=400"`
literally returns DejaVu Sans **Bold**, because 400 on fontconfig's own scale
is well past bold. Every weight value that reaches fontconfig in this module
is first translated with `_ot_weight_to_fc`.
"""
import ctypes
import ctypes.util
import functools
import subprocess
import sys
import threading

from fontTools.ttLib import TTFont

# gl.IS_MAC guards the one spot (fallback_font's caller chain / KeyLabel) that
# still needs a matplotlib-backed resolver; on Linux nothing below ever
# imports matplotlib.
IS_MAC = sys.platform == "darwin"


# --------------------------------------------------------------------- #
# OpenType/CSS (100-900) -> fontconfig (0-215) weight mapping.
#
# This is FcWeightFromOpenTypeDouble's own table (fontconfig's fcweight.c):
# piecewise-linear interpolation between these anchor points. Values here
# are load-bearing -- fontconfig's raw scale is NOT the same as the OT/CSS
# scale the rest of the app uses, and passing an untranslated value silently
# picks the wrong file (see module docstring).
# --------------------------------------------------------------------- #
_OT_TO_FC_WEIGHT = (
    (0, 0),
    (100, 0),
    (200, 40),
    (300, 50),
    (350, 55),
    (380, 75),
    (400, 80),
    (500, 100),
    (600, 180),
    (700, 200),
    (800, 205),
    (900, 210),
    (1000, 215),
)

# fontconfig FC_SLANT values.
_FC_SLANT_ROMAN = 0
_FC_SLANT_ITALIC = 100
_FC_SLANT_OBLIQUE = 110

FC_FAMILY = b"family"
FC_WEIGHT = b"weight"
FC_SLANT = b"slant"
FC_FILE = b"file"

_FC_MATCH_PATTERN = 0  # FcMatchKind.FcMatchPattern


def _ot_weight_to_fc(weight: int) -> int:
    """Translate a numeric Pango/CSS weight (100-900) to fontconfig's 0-215
    scale, via the same piecewise-linear table fontconfig itself uses."""
    if weight is None:
        weight = 400
    weight = max(0, min(1000, weight))

    table = _OT_TO_FC_WEIGHT
    for (ot_lo, fc_lo), (ot_hi, fc_hi) in zip(table, table[1:]):
        if ot_lo <= weight <= ot_hi:
            if ot_hi == ot_lo:
                return fc_lo
            frac = (weight - ot_lo) / (ot_hi - ot_lo)
            return round(fc_lo + frac * (fc_hi - fc_lo))
    return table[-1][1]


def _style_to_fc_slant(style: str) -> int:
    if style == "italic":
        return _FC_SLANT_ITALIC
    if style == "oblique":
        return _FC_SLANT_OBLIQUE
    return _FC_SLANT_ROMAN


def _escape_fc_value(value: str) -> str:
    """Escape characters that are syntactically significant in fontconfig's
    pattern-string mini-language (used only for the fc-match subprocess
    fallback -- the ctypes path sets pattern fields directly and never
    parses a string)."""
    for ch in ("\\", ",", ":", "="):
        value = value.replace(ch, "\\" + ch)
    return value


class _FontConfig:
    """Thin ctypes binding to the handful of libfontconfig entry points we
    need. Lazily initialized (no work happens at import time); a single
    FcConfig is loaded once and reused for the life of the process, guarded
    by a lock since fontconfig's match calls are not documented as safe for
    concurrent use from multiple threads on a shared FcConfig, and label
    rendering can happen off the main thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lib = None
        self._config = None
        self._unavailable = False

    def _ensure_loaded(self) -> bool:
        if self._lib is not None:
            return True
        if self._unavailable:
            return False

        lib_name = ctypes.util.find_library("fontconfig")
        if not lib_name:
            self._unavailable = True
            return False

        try:
            lib = ctypes.CDLL(lib_name)

            lib.FcInitLoadConfigAndFonts.restype = ctypes.c_void_p
            lib.FcInitLoadConfigAndFonts.argtypes = []

            lib.FcPatternCreate.restype = ctypes.c_void_p
            lib.FcPatternCreate.argtypes = []

            lib.FcPatternAddString.restype = ctypes.c_int
            lib.FcPatternAddString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]

            lib.FcPatternAddInteger.restype = ctypes.c_int
            lib.FcPatternAddInteger.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

            lib.FcConfigSubstitute.restype = ctypes.c_int
            lib.FcConfigSubstitute.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]

            lib.FcDefaultSubstitute.restype = None
            lib.FcDefaultSubstitute.argtypes = [ctypes.c_void_p]

            lib.FcFontMatch.restype = ctypes.c_void_p
            lib.FcFontMatch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]

            lib.FcPatternGetString.restype = ctypes.c_int
            lib.FcPatternGetString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]

            lib.FcPatternDestroy.restype = None
            lib.FcPatternDestroy.argtypes = [ctypes.c_void_p]

            config = lib.FcInitLoadConfigAndFonts()
            if not config:
                self._unavailable = True
                return False

            self._lib = lib
            self._config = config
            return True
        except (OSError, AttributeError):
            self._unavailable = True
            return False

    def match(self, family: str, weight: int | None, style: str | None):
        """Returns a dict with "family" and "file" (either may be None if
        fontconfig didn't set that field on the match), or None if
        fontconfig couldn't be reached at all (caller should fall back to
        the fc-match subprocess)."""
        with self._lock:
            if not self._ensure_loaded():
                return None

            lib = self._lib
            pattern = lib.FcPatternCreate()
            if not pattern:
                return None
            try:
                lib.FcPatternAddString(pattern, FC_FAMILY, family.encode("utf-8"))
                if weight is not None:
                    lib.FcPatternAddInteger(pattern, FC_WEIGHT, _ot_weight_to_fc(weight))
                if style is not None:
                    lib.FcPatternAddInteger(pattern, FC_SLANT, _style_to_fc_slant(style))

                lib.FcConfigSubstitute(self._config, pattern, _FC_MATCH_PATTERN)
                lib.FcDefaultSubstitute(pattern)

                result = ctypes.c_int(0)
                matched = lib.FcFontMatch(self._config, pattern, ctypes.byref(result))
                if not matched:
                    return None
                try:
                    return {
                        "family": self._get_string(matched, FC_FAMILY),
                        "file": self._get_string(matched, FC_FILE),
                    }
                finally:
                    lib.FcPatternDestroy(matched)
            finally:
                lib.FcPatternDestroy(pattern)

    def _get_string(self, pattern, obj: bytes) -> str | None:
        value = ctypes.c_char_p()
        res = self._lib.FcPatternGetString(pattern, obj, 0, ctypes.byref(value))
        if res != 0 or value.value is None:  # FcResultMatch == 0
            return None
        return value.value.decode("utf-8", errors="replace")


_fontconfig = _FontConfig()


def _match_via_subprocess(family: str, weight: int | None, style: str | None):
    """fc-match fallback for environments where libfontconfig can't be
    dlopen'd (e.g. a stripped-down flatpak runtime). Same matcher fontconfig
    binaries and the ctypes path both use -- just invoked as a subprocess."""
    parts = [_escape_fc_value(family)]
    if weight is not None:
        parts.append(f"weight={_ot_weight_to_fc(weight)}")
    if style is not None:
        parts.append(f"slant={_style_to_fc_slant(style)}")
    pattern = ":".join(parts)

    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{family}|%{file}", pattern],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0 or not proc.stdout:
        return None

    out = proc.stdout
    if "|" not in out:
        return {"family": None, "file": out or None}
    matched_family, _, matched_file = out.partition("|")
    return {"family": matched_family or None, "file": matched_file or None}


def _resolve_pattern(family: str, weight: int | None, style: str | None):
    result = _fontconfig.match(family, weight, style)
    if result is None:
        result = _match_via_subprocess(family, weight, style)
    return result


@functools.lru_cache(maxsize=256)
def resolve(family: str, weight: int = 400, style: str = "normal") -> str | None:
    """Resolve (family, weight, style) to a concrete font file path via
    fontconfig, mirroring what Pango/GTK's own font picker would land on.

    weight is a numeric Pango/CSS weight (100-900); style is
    "normal"/"italic"/"oblique". Either may be passed as None (e.g. a
    KeyLabel whose defaults haven't been injected yet) and is coalesced to
    the CSS default. Returns None if fontconfig couldn't be reached at all
    (missing library AND missing fc-match binary).
    """
    if weight is None:
        weight = 400
    if style is None:
        style = "normal"
    if not family:
        family = "sans"

    if IS_MAC:
        return _resolve_mac(family, weight, style)

    result = _resolve_pattern(family, weight, style)
    if result is None:
        return None
    return result.get("file")


def _resolve_mac(family: str, weight: int, style: str) -> str | None:
    # macOS keeps matplotlib as its font backend (requirement_macos.txt);
    # imported lazily here so the Linux path never touches it.
    import matplotlib.font_manager
    return matplotlib.font_manager.findfont(
        matplotlib.font_manager.FontProperties(family=family, weight=weight, style=style)
    )


@functools.lru_cache(maxsize=1)
def fallback_font() -> str | None:
    """Resolve fontconfig's generic "sans" alias to a concrete family name
    (e.g. "DejaVu Sans" / "Noto Sans" depending on the system), the same
    role the old `find_fallback_font()` played -- but without the
    import-time full-system-font scan that used to back it. Cached: this is
    still one fontconfig round trip, just deferred to first use."""
    if IS_MAC:
        import matplotlib.font_manager
        for name in ("DejaVu Sans", "Arial"):
            try:
                matplotlib.font_manager.findfont(
                    matplotlib.font_manager.FontProperties(family=name), fallback_to_default=False
                )
                return name
            except Exception:
                continue
        return None

    result = _resolve_pattern("sans", None, None)
    if result is None:
        return "DejaVu Sans"
    return result.get("family") or "DejaVu Sans"


def font_name_from_path(font_path: str) -> str | None:
    """Read the human-readable family name out of a font file's `name`
    table (IDs 1 "Font Family" / 16 "Typographic Family"), the same data
    matplotlib.font_manager.FontProperties(fname=...).get_family() used to
    surface -- via fontTools instead, which is already a hard dependency
    (KeyLabel's symbol-font detection)."""
    try:
        font = TTFont(font_path, fontNumber=0, lazy=True)
    except Exception:
        return None

    try:
        name_table = font["name"]
    except KeyError:
        return None

    best = {}
    for record in name_table.names:
        if record.nameID not in (1, 16):
            continue
        try:
            value = record.toUnicode()
        except Exception:
            continue
        if not value:
            continue
        # Prefer nameID 16 (Typographic Family) over 1 (Font Family), and
        # within a nameID prefer Windows platform records (platformID 3)
        # since that's what matplotlib/fontTools consumers usually expect.
        priority = (record.nameID, 1 if record.platformID == 3 else 0)
        if record.nameID not in best or priority > best[record.nameID][0]:
            best[record.nameID] = (priority, value)

    if 16 in best:
        return best[16][1]
    if 1 in best:
        return best[1][1]
    return None
