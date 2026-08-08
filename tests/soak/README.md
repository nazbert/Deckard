# Memory soak procedure (Phase 0, P0.6)

Companion to `docs/memory-footprint-plan.md` and `docs/memory-footprint-impl-plan.md`
(Phase 0). These scripts don't replace the `tests/scenario_*.py` FakeDeck
harness (`tests/run_all.py`) -- they're for the longer, hardware-attached
soaks that the harness can't do: multi-hour idle drift, real USB
unplug/replug, and eyeballing where RSS actually goes with `mem_census.py`.

**These scripts talk to a real, running Deckard process (over
DBus and /proc), and one of them changes the active page on real
hardware.** Only point them at a Deckard instance you intend to
soak-test right now, not a system your device is actively depended on --
`soak_driver.py` will cycle its Stream Deck's displayed page.

## Setup

Run the app from source with telemetry (and, optionally, the trim probe)
enabled, so there's a `mem_telemetry.csv` to read afterwards:

```sh
SC_MEM_TELEMETRY=1 SC_MALLOC_TRIM=1 .venv/bin/python main.py
```

The CSV lands at `<DataPath>/logs/mem_telemetry.csv` (query `DataPath`
over the DBus API, or check `~/.var/app/io.github.nazbert.Deckard/data`
for a source run without `--data`).

## Automated driving: `soak_driver.py`

Cycles every connected controller through its configured pages over the
app's DBus API (`src/api.py`), dropping start/stop markers into
`mem_telemetry.csv` so the switches are visible against the RSS timeline:

```sh
.venv/bin/python tests/soak/soak_driver.py --cycles 100 --interval 1.0
```

If the app isn't running (or `dasbus` isn't importable in the interpreter
you ran this with), it prints why and exits 1 -- it never raises a
traceback into a soak log. Brightness and screensaver-force cycling aren't
exposed on the DBus API yet, so only page switches are driven; extend this
script once those methods land.

## Manual soak matrix

Things the DBus API doesn't reach yet -- drive these by hand, watching
`mem_telemetry.csv` (or `watch -n5 grep VmRSS /proc/<pid>/status`) across
each:

- **USB unplug/replug x20** (Phase 1's real gate, but worth a Phase-0
  baseline too): unplug the deck, wait for the disconnect to settle,
  replug. Repeat 20x. Watch thread count and fd count in the CSV --
  Phase 0 doesn't fix per-unplug leaks, so a slope here is expected and is
  the Phase 1 target, not a Phase 0 regression.
- **Config window open/close x20**: open the deck's settings/config
  window, close it, repeat. Watch RSS and gc counts.
- **Right-click x50** (key grid and dial context menus): each leaks one
  `PopoverMenu` today (bug 4 in the design doc's appendix, fixed in Phase
  1) -- Phase 0's read here is a baseline, not a pass/fail gate.
- **Image-cache ceiling leg (#142)**, 2 h minimum, on a page with a looping
  background video and `soak_driver.py --cycles 200` cycling pages
  underneath it. The leg has TWO valid regimes, chosen by where the ceiling
  sits relative to the deck's *active working set* -- measured at **~72 MB**
  on the reference rig (real pages + noise video; read it off `img_cache_kb`
  at the default ceiling before choosing):

  ```sh
  # churn-stress regime: ceiling BELOW the working set -- eviction runs
  # continuously; EXPECT lockstep `img_cache_evictions` and the re-admitted
  # tripwire in logs.log. Gates: bound never exceeded, fps unaffected.
  SC_MEM_TELEMETRY=1 DECKARD_IMAGE_CACHE_MB=48 .venv/bin/python main.py

  # no-thrash regime: ceiling AT/ABOVE the working set (e.g. 96) -- only
  # cold entries age out. Gates: evictions rising but NOT in lockstep with
  # the sample count.
  SC_MEM_TELEMETRY=1 DECKARD_IMAGE_CACHE_MB=96 .venv/bin/python main.py
  ```

  Shared gates for both regimes: every CSV row's `img_cache_kb` at or under
  the ceiling (one wake's worth of paints of slack, no more); media-loop fps
  (`DECKARD_MEDIA_PROFILE=1`) within noise of the same run on `main`, since
  the whole design premise is that the writer never stalls for the budget.
  Then repeat once at the default ceiling to confirm it does not bind on a
  normal rig (`img_cache_evictions` stays 0). Field reference (2026-08-08
  overnight, MR !94): 381 k evictions/2 h at ~53/s in the churn regime with
  the bound never exceeded and fps 32.3->32.2.
- **2+ hour idle** with the deck showing a page with looping bg video: this
  is the number that matters for Phase 0 -- with `MALLOC_ARENA_MAX=2` and
  the thread caps in place, does `VmSwap` still grow, or was it mostly
  arena fragmentation? That answer re-prioritizes Phase 5 (see the Phase 0
  gate note in the impl plan).

## Reading the results

```sh
tests/soak/mem_census.py <pid>          # anonymous-VMA size-class table (rss + swap)
tests/soak/mem_census.py <pid> --max-rss-mb 800 --max-swap-mb 200   # fail on breach
grep -v '^#' logs/mem_telemetry.csv     # the sampled rows, markers stripped
```

### Image-cache columns

The last five CSV columns come from the image-cache budget (#142) and are
what make its ceiling tunable against a real soak rather than a guess:

| column | meaning |
|---|---|
| `img_cache_kb` | Σ of the **evictable** native-image caches (every deck's `encode_memo` + `native_tile_cache`). This is the quantity `DECKARD_IMAGE_CACHE_MB` governs, so **no row may ever exceed the ceiling** by more than one wake's worth of paints -- that is the soak's mechanical gate. |
| `img_cache_evictions` / `img_cache_evicted_kb` | cumulative (monotonic) entries and kB the global ceiling has shed. Flat at 0 for a whole soak = the ceiling never bound, i.e. it is not the thing limiting RSS. A steep, continuous slope on an otherwise steady `img_cache_kb` = **thrashing**: the ceiling is fighting a live working set and every eviction is buying a re-encode. `logs.log` carries a matching `cache-budget: ... re-admitted N key(s)` warning when that happens. |
| `video_readers_kb` | census only, never evicted: the one-frame payloads held by background/key video readers. Grows with the number of video assets on screen, not with time -- a slope here is an instance leak, not a cache growing. |
| `gif_frames_kb` | census only, never evicted: whole decoded GIF frame lists. **The largest un-capped image holder** -- a 32-key page of 200-frame GIFs is ~0.9 GiB, roughly 10x the entire budget the ceiling governs. This column exists to size that follow-up against real pages. |

The default ceiling (`MemTotal/64`, clamped to 64-256 MiB) is deliberately
non-binding on a typical single-deck rig, so expect `img_cache_evictions` to
stay at 0 unless you tune `DECKARD_IMAGE_CACHE_MB` down or attach several
decks. Set it low on purpose for one soak leg to exercise the eviction path.

A run whose header does not match the current schema is rotated to
`mem_telemetry.csv.old` on the next start and a fresh file begun -- so a CSV
from before this change is preserved, not silently widened.

`mem_census.py` buckets anonymous mappings (heap, arenas, anonymous mmaps
-- not file-backed .so's or cache mp4s) by size class, reporting **both Rss
and Swap** per bucket plus the process-wide VmRSS/VmSwap from
`/proc/<pid>/status`. Swap is reported because the 2+ hour idle symptom was
RSS regrowth *plus* ~463MB VmSwap -- an Rss-only view under-reports it.
Before P0.4, expect several ~57.6MB entries (glibc's default per-thread
arena size on a 64-bit process); after, arena count should be bounded by
`MALLOC_ARENA_MAX=2` regardless of thread count.

Pass `--max-rss-mb` and/or `--max-swap-mb` to turn a soak into a mechanical
pass/fail check: the tool exits 1 (with a `THRESHOLD BREACH` line on stderr)
when the process-wide VmRSS/VmSwap exceeds the given ceiling, so an
overnight soak can fail on its own instead of needing a human to read the
table. Both flags are optional -- the bare `mem_census.py [pid]` invocation
is unchanged.
