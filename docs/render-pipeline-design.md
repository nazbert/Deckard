# Rendering & Page‑Switch Pipeline — Design (v2) — CLOSED

**Status: CLOSED 2026‑07‑02.** The full pipeline rebuild was not executed; its motivating defects were resolved incrementally and hardware‑verified. This document is retained as the analysis record and as the re‑open map for the deferred items below.

## Closure disposition

**Landed (all hardware‑verified, see `fix/deck-core-concurrency` + `perf/media-pipeline` branches):**
- **P3 / Step 3** — busy‑waits removed, gen‑guarded clear + event handoff (`2754514`); the ~1 s switch latency is gone.
- **P1 (mitigated)** — all device I/O serialized behind the per‑deck `BetterDeck` lock (`3308e89`, `fc9e3ed`); lock‑serialized rather than single‑owner‑thread.
- **P2 / Step 1 (essentials)** — per‑input `config_gen` stamped on loads, captured at render start, judged at the present boundary with drain‑then‑snapshot (`05c8ccb`); touchscreen gen‑tagged (`5411d80`); dual‑hash present‑time dedup (`a0953ca`).
- **P5** — the "overloaded media thread" was measured (env‑gated profiler) and fixed at the source: encode memo + resized‑foreground cache, loop 19→29.9 FPS.
- **P6 / Step 0** — lifecycle races fixed (`7b6be5a`, `6b100d7`, `3926c13`, bounded `stop()`).
- **§9 coordinator serialization** — `_load_page_lock` serializes the switch body.
- **§10 follow‑ups** — mirror‑paint guards, Option‑D GTK marshaling, `allow_relaod`, `@background`: all landed.
- **§4.2 epoch‑aware enqueue + the `set_state` race** — closed 2026‑07‑02 by the per‑input paint‑sequence guard at the enqueue slot (`fix(decks): reject out-of-order paints at the enqueue slot`); enforced at enqueue only, present untouched, so it cannot strand.
- **Step 2 single device owner** — executed as `docs/presenter-migration-plan.md`'s M1, in the media‑thread‑as‑sole‑writer variant (no new presenter thread/module; `MediaPlayerThread` becomes the sole device writer behind a control queue) — see plan §6.1 for why the separate‑thread design was dropped. Branch `refactor/single-writer` (M1, `70af541`).
- **Step 3 remainder** — brightness/clear/`close_all` rerouted from direct multi‑thread writes to seq‑stamped `control_q` messages drained by the media thread, closing the direct‑write gaps the busy‑wait removal (above) didn't touch. Branch `refactor/single-writer` (M1, `70af541`).
- **Step 5** — screensaver's unlocked three‑thread state machine replaced by one three‑phase transition (heavy work outside any lock → decide+swap under `_load_page_lock` → `load_page` after release) serializing all six requesters; animation counters decoupled from paint fate via wall‑clock/timeline picking (`KeyGIF` cumulative‑delay bisect; `InputVideo` wall‑clock‑once‑complete + sequential‑while‑building, mirroring `BackgroundVideo`). Branch `refactor/single-writer` (M3 `6e0e6bb`, M4 for the `KeyGIF`/`InputVideo` counters).
- **§7.1 fault‑injection harness** — `FaultyFakeDeck` (scriptable `TransportError`s, write journal, latency injection) + unit/integration fixture tiers, committed as `tests/scenario_*.py` (16 scenarios). Branch `refactor/single-writer` (M0, `53183dc`).

**Reversed decision:** §8.1's locked "`on_ready` on the switch critical path with bounded timeout" was invalidated in production — a plugin wedged in `on_ready` (pulsectl deadlock) froze the app precisely because plugin callbacks sat on critical paths. Shipped behavior is the opposite: `on_ready`/`on_update` run on the bounded action pool; first frames may fill in asynchronously.

**Deferred, with re‑open triggers:**
- **Step 4 immutable render snapshot** (torn‑frame class) — trigger: a reproduced torn frame in the wild.
- **§10.1 pull‑model UI mirror**; **Step 6 XL render topology** (the profiler it needs now exists: `STREAMCONTROLLER_MEDIA_PROFILE=1`).
- Minor: `update_all_inputs` still skips keys when a bg video is present (self‑heals within one 33 ms tick).

---

*Original v2 design follows, unmodified, as the analysis record.*

**Status:** Draft for iteration · **v2** incorporates a 5‑lens adversarial review that invalidated the v1 scalar‑epoch model and the single‑render‑worker topology. · **Decisions (locked):** full migration arc **0–6**; `on_ready` stays on the switch critical path with a bounded timeout (§8). · **Scope:** `src/backend/DeckManagement/` (esp. `DeckController.py`, `BetterDeck.py`), `src/backend/PageManagement/Page.py`, plugin paint paths in `src/backend/PluginManager/ActionCore.py`.

This document proposes replacing the current ad‑hoc, timing‑dependent render/present/switch machinery with an explicit **pipeline** whose correctness does not depend on how fast the app runs. It is grounded in a full subsystem audit (7 facets) and an adversarial design review (5 lenses); the review's blocker/major findings are folded in and cited inline as **[R:lens]**.

---

## 1. Motivation

The investigation began from a UX symptom — **page switching takes ~0.7–1.3 s for ~0.1–0.2 s of real work** — but uncovered a structural problem:

- **Latency and correctness are coupled.** The ~1 s is two busy‑waits (`load_page` `:794‑797`, `clear_media_player_tasks` `:958`) polling a tick counter. Their latency *serializes paints*, so a stale old‑page paint lands *before* the new page's final paint (which overwrites it) → transient, self‑resolving bleed. Remove the latency naively (we tried) and stale paints land *after* on a static page → **permanent bleed**.
- **The threading is structural, not for throughput.** Under the GIL, the render work does not parallelize much (Pillow drops the GIL for its C ops, but that is unmeasured here — see §8). Threads earn their keep in two places only: **USB writes** (hidapi releases the GIL for the syscall) and **blocking action callbacks**.

Goal: **predictable rendering and switching** — deterministic staleness, no timing‑as‑synchronization, one owner of the device, no bleed at any speed — *without regressing* interactive latency or animation.

---

## 2. Current architecture (audit summary)

**Threads that touch render/present state (per deck):** GTK/main (page loads, brightness, rotation — writes device directly), `MediaPlayerThread` (30/2 FPS: animate + run queued tasks + write images), `action_executor` pool `max(8, inputs+4)` (on_ready/tick/update/events + **plugin `set_media`/`set_label`** → `update()` → device), `tick_actions` (1 s), `ScreenSaverTimer` (writes device directly), USB event thread (press feedback → `update()`), plus USBMonitor/Flatpak/Resume monitors.

**Core pathologies (evidence):**

- **P1 — No single device owner.** `BetterDeck` has zero locks; `set_key_image`/`set_touchscreen_image`/`set_brightness`/`clear` are called concurrently from GTK, `MediaPlayerThread`, and `ScreenSaverTimer`. USB HID is not multi‑thread‑safe.
- **P2 — Guards at some call sites only.** Gen token guards `load_all_inputs`/`update_all_inputs` (`:694‑698`); `perform_media_player_tasks` checks `task.page is active_page` for the `tasks` list (`:308`) — but `image_tasks`/`touchscreen_task` paint unguarded (`:316‑326`), `touchscreen_task` is never cleared on switch, and **plugin `update()` bypasses the gen token** (gated only by `get_is_present()`, `ActionCore:430‑435`). **[R:plugin-compat]** The real defect is a **TOCTOU** between `get_is_present()` and the deferred `update()`/write, not an absence of guards.
- **P3 — Timing as synchronization.** Two busy‑waits poll `media_ticks`; idle FPS = 2 makes each ~0.5 s. `clear_media_player_tasks` can hang if the media thread is *stopped* (not just paused). **[R:migration]**
- **P4 — Incremental per‑key present.** Switches paint key‑by‑key; two pages interleave on the device.
- **P5 — Overloaded media thread + racy dedup.** `_last_img_hash` (per key) is read/written from multiple threads; a racy read can *suppress* a needed repaint → bleed.
- **P6 — Lifecycle races.** `on_disconnect` iterates `deck_controller` while mutating it (skips 2nd deck); `remove_controller` is non‑idempotent and reachable from USBMonitor *and* the media thread's error path → double `delete()`; `MediaPlayerSetTouchscreenImageTask.n_failed_in_row` is a **scalar shared across decks**; `on_resumed` swaps the deck object non‑atomically; `media_player.stop()` spins with no timeout; `action_executor.shutdown(wait=False)` runs callbacks post‑delete. **[R:lifecycle]**

---

## 3. Design principles (revised)

1. **Correctness token is per‑input, not a global scalar.** Each `ControllerInput` carries a monotonic **generation** that bumps on *any* change to its intended visual: page switch, reload, `set_state`, or a plugin `set_*`. A page switch bumps all inputs' generations at once (that is all a "page epoch" is — a convenience). This is the fix for **[R:epoch‑correctness]**: one scalar cannot encode both page‑identity *and* config/state version; a per‑input generation encodes exactly "which intended visual is current for this input," which is what present must check.
2. **Capture the token atomically at the paint's *decision point*, not at the write.** A paint (plugin `set_media`, animation tick, press feedback) captures its input's generation atomically with the `active_page`/present check (under the page lock), and carries it through to the presenter. Re‑reading "current" at write time re‑introduces straggler bleed; capturing at creation goes stale on reuse. **[R:plugin-compat]**
3. **Guard at the boundary, keep source checks too.** The presenter drops any frame whose carried generation ≠ the input's current generation. This covers switches, plugin paints, animation, and press feedback uniformly. But the epoch is **necessary, not sufficient**: existing source‑side checks (`get_is_present` membership for removed same‑page actions) stay. **[R:plugin-compat]**
4. **One device owner; the presenter is thin.** Exactly one thread per deck writes the deck (images, touchscreen, brightness, clear, screensaver). It only: check‑generation → dedup vs last‑written bytes for that input → write. **Rendering is NOT on this thread** — it stays distributed (callback pool for live updates; a bounded decode/render pool for switches). This preserves today's cross‑key render parallelism and keeps the presenter un‑stallable by render. **[R:perf]**
5. **Detection ≠ teardown.** The presenter never tears itself down. On repeated write errors it sets a self‑owned *dead/suspended* flag, drops its queue, and exits its loop; a **different** thread (DeckManager) reaps via an idempotent, lock‑guarded `remove_controller`. **[R:lifecycle]**
6. **Immutable render input.** A switch resolves each input into an immutable spec/snapshot; renders read the snapshot, never live `ControllerInputState` that the next switch mutates in place. Without this, a generation stamp can certify a *torn* frame. **[R:epoch‑correctness]**
7. **Predictable handoff, no polling.** Stage handoff is a generation‑tagged request + event/condition, never a `sleep`‑poll. Switch requests coalesce latest‑wins; **live per‑input updates accumulate a dirty *set* (merge, not overwrite)** so a multi‑key plugin loop isn't clobbered. **[R:plugin-compat]**

---

## 4. Target architecture

```
 request(switch / reload / brightness / screensaver / rotation)
        │  (all coalesced through ONE serialized coordinator entry — load_page
        │   is called from GTK, USB-event, API and monitor threads today)
        ▼
 ┌──────────────┐  bumps per-input generations; builds an immutable
 │ Coordinator  │  RenderModel snapshot; dispatches render jobs. Media
 │ (serialized) │  spec resolution = disk decode (NOT cheap) → runs on a
 └──────┬───────┘  bounded decode pool, never on GTK. [R:perf]
        │ render jobs (switch: all inputs; live: one input) carry (input, gen)
        ▼
 ┌──────────────┐  DISTRIBUTED render (callback pool for live updates,
 │  Renderers   │  decode pool for switches). Composite + encode to native
 │ (pooled)     │  bytes. No device I/O, no shared-mutable dedup state.
 └──────┬───────┘  Emit (input, gen, native_bytes).
        │
        ▼
 ┌──────────────┐  THE ONLY thread that touches the deck. Per (input, gen):
 │  Presenter   │  if gen == input.current_gen: dedup vs last-written bytes
 │ (1/deck,     │  → write; else drop. Owns brightness/clear/screensaver/
 │  thin, sole) │  close as ORDERED control messages. On write error: set
 └──────┬───────┘  dead/suspended flag, drop queue, exit — never self-reap.
        ▼
      device

 Callback pool (bounded): on_key_down/on_tick/on_ready/plugin set_*. Never
 touches the deck. A visual change captures (active_page, input.gen) under the
 page lock, renders, and submits (input, gen, bytes) to the presenter.

 Animation: renderers re-render animated inputs at page FPS with the current
 gen; a switch bumps gens → old-gen frames dropped at present. Frame counters
 advance only AFTER a present succeeds, so a dropped speculative frame does
 not desync the video. [R:lifecycle]
```

**Stage ownership:**

| Stage | Thread | Touches device | Drops stale by |
|---|---|---|---|
| Coordinator | one serialized entry | no | *authors* per‑input generations |
| Renderers | pooled (callback + decode) | no | carry the gen captured at decision point |
| Presenter | 1 / deck (**sole owner, thin**) | **yes** | `gen == input.current_gen` check + dedup, at the write |
| Callback pool | bounded | no | capture `(active_page, gen)` atomically at the paint decision |

> **Presenter vs render threading (resolves v1 Q1):** render is **not** collapsed onto the presenter. The presenter is write‑only. Whether renderers are one shared worker, the callback pool, or a per‑input‑group shard is a **profiling decision** (§8) — a 32‑key XL animating may need real render parallelism; an 8‑key MK.2 does not. The presenter being sole USB owner is fixed; render topology is tunable per deck model.

### 4.1 The generation model (precise)

- Each `ControllerInput` has `current_gen: int`. A **switch/reload** bumps every input's `current_gen` (under the page lock) *before* `on_ready`/`update_all_inputs` run for the new page — so the new page's own opening paints carry the new gen and present. A **`set_state`**, a UI **edit**, or a **plugin `set_*`** bumps only that input's `current_gen`.
- A paint captures `gen = input.current_gen` **atomically with** its `get_is_present()` decision (same page‑lock critical section). It carries `gen` through `update() → add_image_task → present`. The presenter writes iff `frame.gen == input.current_gen` at write time.
- **Why this fixes the blockers:** old‑page straggler captured an old gen → dropped after switch. Post‑reload plugin paint captures the *re‑stamped* current gen → presents (no frozen clock). Pre‑edit animation frame carries the old gen → dropped. `set_state` bump drops in‑flight old‑state paints (the multi‑state bleed the scalar epoch missed). **[R:epoch‑correctness]**
- **Internal‑API change (not a one‑liner):** `update()`/`add_image_task` must carry a gen. Keep a public zero‑arg `update()` that internally captures `(active_page, gen)` atomically, so **no plugin‑facing signature changes**; enumerate every internal `update()` caller (press feedback `:2231`, animation `:2218`, tick, bare `ControllerInputState.update()` `:1802`, overlay‑hide Timer). **[R:plugin-compat]**
- **Dedup moves to the presenter** (single owner): compare against the last bytes actually written to that input. This removes the multi‑thread race on `_last_img_hash` *and* fixes the coherence bug where a presenter‑side `clear` left a stale hash → key stayed blank after screensaver. **[R:epoch‑correctness]**

### 4.2 Enqueue / coalescing rules

- **Switch request:** single‑slot latest‑wins per deck (a newer switch supersedes an in‑flight one). Also check the gen at **pull time and before per‑input encode**, not only at present, so a superseded switch abandons expensive render early. **[R:perf]**
- **Live per‑input update:** accumulate a **dirty set keyed by input** (merge, not overwrite) so `for k in keys: set_media(k)` repaints every key. **[R:plugin-compat]**
- **Epoch‑aware enqueue (closes the eviction hole):** a pending frame for an input must **not** be replaced by one carrying an *older* gen. Without this, a stale straggler can evict the current‑gen frame and then be dropped → permanent stale key — the hole that makes v1 "Step 1" insufficient on its own. **[R:migration]**
- **Touchscreen** is a whole‑surface composite fed by all dials; treat it as a mini frame‑atomic present tagged with the current page gen, never with one dial's source gen. **[R:epoch‑correctness]**

### 4.3 What "atomic" honestly means

The streamdeck lib has **no atomic multi‑key commit** — `set_key_image` sends per‑key JPEG as a Python loop of 1024‑byte HID reports under a per‑handle mutex. So the guarantee is **"no cross‑*page* interleave"** (the generation gate genuinely provides this), **not** "all keys change simultaneously / no flicker." A large deck still updates key‑by‑key; we shrink the wipe by writing only changed keys (dedup). Do not promise simultaneity. **[R:perf]**

---

## 5. How the target dissolves each pathology

| Pathology | Resolution |
|---|---|
| **P1** 3 threads write USB | One thin presenter owns the deck; all writes serialize through it. |
| **P2** guards at some call sites; plugin TOCTOU | Per‑input gen captured atomically at the decision point, checked at the one write boundary; source checks retained. |
| **P3** busy‑wait timing‑sync | Generation‑tagged request + event handoff; no `sleep`‑poll. Latency win, safe once §4.2 enqueue is in. |
| **P4** per‑key interleave | Generation gate → no cross‑page interleave (honest scope, §4.3). |
| **P5** overloaded thread, racy dedup | Render distributed; dedup single‑owned by the presenter (no race, coherent with clear). |
| **P6** lifecycle races | Detection≠teardown; idempotent guarded `remove_controller`; per‑deck counter; bounded joins; presenter‑mediated close/resume. |

---

## 6. Edge cases (current problem → design handling)

- **Plugin‑initiated paints (root bleed cause)** — TOCTOU after `get_is_present`. → Capture `(active_page, gen)` atomically at that check; carry through; presenter drops stale. Keep the `get_is_present` membership check for same‑page action removal. **[R:plugin-compat]**
- **`on_ready` / first frame vs async callbacks** — today `initialize_actions` runs synchronously before the final paint, which is *why* the first frame includes plugin content. Moving `on_ready` to the pool (v1 Step 5) would make switches show default keys then pop in — the partial‑page look we're trying to kill. → **Decision required (§8):** either keep `on_ready` on the switch critical path (a slow plugin stalls the switch), or the first frame for a gen blocks until the page's painting `on_ready`s complete. "Atomicity" and "off‑critical‑path callbacks" are mutually exclusive; pick one. **[R:plugin-compat][R:lifecycle]**
- **Screensaver** — a two‑thread state machine (Timer vs USB‑event) mutating `showing`/`inputs` with no lock, writing the deck directly. → Model as a **coordinator‑owned mode** mutated only under the page lock (one thread of truth; show/hide serialized), show/hide bumps generations, all writes via the presenter. Note the dedup‑coherence fix (§4.1) is what stops the "blank after wake." **[R:lifecycle]**
- **Video‑background switch** — `update_all_inputs` skips *all* keys when a background video is present (`:484‑489`), so the switch frame has **zero keys** and the old page shows until the next tick. → The switch must render keys composited over the first video tile; remove that skip from the switch path. **[R:epoch‑correctness]**
- **Brightness / rotation** — direct writes from any thread. → Ordered, **fire‑and‑forget** control messages to the presenter (never caller‑blocking, presenter never awaits GTK → no deadlock). Rotation also bumps generations (it changes the physical key mapping) and its KeyGrid UI regen stays on GTK. **[R:migration][R:lifecycle]**
- **Dials / touchscreen** — whole‑surface composite; per‑dial live update re‑renders it all. → One touchscreen frame per current page gen (§4.2); dedup avoids redundant re‑encode of the 800×100 JPEG. **[R:epoch‑correctness]**
- **Animation statefulness** — `get_next_tiles`/GIF advance `active_frame` *during* render; dropping a speculative frame desyncs. → Advance frame counters only **after** a present succeeds. **[R:lifecycle]**
- **Hotplug / disconnect** — iterator invalidation (2+ decks) + non‑idempotent cross‑thread `remove_controller`. → Copy‑on‑iterate; single idempotent lock‑guarded `remove_controller` with an "already torn down" flag; enumerate all callers (USBMonitor, Flatpak poll, resume, presenter error path). **[R:lifecycle]**
- **`beta-resume-mode` (default ON)** — makes `on_resumed`/handle‑swap dead code; key writes swallow `TransportError` and retry, touchscreen writes don't (and count toward removal). → First‑class **suspended** state distinct from **dead**: in beta mode the presenter marks suspended and drops frames *without counting*, retrying open on the same handle; only a real disconnect counts toward removal. Unify key vs touchscreen error policy. **[R:lifecycle]**
- **Hung USB write** — can't be interrupted from Python; a wedged presenter freezes everything. → Watchdog + **bounded** joins everywhere; teardown/resume may **abandon** (not join) a wedged presenter. "drop all queued frames" for disconnect, not "drain." **[R:lifecycle]**
- **App shutdown ordering** — `close_all` does `clear()` then `close()` assuming ordered writes. → A terminal "clear+close" control message on the presenter; `close_all`/`delete` block on a **bounded** presenter join. **[R:lifecycle]**
- **Multi‑deck** — per‑deck everything; per‑deck failure counter; guard the global list. Every migration step needs a **2‑deck test gate** (iterator invalidation and the shared counter only manifest with 2+). **[R:migration]**
- **Remote / Fake decks** — non‑USB transports added as DeckControllers; define presenter/counter semantics for them (no USB mutex, different error model). **[R:lifecycle]**

---

## 7. Migration — strangler, re‑sequenced

Every step compiles, is hardware‑tested (with a **fault‑injection** harness — see §7.1 — because the target races are too narrow for incidental hardware testing to catch **[R:migration]**), and is independently revertable *except where a forward dependency is called out*.

- **Step 0 — Isolated lifecycle bugfixes (ship first, no pipeline dependency).** Copy‑on‑iterate `deck_controller`; idempotent lock‑guarded `remove_controller`; per‑deck touchscreen `n_failed_in_row`; bounded `media_player.stop()`. Pure correctness, multi‑deck testable, zero paint‑ordering risk. **[R:migration]**
- **Step 1 — Per‑input generation + boundary drop (fixes the live bleed).**
  - 1a: per‑input `current_gen`; bump on switch/reload/set_state; capture `(active_page, gen)` atomically at `get_is_present`; thread through the internal `update()`/`add_image_task` path for **key, dial, and touchscreen** (not just `ControllerKey`); presenter drops stale.
  - 1b: **epoch‑aware enqueue** (§4.2) so a stale straggler can't evict a current frame; clear `touchscreen_task` on switch; **screensaver bumps the gen** (cheap) so screensaver‑entry stragglers drop too.
  - 1c: move dedup to the write boundary (coherent with clear).
  - *Outcome (honest):* cross‑page and cross‑state permanent bleed impossible; transient bleed gone. Torn frames from in‑place state mutation remain theoretically possible until Step 4. No latency change yet. *Forward‑coupling: the `update()` signature change means reverting Step 1 after Steps 3–4 build on it is not clean.* **[R:migration]**
- **Step 2 — Single thin presenter.** Route **all** writes (key/touchscreen/brightness/clear/screensaver/close) through one owner; move dedup into it; split detection from teardown (§3.5). *Still per‑key writes.* **Couples with Step 3** — do them together, because routing `clear`/brightness through the owner while `clear_media_player_tasks` still wipes the queue causes a screensaver regression. **[R:migration]**
- **Step 3 — Replace busy‑waits with generation/event handoff.** *Latency win lands here.* **Precondition:** Step 1's enqueue must be verified order‑independent under concurrent enqueue (fault‑injection), else removing the waits widens the straggler window. **[R:migration]**
- **Step 4 — Immutable RenderModel snapshot per switch.** Kills torn frames; provides the frame‑complete barrier for batched present; fixes the video‑background switch (composite keys over the first tile). Reframe present as "no cross‑page interleave" (§4.3).
- **Step 5 — Unify animation + screensaver as coordinator modes;** resolve the `on_ready` atomicity‑vs‑async decision (§6/§8); advance animation counters only post‑present.
- **Step 6 — Profiling‑driven render topology.** Measure render (Python‑vs‑C fraction) on a 32‑key XL; decide single vs sharded renderers and whether the XL needs a dedicated split. Define remote/fake‑deck semantics. **[R:perf]**

### 7.1 Testing (the review's strongest process critique)

Hardware testing alone gives false confidence — the target races are narrow. Add a **fault‑injection harness**: force a straggler enqueue between render and flush; inject a `TransportError` mid‑frame; drive rapid switch/reload/screensaver storms deterministically; run every step with **2 decks**. FakeDeck cannot exercise suspend/USB‑re‑enumeration, so Steps 5–6 need a documented manual suspend/hotplug protocol. **[R:migration][R:lifecycle]**

---

## 8. Open decisions (updated with review answers)

1. **`on_ready` timing (the sharp one).** Keep on the switch critical path (first frame includes plugin content, but a slow plugin stalls the switch), *or* present a default first frame then fill asynchronously (no stall, but keys pop in)? These are mutually exclusive. **Recommendation:** keep painting `on_ready`s on the critical path with a bounded timeout, so the common "set my image in on_ready" pattern stays flicker‑free. Your call.
2. **Render topology.** Presenter is fixed as sole write‑only owner. Renderers: reuse the callback pool + a decode pool now, revisit sharding after profiling the XL. Agree to defer via profiling rather than commit now?
3. **Scope.** Ship **Step 0 + Step 1** (correctness) and stop, ship through **Step 3** (correctness + latency), or commit to the full **0–6** arc? Given the review, 0→3 is the natural correctness‑plus‑latency milestone; 4–6 is a larger, separable project.
4. **Immutable snapshot cost (Step 4).** Deep‑snapshotting every input's visual state per switch has memory/CPU cost on a 32‑key XL. Acceptable, or do we need copy‑on‑write?

*(v1 Q2 "epoch attribution: both work" is **resolved: neither** — replaced by the per‑input generation captured atomically, §4.1. v1 Q1 "one thread or two" is **resolved:** presenter is always its own thread; render topology is a profiling decision.)*

---

## 9. Known‑hard / still‑open

- **Immutable RenderModel vs live `ControllerInputState`.** Step 1 reduces bleed but cannot fully prevent *torn* frames until config is snapshotted (Step 4); the isolation boundary needs precise definition (which fields are snapshotted, how plugin deltas apply).
- **Coordinator is not single‑threaded GTK.** `load_page` is invoked from GTK, USB‑event (screensaver hide), API, and monitor threads; the "serialized coordinator entry" must actually serialize these (a lock or a single queue), which today it does not.
- **GIL contention** between CPU‑bound plugin callbacks and renderers is real; the FPS‑warning banner will trip on an XL that can't hit 30 FPS. Needs the profiling in Step 6.
- **USB reader thread** (`deck.run_read_thread`) ownership/shutdown ordering relative to the presenter and callback pool is unspecified.

---

## 10. Follow‑up: UI‑threading hardening (separate project — surfaced 2026‑06‑30)

The rapid‑switch stress test (enabled by the committed latency win, `7affed2`, and the Step‑4 bleed fix, `dbea6d2`) surfaced a cluster of **pre‑existing** GTK threading/lifecycle crashes, independent of the render pipeline. `faulthandler` (added in `main.py`, commit `a9f8376`) or `kill -QUIT <pid>` pinpoints each. Scoped follow‑up:

1. **UI‑mirror paints on disposed widgets** — *fixed & committed.* `ScreenBarImage.set_pixbuf_and_del` and `KeyButton.show_pixbuf` painted their widgets unconditionally at `PRIORITY_HIGH`; a page‑switch UI rebuild could paint a torn‑down widget → libgtk‑4 crash. Both now guard `get_mapped()` + try/except and use default idle priority. **Robust follow‑up:** replace the push model (a pixbuf per render via `idle_add`) with a **pull model** — one low‑rate GTK timer reads the latest per‑input image — removing the "paint during rebuild" class entirely and cutting main‑loop pressure.

2. **Plugin `on_ready`/`on_update` build GTK off the main thread** — *FIXED (Option D), pending hardware test (2026‑07‑01).* `load_all_inputs` runs `load_from_input_dict` on a `ThreadPoolExecutor` worker, which calls `own_actions_update` → `on_update` → `on_ready` **synchronously on that worker**; plugins (e.g. HomeAssistant PerformAction via GenerativeUI `ComboRow.populate`/`add_items`) build real GTK there → GTK‑off‑main‑thread → crash under load. Pre‑existing on `main` (since `e83fdfc`), not a branch regression. **Fix:** a `run_on_main()` helper (blocking marshal, inline‑if‑already‑main) + `@on_main` decorator in `GtkHelper/GtkHelper.py`; `GenerativeUI.signal_manager` marshals every decorated mutator; the base `GenerativeUI.__init__(build=…)` runs each subclass's construction closure on main (template method); remaining imperative mutators use `@on_main`. Image/`set_media` compositing stays on the pool. Risk to validate: `run_on_main` blocks the worker on the GTK loop — hang only if the main loop isn't iterating during the first startup load.

3. **Incidental bugs found during review** (worth filing): `allow_relaod` typo at `PageManagerBackend.py:340` (a `@log.catch` swallows the arg‑binding error, so that reload silently no‑ops); the GenerativeUI action‑reuse `#FIXME: gets never used` at `Page.py:209`.

4. **`@background` decorator (deferred to this pipeline work)** — the mirror of `@on_main`: push slow work *off* the GTK thread onto a bounded pool so handlers don't freeze the UI, formalizing the ad‑hoc `threading.Thread(target=load_background/load_screensaver)` and `_submit_action_callback` patterns. Returns a `Future`. Caveats to bake in: (a) only helps for I/O‑ or GIL‑releasing C work (network, disk, PIL/cairo, subprocess RPC) — pure‑Python CPU needs `multiprocessing`; (b) must submit to a bounded, lifecycle‑managed pool (reuse `action_executor`) to avoid the thread explosion `a8c25bd` fixed; (c) fire‑and‑forget / done‑callback only — `.result()` on the GTK thread while the work calls an `@on_main` method deadlocks.

---

*Grounded in the `render-subsystem-audit` (7 Explore agents) and `design-review-render-pipeline` (5 adversarial lenses). File:line references are against the current `fix/action-callback-thread-pool` state; re‑verify as code moves.*
