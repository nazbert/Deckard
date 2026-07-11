#!/usr/bin/env python3
"""
mem_census.py -- bucket a process's anonymous VMAs by size class.

Reads /proc/<pid>/smaps and prints a table of anonymous-mapping counts and
total RSS per size-class bucket. This is the view that surfaces glibc
arena shapes: the live process examined in docs/memory-footprint-plan.md
§2 showed "anonymous regions of 61, 59, 57.6x3, 32MB -- the 57.6MB
triplets are classic glibc per-thread arena shapes", which a flat RSS
number can't distinguish from genuine content growth.

Usage:
    .venv/bin/python tests/soak/mem_census.py [pid]

If no pid is given, scans /proc for a process whose cmdline mentions
main.py and StreamController.
"""
import argparse
import os
import re
import sys

# (label, exclusive upper bound in kB) -- last bucket has no upper bound.
SIZE_CLASSES_KB = [
    ("<64KB", 64),
    ("64KB-256KB", 256),
    ("256KB-1MB", 1024),
    ("1MB-4MB", 4096),
    ("4MB-16MB", 16384),
    ("16MB-64MB", 65536),
    (">=64MB", None),
]

# smaps region header, e.g.:
# 7f1234000000-7f1234021000 rw-p 00000000 00:00 0                          [heap]
_HEADER_RE = re.compile(
    r"^[0-9a-f]+-[0-9a-f]+\s+\S+\s+\S+\s+\S+\s+\d+\s*(?P<pathname>.*)$"
)


def find_streamcontroller_pid() -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
        except OSError:
            continue
        if "main.py" in cmdline and "StreamController" in cmdline:
            return int(entry)
    return None


def bucket_for(size_kb: int) -> str:
    for label, upper in SIZE_CLASSES_KB:
        if upper is None or size_kb < upper:
            return label
    return SIZE_CLASSES_KB[-1][0]


def census(pid: int) -> dict[str, dict[str, int]]:
    """Return {bucket_label: {"count": n, "rss_kb": n}} for anonymous VMAs
    (mappings with no backing file -- heap, arenas, mmap'd anon regions;
    excludes file-backed mappings like .so's and mp4 cache files, and the
    kernel's [vdso]/[vvar]/[vsyscall])."""
    buckets = {label: {"count": 0, "rss_kb": 0} for label, _ in SIZE_CLASSES_KB}
    with open(f"/proc/{pid}/smaps") as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        pathname = m.group("pathname").strip()
        is_anon = pathname == "" or pathname.startswith("[anon") or pathname == "[heap]"
        size_kb = rss_kb = 0
        i += 1
        while i < len(lines) and not _HEADER_RE.match(lines[i]):
            line = lines[i]
            if line.startswith("Size:"):
                size_kb = int(line.split()[1])
            elif line.startswith("Rss:"):
                rss_kb = int(line.split()[1])
            i += 1
        if is_anon and size_kb > 0:
            label = bucket_for(size_kb)
            buckets[label]["count"] += 1
            buckets[label]["rss_kb"] += rss_kb
    return buckets


def print_table(buckets: dict[str, dict[str, int]]) -> None:
    print(f"{'size class':<14}{'count':>8}{'rss (MB)':>12}")
    total_count = total_rss_kb = 0
    for label, _ in SIZE_CLASSES_KB:
        b = buckets[label]
        if b["count"] == 0:
            continue
        print(f"{label:<14}{b['count']:>8}{b['rss_kb'] / 1024:>12.1f}")
        total_count += b["count"]
        total_rss_kb += b["rss_kb"]
    print("-" * 34)
    print(f"{'total':<14}{total_count:>8}{total_rss_kb / 1024:>12.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pid", nargs="?", type=int, default=None,
                         help="target pid (default: auto-detect the running StreamController)")
    args = parser.parse_args()

    pid = args.pid or find_streamcontroller_pid()
    if pid is None:
        print("No pid given and no running StreamController process found "
              "(looked for main.py in /proc/*/cmdline).", file=sys.stderr)
        return 1

    try:
        buckets = census(pid)
    except PermissionError:
        print(f"Permission denied reading /proc/{pid}/smaps (same user or root required).", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"No such process: {pid}", file=sys.stderr)
        return 1

    print(f"anonymous-mapping census for pid {pid}\n")
    print_table(buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
