# AUR / PKGBUILD packaging plan — Deckard

Status: **planning** (2026-07-14). Secondary distribution format; flatpak (issue #128)
stays the broad cross-distro primary. Decision record on why AUR (and why not AppImage
or a full deb+rpm suite): #128 note_2142.

Scope: **Arch-family only** by design (Arch, Manjaro, EndeavourOS, CachyOS). Not a
coverage play — serves the maintainer's own CachyOS machine and the Arch ecosystem.
Ubuntu/Fedora users stay on flatpak.

---

## 1. Goal

Ship an installable, low-maintenance native package for Arch-family systems that:
- Installs a `Deckard` command on `PATH` and a desktop entry (files already exist:
  `flatpak/deckard-app.desktop` → `Exec=Deckard`, `flatpak/autostart-native.desktop`
  → `Exec=Deckard -b`).
- **Installs the udev rules** (`udev.rules`) to the host — the one thing flatpak/AppImage
  structurally cannot do. This is the native package's headline advantage for a
  USB-HID device app.
- Tracks `main` first (`deckard-git`), graduates to a tagged stable `deckard` once the
  #128 pipeline stamps real `v*` tags (the `VERSION` file is currently empty).

## 2. Two package variants (staged)

| Variant | Source | When | Blocks on |
|---|---|---|---|
| `deckard-git` | `git+https://…/Deckard.git#branch=main`, `pkgver()` from `git describe`/commit count | **now** | nothing |
| `deckard` (stable) | `v*` release tarball | after #128 tags | #128 versioning bootstrap (VERSION empty today) |

Start with `deckard-git`. No stable tags exist yet, and a `-git` package matches the
flatpak manifest's own "track main" stance (`io.github.nazbert.Deckard.yml` Deckard
module → `branch: main`).

## 3. Dependency strategy — THE key decision (confirm before writing the recipe)

`requirements.txt` is ~30 direct deps, heavily version-pinned to cp313 wheels, and
~15 of them (`streamcontroller-streamdeck`, `streamcontroller-plugin-tools`, `dasbus`,
`python-wayland-extra`, `pyclip`, `get-video-properties`, `usb-monitor`, `async-lru`,
`rpyc`, `serpent`, `fuzzywuzzy`, `Levenshtein`, `py-gcode-metadata`, …) are **not** in
Arch repos. Three ways to handle this:

### Option A — bundled venv (RECOMMENDED)
Depend on the **native/system** libraries only (Python, GTK4, libadwaita,
gobject-introspection, the native modules — see §4). At `build()`, create a venv and
`pip install -r requirements.txt` into it; ship venv + app source under
`/opt/deckard`. `/usr/bin/Deckard` is a wrapper (see §5).

- **Pro:** one dependency source of truth — the *same* `requirements.txt` the flatpak
  already consumes; pip resolves the pinned wheels for the host's Python. No per-dep
  Arch mapping to maintain. Isolated from Arch's python-package churn. Lowest ongoing
  maintenance → matches the "cheap recipe" rationale for doing AUR at all.
- **Con:** not idiomatic Arch (bundled venv, doesn't share system python packages);
  large (~numpy+opencv+matplotlib+pillow); `pip` needs network at build (fine — AUR
  builds locally on the user's machine); a few sdists compile (see §4 build deps).

### Option B — full system-python deps
Map every dep to `python-*` (repo or new AUR sub-packages).
- **Pro:** idiomatic; shares system packages; smaller.
- **Con:** ~15 new AUR sub-packages to create+maintain; **version skew** — Arch's
  numpy/opencv/pillow won't match the pins, risking subtle media-pipeline breakage.
  Contradicts the low-maintenance goal. Rejected unless upstream already maintains
  these sub-packages we can depend on.

### Option C — hybrid
System packages for the big compiled deps (`python-numpy`, `python-opencv`,
`python-pillow`, `python-gobject`, `python-cairo`) to save disk and get Arch's
optimized builds; venv with `--system-site-packages` for the pure-Python long tail.
- Middle ground; more moving parts to get right. Consider only if A's package size
  becomes a real complaint.

**Recommendation: A.** Confirm before writing the PKGBUILD. Also **pull the upstream
`streamcontroller-git` AUR PKGBUILD as a reference** — it solves the identical
dependency problem for the parent project; adopt its approach where sane (and avoid its
known autostart landmine, §6).

## 4. Dependencies (Option A)

Native/system runtime deps (Arch repo package names — verify each at recipe time):
- `python`, `gtk4`, `libadwaita`, `gobject-introspection`, `gobject-introspection-runtime`
- `hidapi` (manifest builds 0.15.0), `libusb`, `libgusb` (manifest 0.4.9)
- `libpeas2` (manifest libpeas 2.2.1 — **verify Arch has the 2.x package, not v1**)
- `libportal`, `libportal-gtk4` (manifest enables the gtk4 backend)
- `cairo`, `pango`, `gdk-pixbuf2`, `librsvg` (CairoSVG/pixbuf loaders)
- `gstreamer` + `gst-plugins-base`/`-good` if GTK media playback is exercised
  (verify — video-bg goes through cv2/imageio-ffmpeg, but confirm no GtkMedia path)
- `pulseaudio`/`libpulse` (pulsectl), `dbus`

Build deps (`makedepends`, because several wheels build from sdist — `PyGObject`,
`pycairo`, `dbus-python`, `evdev`, `indexed_bzip2`, `pyperclip`):
- `base-devel`, `python-pip`, `cmake`/`meson` (if any dep needs it), plus headers:
  `gobject-introspection`, `cairo`, `dbus` (dev headers ship in the base packages on
  Arch), `python` (Python.h).

Do **not** rebuild hidapi/libgusb/libpeas from source — depend on the Arch binaries.

## 5. Package layout (Option A)

```
/opt/deckard/                     # app source (cp -r repo) + venv/
/opt/deckard/venv/                # pip-installed requirements.txt
/usr/bin/Deckard                  # wrapper (below)
/usr/share/applications/io.github.nazbert.Deckard.desktop   # from deckard-app.desktop
/usr/share/icons/hicolor/256x256/apps/io.github.nazbert.Deckard.png  # flatpak/icon_256.png
/usr/share/metainfo/io.github.nazbert.Deckard.metainfo.xml  # flatpak/…metainfo.xml
/usr/lib/udev/rules.d/60-deckard.rules                      # udev.rules  ← the advantage
```

`/usr/bin/Deckard` wrapper mirrors `flatpak/launch.sh` — it **must** export the malloc
tunables so `main.py` skips its `os.execve` self-re-exec (main.py:29):
```sh
#!/bin/sh
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=131072
exec /opt/deckard/venv/bin/python /opt/deckard/main.py "$@"
```

`post_install`: `udevadm control --reload && udevadm trigger` (+ note to replug the
deck); `update-desktop-database`; `gtk-update-icon-cache`.

## 6. Landmines & risks (grounded in the code)

1. **Autostart landmine (the one the memory flagged).** `autostart.py` self-heals
   legacy entries and a portal fallback path bakes `/app/bin/launch.sh`, which does not
   exist on a native install. Native autostart must go through `Deckard -b`
   (`autostart-native.desktop` is exactly this). **Verify** the enable-autostart flow
   on a native install writes an entry that execs `Deckard`, not `launch.sh`, and that
   `LEGACY_AUTOSTART_NAMES` cleanup doesn't fight it.
2. **`is_flatpak()` branches.** `autostart.py` and others branch on `/.flatpak-info`.
   A native install exercises the less-tested "not flatpak" paths — needs a hardware
   pass, not just a byte-compile.
3. **Data path.** `globals.py` defaults `DATA_PATH` to `~/.var/app/io.github.nazbert.Deckard/data`
   even natively (overridable via `--data`, static `settings.json`, or the `PLUGIN_DIR`
   env the nix packaging already uses). **Decide:** keep the `~/.var/app` default (simplest;
   would even share data with a co-installed flatpak — feature or footgun?) or point the
   wrapper at an XDG path. Recommend keeping the default for v1 to minimise divergence.
4. **Python-version churn.** Pins are cp313; when Arch moves to 3.14 the pinned wheels
   stop matching. Option A's fresh `pip` resolution mostly absorbs this (pip picks the
   right wheel per host Python), but sdist-built deps then recompile — CI/local rebuild
   needed on each Python major bump. A `-git` package rebuilds anyway.
5. **First-run rebrand migration.** `main.py` runs `rebrand_migration` before
   `import globals`; on a fresh native install with no prior StreamController data it
   should no-op. Confirm it doesn't try to migrate a non-existent `~/.var/app` tree.
6. **Package size / build time.** numpy+opencv+matplotlib in a venv is heavy; first
   `makepkg` will be slow. Acceptable for AUR; note it in the package description.

## 7. Verification (on the CachyOS host — matches the "run in the real app" bar)

1. `makepkg -si` clean-chroot build succeeds.
2. `Deckard` launches; UI renders (GTK4/libadwaita resolve from system libs).
3. Plug a Stream Deck → device detected (udev rule + hidapi/libusb path working).
4. Install a plugin from the store → lands in the data dir, loads (validates the
   plugin-store path under a bundled venv — the mechanism AppImage couldn't support).
5. Enable autostart → inspect the written `~/.config/autostart/*.desktop`, confirm it
   execs `Deckard`, relogin persists (landmine #1).
6. Uninstall → udev rule removed, no dangling autostart entry.

## 8. Deliverables & milestones

- [ ] **M0 — decision:** confirm dependency strategy (§3, recommend A) + data-path
      policy (§6.3).
- [ ] **M1 — recipe:** `deckard-git` PKGBUILD + `Deckard` wrapper + `.install` scriptlet,
      in-repo under `packaging/aur/deckard-git/` (source of truth; AUR is a mirror).
- [ ] **M2 — build/verify:** clean-chroot `makepkg`, run §7 on CachyOS hardware.
- [ ] **M3 — publish:** push to the AUR as `deckard-git`; link from README.
- [ ] **M4 — stable:** add `deckard` (tag-based) once #128 stamps `v*` — depends on #128.

## 9. Open decisions for Nigel

1. **Dependency strategy** — Option A (bundled venv, recommended) vs B vs C (§3).
2. **Data-path policy** — keep `~/.var/app` default or switch native to XDG (§6.3).
3. **Repo home for the recipe** — `packaging/aur/` in this repo (recommended, keeps it
   versioned with the deps it mirrors) vs a standalone AUR-only repo.
4. **Scope of variant 1** — `deckard-git` only for now, defer stable `deckard` to M4
   behind #128 (recommended).
