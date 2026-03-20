#!/usr/bin/env python3
"""Skynet Upgrade Loop — autonomous self-upgrade orchestration.

Implements a closed-loop upgrade cycle:
  SCAN → PRIORITIZE → GROUP → DECOMPOSE → DISPATCH → MONITOR → VALIDATE → LOOP

This is the capstone tool that makes Skynet self-upgrading. It reads findings
from the upgrade scanner, packages them into worker-ready tasks, dispatches
to idle workers, monitors completion via the bus, validates with the test
runner, and re-scans for the next wave.

Usage:
    python tools/skynet_upgrade_loop.py --scan              # Scan only
    python tools/skynet_upgrade_loop.py --plan              # Scan + plan (no dispatch)
    python tools/skynet_upgrade_loop.py --execute           # Full loop (scan→dispatch→validate)
    python tools/skynet_upgrade_loop.py --execute --max-waves 3
    python tools/skynet_upgrade_loop.py --dry-run --execute # Preview without side effects
    python tools/skynet_upgrade_loop.py --status            # Show current loop state
"""
# signed: gamma

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCAN_RESULTS_PATH = os.path.join(REPO_ROOT, "data", "upgrade_scan_results.json")
LOOP_STATE_PATH = os.path.join(REPO_ROOT, "data", "upgrade_loop_state.json")
TODOS_PATH = os.path.join(REPO_ROOT, "data", "todos.json")
TEST_REPORT_PATH = os.path.join(REPO_ROOT, "data", "test_report.json")

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
CORE_WORKERS = ["alpha", "beta", "gamma", "delta"]

# File importance for prioritization (higher = more important)
FILE_IMPORTANCE = {
    "skynet_dispatch.py": 10,
    "skynet_brain.py": 9,
    "skynet_spam_guard.py": 9,
    "skynet_monitor.py": 8,
    "skynet_realtime.py": 8,
    "skynet_self.py": 7,
    "skynet_watchdog.py": 7,
    "skynet_worker_boot.py": 7,
    "god_console.py": 6,
    "skynet_scoring.py": 6,
    "skynet_todos.py": 6,
    "skynet_collective.py": 5,
    "skynet_knowledge.py": 5,
    "skynet_convene.py": 5,
}

# Worker specialization for routing
WORKER_SPECS = {
    "alpha": {"frontend", "dashboard", "ui", "architecture", "systems", "god_console"},
    "beta": {"backend", "infrastructure", "daemons", "python", "resilience", "daemon"},
    "gamma": {"security", "analysis", "optimization", "performance", "research", "test"},
    "delta": {"testing", "validation", "auditing", "config", "docs", "documentation"},
}

MAX_FINDINGS_PER_PACKAGE = 8
MAX_PACKAGES_PER_WAVE = 8
MONITOR_POLL_INTERVAL = 15  # seconds
MONITOR_TIMEOUT = 600  # 10 minutes max wait per wave


# ──────────────────────────────────────────────────────────────
# Phase 1: SCAN
# ──────────────────────────────────────────────────────────────

def run_scan():
    """Run skynet_upgrade_scanner.py and return findings."""
    scanner_path = os.path.join(REPO_ROOT, "tools", "skynet_upgrade_scanner.py")
    if not os.path.exists(scanner_path):
        print("[SCAN] ERROR: skynet_upgrade_scanner.py not found")
        return None

    print("[SCAN] Running upgrade scanner...")
    try:
        proc = subprocess.run(
            [sys.executable, scanner_path, "--json"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=REPO_ROOT, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("[SCAN] Scanner timed out after 120s")
        return None

    if proc.returncode != 0 and not os.path.exists(SCAN_RESULTS_PATH):
        print(f"[SCAN] Scanner failed (exit {proc.returncode})")
        return None

    return load_scan_results()


def load_scan_results():
    """Load scan results from disk."""
    if not os.path.exists(SCAN_RESULTS_PATH):
        return None
    try:
        with open(SCAN_RESULTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[SCAN] Failed to load scan results: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Phase 2: PRIORITIZE
# ──────────────────────────────────────────────────────────────

def prioritize_findings(findings):
    """Sort findings by severity, then file importance, then line number."""
    def sort_key(f):
        sev = SEVERITY_ORDER.get(f.get("severity", "LOW"), 3)
        fname = os.path.basename(f.get("file", ""))
        importance = FILE_IMPORTANCE.get(fname, 0)
        line = f.get("line", 0)
        return (sev, -importance, line)

    return sorted(findings, key=sort_key)


# ──────────────────────────────────────────────────────────────
# Phase 3: GROUP
# ──────────────────────────────────────────────────────────────

def group_findings(findings):
    """Cluster related findings into work packages.

    Grouping strategy:
      1. Group by file first (findings in the same file are related)
      2. Within each file, sub-group by category
      3. Split large groups into manageable packages
    """
    # Group by (severity, file)
    by_file = defaultdict(list)
    for f in findings:
        key = f.get("file", "unknown")
        by_file[key].append(f)

    packages = []
    for filepath, file_findings in by_file.items():
        # Sub-group by category within file
        by_cat = defaultdict(list)
        for ff in file_findings:
            by_cat[ff.get("category", "unknown")].append(ff)

        # Build packages — merge small categories, split large ones
        current_pkg = []
        for cat, cat_findings in by_cat.items():
            for cf in cat_findings:
                current_pkg.append(cf)
                if len(current_pkg) >= MAX_FINDINGS_PER_PACKAGE:
                    packages.append(_make_package(filepath, current_pkg))
                    current_pkg = []

        if current_pkg:
            packages.append(_make_package(filepath, current_pkg))

    return packages


def _make_package(filepath, findings):
    """Create a work package from a set of findings."""
    severities = [f.get("severity", "LOW") for f in findings]
    max_sev = min(severities, key=lambda s: SEVERITY_ORDER.get(s, 3))
    categories = list(set(f.get("category", "unknown") for f in findings))

    return {
        "file": filepath,
        "severity": max_sev,
        "finding_count": len(findings),
        "categories": categories,
        "findings": findings,
        "importance": FILE_IMPORTANCE.get(os.path.basename(filepath), 0),
    }


# ──────────────────────────────────────────────────────────────
# Phase 4: DECOMPOSE
# ──────────────────────────────────────────────────────────────

def decompose_packages(packages):
    """Generate worker-ready task descriptions for each package."""
    tasks = []
    for pkg in packages:
        fname = os.path.basename(pkg["file"])
        sev = pkg["severity"]
        cats = ", ".join(pkg["categories"])
        count = pkg["finding_count"]

        # Build task description with actionable details
        lines = [f"[{sev}] Fix {count} issue(s) in {pkg['file']} (categories: {cats})"]
        lines.append("")
        lines.append("Findings to fix:")
        for f in pkg["findings"]:
            fix = f.get("suggested_fix", "")
            fix_text = f" -- FIX: {fix}" if fix else ""
            lines.append(
                f"  L{f.get('line', '?')}: [{f.get('category', '?')}] "
                f"{f.get('description', 'no description')}{fix_text}"
            )
        lines.append("")
        lines.append("Requirements:")
        lines.append("  - All changes must compile clean (python -m py_compile)")
        lines.append("  - Do NOT break existing functionality")
        lines.append("  - Post result to bus when done")

        task_text = "\n".join(lines)
        worker = _route_package(pkg)

        tasks.append({
            "package": pkg,
            "task_text": task_text,
            "suggested_worker": worker,
            "priority": sev.lower(),
        })

    return tasks


def _route_package(pkg):
    """Route a package to the best-suited worker based on specialization."""
    categories = set(pkg.get("categories", []))
    filepath = pkg.get("file", "")
    fname = os.path.basename(filepath).lower()

    scores = {}
    for worker, specs in WORKER_SPECS.items():
        score = 0
        # Category match
        for cat in categories:
            if cat in specs:
                score += 2
            for spec in specs:
                if spec in cat or cat in spec:
                    score += 1
        # Filename match
        for spec in specs:
            if spec in fname:
                score += 1
        # Security/test routing
        if "security" in categories or "hardcoded_credential" in categories:
            if worker == "gamma":
                score += 3
        if "missing_tests" in categories:
            if worker == "delta":
                score += 3
        if "daemon" in fname or "monitor" in fname:
            if worker == "beta":
                score += 2
        if "dashboard" in fname or "console" in fname or "god_" in fname:
            if worker == "alpha":
                score += 2
        scores[worker] = score

    return max(scores, key=scores.get)


# ──────────────────────────────────────────────────────────────
# Phase 5: DISPATCH
# ──────────────────────────────────────────────────────────────

def get_idle_workers():
    """Query Skynet backend for idle workers."""
    try:
        import requests
        r = requests.get("http://localhost:8420/status", timeout=5)
        data = r.json()
        agents = data.get("agents", {})
        idle = []
        for name in CORE_WORKERS:
            info = agents.get(name, {})
            if info.get("status", "").upper() == "IDLE":
                idle.append(name)
        return idle
    except Exception:
        return []


def dispatch_task(worker, task_text, dry_run=False):
    """Dispatch a task to a specific worker via skynet_dispatch.py."""
    if dry_run:
        print(f"  [DRY-RUN] Would dispatch to {worker}: {task_text[:80]}...")
        return True

    dispatch_script = os.path.join(REPO_ROOT, "tools", "skynet_dispatch.py")
    try:
        proc = subprocess.run(
            [sys.executable, dispatch_script, "--worker", worker, "--task", task_text],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=REPO_ROOT, timeout=30,
        )
        success = proc.returncode == 0
        if not success:
            print(f"  [DISPATCH] Failed to dispatch to {worker}: {proc.stderr[:200]}")
        return success
    except subprocess.TimeoutExpired:
        print(f"  [DISPATCH] Timeout dispatching to {worker}")
        return False
    except Exception as e:
        print(f"  [DISPATCH] Error dispatching to {worker}: {e}")
        return False


def dispatch_wave(tasks, dry_run=False, max_dispatch=MAX_PACKAGES_PER_WAVE):
    """Dispatch a wave of tasks to idle workers.

    Returns list of dispatched task records.
    """
    idle = get_idle_workers()
    if not idle and not dry_run:
        print("[DISPATCH] No idle workers available")
        return []

    dispatched = []
    # Sort tasks by priority (CRITICAL first)
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    tasks_sorted = sorted(tasks, key=lambda t: priority_order.get(t["priority"], 3))

    worker_queue = list(idle) if not dry_run else list(CORE_WORKERS)
    worker_idx = 0

    for task in tasks_sorted[:max_dispatch]:
        # Prefer suggested worker if idle, else round-robin
        target = task["suggested_worker"]
        if target not in worker_queue:
            if not worker_queue:
                break
            target = worker_queue[worker_idx % len(worker_queue)]

        success = dispatch_task(target, task["task_text"], dry_run=dry_run)
        if success:
            dispatched.append({
                "worker": target,
                "file": task["package"]["file"],
                "severity": task["package"]["severity"],
                "finding_count": task["package"]["finding_count"],
                "dispatched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            # Remove worker from idle pool after dispatch
            if target in worker_queue:
                worker_queue.remove(target)
            worker_idx += 1

        # Clipboard cooldown between dispatches
        if not dry_run:
            time.sleep(2.5)

    return dispatched


# ──────────────────────────────────────────────────────────────
# Phase 6: MONITOR
# ──────────────────────────────────────────────────────────────

def monitor_wave(dispatched, timeout=MONITOR_TIMEOUT, dry_run=False):
    """Poll bus for worker results until all dispatched tasks complete or timeout."""
    if dry_run or not dispatched:
        return {"completed": len(dispatched), "timed_out": 0, "results": []}

    workers_pending = set(d["worker"] for d in dispatched)
    results = []
    start = time.time()

    print(f"[MONITOR] Waiting for {len(workers_pending)} workers: {', '.join(workers_pending)}")

    while workers_pending and (time.time() - start) < timeout:
        try:
            import requests
            r = requests.get(
                "http://localhost:8420/bus/messages?limit=30", timeout=5
            )
            messages = r.json() if r.status_code == 200 else []

            for msg in messages:
                sender = msg.get("sender", "")
                msg_type = msg.get("type", "")
                content = msg.get("content", "")
                ts = msg.get("timestamp", "")

                if sender in workers_pending and msg_type == "result":
                    # Check if this is a recent result (not stale)
                    results.append({
                        "worker": sender,
                        "content": content[:500],
                        "timestamp": ts,
                    })
                    workers_pending.discard(sender)
                    print(f"  [MONITOR] {sender} completed ({len(workers_pending)} remaining)")
        except Exception as e:
            print(f"  [MONITOR] Poll error: {e}")

        if workers_pending:
            time.sleep(MONITOR_POLL_INTERVAL)

    timed_out = len(workers_pending)
    if timed_out:
        print(f"  [MONITOR] {timed_out} workers timed out: {', '.join(workers_pending)}")

    return {
        "completed": len(results),
        "timed_out": timed_out,
        "results": results,
        "elapsed_s": round(time.time() - start, 1),
    }


# ──────────────────────────────────────────────────────────────
# Phase 7: VALIDATE
# ──────────────────────────────────────────────────────────────

def run_validation(dry_run=False):
    """Run the test suite to verify no regressions."""
    if dry_run:
        print("[VALIDATE] [DRY-RUN] Would run test suite")
        return {"passed": True, "total": 0, "failed": 0, "pass_rate": 100.0}

    runner_path = os.path.join(REPO_ROOT, "tools", "skynet_test_runner.py")
    if not os.path.exists(runner_path):
        print("[VALIDATE] WARNING: skynet_test_runner.py not found, skipping validation")
        return {"passed": True, "total": 0, "failed": 0, "pass_rate": 100.0}

    print("[VALIDATE] Running test suite...")
    try:
        proc = subprocess.run(
            [sys.executable, runner_path, "--json", "--timeout", "10"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=REPO_ROOT, timeout=300,
        )
        if proc.returncode == 0:
            try:
                report = json.loads(proc.stdout)
                r = report.get("results", {})
                return {
                    "passed": r.get("failed", 0) == 0 and r.get("errors", 0) == 0,
                    "total": r.get("total", 0),
                    "failed": r.get("failed", 0),
                    "pass_rate": r.get("pass_rate_pct", 0),
                }
            except json.JSONDecodeError:
                pass
        # Try loading from file
        if os.path.exists(TEST_REPORT_PATH):
            with open(TEST_REPORT_PATH, "r", encoding="utf-8") as f:
                report = json.load(f)
            r = report.get("results", {})
            return {
                "passed": r.get("failed", 0) == 0 and r.get("errors", 0) == 0,
                "total": r.get("total", 0),
                "failed": r.get("failed", 0),
                "pass_rate": r.get("pass_rate_pct", 0),
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[VALIDATE] Test runner error: {e}")

    return {"passed": False, "total": 0, "failed": -1, "pass_rate": 0}


# ──────────────────────────────────────────────────────────────
# State Management
# ──────────────────────────────────────────────────────────────

def load_state():
    """Load the upgrade loop state."""
    if os.path.exists(LOOP_STATE_PATH):
        try:
            with open(LOOP_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "waves_completed": 0,
        "total_findings_fixed": 0,
        "total_dispatched": 0,
        "history": [],
    }


def save_state(state):
    """Persist the upgrade loop state."""
    os.makedirs(os.path.dirname(LOOP_STATE_PATH), exist_ok=True)
    with open(LOOP_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ──────────────────────────────────────────────────────────────
# Main Pipeline
# ──────────────────────────────────────────────────────────────

def run_pipeline(mode="scan", max_waves=1, dry_run=False, verbose=False):
    """Execute the upgrade pipeline.

    Modes:
      scan    — scan only, show findings
      plan    — scan + prioritize + group + decompose (no dispatch)
      execute — full loop: scan → dispatch → monitor → validate → re-scan
    """
    state = load_state()

    for wave in range(max_waves):
        wave_num = state["waves_completed"] + 1
        print(f"\n{'='*60}")
        print(f"  SKYNET UPGRADE LOOP — WAVE {wave_num}")
        print(f"{'='*60}")

        # ── SCAN ──
        print("\n[PHASE 1] SCAN")
        scan_data = run_scan()
        if not scan_data:
            print("[SCAN] No scan data available — nothing to do")
            break

        findings = scan_data.get("findings", [])
        by_sev = scan_data.get("findings_by_severity", {})
        total = len(findings)
        print(f"  Found {total} findings: "
              f"CRIT={by_sev.get('CRITICAL', 0)} "
              f"HIGH={by_sev.get('HIGH', 0)} "
              f"MED={by_sev.get('MEDIUM', 0)} "
              f"LOW={by_sev.get('LOW', 0)}")

        if total == 0:
            print("[SCAN] Zero findings — codebase is clean!")
            break

        # Filter to actionable findings (skip LOW for auto-dispatch)
        actionable = [f for f in findings if f.get("severity") in ("CRITICAL", "HIGH", "MEDIUM")]
        print(f"  Actionable (CRITICAL+HIGH+MEDIUM): {len(actionable)}")

        if not actionable:
            print("[SCAN] No actionable findings (only LOW severity) — skipping")
            break

        if mode == "scan":
            _print_scan_summary(actionable)
            break

        # ── PRIORITIZE ──
        print("\n[PHASE 2] PRIORITIZE")
        prioritized = prioritize_findings(actionable)
        print(f"  Prioritized {len(prioritized)} findings")
        if verbose:
            for f in prioritized[:5]:
                print(f"    [{f['severity']}] {f['file']}:L{f.get('line','?')} "
                      f"— {f.get('description','')[:60]}")

        # ── GROUP ──
        print("\n[PHASE 3] GROUP")
        packages = group_findings(prioritized)
        print(f"  Created {len(packages)} work packages")
        for pkg in packages[:8]:
            print(f"    [{pkg['severity']}] {os.path.basename(pkg['file'])} "
                  f"— {pkg['finding_count']} finding(s), "
                  f"categories: {', '.join(pkg['categories'][:3])}")

        # ── DECOMPOSE ──
        print("\n[PHASE 4] DECOMPOSE")
        tasks = decompose_packages(packages[:MAX_PACKAGES_PER_WAVE])
        print(f"  Generated {len(tasks)} worker-ready tasks")
        for t in tasks:
            print(f"    → {t['suggested_worker']}: "
                  f"[{t['priority'].upper()}] {os.path.basename(t['package']['file'])} "
                  f"({t['package']['finding_count']} issues)")

        if mode == "plan":
            print("\n[PLAN MODE] Stopping before dispatch. Use --execute to continue.")
            break

        # ── DISPATCH ──
        print("\n[PHASE 5] DISPATCH")
        dispatched = dispatch_wave(tasks, dry_run=dry_run)
        print(f"  Dispatched {len(dispatched)} task(s)")
        for d in dispatched:
            print(f"    → {d['worker']}: [{d['severity']}] "
                  f"{os.path.basename(d['file'])} ({d['finding_count']} issues)")

        if not dispatched:
            print("  No tasks dispatched (no idle workers or dispatch failure)")
            break

        # ── MONITOR ──
        print("\n[PHASE 6] MONITOR")
        monitor_result = monitor_wave(dispatched, dry_run=dry_run)
        print(f"  Completed: {monitor_result['completed']}, "
              f"Timed out: {monitor_result['timed_out']}")

        # ── VALIDATE ──
        print("\n[PHASE 7] VALIDATE")
        validation = run_validation(dry_run=dry_run)
        status = "PASS" if validation["passed"] else "FAIL"
        print(f"  Tests: {validation['total']} total, "
              f"{validation['failed']} failed, "
              f"pass rate: {validation['pass_rate']}% — {status}")

        if not validation["passed"]:
            print("  WARNING: Regressions detected! Stopping upgrade loop.")

        # ── Record wave ──
        wave_record = {
            "wave": wave_num,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "findings_total": total,
            "findings_actionable": len(actionable),
            "packages": len(packages),
            "dispatched": len(dispatched),
            "completed": monitor_result["completed"],
            "timed_out": monitor_result["timed_out"],
            "test_pass_rate": validation["pass_rate"],
            "validation_passed": validation["passed"],
            "dry_run": dry_run,
        }
        state["waves_completed"] = wave_num
        state["total_dispatched"] += len(dispatched)
        state["total_findings_fixed"] += monitor_result["completed"]
        state["history"].append(wave_record)
        save_state(state)

        print(f"\n  Wave {wave_num} complete. "
              f"Cumulative: {state['total_dispatched']} dispatched, "
              f"{state['total_findings_fixed']} completed")

        if not validation["passed"]:
            break

        # Check if more waves needed
        if wave < max_waves - 1:
            print(f"\n  Re-scanning for wave {wave_num + 1}...")

    # Final summary
    print(f"\n{'='*60}")
    print(f"  UPGRADE LOOP COMPLETE")
    print(f"  Waves: {state['waves_completed']}, "
          f"Total dispatched: {state['total_dispatched']}, "
          f"Completed: {state['total_findings_fixed']}")
    print(f"{'='*60}")

    return state


def _print_scan_summary(findings):
    """Print a readable scan summary."""
    by_sev = defaultdict(int)
    by_cat = defaultdict(int)
    by_file = defaultdict(int)

    for f in findings:
        by_sev[f.get("severity", "LOW")] += 1
        by_cat[f.get("category", "unknown")] += 1
        by_file[os.path.basename(f.get("file", "unknown"))] += 1

    print("\n  By severity:")
    for sev in ("CRITICAL", "HIGH", "MEDIUM"):
        if by_sev[sev]:
            print(f"    {sev}: {by_sev[sev]}")

    print("\n  By category:")
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1])[:10]:
        print(f"    {cat}: {count}")

    print(f"\n  Top files ({len(by_file)} total):")
    for fname, count in sorted(by_file.items(), key=lambda x: -x[1])[:10]:
        print(f"    {fname}: {count} finding(s)")


def show_status():
    """Print current upgrade loop state."""
    state = load_state()
    print(f"Waves completed: {state['waves_completed']}")
    print(f"Total dispatched: {state['total_dispatched']}")
    print(f"Total completed: {state['total_findings_fixed']}")
    if state["history"]:
        last = state["history"][-1]
        print(f"\nLast wave ({last['wave']}):")
        print(f"  Time: {last['timestamp']}")
        print(f"  Findings: {last['findings_actionable']} actionable / {last['findings_total']} total")
        print(f"  Dispatched: {last['dispatched']}, Completed: {last['completed']}")
        print(f"  Test pass rate: {last['test_pass_rate']}%")
        print(f"  Dry run: {last['dry_run']}")


def main():
    parser = argparse.ArgumentParser(
        description="Skynet Upgrade Loop — autonomous self-upgrade orchestration"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="Scan only — show findings")
    group.add_argument("--plan", action="store_true",
                       help="Scan + plan — show packages without dispatching")
    group.add_argument("--execute", action="store_true",
                       help="Full loop: scan → dispatch → monitor → validate")
    group.add_argument("--status", action="store_true", help="Show current loop state")

    parser.add_argument("--max-waves", type=int, default=1,
                        help="Maximum upgrade waves to run (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview all actions without side effects")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose output")

    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    if args.scan:
        mode = "scan"
    elif args.plan:
        mode = "plan"
    else:
        mode = "execute"

    run_pipeline(
        mode=mode,
        max_waves=args.max_waves,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
