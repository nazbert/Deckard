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

import gi
from gi.repository import GLib

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

import subprocess
import shlex
from typing import Any
from loguru import logger as log

import appinfo

import globals as gl

from src.windows.Permissions.FlatpakPermissionRequest import FlatpakPermissionRequestWindow


class FlatpakPermissionManager:
    def __init__(self):
        self.portal = Xdp.Portal.new()
        self.app_id = appinfo.APP_ID

    def get_is_flatpak(self):
        return self.portal.running_under_flatpak()
    
    def add_spawn_prefix_if_needed(self, command: str) -> str:
        if self.get_is_flatpak() and not command.startswith("flatpak-spawn"):
            command = "flatpak-spawn --host " + command
        return command
    
    def get_flatpak_permissions(self) -> dict[str, Any]:
        command = self.add_spawn_prefix_if_needed(f"flatpak info --show-permissions {self.app_id}")
        process = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/")
        stdout, stderr = process.communicate()

        if stderr:
            log.error(f"Error running command: {stderr.decode()}")
            return {}
        
        permissions_output = stdout.decode()
        # The shape differs per section. The context section maps to a dict,
        # and a bus policy section maps to a list.
        permissions_dict: dict[str, Any] = {}
        
        sections = permissions_output.split('\n\n')
        for section in sections:
            lines = section.strip().split('\n')
            header = lines.pop(0).strip('[]').lower().replace(' ', '-')
            if header == 'context':
                context_dict = {}
                for line in lines:
                    # Each value ends with ';', so drop the empty last element.
                    if '=' in line:
                        key, value = line.split('=')
                        context_dict[key] = value.split(';')[:-1]
                permissions_dict[header] = context_dict
            else: # For 'Session Bus Policy' and 'System Bus Policy' sections
                policy_list = []
                for line in lines:
                    # Each line reads '<name>=talk'; keep the name.
                    if '=' in line:
                        policy = line.split('=')[0]
                        policy_list.append(policy)
                permissions_dict[header] = policy_list

        return permissions_dict
    
    def has_dbus_permission(self, name: str, bus: str="session") -> bool:
        if bus not in ["session", "system"]:
            raise ValueError("Invalid bus type. Must be 'session' or 'system'.")
        permissions = self.get_flatpak_permissions()
        policy_permissions = permissions.get(f"{bus}-bus-policy", [])
        return name in policy_permissions
    
    def get_dbus_permission_add_command(self, name: str, bus: str="session") -> str:
        if bus not in ["session", "system"]:
            raise ValueError("Invalid bus type. Must be 'session' or 'system'.")
        
        command = "flatpak override --user"
        if bus == "session":
            command += " --talk-name="
        else:
            command += " --system-talk-name="
        command += name

        command += f" {self.app_id}"

        return command
    
    def show_dbus_permission_request_dialog(self, name: str, bus: str="session", description: str="None"):
        if not self.get_is_flatpak():
            return
        if self.has_dbus_permission(name, bus):
            return
        if bus not in ["session", "system"]:
            raise ValueError("Invalid bus type. Must be 'session' or 'system'.")
        
        if description is None:
            description = gl.lm.get("permissions.request.default-description")

        command = self.get_dbus_permission_add_command(name, bus)
        window = None
        # The request can arrive before the main window exists, so check both.
        app = gl.app
        if app is not None and hasattr(app, "main_win"):
            if app.main_win is not None:
                window = app.main_win

        if hasattr(gl, "store"):
            if gl.store is not None:
                window = gl.store

        window = FlatpakPermissionRequestWindow(gl.app, window, command=command, description=description)
        # window.present()
        GLib.idle_add(window.present) # Present on the idle loop, because a direct present() flickers