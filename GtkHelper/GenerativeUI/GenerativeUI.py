import functools
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar

from gi.repository import Gtk

from typing import TYPE_CHECKING

from loguru import logger as log


if TYPE_CHECKING:
    from src.backend.PluginManager.ActionCore import ActionCore

T = TypeVar("T")

class GenerativeUI[T](ABC):
    """
       Abstract base class for creating dynamic UI elements linked to an ActionCore instance.

       Attributes:
           _action_core (ActionCore): The action this UI element is associated with.
           _var_name (str): The key used to store the value in the action's settings.
           _default_value (T): The default value for this UI element.
           on_change (Callable[[Gtk.Widget, T, T], None]): Function called when the value changes.
           _widget (Gtk.Widget): The GTK widget representing the UI element.
           _can_reset (bool): Whether the UI element can be reset to its default value.
           _auto_add (bool): Whether the UI element is automatically added to the action.
       """
    _action_core: "ActionCore"
    _var_name: str # name of the key in the actions settings
    _default_value: T # default value of the key
    # Runs when the value changes. The first argument is the row widget that
    # the subclass built, which is a concrete Adw row type, or None while the
    # widget is unbuilt. See _handle_value_changed. The type is therefore Any
    # and not Gtk.Widget.
    on_change: Callable[[Any, T, T], None] | None
    _widget: Gtk.Widget | None # The actual widget of the UI Element; None until built and after destroy()
    _can_reset: bool
    _auto_add: bool
    _complex_var_name: bool

    # Classes that already logged the off-main forced-build note in
    # _ensure_built. The log holds one line per class, not one per instance.
    _logged_forced_build_classes: set[str] = set()

    def __init__(self, action_core: "ActionCore", var_name: str, default_value: T, can_reset: bool = True,
                 auto_add: bool = True, complex_var_name: bool = False,
                 on_change: Callable[[Any, T, T], None] | None = None,
                 build: Callable[[], None] | None = None):
        """
        Initializes the UI element. This does not build the widget. The
        first .widget access builds it, which usually happens when the config
        sidebar opens for this action. An action whose config sidebar never
        opens therefore pays for no Adw row tree. See _ensure_built for the
        build trigger and its off-main handling.

        Args:
            action_core (ActionCore): The action this UI element is associated with.
            var_name (str): The key used to store the value in the action's settings.
            default_value (T): The default value for this UI element.
            can_reset (bool, optional): Whether the UI element can be reset. Defaults to True.
            auto_add (bool, optional): Whether the UI element is automatically added to the action. Defaults to True.
            on_change (Callable[[Gtk.Widget, T, T], None], optional): Function called when the value changes. Defaults to None.
            build (Callable[[], None], optional): Builds self._widget (and any widget-only
                state the subclass needs). Called at most once, on first .widget access.
        """
        self._action_core = action_core
        self._var_name = var_name
        self._default_value = default_value
        self.on_change = on_change
        self._can_reset = can_reset
        self._auto_add = auto_add
        self._complex_var_name = complex_var_name
        self._widget = None
        self._built = False
        self._build_flag_lock = threading.Lock()
        self._build_fn = build

        # Register at once. load_initial_generative_ui and the teardown both
        # accept an object that has not built its widget, through is_built and
        # through the _widget is None skip in _destroy_gen_ui_batch.
        self._action_core.add_generative_ui_object(self)

    def _ensure_built(self):
        """Build the widget on the first access. Every .widget read may call it.

        An off-main access marshals through run_on_main, under its 30 s bound,
        and logs one note per class. A .widget read before the config opens
        forces an eager build and loses the laziness.
        """
        # Lock the flag transition only, never the build. run_on_main runs
        # build_fn on the main thread, so a worker that holds a lock across it
        # deadlocks as soon as build_fn re-enters .widget. The early flag also
        # stops that re-entrant access from recursing, because it sees _built
        # and falls through to self._widget.
        #
        # One known limitation stays. Between the flag flip here and the
        # marshalled build reaching the main loop, another main-thread reader
        # sees _built=True with self._widget still None, and gets None. A fix
        # needs a main-thread inline-build path and a build-executed latch.
        # Every reader in the tree either runs after the config opens, or
        # guards against None.
        with self._build_flag_lock:
            if self._built:
                return
            self._built = True
        build_fn = self._build_fn
        if build_fn is None:
            return
        if threading.current_thread() is not threading.main_thread():
            cls_name = type(self).__name__
            if cls_name not in GenerativeUI._logged_forced_build_classes:
                GenerativeUI._logged_forced_build_classes.add(cls_name)
                log.debug(
                    f"{cls_name}: gen-ui widget forced at construction; will become config-open-only"
                )
        from GtkHelper.GtkHelper import run_on_main
        try:
            run_on_main(build_fn)
        except BaseException:
            # A failed build must not latch the object into a built state
            # with no widget. Let a later access retry.
            with self._build_flag_lock:
                self._built = False
            raise

    @abstractmethod
    def connect_signals(self):
        """Connects signals for the UI element."""
        pass

    @abstractmethod
    def disconnect_signals(self):
        """Disconnects signals for the UI element."""
        pass

    @property
    def action_core(self):
        """Returns the associated ActionCore instance."""
        return self._action_core

    @property
    def var_name(self):
        """Returns the variable name used in settings."""
        return self._var_name

    @property
    def default_value(self):
        """Returns the default value of the UI element."""
        return self._default_value

    @property
    def widget(self):
        """Returns the GTK widget representing the UI element, building it on
        first access (see _ensure_built)."""
        self._ensure_built()
        # Back-reference so a container can recover the owning GenerativeUI object.
        if self._widget is not None:
            try:
                self._widget._generative_ui_owner = self
            except Exception:
                pass
        return self._widget

    @property
    def is_built(self) -> bool:
        """True once the widget has actually been constructed. Value-layer
        operations (get_value/set_value/settings sync) never need this;
        widget-sync code paths use it to skip work when there's no widget to
        sync yet, instead of forcing a build just to find that out."""
        return self._widget is not None

    @property
    def can_reset(self):
        """Returns whether the UI element can be reset."""
        return self._can_reset

    @property
    def auto_add(self):
        """Returns whether the UI element is automatically added to the action."""
        return self._auto_add

    @property
    def complex_var_name(self):
        """Returns the complex variable name used in settings."""
        return self._complex_var_name

    @staticmethod
    def signal_manager(func):
        """
        Decorator to manage signal connections by disconnecting and reconnecting signals around the function call.

        Args:
            func (Callable): The function to wrap.

        Returns:
            Callable: The wrapped function.
        """

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            from GtkHelper.GtkHelper import run_on_main

            def _run():
                self.disconnect_signals()
                try:
                    return func(self, *args, **kwargs)
                finally:
                    self.connect_signals()

            return run_on_main(_run)

        return wrapper

    @abstractmethod
    @signal_manager
    def set_ui_value(self, value: T):
        """
        Sets the UI element to the specified value.

        Args:
            value (T): The value to set in the UI.
        """
        pass

    def _handle_value_changed(self, new_value: T, update_settings: bool = True, trigger_callback: bool = True):
        """
        Handles changes in the UI element's value.

        Args:
            new_value (T): The new value of the UI element.
        """
        old_value = self.get_value()

        if update_settings:
            self.set_value(new_value)

        if trigger_callback and self.on_change:
            # Pass the raw widget reference, which is None while unbuilt.
            # This is a value-layer operation, and it must not force a build
            # for a widget that the callback may never read.
            self.on_change(self._widget, new_value, old_value)

    def update_value_in_ui(self):
        """Updates the UI element with the current value from settings."""
        value = self.get_value()
        self.set_ui_value(value)

    def reset_value(self):
        """Reset the value to its default.

        It syncs the widget only when the widget exists. An unbuilt row has
        nothing to sync, so a reset must not force a build.
        """
        self._handle_value_changed(self._default_value)
        if self._widget is not None:
            self.update_value_in_ui()

    def resolve_var_name(self):
        keys = [self.var_name]

        if self.complex_var_name:
            keys = self.var_name.split('.')

        return keys

    def set_value(self, value: T):
        """
        Sets the value in the action's settings.

        Args:
            value (T): The value to set.
        """
        # A local annotation, not a cast. ActionCore.get_settings declares a
        # return type of dir, a typo for dict, so this file cannot use it.
        settings: dict = self._action_core.get_settings()

        keys = self.resolve_var_name()

        d = settings

        for key in keys[:-1]:
            if key not in d:
                d[key] = {}
            d = d[key]

        d[keys[-1]] = value

        self._action_core.set_settings(settings)

    def get_value(self, fallback: T = None) -> T:
        """
        Retrieves the value from the action's settings.

        Args:
            fallback (T, optional): The fallback value if the key is not found. Defaults to None.

        Returns:
            T: The retrieved value.
        """
        settings: dict = self._action_core.get_settings()

        keys = self.resolve_var_name()

        d: Any = settings
        for key in keys:
            if not isinstance(d, dict) or key not in d:
                return fallback if fallback is not None else self._default_value
            d = d[key]

        return d

    def load_initial_ui(self):
        """Loads the initial UI state based on the stored value."""
        value = self.get_value()
        self.set_ui_value(value)
        self._handle_value_changed(value, False)

    def load_ui_value(self):
        """Loads the UI element with the stored value."""
        value = self.get_value()
        self.set_ui_value(value)

    def get_translation(self, key: str, fallback: str = None):
        """
        Retrieves a translated string for the given key.

        Args:
            key (str): The translation key.
            fallback (str, optional): The fallback text if translation is not found.

        Returns:
            str: The translated string.
        """
        return self._action_core.get_translation(key, fallback) if key else ""

    def unparent(self):
        """Removes the UI element from its parent widget if it has one. A
        never-built widget has no parent to remove, so this is a no-op that
        does not force a build."""
        from GtkHelper.GtkHelper import run_on_main

        def _do():
            widget = self._widget
            if widget is not None and widget.get_parent():
                widget.unparent()
        run_on_main(_do)

    def destroy(self):
        """Disconnect the signals, unparent the widget, and unregister.

        A second call does nothing. Never call run_dispose() on the widget,
        because a dispose of a live Adw composite, such as a ComboRow or an
        ExpanderRow, logs Gtk-CRITICAL messages.
        """
        from GtkHelper.GtkHelper import run_on_main

        def _do():
            # A widget that never built has nothing to disconnect and nothing
            # to unparent, and a .widget read here forces the build that this
            # class avoids.
            if self._widget is not None:
                try:
                    self.disconnect_signals()
                except Exception:
                    pass
            self._action_core.remove_generative_ui_object(self)
            widget = self._widget
            if widget is not None and widget.get_parent() is not None:
                widget.unparent()
            self._widget = None
        run_on_main(_do)

    def _create_reset_button(self):
        """Creates a reset button for the UI element."""
        button = Gtk.Button(icon_name="edit-undo-symbolic", vexpand=True, css_classes=["no-rounded-corners"],
                            overflow=Gtk.Overflow.HIDDEN)
        button.connect("clicked", lambda _: self.reset_value())
        return button

    def _get_suffix_box(self):
        """
        Retrieves the suffix box widget from the UI element.

        Returns:
            Gtk.Widget: The suffix box widget.
        """
        return self.widget.get_first_child().get_last_child()

    def _handle_reset_button_creation(self):
        """
        Handles the creation and addition of the reset button to the UI element.
        """

        if not self.can_reset:
            return

        self.widget.add_css_class("gen-ui-row")
        self.widget.set_overflow(Gtk.Overflow.HIDDEN)
        self.widget.get_child().add_css_class("gen-ui-box")
        self.widget.get_child().set_overflow(Gtk.Overflow.HIDDEN)

        suffix_box = self._get_suffix_box()
        if suffix_box:
            suffix_box.add_css_class("no-margin")

        if self._can_reset:
            self.widget.add_suffix(self._create_reset_button())