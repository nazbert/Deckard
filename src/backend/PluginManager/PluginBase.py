import importlib
import os
import inspect
import json
import threading
import time
import subprocess
from collections.abc import Callable
from typing import Any

from packaging import version

from loguru import logger as log

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.protocol import Connection
from rpyc.core import netref

import gi

from locales.LocaleManager import LocaleManager
from src.backend.PluginManager.ActionHolderGroup import ActionHolderGroup
from src.backend.PluginManager.PluginSettings.Asset import Icon, Color
from src.backend.PluginManager.PluginSettings.PluginAssetManager import AssetManager

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk

import globals as gl

from locales.LegacyLocaleManager import LegacyLocaleManager
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.EventHolder import EventHolder
from src.backend.settings_store import PluginSettings


class PluginBase(rpyc.Service):
    """The base class of every plugin."""

    # {plugin_id: {"object": PluginBase, "meta": ...}}. See register().
    plugins: dict[str, dict[str, Any]] = {}
    disabled_plugins: dict[str, dict[str, Any]] = {}

    def __init__(self, use_legacy_locale: bool = True, legacy_dir: str = "locales"):
        self.backend_connection: Connection = None
        self.backend: netref = None
        self.server: ThreadedServer = None
        self.backend_process: subprocess.Popen | None = None
        # Bookkeeping for the registration watchdog in
        # _watch_backend_registration. The generation counter disarms a stale
        # watchdog after a fast relaunch, which would otherwise attribute the
        # registration of the new backend, or the exit of the old process, to
        # the launch it was armed for. The stop flag suppresses the error for
        # an exit before the registration when a caller asked for that exit,
        # through a plugin deactivation or an unload with on_disconnect.
        self._backend_launch_gen: int = 0
        self._backend_stop_requested: bool = False
        # register_backend sets this on an rpyc service thread, which the
        # backend process drives, and it wakes wait_for_backend on the
        # launching thread.
        self._backend_ready = threading.Event()

        self.logger = gl.loggers.get("plugins", None)

        self.PATH = os.path.dirname(inspect.getfile(self.__class__))
        self.settings_path: str = self._resolve_settings_path()
        # Serializes get_settings and set_settings. The actions of one plugin
        # run on_ready in parallel on the page-load pool, so a concurrent
        # read-modify-write cycle loses an update, and the atomic write only
        # stops a torn file. A plain Lock suffices, because each accessor takes
        # it once, re-acquires nothing and calls no other accessor under it.
        # Underneath run filesystem I/O and the leaf cache lock of the settings
        # store, and no callback re-enters locked code. That is the one lock
        # order of this file. This lock goes outside and the store's inside,
        # never the reverse.
        self._settings_lock = threading.Lock()

        self.locale_manager: LegacyLocaleManager | LocaleManager
        if use_legacy_locale:
            self.locale_manager = LegacyLocaleManager(os.path.join(self.PATH, legacy_dir))
        else:
            self.locale_manager = LocaleManager(os.path.join(self.PATH, "locales.csv"))
        self.locale_manager.set_to_os_default()

        self.action_holders: dict = {}

        self.action_holder_groups: set[ActionHolderGroup] = set()

        self.event_holders: dict = {}

        self.registered: bool = False

        self.plugin_name: str | None = None

        self.asset_manager: AssetManager = AssetManager(self)
        self.asset_manager.load_assets()

        self.has_plugin_settings: bool = False
        self.first_setup: bool = True

        self.registered_pages: list[str] = []

    def get_plugin_id(self) -> str:
        """Read the plugin id from the manifest.

        Without an id in the manifest it uses the folder name.

        Returns:
            str: The plugin ID.
        """
        # Memoized per instance, so the instance frees the cache.
        cached = getattr(self, "_plugin_id_cache", None)
        if cached is not None:
            return cached
        manifest = self.get_manifest()
        self._plugin_id_cache = manifest.get("id") or self.get_plugin_id_from_folder_name()
        return self._plugin_id_cache

    def _resolve_settings_path(self) -> str:
        """Give the settings path of this plugin, under the manifest id.

        The manifest id is the identity that registration and the store use.
        A plugin whose folder name differs from its id, or changes between a
        store install and a git clone, loses its settings on every reinstall
        under a folder-name path. Settings that an earlier version wrote under
        the folder-name path migrate once, at the first construction with a
        folder name that differs from the id.

        The decision reads the settings file at the id path, and not the id
        directory alone. An id directory without a settings.json, left by an
        aborted first setup, a half-finished migration or another tool, must
        not win. A win there orphans the real folder-name settings and starts
        the plugin empty.
        """
        plugins_root = os.path.join(gl.DATA_PATH, "settings", "plugins")
        folder_name = self.get_plugin_id_from_folder_name()
        plugin_id = self.get_plugin_id()
        id_dir = os.path.join(plugins_root, plugin_id)
        id_settings = os.path.join(id_dir, "settings.json")

        if plugin_id != folder_name:
            folder_dir = os.path.join(plugins_root, folder_name)
            folder_settings = os.path.join(folder_dir, "settings.json")

            if os.path.isfile(id_settings):
                # The id path holds settings already, so it wins. A legacy
                # folder-name path with settings stays untouched, because the
                # user's other copy must survive, and this warns instead.
                if os.path.isfile(folder_settings):
                    log.warning(
                        f"Plugin {plugin_id}: settings exist under both {id_dir} "
                        f"(used) and {folder_dir} (ignored, left in place)"
                    )
            elif os.path.isfile(folder_settings):
                # This makes the quarantine and legacy interplay visible. A
                # quarantine removes the id-path settings file, which brings
                # this branch in and migrates the old folder-name settings.
                # A corruption then restores the pre-rename configuration
                # instead of an empty start. That outcome is an open design
                # question, and this warning keeps it from happening
                # unseen.
                try:
                    quarantined = sorted(
                        e for e in os.listdir(id_dir)
                        if e.startswith("settings.json.corrupt")
                    )
                except OSError:
                    quarantined = []
                if quarantined:
                    log.warning(
                        f"Plugin {plugin_id}: the id-path settings in {id_dir} were "
                        f"quarantined ({', '.join(quarantined)}) and the legacy "
                        f"folder-name settings in {folder_dir} are being migrated in "
                        f"-- the plugin will come back with those OLDER settings, not "
                        f"the quarantined ones"
                    )
                # The legacy folder-name path holds the only settings, so
                # migrate them to the id path and keep the plugin's data.
                try:
                    if not os.path.exists(id_dir):
                        # The fast path moves the whole directory, which keeps
                        # the sibling files beside settings.json.
                        os.makedirs(plugins_root, exist_ok=True)
                        os.rename(folder_dir, id_dir)
                    else:
                        # The id directory exists without a settings.json.
                        # Move the file and its siblings into it, then drop the
                        # empty legacy directory.
                        os.makedirs(id_dir, exist_ok=True)
                        for entry in os.listdir(folder_dir):
                            dest = os.path.join(id_dir, entry)
                            if not os.path.exists(dest):
                                os.rename(os.path.join(folder_dir, entry), dest)
                        try:
                            os.rmdir(folder_dir)
                        except OSError:
                            # The directory is not empty, because a name
                            # collided and stayed in the source, or it resists
                            # removal. Both are harmless.
                            pass
                    log.info(
                        f"Plugin {plugin_id}: migrated settings from folder-name "
                        f"path {folder_dir} to id path {id_dir}"
                    )
                except OSError as e:
                    # Keep reading the settings where they are, instead of an
                    # empty start.
                    log.opt(exception=e).error(
                        f"Plugin {plugin_id}: could not migrate settings dir "
                        f"{folder_dir} -> {id_dir}; keeping the folder-name path"
                    )
                    return folder_settings

        return id_settings

    def register(self, plugin_name: str = None, github_repo: str = None, plugin_version: str = None,
                 app_version: str = None):
        """Register a plugin with the given information.

        Args:
            plugin_name (str, optional): The name of the plugin. Defaults to None.
            github_repo (str, optional): The GitHub repository of the plugin. Defaults to None.
            plugin_version (str, optional): The version of the plugin. Defaults to None.
            app_version (str, optional): The version of Deckard. Defaults to None.

        Raises:
            ValueError: If the plugin name is not specified or if the plugin already exists.

        Returns:
            None
        """

        manifest = self.get_manifest()
        self.plugin_name = plugin_name or manifest.get("name") or None
        self.github_repo = github_repo or manifest.get("github") or None
        self.plugin_version = plugin_version or manifest.get("version") or None
        self.min_app_version = manifest.get("minimum-app-version")
        self.app_version = app_version or manifest.get("app-version")
        self.plugin_id = self.get_plugin_id()

        if self.plugin_name in ["", None]:
            log.error("Plugin: Please specify a plugin name")
            return
        if self.plugin_id in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a plugin id")
            return
        if self.github_repo in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a github repo")
            return
        if self.plugin_version in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a plugin version")
            return
        if self.app_version in ["", None]:
            log.error(f"Plugin: {self.plugin_name}: Please specify a app version")
            return


        for plugin_id in PluginBase.plugins.keys():
            plugin = PluginBase.plugins[plugin_id]["object"]
            if plugin.plugin_name == self.plugin_name:
                log.error(f"Plugin: {self.plugin_name}: Plugin already exists")
                return
            
        # A version check can raise over an unparseable version string or a
        # missing minimum-app-version. It must not unwind the plugin's
        # __init__, which makes the plugin vanish, neither registered nor
        # disabled, behind one log line without a traceback. Treat it like an
        # incompatible version and disable the plugin visibly.
        version_check_failed = False
        try:
            app_version_matching = self.is_app_version_matching()
        except Exception as e:
            log.opt(exception=e).error(
                f"Plugin {self.plugin_id}: could not check version compatibility "
                f"(app-version={self.app_version!r}, minimum-app-version={self.min_app_version!r}). Disabling plugin."
            )
            app_version_matching = False
            version_check_failed = True

        if app_version_matching:
            PluginBase.plugins[self.plugin_id] = {
                "object": self,
                "plugin_version": self.plugin_version,
                "minimum_app_version": self.min_app_version,
                "github": self.github_repo,
                "folder_path": os.path.dirname(inspect.getfile(self.__class__)),
                "file_name": os.path.basename(inspect.getfile(self.__class__))
            }
            self.registered = True

            settings = self.get_settings()
            self.first_setup = settings.get("first-setup", True)
        else:
            reason = "invalid-version" if version_check_failed else None

            if not version_check_failed:
                try:
                    min_app_version = self._get_parsed_base_version(self.min_app_version)
                    if min_app_version is not None and min_app_version > self._get_parsed_base_version(gl.app_version):
                        # The plugin is newer than this Deckard.
                        log.warning(
                            f"Plugin {self.plugin_id} is not compatible with this version of Deckard. "
                            f"Please update Deckard! Plugin requires app version {self.min_app_version} "
                            f"you are running version {gl.app_version}. Disabling plugin."
                        )
                        reason = "app-out-of-date"

                    elif version.parse(self.app_version).major != version.parse(gl.app_version).major:
                        # The plugin is older than this Deckard.
                        max_version = f"{version.parse(self.app_version).major}.x.x"
                        log.warning(
                            f"Plugin {self.plugin_id} is not compatible with this version of Deckard. "
                            f"Please update your assets! Plugin requires an app version between {self.min_app_version} and {max_version} "
                            f"you are running version {gl.app_version}. Disabling plugin."
                        )
                        reason = "plugin-out-of-date"
                except Exception as e:
                    # The and in is_app_version_matching() short-circuits, so
                    # a malformed minimum-app-version can appear here first.
                    log.opt(exception=e).error(
                        f"Plugin {self.plugin_id}: could not determine the disable reason from its version "
                        f"metadata (app-version={self.app_version!r}, minimum-app-version={self.min_app_version!r})."
                    )
                    reason = "invalid-version"

            PluginBase.disabled_plugins[self.plugin_id] = {
                "object": self,
                "plugin_version": self.plugin_version,
                "minimum_app_version": self.min_app_version,
                "github": self.github_repo,
                "folder_path": os.path.dirname(inspect.getfile(self.__class__)),
                "file_name": os.path.basename(inspect.getfile(self.__class__)),
                "reason": reason
            }

    def _get_parsed_base_version(self, version_str: str) -> version.Version:
        """Parse a version string and return the base version.

        Args:
            version_str (str): The version string to parse.

        Returns:
            version.Version: The parsed base version.

        Raises:
            None.
        """
        if version_str is None:
            return
        base_version = version.parse(version_str).base_version
        return version.parse(base_version)

    def get_plugin_id_from_folder_name(self) -> str:
        """Read the plugin id from the folder name of the subclass file.

        Returns:
            str: The plugin id from the folder name.
        """
        module = importlib.import_module(self.__module__)
        subclass_file = module.__file__
        if subclass_file is None:
            # Only a namespace package and a built-in have no __file__, and a
            # plugin always loads from a folder. Without this guard the call
            # reaches os.path.abspath(None) and raises a bare TypeError.
            raise RuntimeError(f"Plugin module {self.__module__} has no file location")
        return os.path.basename(os.path.dirname(os.path.abspath(subclass_file)))
    
    def is_minimum_version_ok(self) -> bool:
        """Check that the app meets the minimum version of the plugin.

        Returns:
            bool: True when the app meets the minimum version.
        """
        if self.min_app_version is None:
            return True
        
        app_version = self._get_parsed_base_version(gl.app_version)
        min_app_version = self._get_parsed_base_version(self.min_app_version)

        return app_version >= min_app_version

    def are_major_versions_matching(self) -> bool:
        """Check that the major versions of the app and the plugin match.

        Returns:
            bool: True when the major versions match.
        """
        app_version = version.parse(gl.app_version)
        # Use the app version the plugin states, not its minimum app version.
        current_app_version = version.parse(self.app_version)

        return app_version.major == current_app_version.major

    #TODO: Better error handling for are_major_versions_matching and is_minimum_version_ok
    def is_app_version_matching(self) -> bool:
        """Check that the app version fits this plugin.

        Returns:
            bool: True when the app version fits the plugin.
        """
        return self.are_major_versions_matching() and self.is_minimum_version_ok()

    def add_action_holder(self, action_holder: ActionHolder):
        """Add an action holder to the plugin.

        Args:
            action_holder (ActionHolder): The action holder to be added.

        Raises:
            ValueError: If action_holder is not an instance of ActionHolder.

        Returns:
            None
        """
        if not isinstance(action_holder, ActionHolder):
            raise ValueError("Please pass an ActionHolder")
        
        if not action_holder.get_is_compatible():
            return
        
        self.action_holders[action_holder.action_id] = action_holder

    def add_action_holders(self, action_holders: list[ActionHolder]):
        for action_holder in action_holders:
            self.add_action_holder(action_holder)

    def add_event_holder(self, event_holder: EventHolder) -> None:
        """Add an event holder to the plugin.

        Args:
            event_holder (EventHolder): The event holder

        Raises:
            ValueError: If the event holder is not an EventHolder

        Returns:
            None
        """
        if not isinstance(event_holder, EventHolder):
            raise ValueError("Please pass an SignalHolder")

        self.event_holders[event_holder.event_id] = event_holder

    def add_event_holders(self, event_holders: list[EventHolder]):
        for event_holder in event_holders:
            self.add_event_holder(event_holder)

    def add_action_holder_group(self, action_holder_group: ActionHolderGroup) -> None:
        self.action_holder_groups.add(action_holder_group)

    def add_action_holder_groups(self, action_holder_groups: list[ActionHolderGroup]) -> None:
        self.action_holder_groups.update(action_holder_groups)

    def connect_to_event(self, callback: Callable[..., Any], event_id: str = None, event_id_suffix: str = None) -> None:
        """Connect a callback to the event with this event id.

        Args:
            callback (callable): The callback the event calls
            event_id (str): The full id of the event. Pass event_id_suffix
                instead to address this plugin's own
                "<plugin_id>::<suffix>" events.

        Returns:
            None
        """
        full_id = event_id or f"{self.get_plugin_id()}::{event_id_suffix}"

        if full_id in self.event_holders:
            self.event_holders[full_id].add_listener(callback)
        else:
            log.warning(f"{full_id} does not exist in {self.plugin_name}")

    def connect_to_event_directly(self, plugin_id: str, event_id: str, callback: Callable[..., Any]) -> None:
        """Connect a callback to the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin
            event_id (str): The id of the event
            callback (callable): The callback the event calls

        Returns:
            None
        """
        plugin = self.get_plugin(plugin_id)
        if plugin is None:
            log.warning(f"{plugin_id} does not exist")
        else:
            plugin.connect_to_event(callback=callback, event_id=event_id)

    def disconnect_from_event(self, event_id: str = None, callback: Callable[..., Any] = None, event_id_suffix: str = None) -> None:
        """Disconnect a callback from the event with this event id.

        Args:
            event_id (str): The full id of the event. Pass event_id_suffix
                instead to address this plugin's own
                "<plugin_id>::<suffix>" events, as connect_to_event does.
            callback (callable): The callback to remove

        Returns:
            None
        """
        full_id = event_id or f"{self.get_plugin_id()}::{event_id_suffix}"

        if full_id in self.event_holders:
            self.event_holders[full_id].remove_listener(callback)
        else:
            log.warning(f"{full_id} does not exist in {self.plugin_name}")

    def disconnect_from_event_directly(self, plugin_id: str, event_id: str, callback: Callable[..., Any]) -> None:
        """Disconnect a callback from the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin
            event_id (str): The full id of the event
            callback (callable): The callback to remove

        Returns:
            None
        """
        plugin = self.get_plugin(plugin_id)
        if plugin is None:
            log.warning(f"{plugin_id} does not exist")
        else:
            plugin.disconnect_from_event(event_id=event_id, callback=callback)

    # Guards the lazy creation of a per-instance settings lock. An instance
    # built through __new__, by the rpyc service plumbing or a harness stub,
    # runs no __init__.
    _settings_lock_guard = threading.Lock()

    def _get_settings_lock(self) -> threading.Lock:
        lock = getattr(self, "_settings_lock", None)
        if lock is None:
            with PluginBase._settings_lock_guard:
                lock = getattr(self, "_settings_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self._settings_lock = lock
        return lock

    def get_settings(self):
        """Read the settings from the settings file.

        Returns:
            dict: The stored settings, or an empty dict without a file.
        """
        # The settings store owns the file layout, the policy for a corrupt or
        # unreadable file, and the migration off the pre-envelope format, with
        # every other settings file of the app. This method owns the lock.
        with self._get_settings_lock():
            return PluginSettings(self.settings_path).read()

    def get_manifest(self):
        """Read the manifest file from the plugin's directory.

        Returns:
            dict: The manifest content, or an empty dict without a file.
        """
        manifest_path = os.path.join(self.PATH, "manifest.json")
        if os.path.exists(manifest_path):
            # A corrupt manifest must not raise. get_plugin_id() and
            # register() call this inside plugin __init__, where an exception
            # makes the plugin vanish without a trace.
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
            except ValueError as e:
                # This file gets no quarantine. manifest.json lives in the
                # plugin's source tree, which the app never writes, so no later
                # save overwrites it and a move aside protects nothing. A move
                # would rename a file out of the developer's git working tree,
                # because a dev plugin here is a symlink into a source
                # checkout, and it would turn a manifest under a rebase into a
                # deleted file. Log it and leave the file where it is.
                #
                # The degradation matches a missing manifest. get_plugin_id()
                # falls back to the folder name, and register() stops at
                # "Please specify a plugin name", which
                # PluginManager.init_plugins() records in load_errors. The scan
                # continues, and a neighboring plugin still loads.
                log.error(
                    f"Plugin manifest {manifest_path} contains invalid JSON: {e} -- treating "
                    f"it as empty and leaving it in place (the app never writes plugin "
                    f"source files)"
                )
                return {}
            except OSError as e:
                log.opt(exception=e).error(
                    f"Could not read plugin manifest {manifest_path} -- treating it as empty"
                )
                return {}
            if isinstance(manifest, dict):
                return manifest
            log.error(f"Plugin manifest {manifest_path} does not contain a JSON object -- treating it as empty")
        return {}

    def get_about(self):
        """Read the about file from the plugin's directory.

        A missing about.json, an undecodable one and one that holds no object
        each give an empty dict. An OSError from an unreadable file still
        propagates, because an unreadable file is a system problem and not bad
        content, and this file comes from the plugin's source tree.

        Returns:
            dict: The about content, or an empty dict without a file.
        """

        about_path = os.path.join(self.PATH, "about.json")
        if os.path.exists(about_path):
            try:
                with open(about_path, "r") as f:
                    about = json.load(f)
            except ValueError as e:
                # Degrade to the missing-file result instead of a raise into
                # the about window. This file gets no quarantine. about.json is
                # a plugin source file the app never writes, so no save
                # destroys a corrupt one, and a move aside would rename a file
                # out of the developer's working tree.
                log.error(
                    f"Plugin about file {about_path} contains invalid JSON: {e} -- treating "
                    f"it as empty and leaving it in place (the app never writes plugin "
                    f"source files)"
                )
                return {}
            if isinstance(about, dict):
                return about
            # A valid about.json that holds a list or a bare string reaches
            # PluginAbout unchanged and raises AttributeError on .get().
            log.error(
                f"Plugin about file {about_path} does not contain a JSON object "
                f"-- treating it as empty"
            )
        return {}
    
    def set_settings(self, settings):
        """Save the given settings to the settings file.

        Args:
            settings (dict): The settings to save.

        Returns:
            None
        """
        # A read-modify-write of one file, under the lock the read side takes.
        # The store wraps this argument in the envelope, keeps the rest of the
        # file, and writes it atomically.
        with self._get_settings_lock():
            PluginSettings(self.settings_path).write(settings)


    def add_css_stylesheet(self, path):
        """Add a CSS stylesheet to the style context of the application.

        This marshals the work onto the GTK main loop. A plugin calls it from
        __init__, which runs on a store worker thread on the install path, and
        both the provider construction and the style-context mutation need the
        main thread. On the main thread the marshal runs inline.

        Args:
            path (str): The path to the CSS file.

        Returns:
            None
        """
        def _add():
            css_provider = Gtk.CssProvider()
            css_provider.load_from_path(path)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        from src.backend.main_loop import run_on_main
        run_on_main(_add)

    def register_page(self, path: str) -> None:
        """Register a page of this plugin for the UI.

        Args:
            path (str): The path of the page to register.

        Returns:
            None
        """
        if gl.page_manager is not None:
            gl.page_manager.register_page(path)
        self.registered_pages.append(path)

    def get_selector_icon(self) -> Gtk.Widget:
        """Return a Gtk.Image widget with the icon "view-paged".

        This marshals the work onto the GTK main loop, for the reason
        add_css_stylesheet and the default icon of ActionHolder give. GTK4
        works on the main thread alone. The one caller in this tree,
        ActionChooser, runs on main, and a plugin override reachable from
        another thread would otherwise build a widget off main and abort the
        process. On the main thread the marshal runs inline.

        Returns:
            Gtk.Widget: A Gtk.Image widget.
        """
        from src.backend.main_loop import run_on_main
        return run_on_main(lambda: Gtk.Image(icon_name="view-paged"))
    
    def on_uninstall(self) -> None:
        """Unregister the plugin pages and stop a running backend connection.

        The app calls this during the uninstall of the plugin.

        Returns:
            None
        """ 
        for page in self.registered_pages:
            if gl.page_manager is not None:
                gl.page_manager.unregister_page(page)
        try:
            if self.backend is not None:
                self.on_disconnect(self.backend_connection)
        except Exception as e:
            log.error(e)

    def get_plugin(self, plugin_id: str) -> "PluginBase | None":
        """Return the plugin with this plugin id.

        Args:
            plugin_id (str): The id of the plugin to return.

        Returns:
            PluginBase: The plugin object, or None.
        """
        if gl.plugin_manager is None:
            return None
        return gl.plugin_manager.get_plugin_by_id(plugin_id) or None

    # Asset Management

    def add_icon(self, key: str, path: str, size:float=1.0, halign:float=0.0, valign:float=0.0):
        self.asset_manager.icons.add_asset(key=key, asset=Icon(path=path, size=size, halign=halign, valign=valign))

    def add_color(self, key: str, color: tuple[int, int, int, int]):
        self.asset_manager.colors.add_asset(key=key, asset=Color(color=color))

    def get_asset_path(self, asset_name: str, subdirs: list[str] = None, asset_folder: str = "assets") -> str:
        """
        Helper method that returns paths to plugin assets.

        Args:
            asset_name (str): Name of the Asset File
            subdirs (list[str], optional): Subdirectories. Defaults to [].
            asset_folder (str, optional): Name of the folder where assets are stored. Defaults to "assets".

        Returns:
            str: The full path to the asset
        """

        if not subdirs:
            return os.path.join(self.PATH, asset_folder, asset_name)

        subdir = os.path.join(*subdirs)
        if subdir != "":
            return os.path.join(self.PATH, asset_folder, subdir, asset_name)
        return ""

    def get_settings_area(self):
        pass

    # Rpyc

    def start_server(self) -> None:
        """Start the rpyc server of the plugin.

        It starts a ThreadedServer, which accepts remote procedure calls for
        the plugin. With a running server it logs a warning and starts none.

        Returns:
            None
        """
        if self.server is not None:
            log.warning("Server already running, skipping...")
            return
        self.server = ThreadedServer(self, hostname="localhost", port=0, protocol_config={"allow_public_attrs": True})
        threading.Thread(target=self.server.start, name="server_start", daemon=True).start()

    def on_disconnect(self, conn: Connection) -> None:
        """Handle the disconnection of the rpyc server.

        It releases the rpyc server, the backend connection and the backend
        process. It clears the references here, and the blocking close and
        terminate work runs on a worker thread. See
        _release_backend_resources. A call from the UI thread, such as the
        uninstall path through on_uninstall, therefore never stalls.

        Args:
            conn (Connection): The connection to disconnect.

        Returns:
            None
        """
        # The caller asked for this stop. A deactivation and an unload route
        # here, and an rpyc drop lands here too. Disarm an armed registration
        # watchdog, so it does not report the terminate below as a backend that
        # exited before it registered.
        self._backend_stop_requested = True
        self._release_backend_resources()

    def _release_backend_resources(self) -> None:
        """Detach and tear down the rpyc server, connection and process.

        It mirrors ActionCore._release_backend_resources. It clears the
        references here, so a later launch_backend() or start_server() finds a
        clean slate instead of a dead server to skip against. The blocking work
        runs on a daemon worker, because an rpyc close can wait on a running
        call, and terminate_backend_process sends SIGTERM, waits 3 seconds,
        sends SIGKILL and waits 2 more. The caller, often the GTK main thread
        on the uninstall path, therefore never blocks. It is idempotent, and
        concurrent callers tolerate a lost race, as the ActionCore version
        does, because a close or a terminate of a dead resource is
        harmless."""
        if self.backend_connection is None and self.server is None and self.backend_process is None:
            return

        # Snapshot and detach the backend resources, then close them
        # off-thread.
        server, connection, process = self.server, self.backend_connection, self.backend_process
        self.server = None
        self.backend_connection = None
        self.backend_process = None
        self.backend = None

        # Drop these from the global registries. Both are list removals.
        if connection is not None and gl.plugin_manager is not None:
            try:
                gl.plugin_manager.backends.remove(connection)
            except ValueError:
                pass
        if process is not None and gl.plugin_manager is not None:
            try:
                gl.plugin_manager.backend_processes.remove(process)
            except ValueError:
                pass

        threading.Thread(
            target=self._teardown_backend_resources,
            args=(server, connection, process),
            name="plugin_backend_teardown",
            daemon=True,
        ).start()

    @staticmethod
    def _teardown_backend_resources(server, connection, process) -> None:
        # This runs on a worker thread. See _release_backend_resources. Each
        # close and terminate tolerates a failure, because a hung backend must
        # not stop the app.
        if connection is not None:
            try:
                connection.close()
            except Exception as e:
                log.error(f"Failed to close backend connection: {e}")
        if server is not None:
            try:
                server.close()
            except Exception as e:
                log.error(f"Failed to close backend server: {e}")
        if process is not None:
            from src.backend.PluginManager.PluginManager import terminate_backend_process
            terminate_backend_process(process)

    def launch_backend(self, backend_path: str, venv_path: str = None, open_in_terminal: bool = False) -> None:
        """Launch the backend process of the plugin.

        It starts the rpyc server, builds the command that runs the backend
        script, and runs it in a new subprocess. It can open the backend in a
        new terminal window.

        Args:
            backend_path (str): The path to the backend script.
            venv_path (str, optional): The path to the virtual environment
                whose interpreter runs the backend. Defaults to None, which
                selects this app's own interpreter.
            open_in_terminal (bool, optional): Open the backend in a new
                terminal window. Defaults to False.

        Raises:
            ValueError: When backend_path is None or absent, or when a given
                venv_path is absent. The validation stops a bad path here,
                before Popen receives it.

        Returns:
            None
        """
        from src.backend.PluginManager.PluginManager import build_backend_launch_command

        self.start_server()
        port = self.server.port

        # It validates the paths and returns argv, and not a shell string.
        command = build_backend_launch_command(backend_path, venv_path, port, open_in_terminal)

        log.info(f"Launching backend: {command}")
        self._backend_stop_requested = False
        self._backend_launch_gen += 1
        # Cleared after the validation and before the spawn, so a relaunch
        # waits for the registration of the new backend instead of a return on
        # the registration of the previous one.
        self._backend_ready.clear()
        self.backend_process = subprocess.Popen(command, start_new_session=True)
        if gl.plugin_manager is not None:
            gl.plugin_manager.backend_processes.append(self.backend_process)

        self.wait_for_backend()
        if self.backend_connection is None:
            # The registration is asynchronous. The subprocess must start
            # python, connect back over rpyc and call register_backend, and the
            # bounded wait above gives up after about 0.3 seconds. A boot misses
            # that window often, so this keeps watching and makes the gap
            # visible instead of a silent None in self.backend.
            self._watch_backend_registration(self.backend_process, self._backend_launch_gen)

    def _watch_backend_registration(self, process: subprocess.Popen, launch_gen: int, timeout: float = 30.0) -> None:
        """Observe a launched backend that has not registered yet.

        This manages nothing. On a bounded daemon thread it logs the
        registration latency when the registration arrives, and an error when
        the process dies or the timeout expires. launch_backend, on_disconnect
        and terminate_backend_process own the process lifecycle. The watch
        disarms in silence when a relaunch supersedes it, which a launch_gen
        mismatch shows, when a caller asked for the stop through
        _backend_stop_requested, or when the app quits."""
        plugin_id = self.get_plugin_id_from_folder_name()

        def _watch() -> None:
            start = time.time()
            deadline = start + timeout
            while time.time() < deadline:
                if self._backend_launch_gen != launch_gen:
                    # A relaunch superseded this watchdog, and the new launch
                    # has its own. Without this check the watchdog attributes
                    # the registration of the new backend, or the reaped exit
                    # of the old process, to the launch it was armed for.
                    return
                if self._backend_stop_requested:
                    log.debug(f"Plugin {plugin_id}: backend stop requested before registration; watchdog disarmed")
                    return
                if not gl.threads_running:
                    return
                if self.backend_connection is not None:
                    log.info(f"Plugin {plugin_id}: backend registered after {time.time() - start:.1f}s")
                    return
                if process is not None and process.poll() is not None:
                    log.error(
                        f"Plugin {plugin_id}: backend process exited with code "
                        f"{process.returncode} before registering -- its actions will stay inert"
                    )
                    return
                time.sleep(0.25)
            log.error(
                f"Plugin {plugin_id}: backend did not register within {timeout:.0f}s -- "
                f"its actions will stay inert until it does"
            )

        threading.Thread(target=_watch, name=f"backend_watch_{plugin_id}", daemon=True).start()

    def wait_for_backend(self, tries: int = 3) -> None:
        """Wait for the backend to establish a connection.

        It blocks until register_backend signals the connection, or until the
        timeout expires.

        Args:
            tries (int, optional): A timeout budget in units of 0.1 seconds,
                so the default of 3 waits up to 0.3 seconds. The registration
                wakes this thread at once.

        Returns:
            None
        """
        self._backend_ready.wait(timeout=tries * 0.1)

    def register_backend(self, port: int) -> None:
        """Register the backend connection of the plugin.

        This is an internal method. Do not call it manually. It connects to the
        backend on the given port and adds the connection to the global plugin
        manager.

        Args:
            port (int): The port of the backend.

        Returns:
            None
        """
        self.backend_connection = rpyc.connect("localhost", port, config={"allow_public_attrs": True})
        self.backend = self.backend_connection.root

        if gl.plugin_manager is not None:
            gl.plugin_manager.backends.append(self.backend_connection)

        # Only after the connection attributes hold their values, because the
        # caller that wait_for_backend wakes reads self.backend at once. Also
        # before the plugin hook below, which can be slow.
        self._backend_ready.set()

        # The backend process itself calls register_backend over rpyc, so this
        # isolates the hook. A raising plugin hook must not break the
        # registration call of the backend.
        try:
            self.on_backend_ready()
        except Exception as e:
            log.error(f"Plugin {self.get_plugin_id_from_folder_name()}: on_backend_ready failed: {e}")

    def on_backend_ready(self) -> None:
        """The app calls this after the backend connected and registered.

        It mirrors ActionCore.on_backend_ready. A backend can register late on
        a busy boot, so sync the state that depends on the backend here instead
        of at launch_backend() time. It runs on the rpyc service thread, so do
        not touch GTK from it. It does nothing by default.
        """
        pass

    def on_app_ready(self) -> None:
        """The app calls this once after it finished starting.

        The call is asynchronous, in windowed mode and in background mode with
        -b. Launch a plugin backend or start long-lived work here. __init__
        runs during startup and blocks it, and the on_ready of an action never
        fires while no deck is connected, so a backend launched from either one
        can leave the first hardware presses inert after an autostart boot. It
        runs on a background thread, so do not touch GTK from it. It does
        nothing by default.
        """
        pass

    def ping(self) -> bool:
        """Check that the plugin answers.

        Returns:
            bool: Always True.
        """
        return True

    def request_dbus_permission(self, name: str, bus: str = "session", description: str = None) -> None:
        """Request a DBus permission for the plugin.

        It shows a dialog that requests the DBus permission for the given bus,
        name and description. Without a description it uses a default one.

        Args:
            name (str): The name of the bus.
            bus (str, optional): The bus type, "session" or "system".
                Defaults to "session".
            description (str, optional): Why the plugin needs the permission.
                Defaults to None.

        Raises:
            ValueError: When the plugin requests a permission before it
                registers.

        Returns:
            None
        """
        if description is None:
            description = gl.lm.get("permissions.request.plugin-blueprint")
            if self.plugin_name is None:
                raise ValueError("Register the plugin before requesting permissions")
            description = description.replace("{name}", self.plugin_name)
        gl.flatpak_permission_manager.show_dbus_permission_request_dialog(name, bus, description)

    def get_config_rows(self) -> list[Adw.PreferencesRow]:
        return []