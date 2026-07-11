# Single-Writer Migration — Implementation Plan (Steps 2+3 + Step 5)

**Status: v2 — EXECUTED 2026-07-05 (M0–M4 on `refactor/single-writer`).** v1 was reviewed by four adversarial passes (coverage gaps, concurrency, over-engineering, internal consistency/feasibility); v2 integrates their findings. §10 carries the finding-by-finding disposition; the headline change is architectural: **no new presenter thread** — the media thread becomes the sole device writer.

**As-built notes (deltas from this plan as executed):**
- **Media-thread-as-writer confirmed.** No new thread/module was added at any milestone; `MediaPlayerThread` is the sole device writer behind `control_q`, exactly as §2/§6.1 specify.
- **Resume repaint is a pending-retry, not a one-shot hook.** §3's "reopen-success path gains a hook" became a small coordinator revision: `check_resume_gap`/write-error edges call `_schedule_full_repaint()`, which arms a flag that `_run_pending_repaint()` drains on a rate-limited (2 s) retry loop each media-thread wake — idempotent under repeated failures, not a single fire-and-forget call.
- **Keep-verdict path re-verified, not re-derived.** `perform_media_player_tasks`'s drain-then-snapshot judge (§2.2/§5) was confirmed byte-for-byte unchanged at each milestone rather than re-proven from scratch; no deviation, just how the gate was checked.
- **Harness lives at `tests/`** (not a scratchpad) from M0 onward, per §4 M0/§7 — 16 committed `scenario_*.py` files plus `fixtures.py`/`faulty_fake_deck.py`/`run_all.py`, all green as of M4.

Parent design: `docs/render-pipeline-design.md` (CLOSED 2026-07-02). This plan executes its deferred **Step 2** (single device owner), the presenter-native remainder of **Step 3** (switch-boundary/control integration — the busy-wait latency win already landed), and **Step 5** (screensaver as serialized mode; animation counters decoupled from paint fate). All file:line references verified against the working tree on 2026-07-04.

---

## 0. Goals, non-goals, success criteria

**Goals:**
1. **One device owner.** Exactly one thread per deck performs device writes. Today's correctness rests on a web of invariants (BetterDeck RLock + gen tokens + dual-hash + enqueue guard + lock-ordering rules); afterwards the topology guarantees no concurrent writes and the invariants become defense-in-depth.
2. **Screensaver becomes single-writer** — its three-thread unlocked state machine (`ScreenSaver.py:28-164`) is replaced by serialized transitions (and its full requester set is six callers, not three — §5).
3. **One error policy**, per-controller, in one place — replacing two duplicated per-task-class `ClassVar` blocks plus a third copy on the resume path.
4. **Animation counters stop depending on paint fate** (wall-clock picking, §6).
5. **Fix two real bugs found during planning:** nothing repaints a static page after a beta-resume handle reopen (frames dropped during suspend are simply lost), and dedup state survives a `clear()` (blank-after-screensaver class).
6. **No regression** in interactive latency, animation smoothness, switch behavior, screensaver, suspend/resume, hotplug, shutdown.

**Non-goals:** render topology (Step 6), immutable snapshots (Step 4), remote-transport semantics, plugin-facing API changes, raising the 15 Hz video write cap (documented as a *stretch experiment*, not built for — §9).

**Success criteria:**
- `grep`-provable: no call site of `deck.set_key_image / set_touchscreen_image / set_screen_image / set_key_color / set_brightness` and no multi-write clear outside `DeckController`'s media thread paths, except the documented bootstrap probe (§4 M1). (`set_screen_image`/`set_key_color` have **zero** in-tree callers today and must stay that way.)
- Owner-assertion mode (`STREAMCONTROLLER_ASSERT_DEVICE_OWNER=1`, one env name everywhere) runs the harness suite and a hardware soak with zero violations.
- Harness scenarios (§7) green at every milestone; per-milestone hardware checklist.
- Interactive paint latency ≤ baseline; media-loop FPS ≥ baseline (`STREAMCONTROLLER_MEDIA_PROFILE=1` on the SD+).

---

## 1. Current state (inventory summary — corrected)

**The media thread is already the de-facto presenter.** `MediaPlayerThread` (`DeckController.py:189-457`) drains `tasks` (generic render/load callables), `image_tasks` (dict keyed by key index — latest-wins slots), and `touchscreen_task` (single slot); judges every paint at the present boundary against an atomic `(active_page, gen)` snapshot under `_page_gen_lock` — **draining before snapshotting**, which is load-bearing (`:396-398`: the reverse order would drop a just-queued new-page frame, unrun). It already has event wake, idle throttling, bounded `stop()`, and is already the only writer of `_last_img_hash` (`:160-164`). What it is *not* today: the **sole** writer.

**Writes that bypass it (complete list — v1 missed two rows):**

| Op | Call sites | Thread(s) |
|---|---|---|
| `set_brightness` (`:1025-1028`) | init `:564`, `load_brightness :831`, ScreenSaver `:111,150`, DeckGroup UI `:159` | GTK, Timer, switch threads |
| `clear()` (`:1118-1131`) | init `:478` (bootstrap probe), **`load_page(None)` `:966-968`**, ScreenSaver show `:113`/hide `:131`, `close_all` (`DeckManager.py:275`) | GTK, Timer, switch, shutdown |
| `deck.close()` | error paths `:136,182`, init failure `:484`, `close_all`, resume `DeckManager.py:336` | media thread, monitors, shutdown |
| `open/reset` | `DeckManager.py:136,143,309,336`, startup reset | monitors, main — lifecycle, stay direct |
| **`on_resumed` handle swap** (non-beta only) | `DeckManager.py:315-341` — replaces `deck_controller.deck` with a new `BetterDeck`, calls `update_all_inputs()`, has its own removal branch `:336-338` | resume thread |

Additional paint sources, all already funneling through `add_image_task`/`add_touchscreen_task` (verified — no side-channel writes): press feedback (`:2511-2522`), `set_state` (`:2313-2325`), `tick_actions`' 1 Hz `i.update()` sweep (`:1059-1066`, active during screensaver too), plugin `set_media`/`set_label` via `ActionCore` → `ControllerKey.update() :2424-2472`.

**Dead code discovered:** `MediaPlayerThread.pause` (written once, `:207`, never set true) — delete in M1. `check_connection` (`:442-457`) — only call site commented out (`:227`) — delete in M2.

**Dedup contract:** `_last_enqueued_hash` set at enqueue (`:2470`), `_last_img_hash` set only on successful USB write by the media thread (`:164`), skip only when **both** match (`:2444-2449`). `clear()` today touches neither — that is the blank-after-clear coherence bug this plan fixes in M2.

**Error policy:** beta-resume (default **on**) swallows `TransportError` without counting (`:127-129,171-173`); non-beta counts per-serial to 5, then the writing thread closes the deck inline and calls the (idempotent, `DeckManager.py:224-233`) `remove_controller`. Because beta-resume defaults on, the whole 5-strike apparatus is near-dead code for most users — see the open question in §9.

**Resume-repaint gap (real bug, inherited from today):** under beta mode the handle is reopened by the read thread (`beta_resume.py:41-61`); nothing repaints afterwards — `DetectResumeThread` (which calls `update_all_inputs`) only runs in non-beta (`DeckManager.py:84-85`). Static pages survive suspend only if the device retained its framebuffer.

**Screensaver:** `show()` swaps `deck_controller.inputs` wholesale, writes brightness+clear directly, from a `threading.Timer`; `hide()` runs from the USB-event path and calls `load_page(active_page, allow_reload=True)`. **Six** requester classes (§5). No lock.

**Animation counters:** `InputVideo` advances on tick ratio (`KeyVideo.py:36-45`); `KeyGIF` advances at render (`:1466-1475`); `BackgroundVideo` uses wall-clock picking when its cache is complete and **sequential advance while building** (`:1410-1433` — both branches are load-bearing, see §6).

---

## 2. Target architecture

**No new thread, no new module.** `MediaPlayerThread` becomes the explicit single writer (docstring + assertions declare the role; class name kept to avoid churn). Two additions to its loop; everything else is caller rerouting.

```
GTK / switch / Timer / USB-event / shutdown threads
        │  submit_control(msg)          — non-blocking, FIFO deque + wake
        ▼
┌────────────────────────────────────────────────┐
│ MediaPlayerThread (sole device writer)         │
│  per wake: 1. drain control_q FIFO             │
│            2. animation tick (unchanged)       │
│            3. perform_media_player_tasks()     │
│               (drain-then-snapshot, UNCHANGED) │
└──────────────────────┬─────────────────────────┘
                       ▼
              BetterDeck (RLock retained as defense-in-depth;
              owner assertion in harness/debug runs)
```

### 2.1 Control messages

`control_q: collections.deque` (append/popleft are GIL-atomic; no new lock). Messages, FIFO among themselves:

- `SetBrightness(value)`
- `Clear(seq)` — **sequence-stamped** (see 2.2)
- `ClearAndClose()` — terminal: wipe slots, write blanks, `deck.close()`, exit loop
- *(that's the whole set — no `DropFrames` message: slot dropping stays a synchronous call, see 2.3; no rotation/reset messages: no device write involved / lifecycle-only)*

Every frame submission (`add_image_task`/`add_touchscreen_task`) is stamped with a monotonic `submit_seq` (single `itertools.count` per controller; `next()` is GIL-atomic). `Clear(seq)` captures the counter at submission and, at drain time, wipes **only slots whose frame seq < clear seq**, resets dedup state (from M2), then writes the blanks. Frames submitted after the Clear survive and paint after the blank — this preserves today's synchronous-`clear()` happens-before exactly (caller does clear-then-paint; device sees blank-then-content), without which a static-image screensaver would go permanently black (review finding C-F2).

### 2.2 Loop contract

1. **Drain `control_q` fully, first, each wake.**
2. Animation tick — unchanged (`:230-269`), including the 15 Hz video render gate (`:236-253`), which remains the **sole** pacing mechanism (v1's presenter-side pacing backstop and `Frame.source` classification: cut).
3. `perform_media_player_tasks()` — **byte-for-byte unchanged**: drain all three queues, *then* snapshot `(active_page, gen)` under `_page_gen_lock`, then judge, then write (`:395-441`). The drain-then-snapshot order is the invariant that makes a just-queued new-page frame survive (`:396-398`).
4. **Wake latency:** the active-FPS sleep becomes `_wake_event.wait(wait)` like the idle path (`:292-296` currently splits them), so control ops and interactive paints never wait a full tick.
5. **Errors** (from M2, per the §9.1 graduation decision): attempt-every-write-and-swallow (today's default-mode semantics — *not* a latch that stops attempting; no SUSPENDED enum, review cut O-3/C-F4). Controller removal comes solely from USB disconnect events (`DeckManager.on_disconnect`), which is already the de-facto behavior with beta-resume defaulting on. The 5-strike apparatus is deleted, not relocated.

**Ordering guarantee stated honestly:** control ops are FIFO among themselves; frames order against `Clear` via seq stamps; frames order against each other per-slot latest-wins; cross-page/state correctness is the existing gen judgment. This *is* today's consistency model with `clear()`/brightness moved from "caller-synchronous" to "seq-ordered queue resident" — the seq stamp is what makes that move order-preserving.

**Blocking rules:** all submit APIs non-blocking. The media thread never blocks toward GTK (`GLib.idle_add` only — already true). Control ops can be delayed behind an in-flight generic task (e.g. the background-decode await in `_update_all_inputs_awaiting_background`); accepted and documented — the deferred-split re-open trigger is *measured* control starvation (§9).

**Handle discipline (C-M-C):** the writer resolves `self.deck_controller.deck` **at each write** (the task classes already do, `:157`); nothing may cache the `BetterDeck` across cycles, because non-beta `on_resumed` swaps it (`DeckManager.py:324`). The swap also resets the failure counter (M2).

### 2.3 What stays exactly where it is

- `image_tasks`/`touchscreen_task` slots, the judge, `add_image_task`/`add_touchscreen_task` and both their callers (`:2471`, `:2857`): untouched.
- `clear_media_player_tasks(gen)` (`:1145-1154`): untouched — its gen-guarded, `_page_gen_lock`-atomic triple wipe is correct and **must stay a synchronous call**; a queued equivalent judged against a captured gen can destroy newer-gen frames after a writer stall (C-F3).
- `_last_enqueued_hash` enqueue guard (`:2444-2449, :2470`): untouched; still load-bearing under a single writer (in-flight frames still exist).
- `BetterDeck` + RLock: retained permanently as the third-party-plugin defense (plugins can reach `deck_controller.deck`); the owner assertion is harness/dev tooling, not a shipping mode. These are the *stated* permanent/temporary roles (review flagged v1 for shipping three overlapping defenses with no disposition).
- **Bootstrap exception:** `__init__`'s `clear()` (`:478`) stays a direct synchronous write — it is the liveness probe whose exception aborts construction; renamed `_clear_direct()`, assertion-allowlisted.
- Lifecycle `open/reset` (DeckManager): direct — they act on decks with no live controller.

### 2.4 Shutdown ordering (X-M-A — v1 broke this)

`on_quit` (`app.py:191-243`) currently runs `ctrl.delete()` *before* `close_all()`; once `clear()` is queue-routed, that order would stop the writer before the final clear+close executes. Fix in M1: `close_all()` submits `ClearAndClose` per deck and joins each media thread bounded (2 s; the existing 6 s force-quit timer backstops), and runs **before** the `delete()` loop; `delete()` skips `media_player.stop()` when a terminal message was consumed. The second quit path (`headerBar.py:112-116`: `close_all()` then `os._exit(0)`) has **no** force-quit backstop — its safety is exactly `close_all`'s bounded join; harness asserts the journal ends `…clear, close`.

---

## 3. Dedup and resume repaint (M2)

- `_last_img_hash` stays a per-input attribute (v1's presenter-owned `SlotId → hash` map: cut — it preserved half of an AND-condition whose other half lives on objects the screensaver churns; it could never fire a skip the attribute can't, O-6). The media thread remains its sole writer.
- `ControllerTouchScreen` gains the same attribute pair — touchscreen dedup for free (saves redundant 800×100 JPEG writes on unchanged composites).
- `Clear` resets the attributes on all current inputs (and the touchscreen) — the dedup-coherence fix. Note screensaver transitions swap input objects wholesale, which already implicitly resets; `Clear`'s reset covers the non-swap callers.
- **Resume repaint (real-bug fix):** `beta_resume.py`'s reopen-success path (`:41-61`) gains a hook: null all dedup hashes, then `update_all_inputs()`. Without the nulling, a re-rendered identical frame would be hash-skipped against a pre-suspend write the device may have lost (C-F4). This is the one place "not advancing but not invalidating" the hash bites.

---

## 4. Migration sequence

Branch: `refactor/single-writer`, **based on `local-integration`** (it needs deck-core + media-pipeline + dial-starvation code simultaneously, so it is the first topic branch not independently PR-able against `main` until the stack lands — accepted, stated). Current uncommitted working-tree changes (video cache rewrite, sweeper, page fixes) must be committed first. Each milestone = one commit series, compiles standalone, harness-green, hardware-gated before the next.

**M0 — Harness (precondition). ~2 sessions.**
- **Committed into the repo** (`tests/` — not session scratchpads; every later gate depends on these artifacts persisting).
- `FaultyFakeDeck(FakeDeck)`: write journal `[(t, seq, op, slot, bytes_hash, thread_name)]`, scriptable `TransportError` schedule, per-write latency injection, key/dial event injection. Fix `FakeDeck` first: `is_touch()` returns the bound method (always truthy, `FakeDeck.py:87-88`), `key_layout` hardcoded `[2,4]` ignoring settings (`:28`), `id()` returns a fresh uuid per call (`:59-60`).
- **Two fixture tiers, explicitly specced (X-F15):**
  - *Unit tier* — stub controller exposing exactly the judge's needs (`_page_gen_lock`, `active_page`, `_page_load_generation`, `deck`, settings stub): drives loop/queue/judge/Clear-seq logic. The seam is "the writer's loop must be drivable with a stub controller"; M1 keeps it that way.
  - *Integration tier* — headless backend bootstrap: real `DeckController` over `FaultyFakeDeck`, using the existing `dev.n-fake-decks` settings path (`DeckManager.py:153-163`) + `--skip-load-hardware-decks` (`globals.py:19`). No GTK main loop needed (UI touches are `recursive_hasattr`-guarded). ScreenSaver transitions test at this tier — they are integration by nature.
  - App-level: **manual smoke only** — page-switch storms via the existing `SetActivePage` DBus method (`src/api.py:87`) / `change_page` GAction. v1's automated DBus storms are cut: no brightness/screensaver DBus methods exist (X-F10), and dasbus serializes handlers on the GLib loop anyway, so it can't produce cross-thread interleavings — the tiers above own those.
- Owner-assertion tooling (`STREAMCONTROLLER_ASSERT_DEVICE_OWNER`) built **here** and run from M1 onward (an assertion that verifies "one writer" has maximum value while the write paths are being cut over, not as a finale — O-5).
- Hardware baseline: profiler medians + subjective latency (press feedback, dial-spin-during-video).

**M1 — Control queue + single writer. ~1–2 sessions + soak.**
- `control_q` + drain-first; seq stamps on frame submits; seq-stamped `Clear`; event-based active-FPS wait; delete `pause`.
- Reroute: `set_brightness` → msg (callers unchanged); `clear()` → `Clear` msg + `_clear_direct()` bootstrap probe; ScreenSaver's four direct writes (`:111,113,131,150`) → msgs (state machine still old-shape; ownership comes in M3 — writes first, ownership second); `close_all` → `ClearAndClose` + bounded join; shutdown reorder per §2.4.
- **Show/hide bump `_page_load_generation`** (pulled forward from v1's M4, X-F6): cheap, and it means post-transition frames outrank pre-transition stragglers from M1 on.
- No write relocation happens in this milestone at all — v1's highest-risk cut (moving slots to a new thread) no longer exists.
- *Hardware gate: full soak — switch feel, screensaver entry/exit with static image AND video, brightness slider, shutdown leaves deck dark.*

**M2 — Beta-resume graduation, dedup coherence, resume repaint. ~1 session.**
- Graduation per §9.1 (decided): delete the non-beta apparatus wholesale — `DetectResumeThread`, `on_resumed`/handle swap, `ClassVar` counters + 5-strike removal, `beta-resume-mode` settings reads + UI toggle, vestigial `beta_resume.py` + unused import, dead `check_connection`. Both write-task error paths collapse to log-and-swallow.
- Dedup per §3 (touchscreen attrs, Clear resets) + resume repaint **without touching the vendored library**: (a) the media loop detects a process-suspend by a wall-clock gap ≥5s between iterations (DetectResumeThread's proven technique, relocated into the existing loop — no new thread) and (b) the unified write-error handler triggers on a failure→success edge. Either trigger nulls all dedup hashes and schedules `update_all_inputs()`; (a) covers the static-page/no-writes case, (b) covers a repaint attempted before the library's read thread finished reopening the handle. Idempotent, cheap, both needed.
- *Hardware gate: suspend/resume with a static page (the framebuffer-loss case — the reason this milestone exists), USB pull mid-video, replug.*

**M3 — Screensaver serialization. ~1–1.5 sessions.**
- **All six requesters** route through one transition entry: `on_timer_end` (Timer), `on_key_change` (USB-event), `set_enable` (GTK), `LockScreenManager.lock/unlock` (`LockScreenManager.py:66-70` — hits *all* controllers and flips `allow_interaction`), `Page.update_input(wake=True)` (`Page.py:727-731` — called by ~14 page setters from **action-pool threads**), `__init__:572-574` (deck reconnect while screen locked; also fixes the presenter-must-exist-before-`:564` construction-order note).
- **Transition structure (fixes the v1 blocker G-B1 and C-F6/C-F7):**
  1. *Outside any lock:* pre-resolve heavy media — construct the screensaver `BackgroundVideo` (full-file md5 + capture open can take seconds) before acquiring anything.
  2. *Under `_load_page_lock`:* coalesce (`showing == target` → return), flip `showing`, swap inputs, bump gen, submit `SetBrightness`/`Clear`, swap the pre-built background in via `_background_load_lock` with a gen re-check inside (the screensaver path today skips that lock, letting an in-flight `load_background` worker overwrite the screensaver background after the bump — C-F6). Lock order fixed as `_load_page_lock` → `_background_load_lock`, matching `load_page`; never reversed anywhere.
  3. *After release:* hide's `load_page(active_page, allow_reload=True)` runs **outside** the transition hold. This is the blocker fix: `_load_page_lock` is an RLock, so calling `load_page` inside the transition would re-enter it and run `initialize_actions`/`ChangePage` — deliberately kept outside the lock today (`:1004-1005`) — under the *outer* hold, re-arming the exact run_on_main/pulsectl deadlock this codebase already froze on. The transition never wraps `load_page`.
- Action-pool requesters block only on the (now heavy-work-free) locked section — bounded; noted in the deadlock analysis.
- *Hardware gate: screensaver with video background + dial interaction during entry/exit; lock-screen lock/unlock.*

**M4 — Wall-clock animation counters + docs. ~1 session.**
- `InputVideo`: **both** BackgroundVideo branches — sequential +1 advance while `is_cache_complete()` is false (pure wall-clock during a first-playthrough cache build causes per-tick decode amplification: each jumped frame decodes+writes every intermediate frame under the cache lock — C-F8), wall-clock picking once complete, with the >1 s gap reseed.
- `KeyGIF`: cumulative-delay timeline (`list(accumulate(frame_delays))` at load, `bisect` by elapsed — fewer lines than the loop it replaces; variable frame durations make a single-fps factor wrong).
- Unit scenarios for both (the GIF timeline is new arithmetic and trivially unit-testable — X-F14).
- Docs: parent doc disposition header updated (Steps 2/3/5 landed; Step 4/6 + pull-model mirror remain deferred); memory notes.
- *Hardware gate: GIF keys + key videos + bg video with strip extension simultaneously; frame-rate vs M0 baseline.*

---

## 5. Semantics preserved — contract checklist

| Behavior | Where enforced after migration |
|---|---|
| Paint dropped when page switched away / gen superseded | judge, unchanged (`:419-430`) |
| **Drain-then-snapshot** (new-page frame queued mid-cycle survives) | `perform_media_player_tasks` unchanged (`:396-415`) |
| Dropped paint must not advance dedup hash | hash recorded only on successful write, media thread sole writer (`:160-164`, unchanged) |
| Out-of-order enqueue rejected | `_last_enqueued_hash` guard, unchanged |
| Latest-wins per slot | `image_tasks` dict, unchanged |
| Switch clears pending work, gen-guarded, atomically | `clear_media_player_tasks` unchanged — synchronous, never a queue message |
| clear-then-paint caller order = blank-then-content device order | seq-stamped `Clear` (new, §2.1) |
| Clear resets dedup state | M2 (today: neither hash touched — parity until M2, fixed after) |
| `initialize_actions`/`ChangePage` outside `_load_page_lock` | untouched; screensaver transition structured to never wrap `load_page` (§4 M3) |
| beta-resume swallows errors, keeps attempting | unchanged semantics; + repaint-on-reopen fix (M2) |
| non-beta 5-strike removal, handle closed before reap | unified handler, close inline as today (M2) |
| Rotation: no device write; in-flight frame may land post-rotation until `load_page` repaints (parity); gen bump via the rotation path's `load_page :1050` | unchanged; row added for completeness |
| `tick_actions` 1 Hz repaint sweep (incl. during screensaver) | funnels through `add_image_task`, gen-stamped via `init_inputs :587` — covered, unchanged |
| Video writes capped 15 Hz | ticker render gate — the sole mechanism |
| Idle CPU ~2 FPS | unchanged |
| Bounded teardown; both quit paths leave deck cleared+closed | §2.4 (fixed ordering; headerBar path relies on the bounded join — stated) |

---

## 6. Deliberate deviations from the parent design doc

1. **No separate thin presenter thread.** The parent doc's presenter-un-stallable-by-render rationale rested on render being expensive and an XL-scale fleet — both overturned by this repo's own profiling (render was made cheap: fg-cache/encode-memo, 19→29.9 FPS) and hardware reality (one SD+). The split's one real buy — control-op latency bounded independently of ticker tasks — is not worth a new thread, module, lock, and cross-thread dedup handoff today. **Re-open triggers:** measured control-op starvation behind ticker tasks; Step 6 (XL topology); remote transports.
2. **Animation counters: wall-clock, not post-present feedback.** Same user-visible property, zero new coupling, production-proven in `BackgroundVideo` — including its sequential-while-building branch, which is part of the contract, not an implementation detail.
3. **BetterDeck RLock retained permanently** (third-party-plugin defense); owner assertion is dev/harness tooling only. Stated disposition, no permanent belt-and-suspenders.
4. **No new coordinator object.** `_load_page_lock` (now also serializing screensaver transitions) + gen authorship in `load_page` *is* the coordinator.
5. **No SUSPENDED state, no `DropFrames` message, no pacing classes, no Neo/`set_key_color` slots** — each was mechanism without a behavior delta or without a caller (dispositions in §10).

---

## 7. Test matrix

| Scenario | Tier | Milestone | Pass criterion |
|---|---|---|---|
| Switch storm ×200 | integration | M1+ | journal: no cross-page frame after last switch; no stranded blanks |
| Straggler injection (write-latency during switch) | unit | M1+ | stale gen dropped; correcting paint lands |
| **Clear-vs-frames ordering:** static-image screensaver entry under injected write latency | unit | M1 | journal ends blank → screensaver image (image after blank, never wiped) |
| Screensaver **exit** repaint completeness | integration | M1+ | every key repainted after hide; no blank survivors |
| Screensaver entry during old-page video | integration | M1+ | no old-page frame after the Clear; screensaver media paints within one tick |
| Shutdown during active video (both quit paths) | integration | M1+ | journal ends `…clear, close`; join bounded |
| TransportError ×6 non-beta | unit | M2 | handler fires once; deck closed; controller removed once; no stall >100 ms |
| TransportError burst, beta | unit | M2 | swallowed, keeps attempting, no removal |
| Suspend/resume repaint (framebuffer-loss sim: journal cleared, then reopen) | integration | M2 | full repaint lands post-reopen (dedup did not skip) |
| Six-requester transition storm (Timer+USB+GTK+pool threads) | integration | M3 | serialized; `showing` consistent; watchdog 5 s no deadlock |
| Blocked-plugin transition (sleep in ChangePage handler) + concurrent load_page | integration | M3 | no deadlock; transition completes (the G-B1 regression test) |
| Background-load vs screensaver race (delayed `load_background` worker) | integration | M3 | screensaver background wins after bump (C-F6 regression test) |
| GIF wall-clock timeline: variable delays, tick gaps, loop/no-loop | unit | M4 | time-correct frame via bisect |
| KeyVideo during cache build | unit | M4 | sequential advance, one decode per tick (no amplification) |
| Two fake decks, storm both | integration | M1+ | journals independent; counters per-instance (M2) |
| Owner assertion | all runs | M1+ | zero violations |
| Hardware checklist | hw | every M | §4 per-milestone gates |

---

## 8. Risks

- **Highest-risk milestone is now M1** (control queue + shutdown reorder) — but it is additive routing, not relocation; the write paths and judge do not move. Mitigation: harness first; seq-Clear unit scenarios; full soak.
- **Control ops can lag behind a long generic task** (background decode await) — accepted; bounded by decode time (small since the canvas-cache rewrite); re-open trigger stands (§6.1).
- **Screensaver transition holds `_load_page_lock` briefly from action-pool threads** — bounded by a heavy-work-free critical section (§4 M3 structure); the blocked-plugin scenario guards the structure.
- **Beta-resume graduation** (§9) may delete much of M2's unified handler later — M2 keeps it small precisely for that.

## 9. Open questions / stretch

1. **Beta-resume graduation — DECIDED 2026-07-04: graduate.** Resume-from-suspend behavior (library-side handle reopen, `reconnect_after_suspend`) becomes the only mode. M2 accordingly **deletes** rather than unifies: `DetectResumeThread` + `on_resumed` + the `BetterDeck` handle swap (`DeckManager.py:315-341,360-375`), both per-task-class `ClassVar` counters and the 5-strike removal, all `beta-resume-mode` settings reads and the Settings toggle (`Settings.py:725-765`), the vestigial `beta_resume.py` + its unused import (`DeckManager.py:39`), and dead `check_connection`. `deck.open()` always passes `resume_from_suspend=True`. Error policy simplifies to: swallow `TransportError` on writes (today's default-mode behavior — controller removal comes only from USB disconnect events, which is already the de-facto reality). The resolve-deck-at-each-write contract (§2.2) stays as cheap hygiene even though the swap is gone.
2. **Video write cap raise — DONE 2026-07-05.** Inter-write yields in bulk batches landed (`b7dcc06`: 1.5ms between writes for batches ≥4, interactive paints unpaced — forces the writer off the unfair transport mutex so the 20Hz HID read poll always gets a slot); cap default raised 15→30Hz after hardware verification: usb_write 49→83/s, loop_fps 32, dials confirmed clean by hand.
3. Rename `MediaPlayerThread` → `DeckWriterThread`? Cosmetic; deferred to M4 docs pass (log-grep continuity argues for keeping the name).

## 10. Review disposition (v1 → v2)

Findings from the four review passes — G=gaps, C=concurrency, O=over-engineering, X=consistency/feasibility. **Accepted unless noted.**

| Finding | Disposition |
|---|---|
| O-1 separate presenter thread unjustified | **Accepted — the v2 pivot.** Dissolves X-F5 (self-stop stall), removes v1-M1 relocation risk entirely; C-F1's drain-then-snapshot inversion becomes moot (code path now untouched). Deviation §6.1 with re-open triggers. |
| C-F1 snapshot-before-pop drops new-page frames | Accepted; `perform_media_player_tasks` now explicitly unchanged, invariant documented in §1/§5. |
| C-F2 / X-F6 async Clear loses happens-before (black screensaver) | Accepted; seq-stamped Clear (§2.1) + show/hide gen bump pulled into M1; harness criterion fixed (was accepting the failure mode as a pass, X-F12). |
| C-F3 queued DropFrames destroys newer-gen frames | Accepted; no DropFrames message exists — `clear_media_player_tasks` stays synchronous (§2.3). |
| C-F4 / O-3 / X-F9 SUSPENDED ambiguous; resume-repaint gap real | Accepted both ways: SUSPENDED cut (attempt-and-swallow stays); resume repaint + dedup invalidation added as a real-bug fix (§3, M2). |
| C-F5 DEAD reap leaks the deck handle | Accepted; close stays inline as today (§2.2.5). |
| C-F6 screensaver bypasses `_background_load_lock` | Accepted; transition swaps background under the lock with gen re-check (§4 M3). |
| G-B1 / C-F7 transition under RLock re-arms run_on_main deadlock; md5 under lock stalls GTK+USB threads | Accepted; three-phase transition: heavy work outside, decide under, `load_page` after release (§4 M3). |
| C-F8 pure wall-clock KeyVideo → decode amplification during build | Accepted; both branches kept (§4 M4, §6.2). |
| G-M-A / X shutdown ordering breaks async clear; headerBar path unbackstopped | Accepted; §2.4. |
| G-M-B six screensaver requesters (LockScreenManager, `Page.update_input(wake=True)` from pool threads, `__init__` reconnect) | Accepted; §4 M3; construction-order note included. |
| G-M-C on_resumed handle swap unaddressed | Accepted; resolve-at-write contract + counter reset + removal branch unified (§2.2, M2). |
| G-M-D / X-F10 DBus storm drivers don't exist; dasbus serializes on GLib loop | Accepted; app-tier automation cut to manual smoke (`SetActivePage` named); cross-thread interleavings owned by unit/integration tiers (§4 M0). |
| O-2 pacing classes / `Frame.source` | Accepted — cut; render gate is the sole mechanism; §9.2 stretch note. |
| O-4 / G-m3 Neo `("screen",)` slot + `set_key_color`: zero callers | Accepted — cut; kept only in the success-criteria grep list. |
| O-5 owner assertion placement | Accepted — built in M0, run from M1; dev tooling, not a shipping mode or milestone. |
| O-6 presenter dedup map | Accepted — cut; per-input attributes, media thread sole writer, Clear resets, touchscreen attrs added (§3). |
| O-7 / X-F15 app-tier brittle; unit-tier seam unspecified; harness must live in-repo | Accepted; two specced tiers + in-repo `tests/` (§4 M0). |
| O-8 milestone merges | Accepted in effect — v2 is four milestones (M0–M4), one fewer module, no thread. |
| O-9 GIF bisect "over-built" | **Rejected by the reviewer itself** — bisect is fewer lines and correct for variable delays; kept. |
| O beta-resume graduation observation | Accepted as §9.1 open question (user decision). |
| G-m1/X-F4 `pause` dead code; G-m6 `check_connection` dead | Accepted; deleted in M1/M2. |
| G-m2/X-F3 `clear()` caller at `:966-968`; resume removal branch in M2 scope; G-m5 rotation row; G-m8 `tick_actions` row | Accepted; §1/§5 corrected. |
| G-m4/X-F11 FakeDeck defects (`is_touch` bound method, hardcoded layout, unstable `id()`) | Accepted; fixed in M0. |
| G-m7 Clear loses today's gen guard | Accepted via the seq-stamp design (subsumed by C-F2 fix). |
| X-F1 §2.2 stated M3 semantics unannotated | Moot for error/dedup staging under the restructure; §2.1/§3 now carry explicit milestone tags. |
| X-F2 env-var name mismatch | Accepted; `STREAMCONTROLLER_ASSERT_DEVICE_OWNER` everywhere. |
| X-F13 M1's riskiest change untested until M3 | Restructure moves error-policy change to M2 **with** its scenarios; M1's new risk (Clear ordering) gets M1-tagged unit scenarios. |
| X-F16 effort understated | Accepted; §4 numbers revised (M0 ~2 sessions). |
| X-F17 branch base unstated; dirty working tree | Accepted; §4 header. |
| C-F9/F10/F11, X-F7/F8/F11, G items 4/5/9 | Attacks failed / checks passed — recorded here so they aren't re-litigated: dual-hash relocation sound (now moot), task/frame ordering not load-bearing, Clear wipe-window benign, M2→M3 dedup gap is parity with today, fake decks + `SetActivePage` feasible as claimed. |
