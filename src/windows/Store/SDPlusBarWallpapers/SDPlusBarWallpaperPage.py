"""
Year: 2024

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

from src.windows.Store.StoreData import SDPlusBarWallpaperData

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

# Import python modules
from loguru import logger as log

# Import own modules
from src.windows.Store.StorePage import StorePage
from src.windows.Store.Preview import StorePreview
from src.backend.Store.store_result import Err

# Import globals
import globals as gl

# Typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.Store.Store import Store

class SDPlusBarWallpaperPage(StorePage):
    def __init__(self, store: "Store"):
        super().__init__(store=store)
        self.store = store
        self.compatible_section.search_entry.set_placeholder_text(gl.lm.get("store.wallpapers.search-placeholder"))

    # Carry no @log.catch here. StorePage._load_guarded must see the failure,
    # so it can show the error page and arm the tab for a retry.
    def load(self):
        self.set_loading()
        result = self.store.backend.get_all_sd_plus_bar_wallpapers()
        if isinstance(result, Err):
            self.show_connection_error()
            return
        wallpapers = result.value
        for wallpaper in wallpapers:
            if wallpaper.is_compatible:
                section = self.compatible_section
            else:
                section = self.incompatible_section
            self.append_preview_on_main(section, lambda wallpaper=wallpaper: SDPlusBarWallpaperPreview(wallpaper_page=self, wallpaper_data=wallpaper))

        self.set_loaded()


class SDPlusBarWallpaperPreview(StorePreview):
    def __init__(self, wallpaper_page:SDPlusBarWallpaperPage, wallpaper_data:SDPlusBarWallpaperData):
        super().__init__(store_page=wallpaper_page)
        self.wallpaper_data = wallpaper_data
        self.wallpaper_page = wallpaper_page

        self.set_author_label(wallpaper_data.author)
        self.set_name_label(wallpaper_data.name)
        self.set_image(wallpaper_data.image)
        self.set_url(wallpaper_data.github)

        self.set_official(wallpaper_data.official)
        self.set_verified(wallpaper_data.verified)

        self.warning_badge.set_tooltip("store.badges.sd_plus_bar_wallpaper.warning")
        self.official_badge.set_tooltip("store.badges.sd_plus_bar_wallpaper.official")
        self.verified_badge.set_tooltip("store.badges.sd_plus_bar_wallpaper.verified")

        if not self.check_required_version(wallpaper_data.minimum_app_version):
            self.main_button_box.add_css_class("red-border")
        else:
            self.main_button_box.remove_css_class("red-border")

        if wallpaper_data.local_sha is None:
            self.set_install_state(0)
        elif wallpaper_data.local_sha == wallpaper_data.commit_sha:
            self.set_install_state(1)
        else:
            self.set_install_state(2)

        description = self.wallpaper_data.short_description
        if description in ["", "N/A", None]:
            description = self.wallpaper_data.description
        self.set_description(description)

    def install(self) -> bool:
        """Run on the download worker thread. Returns True on a real install.

        A failed install returns an Err, and the button keeps its previous
        state instead of moving to installed. A 400, a 404 or an offline
        download must not read as installed.
        """
        backend = self.store.backend
        if backend is None:
            log.error(f"Store backend unavailable; cannot install {self.wallpaper_data.id}")
            self.notify_install_failure()
            return False
        result = backend.install_sd_plus_bar_wallpaper(sd_plus_bar_wallpaper_data=self.wallpaper_data)
        if isinstance(result, Err):
            log.error(f"Failed to install SD+ bar wallpaper {self.wallpaper_data.id}: {result!r}")
            self.notify_install_failure()
            return False
        GLib.idle_add(self.set_install_state, 1)
        return True

    def notify_install_failure(self):
        name = self.wallpaper_data.name or self.wallpaper_data.id
        gl.notify.error(f"The SD+ bar wallpaper {name} could not be installed",
                        title="SD+ bar wallpaper install failed")

    def uninstall(self):
        self.store.backend.uninstall_sd_plus_bar_wallpaper(sd_plus_bar_wallpaper_data=self.wallpaper_data)
        self.set_install_state(0)

    def update(self):
        self.install()

    def on_click_main(self, button: Gtk.Button):
        self.wallpaper_page.set_info_visible(True)

        # Update info page
        self.wallpaper_page.info_page.set_name(self.wallpaper_data.name)
        self.wallpaper_page.info_page.set_description(self.wallpaper_data.description)
        self.wallpaper_page.info_page.set_author(self.wallpaper_data.author)
        self.wallpaper_page.info_page.set_version(self.wallpaper_data.version)

        self.wallpaper_page.info_page.set_license(self.wallpaper_data.license)
        self.wallpaper_page.info_page.set_copyright(self.wallpaper_data.copyright)
        self.wallpaper_page.info_page.set_original_url(self.wallpaper_data.original_url)
        self.wallpaper_page.info_page.set_license_description(self.wallpaper_data.license_descriptions)

