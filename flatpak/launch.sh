#!/bin/bash
cd /app/bin/Deckard
# Set here so main.py's own env check (see P0.4, docs/memory-footprint-plan.md
# §4 D5) finds them already present and skips its self-re-exec.
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=131072
python3 /app/bin/Deckard/main.py "$@"