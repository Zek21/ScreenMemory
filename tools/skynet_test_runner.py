#!/usr/bin/env python3
"""Skynet Test Runner — automated test execution, reporting, and coverage gap analysis.

Discovers test files in tests/, runs them via pytest with timeout protection,
generates structured reports in data/test_report.json, and identifies modules
without test coverage.

Usage:
    python tools/skynet_test_runner.py                  # Run all tests
    python tools/skynet_test_runner.py --verbose         # Verbose output
    python tools/skynet_test_runner.py --json            # JSON output only
    python tools/skynet_test_runner.py --file "brain"    # Filter by pattern
    python tools/skynet_test_runner.py --timeout 10      # Per-test timeout (seconds)
    python tools/skynet_test_runner.py --coverage-only   # Just show coverage gaps
"""
# signed: gamma

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
CORE_DIR = os.path.join(REPO_ROOT, "core")
REPORT_PATH = os.path.join(REPO_ROOT, "data", "test_report.json")

# Files that are known to hang due to subprocess/network calls during collection
KNOWN_HANG_FILES = [
    "test_sprint2_exploratory.py",  # subprocess to skynet_self.py pulse at collection time
]

DEFAULT_TIMEOUT = 15  # seconds per test


def discover_test_files(pattern=None):
    """Find all test_*.py files in tests/."""
    files = []
    if not os.path.isdir(TESTS_DIR):
        return files
    for fname in sorted(os.listdir(TESTS_DIR)):
        if fname.startswith("test_") and fname.endswith(".py"):
            if pattern and pattern.lower() not in fname.lower():
                continue
            files.append(fname)
    return files


def discover_modules(directory, prefix=""):
    """List Python module basenames in a directory (non-recursive top-level)."""
    modules = []
    if not os.path.isdir(directory):
        return modules
    for fname in sorted(os.listdir(directory)):
        if fname.endswith(".py") and not fname.startswith("__"):
            modules.append(fname[:-3])
    return modules


def find_coverage_gaps():
    """Identify modules in tools/ and core/ that lack test files."""
    tools_modules = discover_modules(TOOLS_DIR)
    core_modules = discover_modules(CORE_DIR)
    test_files = discover_test_files()

    # Build set of tested module name fragments from test filenames
    tested_fragments = set()
    for tf in test_files:
        # test_skynet_dispatch_core.py -> skynet_dispatch_core, skynet_dispatch
        base = tf[5:-3]  # strip test_ and .py
        tested_fragments.add(base)
        # Also add without trailing _core, _unit, _integration suffixes
        for suffix in ("_core", "_unit", "_integration", "_acceptance",
                        "_regression", "_exploratory", "_cv3", "_comprehensive"):
            if base.endswith(suffix):
                tested_fragments.add(base[:-len(suffix)])

    gaps = {"tools": [], "core": []}

    for mod in tools_modules:
        # Check if any test file covers this module
        has_test = any(mod in frag or frag in mod for frag in tested_fragments)
        if not has_test:
            gaps["tools"].append(mod)

    for mod in core_modules:
        has_test = any(mod in frag or frag in mod for frag in tested_fragments)
        if not has_test:
            gaps["core"].append(mod)

    return gaps, len(tools_modules), len(core_modules)


def run_tests(test_files, timeout=DEFAULT_TIMEOUT, verbose=False):
    """Run pytest on specified test files with timeout protection.

    Returns a dict with overall results and per-file breakdown.
    """
    if not test_files:
        return {
            "total": 0, "passed": 0, "failed": 0, "errors": 0,
            "skipped": 0, "timed_out": 0, "files": {},
        }

    # Build file paths, excluding known hangers
    run_files = []
    skipped_files = []
    for f in test_files:
        if f in KNOWN_HANG_FILES:
            skipped_files.append(f)
        else:
            run_files.append(os.path.join(TESTS_DIR, f))

    results = {
        "total": 0, "passed": 0, "failed": 0, "errors": 0,
        "skipped": 0, "timed_out": 0, "files": {},
    }

    # Add skipped files to results
    for sf in skipped_files:
        results["files"][sf] = {
            "status": "skipped_known_hang",
            "passed": 0, "failed": 0, "errors": 0,
            "duration_s": 0.0,
            "note": "Excluded: hangs during collection due to subprocess/network calls",
        }

    if not run_files:
        return results

    # Run pytest with JSON-compatible output via --tb=line and parse results
    # Use pytest-timeout for per-test timeout protection
    cmd = [
        sys.executable, "-m", "pytest",
        f"--timeout={timeout}",
        "-q", "--tb=line", "--no-header",
    ]
    if verbose:
        cmd.append("-v")
    cmd.extend(run_files)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=REPO_ROOT,
            timeout=max(timeout * len(run_files), 600),  # generous overall timeout
        )
        output = proc.stdout + "\n" + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        output = "OVERALL TIMEOUT: pytest did not complete within the time limit"
        exit_code = 2
    elapsed = time.perf_counter() - start

    # Parse summary line: "X passed, Y failed, Z errors in Ns"
    summary_match = re.search(
        r"(\d+)\s+passed(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+error)?(?:.*?(\d+)\s+skipped)?",
        output,
    )
    if summary_match:
        results["passed"] = int(summary_match.group(1) or 0)
        results["failed"] = int(summary_match.group(2) or 0)
        results["errors"] = int(summary_match.group(3) or 0)
        results["skipped"] = int(summary_match.group(4) or 0)
    results["total"] = (
        results["passed"] + results["failed"]
        + results["errors"] + results["skipped"]
    )

    # Count timeouts from output
    timeout_count = output.count("Timeout")
    results["timed_out"] = timeout_count

    # Parse per-file results from verbose output or FAILED lines
    failed_lines = re.findall(r"FAILED\s+tests/(\S+)::", output)
    error_lines = re.findall(r"ERROR\s+tests/(\S+)", output)

    # Per-file breakdown from test IDs in output
    file_pass_counts = {}
    file_fail_counts = {}
    file_error_counts = {}

    for line in output.splitlines():
        # Verbose mode: tests/test_foo.py::test_bar PASSED
        m = re.match(r"\s*tests[/\\](\S+\.py)::\S+\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            fname = m.group(1)
            status = m.group(2)
            if status == "PASSED":
                file_pass_counts[fname] = file_pass_counts.get(fname, 0) + 1
            elif status == "FAILED":
                file_fail_counts[fname] = file_fail_counts.get(fname, 0) + 1
            elif status == "ERROR":
                file_error_counts[fname] = file_error_counts.get(fname, 0) + 1

    for fl in failed_lines:
        if fl not in file_fail_counts:
            file_fail_counts[fl] = file_fail_counts.get(fl, 0) + 1

    for el in error_lines:
        if el not in file_error_counts:
            file_error_counts[el] = file_error_counts.get(el, 0) + 1

    # Combine per-file data
    all_mentioned_files = set(file_pass_counts) | set(file_fail_counts) | set(file_error_counts)
    for fname in all_mentioned_files:
        p = file_pass_counts.get(fname, 0)
        f = file_fail_counts.get(fname, 0)
        e = file_error_counts.get(fname, 0)
        status = "passed" if (f == 0 and e == 0) else "failed"
        results["files"][fname] = {
            "status": status, "passed": p, "failed": f, "errors": e,
            "duration_s": 0.0,
        }

    # For non-verbose runs, populate file entries for all run files
    for fpath in run_files:
        fname = os.path.basename(fpath)
        if fname not in results["files"]:
            is_failed = fname in [os.path.basename(f) for f in failed_lines]
            is_errored = fname in [os.path.basename(f) for f in error_lines]
            status = "failed" if (is_failed or is_errored) else "passed"
            results["files"][fname] = {
                "status": status, "passed": 0, "failed": 0, "errors": 0,
                "duration_s": 0.0,
            }

    results["total_duration_s"] = round(elapsed, 2)
    results["exit_code"] = exit_code
    results["raw_output_tail"] = output[-2000:] if len(output) > 2000 else output

    return results


def load_previous_report():
    """Load the previous test report for trend comparison."""
    if os.path.exists(REPORT_PATH):
        try:
            with open(REPORT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def compute_trend(current, previous):
    """Compute trend data comparing current results with previous."""
    if not previous or "results" not in previous:
        return {"previous_run": None, "comparison": "no_previous_data"}

    prev = previous["results"]
    return {
        "previous_run": previous.get("timestamp", "unknown"),
        "total_delta": current["total"] - prev.get("total", 0),
        "passed_delta": current["passed"] - prev.get("passed", 0),
        "failed_delta": current["failed"] - prev.get("failed", 0),
        "pass_rate_prev": (
            round(prev.get("passed", 0) / max(prev.get("total", 1), 1) * 100, 1)
        ),
        "pass_rate_curr": (
            round(current["passed"] / max(current["total"], 1) * 100, 1)
        ),
    }


def generate_report(results, coverage_gaps, tools_count, core_count, trend):
    """Generate and save the test report."""
    pass_rate = round(
        results["passed"] / max(results["total"], 1) * 100, 1
    )
    covered_tools = tools_count - len(coverage_gaps["tools"])
    covered_core = core_count - len(coverage_gaps["core"])

    report = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "results": {
            "total": results["total"],
            "passed": results["passed"],
            "failed": results["failed"],
            "errors": results["errors"],
            "skipped": results["skipped"],
            "timed_out": results["timed_out"],
            "pass_rate_pct": pass_rate,
            "duration_s": results.get("total_duration_s", 0),
            "exit_code": results.get("exit_code", -1),
        },
        "per_file": results["files"],
        "coverage": {
            "tools_modules_total": tools_count,
            "tools_modules_tested": covered_tools,
            "tools_coverage_pct": round(covered_tools / max(tools_count, 1) * 100, 1),
            "core_modules_total": core_count,
            "core_modules_tested": covered_core,
            "core_coverage_pct": round(covered_core / max(core_count, 1) * 100, 1),
            "tools_gaps": coverage_gaps["tools"][:30],  # cap list size
            "core_gaps": coverage_gaps["core"][:20],
        },
        "trend": trend,
    }

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report


def print_summary(report, json_mode=False):
    """Print human-readable or JSON summary."""
    if json_mode:
        print(json.dumps(report, indent=2))
        return

    r = report["results"]
    c = report["coverage"]
    t = report.get("trend", {})

    print("=" * 60)
    print("  SKYNET TEST REPORT")
    print("=" * 60)
    print(f"  Total:     {r['total']}")
    print(f"  Passed:    {r['passed']}")
    print(f"  Failed:    {r['failed']}")
    print(f"  Errors:    {r['errors']}")
    print(f"  Skipped:   {r['skipped']}")
    print(f"  Timed out: {r['timed_out']}")
    print(f"  Pass rate: {r['pass_rate_pct']}%")
    print(f"  Duration:  {r['duration_s']}s")
    print()
    print(f"  Tools coverage: {c['tools_modules_tested']}/{c['tools_modules_total']}"
          f" ({c['tools_coverage_pct']}%)")
    print(f"  Core coverage:  {c['core_modules_tested']}/{c['core_modules_total']}"
          f" ({c['core_coverage_pct']}%)")

    if c["core_gaps"]:
        print(f"\n  Core gaps: {', '.join(c['core_gaps'][:10])}")
    if c["tools_gaps"]:
        print(f"  Tools gaps (top 10): {', '.join(c['tools_gaps'][:10])}")

    if t and t.get("previous_run"):
        sign = lambda x: f"+{x}" if x > 0 else str(x)
        print(f"\n  Trend vs {t['previous_run'][:19]}:")
        print(f"    Tests: {sign(t.get('total_delta', 0))}"
              f"  Passed: {sign(t.get('passed_delta', 0))}"
              f"  Failed: {sign(t.get('failed_delta', 0))}")
        print(f"    Pass rate: {t.get('pass_rate_prev', '?')}%"
              f" -> {t.get('pass_rate_curr', '?')}%")

    # Show failed files
    failed_files = [
        fname for fname, info in report.get("per_file", {}).items()
        if info.get("status") == "failed"
    ]
    if failed_files:
        print(f"\n  FAILED FILES ({len(failed_files)}):")
        for ff in failed_files[:15]:
            print(f"    - {ff}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Skynet Test Runner")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose pytest output")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON only")
    parser.add_argument("--file", type=str, default=None,
                        help="Filter test files by pattern")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Per-test timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--coverage-only", action="store_true",
                        help="Show coverage gaps without running tests")
    args = parser.parse_args()

    # Coverage gaps
    gaps, tools_count, core_count = find_coverage_gaps()

    if args.coverage_only:
        print(f"Tools: {tools_count - len(gaps['tools'])}/{tools_count} covered")
        print(f"Core: {core_count - len(gaps['core'])}/{core_count} covered")
        if gaps["core"]:
            print(f"\nCore gaps: {', '.join(gaps['core'])}")
        if gaps["tools"]:
            print(f"\nTools gaps ({len(gaps['tools'])}):")
            for g in gaps["tools"]:
                print(f"  - {g}")
        return 0

    # Discover and run tests
    test_files = discover_test_files(args.file)
    if not test_files:
        print("No test files found matching criteria.")
        return 1

    if not args.json:
        print(f"Discovered {len(test_files)} test files, running with"
              f" {args.timeout}s timeout...")

    results = run_tests(test_files, timeout=args.timeout, verbose=args.verbose)

    # Trend
    previous = load_previous_report()
    trend = compute_trend(results, previous)

    # Generate report
    report = generate_report(results, gaps, tools_count, core_count, trend)
    print_summary(report, json_mode=args.json)

    if not args.json:
        print(f"\nReport saved to: {REPORT_PATH}")

    return 0 if results["failed"] == 0 and results["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
