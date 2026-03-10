#!/usr/bin/env python3
"""
Skynet Health Dashboard — Comprehensive system health checker.

Checks worker window visibility, server health, model correctness via UIA,
and worker scores data. Outputs a JSON health report.

Usage:
    python tools/skynet_health.py          # Full health check, human-readable
    python tools/skynet_health.py --json   # Machine-readable JSON
"""

import json
import sys
import time
import io

# Force UTF-8 stdout for Unicode symbols
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import ctypes
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
SCORES_FILE = DATA_DIR / "worker_scores.json"
SKYNET_URL = "http://localhost:8420"
EXPECTED_MODEL_FRAGMENT = "opus 4.6"
EXPECTED_WORKERS = ["alpha", "beta", "gamma", "delta"]

user32 = ctypes.windll.user32


class SkynetHealthDashboard:
    """Runs health checks across the Skynet system and produces a JSON report."""

    def __init__(self):
        self.checks = []

    def _add(self, name, ok, message, details=None):
        self.checks.append({
            "check": name,
            "status": "pass" if ok else "fail",
            "message": message,
            "details": details or {},
        })
        return ok

    # ── (1) Worker window visibility ─────────────────────────────
    def check_worker_windows(self):
        """Verify all 4 worker windows exist and are visible."""
        if not WORKERS_FILE.exists():
            return self._add("worker_windows", False, "workers.json not found")

        workers = json.loads(WORKERS_FILE.read_text()).get("workers", [])
        results = {}
        all_visible = True

        for w in workers:
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)
            is_window = bool(user32.IsWindow(hwnd))
            is_visible = bool(user32.IsWindowVisible(hwnd))
            results[name] = {"hwnd": hwnd, "exists": is_window, "visible": is_visible}
            if not (is_window and is_visible):
                all_visible = False

        visible_count = sum(1 for r in results.values() if r["visible"])
        return self._add(
            "worker_windows", all_visible,
            f"{visible_count}/{len(workers)} workers visible",
            results,
        )

    # ── (2) Go server health ─────────────────────────────────────
    def check_server(self):
        """Verify Go server on port 8420 responds."""
        try:
            t0 = time.perf_counter()
            req = urllib.request.Request(f"{SKYNET_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            ok = data.get("status") == "ok"
            return self._add(
                "server", ok,
                f"Server {'healthy' if ok else 'unhealthy'} — "
                f"{latency_ms}ms, uptime {data.get('uptime_s', 0):.0f}s, "
                f"{data.get('workers_alive', 0)} workers",
                {"latency_ms": latency_ms, **data},
            )
        except Exception as e:
            return self._add("server", False, f"Server unreachable: {e}")

    # ── (3) Model verification via UIA ───────────────────────────
    def check_worker_models(self):
        """Inspect each worker's model via UIA tree — must be Claude Opus 4.6 fast."""
        if not WORKERS_FILE.exists():
            return self._add("worker_models", False, "workers.json not found")

        workers = json.loads(WORKERS_FILE.read_text()).get("workers", [])
        try:
            from tools.uia_engine import get_engine
            engine = get_engine()
        except Exception as e:
            return self._add("worker_models", False, f"UIA engine failed: {e}")

        results = {}
        all_correct = True
        for w in workers:
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)
            try:
                scan = engine.scan(hwnd)
                model_ok = EXPECTED_MODEL_FRAGMENT in scan.model.lower() if scan.model else False
                results[name] = {
                    "model": scan.model,
                    "agent": scan.agent,
                    "model_ok": model_ok,
                    "agent_ok": scan.agent_ok,
                    "state": scan.state,
                    "scan_ms": round(scan.scan_ms, 2),
                }
                if not model_ok:
                    all_correct = False
            except Exception as e:
                results[name] = {"error": str(e)}
                all_correct = False

        correct_count = sum(1 for r in results.values() if r.get("model_ok"))
        return self._add(
            "worker_models", all_correct,
            f"{correct_count}/{len(workers)} workers on correct model",
            results,
        )

    # ── (4) Worker scores file ───────────────────────────────────
    def check_worker_scores(self):
        """Verify worker_scores.json exists and has data."""
        if not SCORES_FILE.exists():
            return self._add("worker_scores", False, "worker_scores.json not found")

        try:
            data = json.loads(SCORES_FILE.read_text())
            if not data:
                return self._add("worker_scores", False, "worker_scores.json is empty")

            entry_count = len(data) if isinstance(data, (list, dict)) else 0
            return self._add(
                "worker_scores", True,
                f"worker_scores.json has {entry_count} entries",
                {"entries": entry_count, "type": type(data).__name__},
            )
        except json.JSONDecodeError as e:
            return self._add("worker_scores", False, f"Invalid JSON: {e}")

    # ── Full Report ──────────────────────────────────────────────
    def run(self):
        """Execute all health checks and return JSON report."""
        self.checks = []
        self.check_worker_windows()
        self.check_server()
        self.check_worker_models()
        self.check_worker_scores()

        passes = sum(1 for c in self.checks if c["status"] == "pass")
        total = len(self.checks)
        score = int(passes / total * 100) if total else 0

        return {
            "timestamp": datetime.now().isoformat(),
            "health_score": score,
            "passed": passes,
            "failed": total - passes,
            "total_checks": total,
            "checks": self.checks,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Health Dashboard")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    dashboard = SkynetHealthDashboard()
    report = dashboard.run()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        score = report["health_score"]
        color = "\033[92m" if score >= 75 else "\033[93m" if score >= 50 else "\033[91m"
        reset = "\033[0m"
        icons = {"pass": "\033[92m✓\033[0m", "fail": "\033[91m✗\033[0m"}

        print(f"\n{'='*55}")
        print(f"  SKYNET HEALTH DASHBOARD — {report['timestamp'][:19]}")
        print(f"{'='*55}")
        print(f"  Score: {color}{score}/100{reset}  "
              f"({report['passed']} pass / {report['failed']} fail)")
        print(f"{'='*55}\n")

        for c in report["checks"]:
            icon = icons.get(c["status"], "?")
            print(f"  {icon} {c['check']:18s} {c['message']}")

            # Show per-worker detail for model/window checks
            if c["check"] in ("worker_models", "worker_windows") and c["details"]:
                for name, info in sorted(c["details"].items()):
                    if isinstance(info, dict):
                        if "model" in info:
                            m_icon = "\033[92m✓\033[0m" if info.get("model_ok") else "\033[91m✗\033[0m"
                            model_short = info["model"][:50] if info.get("model") else "?"
                            print(f"      {m_icon} {name:8s} {model_short}  [{info.get('state','?')}]")
                        elif "visible" in info:
                            v_icon = "\033[92m✓\033[0m" if info["visible"] else "\033[91m✗\033[0m"
                            print(f"      {v_icon} {name:8s} hwnd={info['hwnd']}")
        print()
