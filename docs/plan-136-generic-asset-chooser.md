# Implementation plan — GenericAssetChooser extraction (+ the off-main FlowBox construction fix)

Status: plan (2026-08-09). Base: `main` @ `72ef781e`.

Branch: `refactor/136-generic-asset-chooser` · 1 MR, `Closes #136` · Estimate: **M**. AssetManager UI — not a listed contract surface → single review pass. The **off-main GTK construction fix is the substantive half** (documented segfault class); the dedup is the vehicle.

## Design

1. One `GenericAssetChooser` base (or composition helper — implementer's call, smallest honest diff wins) parameterized by asset attribute name / flow-box class / preview class, absorbing the ~20-line duplicated `sort_func`/`filter_func` (`fuzz.ratio` scoring) across `IconChooser`/`WallpaperChooser`/`SDPlusBarWallpaperChooser`, and the shared `PackChooser` build shape.
2. **The crash-class fix**: the loader thread must stop constructing `*FlowBox` children off-main — data loading stays on the worker (`@background` or plain thread per surrounding idiom), widget construction and append marshal via `run_on_main`/`idle_add` per the repo's threading invariants (GTK is main-thread-only). This is the behavior change; the refactor must not smuggle in any other.
3. Visual/behavioral parity everywhere else: search scoring, sort order, empty-query behavior, pack navigation — identical by construction (shared code) and pinned where testable.

## Tests

Headless limits are real (FlowBox children need GTK but not a display — gi works in the harness; a `Gtk.init_check`-gated scenario tier exists? verify — if widget construction headless is not viable, pin what IS: the sort/filter functions extracted as pure logic (score ordering, attribute keying for all three asset types), plus an AST/inspection tripwire that the loader path contains no widget construction outside a main-thread marshal). State honestly in the report what could not be pinned headless; flag the AssetManager open-and-browse as a line item for the next hardware/field session.

## Constraints
Do NOT touch: DeckController.py, StoreCache/StoreBackend (wave-A claims StoreBackend), Settings.py, app.py, Page.py/KeyGrid.py/FlatpakPermissionRequest.py (wave-B claims), log_hooks.py (exception-hooks worker claims). The AssetManager subtree plus a new shared module is the whole footprint.

## Critical files
`src/windows/AssetManager/**` (six chooser/pack files + new shared base), new `tests/scenario_asset_chooser_logic.py`.
