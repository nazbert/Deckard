# Deckard

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Made with Python](https://img.shields.io/badge/Made%20with-Python-ff7b3f.svg)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/nazbert/Deckard)](https://github.com/nazbert/Deckard/releases)

**Deckard** is a Linux application for the Elgato Stream Deck, with plugin support, automatic page switching, video wallpapers, and full Stream Deck + (dials and touchscreen) support.

It is a heavily reworked fork of [StreamController](https://github.com/StreamController/StreamController) by [Core447](https://github.com/Core447), which remains the foundation of this app. The fork has diverged too far to be reintegrated upstream; upstream contributions should go to StreamController.

![Main Screen](https://streamcontroller.core447.com/assets/screenshots/main_screen.png)
*Background image by [kvacm](https://kvacm.artstation.com)*

## About this fork

Deckard shares StreamController's UI and plugin ecosystem but has rebuilt most of what sits underneath it. Notable divergences from upstream:

### Rendering and performance

- Single-writer render pipeline: one media thread owns all device I/O, behind a per-deck fair FIFO transport lock — key presses and dial turns are never starved by video frames.
- Frame-identity tile caching for video backgrounds and a raster cache for static labels, giving substantially higher video-wallpaper frame rates at lower CPU.
- GIFs render with correct transparency and per-frame timing, with a bounded decode working set so large GIFs can't blow the memory budget.

### Stream Deck + support

- Dial and touchscreen events (taps, long presses, swipes, drags, dial turns and pushes) route to actions.
- Background images and videos extend onto the touchscreen strip.

### Robustness

- All settings and page JSON is written atomically; corrupt files are quarantined and healed by the loader instead of crashing the app or being silently overwritten.
- Plugin-store installs are transactional (stage, validate, swap) — an interrupted download can't leave a half-installed plugin behind.
- The single-instance lock is claimed atomically at boot, and SIGTERM/logout shuts the app down cleanly, including plugin backend processes.
- Uncaught exceptions from every thread are routed into the log file, with credential redaction.
- Store networking uses a shared session with retry and backoff instead of one-shot requests.

### Memory and long uptimes

- Cache budgets across the render path, weak-reference plugin event observers, and a long series of leak fixes — the app is built to run for weeks, verified by multi-day soak testing.

### Modernization

- `dbus-python` is gone; all D-Bus work goes through GLib/Gio.
- The entire tree type-checks clean under mypy, and mypy and ruff are blocking CI gates.
- An extensive headless regression harness (`tests/`) exercises the render pipeline, persistence, plugin events and store against fake deck hardware.

### Releases and packaging

- The fork carries its own release line (`VERSION` file, `vX.Y.Z` tags, `CHANGELOG.md`), independent of upstream's version numbers.
- Every release publishes an installable flatpak bundle; an Arch PKGBUILD is maintained in-repo.
- Native installs run as the `deckard` command and keep their data under `~/.local/share/deckard`, migrated automatically from previous StreamController or Deckard locations.

### Fork policy

Nothing is cherry-picked from upstream — the trees have diverged too far for mechanical patches. Upstream commits are treated as idea sources, and anything worth having is reimplemented natively.

## Supported Devices

Deckard supports the following Elgato Stream Deck models:

- Stream Deck Original (2)
- Stream Deck Mini
- Stream Deck XL
- Stream Deck Pedal
- Stream Deck Plus
- Stream Deck Neo (only the normal buttons)
- Stream Deck Modules

## Features

### Plugins

Plugin support with a built-in store to download actions; plugins from the upstream StreamController store are compatible. For plugin development details, see the upstream [Wiki](https://streamcontroller.github.io/docs).

### Wallpapers

Customize your Stream Deck pages with image and video wallpapers — including extending them onto the Stream Deck +'s touchscreen strip.

### Screen Saver

Set up a custom screen saver to display a picture or video when your Stream Deck is idle.

### Automatic Page Switching

Available for GNOME, Hyprland, Sway, KDE (when kdotool is installed) and all X11 desktops: automatically change the active page based on the focused window.

### Auto-Lock

Lock your Stream Deck when your system is locked. GNOME, Cinnamon, KDE and Hyprland are detected natively; other environments (Niri, Sway, river, …) are covered by a systemd-logind fallback.

## Installation

### Flatpak bundle

Every release ships an installable flatpak bundle on the [releases page](https://github.com/nazbert/Deckard/releases):

```sh
flatpak install --user ./deckard-<version>-x86_64.flatpak
```

The required GNOME runtime is pulled from Flathub if it is not already installed. Flatpak cannot install udev rules, so copy `udev.rules` from this repository to `/etc/udev/rules.d/` if your user lacks direct access to the deck hardware.

### Arch Linux

An Arch package recipe is maintained at `packaging/aur/deckard-git`. It builds the latest `main` into a self-contained Python 3.13 environment under `/opt/deckard` with a `deckard` launcher, and installs the udev rule system-wide:

```sh
git clone https://github.com/nazbert/Deckard.git
cd Deckard/packaging/aur/deckard-git
makepkg -si
```

### From source

Requires Python 3.13+.

```sh
git clone https://github.com/nazbert/Deckard.git Deckard
cd Deckard
python -m venv .venv
.venv/bin/pip install -r requirements.txt
ln -s "$(pwd)/scripts/Deckard" ~/.local/bin/Deckard
Deckard
```

Copy `udev.rules` to `/etc/udev/rules.d/` if your user lacks direct access to the deck hardware.

### Data migration

On first launch, existing data is migrated automatically — both from a previous StreamController installation (`~/.var/app/com.core447.StreamController`) and from older Deckard locations. Native installs keep their data under `~/.local/share/deckard`; flatpak installs use the usual `~/.var/app` location.

## Attribution

Deckard is derived from [StreamController](https://github.com/StreamController/StreamController), copyright Core447 and contributors, licensed under GPL-3.0. If you find this app useful, consider [supporting Core447](https://ko-fi.com/core447), whose work this fork builds on.

## Links

- [Upstream project](https://github.com/StreamController/StreamController)
- [Upstream Wiki](https://streamcontroller.github.io/docs)
- [Upstream Discord](https://discord.gg/MSyHM8TN3u)

## Note

This application is unofficial and not affiliated with Elgato.
