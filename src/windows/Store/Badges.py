"""
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import gtk modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

# Import globals
import globals as gl

class Badge(Gtk.Button):
    def __init__(self, label: str, tooltip: str = None, *args, **kwargs):
        super().__init__(  # type: ignore[misc]  # gi stub: the stub models GObject properties as positional-or-keyword params, so *args reads as a second binding for them; at runtime GObject.__init__ takes properties by keyword only
            label=gl.lm.get(label),
            *args, **kwargs
        )
        self.set_tooltip(tooltip)

    def set_tooltip(self, tooltip: str | None):
        if tooltip:
            self.set_has_tooltip(True)
        else:
            self.set_has_tooltip(False)
        # No key gives no tooltip text, which is what gl.lm.get(None) returns.
        self.set_tooltip_text(gl.lm.get(tooltip) if tooltip else None)