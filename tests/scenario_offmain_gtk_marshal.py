"""
Integration scenario for framework GTK construction at plugin registration.

The store-install path runs a plugin's whole __init__ on the installer
thread. GTK4 is main-thread-only, so the ActionHolder default icon,
add_css_stylesheet and get_selector_icon all construct on the main thread,
and stay inline when the caller already runs on main. No GTK main loop runs,
so the scenario pumps the default GLib.MainContext itself.
"""
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

import src.backend.PluginManager.ActionHolder as action_holder_module
import src.backend.PluginManager.PluginBase as plugin_base_module
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase


def _pump_until_dead(thread: threading.Thread, timeout: float = 5.0) -> None:
    """Pumps the default context, which serves run_on_main's queued idles,
    until the worker thread finishes."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)
    thread.join(timeout=0.5)
    assert not thread.is_alive(), "worker did not finish while the context was pumped"


class _RecordingImage:
    """Gtk.Image stand-in that records the constructing thread."""

    threads: list[threading.Thread] = []

    def __init__(self, **kwargs):
        _RecordingImage.threads.append(threading.current_thread())
        self.kwargs = kwargs


class _RecordingCssProvider:
    """Gtk.CssProvider stand-in that records the constructing thread."""

    threads: list[threading.Thread] = []

    def __init__(self):
        _RecordingCssProvider.threads.append(threading.current_thread())

    def load_from_path(self, path):
        pass


class _RecordingStyleContext:
    add_threads: list[threading.Thread] = []

    @staticmethod
    def add_provider_for_display(display, provider, priority):
        _RecordingStyleContext.add_threads.append(threading.current_thread())


_gtk_shim = types.SimpleNamespace(
    Image=_RecordingImage,
    CssProvider=_RecordingCssProvider,
    StyleContext=_RecordingStyleContext,
    STYLE_PROVIDER_PRIORITY_APPLICATION=800,
)
_gdk_shim = types.SimpleNamespace(
    Display=types.SimpleNamespace(get_default=lambda: object()),
)


def _install_shims() -> None:
    # The code under test resolves Gtk and Gdk through its module globals at
    # call time, so swapping the names records the construction thread
    # without real widgets.
    action_holder_module.Gtk = _gtk_shim
    plugin_base_module.Gtk = _gtk_shim
    plugin_base_module.Gdk = _gdk_shim


def _make_holder() -> ActionHolder:
    # action_id is explicit, so plugin_base is never dereferenced. Only the
    # default-icon branch matters here.
    return ActionHolder(
        plugin_base=None,
        action_name="Probe",
        action_core=object(),
        action_id="test::probe",
    )


def check_action_holder_default_icon_marshals() -> None:
    _RecordingImage.threads.clear()
    box: dict = {}

    def target():
        try:
            box["holder"] = _make_holder()
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e

    worker = threading.Thread(target=target, name="fake-installer", daemon=True)
    worker.start()
    _pump_until_dead(worker)

    assert "exc" not in box, f"ActionHolder construction raised: {box['exc']!r}"
    assert len(_RecordingImage.threads) == 1, (
        f"expected exactly one default-icon construction, got {len(_RecordingImage.threads)}"
    )
    assert _RecordingImage.threads[0] is threading.main_thread(), (
        "ActionHolder built its default Gtk.Image on the installer thread -- "
        "GTK objects must only be constructed on the main thread"
    )
    assert isinstance(box["holder"].icon, _RecordingImage), "holder.icon is not the constructed image"
    print("PASS: ActionHolder default icon constructs on the main thread from a worker")


def check_add_css_stylesheet_marshals() -> None:
    _RecordingCssProvider.threads.clear()
    _RecordingStyleContext.add_threads.clear()

    # __init__ is bypassed because it needs a real plugin directory, and
    # add_css_stylesheet touches no instance state.
    plugin = PluginBase.__new__(PluginBase)
    box: dict = {}

    def target():
        try:
            plugin.add_css_stylesheet("/nonexistent/style.css")
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e

    worker = threading.Thread(target=target, name="fake-installer", daemon=True)
    worker.start()
    _pump_until_dead(worker)

    assert "exc" not in box, f"add_css_stylesheet raised: {box['exc']!r}"
    assert _RecordingCssProvider.threads == [threading.main_thread()], (
        "Gtk.CssProvider was constructed off the main thread -- GTK objects "
        "must only be constructed on the main thread"
    )
    assert _RecordingStyleContext.add_threads == [threading.main_thread()], (
        "style-context mutation ran off the main thread"
    )
    print("PASS: add_css_stylesheet runs its GTK work on the main thread from a worker")


def check_get_selector_icon_marshals() -> None:
    _RecordingImage.threads.clear()

    # __init__ is bypassed because get_selector_icon touches no state.
    plugin = PluginBase.__new__(PluginBase)
    box: dict = {}

    def target():
        try:
            box["icon"] = plugin.get_selector_icon()
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e

    worker = threading.Thread(target=target, name="fake-installer", daemon=True)
    worker.start()
    _pump_until_dead(worker)

    assert "exc" not in box, f"get_selector_icon raised: {box['exc']!r}"
    assert _RecordingImage.threads == [threading.main_thread()], (
        "get_selector_icon built its Gtk.Image off the main thread -- GTK "
        "objects must only be constructed on the main thread"
    )
    assert isinstance(box["icon"], _RecordingImage), "get_selector_icon did not return the constructed image"
    print("PASS: get_selector_icon constructs its Gtk.Image on the main thread from a worker")


def check_inline_on_main_thread() -> None:
    _RecordingImage.threads.clear()
    _RecordingCssProvider.threads.clear()

    # No pumping here. On the main thread all three run inline through
    # run_on_main's fast path, as the startup load path does.
    holder = _make_holder()
    assert _RecordingImage.threads == [threading.main_thread()]
    assert isinstance(holder.icon, _RecordingImage)

    plugin = PluginBase.__new__(PluginBase)
    plugin.add_css_stylesheet("/nonexistent/style.css")
    assert _RecordingCssProvider.threads == [threading.main_thread()]

    _RecordingImage.threads.clear()
    icon = plugin.get_selector_icon()
    assert _RecordingImage.threads == [threading.main_thread()]
    assert isinstance(icon, _RecordingImage)
    print("PASS: main-thread registration stays inline (startup path unchanged)")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_offmain_gtk_marshal")
    _install_shims()

    check_action_holder_default_icon_marshals()
    check_add_css_stylesheet_marshals()
    check_get_selector_icon_marshals()
    check_inline_on_main_thread()

    print("PASS: scenario_offmain_gtk_marshal")


if __name__ == "__main__":
    main()
