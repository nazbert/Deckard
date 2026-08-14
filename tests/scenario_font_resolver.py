"""font_resolver.py replaces matplotlib.font_manager as the font backend.

resolve() must map the app weight scale onto the fontconfig one, the resolved
file must open in PIL, and fallback_font() must stay lazy at import.
"""
import os
import subprocess
import sys

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend.DeckManagement import font_resolver

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# A family verified with fc-list to be present on the harness machine with both
# a regular and a bold face, so the weight check has something real to bite on.
_TEST_FAMILY = "DejaVu Sans"


def check_weight_selects_different_files() -> None:
    regular = font_resolver.resolve(_TEST_FAMILY, 400, "normal")
    bold = font_resolver.resolve(_TEST_FAMILY, 700, "normal")

    assert regular is not None, f"resolve() found no regular file for {_TEST_FAMILY!r}"
    assert bold is not None, f"resolve() found no bold file for {_TEST_FAMILY!r}"
    assert regular != bold, (
        f"weight=400 and weight=700 resolved to the SAME file ({regular!r}) -- "
        "this is the exact regression the OT->fc weight table exists to prevent "
        "(an untranslated raw weight makes every 'normal' label render bold)."
    )
    assert "bold" not in os.path.basename(regular).lower(), (
        f"weight=400 (normal) resolved to a file that looks bold: {regular!r}"
    )
    print(f"PASS: weight 400 -> {regular}, weight 700 -> {bold} (distinct files)")


def check_resolved_file_openable() -> None:
    from PIL import ImageFont

    path = font_resolver.resolve(_TEST_FAMILY, 400, "normal")
    assert path is not None
    assert os.path.isfile(path), f"resolved path does not exist on disk: {path!r}"

    font = ImageFont.truetype(path, 16, encoding="unic")
    assert font is not None
    print(f"PASS: PIL.ImageFont.truetype opened resolved file {path}")


def check_font_name_round_trips() -> None:
    path = font_resolver.resolve(_TEST_FAMILY, 400, "normal")
    assert path is not None

    name = font_resolver.font_name_from_path(path)
    assert name, f"font_name_from_path returned empty/None for {path!r}"
    assert isinstance(name, str)
    print(f"PASS: font_name_from_path({path!r}) -> {name!r}")


def check_fallback_font_lazy_at_import() -> None:
    # Fresh interpreter. Import globals, check the lazy attribute is not
    # materialized, then access it and check that it is.
    script = (
        "import sys, tempfile\n"
        f"sys.path.insert(0, {_REPO_ROOT!r})\n"
        "sys.argv = [sys.argv[0], '--data', tempfile.mkdtemp(), '--devel', "
        "'--skip-load-hardware-decks']\n"
        "import globals as gl\n"
        "assert 'fallback_font' not in vars(gl), (\n"
        "    'fallback_font must not be materialized by `import globals` alone -- '\n"
        "    'found in vars(gl) before first access'\n"
        ")\n"
        "value = gl.fallback_font\n"
        "assert value, 'gl.fallback_font resolved to empty/None on first access'\n"
        "assert 'fallback_font' in vars(gl), (\n"
        "    'first access must cache the resolved value as a plain module attribute'\n"
        ")\n"
        "print('OK', value)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.startswith("OK"), result.stdout
    print(f"PASS: gl.fallback_font is lazy ({result.stdout.strip()})")


def check_no_matplotlib_imported() -> None:
    # font_resolver itself, and everything it pulls in on the Linux path, must
    # never import matplotlib.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {_REPO_ROOT!r})\n"
        "from src.backend.DeckManagement import font_resolver\n"
        "font_resolver.resolve('DejaVu Sans', 400, 'normal')\n"
        "font_resolver.fallback_font()\n"
        "assert 'matplotlib' not in sys.modules, ("
        "    'font_resolver pulled matplotlib into sys.modules on Linux'"
        ")\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout
    print("PASS: font_resolver never imports matplotlib on Linux")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_font_resolver")
    check_weight_selects_different_files()
    check_resolved_file_openable()
    check_font_name_round_trips()
    check_fallback_font_lazy_at_import()
    check_no_matplotlib_imported()
    print("PASS: scenario_font_resolver")


if __name__ == "__main__":
    main()
