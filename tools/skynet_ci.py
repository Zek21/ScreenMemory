#!/usr/bin/env python3
"""skynet_ci.py -- Continuous integration runner for ScreenMemory/Skynet.

Discovers and runs test suites, security audits, and codebase health scans.
Collects results, generates unified JSON reports, and stores run history
in data/ci/.  Works standalone without Skynet backend.

Usage:
    python tools/skynet_ci.py run                  # run all tests
    python tools/skynet_ci.py run --pattern test_missions  # run matching tests
    python tools/skynet_ci.py run --full            # tests + security audit + health scan
    python tools/skynet_ci.py report               # show latest report
    python tools/skynet_ci.py report --run-id RUN_ID
    python tools/skynet_ci.py status               # quick pass/fail summary
    python tools/skynet_ci.py history [--limit 10]  # recent CI runs
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
CORE_DIR = ROOT / "core"
TOOLS_DIR = ROOT / "tools"
CI_DIR = ROOT / "data" / "ci"
MAX_RUNS = 50
CI_DEPTH_ENV = "SKYNET_CI_DEPTH"


def discover_test_files(pattern: Optional[str] = None) -> list[Path]:
    """Find test files under tests/."""
    if not TESTS_DIR.exists():
        return []
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if pattern:
        files = [f for f in files if pattern in f.stem]
    return files


def _check_recursion_depth(test_files):
    """Check CI recursion depth. Returns early-return dict if nested, else None."""
    depth = 0
    try:
        depth = int(os.environ.get(CI_DEPTH_ENV, "0"))
    except ValueError:
        depth = 0
    if depth > 0:
        return {
            "exit_code": -3,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "total": 1,
            "duration_s": 0.0,
            "output": f"Nested skynet_ci pytest invocation blocked at depth={depth}",
            "files": [f.name for f in test_files],
        }
    return None


def _execute_pytest(file_args, env, timeout):
    """Run pytest subprocess and return (output, exit_code, duration)."""
    cmd = [sys.executable, "-m", "pytest"] + file_args + [
        "-v", "--tb=short", "--no-header", "-q",
    ]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            env=env,
        )
        output = proc.stdout + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        output = f"TIMEOUT after {timeout}s"
        exit_code = -1
    except Exception as e:
        output = f"Error running pytest: {e}"
        exit_code = -2
    duration = round(time.time() - start, 2)
    return output, exit_code, duration


def run_pytest(test_files: list[Path], timeout: int = 300) -> dict:
    """Run pytest on given files and return structured results."""
    if not test_files:
        return {
            "exit_code": 0, "passed": 0, "failed": 0, "errors": 0,
            "skipped": 0, "total": 0, "duration_s": 0.0,
            "output": "No test files to run", "files": [],
        }

    blocked = _check_recursion_depth(test_files)
    if blocked:
        return blocked

    file_args = [str(f) for f in test_files]
    env = os.environ.copy()
    depth = int(env.get(CI_DEPTH_ENV, "0"))
    env[CI_DEPTH_ENV] = str(depth + 1)

    output, exit_code, duration = _execute_pytest(file_args, env, timeout)
    passed, failed, errors, skipped = _parse_summary(output)

    return {
        "exit_code": exit_code,
        "passed": passed, "failed": failed, "errors": errors,
        "skipped": skipped, "total": passed + failed + errors + skipped,
        "duration_s": duration, "output": output,
        "files": [f.name for f in test_files],
    }


def _parse_summary(output: str) -> tuple[int, int, int, int]:
    """Extract pass/fail/error/skip counts from pytest output."""
    import re
    passed = failed = errors = skipped = 0

    # Match "X passed", "X failed", etc. in the summary line
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+)\s+error", output)
    if m:
        errors = int(m.group(1))
    m = re.search(r"(\d+)\s+skipped", output)
    if m:
        skipped = int(m.group(1))

    # Fallback: count PASSED/FAILED lines
    if passed == 0 and failed == 0:
        passed = output.count(" PASSED")
        failed = output.count(" FAILED")

    return passed, failed, errors, skipped


def run_security_audit() -> dict:
    """Run security audit if skynet_security_audit.py exists. Standalone-safe."""
    audit_path = TOOLS_DIR / "skynet_security_audit.py"
    if not audit_path.exists():
        return {"status": "SKIP", "reason": "skynet_security_audit.py not found",
                "passed": 0, "failed": 0, "warnings": 0, "critical": 0}
    try:
        if str(TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(TOOLS_DIR))
        from skynet_security_audit import audit_dispatch_pipeline, audit_config_files
        combined_passed = combined_failed = combined_warnings = combined_critical = 0
        details = []
        components = {}
        for name, fn in [("dispatch", audit_dispatch_pipeline), ("config", audit_config_files)]:
            try:
                result = fn()
                d = result.to_dict()
                components[name] = d
                combined_passed += d["passed"]
                combined_failed += d["failed"]
                combined_warnings += d["warnings"]
                combined_critical += d.get("critical", 0)
                details.extend(d["details"])
            except Exception as e:
                components[name] = {"error": str(e)}
                combined_failed += 1
                details.append({"status": "FAIL", "check": f"{name}_error", "detail": str(e)})
        status = "PASS" if combined_failed == 0 and combined_critical == 0 else "FAIL"
        return {
            "status": status, "passed": combined_passed, "failed": combined_failed,
            "warnings": combined_warnings, "critical": combined_critical,
            "components": components, "details": details,
        }
    except Exception as e:
        return {"status": "FAIL", "error": str(e),
                "passed": 0, "failed": 1, "warnings": 0, "critical": 0}


def run_health_scan() -> dict:
    """Syntax-check all core/ and tools/ modules via py_compile. Standalone-safe."""
    import py_compile
    results = {"core": {}, "tools": {}}
    total_ok = total_fail = 0

    for label, scan_dir in [("core", CORE_DIR), ("tools", TOOLS_DIR)]:
        if not scan_dir.is_dir():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            if py_file.name.startswith("_") and py_file.name != "__init__.py":
                continue
            if "__pycache__" in str(py_file):
                continue
            rel = str(py_file.relative_to(ROOT))
            try:
                py_compile.compile(str(py_file), doraise=True)
                results[label][rel] = "ok"
                total_ok += 1
            except py_compile.PyCompileError as e:
                results[label][rel] = f"FAIL: {str(e)[:120]}"
                total_fail += 1
            except Exception as e:
                results[label][rel] = f"FAIL: {type(e).__name__}: {str(e)[:120]}"
                total_fail += 1

    return {
        "status": "PASS" if total_fail == 0 else "FAIL",
        "modules_ok": total_ok,
        "modules_failed": total_fail,
        "details": results,
    }


def _run_optional_scans(full):
    """Run security audit and health scan if full=True. Returns (security_result, health_result, categories_update)."""
    if not full:
        return None, None, {}

    security_result = run_security_audit()
    health_result = run_health_scan()

    categories = {}
    categories["security"] = {"status": security_result["status"],
                               "passed": security_result.get("passed", 0),
                               "failed": security_result.get("failed", 0)}
    categories["health"] = {"status": health_result["status"],
                             "modules_ok": health_result["modules_ok"],
                             "modules_failed": health_result["modules_failed"]}
    return security_result, health_result, categories


def run_ci(
    pattern: Optional[str] = None,
    timeout: int = 300,
    save: bool = True,
    full: bool = False,
) -> dict:
    """Full CI run: discover tests, execute, generate report, save."""
    run_id = datetime.now(timezone.utc).strftime("ci-%Y%m%d-%H%M%S")
    test_files = discover_test_files(pattern)
    results = run_pytest(test_files, timeout=timeout)

    overall_status = "PASS" if results["exit_code"] == 0 else "FAIL"
    categories = {"tests": {"status": "PASS" if results["exit_code"] == 0 else "FAIL",
                            "passed": results["passed"], "failed": results["failed"],
                            "errors": results["errors"]}}

    security_result, health_result, scan_cats = _run_optional_scans(full)
    categories.update(scan_cats)
    if security_result and security_result["status"] == "FAIL":
        overall_status = "FAIL"
    if health_result and health_result["status"] == "FAIL":
        overall_status = "FAIL"

    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pattern": pattern, "full": full,
        "test_count": len(test_files), "results": results,
        "categories": categories, "status": overall_status,
        "summary": (
            f"{results['passed']} passed, {results['failed']} failed, "
            f"{results['errors']} errors in {results['duration_s']}s"
        ),
    }
    if security_result:
        report["security"] = security_result
    if health_result:
        report["health"] = health_result
    if save:
        _save_run(run_id, report)
    return report


def _save_run(run_id: str, report: dict) -> Path:
    """Persist CI run to data/ci/."""
    CI_DIR.mkdir(parents=True, exist_ok=True)
    path = CI_DIR / f"{run_id}.json"
    # Strip verbose output for storage
    stored = {k: v for k, v in report.items()}
    stored["results"] = {k: v for k, v in report["results"].items() if k != "output"}
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    _rotate_runs()
    return path


def _rotate_runs() -> None:
    """Keep at most MAX_RUNS CI results."""
    files = sorted(CI_DIR.glob("ci-*.json"))
    while len(files) > MAX_RUNS:
        files[0].unlink(missing_ok=True)
        files.pop(0)


def load_run(run_id: str) -> Optional[dict]:
    """Load a specific CI run."""
    path = CI_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def latest_run() -> Optional[dict]:
    """Load the most recent CI run."""
    files = sorted(CI_DIR.glob("ci-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_runs(limit: int = 10) -> list[dict]:
    """List recent CI runs (metadata only)."""
    files = sorted(CI_DIR.glob("ci-*.json"), reverse=True)[:limit]
    runs = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            runs.append({
                "run_id": data.get("run_id", f.stem),
                "timestamp": data.get("timestamp", ""),
                "status": data.get("status", "UNKNOWN"),
                "summary": data.get("summary", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def _report_header_lines(run):
    """Generate the header section of a CI report."""
    r = run.get("results", {})
    return [
        f"{'='*60}",
        f"CI REPORT: {run.get('run_id', '?')}",
        f"{'='*60}",
        f"  Status:    {run.get('status', '?')}",
        f"  Timestamp: {run.get('timestamp', '?')}",
        f"  Pattern:   {run.get('pattern') or 'all'}",
        f"  Full:      {run.get('full', False)}",
        f"  Files:     {run.get('test_count', 0)}",
        f"  Passed:    {r.get('passed', 0)}",
        f"  Failed:    {r.get('failed', 0)}",
        f"  Errors:    {r.get('errors', 0)}",
        f"  Skipped:   {r.get('skipped', 0)}",
        f"  Duration:  {r.get('duration_s', 0)}s",
    ]


def _report_detail_lines(run):
    """Generate the detail section of a CI report (categories, security, health, files, output)."""
    r = run.get("results", {})
    lines = []
    cats = run.get("categories", {})
    if cats:
        lines.append(f"  {'_'*40}")
        lines.append("  Categories:")
        for cat, info in cats.items():
            lines.append(f"    {cat}: {info.get('status', '?')}")

    lines.append(f"{'='*60}")

    sec = run.get("security")
    if sec:
        lines.append(f"\n  Security Audit: {sec.get('status', '?')}  "
                      f"(pass={sec.get('passed',0)} fail={sec.get('failed',0)} "
                      f"warn={sec.get('warnings',0)} crit={sec.get('critical',0)})")

    hlth = run.get("health")
    if hlth:
        lines.append(f"  Health Scan:    {hlth.get('status', '?')}  "
                      f"(ok={hlth.get('modules_ok',0)} fail={hlth.get('modules_failed',0)})")

    if r.get("files"):
        lines.append("  Test files:")
        for f in r["files"]:
            lines.append(f"    - {f}")
    if r.get("output"):
        lines.append("\n--- Output ---")
        lines.append(r["output"][:2000])
    return lines


def generate_report(run: Optional[dict] = None) -> str:
    """Generate a human-readable report from a CI run."""
    if run is None:
        run = latest_run()
    if run is None:
        return "No CI runs found."
    lines = _report_header_lines(run)
    lines.extend(_report_detail_lines(run))
    return "\n".join(lines)


def ci_status() -> dict:
    """Quick status: latest run result + trend."""
    run = latest_run()
    history = list_runs(5)
    streak = 0
    if history:
        for h in history:
            if h["status"] == "PASS":
                streak += 1
            else:
                break
    return {
        "latest": run.get("status", "UNKNOWN") if run else "NO_RUNS",
        "latest_summary": run.get("summary", "") if run else "",
        "pass_streak": streak,
        "recent": history,
    }


def _build_arg_parser():
    """Build and return the argument parser for skynet_ci."""
    parser = argparse.ArgumentParser(description="Skynet CI Runner")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run CI tests")
    p_run.add_argument("--pattern", help="Filter test files by pattern")
    p_run.add_argument("--timeout", type=int, default=300)
    p_run.add_argument("--full", action="store_true",
                       help="Run tests + security audit + health scan")

    p_report = sub.add_parser("report", help="Show CI report")
    p_report.add_argument("--run-id", help="Specific run ID")

    sub.add_parser("status", help="Quick CI status")

    p_hist = sub.add_parser("history", help="List recent runs")
    p_hist.add_argument("--limit", type=int, default=10)
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.command == "run":
        report = run_ci(pattern=args.pattern, timeout=args.timeout, full=args.full)
        print(generate_report(report))
        if args.full:
            print(json.dumps(report.get("categories", {}), indent=2))
        return 0 if report["status"] == "PASS" else 1

    if args.command == "report":
        if hasattr(args, "run_id") and args.run_id:
            run = load_run(args.run_id)
        else:
            run = latest_run()
        print(generate_report(run))
        return 0

    if args.command == "status":
        s = ci_status()
        print(f"Latest: {s['latest']} | Streak: {s['pass_streak']} consecutive passes")
        if s["latest_summary"]:
            print(f"  {s['latest_summary']}")
        return 0

    if args.command == "history":
        runs = list_runs(limit=args.limit)
        if not runs:
            print("No CI runs found.")
            return 0
        for r in runs:
            print(f"  [{r['status']}] {r['run_id']}  {r['summary']}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
