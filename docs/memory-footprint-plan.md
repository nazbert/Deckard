# Memory Footprint Reduction — Design & Implementation Plan

**Date:** 2026-07-06 · **Branch audited:** `local-integration` (clean, post single-writer merge)
**Method:** six parallel deep audits (media pipeline, GTK UI, plugin/action system, pages/assets/settings, device comm/threads, dependency/import measurement) + live inspection of the running process (PID 302014).

---

## 1. Executive summary

The app's memory story has three distinct problems, and they need three different treatments:

1. **A large fixed baseline (~150–200MB before any content):** ~119MB of third-party imports at startup (matplotlib loaded for three font-lookup helpers; cv2; eager import of everything), plus an uncapped OpenCV worker pool (32 threads on this machine) that multiplies glibc malloc arenas.
2. **Content-scalable raw-pixel caches:** the background-video fix (canvas-mp4 re-encode) was never ported to **key/dial videos** (`VideoFrameCache` holds every decoded frame as raw PIL — 14–155MB per key video, and the user's cache opt-out is dead code) or **GIFs** (all frames kept at *source* resolution RGBA — a 500px/200-frame GIF ≈ 200MB on one key). Static media (`InputImage`, `BackgroundImage`) is retained at source resolution forever (4K wallpaper = 33MB for a ≤1MB canvas).
3. **Lifecycle leaks — unbounded over events, not time:** every USB unplug/replug leaks the entire dead `DeckController` graph (2–70MB+ per cycle); GenerativeUI rows, context-menu popovers, and plugin event-observer registrations accumulate forever; several teardown paths bypass `clean_up()` entirely.

**Live evidence that this matters:** the currently running instance is at **788MB RSS + 463MB swap ≈ 1.25GB total anonymous** after 13h42m, versus the ~513MB/0-swap steady state measured right after the video-cache fix. VmData is 6.1GB across 71 threads, with multiple ~57MB dirty glibc arena regions — allocator fragmentation amplified by thread count is a first-class contributor, not a footnote.

**Realistic targets** (single SD+ with background video, typical plugin set):

| Milestone | Steady-state RSS | Long-session behavior |
|---|---|---|
| Today | ~513MB fresh → 1.25GB total after 14h | grows (leaks + fragmentation + swap) |
| Phase 0 (env/thread caps) | ~400–450MB | fragmentation strongly reduced |
| Phase 1 (lifecycle) | unchanged fresh | **flat** over days; unplug/replug free |
| Phases 2–3 (media + import diet) | ~300–350MB | flat |
| Phase 4–5 (UI + churn) | ~280–330MB | flat, lower allocator pressure |

---

## 2. Measured state of the live process (2026-07-06, uptime 13h42m)

- `VmRSS` 788MB, `VmSwap` 463MB, `Pss_Anon` 606MB, VmData 6.1GB, 71 threads, 165 fds.
- Largest private-dirty mappings: 79MB main heap + anonymous regions of 61, 59, 57.6×3, 33, 32MB — the 57.6MB triplets are classic glibc per-thread arena shapes.
- **32 threads named `background_0`, all created in the same second at startup.** These are *not* Python threads: Python ≥3.13 sets the OS thread name (comm), and threads spawned by C libraries inherit the creating thread's comm. First cv2 call ran on a GtkHelper `background` pool worker → OpenCV's global `cv::parallel_for_` pool (sized to nproc = 32) inherited the name. **Nothing in the app calls `cv2.setNumThreads()`.**
- Uncapped FFmpeg decode threads: `VideoCapture` at `src/backend/MediaManager.py:73` and `src/backend/DeckManagement/Subclasses/key_video_cache.py:33` set no `CAP_PROP_N_THREADS` (the background-video cache correctly caps at 1/4). On 32 cores, each uncapped open spawns ~32 frame-threads, each holding decoder state + a full decoded frame.
- Short-term drift ~1MB/h at idle; the 14h growth to 1.25GB is therefore mostly episodic (page loads, UI opens, screensaver cycles) plus fragmentation — consistent with the leak inventory in §4.
- Import cost measured in the venv (Python 3.14.6): bare interpreter 17.7MB → `import globals` alone **+97.8MB**. Marginal stacked costs: gi/Gtk4+Adw 45.3MB, cv2 34.1MB, matplotlib.font_manager 13.8MB, cairosvg 7.7MB, requests 5.9MB, PIL 5.6MB → **~119MB in imports before any object is created**.

---

## 3. Where the memory goes — complete taxonomy

### 3.1 Fixed baseline (paid at startup, every run)

| Item | Cost | Source |
|---|---|---|
| GTK4/Adw/gi | 45MB | unavoidable |
| cv2 (+numpy) | 34MB | needed for video; headless variant saves ~5–10MB |
| **matplotlib.font_manager** | **14MB** | `HelperMethods.py:22`, `KeyLabel.py:18` — used *only* for `findfont`/`findSystemFonts`/`addfont`; also a full font scan at import time via `globals.py:135` |
| cairosvg + cairocffi | 8MB | eager in `HelperMethods.py:27` |
| requests | 6MB | eager in `HelperMethods.py:26` (on the `import globals` chain!) |
| misc (loguru, Pyro5, dasbus, evdev, rpyc, imageio) | ~8MB | Pyro5 used only for a type annotation (`globals.py:121`); `import imageio` in `MediaManager.py:19` is **dead** |
| OpenCV parallel pool | 32 idle threads + arena multiplication | no `cv2.setNumThreads` call anywhere |
| Plugin module trees | tens of MB (plugin-dependent) | all `plugins.*.main` imported eagerly at startup (`PluginManager.py:64-80`) |

### 3.2 Content-scalable pixel memory

| Structure | Behavior | Scale |
|---|---|---|
| `VideoFrameCache.cache` (`key_video_cache.py:35`) | one raw PIL RGB per frame, no eviction, eager full decode at page load; **opt-out dead** (`:60` overwrites the setting) | 14MB (72px/30s) → 155MB (120px/2min) per key video |
| `KeyGIF.frames` (`DeckController.py:1861-1873`) | all frames, **source resolution**, RGBA | 41–200MB per GIF key |
| `InputImage.image` (`KeyImage.py:36-49`) | source-res RGBA retained per key/state forever | tens of MB for photos |
| `BackgroundImage.image` (`DeckController.py:1701-1716`) | source-res retained for page lifetime | 33MB for 4K |
| `EncodedImageCache` (`encoded_image_cache.py`) | correct 32MB LRU per deck; **thrashes at 0% hit** on high-entropy bg video | ≤32MB × decks |
| Background video path (post-fix) | disk-backed mp4, ~1–2 canvas frames in RAM | healthy — the model to copy |

### 3.3 Lifecycle leaks (unbounded over events)

1. **Dead `DeckController` graph per unplug/replug** — three independent retention roots, all must be fixed:
   - `gl.page_manager.pages` keyed by controller object, never purged (`PageManagerBackend.py:46`; `DeckManager.remove_controller` `DeckManager.py:216-225`); the dead controller's `active_page` is *permanently unevictable* (`PageManagerBackend.py:131`) and still counts toward the eviction budget, evicting live controllers' pages early.
   - `DeckStack.deck_attributes` keyed by controller, never cleaned (`DeckStack.py:52`, `remove_page` `:116-140`).
   - `DeckController.delete()` (`DeckController.py:1489-1507`) zeroes `active_page.action_objects` **without** `clean_up()` → SignalManager keeps bound methods → action → controller. Also: no screensaver `timer.cancel()` (pending Timer pins the controller then fires `show()` on a dead deck, opening a cv2 capture that's never closed), no `deck.close()`, no background-video `close()`, no input `close_resources()`.
   - Cost: 2–5MB/cycle static, 30–70MB+ with video content, plus open FDs and cv2 captures.
2. **GenerativeUI rows**: self-register in `GenerativeUI.__init__` (`GenerativeUI.py:67`) onto `action.generative_ui_objects`; `destroy()` exists but has **zero framework callers**; `ActionCore.clean_up()` never touches them. Row-rebuilding plugins accumulate full Adw row trees per rebuild. ~4–14MB steady, unbounded dynamic.
3. **Plugin event registries**: `EventHolder.observers` and AssetManager-plugin `Observer.observers` are plugin-lifetime and **never deregistered on action teardown**; every page reload rebuilds actions (the reuse path is dead code, `Page.py:217-221` `#FIXME`) and appends new bound methods. **This is the dominant steady-state growth mechanism** for event-using plugins (e.g., AudioControl's PulseEvent).
4. **Context-menu popovers**: one leaked `PopoverMenu` per right-click, forever (`KeyGrid.py:346-348, 522, 543-545`; same in `DialBox.py:238-247`).
5. **Plugin uninstall**: `remove_plugin_action_objects` drops actions without `clean_up` (`Page.py:353-371`); registrations pin the purged module tree despite the `sys.modules` purge (which also uses the wrong key when manifest id ≠ folder name, `StoreBackend.py:969-972` vs `PluginManager.py:71-75`).
6. **Fake-deck removal never calls `delete()`** (`DeckManager.py:163-172`): media/tick threads + executor run forever.
7. **Per-page-switch throwaway `ThreadPoolExecutor()`** (`DeckController.py:1133`): a wedged plugin callback strands non-daemon workers per switch, linearly.
8. **ScreenSaver stash**: `show()` pins the entire previous input set (incl. video caches — 50–150MB) for the screensaver's duration, then discards it without `close_resources()` (`ScreenSaver.py:149-151, 219`).

### 3.4 Allocation churn → fragmentation (the swap story)

Per background-video frame, the same pixels are materialized ~8–10× (cache decode → tile copies ×3 → label copy → `tobytes()` hash → encode canvas → UI RGBA/`GLib.Bytes`/pixbuf). At XL scale the `tobytes()` dedup hash alone is ~35MB/s of transient allocation; the vendored HID write path adds ~10–15MB/s of per-chunk copies. None of it leaks — but spread across 71 threads it drives glibc arena growth, fragmentation, and eventually the 463MB of swapped-out dirty pages observed live.

### 3.5 Dependency hygiene (disk, not RSS)

`requirements.txt` is a pip freeze: nltk (43MB if ever imported), pymongo, textual, rich, pydantic, memray, pipenv, meson, patchelf etc. are not app dependencies. Three overlapping video stacks are declared (cv2, imageio+imageio-ffmpeg, get-video-properties/ffprobe); only cv2 is exercised.

---

## 4. Design decisions

**D1 — One deterministic teardown seam per lifetime.** Every owning object gets a single `close()`/`clean_up()` that the *framework* calls at every drop site; plugin hooks (`on_removed_from_cache`, `on_remove`) become pure notifications invoked *by* the framework teardown, never the teardown itself (today plugins override them without `super()` and silently disable cleanup — OSPlugin `GraphBase.py:184`, MediaPlugin `main.py:1241/1249`). This one principle closes leak classes 1, 2, 5, and 6 above.

**D2 — Weak registries for cross-lifetime subscriptions.** `SignalManager`, `EventHolder`, and the AssetManager `Observer` hold `weakref.WeakMethod` for bound-method callbacks (strong ref fallback for free functions, preserving plugin compat). Fixes observer growth *without requiring plugin updates*.

**D3 — One disk-backed video cache implementation.** Factor `BackgroundVideoCache`'s canvas-mp4 design into a shared `TileVideoCache` and use it for key videos, dial videos, and GIFs (cv2 decodes GIFs). RAM becomes O(1 frame) per animated asset; the JPEG-per-frame disk format and eager decode-all disappear.

**D4 — Decode/retain at display size.** No source-resolution pixels retained anywhere: `InputImage`/`BackgroundImage` pre-fit to ≤2× target at construction and drop the source. (UI thumbnails already do this correctly.)

**D5 — Bounded native parallelism.** `cv2.setNumThreads(2)` at startup, `CAP_PROP_N_THREADS` (1–2) on every `VideoCapture`, `MALLOC_ARENA_MAX=2` in the launch environment. The measured video-bg loop is PIL/encode-bound, not cv2-parallel-bound, so this does not threaten the 33–35fps result — verify with the tests/ harness.

**D6 — Lazy by default at the edges.** Imports that aren't needed to paint the first frame (requests, cairosvg, dasbus, store/UI-only cv2 uses) move into functions; matplotlib is removed outright (fontconfig via Pango — already imported — or `fc-match` subprocess). Plugin `main` import stays eager for now (action index needs it) with lazy plugin import as a stretch goal gated on a manifest schema change.

**Rejected:** uniform out-of-process plugin backends (adds 30–80MB *per plugin*; in-process is the memory-cheap model — keep subprocess opt-in); sqlite/mmap asset registry (metadata is ~1KB/asset); sampled pixel hashing for dedup (correctness risk on 1px changes; this code has bled before).

---

## 5. Implementation plan

Phases are ordered by (impact × confidence) / risk. Each lands as its own topic branch off `local-integration`, hardware-verified with the tests/ harness (16 scenarios) plus the phase-specific checks listed.

### Phase 0 — Measure, cap, and quantify (½ day, no behavior change)

1. **RSS/thread/arena telemetry**: extend the env-gated profiler to log `smaps_rollup` + thread count every N minutes so every later phase has before/after data.
2. `cv2.setNumThreads(2)` in `main.py` before any cv2 use; add `CAP_PROP_N_THREADS=1` to `MediaManager.py:73` and `key_video_cache.py:33`.
3. Launch env: `MALLOC_ARENA_MAX=2` (and try `MALLOC_TRIM_THRESHOLD_=131072`); A/B overnight run against baseline.
4. Optional experiment: `malloc_trim(0)` via ctypes after `load_page`'s existing `gc.collect()`.
   - *Expected:* −30 threads immediately; fragmentation/swap reduction of 100–300MB over long sessions (to be confirmed by the A/B — this is the cheapest possibly-huge win). *Risk:* near zero; fps harness re-run guards D5.

### Phase 1 — Lifecycle correctness (2–4 days) ← biggest long-session win

1. **`DeckController.close()` sweep** (fixes deck-comm B1/B2/B4/B8/B12 as a unit): cancel screensaver timer; `ClearAndCloseMsg` through the writer (keeps single-writer ownership of last device ops); framework `clean_up()` on the active page's actions; `background` image/video `close()`; per-state `close_resources()`; `encode_memo.clear()`; `deck.close()` after writer join; then purge `gl.page_manager.pages[self]` and `DeckStack.deck_attributes`. Do **not** take `_load_page_lock` inside (run_on_main deadlock class — see branch-review history).
2. **Framework-owned action teardown (D1)**: call `action.clean_up()` at every drop site (`remove_plugin_action_objects`, `DeckController.delete/close`, `MissingRow`, cache eviction already does); `clean_up()` additionally destroys `generative_ui_objects` (unparent-only semantics per the `destroy()` docstring) and releases backends.
3. **Weak registries (D2)** in SignalManager (+ a lock — it's mutated cross-thread with no synchronization today, `SignalManager.py:50-54`), EventHolder, Observer.
4. **Popover fix**: `closed` → idle `unparent()` in `KeyButtonContextMenu` and `DialContextMenu` (or one reusable menu per grid).
5. Fake-deck removal calls `delete()`; `load_all_inputs` uses a persistent bounded per-deck loader pool instead of a throwaway executor; honor `performance.n-cached-pages` in `PageManagerBackend.__init__` (and fix its ±1 semantics).
6. Fix `key_video_cache.py:60` (`do_caching` overwrite) so the existing RAM/disk opt-out works again.
   - *Expected:* long-session growth → flat; unplug/replug becomes free; stops 2–70MB/cycle + FD leaks. *Risk:* medium — teardown ordering vs in-flight plugin callbacks; mitigations: bounded joins (already exist), hooks-before-detach ordering per `clear_action_objects` comments, hardware test unplug/replug ×20 with bg video playing.

### Phase 2 — Media memory redesign (3–5 days)

1. **`TileVideoCache` (D3)**: shared per `(video_md5, size, saturation)`, mp4-backed, `CAP_PROP_N_THREADS=1`, replaces `VideoFrameCache` internals and the GIF frame list; wall-clock frame-pick logic in `InputVideo` carries over. Delete the JPEG-per-frame disk format (sweeper migration cleans old dirs).
2. **Downsample-at-load (D4)** for `InputImage` and `BackgroundImage` (cap ≤2× target; free source). Memoize the touchscreen background fit that currently re-opens the file from disk per composite (`DeckController.py:3617-3621`).
3. **Encode-memo admission control**: track hit rate per window; stop inserting video-frame encodes when observed hit rate ≈0 over N puts (gate on observed hits, not content type, to protect the looping-video hit path that feeds the 33–35fps result).
4. ScreenSaver: release/close the stashed input set at `show()` (it is never restored — `hide()` rebuilds via `load_page`).
   - *Expected:* key-video/GIF pages drop from O(content) to O(1) — tens to hundreds of MB for affected users; 4K bg 33MB → ~1MB; up to 32MB/deck reclaimed on high-entropy video. *Risk:* low-medium; per-key decode throughput is thousands of fps at ≤200px, but verify page-switch latency (eager decode-all previously hid seek costs) and dial-video smoothness on hardware.

### Phase 3 — Import & dependency diet (1–2 days)

1. Remove matplotlib: font resolution via Pango/fontconfig (already imported) or `fc-match`; port `find_fallback_font` (and stop doing a full font scan at *import time* of `globals.py`).
2. Delete dead/accidental imports: `import imageio` (`MediaManager.py:19`), `from cv2 import exp` (`ActionConfigurator.py:16`), `from numpy import isin` (`Page.py:28-34`), `from ast import main` (`app.py:16`); Pyro5 behind `TYPE_CHECKING` (verify no plugin touches `gl.pyro_daemon` first).
3. Lazy-import requests, cairosvg (function-level in `HelperMethods.py` / `MediaManager.py`), dasbus (api.py path).
4. `opencv-python` → `opencv-python-headless` (no highgui usage in repo); rewrite `requirements.txt` as a curated dependency list (drop nltk/pymongo/textual/rich/pydantic/dev-tools); remove imageio/imageio-ffmpeg.
5. Stretch (separate decision): lazy plugin import keyed off a manifest-declared action index — tens of MB for many-plugin users, but a plugin-API/manifest change; defer until D1/D2 have landed and the ecosystem-facing changes can be batched.
   - *Expected:* −25–35MB fresh RSS, ~200ms faster startup. *Risk:* low; font-rendering parity needs visual verification across the installed label fonts.

### Phase 4 — UI structural (2–3 days)

1. **Lazy GenerativeUI construction**: rows factory-built when `ConfigGroup.load_for_action` runs, not eagerly at `on_ready` for every action on every cached page. Also removes the root of the off-main-thread GTK construction crash class (construction moves to the main thread at config-open time — supersedes the "Option D" run_on_main wrapper). Needs a compat shim in `GenerativeUI.__init__` deferring widget build; plugin-visible, so batch with the Phase 3.5/D2 ecosystem notes.
2. AssetManager: reuse/present the existing instance in `let_user_select_asset` (today each call constructs a new window and orphans any open one); convert the Custom Assets tab to the existing `DynamicFlowBox` recycler (eager `AssetPreview` per asset ≈ 17MB/open at 100 assets).
3. Store: lazy-build catalog pages on first tab switch (`Store.py:86-95`).
4. Delete dead code with trap patterns: `headerBar.py` HeaderBar, `Sidebar.dial_editor`, `mainWindow.PageManagerNavPage`, legacy `media_player_tasks` queue, `close_image_ressources` (replaced by Phase 1's real sweep).
   - *Expected:* 5–20MB steady + removal of the eager-build crash class. *Risk:* medium on gen-UI (public API); low elsewhere.

### Phase 5 — Churn & allocator pressure (opportunistic, perf-adjacent)

1. Passthrough keys: skip `tobytes()` hashing when the frame entry object is identical (`id(frame_entries), key_index`), falling back to pixel hash otherwise; stop `get_next_tiles` copying tiles if treated as immutable. (~35MB/s + ~1.4MB/s churn at scale; keep dual-hash enqueue/present semantics intact — they're load-bearing.)
2. Vendored streamdeck lib: reuse one per-device chunk buffer (`memmove` + `memoryview` slices) in `set_key_image`/touchscreen writes; keep per-chunk mutex semantics so BetterDeck reader-fairness yields still work. Add `encode_native_touchscreen` at q90 (strip currently encodes at PILHelper's hardcoded q100 — the largest single write in the system).
3. Single timer scheduler for hold/overlay/screensaver timers (thread-per-`threading.Timer` today, recreated per keypress).
4. Skip UI mirroring composites entirely while hidden (store a dirty marker; recomposite on map).

### Sequencing & verification

- Order: 0 → 1 → 2 → 3 → 4 → 5. Phases 2/3 are independent of each other and can be parallel branches; both depend on Phase 0's telemetry to prove their wins.
- Every phase: run tests/run_all.py (16 scenarios), the fps harness (loop_fps must stay ≥30), and a scripted soak: 50 page switches + 20 unplug/replug + screensaver cycles + 2h idle, comparing RSS/swap/thread-count telemetry against the previous phase.
- The A/B for Phase 0's `MALLOC_ARENA_MAX` decides how much of §3.4 is worth Phase 5's engineering; if arenas account for most of the fragmentation, Phase 5 items 1–2 drop in priority.

---

## 6. Bug appendix — everything found along the way

Bugs are grouped; **bold** = memory-relevant. File:line references are to `local-integration` @ audit date.

### A. Teardown / leak bugs (fixed by Phase 1 design)
1. **`gl.page_manager.pages` never purged on controller removal** (`PageManagerBackend.py:46`, `DeckManager.py:216-225`); dead controller's `active_page` unevictable and distorts the eviction budget (`PageManagerBackend.py:118, 131`).
2. **`DeckStack.deck_attributes` never cleaned** (`DeckStack.py:52, 116-140`).
3. **`DeckController.delete()` skips `clean_up()`** on actions (`DeckController.py:1489-1492`), never cancels the screensaver Timer, never closes the HID handle, background video, or input resources, never drains slots (`:1489-1507`).
4. **Fake-deck removal never calls `delete()`** (`DeckManager.py:163-172`) — threads run forever.
5. **GenerativeUI `destroy()` has zero framework callers** (`GenerativeUI.py:67, 270-286`; `ActionCore.py:599-613`); stale rows also get re-parented and re-fired by `ConfigGroup.load_for_action` (`ActionConfigurator.py:164-208`) and `load_initial_generative_ui` (`ActionCore.py:521-526`).
6. **`EventHolder`/`Observer` observers never deregistered on action teardown** (`PluginBase.py:310-326`; `PluginSettings/Manager.py:84-85`); bound-method dedupe can't work across recreations (`EventHolder.py:19`).
7. **Plugin uninstall drops actions without teardown** (`Page.py:353-371`; `del action` at `:366` is a no-op); `sys.modules` purge uses `plugins.{plugin_id}` but import key is `plugins.{folder}.main` (`StoreBackend.py:969-972`).
8. **Plugins overriding `on_removed_from_cache`/`on_remove` without super() disable all framework cleanup** (OSPlugin `GraphBase.py:184`, MediaPlugin `main.py:1241/1249`) — design flaw in the hook contract.
9. **Context-menu popovers leak per right-click** (`KeyGrid.py:346-348, 522, 543-545`; `DialBox.py:238-247`).
10. **ScreenSaver `show()` pins the previous input set for the whole screensaver duration, `hide()` discards without closing** (`ScreenSaver.py:149-151, 219`); screensaver cv2 capture never closed (`:122-124`).
11. **Throwaway `ThreadPoolExecutor()` per page load strands workers under a wedged plugin** (`DeckController.py:1133, 1154`).
12. **`control_q` accepts messages after writer stop** (`DeckController.py:379-383`) — minor unbounded edge.
13. `app_loading_finished_tasks` never cleared after execution (`app.py:115-117`); `api_page_requests` entries read but never deleted (`DeckController.py:969-970`). Tiny.
14. AssetManager `on_close` clears `gl.asset_manager` but not `gl.app.asset_manager` (`AssetManager.py:62-63`); `let_user_select_asset` orphans an already-open window (`app.py:138-141`).

### B. Media pipeline bugs
15. **`key_video_cache.py:60` — `self.do_caching = True` unconditionally overwrites the `performance.cache-videos` setting** read on the previous line; the RAM/disk opt-out is dead.
16. **Uncapped FFmpeg threads**: `MediaManager.py:73`, `key_video_cache.py:33` (no `CAP_PROP_N_THREADS`); **no `cv2.setNumThreads()` anywhere** → 32-thread OpenCV pool on this machine (observed live).
17. **Early decode failure never clamps `n_frames`** (`key_video_cache.py:91-95`): cache can never complete, capture never released, one doomed `cap.read()` per tick thereafter.
18. **`ControllerKeyState.set_video` doesn't close the previous video** (`DeckController.py:3804-3808`, asymmetric with `set_image`); `InputVideo` has no `close()` override at all (`SingleKeyAsset.py:39-40`) — video teardown is refcount-luck.
19. **`close_image_ressources` is dead *and* broken** (`DeckController.py:1194-1204`): zero callers; would raise `AttributeError` (inputs have no `close_resources`; `BackgroundImage` has no `close`).
20. `ControllerTouchScreenState.close_resources` dereferences possibly-unset `self.current_image` (`DeckController.py:3716-3718`).
21. **Overlay passthrough regression**: `hide_overlay()` sets `_overlay = False` but the fast path tests `is None` (`DeckController.py:2467` vs `:2990`) — after one error-overlay cycle the key recomposites every bg-video frame forever (erodes the 33–35fps win).
22. Suspected: `svg_to_pil(path, 192)` passes only `width`; height stays 96 and both go to `svg2png(output_width, output_height)` → distorted non-square renders (`HelperMethods.py:417`; callers `DeckController.py:3178, 3535`, `MediaManager.py:83` at 1024 wide). Verify at runtime.
23. `MediaManager.get_thumbnail` cached branch leaks the open file handle (`MediaManager.py:52-54`); `generate_video_thumbnail` crashes on unreadable video (`frame=None` → `cvtColor`, `:72-80`); dial image `Image.open` without context manager (`DeckController.py:3529-3532`).
24. Cosmetic: redundant `.copy()` of fresh alpha key (`DeckController.py:3020`); double-close of same object (`:3067-3069`); throwaway state object per `get_active_state` miss (`:2749-2751`); touchscreen strip encoded at q100 via PILHelper (`DeckController.py:3298`, `PILHelper.py:51` — which also mutates the caller's image in-place).

### C. Plugin/action system bugs
25. **`PluginManager.get_plugins(include_disabled=True)` mutates the live class registry** (`PluginManager.py:140-146`) — first `get_plugin_by_id` call permanently merges disabled plugins into `PluginBase.plugins`.
26. `PluginBase.connect_to_event` looks up `event_holders[event_id]` instead of `full_id` (`PluginBase.py:321-324`) — `KeyError` whenever `event_id_suffix` is used.
27. `Observer.notify` closes event loops it doesn't own (`Observer.py:22-34`); `EventHolder.trigger_event` builds a new asyncio loop (+ its default executor threads) per trigger (`EventHolder.py:28-50`) — churn amplifier on high-frequency events.
28. **`SignalManager` has no lock**; `trigger_signal` iterates the live list while any thread mutates it (`SignalManager.py:50-54`).
29. `MissingRow.on_remove_click` deletes the entire input subtree for one action (`MissingRow.py:126`), no lifecycle calls.
30. `ActionManager.on_click_remove` dead-broken (`ActionManager.py:578-604`): wrong attr, wrong arity, nonexistent kwarg — TypeErrors if triggered.
31. `Logger.add_sink` filter contains a leftover `print(record)` (`src/backend/Logger.py:61`); plugin log methods run `inspect.stack()[1]` per line (`:43`) — full stack walk per plugin log call.
32. `wait_for_backend` waits max 0.3s (`ActionCore.py:573-576`) — slow backends yield `backend is None`.
33. `GenerativeUI.set_value` writes settings twice (`GenerativeUI.py:198-199`).
34. Dead action-reuse path `#FIXME: gets never used` (`Page.py:217-221`) — every reload rebuilds all action objects (churn + observer growth amplifier).

### D. Pages / assets / settings bugs
35. **`performance.n-cached-pages` never applied at startup** — only when the Settings window opens (`Settings.py:681-692`; `PageManagerBackend.__init__` defaults 3); ±1 semantics mismatch (`PageManagerBackend.py:183`).
36. `AssetManagerBackend.remove_invalid_data` mutates the list while iterating (skips entries, `AssetManagerBackend.py:229-234`); `add()` `UnboundLocalError` when file already in cache dir (`:73-76`); `Assets.json` rewritten twice on every startup even when clean (`:227, 234`).
37. `Page.move_key_to_end` ignores its `dictionary` argument and mutates `self.dict` (`Page.py:146-149`).
38. `Page.remove_plugin_actions_from_json` broken and live from plugin uninstall (`Page.py:406-415`): `action.id` on a dict, mutates list during enumeration, unguarded `self.dict[type]`.
39. `PageManagerBackend.move_page` instantiates the page for every controller and never re-keys the cache entry after mutating `json_path` → duplicate `Page` objects later (`PageManagerBackend.py:195-204`).
40. `remove_old_backups` off-by-one keeps 4 not 5 (`PageManagerBackend.py:491-495`).
41. `StoreBackend.api_cache`/`manifest_cache`/`attribution_cache` never initialized — `AttributeError` if their code paths are ever reached (`StoreBackend.py:293-314, 669-688`, currently dormant).
42. `get_app_settings` `@lru_cache` returns a shared mutable dict — mutations bleed globally (`SettingsManager.py:91-98`); dead `settings == None` branches (`:69, 95`).

### E. UI bugs
43. `app.py:97` — `on_finished.append(self.show_donate())` *calls* it and appends `None`; tasks appended there can never run (list already drained in `MainWindow.__init__`, `mainWindow.py:159`).
44. `ActionRow.update_comment`/`set_comment` paths both reference nonexistent attributes (`ActionManager.py:258-266, 669-678`).
45. Reorder buttons crash at list boundaries: `isinstance(x, AddActionButtonRow.button)` on an instance-only attribute (`ActionManager.py:561-573`).
46. **`MultiDeckSelectorRow` mutable default argument, mutated** (`MultiDeckSelectorRow.py:28, 70-78`) — cross-row state corruption.
47. `EventAssignerRow.select_event` fragile None-sentinel ordering (`ActionConfigurator.py:478-497`).
48. `image2pixbuf` hardcodes `force_transparency=True`, 3-channel path dead (`ImageHelpers.py:126-155`); touchscreen entry of `ui_image_changes_while_hidden` may never be popped on remap (only `Input.Key` handled, `KeyGrid.py:90-99`) — verify.
49. Dead code with trap patterns: `headerBar.py` (incl. `os._exit(0)` quit), `Sidebar.dial_editor` (built, never shown, `Sidebar.py:83-84, 169-171`), `mainWindow.py:335-376`.

### F. Concurrency / device bugs (non-memory)
50. `add_newly_connected_deck` appends without `_controllers_lock` (`DeckManager.py:249`) — racing connects can double-instantiate; the failure path closes the shared handle (`DeckController.py:661-669`).
51. Flatpak disconnect poll runs `hid_enumerate` under the transport mutexes shared with writes every 2s per controller (`DeckManager.py:325-331`, `LibUSBHIDAPI.py:425-427`) — periodic writer stalls.
52. `run_command` forks the full multi-threaded GTK process via `multiprocessing.Process(target=subprocess.Popen)` and never joins (`HelperMethods.py:384-385`) — fork-with-threads hazard.
53. Accidental IDE auto-imports: `from cv2 import exp` (`ActionConfigurator.py:16`), `from numpy import isin` (`Page.py:34`), `from ast import main` (`app.py:16`).

---

## 7. Open measurement questions (Phase 0 answers these)

1. How much of the live 1.25GB is reclaimable arena waste? (`MALLOC_ARENA_MAX=2` A/B; `malloc_trim` probe.)
2. Does capping cv2 threads change loop_fps? (Expected no — pipeline is PIL/encode-bound per the video-bg-perf work; verify.)
3. What fraction of the 14h growth is the §3.3 leak inventory vs fragmentation? (Soak script with unplug/replug + page-switch counters against telemetry.)
4. `svg_to_pil` height bug (§6.22) — confirm with a square SVG render before filing upstream/fixing.
