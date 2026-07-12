
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
import threading
import time
from loguru import logger as log
from copy import copy
import subprocess
import os
from PIL import Image
import gi

from src.backend.PluginManager.EventManager import EventManager
from src.backend.PluginManager.EventAssigner import EventAssigner

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.protocol import Connection
from rpyc.core import netref

# Import own modules
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
from src.backend.DeckManagement.HelperMethods import is_image, is_svg, is_video
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Media.Media import Media
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.Signals.Signals import Signal

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING

from src.backend.PluginManager.PluginSettings.Asset import Color,Icon

if TYPE_CHECKING:
    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.DeckManagement.DeckController import DeckController, ControllerKey, ControllerKeyState
    from src.backend.PageManagement.Page import Page
    from src.backend.DeckManagement.DeckController import ControllerInput, ControllerInputState

class ActionCore(rpyc.Service):
    # Change to match your action
    def __init__(self, action_id: str, action_name: str,
                 deck_controller: "DeckController", page: "Page", plugin_base: "PluginBase", state: int,
                 input_ident: "InputIdentifier"):
        self.backend_connection: Connection = None
        self.backend: netref = None
        self.server: ThreadedServer = None
        self.backend_process: subprocess.Popen = None

        # (signal, callback) pairs registered by this action, disconnected on teardown.
        self._connected_signals: list[tuple] = []

        # clean_up() is reachable from eviction (whatever thread calls
        # get_page -- USB monitor, media thread) AND the rpyc on_disconnect
        # hook, so idempotency needs a real lock, not just a bool.
        self._cleaned_up = False
        self._cleanup_lock = threading.Lock()

        self.deck_controller = deck_controller
        self.page = page
        self.state = state
        self.input_ident = input_ident
        self.action_id = action_id
        self.action_name = action_name
        self.plugin_base = plugin_base
        self.generative_ui_objects: list[GenerativeUI] = []

        self.on_ready_called = False
        # Set only after on_ready() has returned (or raised). Ticks and
        # external on_update() dispatch gate on this, not on_ready_called:
        # on_ready_called is true from schedule time so that plugin API calls
        # made *inside* on_ready pass raise_error_if_not_ready.
        self.on_ready_finished = False

        self.has_configuration = False
        self.allow_event_configuration: bool = True

        self.put_custom_config_rows_below_gen_ui: bool = False

        self.labels = {}

        self.event_manager = EventManager()

        log.info(f"Loaded action {self.action_name} with id {self.action_id}")

    def clear_event_assigners(self):
        self.event_manager.clear_event_assigners()

    def load_event_overrides(self):
        self.event_manager.set_overrides(self.get_event_assignments())
        
    def set_deck_controller(self, deck_controller):
        """
        Internal function, do not call manually
        """
        self.deck_controller = deck_controller
 
    def set_page(self, page):
        """
        Internal function, do not call manually
        """
        self.page = page

    def get_input(self) -> "ControllerInput":
        return self.deck_controller.get_input(self.input_ident)
    
    def get_state(self) -> "ControllerInputState":
        i = self.get_input()
        if i is None: return
        return i.states.get(self.state)
    
    def add_event_assigner(self, event_assigner: EventAssigner):
        self.event_manager.add_event_assigner(event_assigner)

    def _raw_event_callback(self, event: InputEvent, data: dict = None):
        event_assigner = self.event_manager.get_event_assigner_for_event(event)
        if event_assigner:
            event_assigner.call(data)

    def event_callback(self, event: InputEvent, data: dict = None):
        pass

    def on_trigger(self):
        pass

    def on_tick(self):
        pass

    def on_ready(self):
        """
        This method is called when the page is ready to process requests made by the actions.
        Setting the default image in this method is recommended over setting it in the constructor.

        Threading contract: this hook runs OFF the GTK main thread (page
        loads happen on worker/USB/store threads). Do not construct or touch
        raw GTK objects here -- that is the process-fatal off-main-GTK crash
        class (issue #35). Use the GenerativeUI layer (which marshals itself
        to the main loop) or wrap unavoidable GTK work in
        GtkHelper.GtkHelper.run_on_main.
        """
        pass

    def on_update(self):
        """
        This method gets called when the app wants the action to redraw itself (image, labels, etc.).
        """
        self.on_ready() # backward compatibility

    def set_media(self, image = None, media_path=None, size: float = None, valign: float = None, halign: float = None, fps: int = 30, loop: bool = True, update: bool = True):
        self.raise_error_if_not_ready()

        if type(self.input_ident) not in [Input.Key, Input.Dial]:
            return

        if not self.get_is_present(): return
        if self.has_custom_user_asset(): return
        if not self.has_image_control(): return #TODO
        
        input_state = self.get_state()

        if input_state is None:
            return
        if self.get_state().state != self.state:
            return

        # mem-plan P2.4: only set when `image` came from opening media_path
        # ourselves -- a plugin-supplied `image` has no known source file to
        # re-decode from later, so InputImage must keep upscaling it as
        # before rather than trying (and failing) to re-open media_path.
        path_for_reopen = None
        if is_image(media_path) and image is None:
            with Image.open(media_path) as img:
                image = img.copy()
            path_for_reopen = media_path

        if is_svg(media_path) and image is None:
            image = gl.media_manager.generate_svg_thumbnail(media_path)

        if image is not None:
            input_state.set_image(InputImage(
                controller_input=self.get_state().controller_input,
                image=image,
                path=path_for_reopen,
            ), update=False)

        elif is_video(media_path):
            input_state.set_video(InputVideo(
                controller_input=self.get_state().controller_input,
                video_path=media_path,
                fps=fps,
                loop=loop
            ))

        else:
            input_state.set_image(None, update=False)

        self.get_state().layout_manager.set_action_layout(ImageLayout(
            valign=valign,
            halign=halign,
            size=size
        ), update=False)

        if update:
            self.get_input().update()

    def set_background_color(self, color: list[int] = [0, 0, 0, 0], update: bool = True):
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return

        if not self.has_background_control(): return

        if not self.on_ready_called:
            update = False

        state = self.get_state()
        if state is None or state.state != self.state: return

        state.background_manager.set_action_color(color)
        if update:
            self.get_input().update()

    def show_error(self, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        try:
            self.get_state().show_error(duration=duration)
        except AttributeError as e:
            log.error(e)
            pass

    def hide_error(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        try:
            self.get_state().hide_error()
        except AttributeError:
            pass

    def show_overlay(self, image: Image.Image, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        try:
            self.get_state().show_overlay(image, duration=duration)
        except AttributeError:
            pass

    def hide_overlay(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        try:
            self.get_state().hide_overlay()
        except AttributeError:
            pass

    def set_label(self, text: str, position: str = "bottom", color: list[int]=None,
                  font_family: str=None, font_size=None, outline_width: int = None, outline_color: list[int] = None,
                  font_weight: int = None, font_style: str = None,
                  update: bool=True):
        self.raise_error_if_not_ready()

        if type(self.input_ident) not in [Input.Key, Input.Dial]:
            return
        
        if self.get_state() is None:
            log.error(f"Could not find state, action: {self.action_id}, state: {self.state}")
            return
        
        if not self.get_is_present():
            return
        if not self.on_ready_called:
            update = False
            update = True #FIXME

        if font_style not in ["normal", "italic", "oblique", None]:
            raise ValueError("font_style must be one of ['normal', 'italic', 'oblique', None]")

        label_index = 0 if position == "top" else 1 if position == "center" else 2

        if not self.has_label_control(label_index):
            return
        
        if text is None:
            text = ""

        text = str(text)

        self.labels[position] = {
            "text": text,
            "color": color,
            "font-family": font_family,
            "font-size": font_size,
            "outline_width": outline_width,
            "outline_color": outline_color,
            "font-weight": font_weight,
            "font-style": font_style
        }
        
        key_label = KeyLabel(
            controller_input=self.get_state().controller_input,
            text=text,
            font_size=font_size,
            font_name=font_family,
            color=color,
            outline_width=outline_width,
            outline_color=outline_color,
            font_weight=font_weight,
            style=font_style
        )
        self.get_state().label_manager.set_action_label(label=key_label, position=position, update=update)

    def set_top_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text, "top", color, font_family, font_size, outline_width, outline_color, font_weight, font_style, update)

    def set_center_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text, "center", color, font_family, font_size, outline_width, outline_color, font_weight, font_style, update)

    def set_bottom_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text, "bottom", color, font_family, font_size, outline_width, outline_color, font_weight, font_style, update)

    def on_labels_changed_in_ui(self):
        # TODO
        pass

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        return []
    
    def get_custom_config_area(self):
        return
    
    def get_settings(self) -> dir:
        # self.page.load()
        if self.page is None:
            return {}
        return self.page.get_action_settings(action_object=self)
    
    def set_settings(self, settings: dict):
        if self.page is None:
            return
        self.page.set_action_settings(action_object=self, settings=settings)

    def connect(self, signal: Signal = None, callback: callable = None) -> None:
        # Connect
        gl.signal_manager.connect_signal(signal = signal, callback = callback)
        # Track so we can disconnect on teardown (see clean_up)
        self._connected_signals.append((signal, callback))

    def get_own_key(self) -> "ControllerKey":
        # The old body read `deck_controller.keys` / `self.key_index`,
        # neither of which has ever existed on these classes (issue #56).
        # Kept (rather than deleted) because it is upstream plugin-API
        # surface; resolve through the identifier like get_input() does.
        # Returns None for non-key actions.
        if not isinstance(self.input_ident, Input.Key):
            return None
        return self.deck_controller.get_input(self.input_ident)
    
    def get_is_multi_action(self) -> bool:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        actions = self.page.action_objects.get(self.input_ident.input_type, {}).get(self.input_ident.json_identifier, [])
        return len(actions) > 1

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
            return os.path.join(self.plugin_base.PATH, asset_folder, asset_name)

        subdir = os.path.join(*subdirs)
        if subdir != "":
            return os.path.join(self.plugin_base.PATH, asset_folder, subdir, asset_name)
        return ""

    def get_icon(self, key: str, skip_override: bool = False) -> Icon | None:
        return self.plugin_base.asset_manager.icons.get_asset(key, skip_override)

    def get_color(self, key: str, skip_override: bool = False) -> Color | None:
        return self.plugin_base.asset_manager.colors.get_asset(key, skip_override)

    def get_translation(self, key: str, fallback: str = None):
        return self.plugin_base.locale_manager.get(key, fallback)
    
    def has_label_controls(self):
        own_action_index = self.get_own_action_index()
        return [own_action_index == i for i in self.get_state().action_permission_manager.get_label_control_indices()]
    
    def has_label_control(self, label_index) -> list[bool]:
        #TODO: Might require performance improvements
        return self.get_state().action_permission_manager.get_label_control_index(label_index) == self.get_own_action_index()

    def has_image_control(self):
        #TODO: Might require performance improvements
        image_control_index = self.get_state().action_permission_manager.get_image_control_index()
        return image_control_index == self.get_own_action_index()


        key_dict = self.input_ident.get_config(self.page).get("states", {}).get(str(self.state), {})

        if key_dict.get("image-control-action") is None:
            return False
        
        if ("image-control-action" not in key_dict) and (not self.get_is_multi_action()):
            return True

        return self.get_own_action_index() == key_dict.get("image-control-action")
    
    def has_background_control(self):
        #TODO: Might require performance improvements
        background_control_index = self.get_state().action_permission_manager.get_background_control_index()
        return background_control_index == self.get_own_action_index()
    
    def get_is_present(self):
        if self.page is None: return False
        if self.page.deck_controller.active_page is not self.page: return False
        if self.page.deck_controller.screen_saver.showing: return False
        # if self.state != self.get_state().state: return False #TODO: Check for touchscreen and dial states
        return self in self.page.get_all_actions()
    
    def has_custom_user_asset(self) -> bool:
        if not self.get_is_present(): return False
        media = self.input_ident.get_config(self.page).get("states", {}).get(str(self.state), {}).get("media", {})
        return media.get("path", None) is not None
    
    def get_own_action_index(self) -> int:
        if not self.get_is_present(): return -1
        actions = self.page.get_all_actions_for_input(self.input_ident, self.state)
        if self not in actions:
            return
        return actions.index(self)

    def get_page_event_assignments(self) -> dict[InputEvent, InputEvent]:
        assignment = {}

        page_assignment_dict = self.page.get_action_event_assignments(action_object=self)

        all_events = Input.AllEvents()
        for event in all_events:
            if event.string_name in page_assignment_dict:
                assignment[event] = Input.EventFromStringName(page_assignment_dict[event.string_name])
            else:
                assignment[event] = event

        return assignment
    
    def set_all_events_to_null(self):
        for input_type in self.event_manager.get_event_map().keys():
            self.set_event_assignment(input_type, None)

    
    def get_event_assignments(self) -> dict[str, str]:
        return self.page.get_action_event_assignments(
            action_object=self
        )
    
    def set_event_assignment(self, input_event: InputEvent | None, event_assigner: EventAssigner | None):
        self.page.set_action_event_assigment(
            event_assigner=event_assigner,
            input_event=input_event,
            action_object=self
        )

        self.load_event_overrides()
    
    def raise_error_if_not_ready(self):
        if self.on_ready_called:
            return
        raise Warning("Seems like you're calling this method before the action is ready")
    
    def get_generative_ui_objects(self) -> list[GenerativeUI]:
        objects = []
        for attr in dir(self):
            if isinstance(getattr(self, attr), GenerativeUI):
                objects.append(getattr(self, attr))

        return objects

    def add_generative_ui_object(self, generative_ui_object: GenerativeUI):
        self.generative_ui_objects.append(generative_ui_object)

    def remove_generative_ui_object(self, generative_ui_object: GenerativeUI):
        """Unregister a GenerativeUI element (e.g. a dynamically-rebuilt config
        row) so it stops being retained for the action's lifetime."""
        try:
            self.generative_ui_objects.remove(generative_ui_object)
        except ValueError:
            pass

    def get_generative_ui(self):
        return self.generative_ui_objects

    def get_generative_ui_widgets(self):
        widgets = []

        for generative_object in self.generative_ui_objects:
            widget = generative_object.widget

            if widget is None:
                continue

            widgets.append(widget)
        return widgets

    def load_initial_generative_ui(self):
        GLib.idle_add(self._do_load_initial_generative_ui)

    def _do_load_initial_generative_ui(self):
        # P4.1: GenerativeUI widgets build lazily on first `.widget` access
        # (config-open, normally). Calling load_initial_ui()
        # unconditionally here would touch `.widget` on every action's
        # on_ready and force every gen-ui object in the app to build,
        # defeating the laziness entirely. The persisted value is already
        # the source of truth (get_value() reads settings directly), so an
        # unbuilt object has nothing to sync -- only reconcile widgets that
        # some plugin already forced into existence (e.g. touched `.widget`
        # at construction time).
        for generative_object in self.generative_ui_objects:
            if generative_object.is_built:
                generative_object.load_initial_ui()
    
    # ---------- #
    # Rpyc stuff #
    # ---------- #

    def start_server(self):
        if self.server is not None:
            log.warning("Server already running, skipping...")
            return
        self.server = ThreadedServer(self, hostname="localhost", port=0, protocol_config={"allow_public_attrs": True})
        threading.Thread(target=self.server.start, name="server_start", daemon=True).start()

    def on_disconnect(self, conn=None):
        # rpyc disconnect hook: a dropped connection with the process still
        # alive would orphan the backend, so run the full teardown here too.
        self._release_backend_resources()
    
    def launch_backend(self, backend_path: str, venv_path: str = None, open_in_terminal: bool = False):
        self.start_server()
        port = self.server.port

        if venv_path is not None:
            if not os.path.exists(venv_path):
                raise ValueError(f"Venv path does not exist: {venv_path}")
        # The gate used to be inverted (`if backend_path is None:` guarding
        # the exists() check), so None reached os.path.exists -> TypeError
        # and a real-but-missing path sailed through to Popen (issue #56).
        if backend_path is None or not os.path.exists(backend_path):
            raise ValueError(f"Backend path does not exist: {backend_path}")

        ## Launch
        if open_in_terminal:
            command = "gnome-terminal -- bash -c '"
            if venv_path is not None:
                command += f". {venv_path}/bin/activate && "
            command += f"python3 {backend_path} --port={port}; exec $SHELL'"
        else:
            command = ""
            if venv_path is not None:
                command = f". {venv_path}/bin/activate && "
            command += f"python3 {backend_path} --port={port}"

        log.info(f"Launching backend: {command}")
        self.backend_process = subprocess.Popen(command, shell=True, start_new_session=True)
        gl.plugin_manager.backend_processes.append(self.backend_process)

        self.wait_for_backend()

    def wait_for_backend(self, tries: int = 3):
        while tries > 0 and self.backend_connection is None:
            time.sleep(0.1)
            tries -= 1

    def register_backend(self, port: int):
        """
        Internal method, do not call manually
        """
        self.backend_connection = rpyc.connect("localhost", port, config={"allow_public_attrs": True})
        self.backend = self.backend_connection.root
        gl.plugin_manager.backends.append(self.backend_connection)
        self.on_backend_ready()

    def on_backend_ready(self):
        pass

    def ping(self) -> bool:
        return True
    
    def on_removed_from_cache(self) -> None:
        """Notification hook: fired when this action is dropped from a live
        page/cache (reload diff, plugin uninstall, sidebar/config removal,
        cache eviction -- see docs/memory-footprint-plan.md D1). This is a
        pure notification. The framework unconditionally calls clean_up()
        immediately after invoking this hook -- even if a plugin overrides
        this method without calling super(), and even if the override
        raises -- so plugins must NOT rely on calling clean_up() themselves
        from here (harmless if they do; clean_up() is idempotent)."""
        pass

    def on_remove(self) -> None:
        """Notification hook: fired when the user removes this action via the
        action configurator's remove button. Same contract as
        on_removed_from_cache() -- clean_up() is guaranteed by the framework
        regardless of what this override does."""
        pass

    @staticmethod
    def teardown(action, hook_name: str = "on_removed_from_cache") -> None:
        """Framework-owned drop-site teardown. Call this (instead of just
        invoking the hook) at every place an action is dropped from a live
        structure: it notifies via the named hook (best-effort -- a plugin
        override that raises or forgets super() can't skip cleanup) and then
        unconditionally calls clean_up(). `action` may be a non-ActionCore
        placeholder (NoActionHolderFound/ActionOutdated); those are silently
        ignored, matching the existing isinstance guards at the call sites."""
        if not isinstance(action, ActionCore):
            return
        try:
            getattr(action, hook_name)()
        except Exception:
            log.opt(exception=True).error(
                f"{hook_name} failed for {getattr(action, 'action_id', action)}"
            )
        action.clean_up()

    def clean_up(self) -> None:
        """Framework teardown when this action is dropped (page reload,
        plugin uninstall, sidebar/config removal, or cache eviction).
        Idempotent -- guarded by a lock, since eviction and the rpyc
        on_disconnect path can race to call this from different threads.

        Runs from *any* thread (main, USB monitor, media thread via page
        eviction) -- never call run_on_main() from in here, or from anything
        this method calls synchronously. GenerativeUI disposal is real GTK
        work, so it's marshalled onto the main loop via GLib.idle_add instead
        of being done inline. Backend teardown is likewise offloaded to a
        worker thread: closing an rpyc server/connection can block on an
        in-flight call that needs the main loop, which would deadlock the UI.

        Queued-callback contract (issue #56): clean_up() does NOT flush or
        cancel work already queued elsewhere with a strong reference to this
        action -- an event callback (on_key_down/on_tick/...) submitted to
        the deck's action executor, or a GLib idle, dispatched just before
        teardown can still run *after* clean_up() returns (the executor's
        futures are only cancelled wholesale at deck close). Plugin hooks
        must therefore tolerate running on a cleaned-up action:
        get_is_present() is the recommended guard, and settings reads
        degrade to {} once the page reference drops."""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        # Disconnect signal callbacks synchronously so the SignalManager stops
        # retaining this action.
        for signal, callback in self._connected_signals:
            try:
                gl.signal_manager.disconnect_signal(signal, callback)
            except Exception as e:
                log.error(f"Failed to disconnect signal {signal}: {e}")
        self._connected_signals.clear()

        # Snapshot-and-clear synchronously (cheap list ops) so callers can
        # observe an empty generative_ui_objects list the moment clean_up()
        # returns; the actual widget teardown is GTK work and must happen on
        # the main loop, so it's queued rather than done here.
        gen_ui_snapshot = list(self.generative_ui_objects)
        self.generative_ui_objects.clear()
        if gen_ui_snapshot:
            GLib.idle_add(self._destroy_gen_ui_batch, gen_ui_snapshot)

        self._release_backend_resources()

    @staticmethod
    def _destroy_gen_ui_batch(snapshot: list[GenerativeUI]) -> None:
        """GLib.idle_add callback queued from clean_up(): destroys each
        GenerativeUI object snapshotted at teardown time. Runs on the GTK
        main loop, where GenerativeUI.destroy()'s internal run_on_main()
        executes inline (GtkHelper.py) -- no re-queueing, no deadlock risk."""
        for obj in snapshot:
            try:
                owner = obj.action_core
                if owner is not None and obj in owner.generative_ui_objects:
                    # Re-registered on a live action since the snapshot was
                    # taken (e.g. a rebuilt/resurrected row) -- it's owned
                    # again, don't tear it down out from under the action.
                    continue
                if getattr(obj, "_widget", None) is None:
                    # Never built a widget -- nothing to unparent, and it's
                    # already off generative_ui_objects. Also covers P4.1's
                    # future lazy-widget objects that never got touched.
                    continue
                obj.destroy()
            except Exception:
                log.opt(exception=True).error(f"Failed to destroy GenerativeUI object {obj!r}")

    def _release_backend_resources(self) -> None:
        """Detach and tear down the rpyc server/connection and the backend
        process. Idempotent; safe against concurrent calls from clean_up and
        the rpyc on_disconnect hook (close/terminate tolerate a lost race)."""
        if self.backend_connection is None and self.server is None and self.backend_process is None:
            return

        # Snapshot and detach the backend resources, then close them off-thread.
        server, connection, process = self.server, self.backend_connection, self.backend_process
        self.server = None
        self.backend_connection = None
        self.backend_process = None
        self.backend = None

        # Drop from the global registries synchronously (cheap list removals).
        if connection is not None:
            try:
                gl.plugin_manager.backends.remove(connection)
            except ValueError:
                pass
        if process is not None:
            try:
                gl.plugin_manager.backend_processes.remove(process)
            except ValueError:
                pass

        threading.Thread(
            target=self._teardown_backend_resources,
            args=(server, connection, process),
            name="action_backend_teardown",
            daemon=True,
        ).start()

    @staticmethod
    def _teardown_backend_resources(server, connection, process) -> None:
        # Runs on a worker thread (see clean_up). Each close()/terminate() is
        # best-effort; a hung backend must not take the app down with it.
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
