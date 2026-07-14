"""
Author: Core447
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
import json
import sys
import threading
import gi
import webbrowser as web

import urllib.request
from src.backend.DeckManagement.HelperMethods import open_web

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, Adw, GLib

# Import globals
import appinfo
import globals as gl

# Import python modules
import  os
from loguru import logger as log

# Import own modules
from src.windows.Store.Store import Store
from src.windows.Settings.Settings import Settings

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from windows.mainWindow.mainWindow import MainWindow

class HeaderHamburgerMenuButton(Gtk.MenuButton):
    def __init__(self, main_window: "MainWindow", **kwargs):
        self.main_window: "MainWindow" = main_window
        super().__init__(**kwargs)
        self.set_icon_name("open-menu-symbolic")
        
        self.init_actions()
        self.build()

    def init_actions(self):
        # Open store
        self.open_store_action = Gio.SimpleAction.new("open-store", None)
        self.open_store_action.connect("activate", self.on_open_store)
        self.main_window.add_action(self.open_store_action)
        # Open settings
        self.open_settings_action = Gio.SimpleAction.new("open-settings", None)
        self.open_settings_action.connect("activate", self.on_open_settings)
        self.main_window.add_action(self.open_settings_action)
        # Support app
        self.support_action = Gio.SimpleAction.new("support", None)
        self.support_action.connect("activate", self.on_support)
        self.main_window.add_action(self.support_action)
        # Quit App
        self.quit_action = Gio.SimpleAction.new("quit", None)
        self.quit_action.connect("activate", self.on_quit)
        self.main_window.add_action(self.quit_action)
        # Open About
        self.open_about_action = Gio.SimpleAction.new("open-about", None)
        self.open_about_action.connect("activate", self.on_open_about)
        self.main_window.add_action(self.open_about_action)

    def build(self):
        self.menu = Gio.Menu.new()
        self.menu.append(gl.lm.get("open-store"), "win.open-store")
        self.menu.append(gl.lm.get("open-settings"), "win.open-settings")
        self.menu.append(gl.lm.get("quit"), "win.quit")
        self.menu.append(gl.lm.get("open-about"), "win.open-about")
        self.menu.append(gl.lm.get("support"), "win.support")

        # Popover
        self.popover = Gtk.PopoverMenu()
        self.popover.set_menu_model(self.menu)
        self.set_popover(self.popover)

    def on_open_store(self, action, parameter):
        gl.app.open_store()

    def on_open_settings(self, action, parameter):
        self.settings = Settings()
        self.settings.present()
        self.settings = None
        
    def on_quit(self, action, parameter):
        GLib.idle_add(gl.app.on_quit)

    def on_support(self, action, parameter):
        open_web("https://ko-fi.com/core447")

    def get_contributer_list(self):
        try:
            contents = urllib.request.urlopen("https://api.github.com/repos/StreamController/StreamController/contributors", timeout=10).read().decode()
            data = json.loads(contents)

            contributors = []
            for contributor in data:
                if contributor["login"] in ["dependabot[bot]"]:
                    continue
                contributors.append(f"{contributor["login"]} {contributor["html_url"]}")

            return contributors
        except Exception:
            return []

    def _fetch_contributors_async(self, about: Adw.AboutDialog) -> None:
        """Runs on a worker thread: fetches the contributor list and, when it
        arrives, adds the credit section on the main loop. On network failure
        (offline, rate-limited, black-holed connection) the dialog simply
        stays up without a contributors section."""
        contributors = self.get_contributer_list()
        if not contributors:
            return

        def add_section():
            about.add_credit_section(
                f"Contributors ({len(contributors)})",
                sorted(set(contributors), key=str.casefold),
            )
            return False

        GLib.idle_add(add_section)

    def on_open_about(self, action, parameter):
        self.about = Adw.AboutDialog()
        self.about.set_application_name("Deckard")

        # Show the Deckard fork release version (gl.deckard_version), NOT
        # gl.app_version -- the latter stays upstream-aligned for plugin compat.
        version = gl.deckard_version
        if gl.argparser.parse_args().devel:
            version += " devel"
        self.about.set_version(version)
        self.about.set_developers(["nazbert", "Core447 (original StreamController author)"])
        self.about.set_developer_name("nazbert")
        self.about.set_license_type(Gtk.License.GPL_3_0)
        self.about.set_comments(f"Control your Stream Decks. A fork of StreamController {gl.app_version} by Core447.")
        self.about.set_website("https://github.com/nazbert/Deckard")
        self.about.set_issue_url("https://github.com/nazbert/Deckard/issues")
        # self.about.set_support_url("https://discord.com/invite/MSyHM8TN3u")

        self.about.set_copyright("Copyright (C) 2024 Core447, 2026 nazbert")
        self.about.set_application_icon(appinfo.APP_ID)
        self.about.set_visible(True)

        self.about.add_legal_section(
            "Stream Deck",
            "Stream Deck is a trademark owned by Elgato/Corsair Memory, Inc.",
            Gtk.License.CUSTOM,
            license=None,
        )

        self.about.add_legal_section(
            "Icons",
            "Deckard uses and ships Adwaita icons",
            license_type=Gtk.License.CUSTOM,
            license=None
        )

        supporter_names = [
            "ADIOP", "Alex", "Alex H.", "AndrewLawrence", "Andy Hopper", "Aniel",
            "Based Supporter", "Ben", "Bill W.", "BKM", "Bottswana", "Brodie Robertson",
            "bäcky", "Calm Wiz", "Chris McGee", "ChristianSchulz", "Chronoes", "CodeFaux",
            "Daisuke88", "Deadlinux", "Dennis", "doc.brown", "dpronk1959", "Eric", "fabi",
            "FearsNONE", "fernandomema", "frank", "Fredrik", "Frieder", "gaensepuemchen",
            "GAPLS", "Graysorrel", "GregoryHelding", "Grimmnebulin", "HannesMC",
            "HorstRohrweck", "Jazzy2040", "Jesse", "JesúsRíoBarrigón", "Joe W", "JofSpad3s",
            "John Fegan", "Jonathan Cremin", "Jonathan Hirschman", "Jonathan Leaders",
            "joshferrara", "JulianARCVT", "Julien", "Jun Iyama", "Just_Don", "Karit", "Kaue",
            "Khalid Al-Baloushi", "kirchou", "lilacjasminetea", "linuxnext", "Logan",
            "loganomar", "Lord_Darkaham", "Lorith", "LukasDominikusAlbert", "lutin mystere",
            "Marcus", "Martin", "Marv", "Matt", "Matthias", "Matthias Reißner", "Matz",
            "MelanX", "mitteltritt", "Myrik Lord", "nasi", "Nathan Pyle", "Nicholas", "nvll",
            "Opnyouri", "Pedro", "Phanatik", "Pommes", "potential_ad4169", "PSF", "pturn3",
            "quintuple-lained", "RandomLegend", "Ricardo", "Ritchie", "Rix", "schlaggi",
            "Siphoned Anomaly", "Skelmy", "steveo", "StuckSouls", "ted", "timo", "Tobbe",
            "Tomasu", "tonygair", "Ty Smith", "veya", "Violet", "Wilbert", "Will Rivera",
            "William Pietri", "YvesCB", "zoolie"
        ]

        self.about.add_acknowledgement_section(
            "Ko-fi Supporters of the original StreamController",
            # ["bäcky https://ko-fi.com/core447"]
            [f"{name} https://ko-fi.com/core447" for name in sorted(set(supporter_names))],
        )

        with gl.logs_lock:
            logs_snapshot = list(gl.logs)
        self.about.set_debug_info("".join(logs_snapshot))
        self.about.set_debug_info_filename(os.path.join(gl.DATA_PATH, "logs", "logs.log"))

        self.about.set_release_notes(gl.release_notes)  
        self.about.set_release_notes_version(gl.app_version)
        
        self.about.present(gl.app.get_active_window())

        # The contributor list comes from the GitHub API -- fetching it here
        # (synchronously) froze the whole UI for a full network round trip and
        # hung indefinitely on a black-holed connection. Present the dialog
        # immediately and fill the section in when (and if) the data arrives.
        threading.Thread(
            target=self._fetch_contributors_async,
            args=(self.about,),
            daemon=True,
            name="about-contributors",
        ).start()

    def set_optional_actions_state(self, state: bool) -> None:
        self.open_store_action.set_enabled(state)
        self.open_settings_action.set_enabled(state)