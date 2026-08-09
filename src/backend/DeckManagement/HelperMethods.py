"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from datetime import datetime
from functools import wraps
import hashlib
from io import BytesIO
import os
import subprocess
import sys
import math
import re
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from loguru import logger as log
from PIL import Image

import gi
from gi.repository import Gio, GLib

# Gdk and Pango are imported lazily inside the four colour/font helpers below
# (#141): they are the only consumers, their only callers live under
# src/windows/, and a module-level import would drag the widget stack into
# every engine import closure that touches HelperMethods.
if TYPE_CHECKING:
    from gi.repository import Gdk, Pango

from src.backend.DeckManagement import font_resolver
from src.backend.atomic_json import atomic_write_json

# Import globals
from autostart import is_flatpak
import globals as gl


def sha256(text: str) -> str:
    """
    Calculates the sha256 hash of a file or string.

    Args:
        text (str): The file path or string.

    Returns:
        str: The sha256 hash of the file or string.
    """
    hash_sha256 = hashlib.sha256()
    if os.path.exists(text):
        with open(text, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
    else:
        hash_sha256.update(text.encode('utf-8'))
    return hash_sha256.hexdigest()


def file_in_dir(file_path, directory) -> bool | None:
    """
    Check if a file is present in a directory.

    Args:
        file_path (str): The path of the file to check.
        dir (str, optional): The directory to check. Defaults to None.

    Returns:
        bool: True if the file is present in the directory, False otherwise;
            None if `directory` names something that is not a directory
            (callers use the result in a boolean context, where that reads
            as "not present").
    """
    if not os.path.isdir(directory) and directory is not None:
        return None

    return os.path.split(file_path)[1] in os.listdir(directory)


def recursive_hasattr(obj, attr_string):
    """
    Check if an attribute exists in an object.

    Args:
        obj (object): The object to check.
        attr_string (str): The attributes to check separated with dots. e.g.: foo.bar

    Returns:
        bool: True if the attribute exists, False otherwise.
    """
    attrs = attr_string.split('.')
    for attr in attrs:
        if not hasattr(obj, attr):
            return False
        obj = getattr(obj, attr)
    return True


def font_path_from_name(font_name: str):
    return font_resolver.resolve(font_name, 400, "normal")


def font_name_from_path(font_path: str):
    return font_resolver.font_name_from_path(font_path)


def get_last_dir(path: str) -> str | None:
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path))
    elif os.path.isfile(path):
        return os.path.basename(os.path.normpath(os.path.dirname(path)))
    return None


def has_dict_recursive(dictionary: dict, *args) -> bool:
    working_dict: Any = dictionary
    for arg in args:
        working_dict = working_dict.get(arg)
        if working_dict is None:
            return False
    return True


def get_sys_param_value(param_name: str) -> str | None:
    for i, param in enumerate(sys.argv):
        if param.startswith(param_name):
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
    return None


def get_sys_args_without_param(param_name: str) -> list:
    """sys.argv minus every argument starting with `param_name` and the
    value following it. Returns a new list -- sys.argv itself is never
    modified (the old in-place version corrupted it for later readers and
    popped past the end when the matched parameter was argv's last
    element)."""
    args = []
    skip_next = False
    for arg in sys.argv:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith(param_name):
            skip_next = True  # also drop the parameter's value, if present
            continue
        args.append(arg)
    return args


def is_video(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.video_extensions

    return False


def is_image(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.image_extensions

    return False


def is_svg(path: str | None) -> bool:
    if path is None:
        return False
    if os.path.isfile(path):
        return os.path.splitext(path)[1][1:].lower().replace(".", "") in gl.svg_extensions

    return path.startswith("<svg ")


def get_image_aspect_ratio(img: Image.Image) -> str:
    width, height = img.size
    gcd = math.gcd(width, height)
    aspect_ratio = f"{width//gcd}:{height//gcd}"
    return aspect_ratio


def create_empty_json(path: str, ignore_present: bool = False):
    if not ignore_present and os.path.exists(path):
        return

    # Write empty json (atomically; also creates parent dirs)
    atomic_write_json(path, {})


def get_file_name_from_url(url: str):
    """
    Extracts the file name from a given URL.

    Args:
        url (str): The URL from which to extract the file name.

    Returns:
        str: The file name extracted from the URL.
    """
    # Parse the url to extract the path
    parsed_url = urlparse(url)
    # Extract the file name from the path
    return os.path.basename(parsed_url.path)


def download_file(url: str, path: str = "", file_name: str = None) -> str:
    """
    Downloads a file from the specified URL and saves it to the specified path.

    Args:
        url (str): The URL of the file to be downloaded.
        path (str): The path of the directory where the file will be saved. If a directory is provided, the filename will be extracted from the URL and appended to the path.

    Returns:
        path (str): The path of the downloaded file.

    Raises:
        requests.RequestException: on a network failure OR an HTTP error
            status. Nothing is left on disk in either case.
    """

    # Lazy import: this module is pulled in by nearly everything at startup,
    # and http_client imports requests.
    from src.backend import http_client

    if file_name is None:
        file_name = get_file_name_from_url(url)

    path = os.path.join(path, file_name)

    # Streamed through the shared session. timeout: never block indefinitely
    # on a black-holed/hung connection (same anti-pattern the About-dialog
    # fetch had -- a timeout-less network call that could hang the caller
    # forever). An HTTP error status raises instead of writing the error page
    # into the asset cache under the requested file name, where the
    # extension-based is_image() check would happily accept it as an asset.
    http_client.download_to_file(url, path, timeout=10)

    return path

def natural_keys(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]


def natural_sort(strings_list: list[str]) -> list[str]:
    return sorted(strings_list, key=natural_keys)


def natural_sort_by_filenames(paths_list: list[str]) -> list[str]:
    sorted_paths = sorted(
        paths_list, key=lambda path: natural_keys(os.path.basename(path)))
    return sorted_paths


def add_default_keys(d: dict, keys: list):
    """
    Add nested default keys to a dictionary.

    :param d: The dictionary to add keys to.
    :param keys: A list of keys to create nested dictionaries for.
    :return: None; the dictionary is modified in place.
    """
    current_level = d
    for key in keys:
        if key not in current_level:
            current_level[key] = {}
        current_level = current_level[key]


def instance_cache(func):
    """Per-instance method memoization: results live in the instance __dict__
    (freed with the instance), keyed by the positional args, which must be
    hashable. Not thread-safe (no lock around the check-then-set)."""
    attr = f"_instance_cache_{func.__name__}"

    @wraps(func)
    def wrapper(self, *args):
        cache = self.__dict__.get(attr)
        if cache is None:
            cache = self.__dict__[attr] = {}
        if args not in cache:
            cache[args] = func(self, *args)
        return cache[args]

    return wrapper


def _load_gdk():
    """Import Gdk on demand -- see the TYPE_CHECKING note at the top (#141)."""
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk
    return Gdk


def _load_pango():
    """Import Pango on demand -- see the TYPE_CHECKING note at the top (#141)."""
    from gi.repository import Pango
    return Pango


def color_values_to_gdk(color_values: tuple[int, int, int] | tuple[int, int, int, int]) -> "Gdk.RGBA":
    Gdk = _load_gdk()
    # Copy before normalizing: callers pass tuples (which .append would
    # crash on) and reuse the sequence they passed in afterwards.
    values = list(color_values)
    if len(values) == 3:
        values.append(255)
    color = Gdk.RGBA()
    # Every caller works in 0-255 on all four channels (that is what
    # gdk_color_to_values hands back, and what the label/font settings
    # persist). CSS rgba() takes the channels in 0-255 but the ALPHA in
    # 0-1, so the raw value has to be scaled -- feeding it 0-255 clamped
    # every alpha >= 1 to fully opaque, and a semi-transparent label colour
    # came back out of the colour chooser as opaque (#203).
    color.parse(f"rgba({values[0]}, {values[1]}, {values[2]}, {values[3] / 255})")

    return color


def gdk_color_to_values(color: "Gdk.RGBA") -> tuple[int, int, int, int]:
    green = round(color.green * 255)
    blue = round(color.blue * 255)
    red = round(color.red * 255)
    alpha = round(color.alpha * 255)

    return red, green, blue, alpha


def get_pango_font_description(font_family: str, font_size: int, font_weight: int, font_style: str) -> "Pango.FontDescription":
    Pango = _load_pango()
    if font_style == "italic":
        font_style = Pango.Style.ITALIC
    elif font_style == "oblique":
        font_style = Pango.Style.OBLIQUE
    else:
        font_style = Pango.Style.NORMAL

    desc = Pango.FontDescription()
    desc.set_family(font_family)
    desc.set_absolute_size(font_size * Pango.SCALE)
    desc.set_weight(font_weight)
    desc.set_style(font_style)

    return desc


def get_values_from_pango_font_description(desc: "Pango.FontDescription") -> tuple[str | None, float, int, str]:
    # The size really is fractional (Pango sizes are in 1024ths and the
    # division keeps the remainder) and the family really can be unset --
    # both values go straight into the persisted font settings, so the
    # annotation follows the data rather than the other way round.
    Pango = _load_pango()
    font_family = desc.get_family()
    font_size = desc.get_size() / Pango.SCALE
    font_weight = desc.get_weight()
    font_style = desc.get_style()

    if font_style == Pango.Style.ITALIC:
        style_name = "italic"
    elif font_style == Pango.Style.OBLIQUE:
        style_name = "oblique"
    else:
        style_name = "normal"

    return font_family, font_size, font_weight, style_name


def get_sub_folders(parent: str) -> list[str]:
    if not os.path.isdir(parent):
        return []

    return [folder for folder in os.listdir(parent) if os.path.isdir(os.path.join(parent, folder))]


def sort_times(time_list):
    """
    Sort a list of datetime strings in ascending order.

    Parameters:
    time_list (list of str): List of datetime strings to be sorted.

    Returns:
    list of str: Sorted list of datetime strings.
    """
    return sorted(time_list, key=lambda x: datetime.fromisoformat(x))


def run_command(command):
    """Detach a shell command line and forget about it.

    `command` is a COMMAND LINE, not an argv list: callers (including
    plugins -- this is de-facto plugin API surface) rely on shell syntax
    such as pipes, `&&` and variable expansion, and the flatpak prefix is
    spliced in as a string. Do not "fix" this to shlex.split/argv; build
    the argv yourself and call subprocess directly if you need that.

    The command gets its own session, its stdio pointed at /dev/null and
    ~ as its cwd, so it outlives us cleanly.

    Fire-and-forget: spawn failures are logged, never raised. The fork
    wrapper this replaced ran the Popen in the child, so an OSError (no
    /bin/sh, a HOME that no longer exists, fork refused under load) never
    reached the caller -- and callers are plugin action callbacks that have
    no way to handle one.
    """
    if command is None:
        return

    if is_flatpak():
        command = "flatpak-spawn --host " + command

    try:
        process = subprocess.Popen(command, shell=True, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    except OSError as e:
        log.error(f"Failed to run command {command!r}: {e}")
        return
    # This used to be a multiprocessing.Process wrapping the Popen -- a fork
    # of the whole interpreter (GTK, plugins, deck threads and all) whose
    # only job was to orphan the grandchild so nobody had to reap it. The
    # direct child needs reaping instead, or it lingers as a zombie for the
    # life of the app; one throwaway daemon thread per spawn does that.
    threading.Thread(target=process.wait, name="run_command_reaper", daemon=True).start()

def open_web(url):
    """Open a URL in the user's default browser.

    Uses Gio rather than shelling out to xdg-open: GLib routes the call
    through the OpenURI portal when sandboxed, so this works in the flatpak
    without `flatpak-spawn --host`, and a URL containing shell metacharacters
    can no longer become a command.
    """
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except GLib.Error as e:
        # The shell path failed silently; Gio raises, so say so.
        log.error(f"Failed to open URL {url}: {e}")

def svg_string_to_pil(svg_string, width: int = 96, height: int = 96):
    """
    Convert an SVG string to a PIL Image object.
    
    Args:
        svg_string (str): String containing SVG data
        width (int, optional): Desired width of the output image
        height (int, optional): Desired height of the output image
        
    Returns:
        PIL.Image: The converted image
    """
    import cairosvg

    # Convert SVG string to PNG using cairosvg
    png_data = cairosvg.svg2png(
        bytestring=svg_string.encode('utf-8'),
        output_width=width,
        output_height=height
    )

    # Create PIL Image from PNG data
    img = Image.open(BytesIO(png_data))

    return img


def svg_to_pil(svg_path: str, width: int = 96, height: int = 96):
    """
    Convert an SVG file to a PIL Image object.
    
    Args:
        svg_path (str): Path to the SVG file or string containing SVG data
        width (int, optional): Desired width of the output image
        height (int, optional): Desired height of the output image
        
    Returns:
        PIL.Image: The converted image
    """
    import cairosvg

    # Read SVG file

    if os.path.exists(svg_path):
        with open(svg_path, 'rb') as f:
            svg_data = f.read()

        # Convert SVG to PNG using cairosvg
        png_data = cairosvg.svg2png(
            bytestring=svg_data,
            output_width=width,
            output_height=height
        )
        
        # Create PIL Image from PNG data
        img = Image.open(BytesIO(png_data))
        
        return img
    elif svg_path.startswith("<svg "):
        return svg_string_to_pil(svg_path, width, height)
    else:
        raise ValueError(f"Could not create SVG from string or path: {svg_path}")