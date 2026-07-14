# Deckard

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Made with Python](https://img.shields.io/badge/Made%20with-Python-ff7b3f.svg)](https://www.python.org/)

**Deckard** is a Linux application for the Elgato Stream Deck, with plugin support, automatic page switching, video wallpapers, and full Stream Deck + (dials and touchscreen) support.

It is a heavily reworked fork of [StreamController](https://github.com/StreamController/StreamController) by [Core447](https://github.com/Core447), which remains the foundation of this app. The fork has diverged too far to be reintegrated upstream; upstream contributions should go to StreamController.

![Main Screen](https://streamcontroller.core447.com/assets/screenshots/main_screen.png)
*Background image by [kvacm](https://kvacm.artstation.com)*

## About this fork

Notable divergences from upstream:

- Single-writer deck render pipeline with substantially higher video-background frame rates
- Stream Deck + dial and touchscreen event routing (swipes, drags, strip taps) to actions
- Background image/video extension onto the SD+ touchscreen strip
- Memory-footprint and long-uptime fixes (cache caps, leak repairs)
- Central exception hooks with log redaction
- An extensive headless regression harness (`tests/`)

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

Lock your Stream Deck when your system is locked (available on KDE, GNOME, and Cinnamon).

## Installation

Deckard runs from source:

```sh
git clone https://github.com/nazbert/Deckard.git Deckard
cd Deckard
python -m venv .venv
.venv/bin/pip install -r requirements.txt
ln -s "$(pwd)/scripts/Deckard" ~/.local/bin/Deckard
Deckard
```

Copy `udev.rules` to `/etc/udev/rules.d/` if your user lacks direct access to the deck hardware.

On first launch after upgrading from StreamController, existing data under `~/.var/app/com.core447.StreamController` is migrated automatically.

A Flatpak manifest (`io.github.nazbert.Deckard.yml`) is maintained but currently untested.

## Attribution

Deckard is derived from [StreamController](https://github.com/StreamController/StreamController), copyright Core447 and contributors, licensed under GPL-3.0. If you find this app useful, consider [supporting Core447](https://ko-fi.com/core447), whose work this fork builds on.

## Links

- [Upstream project](https://github.com/StreamController/StreamController)
- [Upstream Wiki](https://streamcontroller.github.io/docs)
- [Upstream Discord](https://discord.gg/MSyHM8TN3u)

## Note

This application is unofficial and not affiliated with Elgato.
