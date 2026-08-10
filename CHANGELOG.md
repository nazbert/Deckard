# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Each release publishes an installable flatpak
bundle as a release asset.

## [Unreleased]

### Changed

- Log files are now pruned automatically (the ten most recent rotations are
  kept) and default verbosity is lower — files record debug level and up, the
  console info and up. Set `SC_LOG_TRACE=1` to restore full trace logging on
  every sink for diagnosis.

## [0.2.1] - 2026-08-09

### Fixed

- Remote decks no longer misreport touch capability; touchscreen strip frames
  are no longer rendered and encoded for decks that cannot display them.
- Window grabbing survives an unset `XDG_CURRENT_DESKTOP` on X11 — previously
  a startup crash left it silently disabled for the whole session.
- Importing a StreamDeck-UI profile now rejects hotkeys carrying delay tokens
  with a clear message instead of silently dropping the whole hotkey.
- Outdated-action rows in the sidebar render again instead of failing on
  construction.
- Unplugging a deck mid-render no longer crashes the media tick on image-size
  lookups, and media loads skip cleanly when a deck closes during page load.
- Store items with missing or corrupt manifests are dropped with a log line
  instead of crashing catalog preparation workers, and downloads without a
  resolvable ref fail up front instead of staging junk.
- Notifications, plugin installs and DBus page/state changes degrade
  gracefully during startup and teardown instead of raising inside idle
  callbacks.

### Changed

- The entire tree now type-checks clean under mypy and the CI type-check lane
  is a blocking gate; roughly fifty latent defects surfaced by the typing work
  were fixed along the way.

## [0.2.0] - 2026-08-09

### Fixed

- Mutable default arguments across 8 sites, two of them on the plugin-facing
  API (`ActionHolder.action_support`, `ActionCore.set_background_color`): the
  shared default object leaked mutations between unrelated callers for the
  lifetime of the process.

### Changed
- test: deflake fair_lock and native_tile_cache — measure the right intervals, settle the right threads
- fix(action-core): gate the default on_update's compat on_ready on on_ready_finished
- fix(persistence): quarantine corrupt plugin JSON + bound .corrupt retention at every quarantine site
- refactor(asset-manager): one chooser base pair; flow boxes built on the main thread
- fix: backend bug batch — screensaver loop default, store ref fail-hard, redaction idempotence
- docs: retire the false self-heal claim; stamp the landed disposition on the presenter plan
- feat(hooks): SC_NO_ERROR_HOOKS kill-switch + per-site rate limiting
- fix: UI/asset bug batch — permission window arity, label font_name, URL-import sentinel, CSS alpha scale, GNOME ext uuid
- refactor(settings): debounce the font-row page-reload storms
- perf(store): defer read-clock index writes behind a debounced flush
- perf(labels): static-label raster cache — record/replay the draw.text mask blits
- perf(gif): cap the KeyGIF working set — opaque GIFs via the mp4 tile registry, budgeted alpha path
- fix(media): GIF transparency + per-frame timing outside KeyGIF
- fix: upstream-derived small fixes — store tag refs, corrupt-asset gate, Gio data-path open
- feat(lockscreen): systemd-logind fallback lock detector (Niri/Sway/river)
- perf: key/touchscreen mirror PIL→pixbuf off the GTK loop; batch set_main_error
- fix(app): skip destroying a never-realized main window at quit (GTK unrealized-dispose abort)
- fix: subprocess hygiene — list-argv backend launch, de-forked run_command, Gio URL opening
- perf(store): shared requests.Session with 429 retry/backoff + unified download helper
- fix(app): route SIGTERM/SIGHUP through on_quit so logout TERM terminates plugin backends
- refactor: concurrency-idiom cleanups -- sync StoreBackend, join waits, timer-wheel stragglers
- fix(store): transactional installs -- stage, validate, swap, delete old last
- perf: frame-identity native tile cache for background video
- perf: fair FIFO transport lock; retire 20Hz write cap defaults
- fix(ui): page change refreshes the sidebar again
- fix(logging): scrub faulthandler.log in place to keep live fds valid
- fix(ui): bind deck-stack child to its controller by identity, not serial name
- fix(app): never rebuild MainWindow on remote activation
- fix(boot): claim the single-instance lock atomically before touching decks

## [0.1.0] - 2026-07-14

### Added

- First Deckard release: an installable flatpak bundle is built and published
  as a release asset on every `vX.Y.Z` tag.
- Native Arch-family package (`deckard-git`) for the AUR, alongside the flatpak.
- Native installs run as the `deckard` command and store their data under
  `$XDG_DATA_HOME/deckard` (`~/.local/share/deckard`), migrated automatically
  from the previous `~/.var/app` location.
- About dialog shows the Deckard fork release version (from the `VERSION`
  file); the upstream StreamController base is noted in the About comments.
