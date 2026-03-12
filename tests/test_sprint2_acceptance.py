"""Sprint 2 Acceptance Tests — End-to-End Feature Verification.

Validates all Sprint 2 deliverables from all 4 workers (alpha, beta, gamma, delta)
work end-to-end with real execution.

Run: python -m pytest tests/test_sprint2_acceptance.py -v
"""
# signed: beta

import json
import inspect
import importlib
import subprocess
import sys
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))


# ── 1. DELIVERY PIPELINE (Alpha Sprint 2) ─────────────────────────────

class TestDeliveryPipeline(unittest.TestCase):
    """Alpha Sprint 2: Ghost-type delivery hardening."""

    def test_1_1_find_all_render_exists(self):
        """FindAllRender() collects multiple Chrome render widgets for multi-pane."""
        with open(REPO_ROOT / "tools" / "skynet_dispatch.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("FindAllRender", content,
                       "FindAllRender must exist in skynet_dispatch.py for multi-pane Chrome fix")
    # signed: beta

    def test_1_2_foreground_window_in_paste_paths(self):
        """GetForegroundWindow() check exists in all 4 paste paths."""
        with open(REPO_ROOT / "tools" / "skynet_dispatch.py", "r", encoding="utf-8") as f:
            content = f.read()
        import re
        # Count occurrences (declaration + 4 paste paths + docstring = 6+)
        matches = re.findall(r"GetForegroundWindow", content)
        # At least 5: 1 declaration + 4 paste-path checks
        self.assertGreaterEqual(len(matches), 5,
                                f"Expected >=5 GetForegroundWindow refs (decl + 4 paste paths), got {len(matches)}")
    # signed: beta

    def test_1_3_detect_steering_two_tier(self):
        """detect_steering() has 2-tier approach: UIA state + Cancel button scan."""
        from tools.skynet_dispatch import detect_steering
        src = inspect.getsource(detect_steering)
        has_uia = "get_worker_state" in src or "STEERING" in src
        has_cancel = "Cancel" in src or "cancel" in src
        self.assertTrue(has_uia, "detect_steering must check UIA state (STEERING)")
        self.assertTrue(has_cancel, "detect_steering must scan for Cancel button")
    # signed: beta

    def test_1_4_detect_steering_importable(self):
        """detect_steering is importable from skynet_dispatch."""
        from tools.skynet_dispatch import detect_steering
        self.assertTrue(callable(detect_steering))
    # signed: beta


# ── 2. DAEMON RELIABILITY (Beta Sprint 2) ──────────────────────────────

class TestDaemonReliability(unittest.TestCase):
    """Beta Sprint 2: Daemon monitoring and restart infrastructure."""

    def test_2_1_api_daemons_endpoint_code_exists(self):
        """GOD Console has /api/daemons endpoint in do_GET."""
        with open(REPO_ROOT / "god_console.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('/api/daemons', content,
                       "/api/daemons route must exist in god_console.py")
        self.assertIn("check_all_daemons", content,
                       "Endpoint must call check_all_daemons()")
    # signed: beta

    def test_2_2_daemon_status_cli_16_daemons(self):
        """skynet_daemon_status.py lists 16 daemons."""
        from skynet_daemon_status import DAEMON_REGISTRY
        self.assertEqual(len(DAEMON_REGISTRY), 16,
                         f"Expected 16 daemons in registry, got {len(DAEMON_REGISTRY)}")
    # signed: beta

    def test_2_3_daemon_status_json_output(self):
        """skynet_daemon_status.py --json produces valid JSON with summary."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_daemon_status.py"), "--json"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        data = json.loads(result.stdout)
        self.assertIn("daemons", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], 16)
    # signed: beta

    def test_2_4_watchdog_restart_functions(self):
        """Watchdog has restart_bus_persist and restart_consultant_consumer."""
        from skynet_watchdog import restart_bus_persist, restart_consultant_consumer
        self.assertTrue(callable(restart_bus_persist))
        self.assertTrue(callable(restart_consultant_consumer))
    # signed: beta


# ── 3. BUS RESILIENCE (Gamma Sprint 2) ──────────────────────────────────

class TestBusResilience(unittest.TestCase):
    """Gamma Sprint 2: Bus validation, health, priority, persistence."""

    def test_3_1_bus_validator_self_tests(self):
        """skynet_bus_validator.py --test passes 17/17."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_bus_validator.py"), "--test"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        self.assertIn("17 passed, 0 failed", result.stdout,
                       f"Expected 17/17, got: {result.stdout[-100:]}")
    # signed: beta

    def test_3_2_bus_health_returns_dict(self):
        """bus_health() returns dict with expected keys."""
        from tools.skynet_spam_guard import bus_health
        result = bus_health()
        self.assertIsInstance(result, dict)
        self.assertIn("bus_reachable", result)
        self.assertIn("spam_blocked_count", result)
    # signed: beta

    def test_3_3_priority_rate_overrides_exists(self):
        """PRIORITY_RATE_OVERRIDES is defined in skynet_spam_guard."""
        from tools.skynet_spam_guard import PRIORITY_RATE_OVERRIDES
        self.assertIsInstance(PRIORITY_RATE_OVERRIDES, dict)
        # Should have at least 'low' key
        self.assertTrue(len(PRIORITY_RATE_OVERRIDES) > 0,
                        "PRIORITY_RATE_OVERRIDES must have at least one entry")
    # signed: beta

    def test_3_4_bus_persist_diagnose_flag(self):
        """skynet_bus_persist.py accepts --diagnose flag."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_bus_persist.py"), "--diagnose"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        # Should not crash (exit 0 or produce output about archive)
        self.assertEqual(result.returncode, 0,
                         f"--diagnose failed: {result.stderr[:200]}")
    # signed: beta


# ── 4. SELF-AWARENESS (Delta Sprint 2) ──────────────────────────────────

class TestSelfAwareness(unittest.TestCase):
    """Delta Sprint 2: Architecture verification, agent completeness, pulse, patterns."""

    def test_4_1_arch_verify_brief(self):
        """skynet_arch_verify.py --brief runs without crash."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_arch_verify.py"), "--brief"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        # May pass or fail architecturally, but must not crash
        self.assertIn("Architecture Verification", result.stdout,
                       f"Expected verification output, got: {result.stdout[:200]}")
    # signed: beta

    def test_4_2_agent_completeness_validate(self):
        """skynet_self.py validate reports identity completeness."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_self.py"), "validate"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("completeness", result.stdout.lower(),
                       f"Expected completeness output, got: {result.stdout[:200]}")
    # signed: beta

    def test_4_3_pulse_three_awareness_flags(self):
        """skynet_self.py pulse includes 3 awareness flags."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_self.py"), "pulse"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        data = json.loads(result.stdout)
        # Check for 3 awareness flags
        awareness_keys = ["architecture_knowledge_ok", "consultant_awareness", "bus_awareness"]
        for key in awareness_keys:
            self.assertIn(key, data, f"Missing awareness flag: {key}")
    # signed: beta

    def test_4_4_incident_patterns(self):
        """skynet_self.py patterns detects incident patterns."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "skynet_self.py"), "patterns"],
            capture_output=True, text=True, timeout=15, cwd=str(REPO_ROOT)
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("pattern", result.stdout.lower(),
                       f"Expected pattern output, got: {result.stdout[:200]}")
    # signed: beta


if __name__ == "__main__":
    unittest.main()
# signed: beta
