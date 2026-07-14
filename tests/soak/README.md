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
