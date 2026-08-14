#!/usr/bin/env python3
"""
Runner for the scenario harness (docs/presenter-migration-plan.md).

Runs each tests/scenario_*.py in its own subprocess and interpreter, so one
crash or hang cannot corrupt the next scenario.

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

# Each scenario gets an isolated temp data dir from fixtures.py. The run
# prints a PASS and FAIL table, and exits 1 when a scenario fails.
TESTS_DIR = Path(__file__).resolve().parent

# Scenarios that assert behavior the current code does not have yet. Add an
# entry here with a one-line reason instead of weakening its assertions.
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
        # TimeoutExpired keeps stdout and stderr as bytes even with
        # text=True. The capture happens before the text-mode decode step.
        def _decode(b):
            if b is None:
                return ""
            return b.decode(errors="replace") if isinstance(b, bytes) else b
        output = _decode(e.stdout) + _decode(e.stderr) + f"\n[TIMED OUT after {timeout}s]"
        return False, output, elapsed


def _classify(name: str, ok: bool) -> tuple[str, bool]:
    """Map a scenario result to (status, counts_as_hard_failure) through the
    expected-fail list. One function keeps the serial and parallel paths
    identical."""
    expected_fail_reason = EXPECTED_FAIL_UNTIL_M1.get(name)
    if ok:
        return "PASS", False
    if expected_fail_reason is not None:
        return "XFAIL", False  # an expected failure does not fail the run
    return "FAIL", True


def write_junit(path: Path, results: list) -> None:
    """Write a JUnit XML report with one testsuite and one testcase each.

    A FAIL case carries a failure child and an XFAIL case a skipped child. A
    PASS case attaches the captured stdout and stderr as system-out."""
    total_time = sum(elapsed for _, _, elapsed, _ in results)
    n_fail = sum(1 for _, s, _, _ in results if s == "FAIL")
    n_skip = sum(1 for _, s, _, _ in results if s == "XFAIL")

    suite = ET.Element("testsuite", {
        "name": "deckard-harness",
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

    # Collect (name, status, elapsed, output). The table follows discovery
    # order, not completion order, so serial and parallel runs print the same.
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
            # Submit every scenario, then collect in discovery order, so the
            # table and the verbose output match the serial run.
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
