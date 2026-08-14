import os
import signal
import importlib
import shlex
import sys
from loguru import logger as log
import threading

from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from streamcontroller_plugin_tools import BackendBase

import globals as gl
from src.backend import startup_queue


def terminate_backend_process(process, escalate: bool = True) -> None:
    """Send SIGTERM to the process group of a launched backend.

    The backend leads its own session. With escalate, this waits, sends SIGKILL
    to a process that stays, and reaps it. Pass escalate=False at app quit,
    where os._exit reaps the whole tree."""
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


def build_backend_launch_command(backend_path: str, venv_path: str | None, port: int,
                                 open_in_terminal: bool = False) -> list[str]:
    """Build the argv that launches a plugin or action backend.

    ActionCore.launch_backend and PluginBase.launch_backend share this, so the
    path validation covers both and the two cannot drift.

    Raises:
        ValueError: When a given venv_path is absent or has no usable
            interpreter, or when backend_path is None or absent.
    """
    # It returns an argv list and never a shell string. The shell splits a
    # backend or venv path that holds a space into separate words, and it
    # executes the metacharacters in that path.
    if venv_path is not None:
        if not os.path.exists(venv_path):
            raise ValueError(f"Venv path does not exist: {venv_path}")
    # One gate covers both a None path and an absent path. A gate on None
    # alone lets os.path.exists raise TypeError, and it passes an absent path
    # to Popen.
    if backend_path is None or not os.path.exists(backend_path):
        raise ValueError(f"Backend path does not exist: {backend_path}")

    if venv_path is not None:
        # The interpreter is the venv's own python. A python3 from PATH is the
        # system python on a native install, which carries no rpyc, so the
        # backend dies at import. A run of {venv}/bin/python resolves the
        # imports as a source of {venv}/bin/activate does, because venv.create()
        # builds a plugin venv without the system site packages. The activation
        # also exports VIRTUAL_ENV and prepends {venv}/bin to PATH, which this
        # omits, and only a backend that runs a console script of its own venv
        # notices that.
        interpreter = os.path.join(venv_path, "bin", "python")
        # bin/python is a symlink to the interpreter the venv was built
        # against, and a python upgrade under a native install leaves that
        # symlink dangling. exists() follows the link, so this catches it. The
        # check here gives the caller the documented ValueError with a useful
        # message, instead of a bare FileNotFoundError out of Popen.
        if not os.path.exists(interpreter):
            raise ValueError(f"Venv has no usable interpreter: {interpreter}")
    else:
        interpreter = sys.executable

    if not open_in_terminal:
        return [interpreter, backend_path, f"--port={port}"]

    # A debug affordance runs the backend in a terminal that stays open after
    # the backend exits, through exec $SHELL, so its output survives a crash.
    # The paths arrive as bash positional parameters, so bash interpolates
    # nothing in them.
    #
    # DECKARD_TERMINAL holds the whole terminal command prefix and not the
    # binary alone, because no flag for "run this command" works everywhere.
    # gnome-terminal and its family take --, konsole, alacritty, xterm and
    # xfce4-terminal take -e, and kitty takes the command as a bare positional.
    # A split of the whole variable expresses each of those, such as
    # "konsole -e", "alacritty -e" and "kitty". A hardcoded double dash after
    # the binary works for one family, and for the rest the terminal prints its
    # usage and exits, so the backend never registers.
    terminal = shlex.split(os.environ.get("DECKARD_TERMINAL", "")) or ["gnome-terminal", "--"]
    return [*terminal, "bash", "-c", '"$1" "$2" --port="$3"; exec $SHELL',
            "deckard-backend", interpreter, backend_path, str(port)]


class PluginManager:
    action_index: dict[str, ActionHolder] = {}
    def __init__(self):
        self.initialized_plugin_classes = list[PluginBase]()
        self.backends:list[BackendBase] = []
        # The subprocess.Popen handles of the launched backends. The teardown
        # terminates each one.
        self.backend_processes: list = []
        # The first warm_up_plugins() call, from App.on_activate, sets this.
        # After that, load_plugins() runs the warm-up again, so a plugin
        # installed later gets its on_app_ready too. A store install calls
        # load_plugins long after the activation. The fired marker per plugin
        # keeps every hook to one call.
        self._app_ready: bool = False
        # The plugins that failed to load, keyed by their folder name under
        # PLUGIN_DIR, each with a short reason for a reader. The full traceback
        # goes to the logs. The UI shows these in the startup toast and in the
        # empty state of the Add Action dialog, so a broken plugin never fails
        # in silence. An entry is pruned when its folder disappears, or when
        # the plugin registers later.
        #
        # Two threads reach this dict. A store install runs load_plugins() and
        # init_plugins() again on a background thread, from
        # StoreBackend.install_plugin, and that rebuilds and writes the dict.
        # The GTK main thread reads it through get_load_health() for the
        # Add-Action empty state. _load_errors_lock keeps the rebuild atomic
        # against those reads. A plain dict is GIL-safe today, and the prune
        # inside a rebuild could expose a half-built dict on a later Python.
        self.load_errors: dict[str, str] = {}
        self._load_errors_lock = threading.Lock()

    def terminate_all_backends(self) -> None:
        """Terminate every launched backend child process. Called at app quit."""
        for process in list(self.backend_processes):
            terminate_backend_process(process, escalate=False)
        self.backend_processes.clear()

    def warm_up_plugins(self) -> None:
        """Initialize the plugin backends early, without a block on the caller.

        It calls the on_app_ready() hook of every registered plugin that has
        not fired one yet, on one background daemon thread, one plugin at a
        time, each isolated from the exceptions of the rest.
        """
        # This is the supported point for an early backend launch. Background
        # mode with -b opens no config UI, and without an enumerable deck at
        # startup no page load fires an action on_ready, so a lazily launched
        # backend would stay down until some user interaction forces it. A
        # backend launch spawns a subprocess, so this must never run on the GTK
        # main thread or block it.
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
            # One call per plugin instance. The startup warm-up and the
            # warm-ups of a later load, after a store install, share this
            # dict.
            if getattr(plugin_base, "_on_app_ready_fired", False):
                continue
            plugin_base._on_app_ready_fired = True
            try:
                plugin_base.on_app_ready()
            except Exception as e:
                log.error(f"Plugin {plugin_id}: on_app_ready failed: {e}")

    def load_plugins(self, show_notification: bool = False):
        os.makedirs(gl.PLUGIN_DIR, exist_ok=True)
        try:
            folders = os.listdir(gl.PLUGIN_DIR)
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not read the plugin directory {gl.PLUGIN_DIR} -- no plugins will be loaded"
            )
            folders = []

        # Drop the stale errors of the plugins an uninstall removed.
        with self._load_errors_lock:
            self.load_errors = {folder: error for folder, error in self.load_errors.items() if folder in folders}

        for folder in folders:
            if folder.startswith(".") or not os.path.isdir(os.path.join(gl.PLUGIN_DIR, folder)):
                # A stray file and a hidden directory are no plugin.
                continue
            if "." in folder:
                # A dot makes the import below impossible. The import string
                # plugins.<folder>.main reads every dot as a package boundary,
                # so a timestamped backup directory raises ModuleNotFoundError
                # and adds a false toast entry at every startup. This is no
                # plugin failure, so it warns without a traceback and stays out
                # of load_errors. The test covers a dot alone and not
                # isidentifier(), because importlib imports a name that starts
                # with a dash or a digit, and such a name can be a real
                # plugin.
                log.warning(
                    f"Skipping plugin directory '{folder}': dots in the name make "
                    f"it unimportable as a Python module -- rename it to load it "
                    f"as a plugin, or ignore this if it is a backup"
                )
                continue
            import_string = f"plugins.{folder}.main"
            if import_string not in sys.modules.keys():
                try:
                    importlib.import_module(import_string)
                except Exception as e:
                    log.opt(exception=e).error(f"Error importing plugin {folder}: {e}")
                    with self._load_errors_lock:
                        self.load_errors[folder] = f"import failed: {e}"

        # Build an object for every class that inherits from PluginBase.
        self.init_plugins()

        # A plugin installed after startup must get its on_app_ready, like a
        # plugin loaded at startup. A store install runs load_plugins again,
        # and the warm-up of on_activate ran long before. This does nothing for
        # a plugin that is warm already.
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
        # The plugin load calls this, which on the boot path runs before the
        # app exists. The queue answers whether this thread delivers now, or
        # the drain in App.on_activate does. See src/backend/startup_queue.py.
        if startup_queue.get().when_app_ready(call):
            call()

    def show_load_errors_notification(self):
        """Show the plugin load failures to the user.

        Any thread can call this at any point during startup, because gl.notify
        defers the message during startup and marshals it to the main
        thread."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        if n_failed == 0:
            return

        if n_failed == 1:
            body = "1 plugin failed to load -- check the logs for details"
        else:
            body = f"{n_failed} plugins failed to load -- check the logs for details"

        gl.notify.error(body, title="Plugins")

    @staticmethod
    def _plugin_folder_of(subclass) -> str:
        """Map a PluginBase subclass back to its folder name under PLUGIN_DIR.

        The module plugins.<folder>.main gives <folder>, which load_errors
        keys by."""
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
            folder = self._plugin_folder_of(subclass)
            try:
                obj = subclass()
            except Exception as e:
                log.opt(exception=e).error(f"Error initializing plugin {subclass} (folder: {folder}): {e}. Skipping...")
                with self._load_errors_lock:
                    self.load_errors[folder] = f"crashed during initialization: {e}"
                continue
            self.initialized_plugin_classes.append(subclass)

            if getattr(obj, "registered", False):
                # A failure recorded earlier for this folder is stale.
                with self._load_errors_lock:
                    self.load_errors.pop(folder, None)
            elif not self._is_plugin_disabled(obj):
                # register() stopped over an invalid manifest or a duplicate
                # name, and it disabled no plugin. Without this record the
                # plugin vanishes with no trace for the user.
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
        # A copy. An in-place update of PluginBase.plugins, a class attribute,
        # merges the disabled plugins into the enabled registry for good.
        # get_plugin_by_id() defaults to include_disabled=True and runs for
        # every action a page load resolves, so the first call would leak every
        # disabled plugin into the action index and the action chooser.
        plugins = dict(PluginBase.plugins)

        if include_disabled:
            plugins.update(PluginBase.disabled_plugins)

        return plugins
    
    def get_actions_for_plugin_id(self, plugin_id: str):
        return PluginBase.plugins[plugin_id]["object"].ACTIONS
    
    def get_action_holder_from_id(self, action_id: str) -> ActionHolder | None:
        """Example string: dev_core447_MediaPlugin::Pause"""
        try:
            return self.action_index[action_id]
        except KeyError:
            log.warning(f"Requested action {action_id} not found, skipping...")
            return None
            
    def get_plugin_by_id(self, plugin_id:str, include_disabled: bool = True) -> PluginBase | None:
        return self.get_plugins(include_disabled).get(plugin_id, {}).get("object", None)
            
    def remove_plugin_from_list(self, plugin_base: PluginBase):
        # A plugin can live in either registry. A version gate puts a plugin
        # in disabled_plugins alone, and get_plugin_by_id hands it out too,
        # because include_disabled defaults to True. A del on
        # PluginBase.plugins raises KeyError for such a plugin and aborts
        # uninstall_plugin in the middle, which keeps the registry entry and
        # skips the sys.modules purge. An update of a disabled plugin then
        # keeps serving the old code from the module cache.
        PluginBase.plugins.pop(plugin_base.plugin_id, None)
        PluginBase.disabled_plugins.pop(plugin_base.plugin_id, None)

    def get_plugin_id_from_action_id(self, action_id: str) -> str:
        if action_id is None:
            return
        
        return action_id.split("::")[0]
    
    def get_load_health(self) -> tuple[int, int]:
        """Return the count of failed plugins and the count of disabled ones.

        A version gate disables a plugin. The UI reads this on the GTK main
        thread and explains an empty action list with it, instead of a blank
        page. The lock snapshots load_errors against a concurrent store-install
        reload, which rebuilds it on a background thread."""
        with self._load_errors_lock:
            n_failed = len(self.load_errors)
        return n_failed, len(PluginBase.disabled_plugins)

    def get_is_plugin_out_of_date(self, plugin_id: str) -> bool:
        plugin = PluginBase.disabled_plugins.get(plugin_id)
        if plugin is None:
            # The plugin is not installed.
            return False
        
        reason = PluginBase.disabled_plugins[plugin_id].get("reason")
        return reason == "plugin-out-of-date"