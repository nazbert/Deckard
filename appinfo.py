"""Single source of truth for the application's identity strings.

Stdlib-only and side-effect-free, so it can be imported from anywhere --
including ``rebrand_migration.py`` before ``import globals`` and the test
harness. Do NOT import ``globals`` or any ``src.*`` module here, or the
pre-``globals`` import contract of the rebrand migration breaks.

Every derived spelling (D-Bus object path, ayatana underscore form, dotted
suffixes) is built from ``APP_ID`` here so a future id change is a one-line
edit instead of a multi-file, multi-spelling grep.
"""

APP_ID = "io.github.nazbert.Deckard"
APP_NAME = "Deckard"

# /io/github/nazbert/Deckard
DBUS_OBJECT_PATH = "/" + APP_ID.replace(".", "/")
# io_github_nazbert_Deckard  (ayatana NotificationItem path component)
DBUS_UNDERSCORE = APP_ID.replace(".", "_")

# Pre-rename identity, retained for the one-time data migration and the
# transition guard that shoos a still-running pre-rename instance off the
# Stream Deck. Do not reuse these for anything else.
OLD_APP_ID = "com.core447.StreamController"
OLD_DBUS_OBJECT_PATH = "/" + OLD_APP_ID.replace(".", "/")
