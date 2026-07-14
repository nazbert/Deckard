# Deckard rebrand plan v2 (hardened) — rename fork from StreamController

**Decisions (Nigel, 2026-07-14):** name **Deckard**; app ID **`io.github.nazbert.Deckard`**; full technical rebrand; remotes renamed. Post-review decisions: env vars → `DECKARD_*`; donation surfaces kept but relabeled as supporting the upstream author; AUR `streamcontroller-git` package uninstalled (manual).

v2 incorporates a three-way hardening audit (surface inventory, startup/migration ordering, external identity — issue #147, 2026-07-14). Supersedes v1's naive migration design and 18-file surface list.

## Identity map

| Surface | Old | New |
|---|---|---|
| App ID / bus name / GtkApplication id | `com.core447.StreamController` | `io.github.nazbert.Deckard` |
| D-Bus object path (api.py:36) | `/com/core447/StreamController` | `/io/github/nazbert/Deckard` |
| Controller iface (api.py:39,76) | `….Controller` | `io.github.nazbert.Deckard.Controller` |
| D-Bus error (api.py:157) | `….Error.PageExists` | `io.github.nazbert.Deckard.Error.PageExists` |
| Tray menu path (tray.py:7) | `/com/core447/StreamController/Menu` | `/io/github/nazbert/Deckard/Menu` |
| Tray indicator path (tray.py:8) | `…/com_core447_StreamController_TrayIcon` | `/org/ayatana/NotificationItem/io_github_nazbert_Deckard_TrayIcon` |
| SNI Id (tray.py:9) | `….TrayIcon` | `io.github.nazbert.Deckard.TrayIcon` |
| Process title (main.py:37) | `StreamController` | `Deckard` |
| Var-app dir (globals.py:33, main.py:116) | `~/.var/app/com.core447.StreamController` | `~/.var/app/io.github.nazbert.Deckard` |
| Autostart entry (autostart.py:127) | `StreamController.desktop` | `Deckard.desktop` |
| Launcher | `/usr/bin/streamcontroller` (AUR pkg) | `~/.local/bin/deckard` wrapper → `.venv/bin/python main.py` |
| Env vars | `STREAMCONTROLLER_{ASSERT_DEVICE_OWNER, VIDEO_WRITE_HZ, VIDEO_WRITE_YIELD_MS, MEDIA_PROFILE}` | `DECKARD_*` same suffixes |
| Icons | `com.core447.StreamController.png` (48, 512) | `io.github.nazbert.Deckard.png` |
| Page-export default (MenuButton.py:181) | `StreamController_<ts>.json` | `Deckard_<ts>.json` |
| **Unchanged**: GNOME extension bus name `org.gnome.Shell.Extensions.StreamController` (upstream extension), `com_core447_*`/`dev_core447_*` plugin & asset IDs, `streamcontroller-plugin-tools`/`streamcontroller-streamdeck` PyPI names, store repo URLs, `remote.sc.core447.com`. | | |

## Phase 0 — pre-flight (manual, before first launch of the renamed build)

1. `sudo pacman -Rns streamcontroller-git` — removes `/usr/bin/streamcontroller`, the system desktop file, and system icon. **Until then, `~/.config/autostart/StreamController.desktop` execs the packaged upstream at login, racing the fork for the deck.**
2. Quit any running StreamController instance (the migration refuses to run while the old bus name is owned — see Phase 2).
3. Install the launcher: symlink/copy repo `scripts/deckard` (new in this MR) to `~/.local/bin/deckard`.

## Phase 1 — code + packaging rebrand (branch `rename/deckard-rebrand`, one MR)

Full audited rename surface: **36 text files + 2 icon renames**. Grep caveat for implementers: use plain GNU grep — `.gitignore:203`'s bare `StreamController` pattern hides the tracked `src/windows/PageManager/Importer/StreamController/` dir from gitignore-honoring tools.

**Core identity:** `main.py` (37, 116, 172, 286, 412, 538), `globals.py:33`, `src/api.py` (36–39, 76, 120–127, 157 + docstrings), `src/app.py` (284 log, 380 notification id), `src/tray.py` (7–9, 22, 24–26), `src/backend/PermissionManagement/FlatpakPermissionManager.py:40`, `permissons.py:50`, `autostart.py` (109, 127).

**User-visible strings:** `mainWindow.py:97` window title; `HeaderHamburgerMenuButton.py` about dialog (138, 148–149, 153, 165) **+ fix line 200**: `set_debug_info_filename(…, "StreamController.log")` names a file that doesn't exist — point it at `logs.log`; `KeepRunningDialog.py:36`; `ResponsibleNotesDialog.py:33`; `PluginBase.py` compat-error prose (197, 275–288); `MenuButton.py:181` export filename; comments `KeyGrid.py:438`, `DialBox.py:352`; `locales/locales.csv:112` (`onboarding.welcome.header`, all 5 languages; CSV is runtime-parsed, no compile step).

**Env-var namespace (atomic):** `BetterDeck.py:22`, `DeckController.py` (307, 318, 855, 1040), `media_pipeline_profiler.py:68`, `tests/scenario_brightness_routing.py`, `tests/scenario_env_var_resilience.py`. Historical docs mentioning the old names stay.

**Donations (keep, relabel):** About ko-fi (`HeaderHamburgerMenuButton.py:101`) and Onboarding ko-fi (`OnboardingWindow.py:404`) relabeled "Support Core447, author of the original StreamController"; `DonateWindow` likewise; `.github/FUNDING.yml` stays. Upstream-contributors fetch in About (`HeaderHamburgerMenuButton.py:105`) stays as lineage.

**Packaging:**
- `com.core447.StreamController.yml` → `io.github.nazbert.Deckard.yml`: id, module name, `/app/bin/StreamController` (99, 103), icon/desktop/metainfo installs (109–111), source URL → fork (114, 117). **Keep line 20** `--talk-name=org.gnome.Shell.Extensions.StreamController`.
- `flatpak/com.core447.StreamController.metainfo.xml` → `io.github.nazbert.Deckard.metainfo.xml`: id, launchable, name, URLs; `<developer_name>` → nazbert with upstream credited in the description.
- `flatpak/launch.desktop` (Name, Icon, **add missing trailing newline** — its absence produced the malformed `Categories=UtilityStartupWMClass=…` line in the packaged desktop file), `flatpak/autostart.desktop`, `flatpak/autostart-native.desktop` (`Exec=deckard -b`), `flatpak/launch.sh` (2, 7 — missed by v1), `flatpak/install.sh` (ids, build dir, bundle name, and 154–156 raw URLs currently fetching **upstream** — retarget to the fork; mark untested, flatpak tooling absent locally).
- New: `scripts/deckard` launcher wrapper; new `flatpak/deckard-app.desktop` template installed to `~/.local/share/applications/io.github.nazbert.Deckard.desktop` (see Phase 2 §system-side) — required because on Wayland the compositor maps window `app_id` → same-named desktop file for the taskbar icon; today the AUR package's system desktop file provides that and it disappears with Phase 0.
- Icons: rename both hicolor PNGs; `Assets/icons/hicolor/Attribution_README.md:3`.
- `.gitignore:203`: change the artifact pattern (also un-shadows the tracked Importer dir); `.devcontainer/devcontainer.json:2`; `requirements{,-dev}.txt` header comments.

**Docs:** README retitle + fork attribution, drop Flathub badges/install section and repology; `Dev-Planning-Board.md` remove (upstream org's board). Historical docs untouched.

**Tests:** `fixtures.py` — `_REAL_DATA_ROOT` guard must cover **both** old and new real dirs (dropping the old guard while the old 401 MB dir exists reopens the hazard it guards); `scenario_autostart_disable.py` (7, 73 — tracks autostart filename); `soak/soak_driver.py` (31–34, 59 — tracks api.py constants); `soak/mem_census.py` (finds the process by `"StreamController" in cmdline` — tracks new proctitle); `soak/README.md`; `scenario_tray_reregister.py:8`; `run_all.py:94` harness id (cosmetic). Redaction-test sample paths are self-consistent literals — leave.

**Deliberately NOT renamed:** `com_core447_*` / `dev_core447_*` plugin, icon-pack, and on-disk asset IDs (`Migrator_1_5_0.py`, `PluginRecommendations.py`, `StreamDeckUI.py` importer, store tests — renaming breaks every existing page file); `Author: Core447` headers (126 files) and all upstream credit; the `Importer/StreamController/` legacy-format importer (names the *source format*); GNOME WindowGrabber extension uuid + bus name; store backend URLs and `official_authors`; PyPI package names; "Stream Deck" hardware references; historical docs.

## Phase 2 — data migration (same MR)

**Design invalidated in v1 and rebuilt:** `globals.py:51–57,69` and `mp4_tile_cache.py:39` create the data-dir skeleton (`data/plugins/`, `data/cache/videos/`) at **import time on every invocation** — so a bare "new dir missing" check is poisoned before `main()` runs, and the existing MigrationManager slot (main.py:614) is ~500 lines too late (`Migrator.SETTINGS_DIR` et al. bake paths at import).

**Mechanism:** new stdlib-only module `rebrand_migration.py` (repo root), called from `main.py` **between the patcher import (line 53) and the main import block (line 55)** — before `import globals`. It asserts `"globals" not in sys.modules`, re-resolves `--data`/`--devel` overrides from `sys.argv` itself, and migrates the **whole var-app dir** (`data/` + `static/` + flatpak-era `cache/`/`config/`) — v1 moved only `data/`, which would have silently dropped a `static/settings.json` custom-data-path pointer.

With `OLD = ~/.var/app/com.core447.StreamController`, `NEW = ~/.var/app/io.github.nazbert.Deckard`, and a **stateful marker file** `.migrated-from-com.core447.StreamController` (implemented in `rebrand_migration.py`; regression-tested by `tests/scenario_rebrand_migration.py`, 13 cases):

1. **Fast path:** marker in NEW reads `complete` → return. Marker reads `symlink-pending` → finish the symlink and promote to `complete`.
2. **Custom `--data` override active** → skip entirely.
3. **Old-instance guard:** D-Bus `NameHasOwner("com.core447.StreamController")` (dbus-python, no main loop). If owned → **abort startup** with a clear message ("quit the running StreamController first"). Never rename under a live old instance: in the rename→symlink window its next path-based write (`atomic_json` settings save, loguru rotation, tile-cache write) recreates the old tree as a real dir, wedging the symlink with `FileExistsError` and splitting writes across two trees.
4. **State machine** (all transitions logged to stderr; logger isn't up yet):
   - `islink(OLD)` resolving to NEW → backfill `complete` marker, done. Foreign or broken link → abort loudly (never delete something we didn't make).
   - OLD is a real dir:
     - NEW absent → migrate (step 5).
     - NEW is **skeleton-only** (nothing but nested empty dirs — the import-time makedirs residue): remove skeleton, migrate.
     - NEW holds any real file → **abort loudly with instructions; never auto-merge, never delete**.
   - OLD absent, no marker → fresh install, no-op.
5. **Migrate = marker-travels-with-rename** (v2.1 refinement over the original "marker after symlink" design, which could not distinguish repair mode from a fresh install): write `symlink-pending` marker **into OLD**, then `os.rename(OLD, NEW)` (same fs — verified; atomic, instant for 401 MB) — the marker arrives in NEW with the rename — then `os.symlink(NEW, OLD)` and promote the marker to `complete`. A crash anywhere after the rename leaves a pending marker that step 1 heals on the next start; the only unhealed window is between two adjacent syscalls (marker write → rename), which degrades to a plain retry. If OLD reappears as a real dir while pending (old build relaunched), stay pending and log — never delete.
6. **Why the compat symlink:** 6 live JSON files (deck settings `5A5101JD9NN.json`, pages incl. backups) embed absolute old paths; the symlink keeps them valid without rewriting user data.
7. Completion also removes the two stale old-identity autostart entries (`StreamController.desktop`, `com.core447.StreamController.desktop`), closing the relaunch-at-login hole.

**System-side (idempotent, after migration in `main()`):**
- Remove stale `~/.config/autostart/StreamController.desktop` **and** the portal-era `com.core447.StreamController.desktop` (no existing code path deletes either; `autostart.py`'s disable branch only removes its own new filename). `setup_autostart` then regenerates `Deckard.desktop` (`Exec` → `deckard` wrapper). `opendeck.desktop` = unrelated app, untouched.
- Install/refresh `~/.local/share/applications/io.github.nazbert.Deckard.desktop` (Icon by absolute repo path, `StartupWMClass=io.github.nazbert.Deckard`, trailing newline) — mirrors the existing autostart copy-on-launch pattern; restores the Wayland taskbar icon after Phase 0 removes the system desktop file.

**Transition guard in `quit_running()` (main.py:286):** also probe the **old** bus name for one release, so a renamed launch detects a still-running old-branded instance instead of fighting it for the USB device (`reset_all_decks()` would USB-reset decks owned by the old instance).

## Phase 3 — verification

- New harness scenario `scenario_rebrand_migration.py`: normal move, skeleton-poisoned NEW, both-real-data abort, repair mode, idempotent re-run, foreign-symlink abort, `--data` override skip.
- Full harness run (fixtures dual guard, autostart scenario, env-var scenarios renamed).
- Hardware/field: Phase 0 done → launch from source; second-launch D-Bus handoff; tray icon + menu; autostart file regenerated with wrapper Exec; pages/plugins/assets intact post-migration; video background plays (exercises embedded absolute paths through the symlink); Wayland taskbar icon present.

## Phase 4 — remotes + external (after MR merged + field-verified)

1. GitLab: rename `naz/StreamController` → `naz/deckard` (name + path; redirect retained). Update local `gitlab` remote URL.
2. GitHub: `gh repo rename deckard -R nazbert/StreamController` (redirect retained; stays a fork in the network graph). Update `fork` remote; optionally rename `origin` → `upstream`.
3. Local checkout `~/dev/StreamController` → `~/dev/deckard` — manual, **last** (invalidates running session cwd + per-project memory path).

## Follow-ups

- **Icon artwork is still upstream's logo** (XMP metadata: `StreamControllerIcon_to_procreate`) — this MR renames files only; a Deckard logo is a separate task.
- `ci/flatpak-releases` (#128, uncommitted): adopt new manifest name/app-id before committing.
- `~/.config/plasmanotifyrc` stale `[Applications][com.core447.StreamController]` section — cosmetic, Plasma recreates for the new id.
- Memory files / docs pinning old repo URLs — redirects cover them; update opportunistically.

## Risks

- Old-build instance running at first new launch → mitigated by the old-bus-name abort guard + Phase 0 + autostart replacement (the old entry survives otherwise and re-races at every login).
- A future upstream flatpak install of `com.core447.StreamController` would collide with the compat symlink — acceptable, easily undone, flatpak not installed.
- `install.sh` flatpak path untested post-rename (no local tooling) — marked as such.
