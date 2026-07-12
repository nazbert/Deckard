import os
import signal
import importlib
import sys
from loguru import logger as log
import threading

from gi.repository import GLib

# Import own modules
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from streamcontroller_plugin_tools import BackendBase

import globals as gl


def terminate_backend_process(process, escalate: bool = True) -> None:
    """SIGTERM a launched backend's process group (it leads its own session). If
    escalate, wait briefly and SIGKILL if it doesn't exit, then reap it so it
    doesn't linger as a zombie. Pass escalate=False on app-quit -- os._exit reaps
    the whole tree, so we don't want to wait per backend."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except Exception:
            pass
    if not escalate:
        return
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass

class PluginManager:
    action_index = {}
    def __init__(self):
        self.initialized_plugin_classes = list[PluginBase]()
        self.backends:list[BackendBase] = []
        # subprocess.Popen handles for launched backends, terminated on teardown.
        self.backend_processes: list = []
        # Set by the first warm_up_plugins() call (App.on_activate). Once
        # true, load_plugins() re-runs the warm-up so hot-installed plugins
        # (store installs call load_plugins long after activation) get their
        # on_app_ready too; the per-plugin fired marker keeps every hook
        # at-most-once.
        self._app_ready: bool = False
        # Plugins that failed to load, keyed by their folder name under
        # PLUGIN_DIR, with a short human-readable reason (the full traceback
        # goes to the logs). Surfaced in the UI (startup toast + the Add
        # Action dialog's empty state) so a broken plugin never fails
        # silently. Entries are pruned when the folder disappears or the
        # plugin later registers successfully.
        #
        # Cross-thread: a store install re-runs load_plugins()/init_plugins()
        # on a background thread (StoreBackend.install_plugin), which rebuilds
        # and writes this dict, while the GTK main thread reads it via
        # get_load_health() (Add-Action empty state). _load_errors_lock keeps
        # the rebuild atomic against those reads -- a plain dict is GIL-safe
        # today but the mid-rebuild prune could otherwise expose a half-built
        # dict on a future Python.
        self.load_errors: dict[str, str] = {}
        self._load_errors_lock = threading.Lock()

    def terminate_all_backends(self) -> None:
        """Terminate every launched backend child process; called on app quit."""
        for process in list(self.backend_processes):
            terminate_backend_process(process, escalate=False)
        self.backend_processes.clear()

    def warm_up_plugins(self) -> None:
        """Eagerly initialize plugin backends without blocking the caller
        (issue #117).

        Invokes every registered plugin's not-yet-fired on_app_ready() hook
        on a single background daemon thread, one plugin at a time, each
        call exception-isolated; each plugin's hook fires at most once per
        process (per-instance fired marker). This is the supported
        eager-init point for backend launches: in background/autostart mode
        (-b) no config UI is ever opened, and if no deck was enumerable at
        startup no page load fires action on_ready either -- so a
        lazily-launched backend would otherwise stay down until the first
        user interaction that happens to force it, leaving the first
        hardware presses inert. Backend launches spawn subprocesses, so
        this must never run on (or block) the GTK main thread.

        Called from App.on_activate at startup, and again from
        load_plugins() for plugins hot-installed after activation.
        """
        self._app_ready = True
        threading.Thread(
            target=self._warm_up_plugins,
            name="plugin_warm_up",
            daemon=True,
        ).start()

    def _warm_up_plugins(self) -> None:
        for plugin_id, plugin in list(PluginBase.plugins.items()):
            plugin_base = plugin.get("object")
            if plugin_base is None:
                continue
            # At-most-once per plugin instance: startup warm-up and the
            # late-load warm-ups (store installs) overlap over the same dict.
            if getattr(plugin_base, "_on_app_ready_fired", False):
                continue
            plugin_base._on_app_ready_fired = True
            try:
                plugin_base.on_app_ready()
            except Exception as e:
                log.error(f"Plugin {plugin_id}: on_app_ready failed: {e}")

    def load_plugins(self, show_notification: bool = False):
        # get all folders in plugins folder
        os.makedirs(gl.PLUGIN_DIR, exist_ok=True)
        try:
            folders = os.listdir(gl.PLUGIN_DIR)
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not read the plugin directory {gl.PLUGIN_DIR} -- no plugins will be loaded"
            )
            folders = []

        # Drop stale errors for plugins that no longer exist on disk (uninstalled).
        with self._load_errors_lock:
            self.load_errors = {folder: error for folder, error in self.load_errors.items() if folder in folders}

        for folder in folders:
            if folder.startswith(".") or not os.path.isdir(os.path.join(gl.PLUGIN_DIR, folder)):
                # Stray files and hidden directories in the plugin dir are not plugins.
                continue
            # Import main module
            import_string = f"plugins.{folder}.main"
            if import_string not in sys.modules.keys():
                # Import module only if it's not already imported
                try:
                    importlib.import_module(import_string)
                except Exception as e:
                    log.opt(exception=e).error(f"Error importing plugin {folder}: {e}")
                    with self._load_errors_lock:
                        self.load_errors[folder] = f"import failed: {e}"

        # Get all classes inheriting from PluginBase and generate objects for them
        self.init_plugins()

        # Hot-installed plugins (store installs re-run load_plugins after
        # startup) must get their on_app_ready like startup-loaded ones --
        # the on_activate warm-up has already come and gone by then (#117
        # review round 1). No-op for already-warmed plugins.
        if self._app_ready:
            self.warm_up_plugins()

        if show_notification:
            self.show_n_disabled_plugins_notification()
            self.show_load_errors_notification()

    def show_n_disabled_plugins_notification(self):
        n_deactivated_plugins = len(PluginBase.disabled_plugins)
        if n_deactivated_plugins == 0:
            return
        
        body = f"{n_deactivated_plugins} plugins have been disabled because they are no longer compatible with the current app version"
        if n_deactivated_plugins == 1:
            body = f"{n_deactivated_plugins} plugin has been disabled because it is no longer compatible with the current app version"
        
        call = lambda: gl.app.send_notification(
            "dialog-information-symbolic",
            "Plugins",
            body,
            button=("Update All", "app.update-all-assets", None)
        )
        if gl.app is None:
            gl.app_loading_finished_tasks.append(call)
        else:
            call()

    def show_load_errors_notification(self):
        """Surfaces plugin load failures as an in-app error toast. Safe to
        call from any thread and at any point during startup: before the app
        exists the toast is deferred via gl.app_loading_finished_tasks (which
        on_activate drains on the main thread once the window is up),
        afterwards it is dispatched through GLib.idle_add."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        if n_failed == 0:
            return

        if n_failed == 1:
            body = "1 plugin failed to load -- check the logs for details"
        else:
            body = f"{n_failed} plugins failed to load -- check the logs for details"

        def call():
            main_win = getattr(gl.app, "main_win", None) if gl.app is not None else None
            if main_win is not None:
                main_win.show_error_toast(body)

        if gl.app is None:
            gl.app_loading_finished_tasks.append(call)
        else:
            GLib.idle_add(call)

    @staticmethod
    def _get_plugin_folder_from_subclass(subclass) -> str:
        """Maps a PluginBase subclass back to its folder name under
        PLUGIN_DIR (plugins.<folder>.main -> <folder>) for load_errors
        bookkeeping."""
        module = getattr(subclass, "__module__", "") or ""
        parts = module.split(".")
        if len(parts) >= 2 and parts[0] == "plugins":
            return parts[1]
        return module or str(subclass)

    @staticmethod
    def _is_plugin_disabled(plugin_base: PluginBase) -> bool:
        return any(entry.get("object") is plugin_base for entry in PluginBase.disabled_plugins.values())

    def init_plugins(self):
        subclasses = PluginBase.__subclasses__()
        for subclass in subclasses:
            if subclass in self.initialized_plugin_classes:
                log.info(f"Skipping {subclass} because it's already initialized")
                continue
            folder = self._get_plugin_folder_from_subclass(subclass)
            try:
                obj = subclass()
            except Exception as e:
                log.opt(exception=e).error(f"Error initializing plugin {subclass} (folder: {folder}): {e}. Skipping...")
                with self._load_errors_lock:
                    self.load_errors[folder] = f"crashed during initialization: {e}"
                continue
            self.initialized_plugin_classes.append(subclass)

            if getattr(obj, "registered", False):
                # A previously recorded failure for this folder is obsolete.
                with self._load_errors_lock:
                    self.load_errors.pop(folder, None)
            elif not self._is_plugin_disabled(obj):
                # register() bailed out (invalid manifest, duplicate name, ...)
                # without even disabling the plugin -- without this record the
                # plugin would vanish without any user-visible trace.
                log.error(
                    f"Plugin {subclass} (folder: {folder}) initialized but never registered successfully "
                    f"-- its actions will not be available. See the errors above for the reason."
                )
                with self._load_errors_lock:
                    self.load_errors[folder] = "did not register (invalid or incomplete manifest?)"

    def generate_action_index(self):
        self.action_index.clear()
        plugins = self.get_plugins()
        for plugin in plugins.values():
            plugin_base = plugin["object"]
            self.action_index.update(plugin_base.action_holders)

    def get_plugins(self, include_disabled: bool = False) -> dict:
        # Copy: updating PluginBase.plugins (a class attribute) in place would
        # permanently merge the disabled plugins into the enabled registry --
        # get_plugin_by_id() defaults to include_disabled=True and runs on
        # every page-load action resolution, so the very first hit would leak
        # every disabled plugin into the action index and the action chooser.
        plugins = dict(PluginBase.plugins)

        if include_disabled:
            plugins.update(PluginBase.disabled_plugins)

        return plugins
    
    def get_actions_for_plugin_id(self, plugin_id: str):
        return PluginBase.plugins[plugin_id]["object"].ACTIONS
    
    def get_action_holder_from_id(self, action_id: str) -> ActionHolder:
        """
        Example string: dev_core447_MediaPlugin::Pause
        """
        try:
            return self.action_index[action_id]
        except KeyError:
            log.warning(f"Requested action {action_id} not found, skipping...")
            return None
            
    def get_plugin_by_id(self, plugin_id:str, include_disabled: bool = True) -> PluginBase:
        return self.get_plugins(include_disabled).get(plugin_id, {}).get("object", None)
            
    def remove_plugin_from_list(self, plugin_base: PluginBase):
        del PluginBase.plugins[plugin_base.plugin_id]

    def get_plugin_id_from_action_id(self, action_id: str) -> str:
        if action_id is None:
            return
        
        return action_id.split("::")[0]
    
    def get_load_health(self) -> tuple[int, int]:
        """Returns (n_failed, n_disabled) -- how many plugins failed to load
        and how many are disabled (version-gated). Used by the UI (GTK main
        thread) to explain an empty action list instead of showing a blank
        page; the lock snapshots load_errors against a concurrent store-install
        reload rebuilding it on a background thread."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        return n_failed, len(PluginBase.disabled_plugins)

    def get_is_plugin_out_of_date(self, plugin_id: str) -> bool:
        plugin = PluginBase.disabled_plugins.get(plugin_id)
        if plugin is None:
            # Not installed
            return False
        
        reason = PluginBase.disabled_plugins[plugin_id].get("reason")
        return reason == "plugin-out-of-date"