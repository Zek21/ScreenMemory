#!/usr/bin/env python3
"""
Skynet Self-Audit & Auto-Fix Module.

Runs diagnostic checks across the entire Skynet orchestration system and
optionally auto-fixes issues found (model drift, dead monitor, stale bus).

Usage:
    python tools/skynet_audit.py          # Full audit, human-readable scorecard
    python tools/skynet_audit.py --fix    # Audit + attempt auto-fix
    python tools/skynet_audit.py --json   # Machine-readable JSON output
"""

import json
import os
import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
METRICS_DIR = DATA_DIR / "metrics"
MONITOR_PID_FILE = DATA_DIR / "monitor.pid"
WORKERS_FILE = DATA_DIR / "workers.json"
SKYNET_URL = "http://localhost:8420"


class CheckResult:
    """Single audit check result."""

    def __init__(self, name, status, message, details=None):
        self.name = name
        self.status = status  # "pass", "fail", "warning"
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


class SkynetAuditor:
    """Runs diagnostic checks across the Skynet system."""

    def __init__(self):
        self.results = []

    def _add(self, name, status, message, details=None):
        r = CheckResult(name, status, message, details)
        self.results.append(r)
        return r

    # ── Check: Skynet Server ─────────────────────────────────────
    def check_server(self):
        """Verify http://localhost:8420/health returns ok."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{SKYNET_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "ok":
                return self._add("server", "pass",
                                 f"Skynet healthy — uptime {data.get('uptime_s', 0):.0f}s, "
                                 f"{data.get('workers_alive', 0)} workers",
                                 data)
            else:
                return self._add("server", "warning",
                                 f"Server responded but status={data.get('status')}", data)
        except Exception as e:
            return self._add("server", "fail", f"Server unreachable: {e}")

    # ── Check: Message Bus ───────────────────────────────────────
    def check_bus(self):
        """Verify bus/messages endpoint works."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{SKYNET_URL}/bus/messages?limit=5", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            msgs = data if isinstance(data, list) else data.get("messages", [])
            return self._add("bus", "pass",
                             f"Bus operational — {len(msgs)} recent messages",
                             {"message_count": len(msgs)})
        except Exception as e:
            return self._add("bus", "fail", f"Bus unreachable: {e}")

    # ── Check: Workers via UIA ───────────────────────────────────
    def check_workers(self):
        """Use UIA engine to scan all 4 workers and verify state/model/agent."""
        try:
            if not WORKERS_FILE.exists():
                return self._add("workers", "fail", "data/workers.json not found")

            workers_data = json.loads(WORKERS_FILE.read_text())
            workers = workers_data.get("workers", [])
            if not workers:
                return self._add("workers", "fail", "No workers defined in workers.json")

            from tools.uia_engine import get_engine
            engine = get_engine()

            scans = {}
            issues = []
            for w in workers:
                name = w.get("name", "unknown")
                hwnd = w.get("hwnd", 0)
                try:
                    result = engine.scan(hwnd)
                    scan_dict = result.to_dict()
                    scans[name] = scan_dict
                    if not result.model_ok:
                        issues.append(f"{name}: wrong model ({result.model})")
                    if not result.agent_ok:
                        issues.append(f"{name}: wrong agent ({result.agent})")
                    if result.error:
                        issues.append(f"{name}: scan error ({result.error})")
                except Exception as e:
                    issues.append(f"{name}: scan failed ({e})")
                    scans[name] = {"error": str(e)}

            if issues:
                return self._add("workers", "warning",
                                 f"{len(issues)} issue(s): {'; '.join(issues[:3])}",
                                 {"scans": scans, "issues": issues})
            return self._add("workers", "pass",
                             f"All {len(workers)} workers scanned OK",
                             {"scans": scans})
        except Exception as e:
            return self._add("workers", "fail", f"Worker check failed: {e}")

    # ── Check: Monitor Process ───────────────────────────────────
    def check_monitor(self):
        """Verify monitor PID file exists and process is alive."""
        if not MONITOR_PID_FILE.exists():
            return self._add("monitor", "fail", "data/monitor.pid not found")

        try:
            pid = int(MONITOR_PID_FILE.read_text().strip())
        except (ValueError, OSError) as e:
            return self._add("monitor", "fail", f"Invalid PID file: {e}")

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
                 f"Select-Object Id,ProcessName,CPU | ConvertTo-Json"],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                proc = json.loads(result.stdout.strip())
                return self._add("monitor", "pass",
                                 f"Monitor alive — PID {pid} ({proc.get('ProcessName', '?')})",
                                 {"pid": pid, "process": proc})
            else:
                return self._add("monitor", "fail",
                                 f"Monitor PID {pid} not running",
                                 {"pid": pid})
        except Exception as e:
            return self._add("monitor", "fail", f"Process check failed: {e}")

    # ── Check: Dispatch (dry-run) ────────────────────────────────
    def check_dispatch(self):
        """Dry-run dispatch test — verify /worker/<name>/tasks endpoint responds."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{SKYNET_URL}/worker/beta/tasks", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            tasks = data if isinstance(data, list) else []
            return self._add("dispatch", "pass",
                             f"Dispatch endpoint OK — {len(tasks)} pending tasks",
                             {"pending_tasks": len(tasks)})
        except Exception as e:
            return self._add("dispatch", "fail", f"Dispatch check failed: {e}")

    # ── Check: Metrics Directory ─────────────────────────────────
    def check_metrics(self):
        """Verify data/metrics dir has files."""
        if not METRICS_DIR.exists():
            return self._add("metrics", "warning", "data/metrics directory missing")

        files = list(METRICS_DIR.glob("*"))
        jsonl_files = list(METRICS_DIR.glob("session_*.jsonl"))
        summary = METRICS_DIR / "research_summary.json"

        details = {
            "total_files": len(files),
            "session_files": len(jsonl_files),
            "summary_exists": summary.exists(),
        }

        if not jsonl_files and not summary.exists():
            return self._add("metrics", "warning",
                             "Metrics dir exists but no data files", details)
        return self._add("metrics", "pass",
                         f"Metrics OK — {len(jsonl_files)} session files, "
                         f"summary={'yes' if summary.exists() else 'no'}",
                         details)

    # ── Full Audit ───────────────────────────────────────────────
    def run_full_audit(self):
        """Run all checks and return scorecard dict."""
        self.results = []

        self.check_server()
        self.check_bus()
        self.check_workers()
        self.check_monitor()
        self.check_dispatch()
        self.check_metrics()

        passes = sum(1 for r in self.results if r.status == "pass")
        warnings = sum(1 for r in self.results if r.status == "warning")
        fails = sum(1 for r in self.results if r.status == "fail")
        total = len(self.results)

        # Score: pass=100%, warning=50%, fail=0%
        score = int((passes * 100 + warnings * 50) / total) if total else 0

        scorecard = {
            "audit_time": datetime.now().isoformat(),
            "health_score": score,
            "summary": {"pass": passes, "warning": warnings, "fail": fails, "total": total},
            "checks": [r.to_dict() for r in self.results],
            "issues": [r.to_dict() for r in self.results if r.status != "pass"],
        }
        return scorecard

    # ── Auto-Fix ─────────────────────────────────────────────────
    def _fix_dead_monitor(self):
        """Restart the monitor daemon. Returns a fix result dict."""
        try:
            monitor_script = ROOT / "tools" / "skynet_monitor.py"
            if monitor_script.exists():
                proc = subprocess.Popen(
                    [sys.executable, str(monitor_script)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" else 0,
                )
                MONITOR_PID_FILE.write_text(str(proc.pid))
                return {"name": "monitor", "action": "restarted",
                        "success": True, "pid": proc.pid}
            return {"name": "monitor", "action": "restart_failed",
                    "success": False, "reason": "script not found"}
        except Exception as e:
            return {"name": "monitor", "action": "restart_failed",
                    "success": False, "reason": str(e)}

    def _fix_model_drift(self, worker_issues):
        """Fix model drift for workers with wrong model. Returns list of fix results."""
        fixes = []
        for wi in worker_issues:
            if "wrong model" not in wi:
                continue
            worker_name = wi.split(":")[0].strip()
            try:
                workers_data = json.loads(WORKERS_FILE.read_text())
                w_entry = next(
                    (w for w in workers_data.get("workers", [])
                     if w["name"] == worker_name), None)
                if w_entry:
                    from tools.skynet_monitor import fix_model_via_uia
                    hwnd = w_entry["hwnd"]
                    render_hwnd = w_entry.get("render_hwnd", hwnd)
                    fixed = fix_model_via_uia(hwnd, render_hwnd)
                    fixes.append({"name": f"model_drift_{worker_name}",
                                  "action": "fix_model", "success": fixed})
                else:
                    fixes.append({"name": f"model_drift_{worker_name}",
                                  "action": "fix_model",
                                  "success": False, "reason": "worker not in workers.json"})
            except Exception as e:
                fixes.append({"name": f"model_drift_{worker_name}",
                              "action": "fix_model",
                              "success": False, "reason": str(e)})
        return fixes

    def _fix_dead_server(self):
        """Restart the Skynet backend server. Returns a fix result dict."""
        try:
            skynet_exe = ROOT / "Skynet" / "skynet.exe"
            if skynet_exe.exists():
                proc = subprocess.Popen(
                    [str(skynet_exe)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    cwd=str(ROOT / "Skynet"),
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32" else 0,
                )
                time.sleep(2)
                return {"name": "server", "action": "restarted",
                        "success": True, "pid": proc.pid}
            return {"name": "server", "action": "restart_failed",
                    "success": False, "reason": "skynet.exe not found"}
        except Exception as e:
            return {"name": "server", "action": "restart_failed",
                    "success": False, "reason": str(e)}

    def auto_fix(self, issues):
        """Attempt to fix found issues. Returns list of fix results."""
        fixes = []
        for issue in issues:
            name = issue.get("name", "")
            if name == "monitor" and issue["status"] == "fail":
                fixes.append(self._fix_dead_monitor())
            if name == "workers" and issue["status"] in ("warning", "fail"):
                worker_issues = issue.get("details", {}).get("issues", [])
                fixes.extend(self._fix_model_drift(worker_issues))
            if name == "server" and issue["status"] == "fail":
                fixes.append(self._fix_dead_server())
        return fixes


def print_scorecard(scorecard):
    """Pretty-print the audit scorecard."""
    score = scorecard["health_score"]
    s = scorecard["summary"]
    color = "\033[92m" if score >= 80 else "\033[93m" if score >= 50 else "\033[91m"
    reset = "\033[0m"

    print(f"\n{'='*60}")
    print(f"  SKYNET SELF-AUDIT — {scorecard['audit_time'][:19]}")
    print(f"{'='*60}")
    print(f"  Health Score: {color}{score}/100{reset}")
    print(f"  Checks: {s['pass']} pass | {s['warning']} warning | {s['fail']} fail")
    print(f"{'='*60}\n")

    icons = {"pass": "\033[92m✓\033[0m", "warning": "\033[93m⚠\033[0m", "fail": "\033[91m✗\033[0m"}
    for c in scorecard["checks"]:
        icon = icons.get(c["status"], "?")
        print(f"  {icon} {c['name']:12s}  {c['message']}")

    if scorecard["issues"]:
        print(f"\n  Issues requiring attention: {len(scorecard['issues'])}")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Self-Audit & Auto-Fix")
    parser.add_argument("--fix", action="store_true", help="Attempt auto-fix for issues")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()

    auditor = SkynetAuditor()
    scorecard = auditor.run_full_audit()

    fix_results = []
    if args.fix and scorecard["issues"]:
        fix_results = auditor.auto_fix(scorecard["issues"])
        scorecard["fixes"] = fix_results
        # Re-audit after fixes
        scorecard_post = auditor.run_full_audit()
        scorecard["post_fix_score"] = scorecard_post["health_score"]
        scorecard["post_fix_checks"] = scorecard_post["checks"]

    if args.json:
        print(json.dumps(scorecard, indent=2))
    else:
        print_scorecard(scorecard)
        if fix_results:
            print("  Auto-fix results:")
            for f in fix_results:
                icon = "\033[92m✓\033[0m" if f["success"] else "\033[91m✗\033[0m"
                print(f"    {icon} {f['name']}: {f['action']}")
            print(f"\n  Post-fix score: {scorecard.get('post_fix_score', '?')}/100\n")
