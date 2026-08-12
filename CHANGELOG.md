# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Each release publishes an installable flatpak
bundle as a release asset.

## [Unreleased]

### Fixed

- Screensaver and brightness settings now show the values the deck actually
  uses. A page's screensaver brightness slider read 75 for a screensaver that
  had never been given one, while the deck dimmed to 30; a deck's own settings
  page offered 50 as the brightness for a deck that was running at 75. Decks
  that already have these values chosen keep them exactly as they are.
- Opening a deck's settings no longer changes or rewrites its configuration.
  The page used to fill in every setting the deck had never been given and
  save the result — which, for brightness, also dimmed the deck on the spot,
  and froze that day's defaults into the deck's configuration so that later
  improvements to them never reached it.
- Deck-level background media without an explicit loop setting now loops,
  instead of playing once and holding its last frame for as long as the page
  is up. Page-level background media is unchanged: it still plays once by
  default.
- A deck plugged in while the session is locked now shows the screensaver it
  is configured with, at the brightness it is configured with, instead of a
  blank panel at a brightness nothing had chosen.
- Opening the settings of a deck that has been rotated no longer re-saves and
  re-applies that rotation, with the full page reload that comes with it.
- Selecting pages in the page manager no longer makes the page editor's
  screensaver brightness slider write its own displayed value back to each
  page it visits, and reapply it to the deck.
- A deck plugged in after Deckard has started now appears on the D-Bus
  interface, and a deck that is unplugged disappears from it. Only the decks
  present at launch were ever published, so scripts driving Deckard over
  D-Bus could not see a deck plugged in later — including the common case of
  starting with the session, before the deck is ready — while a deck that had
  been removed stayed on the interface, still answering calls.
- The active page name a deck reports over D-Bus is now correct from the
  moment it appears there, instead of reading empty until something changed
  the page.
- Asking a deck over the D-Bus interface to show the page it is already
  showing now does nothing, instead of reloading the deck. Scripts that set a
  page on every event — a window rule, a home-automation trigger — made the
  deck re-render all of its keys each time.
- Every `--change-page` and `--change-state` on one command line now takes
  effect. When Deckard was already running, only the first request was sent to
  it: a command carrying several page changes moved one deck and left the
  others alone, and any state change on a command that also changed a page was
  dropped entirely.
- `--change-page` and `--change-state` now report failures in the terminal you
  typed them in, and exit non-zero, instead of leaving the reason in the
  running instance's log where nothing showed it to you. Requests are also no
  longer pre-rejected against limits the CLI invented (coordinates above 10,
  states above 20): a large deck's real coordinates and state numbers now
  reach the running instance, which checks them against the device itself.
- A page that cannot be loaded no longer blanks the deck while reporting
  success. Asking for a page whose file is present but unusable cleared every
  key and said the switch had worked; the deck now keeps the page it was
  showing and the request explains itself.
- Switching to a page while several pages are already cached no longer lands
  you on a dead one. With enough pages open the app could discard a page in
  the moment between picking it and showing it, leaving a deck whose keys
  looked right but did nothing until the page was loaded again — and leaving
  a second, competing copy of that page behind.
- Page edits made in different places at the same time no longer overwrite
  each other. Changing a page's settings — a screensaver, brightness,
  background or window-rule override — while a plugin or a key edit was
  saving the same page could silently drop one of the two changes, from the
  file and from the running app, so a setting you had just made would be back
  to its old value the next time you looked.
- Window-based automatic page switching now works on desktops that report a
  colon-joined `XDG_CURRENT_DESKTOP`, such as `ubuntu:GNOME` — window
  grabbing matched only single-name desktops and stayed silently disabled
  everywhere else.
- An idle deck showing the screensaver no longer repaints every key, dial and
  touchscreen once a second; a still screensaver now costs no per-second work
  at all. Recovering the picture after a dropped device write is handled by
  the existing repaint retry instead, which also covers a rare blank deck on
  screensaver entry that the per-second repaint had been masking.
- A deck that fails to connect no longer leaves background work running for
  the rest of the session. Every failed attempt — a deck whose USB link flakes
  while the session is starting, or one already claimed by another instance —
  stranded the threads that had been started for it, including one still
  trying to draw to the deck it never got. Repeated attempts piled them up, so
  a deck that would not come up cost idle CPU and memory until Deckard was
  restarted.
- Launching Deckard while it is still starting up now waits for that startup
  and brings the window up when it finishes, instead of giving up after ten
  seconds and exiting with an error — which also lost a `--change-page` sent
  in that window. A startup slower than the session bus's own call timeout
  still outlasts the wait.
- A page or state change given to a launch that lost the startup race is no
  longer dropped. Two launches at the same moment leave one of them handing
  over to the other, and the requests it had already taken went with it —
  nothing applied, nothing said, and a successful exit code.
- The message shown when the running Deckard does not answer `--change-page`
  or `--change-state` now names what it can actually mean. It offered a
  startup that had not finished as the likely explanation, which stopped being
  possible when the app started publishing its interface before taking its bus
  name; it now names the two things that remain — an older build, and an
  instance that is shutting down — with what to do about each.
- The list of controllers on the D-Bus interface now matches the objects a
  client can address. It was read from the decks the app had, so during the
  moment between a deck being added and its object appearing it named a deck
  whose object was not there yet — and a client that composed a path from that
  list got an error for a deck that was plainly plugged in.
- Asking to change a page on a deck when none are connected now says that the
  app may still be starting, rather than only that nothing is connected: the
  interface answers from the moment Deckard takes its bus name, which is
  before it has opened any deck.
- A damaged custom-asset library file no longer prevents Deckard from
  starting. It was read while the app was still building itself, so an
  unreadable one stopped the app before any window appeared, with nothing on
  screen to say why. Deckard now starts with an empty custom-asset library and
  says so in the log; the damaged file is renamed aside and kept next to the
  new empty one, so nothing is thrown away, and the asset files themselves are
  untouched.
- Importing a StreamDeck UI profile now updates the deck settings the rest of
  the app is reading. The import wrote its brightness and screensaver
  preferences straight to the file, so anything that had already read that
  deck — the deck itself included — could go on showing the settings from
  before the import until something else caused them to be read again.

### Changed

- A second launch now hands off to the running Deckard through the application
  framework itself, instead of the app deciding for itself whether one was
  already running. Two launches starting at the same moment — a session
  autostart alongside a restored session — can no longer both reach the decks.
- `--close-running` that does not manage to close the running Deckard now
  exits with an error instead of reporting success, and it waits for the
  instance to actually let go rather than for a fixed five seconds. Against an
  instance that is itself still starting up it waits until that instance can
  answer before asking it to quit, so the two can no longer leave you with
  nothing running at all.
- Dragging a key onto another position now changes the page in one step. The
  two keys used to be exchanged one at a time with a save around each stage,
  so the page file was written three times for one drag and a write landing
  mid-swap could put one key's actions under both positions until the next
  save corrected it. Choosing an icon for a key no longer saves the page a
  second time on top of the save that setting it already made.
- Page edits are written in the background about a second after the last
  change, instead of once per keystroke. Typing a label used to write the
  whole page file — with two disk syncs — for every character, on the same
  thread that draws the window. The page is still written immediately when
  you switch page, close a deck or quit the app, and whenever anything reads
  the file, so what you see and what is exported or backed up is always
  current. The trade: a crash or power cut can now cost the last second of
  edits — up to five while you are typing continuously — where before it
  could cost none.
- The active window is no longer watched in the background unless a page
  actually uses a window auto-change rule. Previously every session polled
  the foreground window continuously — several helper processes a second on
  X11 and KDE — whether or not any page asked for it. The watcher now starts
  the moment the first rule is enabled and stops when the last one is removed.
  Consequently the D-Bus `ForegroundWindow` property now tracks the desktop
  only while window rules are in use; it can still be set from outside at any
  time via `NotifyForegroundWindow`.
- Startup is faster and much quieter on the network: the automatic store
  update check now reads the store catalogue plus what is already installed,
  instead of downloading a thumbnail, a manifest and an attribution file for
  every asset in the store — including the ones you never installed. Opening
  the store window still loads the full listing with images.
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
