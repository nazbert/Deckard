#!/usr/bin/env python3
"""
mem_census.py -- bucket a process's anonymous VMAs by size class, and read
its swap footprint.

Reads /proc/<pid>/smaps and prints a table of anonymous-mapping counts and
total RSS *and Swap* per size-class bucket, plus the process-wide VmRSS/
VmSwap from /proc/<pid>/status. This is the view that surfaces glibc
arena shapes: the live process examined in docs/memory-footprint-plan.md
§2 showed "anonymous regions of 61, 59, 57.6x3, 32MB -- the 57.6MB
triplets are classic glibc per-thread arena shapes", which a flat RSS
number can't distinguish from genuine content growth.

Swap matters because the field symptom (README.md "2+ hour idle") was RSS
regrowth accompanied by ~463MB VmSwap -- reading Rss alone under-reports it.

Usage:
    .venv/bin/python tests/soak/mem_census.py [pid]
    .venv/bin/python tests/soak/mem_census.py [pid] --max-rss-mb 800 --max-swap-mb 200

If no pid is given, scans /proc for a process whose cmdline mentions
main.py and StreamController. With --max-rss-mb / --max-swap-mb, exits 1 if
the process-wide VmRSS / VmSwap exceeds the given threshold -- so a soak can
fail mechanically instead of needing a human to eyeball the table.
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
    """Return {bucket_label: {"count": n, "rss_kb": n, "swap_kb": n}} for
    anonymous VMAs (mappings with no backing file -- heap, arenas, mmap'd
    anon regions; excludes file-backed mappings like .so's and mp4 cache
    files, and the kernel's [vdso]/[vvar]/[vsyscall]).

    Swap is summed per bucket alongside Rss: an anonymous region paged out to
    swap has a small Rss but a large Swap, so an Rss-only view under-reports
    where the memory actually went (the field symptom this tool exists for)."""
    buckets = {label: {"count": 0, "rss_kb": 0, "swap_kb": 0} for label, _ in SIZE_CLASSES_KB}
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
        size_kb = rss_kb = swap_kb = 0
        i += 1
        while i < len(lines) and not _HEADER_RE.match(lines[i]):
            line = lines[i]
            if line.startswith("Size:"):
                size_kb = int(line.split()[1])
            elif line.startswith("Rss:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("Swap:"):
                swap_kb = int(line.split()[1])
            i += 1
        if is_anon and size_kb > 0:
            label = bucket_for(size_kb)
            buckets[label]["count"] += 1
            buckets[label]["rss_kb"] += rss_kb
            buckets[label]["swap_kb"] += swap_kb
    return buckets


def read_vm_status(pid: int) -> dict[str, int]:
    """Process-wide VmRSS and VmSwap (kB) from /proc/<pid>/status -- the
    authoritative totals, independent of the per-VMA smaps walk. VmSwap is the
    field the soak README calls out (the 2+ hour idle symptom was RSS regrowth
    plus ~463MB VmSwap); reading VmRSS alone under-reports the footprint."""
    result = {"VmRSS": 0, "VmSwap": 0}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            for field in result:
                if line.startswith(field + ":"):
                    result[field] = int(line.split()[1])
    return result


def print_table(buckets: dict[str, dict[str, int]]) -> None:
    print(f"{'size class':<14}{'count':>8}{'rss (MB)':>12}{'swap (MB)':>12}")
    total_count = total_rss_kb = total_swap_kb = 0
    for label, _ in SIZE_CLASSES_KB:
        b = buckets[label]
        if b["count"] == 0:
            continue
        print(f"{label:<14}{b['count']:>8}{b['rss_kb'] / 1024:>12.1f}{b['swap_kb'] / 1024:>12.1f}")
        total_count += b["count"]
        total_rss_kb += b["rss_kb"]
        total_swap_kb += b["swap_kb"]
    print("-" * 46)
    print(f"{'total':<14}{total_count:>8}{total_rss_kb / 1024:>12.1f}{total_swap_kb / 1024:>12.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pid", nargs="?", type=int, default=None,
                         help="target pid (default: auto-detect the running StreamController)")
    parser.add_argument("--max-rss-mb", type=float, default=None,
                         help="fail (exit 1) if process-wide VmRSS exceeds this many MB")
    parser.add_argument("--max-swap-mb", type=float, default=None,
                         help="fail (exit 1) if process-wide VmSwap exceeds this many MB")
    args = parser.parse_args()

    pid = args.pid or find_streamcontroller_pid()
    if pid is None:
        print("No pid given and no running StreamController process found "
              "(looked for main.py in /proc/*/cmdline).", file=sys.stderr)
        return 1

    try:
        buckets = census(pid)
        vm = read_vm_status(pid)
    except PermissionError:
        print(f"Permission denied reading /proc/{pid} (same user or root required).", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"No such process: {pid}", file=sys.stderr)
        return 1

    print(f"anonymous-mapping census for pid {pid}\n")
    print_table(buckets)
    print()
    print(f"process-wide: VmRSS {vm['VmRSS'] / 1024:.1f} MB, VmSwap {vm['VmSwap'] / 1024:.1f} MB")

    # Threshold gate (opt-in): let a soak fail mechanically on a breach.
    breaches = []
    if args.max_rss_mb is not None and vm["VmRSS"] / 1024 > args.max_rss_mb:
        breaches.append(f"VmRSS {vm['VmRSS'] / 1024:.1f} MB > --max-rss-mb {args.max_rss_mb:g}")
    if args.max_swap_mb is not None and vm["VmSwap"] / 1024 > args.max_swap_mb:
        breaches.append(f"VmSwap {vm['VmSwap'] / 1024:.1f} MB > --max-swap-mb {args.max_swap_mb:g}")
    if breaches:
        for b in breaches:
            print(f"THRESHOLD BREACH: {b}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
