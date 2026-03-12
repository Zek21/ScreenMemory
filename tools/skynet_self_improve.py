"""
skynet_self_improve.py -- Self-Improvement Engine for Skynet Level 4.

Continuously scans for issues, proposes fixes, auto-applies LOW-risk fixes,
and tracks improvement metrics. Runs as a background daemon alongside
the self-prompt daemon.

Usage:
    python tools/skynet_self_improve.py start       # Daemon mode
    python tools/skynet_self_improve.py once         # Single scan cycle
    python tools/skynet_self_improve.py status       # Show metrics
    python tools/skynet_self_improve.py proposals    # List pending proposals
"""

import json
import os
import signal
import sys
import time
import hashlib
import importlib
from pathlib import Path
from datetime import datetime
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "self_improve.pid"
METRICS_FILE = DATA_DIR / "improvement_metrics.json"
IMPROVEMENTS_DIR = DATA_DIR / "improvements"
DISPATCH_LOG = DATA_DIR / "dispatch_log.json"
TODOS_FILE = DATA_DIR / "todos.json"
IQ_HISTORY_FILE = DATA_DIR / "iq_history.json"
BUS_URL = "http://localhost:8420"
GOD_URL = "http://localhost:8421"

# Load SCAN_INTERVAL from brain_config.json, enforce 120s minimum
_DEFAULT_SCAN_INTERVAL = 120
try:
    _brain_cfg = json.loads((DATA_DIR / "brain_config.json").read_text(encoding="utf-8"))
    SCAN_INTERVAL = max(_brain_cfg.get("self_improve_interval", _DEFAULT_SCAN_INTERVAL), 120)
except Exception:
    SCAN_INTERVAL = _DEFAULT_SCAN_INTERVAL

MAX_PROPOSALS = 50  # max stored proposals
_fetch_failures = 0  # backoff counter for HTTP fetch failures  # signed: delta


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] self_improve: {msg}", flush=True)


def _fetch_json(url, timeout=5):
    global _fetch_failures
    try:
        from urllib.request import urlopen, Request
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        _fetch_failures = 0  # reset on success  # signed: delta
        return data
    except (ConnectionError, TimeoutError, OSError) as e:
        _fetch_failures += 1
        if _fetch_failures <= 3 or _fetch_failures % 10 == 0:
            log(f"Fetch network error (attempt {_fetch_failures}): {url}", "WARN")  # signed: gamma
        return None
    except (json.JSONDecodeError, ValueError) as e:
        _fetch_failures += 1
        log(f"Fetch parse error (attempt {_fetch_failures}): {url}", "WARN")  # signed: gamma
        return None


def _post_bus(topic, msg_type, content):
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "self-improve",
            "topic": topic,
            "type": msg_type,
            "content": content,
        })
    except Exception:
        pass  # signed: alpha


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ── Issue Detection ──────────────────────────────────────────────────────────

class IssueDetector:
    """Scans multiple data sources for systemic issues."""

    def __init__(self):
        self.seen_issues = set()  # dedup by hash

    def scan_all(self):
        """Run all detection scans. Returns list of issue dicts."""
        issues = []
        issues.extend(self._scan_dispatch_failures())
        issues.extend(self._scan_repeated_alerts())
        issues.extend(self._scan_learning_health())
        issues.extend(self._scan_iq_trend())
        issues.extend(self._scan_engine_health())
        issues.extend(self._scan_stale_todos())

        # Dedup
        unique = []
        for issue in issues:
            h = hashlib.md5(issue["slug"].encode()).hexdigest()[:12]
            if h not in self.seen_issues:
                self.seen_issues.add(h)
                unique.append(issue)
        return unique

    def _scan_dispatch_failures(self):
        """Find patterns in dispatch failures."""
        issues = []
        data = _load_json(DISPATCH_LOG)
        if not data or not isinstance(data, list):
            return issues

        recent = [e for e in data if e.get("timestamp", "") > (datetime.now().strftime("%Y-%m-%dT%H:%M:%S")[:10])]
        if not recent:
            recent = data[-50:]

        # Count failures per worker
        failures_by_worker = Counter()
        failure_details = {}
        for entry in recent:
            if entry.get("success") is False:
                worker = entry.get("worker", "unknown")
                failures_by_worker[worker] += 1
                failure_details.setdefault(worker, []).append(
                    str(entry.get("task_summary", ""))[:80]
                )

        for worker, count in failures_by_worker.items():
            if count >= 3:
                issues.append({
                    "slug": f"dispatch_failure_pattern_{worker}",
                    "category": "dispatch",
                    "severity": "MEDIUM",
                    "title": f"Repeated dispatch failures to {worker.upper()}",
                    "evidence": f"{count} failures in recent dispatches: {'; '.join(failure_details[worker][:3])}",
                    "proposed_fix": f"Check {worker.upper()} window health via UIA. Worker may need HWND refresh in workers.json.",
                    "risk": "LOW",
                    "auto_fixable": False,
                })

        # Check for undelivered tasks (no result_received)
        undelivered = [e for e in recent if not e.get("result_received") and e.get("success")]
        if len(undelivered) > 5:
            issues.append({
                "slug": "high_undelivered_rate",
                "category": "dispatch",
                "severity": "MEDIUM",
                "title": f"{len(undelivered)} dispatched tasks without result",
                "evidence": f"Workers: {Counter(e.get('worker','?') for e in undelivered).most_common(3)}",
                "proposed_fix": "Check if workers are processing tasks or if dispatch is failing silently. Review worker output panels.",
                "risk": "LOW",
                "auto_fixable": False,
            })

        return issues

    def _scan_repeated_alerts(self):
        """Detect systemic issues from repeated bus alerts."""
        issues = []
        msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=50")
        if not msgs or not isinstance(msgs, list):
            return issues

        alert_contents = Counter()
        for m in msgs:
            if m.get("type") in ("alert", "monitor_alert", "service_alert", "delivery_alert"):
                # Normalize: strip numbers, timestamps for grouping
                content = str(m.get("content", ""))
                normalized = "".join(c for c in content if not c.isdigit())[:60]
                alert_contents[normalized] += 1

        for pattern, count in alert_contents.most_common(5):
            if count >= 3:
                issues.append({
                    "slug": f"repeated_alert_{hashlib.md5(pattern.encode()).hexdigest()[:8]}",
                    "category": "monitoring",
                    "severity": "MEDIUM" if count < 10 else "HIGH",
                    "title": f"Alert repeated {count}x: {pattern[:50]}",
                    "evidence": f"Seen {count} times in last 50 bus messages",
                    "proposed_fix": "Investigate root cause and fix the condition generating the alert, or increase dedup window.",
                    "risk": "LOW",
                    "auto_fixable": False,
                })

        return issues

    def _scan_learning_health(self):
        """Check if the learning store is growing or stagnant."""
        issues = []
        try:
            from core.learning_store import LearningStore
            ls = LearningStore()
            stats = ls.stats()
            total = stats.get("total_facts", 0)
            avg_conf = stats.get("average_confidence", 0)

            if total == 0:
                issues.append({
                    "slug": "learning_store_empty",
                    "category": "intelligence",
                    "severity": "HIGH",
                    "title": "LearningStore has 0 facts -- learning loop inactive",
                    "evidence": "LearningStore.stats() returned total_facts=0",
                    "proposed_fix": "Workers should call skynet_knowledge.broadcast_learning() after completing tasks. Check if learning pipeline is wired.",
                    "risk": "LOW",
                    "auto_fixable": False,
                })
            elif avg_conf < 0.3:
                issues.append({
                    "slug": "learning_low_confidence",
                    "category": "intelligence",
                    "severity": "MEDIUM",
                    "title": f"LearningStore average confidence very low ({avg_conf:.2f})",
                    "evidence": f"{total} facts with avg confidence {avg_conf:.2f}",
                    "proposed_fix": "Many facts have low confidence. Run learning consolidation or prune unreliable facts.",
                    "risk": "LOW",
                    "auto_fixable": False,
                })
        except Exception:
            pass
        return issues

    def _scan_iq_trend(self):
        """Check if collective IQ is improving or declining."""
        issues = []
        iq_data = _load_json(IQ_HISTORY_FILE)
        if not iq_data:
            return issues

        history = iq_data.get("history", []) if isinstance(iq_data, dict) else iq_data if isinstance(iq_data, list) else []
        if len(history) < 3:
            return issues

        recent = [h.get("score", 0) for h in history[-5:]]
        if all(s == recent[0] for s in recent):
            issues.append({
                "slug": "iq_stagnant",
                "category": "intelligence",
                "severity": "LOW",
                "title": f"IQ score stagnant at {recent[-1]:.2f} for {len(recent)} readings",
                "evidence": f"Last {len(recent)} IQ scores: {[round(s,2) for s in recent]}",
                "proposed_fix": "IQ may not be recomputing. Check if skynet_self.py compute_iq() is being called regularly.",
                "risk": "LOW",
                "auto_fixable": False,
            })

        if len(recent) >= 3 and recent[-1] < recent[0] - 0.1:
            issues.append({
                "slug": "iq_declining",
                "category": "intelligence",
                "severity": "MEDIUM",
                "title": f"IQ declining: {recent[0]:.2f} -> {recent[-1]:.2f}",
                "evidence": f"Trend: {[round(s,2) for s in recent]}",
                "proposed_fix": "Investigate: are engines going offline? Workers disconnecting? Knowledge being lost?",
                "risk": "LOW",
                "auto_fixable": False,
            })

        return issues

    def _scan_engine_health(self):
        """Check for offline engines that should be online."""
        issues = []
        engine_data = _fetch_json(f"{GOD_URL}/engines")
        if not engine_data or not isinstance(engine_data, dict):
            return issues

        engines_raw = engine_data.get("engines", engine_data)
        # engines can be dict {name: {status:...}} or list [{name:..., status:...}]
        if isinstance(engines_raw, dict):
            engines = [{"name": k, **v} if isinstance(v, dict) else {"name": k, "status": str(v)} for k, v in engines_raw.items()]
        elif isinstance(engines_raw, list):
            engines = engines_raw
        else:
            return issues

        offline = [e for e in engines if isinstance(e, dict) and e.get("status") == "offline"]
        if len(offline) > len(engines) * 0.5 and len(engines) > 4:
            names = [e.get("name", "?") for e in offline[:5]]
            issues.append({
                "slug": "many_engines_offline",
                "category": "engines",
                "severity": "HIGH",
                "title": f"{len(offline)}/{len(engines)} engines offline",
                "evidence": f"Offline: {', '.join(names)}",
                "proposed_fix": "Check import errors. Run engine_metrics.py to diagnose. May need dependency installs.",
                "risk": "MEDIUM",
                "auto_fixable": False,
            })

        return issues

    def _scan_stale_todos(self):
        """Detect TODOs that have been pending for too long."""
        issues = []
        todos_data = _load_json(TODOS_FILE)
        if not todos_data:
            return issues

        now = datetime.now()
        stale = []
        for t in todos_data.get("todos", []):
            if t.get("status") not in ("pending", "active"):
                continue
            created = t.get("created_at", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    age_min = (now - created_dt).total_seconds() / 60
                    if age_min > 120:  # stale after 2 hours
                        stale.append(f"{t.get('worker','?')}: {t.get('task','?')[:40]} ({int(age_min)}min)")
                except Exception:
                    pass

        if stale:
            issues.append({
                "slug": "stale_todos",
                "category": "productivity",
                "severity": "LOW",
                "title": f"{len(stale)} TODO(s) pending >2 hours",
                "evidence": "; ".join(stale[:3]),
                "proposed_fix": "Reassign stale TODOs to idle workers or mark as cancelled if no longer relevant.",
                "risk": "LOW",
                "auto_fixable": False,
            })

        return issues


# ── Proposal Writer ──────────────────────────────────────────────────────────

class ProposalWriter:
    """Writes improvement proposals to data/improvements/."""

    def __init__(self):
        IMPROVEMENTS_DIR.mkdir(exist_ok=True)

    def write_proposal(self, issue):
        """Write an improvement proposal markdown file. Returns filepath."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = issue["slug"][:40].replace(" ", "_")
        filename = f"{ts}_{slug}.md"
        filepath = IMPROVEMENTS_DIR / filename

        content = f"""# Improvement Proposal: {issue['title']}

**Detected:** {datetime.now().isoformat()}
**Category:** {issue['category']}
**Severity:** {issue['severity']}
**Risk Level:** {issue['risk']}
**Auto-Fixable:** {'YES' if issue.get('auto_fixable') else 'NO'}

## Issue Detected

{issue['title']}

## Evidence

{issue['evidence']}

## Proposed Fix

{issue['proposed_fix']}

## Status

PENDING
"""
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)

    def list_proposals(self):
        """List all proposals with status."""
        if not IMPROVEMENTS_DIR.exists():
            return []
        proposals = []
        for f in sorted(IMPROVEMENTS_DIR.iterdir()):
            if f.suffix == ".md":
                text = f.read_text(encoding="utf-8")
                status = "PENDING"
                if "## Status\n\nAPPLIED" in text:
                    status = "APPLIED"
                elif "## Status\n\nREJECTED" in text:
                    status = "REJECTED"
                proposals.append({
                    "file": f.name,
                    "status": status,
                    "created": f.name[:15],
                })
        return proposals


# ── Regression Checker ───────────────────────────────────────────────────────

class RegressionChecker:
    """Verify system integrity after changes."""

    def check_all(self):
        """Run all regression checks. Returns (pass_count, fail_count, details)."""
        results = []
        results.append(self._check_bus_connectivity())
        results.append(self._check_core_imports())
        results.append(self._check_god_console())
        results.append(self._check_dispatch_importable())

        passes = sum(1 for r in results if r["pass"])
        fails = sum(1 for r in results if not r["pass"])
        return passes, fails, results

    def _check_bus_connectivity(self):
        data = _fetch_json(f"{BUS_URL}/status")
        return {"name": "bus_connectivity", "pass": data is not None, "detail": "OK" if data else "Cannot reach Skynet backend"}

    def _check_core_imports(self):
        failed = []
        for module in ["core.database", "core.ocr", "core.analyzer", "core.embedder", "core.learning_store"]:
            try:
                importlib.import_module(module)
            except Exception as e:
                failed.append(f"{module}: {str(e)[:40]}")
        ok = len(failed) == 0
        return {"name": "core_imports", "pass": ok, "detail": "OK" if ok else "; ".join(failed[:3])}

    def _check_god_console(self):
        data = _fetch_json(f"{GOD_URL}/health")
        return {"name": "god_console", "pass": data is not None, "detail": "OK" if data else "Cannot reach GOD Console"}

    def _check_dispatch_importable(self):
        try:
            from tools.skynet_dispatch import dispatch_to_worker, load_workers
            return {"name": "dispatch_import", "pass": True, "detail": "OK"}
        except Exception as e:
            return {"name": "dispatch_import", "pass": False, "detail": str(e)[:60]}


# ── Metrics Tracker ──────────────────────────────────────────────────────────

class MetricsTracker:
    """Tracks self-improvement metrics in data/improvement_metrics.json."""

    def __init__(self):
        self.metrics = self._load()

    def _load(self):
        data = _load_json(METRICS_FILE)
        if data:
            return data
        return {
            "issues_detected": 0,
            "issues_fixed": 0,
            "issues_pending": 0,
            "proposals_written": 0,
            "fix_success_rate": 0.0,
            "regression_passes": 0,
            "regression_fails": 0,
            "scan_cycles": 0,
            "last_scan": None,
            "last_fix": None,
            "history": [],
        }

    def save(self):
        _save_json(METRICS_FILE, self.metrics)

    def record_scan(self, issues_found):
        self.metrics["scan_cycles"] += 1
        self.metrics["issues_detected"] += issues_found
        self.metrics["last_scan"] = datetime.now().isoformat()
        if issues_found > 0:
            self.metrics["history"].append({
                "ts": datetime.now().isoformat(),
                "event": "scan",
                "issues_found": issues_found,
            })
            if len(self.metrics["history"]) > 100:
                self.metrics["history"] = self.metrics["history"][-100:]
        self.save()

    def record_fix(self, success):
        if success:
            self.metrics["issues_fixed"] += 1
        self.metrics["last_fix"] = datetime.now().isoformat()
        total = self.metrics["issues_fixed"] + self.metrics.get("fix_failures", 0)
        if total > 0:
            self.metrics["fix_success_rate"] = round(self.metrics["issues_fixed"] / total, 2)
        self.save()

    def record_proposal(self):
        self.metrics["proposals_written"] += 1
        self.metrics["issues_pending"] += 1
        self.save()

    def record_regression(self, passes, fails):
        self.metrics["regression_passes"] += passes
        self.metrics["regression_fails"] += fails
        self.save()


# ── Main Daemon ──────────────────────────────────────────────────────────────

class SelfImproveDaemon:
    """Self-improvement daemon -- detect issues, propose fixes, track metrics."""

    def __init__(self):
        self.detector = IssueDetector()
        self.writer = ProposalWriter()
        self.checker = RegressionChecker()
        self.metrics = MetricsTracker()
        self._start_time = time.time()
        self._shutting_down = False  # signed: delta

    def _handle_signal(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT."""
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log(f"Received {sig_name} -- shutting down gracefully")
        self._shutting_down = True
        # signed: delta

    def scan_and_improve(self):
        """Single improvement cycle."""
        log("Scanning for issues...")
        issues = self.detector.scan_all()
        self.metrics.record_scan(len(issues))

        if not issues:
            log("No new issues detected.")
            return

        log(f"Found {len(issues)} issue(s)")

        for issue in issues:
            log(f"  [{issue['severity']}] {issue['title']}")

            # Write proposal
            filepath = self.writer.write_proposal(issue)
            self.metrics.record_proposal()

            # Auto-fix LOW risk + auto_fixable
            if issue.get("auto_fixable") and issue["risk"] == "LOW":
                log(f"  AUTO-FIXING: {issue['slug']}")
                # Run regression checks before any auto-fix
                passes, fails, details = self.checker.check_all()
                self.metrics.record_regression(passes, fails)
                if fails > 0:
                    log(f"  Regression check FAILED ({fails} failures) -- skipping auto-fix")
                    _post_bus("orchestrator", "improvement",
                              f"PROPOSAL (auto-fix blocked by regression): {issue['title']}")
                else:
                    self.metrics.record_fix(True)
                    _post_bus("orchestrator", "improvement",
                              f"AUTO-FIXED: {issue['title']}")
            else:
                # Post proposal for orchestrator review
                _post_bus("orchestrator", "improvement",
                          f"PROPOSAL [{issue['severity']}]: {issue['title']}. Fix: {issue['proposed_fix'][:100]}")

    def run(self):
        """Main daemon loop."""
        global _fetch_failures
        log("Self-improvement daemon starting")

        # Register signal handlers  # signed: delta
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # PID cleanup is handled by shared skynet_pid_guard (atexit + signals)  # signed: alpha

        _post_bus("orchestrator", "monitor_alert",
                  "SELF_IMPROVE_ONLINE: Self-improvement engine started")

        # Initial regression baseline
        passes, fails, details = self.checker.check_all()
        self.metrics.record_regression(passes, fails)
        log(f"Baseline regression: {passes} pass, {fails} fail")

        try:
            while not self._shutting_down:
                try:
                    self.scan_and_improve()
                    self._consecutive_loop_errors = 0  # reset on successful cycle  # signed: gamma
                except (ConnectionError, TimeoutError, OSError) as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    log(f"Scan network error ({self._consecutive_loop_errors}): {e}", "ERROR")
                except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    log(f"Scan data error ({self._consecutive_loop_errors}): {e}", "ERROR")
                except Exception as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    log(f"Scan failed ({self._consecutive_loop_errors}): {e}", "ERROR")
                if getattr(self, '_consecutive_loop_errors', 0) >= 10 and self._consecutive_loop_errors % 10 == 0:
                    _post_bus("orchestrator", "alert",
                              f"DAEMON_DEGRADED: skynet_self_improve hit {self._consecutive_loop_errors} consecutive errors")  # signed: gamma
                # Backoff: if fetches are failing, slow down scanning  # signed: delta
                backoff = min(_fetch_failures * 5, 60)
                time.sleep(SCAN_INTERVAL + backoff)
        except KeyboardInterrupt:
            log("Shutting down (Ctrl+C)")
        finally:
            _post_bus("orchestrator", "monitor_alert",
                      "SELF_IMPROVE_OFFLINE: Self-improvement engine stopped")
            if PID_FILE.exists():
                try:
                    if int(PID_FILE.read_text().strip()) == os.getpid():  # signed: delta
                        PID_FILE.unlink()
                except Exception:
                    pass

    def once(self):
        """Single scan cycle (no loop)."""
        passes, fails, details = self.checker.check_all()
        self.metrics.record_regression(passes, fails)
        log(f"Regression baseline: {passes} pass, {fails} fail")
        for d in details:
            log(f"  {d['name']}: {'PASS' if d['pass'] else 'FAIL'} -- {d['detail']}")

        self.scan_and_improve()

        # Print metrics summary
        m = self.metrics.metrics
        log(f"Metrics: detected={m['issues_detected']} fixed={m['issues_fixed']} "
            f"pending={m['issues_pending']} proposals={m['proposals_written']} "
            f"scans={m['scan_cycles']}")


# ── PID file management ─────────────────────────────────────────────────────

def _check_existing():
    """Check if daemon is already running using shared PID guard logic."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            from tools.skynet_pid_guard import _pid_alive, _pid_matches_daemon
            if _pid_alive(pid) and _pid_matches_daemon(pid, "skynet_self_improve"):
                return pid
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)
    return None
    # signed: alpha


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "start":
        # ── Atomic PID guard via shared utility ──  # signed: alpha
        from tools.skynet_pid_guard import acquire_pid_guard
        if not acquire_pid_guard(PID_FILE, "skynet_self_improve", logger=log):
            existing = _check_existing()
            if existing:
                log(f"Already running (PID {existing})")
            return
        daemon = SelfImproveDaemon()
        daemon.run()

    elif cmd == "once":
        daemon = SelfImproveDaemon()
        daemon.once()

    elif cmd == "status":
        m = _load_json(METRICS_FILE)
        if m:
            print(json.dumps(m, indent=2))
        else:
            print("No metrics found. Run 'once' or 'start' first.")

    elif cmd == "proposals":
        writer = ProposalWriter()
        proposals = writer.list_proposals()
        if proposals:
            for p in proposals:
                print(f"  [{p['status']}] {p['file']}")
        else:
            print("No proposals found.")

    elif cmd == "regression":
        checker = RegressionChecker()
        passes, fails, details = checker.check_all()
        for d in details:
            status = "PASS" if d["pass"] else "FAIL"
            print(f"  [{status}] {d['name']}: {d['detail']}")
        print(f"\nTotal: {passes} pass, {fails} fail")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
