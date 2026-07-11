import os
import signal
import importlib
import sys
from loguru import logger as log
import threading

# Import own modules
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.DeckManagement.HelperMethods import get_last_dir
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

    def terminate_all_backends(self) -> None:
        """Terminate every launched backend child process; called on app quit."""
        for process in list(self.backend_processes):
            terminate_backend_process(process, escalate=False)
        self.backend_processes.clear()

    def warm_up_plugins(self) -> None:
        """Eagerly initialize plugin backends without blocking the caller
        (issue #117).

        Invokes every registered plugin's on_app_ready() hook on a single
        background daemon thread, one plugin at a time, each call
        exception-isolated. This is the supported eager-init point for
        backend launches: in background/autostart mode (-b) no config UI is
        ever opened, and if no deck was enumerable at startup no page load
        fires action on_ready either -- so a lazily-launched backend would
        otherwise stay down until the first user interaction that happens to
        force it, leaving the first hardware presses inert. Backend launches
        spawn subprocesses, so this must never run on (or block) the GTK
        main thread.
        """
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
            try:
                plugin_base.on_app_ready()
            except Exception as e:
                log.error(f"Plugin {plugin_id}: on_app_ready failed: {e}")

    def load_plugins(self, show_notification: bool = False):
        # get all folders in plugins folder
        if not os.path.exists(gl.PLUGIN_DIR):
            os.mkdir(gl.PLUGIN_DIR)
        folders = os.listdir(gl.PLUGIN_DIR)
        for folder in folders:
            # Import main module
            import_string = f"plugins.{folder}.main"
            if import_string not in sys.modules.keys():
                # Import module only if it's not already imported
                try:
                    importlib.import_module(f"plugins.{folder}.main")
                except Exception as e:
                    log.error(f"Error importing plugin {folder}: {e}")

        # Get all classes inheriting from PluginBase and generate objects for them
        self.init_plugins()

        if show_notification:
            self.show_n_disabled_plugins_notification()

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

    def init_plugins(self):
        subclasses = PluginBase.__subclasses__()
        for subclass in subclasses:
            if subclass in self.initialized_plugin_classes:
                log.info(f"Skipping {subclass} because it's already initialized")
                continue
            try:
                obj = subclass()
            except Exception as e:
                log.error(f"Error initializing plugin {subclass}: {e}. Skipping...")
                continue
            self.initialized_plugin_classes.append(subclass)

    def generate_action_index(self):
        self.action_index.clear()
        plugins = self.get_plugins()
        for plugin in plugins.values():
            plugin_base = plugin["object"]
            self.action_index.update(plugin_base.action_holders)

        return
        plugins = self.get_plugins()
        for plugin in plugins.keys():
            if plugin in self.action_index.keys():
                continue
            for action_id in plugins[plugin]["object"].ACTIONS.keys():
                if action_id is None:
                    log.warning(f"Plugin {plugin} has an action with id None, skipping...")
                    continue

                path = plugins[plugin]["folder-path"]
                # Remove everything except the last folder
                path = get_last_dir(path)
                self.action_index[action_id] = plugins[plugin]["object"].ACTIONS[action_id]

    def get_plugins(self, include_disabled: bool = False) -> list[PluginBase]:
        plugins = PluginBase.plugins

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
    
    def get_is_plugin_out_of_date(self, plugin_id: str) -> bool:
        plugin = PluginBase.disabled_plugins.get(plugin_id)
        if plugin is None:
            # Not installed
            return False
        
        reason = PluginBase.disabled_plugins[plugin_id].get("reason")
        return reason == "plugin-out-of-date"