"""
Pins MediaConfig.from_dict against the old inline media defaults (gl#174).

The two state-load blocks in DeckController and ActionCore.set_media used to
spell the "media" section defaults inline at every read
(``state_dict.get("media", {}).get("loop", True)`` and friends), so the same
key could drift between call sites. MediaConfig.from_dict is now the single
extraction point; this scenario locks its defaults and key mapping to the
values those inline reads used, so a transcription slip fails here instead
of silently changing which media a page loads.
"""
import fixtures  # noqa: F401  (isolates gl.DATA_PATH before anything reads it)

from src.backend.DeckManagement.Media.MediaConfig import MediaConfig  # noqa: E402

# The default each inline call site used before the conversion, by field.
# Kept as a literal second copy on purpose: a typo in MediaConfig has to
# disagree with something.
EXPECTED_DEFAULTS = {
    "path": None,
    "loop": True,
    "fps": 30,
    "fill_mode": None,
    "size": None,
    "valign": None,
    "halign": None,
}

# Field name -> page-dict key (kebab-case in the JSON).
KEY_MAP = {
    "path": "path",
    "loop": "loop",
    "fps": "fps",
    "fill_mode": "fill-mode",
    "size": "size",
    "valign": "valign",
    "halign": "halign",
}

# A fully-populated media section with non-default values for every key.
SAMPLE = {
    "path": "/somewhere/clip.mp4",
    "loop": False,
    "fps": 12,
    "fill-mode": "cover",
    "size": 0.5,
    "valign": -1.0,
    "halign": 1.0,
}


def check_empty_dict_reads_as_defaults() -> None:
    """An empty media section must produce exactly the old inline defaults."""
    for cfg in (MediaConfig.from_dict({}), MediaConfig()):
        for field, expected in EXPECTED_DEFAULTS.items():
            got = getattr(cfg, field)
            assert got == expected and type(got) is type(expected), (
                f"MediaConfig.{field} on empty media gave {got!r} "
                f"({type(got).__name__}), expected {expected!r} "
                f"({type(expected).__name__})"
            )
    print("PASS: empty media dict reads back as the pinned defaults")


def check_field_set_matches_key_map() -> None:
    """Every page-dict media key maps to exactly one field -- a renamed or
    added field must show up here."""
    fields = set(MediaConfig.__dataclass_fields__)
    assert fields == set(KEY_MAP), (
        f"MediaConfig fields drifted -- only in class: "
        f"{sorted(fields - set(KEY_MAP))}, only expected: "
        f"{sorted(set(KEY_MAP) - fields)}"
    )
    assert set(EXPECTED_DEFAULTS) == set(KEY_MAP)
    print("PASS: field set matches the pinned key map")


def check_populated_dict_round_trips() -> None:
    """A fully-populated media section must land on the matching fields --
    including the kebab-case fill-mode -> fill_mode mapping."""
    cfg = MediaConfig.from_dict(SAMPLE)
    for field, key in KEY_MAP.items():
        got = getattr(cfg, field)
        assert got == SAMPLE[key], (
            f"MediaConfig.{field} gave {got!r}, expected {SAMPLE[key]!r} "
            f"(from key {key!r})"
        )
    print("PASS: populated media dict round-trips through from_dict")


def check_snake_case_fill_mode_is_not_read() -> None:
    """The JSON key is "fill-mode"; a snake_case "fill_mode" entry is not a
    page-dict key and must not leak into the config."""
    cfg = MediaConfig.from_dict({"fill_mode": "cover"})
    assert cfg.fill_mode is None, (
        f"snake_case fill_mode was read: {cfg.fill_mode!r}"
    )
    print("PASS: only the kebab-case fill-mode key is read")


def check_explicit_none_survives() -> None:
    """dict.get only falls back on MISSING keys, so the old inline reads
    returned a stored null as-is -- even for loop/fps, whose defaults are
    non-None. from_dict must not 'repair' it."""
    cfg = MediaConfig.from_dict({"loop": None, "fps": None, "path": None})
    assert cfg.loop is None, f"stored loop=None read back as {cfg.loop!r}"
    assert cfg.fps is None, f"stored fps=None read back as {cfg.fps!r}"
    assert cfg.path is None
    print("PASS: stored nulls survive from_dict unchanged")


if __name__ == "__main__":
    check_empty_dict_reads_as_defaults()
    check_field_set_matches_key_map()
    check_populated_dict_round_trips()
    check_snake_case_fill_mode_is_not_read()
    check_explicit_none_survives()
    print("\nALL PASS: scenario_media_config")
