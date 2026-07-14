# Refactor Audit — StreamController (2026-07-12)

**Scope:** maintainability/structure only — fragile structures, inconsistent patterns,
concurrency that existing decorators already solve, repeated patterns, ad-hoc design.
This is a **refactor** lens, deliberately distinct from the bug-focused
`docs/deep-audit-2026-07-10.md` (which catalogues races/data-loss; those are not re-listed here).

**Tree:** `main` @ working tree. Suite baseline unchanged. Line numbers verified against the
live tree unless marked *(approx)*.

**Method:** decorator/pattern census (grep counts over `src/ GtkHelper/ main.py globals.py autostart.py`,
excluding worktrees/venv/tests) + 3 parallel subsystem readers (DeckController+media, Store, GTK windows) + spot-verification of load-bearing claims.

---

## 0. Credit — what the last audit recommended and has since landed

Worth stating so this report focuses on what *remains*:

- **Atomic JSON writes consolidated.** `src/backend/atomic_json.py` (`atomic_write_json`,
  `quarantine_corrupt_file`) now backs **16 files** — SettingsManager, PageManagerBackend, Page,
  PluginBase, both migrators, both importers, MenuButton, StoreCache/StoreBackend, AssetManagerBackend.
  The 2026-07-10 "every other JSON write truncates" finding is **substantially closed**. Only **2 stragglers**
  remain: `Migrator_1_5_0_beta_5.py:58` and `:103` (raw `json.dump` into `open(...,"w")`).
- **`run_on_main` hardened** with proper idle-source cancellation (GtkHelper.py:43-106) — the
  double-build race is closed.
- **Central exception hooks** landed (`log_hooks.py`), and `@background` gained auto-logged futures.

The helpers this report leans on (`@on_main`/`run_on_main`, `@background`/`run_in_background`) are
**good** and already correct. The problem is **adoption**, not design.

---

## 1. Concurrency the existing decorators already solve *(highest leverage)*

The project ships two correct concurrency helpers in `GtkHelper/GtkHelper.py`:

- `run_on_main(func,…)` / `@on_main` — marshal to the GTK thread, block for the result, **cancel the
  idle source on timeout** (no double-execute), re-raise the callee's exception.
- `run_in_background(func,…)` / `@background` — submit to a shared 8-worker pool that **auto-logs any
  exception** via `add_done_callback` and is torn down on quit (`shutdown_background_pool`).

**They are barely used outside the helper library itself.** Census:

| Pattern | Count | Helper equivalent | Helper usage |
|---|---:|---|---:|
| raw `GLib.idle_add(…)` | **149** | `run_on_main` / `@on_main` | 16 + 15 = **31** |
| raw `threading.Thread(…).start()` | **~45** (25 files) | `run_in_background` / `@background` | 2 + 3 = **5** |
| inline `ThreadPoolExecutor(…)` | **6** | the shared `@background` pool | — |

And `@on_main` is applied **almost exclusively inside `GtkHelper/GenerativeUI/*`** (the library's own
rows). Application code — `src/windows/**`, `src/backend/**` — hand-rolls raw `GLib.idle_add` and
`threading.Thread`. The good pattern exists but never crossed the library boundary.

### 1a. The one idiom repeated ~10× that the helpers exist to replace

"Spawn a bare thread → do I/O → `GLib.idle_add` the result back to build/mutate UI." Instances (each is
`@background`-worker + `run_on_main`-for-the-UI-touch):

- `windows/Store/StorePage.py:67` (thread) + **15** raw `idle_add` at :130-143
- `windows/PageManager/Importer/Importer.py:61,65` (two threads) + 6 `idle_add`
- `windows/Onboarding/PluginRecommendations.py:44` + 3 `idle_add`
- `windows/Store/Preview.py:186` + 2 `idle_add`
- `windows/AssetManager/CustomAssets/Chooser.py` — *inconsistent within one file*: `:80` correctly uses
  `run_on_main(_build_ui)`, then `:124` reverts to raw `GLib.idle_add(...refresh)` and `:134` spawns a raw thread.

**Consequences of the ad-hoc form:**
- **Exceptions vanish.** A raw `Thread(target=load)` that raises logs nothing; the `@background` pool
  logs every failure. Several loaders have no `@log.catch` either.
- **Shutdown hangs.** Only **28 of ~45** raw threads pass `daemon=True`; the rest are non-daemon
  one-offs outside the pool's lifecycle. The pool is bounded (8) and cancels on quit.
- **Off-main GTK construction (crash class).** Six chooser loaders build `Gtk`/`Adw` widgets **on the
  worker thread**, not inside the `idle_add` (see §2b) — the documented off-main-GTK segfault class.

**Recommendation:** adopt `@background` for the work body and `run_on_main` for every GTK touch. The
`GenerativeUI` subclasses (`ComboRow`, `ScaleRow`, `SpinRow`, `ExpanderRow`) are the in-repo template —
copy the pattern outward rather than inventing per-window.

---

## 2. Repeated patterns (copy-paste families)

### 2a. Store — four asset types, four copies of everything *(the incomplete-fix engine)*

`src/backend/Store/StoreBackend.py` carries a parallel implementation per asset type
(plugin / icon / wallpaper / sd_plus_bar_wallpaper). Families, all ~90-95% identical:

| Family | Members (line ranges *approx*) |
|---|---|
| `prepare_*` | `prepare_plugin` 456-541, `prepare_icon` 589-661, `prepare_wallpaper` 664-733, `prepare_sd_plus_bar_wallpaper` 735-803 |
| `install_*` / `uninstall_*` | 1063-1244 (icon/wallpaper/sdplus are trivial `uninstall→download_repo`; plugin is the complex one) |
| `get_*_to_update` | 1272-1305, 1336-1363, 1382-1409, 1428-1455 |
| `update_all_*` | 1307-1334, 1365-1380, 1411-1425, 1457-1472 |

Inside the four `prepare_*` alone, **four blocks are copy-pasted verbatim**: version-compat resolution,
attribution fetch, thumbnail-with-fallback fetch, description translation.

**Why it matters (not cosmetic):** this duplication is the *mechanism* behind the recurring
"fixed in one sibling, not the others" drift the bug audit keeps finding — e.g. the compatibility guard
exists only in the plugin path; the `rmtree-before-download` hardening reached `install_plugin` but not
the three asset installs. **Collapse to one `_prepare_asset(asset, descriptor)` + thin wrappers keyed by
an asset-type descriptor** and the whole class of drift disappears at the source.

### 2b. AssetManager — six near-identical Chooser/PackChooser classes

`windows/AssetManager/{IconPacks,WallpaperPacks,SDPlusBarWallpaperPacks}/…` each ship a `PackChooser`
and a leaf `*Chooser`. `sort_func`/`filter_func` are duplicated ~20 lines ×3 (identical `fuzz.ratio`
scoring, differing only in the asset attribute name):

- `IconChooser.py:102-122` ≈ `WallpaperChooser.py:95-115` ≈ `SDPlusBarWallpaperChooser.py:95-115` *(approx)*

Every one of them also spawns the raw build-thread from §1 and **constructs its `*FlowBox` on that
thread**. **Recommendation:** one `GenericAssetChooser(asset_type, flow_box_cls, preview_cls)` base (or a
sort/filter mixin) — collapses the duplication *and* fixes the off-main construction in one move.

### 2c. DeckController — render/media setter families

- **Media setters:** `Background.set_image/set_video` (~2286), `ControllerDialState.set_image` (~4922),
  `ControllerKeyState.set_image` (~5004) share ~85% (close-old-media → assign → conditional update) but
  the `update=True` parameter means three *different* things across them (full repaint vs dial update vs
  key update) with no shared contract. Extract `_close_and_replace_media(old,new,on_update)`.
- **Label setters:** `LabelManager.set_page_label` / `set_action_label` (~2889/2910) — same shape.
- **Per-input-type tick loops:** the `for x in inputs.get(Input.Key/Dial/Touchscreen)` triad recurs in
  `_run_one_tick` and `_needs_key_ticks` — parametrize over `Input.All`.
- **Inline centering arithmetic** `((container - item) // 2)` copy-pasted at ≥4 `Image.paste` sites —
  extract `_center_xy(container, item)`.

---

## 3. Fragile structures

### 3a. String class-lookup riding on a wildcard-import leak *(two layers deep)*

`DeckController.py:1110` — `init_inputs()` does:

```python
input_class = getattr(sys.modules[__name__], i.controller_class_name)
```

Two fragilities stacked:
1. **String→class resolution.** A rename of `ControllerKey`/`ControllerDial`/… silently breaks input
   construction — no static reference, no grep-ability.
2. **`sys` is never imported in this file.** It resolves *only* because line 40 is
   `from …HelperMethods import *`, and HelperMethods happens to `import sys` with no `__all__`. Add an
   `__all__` to HelperMethods, or drop its `sys` import, and `init_inputs()` dies with `NameError` at
   runtime — nothing in the file signals the dependency.

**Recommendation:** an explicit module-level registry `{Input.Key: ControllerKey, …}` (the 2026-07-10
audit's Refactor #1 prerequisite). Also removes both `import *` lines (40-41), the only wildcard imports
in the codebase — they pollute the namespace and make every bare name's origin unknowable.

### 3b. `recursive_hasattr(gl, "app.main_win.sidebar…")` — dotted-string singleton navigation

**42 call sites across ~15 files**, 8 distinct dotted paths, strings duplicated (e.g.
`"app.main_win.sidebar.page_selector"` appears 4×). Failure mode is **silent**: a typo
(`sideBar`) or a trailing dot returns `False`, the guard skips, no error, no test can catch it — the
2026-07-10 audit already found one live trailing-dot typo that made a guard dead code.

**Recommendation:** a typed optional-chaining accessor `nav(gl, "app", "main_win", "sidebar", …)` (or
cached property accessors on `gl`) — grep-able, typo-surfacing, IDE-navigable.

### 3c. Giant multi-responsibility methods

`DeckController.close()` (~200 lines, 9 sequential teardown steps) and `load_page()` (~130 lines,
brightness+screensaver+background+inputs+switch interleaved). These are exactly the methods the bug
audit keeps finding step-ordering hazards in; decomposing into named sub-steps
(`_close_inputs`/`_close_media`/`_close_device`) makes the ordering an explicit, reviewable sequence.

---

## 4. Inconsistent patterns / ad-hoc design

- **No error-handling house style.** **24** bare `except:`, **156** `except Exception`, **85**
  `log.catch`, plus silent `print()`/swallow sites (e.g. DeckController `:2727`, `:4249` bare `except:`
  that `print()`s). Pick one: `@log.catch` on top-level entry points, explicit narrow excepts elsewhere;
  ban bare `except:` in a linter.
- **Store return-type zoo.** `install_*`/`download_repo`/`get_remote_file`/`get_last_commit` variously
  return `None` / `False` / `int` HTTP codes / **`NoConnectionError` instances** / `True`, forcing
  `isinstance(x, NoConnectionError)` at **15+ call sites** — and several install call sites
  (`IconPage`/`WallpaperPage`/`SDPlusBarWallpaperPage` `.install`) **don't check the return at all**,
  blindly marking "installed". One typed result channel (2026-07-10 Redesign #4) removes the ambiguity.
- **`StoreCache.set_files` rewrites the entire index file on every `open_cache_file`** (StoreCache.py:298,
  including pure cache hits). It's atomic now, but that's a full-file JSON dump per cache access on the
  loop thread. In-memory index + debounced flush.
- **Magic numbers inline** throughout DeckController (key size 72, touchscreen 800×100, SVG 192, write
  20 Hz, various timeouts/thresholds) — no config object; a `DeckControllerConfig` dataclass read once at
  init would centralize the tuning knobs and the scattered `os.environ.get` reads.
- **6 inline `ThreadPoolExecutor` pools** of varying sizes coexist with the shared `@background` pool.
- **2 migrator truncate-write stragglers** (`Migrator_1_5_0_beta_5.py:58,103`) — the last non-atomic JSON
  writes after the `atomic_json` migration; route them through `atomic_write_json` to finish §0.

---

## 5. Prioritized recommendations

| # | Refactor | Leverage | Effort | Notes |
|---|---|---|---|---|
| 1 | **Adopt `@background` + `run_on_main`** across the ~10 window loaders (§1a); ban raw `Thread`/`idle_add` in new code | High — kills silent-exception + shutdown-hang + off-main-GTK classes at once | Med (mechanical, per-site) | Template already in `GenerativeUI/*` |
| 2 | **Descriptor-collapse the Store `*_asset` families** (§2a) | High — removes the incomplete-fix drift engine | Med-High | One impl + thin wrappers; do before more Store bug-fixing |
| 3 | **Explicit controller-class registry**, delete both `import *` (§3a) | Med — removes a latent `NameError` land-mine + namespace pollution | Low | ~10 lines |
| 4 | **Typed optional-chaining accessor** to replace 42 `recursive_hasattr` string paths (§3b) | Med — closes a silent, untestable failure mode | Med | Migrate call sites incrementally |
| 5 | **`GenericAssetChooser` base** for the 6 chooser clones (§2b) | Med — dedup + fixes off-main construction | Med | |
| 6 | **Finish atomic-write migration** (2 migrator stragglers) + **error-handling house style** (§4) | Low-Med | Low | Quick wins |
| 7 | Media-setter/label/tick-loop helpers in DeckController (§2c); decompose `close()`/`load_page()` (§3c) | Med | Med | Coordinate with any upstream merge train to minimize conflicts |

**Single highest-value move:** #1 — the decorator-adoption gap is the most repeated, most mechanical, and
touches the most files, and every migrated site inherits exception-logging, bounded concurrency, and
clean shutdown for free.

**Cross-reference:** structural items overlapping the prior audit's §8 Refactors / §9 Redesigns
(pin-count page cache, transactional store installs, event-dispatch lanes, thread-safe notify facade)
remain valid and are not re-argued here.
