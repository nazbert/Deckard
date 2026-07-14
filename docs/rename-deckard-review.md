# Deckard rebrand MR (!61) — critical review findings

High-effort multi-agent review of `rename/deckard-rebrand` (8 finder angles → verify pass). All findings below are CONFIRMED (agent-verified, verified against library source, or live-reproduced). Ordered most-severe first.

**Disposition (2026-07-14): ALL findings fixed** in the review-fixes commit on this branch. Highlights: durable fsync'd migration marker + abort-on-write-failure (1); `_is_skeleton` now rejects any symlink and a symlinked new-root aborts cleanly (2, 5); `--data` abbreviation detected (3); old-bus guard rewritten to probe via `NameHasOwner` (no activation), catch `ValueError`, and poll instead of sleep (4); stale-autostart cleanup moved to `autostart.remove_legacy_autostart_entries`, run every launch (6); desktop entries now self-reference an absolute `Exec` with compare-before-write (7, every-launch rewrite); flatpak manifest tracks `branch: main` (8); outward URLs point at redirect-safe current repo names (9); `appinfo.py` now the single source for the app id + derived spellings (altitude); plus `mem_census`, `set_debug_info_filename`, dead `permissons.py` (deleted), and `scripts/deckard` MALLOC exports. Migration scenario grew to 17 cases; harness 122 pass + 1 xfail. Only deferred: `soak_driver.py`'s duplicated D-Bus constants (standalone tooling, not on `sys.path` for `appinfo`).

The migration is a **one-shot, runs-once-against-real-user-data** path with no second chance — hence the migration findings dominate the top of the list even where the trigger is narrow.

## Data-integrity (migration)

### 1. Marker write is not durable, and a failed write does not abort the rename — silent config loss
`rebrand_migration.py:72` (`_write_marker`) is a bare `open("w")`/`write` with no `fsync`; `migrate()` (~line 193) logs and swallows a failed pending-marker write, then proceeds to `os.rename` anyway. Two ways this loses the compat symlink permanently:
- Power loss right after the rename: on ext4 the rename metadata can commit before the marker's data blocks, leaving a **zero-length** marker in the new tree that matches neither `pending` nor `complete` → next start falls to the `fresh install` branch, symlink never created.
- Pending-write fails (EACCES/ENOSPC on the old tree) + crash before symlink → next start sees no marker and no old root → same `fresh install` branch.
Result: the 6 live JSONs embedding absolute `com.core447` paths (deck settings, pages incl. backups) dangle — backgrounds/screensavers/icons silently vanish, no log hint. **Fix:** write the marker with `atomic_json`'s mkstemp→fsync→`os.replace` pattern (verified stdlib-only, importable pre-`globals`), and `_abort` when the pending marker can't be written (old root still intact → clean retry next start).

### 2. `_is_skeleton` ignores directory-symlinks → `rmtree` destroys a user's data-relocation symlinks
`rebrand_migration.py:88` walks `os.walk` filenames only; a symlink-to-directory appears in `dirnames`, unfollowed. A new root containing only dir-symlinks (e.g. `data/plugins -> /mnt/big/plugins`, a common relocation pattern) is classified as deletable skeleton and `rmtree`'d at line 185 — contradicting the function's own docstring ("any regular file or symlink anywhere below means it is not ours to delete"). Live-reproduced. **Fix:** treat any symlink encountered in `dirnames` (or a non-empty `dirnames`/`filenames` mix beyond the known skeleton shape) as "not skeleton."

### 3. `--dat` abbreviation bypasses the migration skip → mutates real data during an isolated run
`rebrand_migration.py:146` scans argv for exactly `--data`/`--data=`, but `globals.py` resolves the flag via argparse, which accepts prefix abbreviations. `main.py --dat /tmp/sandbox` points the *app* at the sandbox while `migrate()` believes no override is active and renames the real `~/.var/app` tree + plants the symlink (and aborts if a pre-rename instance is running). Verified against argparse. **Fix:** parse with the same argparse (or accept any `--da…`/`--de…` prefix), or key the skip on `gl`-independent logic that matches argparse's abbreviation rules.

## Startup correctness

### 4. Old-bus transition guard can *launch* upstream, and an uncaught `ValueError` aborts startup
`main.py:324`. Two verified legs plus two design issues:
- **(a)** `session_bus.get_object("com.core447.StreamController", …)` with the default `follow_name_owner_changes=False` calls `activate_name_owner` (`dbus/proxies.py:249-250`), which sends **`StartServiceByName`** when the well-known name has no owner (`dbus/bus.py:178`). If a D-Bus activation `.service` file exists for the old name (an upstream flatpak install ships one), the probe **launches upstream StreamController during Deckard startup** — the exact race the guard exists to prevent.
- **(b)** The guard catches only `dbus.exceptions.DBusException`; the sibling new-name probe at `main.py:300` also catches `ValueError` ("last instance has not been properly closed"). A crashed pre-rename instance's stale owner state raises `ValueError` here → propagates uncaught → Deckard startup aborts with a traceback.
- **(c)** Unbounded lifetime: force-quits a side-by-side upstream install at *every* launch forever (+5s stall), with no gating on the migration marker.
- **(d)** Fixed `time.sleep(5)` instead of polling `name_has_owner`.
**Fix:** probe with `session_bus.name_has_owner(OLD_ID)` (no activation), only `get_object` on True; catch `ValueError` too; gate the whole guard on `rebrand_migration` reporting the migration incomplete; poll instead of sleep.

### 5. `rmtree` on a symlinked new-root → uncaught `OSError` traceback before any log sink
`rebrand_migration.py:185`. If the user pre-created `~/.var/app/io.github.nazbert.Deckard` as a symlink to another disk and its target holds only empty dirs, `lexists` passes, `_is_skeleton` walks the target, and `shutil.rmtree(symlink)` raises `OSError("Cannot call rmtree on a symbolic link")` — uncaught, at a point before any log sink exists. Live-reproduced. **Fix:** handle `islink(new_root)` explicitly (abort via `_abort` with guidance, or resolve).

## Packaging / UX

### 6. Stale-autostart cleanup is one-shot and never self-heals
`rebrand_migration.py:134`. `_cleanup_stale_autostart` runs only on the completion transition, *after* the `complete` marker is written — so a transient `os.remove` failure is never retried (every later start takes the marker fast-path and returns first), `--data` sessions never clean, and an old build re-run *after* migration recreates `StreamController.desktop` that nothing ever removes again. The old-identity entry then relaunches a stale build at every login, racing for the deck. **Fix:** also remove the legacy filenames from `autostart.setup_autostart` (runs every launch → self-healing) and let `autostart.py` own its own filename list.

### 7. Installed desktop entries hardcode `Exec=deckard` with no existence check or fallback
`flatpak/deckard-app.desktop:6` and `flatpak/autostart-native.desktop`. `Exec=deckard` resolves only if the manual `~/.local/bin/deckard` symlink is on PATH; unlike `Icon=`, `Exec` is never rewritten to the absolute `.venv/main.py` path the process already knows. Skip the (easily-missed) symlink step and app-grid launch + login autostart silently do nothing — and since `ensure_app_desktop_entry` rewrites the file every start, hand-edits don't stick. **Fix:** when `shutil.which("deckard")` is None, rewrite `Exec` to `<sys.executable> <MAIN_PATH>/main.py`.

### 8. Flatpak manifest pins a tag whose tree lacks the renamed files → deterministic build failure
`io.github.nazbert.Deckard.yml:115` pins `tag: 1.5.0-beta.14`; its tree (verified via `git ls-tree`) carries `flatpak/com.core447.StreamController.metainfo.xml`, not the new name. `install -D flatpak/io.github.nazbert.Deckard.metainfo.xml` (line 111) fails deterministically. Flatpak path is untested locally, but this is a hard break, not a stale-pin cosmetic. **Fix:** bump to a post-rename tag/commit (belongs with #128 `ci/flatpak-releases`).

### 9. Outward-facing URLs (incl. the in-app "Report an issue") point at `nazbert/deckard`, which won't exist until Phase 4
`HeaderHamburgerMenuButton.py` (About website/issue URLs), `README.md`, `flatpak/install.sh:154-157`, and the metainfo homepage/contribute URLs all reference `nazbert/deckard` / `gitlab.nb-labs.net/naz/deckard`. Host redirects are created *by* the Phase 4 rename, not before it, so between merge and Phase 4 the About dialog's "Report an issue" and the README/install.sh default download 404. **Fix:** either do the repo renames before/with the merge, or point at the current `nazbert/StreamController` (GitHub's redirect then keeps them working after the rename).

## Lower severity / cleanup

- **`ensure_app_desktop_entry` rewrites the desktop file every launch** (`autostart.py:171`) even when byte-identical → mtime bump makes GNOME/KDE re-scan their app cache at every login (incl. the `-b` autostart). Compare-before-write.
- **`scripts/deckard` drops the `MALLOC_ARENA_MAX`/`MALLOC_TRIM_THRESHOLD_` exports** that `launch.sh` sets → every wrapper launch self-re-execs once (one bare interpreter restart, tens of ms). Add the two `export` lines.
- **`tests/soak/mem_census.py:58`** PID auto-detect requires both `"main.py"` and `"Deckard"` in the cmdline, but `setproctitle` rewrites the whole cmdline to `"Deckard"` — the AND is unsatisfiable (pre-existing, but the MR re-asserted it). Match on `"Deckard"` alone.
- **`HeaderHamburgerMenuButton.py:200`** `set_debug_info_filename` was renamed `StreamController.log`→`Deckard.log`, but the plan's Phase 1 item was to point it at the real `logs/logs.log` (the named file has never existed). Plan item silently dropped.
- **`permissons.py`** (dead: imported nowhere, runs example code at import) was renamed rather than deleted — carries the app-id forward for zero function.
- **App-ID literal sprawl** (altitude): `io.github.nazbert.Deckard` appears ~25× across 12 Python files in 4 spellings (dotted, slash-path, ayatana-underscore, suffixed). `rebrand_migration.py` proves a stdlib-only, pre-`globals`-importable `appinfo.py` constants module is viable for every consumer — worth extracting before the next id change repeats this grep. (`main.py`'s `DEFAULT_DATA_PATH` is *not* dead as an earlier draft claimed — it is a live `--list-pages` fallback; it now derives from `appinfo.APP_ID` too.)
