
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
from loguru import logger as log
import subprocess
import os
from PIL import Image

from src.backend.PluginManager.EventManager import EventManager
from src.backend.PluginManager.EventAssigner import EventAssigner

from gi.repository import GLib

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core.protocol import Connection
from rpyc.core import netref

from src.backend.DeckManagement.HelperMethods import is_image, is_svg, is_video
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.Signals.Signals import Signal

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from src.backend.PluginManager.PluginSettings.Asset import Color,Icon

if TYPE_CHECKING:
    # Type-only. Nothing here runs gi.require_version("Adw", "1"), and the
    # annotation that uses Adw is a string.
    from gi.repository import Adw
    # GenerativeUI imports Gtk at module scope, and ActionCore sits in the
    # import closure of the engine through DeckController and Page. The name
    # stays type-only here, and the isinstance below imports it lazily.
    from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput, ControllerInputState, ControllerKey
    from src.backend.PageManagement.Page import Page

class ActionCore(rpyc.Service):
    # Change to match your action
    def __init__(self, action_id: str, action_name: str,
                 deck_controller: "DeckController", page: "Page", plugin_base: "PluginBase", state: int,
                 input_ident: "InputIdentifier"):
        self.backend_connection: Connection = None
        self.backend: netref = None
        self.server: ThreadedServer = None
        self.backend_process: subprocess.Popen | None = None
        # register_backend sets this on an rpyc service thread, which the
        # backend process drives, and it wakes wait_for_backend on the
        # launching thread.
        self._backend_ready = threading.Event()

        # The (signal, callback) pairs of this action, disconnected on teardown.
        self._connected_signals: list[tuple] = []

        # An eviction reaches clean_up() from whichever thread calls get_page,
        # the USB monitor or the media thread, and the rpyc on_disconnect hook
        # reaches it too. A bool cannot make it idempotent, so it takes a
        # lock.
        self._cleaned_up = False
        self._cleanup_lock = threading.Lock()

        self.deck_controller = deck_controller
        self.page = page
        self.state = state
        self.input_ident = input_ident
        self.action_id = action_id
        self.action_name = action_name
        self.plugin_base = plugin_base
        self.generative_ui_objects: list["GenerativeUI"] = []

        self.on_ready_called = False
        # Set after on_ready() returned or raised. A tick and an external
        # on_update() dispatch gate on this flag and not on on_ready_called.
        # on_ready_called reads True from schedule time, so a plugin API call
        # inside on_ready passes raise_error_if_not_ready.
        self.on_ready_finished = False

        self.has_configuration = False
        self.allow_event_configuration: bool = True

        self.put_custom_config_rows_below_gen_ui: bool = False

        self.labels: dict[str, dict[str, Any]] = {}

        self.event_manager = EventManager()

        log.info(f"Loaded action {self.action_name} with id {self.action_id}")

    def clear_event_assigners(self):
        self.event_manager.clear_event_assigners()

    def load_event_overrides(self):
        self.event_manager.set_overrides(self.get_event_assignments())
        
    def set_deck_controller(self, deck_controller):
        """Internal function. Do not call it manually."""
        self.deck_controller = deck_controller
 
    def set_page(self, page):
        """Internal function. Do not call it manually."""
        self.page = page

    def get_input(self) -> "ControllerInput | None":
        # None when the identifier names an input this deck does not have.
        # DeckController.get_input then falls off the end of its search loop.
        return self.deck_controller.get_input(self.input_ident)

    def get_state(self) -> "ControllerInputState | None":
        i = self.get_input()
        if i is None: return None
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
        """The app calls this when the page can process action requests.

        Set the default image here rather than in the constructor.

        The threading contract runs this hook off the GTK main thread, because
        a page loads on a worker, a USB or a store thread. Do not build or
        touch a raw GTK object here, because that crashes the process. Use the
        GenerativeUI layer, which marshals itself to the main loop, or wrap the
        GTK work in GtkHelper.GtkHelper.run_on_main.
        """
        pass

    def on_update(self):
        """The app calls this when the action must redraw itself."""
        # The compatibility call below re-runs the whole on_ready body, so it
        # fires only after a ready completed. A caller that arrives while the
        # first on_ready still runs would start a second on_ready body beside
        # it, and a plugin allocates, subscribes and spawns backend processes
        # in on_ready. own_actions_update gates the app's own dispatch, and
        # this gate covers every other caller, a plugin that calls on_update()
        # on itself inside on_ready included.
        #
        # The call is skipped and never deferred. The running ready sequence
        # ends with its own on_update in Page._run_ready_callbacks, so no
        # redraw is lost, and a queued duplicate is the re-entry this removes.
        # After a completed ready the compatibility call fires per update.
        if not self.on_ready_finished:
            log.debug(f"{self.action_id}: on_update compat on_ready skipped, on_ready has not finished")
            return
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
        if input_state.state != self.state:
            return

        # Set this only when the code below opened media_path for the image. An
        # image a plugin supplies has no known source file to decode again, so
        # InputImage must upscale it instead of a failed re-open of
        # media_path.
        path_for_reopen = None
        if is_image(media_path) and image is None:
            with Image.open(media_path) as img:
                image = img.copy()
            path_for_reopen = media_path

        if is_svg(media_path) and image is None:
            image = gl.media_manager.generate_svg_thumbnail(media_path)

        controller_input = self.get_input()
        if controller_input is None:
            return

        # The write runs under the input's states lock, against a state object
        # resolved again inside that lock. A concurrent page load replaces
        # every state object through create_n_states, so a write to the object
        # resolved above strands this media on a dead state, and the key stays
        # blank until the action repaints. The image decode above stays outside
        # the lock.
        with controller_input._states_lock:
            input_state = controller_input.states.get(self.state)
            if input_state is None:
                return
            if input_state.state != self.state:
                return

            if image is not None:
                input_state.set_image(InputImage(
                    controller_input=controller_input,
                    image=image,
                    path=path_for_reopen,
                ), update=False)
                self._stamp_media_owner(input_state)

            elif is_video(media_path):
                # A local import. deck_controller/inputs.py imports ActionCore
                # at module level, so a top-level ControllerKey import here
                # closes a cycle. KeyGIF comes in at the same call site.
                from src.backend.DeckManagement.deck_controller.gif_pipeline import KeyGIF
                from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
                key_gif = None
                if os.path.splitext(media_path)[1].lower() == ".gif" and isinstance(controller_input, ControllerKey):
                    # A GIF on a key goes to KeyGIF, which matches the
                    # page-media loader ControllerKey.load_from_input_dict.
                    # KeyGIF keeps the RGBA alpha that the GIF demuxer of cv2
                    # drops, and it honors the per-frame delays. Keys alone
                    # take this route, because KeyGIF is a SingleKeyAsset. A
                    # dial and a touchscreen keep the InputVideo path below.
                    #
                    # KeyGIF decodes at once and raises on a corrupt or
                    # truncated GIF, where the detached cv2 builder of
                    # InputVideo fails soft. set_media must not raise into
                    # plugin code over bad media, so this falls back to the cv2
                    # path, as the GifBackground routes in DeckController do.
                    try:
                        key_gif = KeyGIF(
                            controller_key=controller_input,
                            gif_path=media_path,
                            fps=fps,
                            loop=loop
                        )
                    except Exception:
                        log.opt(exception=True).warning(
                            f"GIF decode failed in set_media, falling back to the opaque cv2 path: {media_path}")
                if key_gif is not None:
                    input_state.set_video(key_gif)
                else:
                    input_state.set_video(InputVideo(
                        controller_input=controller_input,
                        video_path=media_path,
                        fps=fps,
                        loop=loop
                    ))
                self._stamp_media_owner(input_state)

            else:
                input_state.set_image(None, update=False)

            # valign, halign and size are optional here, and ImageLayout
            # stores each one unchanged.
            input_state.layout_manager.set_action_layout(ImageLayout(
                valign=valign,
                halign=halign,
                size=size
            ), update=False)

        if update:
            controller_input.update()

    def _stamp_media_owner(self, input_state) -> None:
        # Record this action as the owner of the media it set, so
        # ControllerKey.load_from_input_dict restores that media across the
        # state wipe of create_n_states while this action object still drives
        # the key. Key states alone carry the attribute, so a dial state takes
        # no part in the restore.
        if hasattr(input_state, "media_owner_action"):
            input_state.media_owner_action = self

    def set_background_color(self, color: list[int] = None, update: bool = True):
        if color is None:
            color = [0, 0, 0, 0]

        self.raise_error_if_not_ready()

        if not self.get_is_present(): return

        if not self.has_background_control(): return

        if not self.on_ready_called:
            update = False

        state = self.get_state()
        if state is None or state.state != self.state: return

        state.background_manager.set_action_color(color)
        if update:
            controller_input = self.get_input()
            if controller_input is not None:
                controller_input.update()

    def show_error(self, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        try:
            state.show_error(duration=duration)
        except AttributeError as e:
            log.error(e)
            pass

    def hide_error(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        try:
            state.hide_error()
        except AttributeError:
            pass

    def show_overlay(self, image: Image.Image, duration: int = -1) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        try:
            state.show_overlay(image, duration=duration)
        except AttributeError:
            pass

    def hide_overlay(self) -> None:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return
        if self.get_is_multi_action(): return
        state = self.get_state()
        if state is None:
            return
        try:
            state.hide_overlay()
        except AttributeError:
            pass

    def set_label(self, text: str, position: str = "bottom", color: list[int]=None,
                  font_family: str=None, font_size=None, outline_width: int = None, outline_color: list[int] = None,
                  font_weight: int = None, font_style: str = None,
                  update: bool=True):
        self.raise_error_if_not_ready()

        if type(self.input_ident) not in [Input.Key, Input.Dial]:
            return
        
        state = self.get_state()
        if state is None:
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

        # Every field below is optional on KeyLabel. An unset field takes the
        # page or font default at compose time, and does not read as empty.
        key_label = KeyLabel(
            controller_input=state.controller_input,
            text=text,
            font_size=font_size,
            font_name=font_family,
            color=color,
            outline_width=outline_width,
            outline_color=outline_color,
            font_weight=font_weight,
            style=font_style
        )

        self.labels[position] = {
            "text": key_label.text,
            "color": key_label.color,
            "font-family": key_label.font_name,
            "font-size": key_label.font_size,
            "outline_width": key_label.outline_width,
            "outline_color": key_label.outline_color,
            "font-weight": key_label.font_weight,
            "font-style": key_label.style
        }

        state.label_manager.set_action_label(label=key_label, position=position, update=update)

    def set_top_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text=text, position="top", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def set_center_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text=text, position="center", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def set_bottom_label(self, text: str, color: list[int] = None,
                      font_family: str = None, font_size = None, outline_width: int = None, outline_color: list[int] = None,
                      font_weight: int = None, font_style: str = None,
                      update: bool = True):
        self.set_label(text=text, position="bottom", color=color, font_family=font_family, font_size=font_size,
                       outline_width=outline_width, outline_color=outline_color,
                       font_weight=font_weight, font_style=font_style, update=update)

    def on_labels_changed_in_ui(self):
        # TODO
        pass

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        return []
    
    def get_custom_config_area(self):
        return
    
    def get_settings(self) -> dict:
        # self.page.load()
        if self.page is None:
            return {}
        return self.page.get_action_settings(action_object=self)
    
    def set_settings(self, settings: dict):
        if self.page is None:
            return
        self.page.set_action_settings(action_object=self, settings=settings)

    def connect(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        gl.signal_manager.connect_signal(signal = signal, callback = callback)
        # Tracked, so the teardown can disconnect it. See clean_up.
        self._connected_signals.append((signal, callback))

    def get_own_key(self) -> "ControllerKey | None":
        # Upstream plugin-API surface, so this method stays. It resolves
        # through the identifier, as get_input() does, and returns None for an
        # action that does not sit on a key.
        if not isinstance(self.input_ident, Input.Key):
            return None
        # A Key identifier always resolves to a ControllerKey. get_input() is
        # declared over the whole input family, so narrow the type here.
        return cast("ControllerKey | None", self.deck_controller.get_input(self.input_ident))
    
    def get_is_multi_action(self) -> bool:
        self.raise_error_if_not_ready()

        if not self.get_is_present(): return False
        actions = self.page.action_objects.get(self.input_ident.input_type, {}).get(self.input_ident.json_identifier, [])
        return len(actions) > 1

    def get_asset_path(self, asset_name: str, subdirs: list[str] = None, asset_folder: str = "assets") -> str:
        """Return the path to a plugin asset.

        Args:
            asset_name (str): Name of the asset file
            subdirs (list[str], optional): Subdirectories. Defaults to [].
            asset_folder (str, optional): The asset folder. Defaults to "assets".

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
    
    def has_label_control(self, label_index) -> bool:
        #TODO: Might require performance improvements
        state = self.get_state()
        if state is None:
            return False
        return state.action_permission_manager.get_label_control_index(label_index) == self.get_own_action_index()

    def has_image_control(self):
        #TODO: Might require performance improvements
        image_control_index = self.get_state().action_permission_manager.get_image_control_index()
        return image_control_index == self.get_own_action_index()


        key_dict = self.input_ident.get_state_dict(self.page, self.state)

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
        media = self.input_ident.get_state_dict(self.page, self.state).get("media", {})
        return media.get("path", None) is not None
    
    def get_own_action_index(self) -> int | None:
        # There are two answers for no index. It returns -1 while the action
        # sits off the active page, and None while the action is absent from
        # this input's actions. None must stay, because a permission getter
        # compares it against an unset control-action entry, which is None
        # too. The annotation states both, and nothing normalizes them.
        if not self.get_is_present(): return -1
        actions = self.page.get_all_actions_for_input(self.input_ident, self.state)
        if self not in actions:
            return None
        return actions.index(self)

    # None is a valid value here. Input.EventFromStringName answers None for
    # the stored str(None), which maps that event to no assigner. Every event
    # key is present, so a caller iterates the map and skips None instead of a
    # probe for a missing key.
    def get_page_event_assignments(self) -> dict[InputEvent, InputEvent | None]:
        assignment: dict[InputEvent, InputEvent | None] = {}

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
    
    def get_generative_ui_objects(self) -> list["GenerativeUI"]:
        from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI

        objects = []
        for attr in dir(self):
            if isinstance(getattr(self, attr), GenerativeUI):
                objects.append(getattr(self, attr))

        return objects

    def add_generative_ui_object(self, generative_ui_object: "GenerativeUI"):
        self.generative_ui_objects.append(generative_ui_object)

    def remove_generative_ui_object(self, generative_ui_object: "GenerativeUI"):
        """Unregister a GenerativeUI element, such as a rebuilt config row.

        The action then stops retaining it for its own lifetime."""
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
        # A GenerativeUI widget builds on the first read of .widget, which
        # normally happens when the config opens. A call to load_initial_ui()
        # for every object would read .widget on every action's on_ready and
        # build every gen-ui object in the app, which ends the laziness.
        # get_value() reads the persisted value from the settings, so an
        # unbuilt object has nothing to sync. Reconcile only the widgets a
        # plugin built already, by a read of .widget at construction time.
        for generative_object in self.generative_ui_objects:
            if generative_object.is_built:
                generative_object.load_initial_ui()
    
    # Rpyc

    def start_server(self):
        if self.server is not None:
            log.warning("Server already running, skipping...")
            return
        self.server = ThreadedServer(self, hostname="localhost", port=0, protocol_config={"allow_public_attrs": True})
        threading.Thread(target=self.server.start, name="server_start", daemon=True).start()

    def on_disconnect(self, conn=None):
        # The rpyc disconnect hook. A dropped connection with a live process
        # orphans the backend, so the full teardown runs here too.
        self._release_backend_resources()
    
    def launch_backend(self, backend_path: str, venv_path: str = None, open_in_terminal: bool = False):
        from src.backend.PluginManager.PluginManager import build_backend_launch_command

        self.start_server()
        port = self.server.port

        # It validates the paths and returns argv, and not a shell string.
        command = build_backend_launch_command(backend_path, venv_path, port, open_in_terminal)

        log.info(f"Launching backend: {command}")
        # Cleared after the validation and before the spawn, so a relaunch
        # waits for the registration of the new backend instead of a return on
        # the registration of the previous one.
        self._backend_ready.clear()
        self.backend_process = subprocess.Popen(command, start_new_session=True)
        if gl.plugin_manager is not None:
            gl.plugin_manager.backend_processes.append(self.backend_process)

        self.wait_for_backend()

    def wait_for_backend(self, tries: int = 3):
        """Block until the backend registers, up to tries * 0.1 seconds.

        A plugin calls this with its own tries value, which stays a parameter.
        It is a timeout budget and not a poll count, because the registration
        wakes this call at once.
        """
        self._backend_ready.wait(timeout=tries * 0.1)

    def register_backend(self, port: int):
        """Internal method. Do not call it manually."""
        self.backend_connection = rpyc.connect("localhost", port, config={"allow_public_attrs": True})
        self.backend = self.backend_connection.root
        if gl.plugin_manager is not None:
            gl.plugin_manager.backends.append(self.backend_connection)
        # Only after the connection attributes hold their values. The caller
        # that wait_for_backend wakes reads self.backend at once.
        self._backend_ready.set()
        self.on_backend_ready()

    def on_backend_ready(self):
        pass

    def ping(self) -> bool:
        return True
    
    def on_removed_from_cache(self) -> None:
        """A notification hook for an action dropped from a live page or cache.

        A reload diff, a plugin uninstall, a removal in the sidebar or the
        config, and a cache eviction each drop an action. See
        docs/memory-footprint-plan.md. This hook only notifies. The framework
        always calls clean_up() right after it, even when a plugin overrides
        this method without super() and even when the override raises. A
        plugin must not call clean_up() itself from here, although a call does
        no harm, because clean_up() is idempotent."""
        pass

    def on_remove(self) -> None:
        """A notification hook for a removal through the action configurator.

        It keeps the contract of on_removed_from_cache(). The framework calls
        clean_up() whatever this override does."""
        pass

    @staticmethod
    def teardown(action, hook_name: str = "on_removed_from_cache") -> None:
        """Framework-owned teardown at a drop site.

        Call this, and not the hook alone, wherever an action leaves a live
        structure. It notifies through the named hook and then always calls
        clean_up(), so a plugin override that raises or omits super() cannot
        skip the cleanup. action can be a placeholder that is no ActionCore,
        such as NoActionHolderFound or ActionOutdated, and this ignores one,
        like the isinstance guards at the call sites."""
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
        """Framework teardown for a dropped action.

        A page reload, a plugin uninstall, a removal in the sidebar or the
        config, and a cache eviction each drop an action. A lock makes this
        idempotent, because an eviction and the rpyc on_disconnect path can
        call it from two threads at once.

        This runs on any thread, the main thread, the USB monitor and the media
        thread through a page eviction. Never call run_on_main() from in here,
        or from anything this method calls synchronously. GenerativeUI disposal
        is GTK work, so GLib.idle_add marshals it onto the main loop. The
        backend teardown goes to a worker thread, because a close of an rpyc
        server or connection can block on a call that needs the main loop and
        deadlock the UI.

        clean_up() flushes and cancels no work queued elsewhere with a strong
        reference to this action. An event callback such as on_key_down or
        on_tick, submitted to the deck's action executor, and a GLib idle
        dispatched just before the teardown, can still run after clean_up()
        returns. The executor cancels its futures at deck close alone. A plugin
        hook must therefore tolerate a cleaned-up action. get_is_present() is
        the recommended guard, and a settings read returns an empty dict once
        the page reference drops."""
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        # Disconnect the signal callbacks here, so the SignalManager stops
        # retaining this action.
        for signal, callback in self._connected_signals:
            try:
                gl.signal_manager.disconnect_signal(signal, callback)
            except Exception as e:
                log.error(f"Failed to disconnect signal {signal}: {e}")
        self._connected_signals.clear()

        # The snapshot and the clear are cheap list operations, and they run
        # here, so a caller reads an empty generative_ui_objects list as soon
        # as clean_up() returns. The widget teardown is GTK work for the main
        # loop, so it goes on a queue.
        gen_ui_snapshot = list(self.generative_ui_objects)
        self.generative_ui_objects.clear()
        if gen_ui_snapshot:
            GLib.idle_add(self._destroy_gen_ui_batch, gen_ui_snapshot)

        self._release_backend_resources()

    @staticmethod
    def _destroy_gen_ui_batch(snapshot: list["GenerativeUI"]) -> None:
        """Destroy each GenerativeUI object of the teardown snapshot.

        clean_up() queues this callback with GLib.idle_add. It runs on the GTK
        main loop, where the run_on_main() inside GenerativeUI.destroy() runs
        inline (main_loop.py), so nothing re-queues and nothing deadlocks."""
        for obj in snapshot:
            try:
                owner = obj.action_core
                if owner is not None and obj in owner.generative_ui_objects:
                    # A live action registered this object again since the
                    # snapshot, as a rebuilt row does. It has an owner, so
                    # leave it alone.
                    continue
                if getattr(obj, "_widget", None) is None:
                    # It built no widget, so there is nothing to unparent, and
                    # it left generative_ui_objects already.
                    continue
                obj.destroy()
            except Exception:
                log.opt(exception=True).error(f"Failed to destroy GenerativeUI object {obj!r}")

    def _release_backend_resources(self) -> None:
        """Detach and tear down the rpyc server, connection and process.

        It is idempotent and safe against a concurrent call from clean_up and
        from the rpyc on_disconnect hook, because close and terminate both
        tolerate a lost race."""
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
            name="action_backend_teardown",
            daemon=True,
        ).start()

    @staticmethod
    def _teardown_backend_resources(server, connection, process) -> None:
        # This runs on a worker thread. See clean_up. Each close and terminate
        # tolerates a failure, because a hung backend must not stop the app.
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
