# Memory Footprint Reduction — Implementation Plan (v2, hardened)

**Date:** 2026-07-06 · **Companion to:** `docs/memory-footprint-plan.md` (design + bug appendix; "bug N" below refers to its §6)
**v2:** revised after an adversarial hardening pass (4 independent reviews verifying every claim against code, with micro-benchmarks). Material changes from v1 are marked **[H]**. Two v1 designs were rejected outright: the shared TileVideoCache *instance* (broke the build invariant and thrashed seeks) and `id()`-keyed frame dedup (unsafe under CPython id reuse). The v1 `fc-match :weight=<N>` template would have rendered all normal labels bold.

**Branch model:** one topic branch per phase off `local-integration`, stacked only where a dependency is stated. One commit per work item unless noted. Hardware verification by Nigel per phase gate.

Each work item: **Change** / **Accept** / **Verify** / **Risk**.

---

## Phase 0 — Caps & telemetry  (branch `perf/mem-phase0-caps`)

### P0.1 Memory telemetry sampler
**Change:** new `src/backend/mem_telemetry.py` **[H: moved out of DeckManagement/Subclasses — it's process-wide]**: daemon thread, env-gated `SC_MEM_TELEMETRY=1`. Every **60s** (never faster — `smaps_rollup` measured 6.4ms median / 20ms max on the live 6.1GB-VmData process, and the walk holds `mmap_lock` for read) append CSV: timestamp, VmRSS, VmSwap, Private_Dirty (smaps_rollup), thread count, fd count, gc counts, and app counters (add a monotonic page-switch counter in `DeckController.load_page`). Start from `main.py` beside the profiler hookup.
**Accept [H]:** sampler-thread CPU time <0.05%/hr in its own accounting; fps-harness **p99 frame gap** unchanged (a once-per-minute ≤20ms tail can't be seen in mean fps — measure the tail).
**Risk:** ≤1 dropped frame/min worst case from the mmap_lock tail; acceptable and now measured for.

### P0.2 Cap OpenCV's global pool
**Change:** `main.py`, pinned line: immediately **after the P0.4 re-exec block and before `import globals`** (`main.py:45`) — `import cv2; cv2.setNumThreads(2)`. Must precede the first *parallel cv op* (the pool is lazily created; `cvtColor` is the only parallel_for_ user in the app — `background_video_cache.py:308-318`, `key_video_cache.py:99`, `MediaManager.py:78`; PIL does all resizing, and VideoWriter/VideoCapture threading is FFmpeg-side, unaffected by this knob).
**Accept:** steady-state thread count −30; ≤2 `background_0`-named threads.
**Verify:** `cat /proc/<pid>/task/*/comm | sort | uniq -c`; fps harness.
**Risk [H]:** downgraded — cache build is *not* a page-load burst (one source frame per media tick during first playthrough); worst case ~4ms extra source-res cvtColor per tick during 4K builds at 2 threads. Fallback value 4.

### P0.3 Cap FFmpeg threads on the two uncapped captures
**Change:** `MediaManager.py:73` → `cv2.VideoCapture(video_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])`; `key_video_cache.py:33` → same with `2` (interim until P2.2 deletes the class).
**Accept/Verify:** opening a key video no longer spawns ~nproc threads (thread telemetry).

### P0.4 Arena cap in the launch environment
**Change:** at the very top of `main.py` (verified insertion point `main.py:15-18`, before `import globals` at `:45` — globals parses argv at import but argv is passed verbatim): if `MALLOC_ARENA_MAX` unset and not `SC_REEXEC`, set `os.environ` (`MALLOC_ARENA_MAX=2`, `MALLOC_TRIM_THRESHOLD_=131072`, `SC_REEXEC=1`) and **[H]** `os.execve(sys.executable, sys.orig_argv, os.environ)` (orig_argv preserves `-X`/`-O` interpreter flags; plain execv would drop them). Packaged launches: **[H]** inject in `flatpak/launch.sh` (simpler than manifest `--env`) and `autostart.py`, so users don't pay the re-exec.
**Accept:** vars visible in `/proc/<pid>/environ`; arena census (P0.6) shows ≤~4 large arena regions.
**Verify:** A/B overnight soak vs control; **[H]** verified no DBus double-send (`execve` replaces the process before `quit_running()`/`make_api_calls()` run) and no re-exec loop (env check + SC_REEXEC).
**Risk:** `--change-page`/`--list-*` CLI fast paths pay one re-exec (~50ms) — document; glibc-only knob, harmless elsewhere.

### P0.5 `malloc_trim` probe (experiment; expect to delete)
**Change [H — rewritten]:** env-gated `SC_MALLOC_TRIM=1`. Trigger from the **telemetry thread on idle signals** (screensaver entry, or N seconds with no page switch) — **not** from `load_page`: `load_page` runs on the main thread from several call sites, and with arenas capped at 2 (P0.4) `malloc_trim(0)` holds arena locks that *every* allocating thread funnels through — a trim on the switch path would hitch the media writer and the UI. Log **duration and RSS delta** per trim.
**Accept [H]:** promotion gate = median reclaim >50MB **and** p99 trim duration under 50ms. Note: `MALLOC_TRIM_THRESHOLD_` from P0.4 already auto-trims; the expected outcome is "measures ~0 → delete the flag".

### P0.6 Soak/measurement scripts
**Change:** `tests/soak/`: (a) `mem_census.py` (bucket anonymous VMAs by size class); (b) `soak_driver.py` (DBus API loop: page switches, brightness, screensaver force where exposed; writes markers into the telemetry CSV); (c) README for the manual steps (USB unplug/replug ×20, config open/close ×20, right-click ×50).

### P0.7 Overlay passthrough one-liner  **[H — promoted from bug appendix (bug 21); it erodes current fps and Phase 5 assumes the fast path works]**
**Change:** `DeckController.py:2467` `hide_overlay()` → set `self._overlay = None` (the fast path tests `is None` at `:2989`).
**Verify:** show_error→hide_error on a bg-video key; profiler shows the key back on the passthrough path.

**Phase 0 gate:** A/B decides how much of the live 463MB swap is arena waste → re-prioritizes Phase 5 (see split gate there).

---

## Phase 1 — Deterministic lifecycle  (branch `fix/lifecycle-teardown`)

Order inside the phase: P1.1 → P1.2 → P1.3; rest independent.

### P1.1 Make `ActionCore.clean_up()` complete and idempotent
**Change** (`src/backend/PluginManager/ActionCore.py`):
- `self._cleaned_up` guard under a small lock (eviction and disconnect paths can race).
- Extend to GenerativeUI: snapshot `list(self.generative_ui_objects)`, clear the list, `GLib.idle_add(_destroy_gen_ui_batch, snapshot)`. Batch helper: per-object try/except; **[H]** skip objects whose action re-registered them (resurrected action owns *new* instances, but guard anyway); **[H]** skip never-built widgets once P4.1 lands (`_widget is None` → just deregister, don't build-to-destroy). `GenerativeUI.destroy()` verified safe from the idle batch: it wraps itself in `run_on_main`, which runs inline on main (`GtkHelper.py:41-42`) — no re-queue.
- **No `run_on_main` in clean_up** — it runs from main *and* worker threads today (eviction happens on whatever thread calls `get_page`, incl. USB monitor and media threads; the docstring at `ActionCore.py:599-603` claiming "runs on the GTK main thread" is stale — fix it in this commit).
**Accept:** double `clean_up()` no-op; after eviction of a gen-ui-using page, `generative_ui_objects` empty and rows unparented.
**Verify:** unit (fake action + recorded destroy()); manual: HA dial config, 5 page switches, debug dump of reachable gen-ui count.

### P1.2 Framework-owned teardown at every drop site — **all five [H]**
The hook contract change (hooks are pure notifications; framework guarantees `clean_up`) makes any missed site a *guaranteed* leak for hook-overriding plugins (today they're saved by the default hook body calling `clean_up`). Verified drop-site inventory:
1. `Page.remove_plugin_action_objects` (`Page.py:353-371`) — also fix collect-then-delete iteration; sibling `remove_plugin_actions_from_json` fixes (bug 38).
2. **[H]** `Page.load_action_objects` reload diff (`Page.py:194-196`) — the path every `reload_similar_pages` config change hits.
3. **[H]** Sidebar action delete (`ActionManager.py:589-601`).
4. **[H]** `ActionConfigurator.py:298-299` (`on_remove` click).
5. `MissingRow.on_remove_click` (**[H]** correct path: `src/windows/mainWindow/elements/Sidebar/elements/ActionMissing/MissingRow.py` ~:121-127) — also fix bug 29 (deletes whole input subtree).
Each: hook (try/except) → `clean_up()`. Contract note in `on_removed_from_cache` docstring.
**Accept:** grep audit — no site removes an ActionCore from a live structure without `clean_up`; uninstall of an active-page plugin leaves no bound methods in SignalManager (debug dump helper).

### P1.3 `DeckController.close()` — one teardown sweep  **[H — sequence rewritten]**
New `close(remove_media: bool, app_quit: bool = False)`; `delete()` aliases it this phase. Runs on a **dedicated daemon thread** (not the shared GtkHelper background pool — a hook-wedged close would starve page loads there, and quit's `shutdown_background_pool()` would cancel it). Sequence:
1. `self._closing = True` — gates: producer paths, `screen_saver.on_key_change` (else a keypress re-arms a fresh Timer after step 2), **`ScreenSaver.hide()`**, and `load_page` itself.
2. **Defuse screensaver directly [H]:** `if screen_saver.timer: timer.cancel()`; `screen_saver.enable = False`; `screen_saver.showing = False`. **Never call `set_enable(False)`/`hide()` here** — v1 did, and `hide()` takes `_load_page_lock` then runs a full `load_page()` (`ScreenSaver.py:190-238`), resurrecting the deck mid-close (deterministically, whenever the screensaver is showing at unplug).
3. **Stop the library read thread [H]:** add `BetterDeck.stop_read_thread()` that sets `run_read_thread=False` **on the wrapped device** and joins briefly. The current `self.deck.run_read_thread = False` in `delete()` (`DeckController.py:1501`) is a **silent no-op** — BetterDeck has no attribute passthrough, so it sets a dead attr on the wrapper while the library thread reads the underlying device's flag. Without this, the reader's `resume_from_suspend` loop (`StreamDeck.py:209-262`) can **re-open the device for 10s after our close** (the "TransportError(-1) on next start" class `app.py:210-215` guards against). Method no-ops when the attr is absent (FakeDeck). Do this *before* the writer message; it also stops input callbacks firing into teardown.
4. **Stop + join tick thread [H — moved before action teardown]:** `keep_actions_ticking = False; tick_thread.join(2.0)`. The tick body iterates `active_page.action_objects` unguarded (`DeckController.py:1323-1341`, `:2480-2490`); concurrent `clear_action_objects` mid-iteration can kill the tick loop or recomposite inputs being swept.
5. `ClearAndCloseMsg` via `submit_control` + `media_player.stop()` (bounded 2s join). Writer performs final clear + device close (`_exec_clear_and_close`, `DeckController.py:458-469`); the terminal message wipes stale image tasks, so plugin enqueues during step 6 can't paint afterward (verified — teardown order vs writer is safe either way).
6. **Action teardown — skipped when `app_quit=True` [H]:** for each cached page of this controller — note values are `{"page": …, "page_number": …}` wrappers (`PageManagerBackend.py:71`) — call `page["page"].clear_action_objects()`. **Also sweep the screensaver stash [H]:** if `screen_saver.original_inputs`/`original_background` are set (deck closed mid-screensaver), run `close_resources()` over the stash and close `original_background.video` — that's where the page's real 50–150MB lives during screensaver. Never under `_load_page_lock`; never on main (hooks may `run_on_main`). *Quit rationale:* on_quit runs on main (`app.py:191-247`) with a 6s force-quit; worker-side hooks that `run_on_main` would block 30s — so quit does steps 1-5, 7-9 only (memory is irrelevant at `os._exit`; device hygiene is what matters).
7. Resource sweep (writer stopped): `background` image/video `close()` incl. `_cache_cap`; give `ControllerInput` a real `close_resources()` forwarding to states (fixes bugs 19/20 — guard `current_image`); `encode_memo.clear()`; drain `image_tasks`/`tasks`/`control_q`; fallback `try: deck.close()` (normally already closed by the writer in step 5 — this is the wedged-writer fallback, both try/except'd).
8. Deregistration: `gl.page_manager.pages.pop(self, None)`; `self.active_page = None`. **[H]** All access to `gl.page_manager.pages` gets a small lock or snapshot-iteration (`clear_old_cached_pages` iterates it from arbitrary threads while close() pops — dict-mutation-during-iteration race, M5).
9. `action_executor.shutdown(wait=False, cancel_futures=True)`; loader pool shutdown (P1.5); `gc.collect()` (graph is cyclic).
**UI detach [H — decoupled from close()]:** `remove_controller` queues **one early GLib.idle** doing `DeckStack.remove_page` + pop `deck_attributes`/`deck_numbers` *before* dispatching the slow close() — (a) fixes the existing bug of `remove_page` doing pure GTK work on the USB monitor thread (`DeckManager.py:223-224`), (b) prevents a fast replug's `add_page` idle racing a late detach and leaving two stack children for one serial.
**Call sites:** `remove_controller` (early UI idle + dedicated close thread), fake-deck decrement path (bug 4), app quit (`app_quit=True`). **[H]** Add a one-line check that `RemoteDeckManager` removals route through `remove_controller` (unverified in review).
**Accept:** after unplug: no `pages` key for the dead controller, thread count back to pre-plug, no fd to device or cache mp4, RSS within noise of pre-plug after the collect. Unplug **while screensaver showing** is an explicit test case.
**Verify:** hardware unplug/replug ×20 with bg video (incl. mid-screensaver); fake-deck add/remove ×50 asserting a `weakref.ref(controller)` dies.
**Risk:** in-flight `action_executor` callbacks during step 6 — bounded by clean_up idempotency; double-close via `_closing` flag.

### P1.4 Weak, locked callback registries + event-dispatch fix
**Change:** `src/Signals/weak_callbacks.py`: `CallbackRegistry` (WeakMethod for bound methods, strong for free callables; `add/remove/snapshot()` under a lock; snapshot prunes dead refs). Backs: `SignalManager.connected_signals` (fixes the unlocked cross-thread iteration, bug 28), `EventHolder.observers`, AssetManager-plugin `Observer`.
**[H] Also in this item (same file, same ecosystem batch — bug 27):** replace `EventHolder.trigger_event`'s per-call `asyncio.new_event_loop()` + `asyncio.to_thread` (`EventHolder.py:28-50`) with direct synchronous dispatch (or one shared dispatcher thread). This is the hottest callback path in the system (AudioControl PulseEvent fires per PulseAudio event — bursts of tens/sec during dial volume changes) and each call currently churns an event loop + default executor + epoll fd; it would pollute P0.1's own fd telemetry.
**Verified frequencies [H]:** no signal fires per-frame/per-tick (hottest `trigger_signal` = ChangePage per page switch) — snapshot-under-lock cost is negligible.
**Plugin audit results [H — recorded]:** OSPlugin `GraphBase.py:40` registers `AppQuit → self.stop_process` where the owner is registry-held — safe under weakrefs *only because* P1.2 guarantees its eviction hook runs (stops the process); AudioControl registers per-action bound methods (the fix target); HA and VolumeMixer register nothing. **Caveat:** closures/lambdas capturing an action keep strong refs (bug-6 growth persists for that pattern) — release-note it; `SC_STRONG_CALLBACKS=1` escape hatch stays.
**Accept [H — reworded]:** page-**switch cycling beyond the cache budget** (evict→recreate) ×100 with AudioControl → observer count constant. (v1 said "page reload ×100", which the *live* action-reuse path would pass trivially — the `#FIXME: gets never used` at `Page.py:221` is stale; reload actually reuses instances via `Page.py:178`. Design-doc bug 34 is thereby withdrawn; eviction-recreate is the real growth path.)

### P1.5 Persistent loader pool  **[H — resized + self-healing]**
**Change:** per-deck `self.load_executor = ThreadPoolExecutor(max_workers=max(8, total_inputs), thread_name_prefix=f"load_{serial}")` (v1's fixed 8 would serialize an XL's 32 inputs 4-deep **on the media-player thread** — `load_all_inputs` runs there via `media_player.add_task`, `DeckController.py:1257`, so its deadline waits block the sole writer). On deadline expiry with `stuck` non-empty: `shutdown(wait=False, cancel_futures=True)` and **replace the pool** — thread leakage then happens once per wedge event (matching today) instead of every switch, while healthy switches stay fast.
**Accept:** steady-state page switches spawn 0 new threads; wedged-plugin scenario leaks ≤1 pool per wedge.

### P1.6 Popover unparent
`KeyButtonContextMenu` (`KeyGrid.py:542-545`) and `DialContextMenu` (`DialBox.py:446-448`): `on_close` → `GLib.idle_add(self.unparent)`, re-entry guard. Accept: right-click ×50 → widget count flat.

### P1.7 Small fixes (one commit each)
- `PageManagerBackend.__init__` honors `performance.n-cached-pages` (+ ±1 semantics) — bug 35.
- `key_video_cache.py:60` delete `do_caching = True` — bug 15.
- Uninstall purge key from actual import name; teardown over cached pages too — bug 7.
- `control_q` reject-after-stop — bug 12; `api_page_requests.pop` — bug 13; `gl.app.asset_manager` clear — bug 14.
- **[H]** `MediaManager.get_thumbnail` fd leak (bug 23) — it would trip this phase's own "no fd growth" gate.
- **[H]** `pages` dict lock (from P1.3 step 8) lands here if not in P1.3's commit.

**Phase 1 gate (hardware):** unplug/replug soak (incl. mid-screensaver) + 2h idle with HA/AudioControl/VolumeMixer: RSS slope ≈0, threads constant, fds constant.

---

## Phase 2 — Media memory  (branch `perf/media-mem`, stacks on Phase 1 — needs `close_resources` plumbing)

### P2.1 Extract `Mp4FrameCache`; share the **file**, not the instance  **[H — redesigned]**
v1's shared-instance registry is rejected on evidence: (a) the build core requires monotonically increasing frame requests — interleaved consumers abort the writer and delete the tmp (`background_video_cache.py:211-215`), so a shared build never completes; (b) post-build, consumers' wall-clock timelines drift outside `MAX_DECODE_AHEAD=30` → container seek per request (measured 0.05→0.92 ms/frame, 18×).
**Change:**
- `mp4_tile_cache.py`: `Mp4FrameCache(source, out_size, sat)` — build via `cv2.VideoWriter` (mp4v — **[H]** benchmarked vs MJPG: half the size, faster sequential *and* seek reads; existing background caches stay valid), atomic promote, `CAP_PROP_N_THREADS=1`, wall-clock pick, `close()`. **No "incomplete-build resume" in the spec [H]** — it doesn't exist today (close aborts + deletes tmp; VideoWriter can't append) and v1 claiming it was fiction.
- **Registry over cache *files*:** `(md5, size, sat) → {path, state, refcount}` under a module lock. Exactly one **detached builder** per cache file: a background thread that decodes source → encodes the full tile mp4 independently of playback ticks (playback decodes source directly until the cache promotes — same as today's first playthrough). Detached build also fixes the v1/today regression where a cache only materializes after one *uninterrupted* playthrough (**[H]** the deleted JPEG format resumed across interruptions; the detached builder is what replaces that property).
- **Per-consumer `VideoCapture`** (N_THREADS=1, cheap at tile size) + per-consumer last-frame memo. `InputVideo.close()` → registry `release()`; capture teardown at refcount zero **under the registry lock** with identity comparison (a weakref-death callback must not evict a newer same-key entry).
- **[H]** Module-level `(path, size, mtime) → md5` memo — both cache classes currently hash the whole source file in the constructor on the page-switch path, and the registry key would multiply that ×N keys.
**Accept:** background path behavior unchanged; builder produces byte-valid caches under kill/restart (tmp cleanup); **[H]** decode-failure during build clamps frame count and releases the capture (bug 17's class must not be ported).
**Verify:** **[H]** port `scenario_keyvideo_build` and `scenario_display_saturation` to the new classes *in the same PR* (they currently pin the code this deletes/refactors — otherwise the harness gate silently tests dead code); micro-bench ≥500fps at 120px (measured headroom ~400×).

### P2.2 Switch key/dial videos to the tile cache
**Change:** `InputVideo` uses `Mp4FrameCache` via the registry; keep `InputVideo`'s own pacing (wall-clock, gap-clamp, `loop=False` end-clamp — `KeyVideo.py:56-82`) and have the cache expose `get_frame(index)` (verified this preserves pause/resume/end behavior). Delete `key_video_cache.py` + JPEG-per-frame format; sweeper one-shot legacy cleanup **[H]** must bypass the referenced-hash check for legacy patterns and cover both `single_key/<stem>/` and `key: <n>/` layouts (`key_video_cache.py:142-144`). Wire `close()` into `ControllerKeyState.set_video`/`clear` (bug 18).
**Accept:** 30s/30fps 120px key video ≈ O(1 frame) RSS (±5MB) vs ~39MB; `performance.cache-videos=false` respected (no build; direct decode).
**Verify:** hardware: key+dial+bg video simultaneously; page-switch latency (expect *improvement* — today's eager `load_cache` decodes every cached JPEG on the loader workers at switch).

### P2.3 GIFs → tile-fitted frames
**Change:** `KeyGIF`: decode once, `ImageOps.contain` to ≤2× tile, store fitted RGBA (alpha must survive — cv2's gif demuxer returns BGR, alpha loss confirmed empirically, so transparent GIFs stay an in-RAM list). Opportunistic: alpha-probe first frames; opaque GIFs route through `Mp4FrameCache`.
**Accept [H — corrected]:** 500px/200-frame GIF ≤ **~46MB** at 2× (240×240 RGBA × 200), vs ~200MB today; if that's judged too high, store 1× (~12MB) and accept softness above 100% media-size (UI max is 200% — `ImageEditor.py:119` × layout). Dial GIFs are dead code (`raise NotImplementedError`, `DeckController.py:3543`) — keys only.

### P2.4 Downsample-at-load for static media
**Change:** `InputImage` (`KeyImage.py:36-49`) and `BackgroundImage` (`DeckController.py:1701-1716`): fit to ≤2× target at construction, keep `self.path` for re-decode. **[H]** The re-decode trigger must key off the **composed** layout (`background.size × layout.size`, incl. plugin `set_action_layout` — `ImageLayout.size` is an unvalidated float that can exceed 2.0 and arrive after load), not the page layout alone. Memoize the touchscreen background fit (`DeckController.py:3617-3621`, keyed (path, mtime, size)) — verified it re-opens + LANCZOS-fits from disk **per composite** today (also a CPU win on every dial tick).
**Consumers audited [H]:** only the composite path and the dial direct-render read these; no editor/save-back/blur depends on source resolution.
**Accept:** 4K background page: background RSS ≤2MB (was ~33MB); pixel A/B on hardware.

### P2.5 Encode-memo: second-hit admission  **[H — redesigned]**
v1's hit-rate gate is rejected: eviction only happens inside `put()`, so a cap-full memo after a content change would refuse all new puts, never evict, never warm the new loop — a *permanent* fps regression (bistable starvation). Also its window metric conflated puts with hits, and 256 puts ≈ 1s at 8 keys×30fps (shorter than any loop period).
**Change:** (a) `encode_memo.clear()` in `Background.set_video`/`set_image` (`DeckController.py:1533-1557`, beside the existing `gc.collect()`); (b) **doorkeeper second-hit admission**: a small ring/Bloom set of recently-seen hashes; a new entry is inserted only on its **second** sighting. Warms any looping content by wrap 2-3, rejects true noise, no window tuning, no `volatile` plumbing needed (v1's "caller passes volatile=True" had no plumbing — `put()`'s one caller sees only the composited image).
**Accept:** noise video: memo stabilizes ≤ a few MB; standard looping test video: loop_fps unchanged (hits by third wrap).

### P2.6 ScreenSaver stash release
**Change:** at `show()`, release the stashed input set + `original_background` (`ScreenSaver.py:149-154`; `hide()` clears without closing, `:219` — the stash is never restored). **[H]** Do the closes via a media-thread-submitted task (or after a tick boundary): show() swaps inputs under `_load_page_lock`, but a tick begun earlier can still hold a state ref — closing under it is bounded to one discarded composite, but do it cleanly. **[H]** Dropped from v1: "close the screensaver's cv2 capture on hide" — already fixed on this branch (hide→load_page→`Background.set_video` closes the old video).
**Verify [H]:** `scenario_screensaver_storm` + `scenario_screensaver_bg_race` explicitly, plus telemetry across 5 cycles.

**Phase 2 gate:** fps ≥ current 33–35; page-switch latency ≤ current; RSS targets. **[H]** Phase 2 and Phase 3 both edit `MediaManager.py` — land Phase 2 first, rebase Phase 3 (they are otherwise independent).

---

## Phase 3 — Import & dependency diet  (branch `perf/import-diet`)

### P3.1 Replace matplotlib font machinery  **[H — weight mapping is mandatory]**
**Change:** `src/backend/font_resolver.py`:
- Resolution via fontconfig — **prefer ctypes `libfontconfig` over `fc-match` subprocess [H]** (same matcher, no PATH/binary dependency — flatpak binary presence was asserted, not verified; keep the subprocess as fallback). `functools.lru_cache(256)`.
- **Weight translation table (blocking) [H]:** the app's weights are numeric Pango/CSS 100–900 (`HelperMethods.py:340-353` → `KeyLabel.py:82-87`); fontconfig's scale is 0–215 (regular=80, bold=200). Verified live: `fc-match "DejaVu Sans:weight=400"` returns **DejaVuSans-Bold** — v1's template would have bolded every normal label. Ship the OT→fc mapping (`FcWeightFromOpenType`, ~10 lines) + a unit test asserting `resolve(family, 400, "normal")` is not the bold file. Styles pass through (`:style=italic` verified).
- **[H]** Port `font_name_from_path` (`HelperMethods.py:102-103`) via fontTools `name` table (fontTools is already a real dependency — see P3.4).
- `addfont` **is deleted, as fact [H]:** its only use (`KeyLabel.py:46`) feeds fc-match-discovered symbol fonts *back into matplotlib* — KeyLabel already shells out to fc-match today (`:39`); keep the `symb`/`unic` encoding logic (`:128`).
- `find_fallback_font` → lazy `fc-match sans` on first label render (removes the import-time scan at `globals.py:132`).
- `matplotlib.colors` in the StreamDeckUI importer → `Gdk.RGBA.parse` (**[H]** verified faithful incl. #RRGGBBAA and 4-digit hex).
- **macOS [H]:** `requirement_macos.txt` keeps matplotlib and `gl.IS_MAC` guards the resolver to a matplotlib fallback there — or declare macOS out of scope in the PR; decide explicitly, don't leave it implicit.
- Consistency note: fonts are picked via Gtk.FontButton → Pango description; Pango is fontconfig-backed on Linux, so picker↔renderer agreement *improves* vs matplotlib's scorer once the weight table is right.
**Accept:** label rendering pixel-identical for the fonts in use (A/B screenshots); no matplotlib in `sys.modules`; weight unit test green.
**Transition aid:** dev-only env flag that cross-checks resolver output against matplotlib and logs diffs (needs the weight table on both sides of the comparison).

### P3.2 Dead/accidental import removal (one commit)
As v1 (imageio, `cv2 exp`, `numpy isin`, `ast main`), plus: **[H]** Pyro5 stays *available* transitively via `streamcontroller-plugin-tools`; demote the `globals.py` import to `TYPE_CHECKING` after grepping installed plugins for `gl.pyro_daemon`, and drop it from the direct requirements list (resolving the v1 P3.2↔P3.4 contradiction).

### P3.3 Lazy edges
**Change:** function-level imports for `requests` and `cairosvg` in `HelperMethods.py` **and [H] `MediaManager.py:20`** (v1 missed it — MediaManager is on the `main.py:56` startup chain, so the v1 accept criterion was unsatisfiable); `dasbus` if `src/api.py` init is off the first-paint chain (check `app.py:53` ordering first).
**Accept [H]:** `-X importtime` over the real startup chain (main.py to window creation), not just `import globals`: neither requests nor cairosvg present.

### P3.4 opencv-headless + curated requirements  **[H — list corrected, ecosystem step added]**
**Keep-list corrections (all verified imports):** add **fonttools** (`KeyLabel.py:15` symbol-font detection — label rendering dies without it), **fuzzywuzzy** (9 call sites: search in Store/AssetManager/ActionChooser/PageSelector), **packaging** (5 call sites: migrators, version checks), **async-lru** (`StoreBackend.py:19`). Drop from the keep-list: pyudev, natsort (zero core imports — verify nothing dynamic, then remove). **websocket-client has zero core imports but stays as an annotated ecosystem pin** — HomeAssistantPlugin runtime-imports `websocket` and ships no runtime requirements.txt.
**Ecosystem gate (blocking) [H]:** plugin deps pip-install into the app venv (`StoreBackend.py:895-897`), and **OSPlugin imports matplotlib for graphs with no requirements.txt of its own** — dropping matplotlib without an upstream OSPlugin release breaks graph actions on fresh installs. Sequence: file/land OSPlugin (and HA) requirements.txt upstream → then remove matplotlib here; or keep matplotlib one release with a deprecation note. Add a pre-merge scan step: installed-plugin imports vs the drop list.
**Rest as v1:** opencv-python-headless (verified: zero highgui use in repo *and* plugins); `requirements-dev.txt` for memray/meson/req2flatpak; nltk/pymongo/textual/rich/pydantic confirmed freeze junk (no core or plugin imports).
**Accept:** fresh venv from the new file passes the harness + manual smoke (store, SVG icon, video bg, HA, **OSPlugin graph action**); `pip check` clean; req2flatpak manifest builds.

**Phase 3 gate:** fresh RSS at deck-idle −25–35MB vs Phase 1 baseline; startup −150ms+.

---

## Phase 4 — UI structure  (branch `feat/ui-mem`, after Phase 1)

### P4.1 Lazy GenerativeUI widget construction  **[H — rescoped]**
Reality check from review: the off-main crash fix **already landed** (`GenerativeUI.__init__` marshals via `run_on_main`, `GenerativeUI.py:61-63`) — this item's value is *memory laziness only*, not crash elimination. The base-class value/widget split largely exists (`get_value`/`set_value` are settings-only), but the build is a 10-subclass job: every subclass builds in `__init__`-time closures, subclass getters read widget state (`EntryRow.get_text`, `SwitchRow.get_active`, `ScaleRow.get_number`), and **ComboRow/ToggleRow override `load_initial_ui`/`reset_value` with direct widget access**.
**Change:** `_widget` lazy property (builds on first access; off-main access marshals via a **timeout-bounded** `run_on_main` + deprecation log — same exposure the current `__init__` build has, and P1.1's teardown never touches `_widget` when unbuilt); `load_initial_generative_ui` (`ActionCore.py:521-526`) becomes build-skipping (value-layer only) or it forces every build at on_ready anyway; rewrite the 10 subclasses to route reads through the value layer. Fold in bug 33 (`set_value` double settings-write, `GenerativeUI.py:198-199`). Long-term contract (factory-built at config-open, `.widget` invalid before) stated in the release notes as the eventual API; not enforced now because **AudioControl and HA touch `.widget` at `__init__` today** (verified: `SetDefaultDevice.py:25`, `SetVolume.py:56-57`, `parameter_combo_row.py:15`, `icon_action.py:41-44`).
**Accept [H — rescoped]:** 0 widgets built for actions that don't touch `.widget` (well-behaved plugins); `.widget`-touching plugins keep working (build forced, logged). Explicit check for design-doc bug 5's second half: stale rows are no longer re-parented by `ConfigGroup.load_for_action` after P1.1 empties the registry.

### P4.2 AssetManager reuse + recycling  **[H — two named prerequisites]**
Callback-swap verified viable (all tabs read `asset_manager.callback_func` at activation time), **but**: (a) remove the `destroy()` calls in `IconChooser.py:84-87` / WallpaperChooser post-selection (reuse dies otherwise); (b) CustomAssets fires its callback on a spawned thread that re-reads selection and `callback_func` *inside the thread* (`FlowBox.py:118-130`) — capture `(asset_path, callback, args)` before spawning, or a stale thread calls the *new* callback with the *new* window's selection. Then: singleton-present in `let_user_select_asset`; on close destroy + null both globals; CustomAssets tab → `DynamicFlowBox` recycler.

### P4.3 Store lazy pages
As v1 — verified easy (`notify::visible-child-name` already connected, `Store.py:75`).

### P4.4 Dead-code deletion
As v1 (headerBar.py, `Sidebar.dial_editor`, `mainWindow.py:335-376`, `media_player_tasks`, `close_image_ressources`).

---

## Phase 5 — Churn & allocator pressure  (branch `perf/churn`)

> **DISPOSITION (2026-07-07):** the RSS rationale for this phase is CLOSED — the overnight
> A/B (10.6h, `MALLOC_ARENA_MAX=2` + default-on idle trim) measured idle slope +0.23MB/h,
> swap 0, trims 0-3ms with post-burst high-water reclaim. Landed on CPU/latency rationale:
> P5.2 app-side (q90 touchscreen encode), P5.3 (timer wheel), P5.4 (hidden-window dirty
> markers). NOT done, by decision: **P5.1** (identity dedup — rewrites the load-bearing
> dual-hash guard for fps only visible at XL scale) and **P5.2's library half** (chunk-buffer
> reuse — needs a streamcontroller-streamdeck fork release for a benefit the allocator data
> no longer supports). Re-open triggers: an XL-class deck enters use; fps target raised
> above 35; sustained trayed operation becomes the norm; or telemetry shows swap regrowth
> (allocator containment stopped holding).

**Gate [H — split, v1's single gate conflated two rationales]:** the *RSS-motivated* parts (allocation churn → fragmentation) are gated on Phase 0's A/B showing arenas are **not** already tamed by `MALLOC_ARENA_MAX`. The *CPU/latency-motivated* parts (P5.1's hash cost ≈ measured profiler "hash" bucket; P5.2's q90 strip = smaller largest-single-write → dial latency) stand on the fps/dial harness regardless of the allocator outcome.

### P5.1 Passthrough dedup by content identity  **[H — id() scheme rejected, redesigned]**
v1 keyed the skip on `id(frame_entry)` — unsafe: per-tick tiles die within a tick and CPython reuses freed addresses for same-type objects, so `id(new_tile) == id(old_tile)` with different pixels → false skip → **frozen key**, the exact bug class the dual-hash guard exists to prevent. (v1's two docs also disagreed on which object gets id()'d.)
**Change:** key on `(background_epoch, frame_index, key_index)` — `background_epoch` from a monotonic `itertools.count()` assigned in `Background.__init__`/`set_video` (never reused), `frame_index` = the cache's active frame. Deterministic decode from the same cache mp4 ⇒ same (epoch, frame) ⇒ same pixels; loop wrap repeats the index with identical content (correct skip); epoch bump on any media change forces repaint. **The same tuple (plus rotation) must also replace the `encode_memo` key and the present-side dedup value** (`(img_hash, rotation)` at `DeckController.py:2889`, `add_image_task` at `:2908`) — otherwise `tobytes()` still runs for the memo and nothing is saved. Tuple keys are type-distinct from int pixel-hashes, so passthrough↔composited transitions force a repaint (fail-safe direction). Pixel-hash path stays for composited keys.
**Then, separate commit, own full regression run:** remove `get_next_tiles`' per-frame `tile.copy()` (`DeckController.py:1840`) treating cache tiles as immutable — note `get_tiles`' error path returns the *previous* tile list (`background_video_cache.py:174-177`); the epoch/frame key handles repeats correctly where `id()` could not.
**Verify:** all tests/ scenarios (this touches the cross-page-bleed guard family) + hardware page-switch storm; fps expect +1–3 at XL, neutral SD+.

### P5.2 streamdeck library: buffer reuse + q90 strip  **[H — scope corrected]**
The library is **pip-installed** (`streamcontroller-streamdeck==0.1.5`), not vendored — edits land in the fork repo, released + pinned (couples to P3.4's requirements rewrite; sequence the pin bump). Two copies to eliminate, not one: per-chunk `bytes(header)+image[slice]` in `StreamDeckPlus.py:434-489` (per-device reusable buffer, memoryview slices) **and** the transport-side `bytes(data)` copy in `LibUSBHIDAPI.py:361` → `(c_char*len).from_buffer(buf)` (safe: `hid_write` is synchronous, verified argtypes/copy semantics). Preserve per-chunk `Device.mutex`/`Library.mutex` acquisition — it is the reader-fairness mechanism from the dial-starvation fix; BetterDeck holds its RLock across whole writes and overrides nothing below (verified).
App-side: `encode_native_touchscreen` at q90 replacing the PILHelper q100 path (`DeckController.py:3298`).
**Accept/Verify [H — added]:** allocation-rate delta via tracemalloc snapshot in the profiler; dial-latency capture scenario; strip visual A/B.

### P5.3 Single timer wheel
One `sched`-based scheduler thread; port screensaver reset-per-keypress Timer churn, overlay hide, hold timer. **Accept [H — added]:** keypress storm (50 presses) spawns 0 threads; timer behavior unchanged (screensaver still fires at configured delay ±1s).

### P5.4 Hidden-window dirty markers
`ui_image_changes_while_hidden` → dirty flags; recomposite on map; fix the touchscreen entry never popped (bug 48). **Accept [H — added]:** hidden-window bg-video: telemetry shows no per-frame PIL retention; map after 1h hidden repaints all inputs correctly (KeyGrid + ScreenBar).

---

## Cross-cutting

**Verification matrix (every phase):** all scenarios in `tests/` (**[H]** count grows: P2.1 ports `scenario_keyvideo_build` + `scenario_display_saturation`; don't hardcode "16"), fps harness (loop_fps ≥30, target no regression from 33–35, **p99 frame gap** for tail-sensitive items), telemetry soak (P0.1+P0.6), hardware unplug/replug ×20 (incl. mid-screensaver), plugin sweep (HA, MediaPlugin, OSPlugin **graph action**, AudioControl, Spotify, VolumeMixer).

**Commit/PR structure:** phases as separate PRs in order 0 → 1 → 2 → 3 → 4 → 5 (**[H]** 2 before 3: both touch MediaManager; 3 rebases). One commit per work item, prefix `mem:`.

**Rollback:** unchanged from v1 (per-phase revertability; `SC_STRONG_CALLBACKS=1`; font resolver behind one module boundary; P2 keeps background cache format/dir compatible).

**Tracked but unscheduled (explicit, so nothing silently drops) [H]:** bug 31 (`inspect.stack()` per plugin log line + leftover `print`), bug 36 (AssetManagerBackend iterate-while-mutate + double startup rewrite), bug 39 (`move_page` duplicate Pages), bugs 42/43/46 (shared lru_cache dict, `show_donate()` call-not-append, mutable default), bugs 25/26/30/32/37/40/41/44/45/47/50/51 (correctness, non-memory), bug 52 (fork-with-threads `run_command` — file a tracking issue; real hazard, separate effort), bug 48's `image2pixbuf` cosmetic half. Bug 34 withdrawn (reuse path is live — stale FIXME comment; see P1.4).

**Non-goals:** unchanged (out-of-process backends, sqlite registry, sampled pixel hashing, lazy plugin import as its own future effort, PangoCairo label rendering — recorded as the long-term fidelity direction, incompatible with this plan's pixel-identical acceptance).
