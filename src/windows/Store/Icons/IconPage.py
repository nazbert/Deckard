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

from src.windows.Store.StoreData import IconData

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

# Import python modules
from loguru import logger as log

# Import own modules
from src.windows.Store.StorePage import StorePage
from src.windows.Store.Preview import StorePreview
from src.backend.Store.store_result import Err

# Typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.Store.Store import Store

# Import globals
import globals as gl


class IconPage(StorePage):
    def __init__(self, store: "Store"):
        super().__init__(store=store)
        self.store = store
        self.compatible_section.search_entry.set_placeholder_text(gl.lm.get("store.icons.search-placeholder"))
        self.incompatible_section.search_entry.set_placeholder_text(gl.lm.get("store.icons.search-placeholder"))

    # Carry no @log.catch here. StorePage._load_guarded must see the failure,
    # so it can show the error page and arm the tab for a retry.
    def load(self):
        self.set_loading()
        result = self.store.backend.get_all_icons()
        if isinstance(result, Err):
            self.show_connection_error()
            return
        icons: list[IconData] = result.value
        for icon in icons:
            if icon.is_compatible:
                section = self.compatible_section
            else:
                section = self.incompatible_section
            self.append_preview_on_main(section, lambda icon=icon: IconPreview(icon_page=self, icon_data=icon))

        self.set_loaded()


class IconPreview(StorePreview):
    def __init__(self, icon_page:IconPage, icon_data:IconData):
        super().__init__(store_page=icon_page)
        self.icon_data = icon_data
        self.icon_page = icon_page

        self.set_author_label(icon_data.author)
        self.set_name_label(icon_data.icon_name)
        self.set_image(icon_data.image)
        self.set_url(icon_data.github)

        self.set_official(icon_data.official)
        self.set_verified(icon_data.verified)

        self.warning_badge.set_tooltip("store.badges.icon.warning")
        self.official_badge.set_tooltip("store.badges.icon.official")
        self.verified_badge.set_tooltip("store.badges.icon.verified")

        if not self.check_required_version(icon_data.minimum_app_version):
            self.main_button_box.add_css_class("red-border")
        else:
            self.main_button_box.remove_css_class("red-border")

        if icon_data.local_sha is None:
            self.set_install_state(0)
        elif icon_data.local_sha == icon_data.commit_sha:
            self.set_install_state(1)
        else:
            self.set_install_state(2)

        # An absent block is None, and get_custom_translation answers "" for
        # None (but None for {}), so keep that branch explicit.
        short_descriptions = self.icon_data.short_descriptions
        descriptions = self.icon_data.descriptions
        description = gl.lm.get_custom_translation(short_descriptions) if short_descriptions is not None else ""
        if description in ["", "N/A", None]:
            description = gl.lm.get_custom_translation(descriptions) if descriptions is not None else ""

        description = self.icon_data.short_description
        if description in ["", "N/A", None]:
            description = self.icon_data.description
        self.set_description(description)

    def install(self) -> bool:
        """Run on the download worker thread. Returns True on a real install.

        A failed install returns an Err, and the button keeps its previous
        state instead of moving to installed. A 400, a 404 or an offline
        download must not read as installed.
        """
        backend = self.store.backend
        if backend is None:
            log.error(f"Store backend unavailable; cannot install {self.icon_data.icon_id}")
            self.notify_install_failure()
            return False
        result = backend.install_icon(icon_data=self.icon_data)
        if isinstance(result, Err):
            log.error(f"Failed to install icon pack {self.icon_data.icon_id}: {result!r}")
            self.notify_install_failure()
            return False
        GLib.idle_add(self.set_install_state, 1)
        return True

    def notify_install_failure(self):
        name = self.icon_data.icon_name or self.icon_data.icon_id
        gl.notify.error(f"The icon pack {name} could not be installed",
                        title="Icon pack install failed")

    def uninstall(self):
        self.store.backend.uninstall_icon(icon_data=self.icon_data)
        self.set_install_state(0)

    def update(self):
        self.install()

    def on_click_main(self, button: Gtk.Button):
        self.icon_page.set_info_visible(True)

        # Update info page
        self.icon_page.info_page.set_name(self.icon_data.icon_name)
        self.icon_page.info_page.set_description(self.icon_data.description)
        self.icon_page.info_page.set_author(self.icon_data.author)
        self.icon_page.info_page.set_version(self.icon_data.icon_version)

        self.icon_page.info_page.set_license(self.icon_data.license)
        self.icon_page.info_page.set_copyright(self.icon_data.copyright)
        self.icon_page.info_page.set_original_url(self.icon_data.original_url)
        self.icon_page.info_page.set_license_description(self.icon_data.license_descriptions)