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
                                      [--junit PATH] [--jobs N]
"""
import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.dom import minidom

TESTS_DIR = Path(__file__).resolve().parent

# Scenarios that are known not to pass against today's code because they
# assert *future* (post-milestone) behavior. Empty for M0 -- every scenario
# below is written to hold against the current codebase, not the target
# design. If a later milestone's scenario can't pass yet, add it here with a
# one-line reason instead of weakening its assertions.
EXPECTED_FAIL_UNTIL_M1: dict[str, str] = {
    # "scenario_example.py": "needs the M1 control queue",
    "scenario_wipe_restore.py":
        "pins the open wipe-without-restore bug (issue #131): create_n_states "
        "discards the action-owned image and a deduping on_update never "
        "restores it -- remove from this dict when #131 is fixed",
    "scenario_store_b06_pack_survival.py": (
        "B-06 unfixed: install_icon/wallpaper/sd_plus rmtree the installed "
        "pack before the fallible download and never restore it on failure "
        "(gl#62 / transactional-install gl#82). Flips to PASS once fixed."
    ),
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


def _classify(name: str, ok: bool) -> tuple[str, bool]:
    """Maps a scenario's pass/fail into (status, counts_as_hard_failure),
    honoring the expected-fail list. Kept as one function so the serial and
    parallel paths classify identically."""
    expected_fail_reason = EXPECTED_FAIL_UNTIL_M1.get(name)
    if ok:
        return "PASS", False
    if expected_fail_reason is not None:
        return "XFAIL", False  # expected failure -- does not fail the run
    return "FAIL", True


def write_junit(path: Path, results: list) -> None:
    """Emits a JUnit XML report: one <testsuite>, one <testcase> per scenario.
    A FAIL/XFAIL testcase carries a <failure> (FAIL) / <skipped> (XFAIL) child;
    the captured stdout+stderr is attached as <system-out> so a CI viewer shows
    exactly what run_all.py printed. Plain stdlib xml.etree -- no deps."""
    total_time = sum(elapsed for _, _, elapsed, _ in results)
    n_fail = sum(1 for _, s, _, _ in results if s == "FAIL")
    n_skip = sum(1 for _, s, _, _ in results if s == "XFAIL")

    suite = ET.Element("testsuite", {
        "name": "streamcontroller-harness",
        "tests": str(len(results)),
        "failures": str(n_fail),
        "skipped": str(n_skip),
        "errors": "0",
        "time": f"{total_time:.3f}",
    })
    for name, status, elapsed, output in results:
        case = ET.SubElement(suite, "testcase", {
            "classname": "scenarios",
            "name": name,
            "time": f"{elapsed:.3f}",
        })
        if status == "FAIL":
            failure = ET.SubElement(case, "failure", {
                "message": f"{name} failed (non-zero exit)",
                "type": "ScenarioFailure",
            })
            failure.text = output
        elif status == "XFAIL":
            skipped = ET.SubElement(case, "skipped", {
                "message": EXPECTED_FAIL_UNTIL_M1.get(name, "expected failure"),
            })
            skipped.text = output
        else:
            system_out = ET.SubElement(case, "system-out")
            system_out.text = output

    xml_bytes = ET.tostring(suite, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    path.write_bytes(pretty)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", dest="substring", default=None,
                         help="only run scenarios whose filename contains this substring")
    parser.add_argument("--timeout", type=float, default=90.0,
                         help="per-scenario timeout in seconds (default: 90)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="print each scenario's captured output even on success")
    parser.add_argument("--junit", type=Path, default=None,
                         help="also write a JUnit XML report to this path")
    parser.add_argument("--jobs", type=int, default=1,
                         help="run up to N scenarios in parallel (default: 1 = serial). "
                              "Each scenario is an isolated subprocess with its own temp "
                              "data dir, so this is safe; the output table and exit code "
                              "are identical to serial.")
    args = parser.parse_args()

    scenarios = discover_scenarios()
    if args.substring:
        scenarios = [s for s in scenarios if args.substring in s.name]

    if not scenarios:
        print("No scenario_*.py files found/matched.")
        return 1

    # Collect (name, status, elapsed, output). Ordered by discovery (sorted
    # filename) regardless of completion order, so the table is stable and
    # identical between serial and parallel runs.
    results_by_name: dict[str, tuple] = {}

    def _record(path: Path, ok: bool, output: str, elapsed: float) -> None:
        status, _ = _classify(path.name, ok)
        results_by_name[path.name] = (path.name, status, elapsed, output)
        if args.verbose or status == "FAIL":
            print(f"----- {path.name} output -----")
            print(output.rstrip())
            print(f"----- end {path.name} -----")

    jobs = max(1, args.jobs)
    if jobs == 1:
        for path in scenarios:
            ok, output, elapsed = run_one(path, args.timeout)
            _record(path, ok, output, elapsed)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            # Submit all up front (they run concurrently), then collect in
            # discovery order -- not completion order -- so the table and any
            # verbose output stay deterministic and match the serial run.
            future_for = {path: pool.submit(run_one, path, args.timeout) for path in scenarios}
            for path in scenarios:
                ok, output, elapsed = future_for[path].result()
                _record(path, ok, output, elapsed)

    results = [results_by_name[path.name] for path in scenarios]
    any_hard_failure = any(status == "FAIL" for _, status, _, _ in results)

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

    if args.junit is not None:
        write_junit(args.junit, results)
        print(f"JUnit XML written to {args.junit}")

    return 1 if any_hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
