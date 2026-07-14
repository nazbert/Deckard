# deckard-git — AUR recipe

Native Arch-family package for [Deckard](https://github.com/nazbert/Deckard),
tracking `main`. **Secondary** distribution format; the flatpak (GitLab #128) is
the broad primary. Plan: `docs/aur-pkgbuild-plan.md` · issue #151.

This directory is the **source of truth**; the AUR repo is a mirror of it.

## Build & install locally

```sh
cd packaging/aur/deckard-git
makepkg -si          # add -C for a clean chroot build (recommended before publish)
```

`makepkg` clones `main`, builds a venv under `/opt/deckard/venv` against
`python3.13`, and installs a `/usr/bin/deckard` launcher, desktop entry, icon,
AppStream metainfo, and the udev rule (`/usr/lib/udev/rules.d/60-deckard.rules`).

Notes:
- Depends on the AUR **`python313`** package (see below).
- `build()` downloads the pinned deps from PyPI (unavoidable with pinned wheels);
  this is a `-git` convenience package, not a fully-declared-sources build.
- First build is slow: PyGObject/pycairo/dbus-python compile from sdist, and
  numpy/opencv/matplotlib are large wheels.

## Publishing to the AUR

```sh
makepkg --printsrcinfo > .SRCINFO
# push PKGBUILD, deckard-git.install, .SRCINFO to ssh://aur@aur.archlinux.org/deckard-git.git
```

Keep the AUR repo limited to `PKGBUILD` + `deckard-git.install` + `.SRCINFO`
(the launcher and scriptlet are generated/embedded, no extra source files).

## Verification (do on real hardware — see plan §7)

1. `deckard` launches; GTK4/libadwaita UI renders.
2. Plug a Stream Deck → detected (udev + hidapi/libusb path).
3. Install a plugin from the store → loads under the bundled venv.
4. Enable autostart → the written `~/.config/autostart/*.desktop` execs `deckard`
   (NOT `/app/bin/launch.sh` — the landmine from the parent AUR package).
5. Uninstall → udev rule gone, no dangling autostart entry.

## Why the venv is pinned to Python 3.13 (`python313`)

The system `python` on Arch is already **3.14**, but `requirements.txt` is pinned
to **cp313** wheels because it is generated from the flatpak's GNOME 50 runtime,
which ships Python 3.13. Building the venv against `python313` therefore:

- reuses the *exact* pinned set the flatpak uses (one dependency source of truth);
- keeps parity with the platform plugins are built and tested against;
- decouples the install from system-python churn — a future `python` 3.15 bump
  will not break this package.

The cost is one AUR dependency (`python313`) and a bundled interpreter.

## Bumping the Python version

Using system `python` (3.14+) instead is **not** just an edit here — the pins
would need cp314 wheels, and most of the current pins (pillow 11.1, numpy 2.2.3,
matplotlib 3.10, …) predate them, so pip would fall back to slow/failing sdist
builds and the AUR's dep versions would drift from the flatpak's.

The Python baseline is set by the **flatpak runtime**, so bump it there first:

1. Raise `runtime-version` in `io.github.nazbert.Deckard.yml` to a GNOME runtime
   shipping the target Python (e.g. the release that carries 3.14).
2. Regenerate `pypi-requirements.yaml` / `requirements.txt` for the new
   `req2flatpak` target (`314-x86_64`, …) and confirm the whole dep set has
   wheels for that interpreter.
3. Re-verify the flatpak + plugin ecosystem on the new runtime.
4. Only then flip this recipe: set `_python=python3.14` **and** replace the
   `python313` dependency with the system `python` (drop the versioned AUR dep).

Until the flatpak leads that bump, staying on `python313` here is the correct
default — it maximises parity and avoids a beta-fresh-interpreter dependency tree
on a hardware app.
