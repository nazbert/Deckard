#!/usr/bin/env python3
"""
Runner for the single-writer migration harness
(docs/presenter-migration-plan.md §4 M0).

Runs every tests/scenario_*.py as an independent subprocess (each gets its
own isolated temp data dir via fixtures.py, and its own interpreter so a
crash/hang in one scenario can't corrupt process-global state -- gl.*,
module-level thread pools -- for the next one), and prints a PASS/FAIL
table. Exits 1 if any non-expected scenario failed.

Usage:
    .venv/bin/python tests/run_all.py [-k SUBSTRING] [--timeout SECONDS]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Scenarios that are known not to pass against today's code because they
# assert *future* (post-milestone) behavior. Empty for M0 -- every scenario
# below is written to hold against the current codebase, not the target
# design. If a later milestone's scenario can't pass yet, add it here with a
# one-line reason instead of weakening its assertions.
EXPECTED_FAIL_UNTIL_M1: dict[str, str] = {
    # "scenario_example.py": "needs the M1 control queue",
}


def discover_scenarios() -> list[Path]:
    return sorted(TESTS_DIR.glob("scenario_*.py"))


def run_one(path: Path, timeout: float) -> tuple[bool, str, float]:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(TESTS_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        ok = proc.returncode == 0
        output = proc.stdout + proc.stderr
        return ok, output, elapsed
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - start
        # TimeoutExpired's stdout/stderr are still bytes even with text=True
        # (they're captured before the text-mode decoding step runs).
        def _decode(b):
            if b is None:
                return ""
            return b.decode(errors="replace") if isinstance(b, bytes) else b
        output = _decode(e.stdout) + _decode(e.stderr) + f"\n[TIMED OUT after {timeout}s]"
        return False, output, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", dest="substring", default=None,
                         help="only run scenarios whose filename contains this substring")
    parser.add_argument("--timeout", type=float, default=90.0,
                         help="per-scenario timeout in seconds (default: 90)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="print each scenario's captured output even on success")
    args = parser.parse_args()

    scenarios = discover_scenarios()
    if args.substring:
        scenarios = [s for s in scenarios if args.substring in s.name]

    if not scenarios:
        print("No scenario_*.py files found/matched.")
        return 1

    results = []  # (name, status, elapsed, output)
    any_hard_failure = False

    for path in scenarios:
        name = path.name
        ok, output, elapsed = run_one(path, args.timeout)
        expected_fail_reason = EXPECTED_FAIL_UNTIL_M1.get(name)

        if ok:
            status = "PASS"
        elif expected_fail_reason is not None:
            status = "XFAIL"  # expected failure -- does not fail the run
        else:
            status = "FAIL"
            any_hard_failure = True

        results.append((name, status, elapsed, output))

        if args.verbose or status == "FAIL":
            print(f"----- {name} output -----")
            print(output.rstrip())
            print(f"----- end {name} -----")

    print()
    print(f"{'SCENARIO':<32} {'STATUS':<8} {'TIME':>8}")
    print("-" * 50)
    for name, status, elapsed, _ in results:
        extra = f"  ({EXPECTED_FAIL_UNTIL_M1[name]})" if status == "XFAIL" else ""
        print(f"{name:<32} {status:<8} {elapsed:>6.2f}s{extra}")

    n_pass = sum(1 for _, s, _, _ in results if s == "PASS")
    n_xfail = sum(1 for _, s, _, _ in results if s == "XFAIL")
    n_fail = sum(1 for _, s, _, _ in results if s == "FAIL")
    print("-" * 50)
    print(f"{n_pass} passed, {n_xfail} expected-fail, {n_fail} failed (of {len(results)})")

    return 1 if any_hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
