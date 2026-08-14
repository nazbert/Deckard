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

from src.windows.Store.StoreData import PluginData

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


class PluginPage(StorePage):
    def __init__(self, store: "Store"):
        super().__init__(store=store)
        self.store = store
        self.compatible_section.search_entry.set_placeholder_text(gl.lm.get("store.plugins.search-placeholder"))
        self.incompatible_section.search_entry.set_placeholder_text(gl.lm.get("store.plugins.search-placeholder"))

    # Carry no @log.catch here. StorePage._load_guarded must see the failure,
    # so it can show the error page and arm the tab for a retry.
    def load(self):
        self.set_loading()
        result = self.store.backend.get_all_plugins()
        if isinstance(result, Err):
            self.show_connection_error()
            return
        plugins: list[PluginData] = result.value
        for plugin in plugins:
            if not plugin:
                continue
            if plugin.is_compatible:
                section = self.compatible_section
            else:
                section = self.incompatible_section
            self.append_preview_on_main(section, lambda plugin=plugin: PluginPreview(plugin_page=self, plugin_data=plugin))

        self.set_loaded()


class PluginPreview(StorePreview):
    def __init__(self, plugin_page: PluginPage, plugin_data: PluginData):
        super().__init__(store_page=plugin_page)
        self.plugin_page = plugin_page
        self.plugin_data = plugin_data

        self.set_author_label(plugin_data.author)
        self.set_name_label(plugin_data.plugin_name)
        self.set_image(plugin_data.image)
        self.set_url(plugin_data.github)

        self.set_official(plugin_data.official)
        self.set_verified(plugin_data.verified)

        self.warning_badge.set_tooltip("store.badges.plugin.warning")
        self.official_badge.set_tooltip("store.badges.plugin.official")
        self.verified_badge.set_tooltip("store.badges.plugin.verified")

        # Set install button state
        self.set_install_state(self.get_install_state_for(plugin_data))

        description = self.plugin_data.short_description
        if description in ["", "N/A", None]:
            description = self.plugin_data.description
        self.set_description(description)

    @staticmethod
    def get_install_state_for(plugin_data: PluginData) -> int:
        """0 is not installed, 1 is installed, and 2 is update available.

        An installed plugin whose pinned store version is incompatible reads
        as state 1. is_compatible is then False, because prepare_plugin pins
        the newest commit of another app major when no compatible one exists.
        That update would replace a working plugin with an incompatible build,
        which get_plugins_to_update also refuses on the auto-update path.
        """
        if plugin_data.local_sha is None:
            return 0
        if plugin_data.local_sha == plugin_data.commit_sha:
            return 1
        if plugin_data.commit_sha is None:
            # The remote tip is unresolved, because get_last_commit returned
            # None for a branch-pinned plugin, after a 429 or an empty answer.
            # A None commit_sha differs from local_sha, which would show an
            # update-available badge whose install can only return 404. No
            # known target exists to update to.
            return 1
        if plugin_data.is_compatible is False:
            return 1
        return 2

    def install(self) -> bool:
        """Run on the download worker thread. Returns True on a real install.

        Any result other than an Err counts as success. A failed install must
        not move the button to installed.
        """
        backend = self.store.backend
        if backend is None:
            log.error("Store backend unavailable; cannot install "
                      f"{self.plugin_data.plugin_id}")
            self.notify_install_failure()
            return False
        result = backend.install_plugin(plugin_data=self.plugin_data)
        if isinstance(result, Err):
            log.error(f"Failed to install plugin {self.plugin_data.plugin_id}: {result!r}")
            self.notify_install_failure()
            # Leave the button in its previous state so the user can retry.
            return False
        GLib.idle_add(self.set_install_state, 1)
        return True

    def uninstall(self):
        self.store.backend.uninstall_plugin(plugin_id=self.plugin_data.plugin_id)
        GLib.idle_add(self.set_install_state, 0)

    def update(self):
        # install_plugin deregisters the old version only after the download
        # succeeds, so a failed update leaves the old version installed and
        # registered. No recovery reload runs here.
        self.install()

    def notify_install_failure(self):
        name = self.plugin_data.plugin_name or self.plugin_data.plugin_id
        gl.notify.error(f"The plugin {name} could not be installed",
                        title="Plugin install failed")

    def on_click_main(self, button: Gtk.Button):
        self.plugin_page.set_info_visible(True)

        # Update info page
        self.plugin_page.info_page.set_name(self.plugin_data.plugin_name)
        self.plugin_page.info_page.set_description(self.plugin_data.description)
        self.plugin_page.info_page.set_author(self.plugin_data.author)
        self.plugin_page.info_page.set_version(self.plugin_data.plugin_version)

        self.plugin_page.info_page.set_license(self.plugin_data.license)
        self.plugin_page.info_page.set_copyright(self.plugin_data.copyright)
        self.plugin_page.info_page.set_original_url(self.plugin_data.original_url)
        # An absent block is None, and get_custom_translation answers "" for
        # None (but None for {}), so keep that branch explicit.
        license_descriptions = self.plugin_data.license_descriptions
        self.plugin_page.info_page.set_license_description(
            gl.lm.get_custom_translation(license_descriptions) if license_descriptions is not None else "")

    # check_required_version comes from StorePreview, which calls the one
    # implementation in StoreData.is_min_app_version_satisfied.
