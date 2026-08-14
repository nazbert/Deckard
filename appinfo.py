"""The identity strings of the application, defined once.

This module uses the standard library only and has no import-time side
effects, so any module can import it, including rebrand_migration.py and the
test harness. Do not import globals, or any src module, here. Either one
breaks the pre-globals import contract of the rebrand migration.
"""

# Every derived spelling below comes from APP_ID, which covers the D-Bus
# object path, the ayatana underscore form and the dotted suffixes, so an id
# change is one edit.
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
