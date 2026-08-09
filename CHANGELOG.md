# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Each release publishes an installable flatpak
bundle as a release asset.

## [Unreleased]

## [0.2.0] - 2026-08-09

### Fixed

- Mutable default arguments across 8 sites, two of them on the plugin-facing
  API (`ActionHolder.action_support`, `ActionCore.set_background_color`): the
  shared default object leaked mutations between unrelated callers for the
  lifetime of the process.

### Changed
- test: deflake fair_lock and native_tile_cache — measure the right intervals, settle the right threads (#186, #202) (!110)
- fix(action-core): gate the default on_update's compat on_ready on on_ready_finished (#179) (!109)
- fix(persistence): quarantine corrupt plugin JSON + bound .corrupt retention at every quarantine site (#152) (!108)
- refactor(asset-manager): one chooser base pair; flow boxes built on the main thread (#136) (!107)
- fix: backend bug batch — screensaver loop default, store ref fail-hard, redaction idempotence (#204 #200 #162) (!106)
- docs: retire the false self-heal claim; stamp the landed disposition on the presenter plan (#88, #89) (!105)
- feat(hooks): SC_NO_ERROR_HOOKS kill-switch + per-site rate limiting (#92, #91) (!104)
- fix: UI/asset bug batch — permission window arity, label font_name, URL-import sentinel, CSS alpha scale, GNOME ext uuid (#190 #208 #191 #203 #185) (!103)
- refactor(settings): debounce the font-row page-reload storms (#78) (!102)
- perf(store): defer read-clock index writes behind a debounced flush (#180) (!101)
- perf(labels): static-label raster cache — record/replay the draw.text mask blits (#207, #188) (!100)
- perf(gif): cap the KeyGIF working set — opaque GIFs via the mp4 tile registry, budgeted alpha path (#201, #199) (!99)
- fix(media): GIF transparency + per-frame timing outside KeyGIF (!93)
- fix: upstream-derived small fixes — store tag refs, corrupt-asset gate, Gio data-path open (!92)
- feat(lockscreen): systemd-logind fallback lock detector (Niri/Sway/river) (!91)
- perf: key/touchscreen mirror PIL→pixbuf off the GTK loop; batch set_main_error (!90)
- fix(app): skip destroying a never-realized main window at quit (GTK unrealized-dispose abort) (!89)
- fix: subprocess hygiene — list-argv backend launch, de-forked run_command, Gio URL opening (!87)
- perf(store): shared requests.Session with 429 retry/backoff + unified download helper (!83)
- fix(app): route SIGTERM/SIGHUP through on_quit so logout TERM terminates plugin backends (!82)
- refactor: concurrency-idiom cleanups -- sync StoreBackend, join waits, timer-wheel stragglers (!80)
- fix(store): transactional installs -- stage, validate, swap, delete old last (!76)
- perf: frame-identity native tile cache for background video (!72)
- perf: fair FIFO transport lock; retire 20Hz write cap defaults (!71)
- fix(ui): page change refreshes the sidebar again (!70)
- fix(logging): scrub faulthandler.log in place to keep live fds valid (!69)
- fix(ui): bind deck-stack child to its controller by identity, not serial name (!68)
- fix(app): never rebuild MainWindow on remote activation (!67)
- fix(boot): claim the single-instance lock atomically before touching decks (!66)

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
