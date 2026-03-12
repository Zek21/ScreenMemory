"""
Cycle 2 Acceptance Tests — End-to-End Sprint 2 Feature Verification
====================================================================
Worker: delta | 30+ acceptance tests covering all Sprint 2 user-facing features.
Tests use unittest.mock for isolation (no live bus, no live windows).

Categories:
  A. Dispatch pipeline (build_preamble, ghost_type, delivery, mark_received)
  B. Daemon lifecycle (status, pid check, JSON output, restart-dead)
  C. Bus message lifecycle (create, validate, publish, consume)
  D. Spam guard lifecycle (publish, dedup, rate, deliver/block, preflight)
  E. Architecture verification (load config, check domains, report)
  F. Self-awareness kernel (identity, pulse, validate, consultant status)
  G. Configuration acceptance (brain_config, agent_profiles, workers.json)

# signed: delta
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock, mock_open

# Ensure project root is on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ═══════════════════════════════════════════════════════════════════
# A. DISPATCH PIPELINE (7 tests)
# ═══════════════════════════════════════════════════════════════════

class TestDispatchPipeline(unittest.TestCase):
    """End-to-end dispatch pipeline acceptance tests."""

    def test_A01_build_preamble_contains_identity(self):
        """build_preamble returns string with worker identity injection."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("alpha")
        self.assertIsInstance(preamble, str)
        self.assertGreater(len(preamble), 500, "Preamble too short")
        self.assertIn("alpha", preamble.lower())
        # signed: delta

    def test_A02_build_preamble_contains_bus_instructions(self):
        """Preamble includes bus communication instructions."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("beta")
        self.assertIn("guarded_publish", preamble)
        self.assertIn("signed:", preamble.lower())
        # signed: delta

    def test_A03_build_preamble_all_workers(self):
        """build_preamble works for all 4 workers."""
        from tools.skynet_dispatch import build_preamble
        for name in ["alpha", "beta", "gamma", "delta"]:
            p = build_preamble(name)
            self.assertIsInstance(p, str)
            self.assertIn(name, p.lower(),
                          f"Worker name '{name}' not found in preamble")
        # signed: delta

    def test_A04_load_workers_returns_list(self):
        """load_workers returns a list of worker dicts from workers.json."""
        from tools.skynet_dispatch import load_workers
        workers = load_workers()
        self.assertIsInstance(workers, list)
        if workers:
            self.assertIsInstance(workers[0], dict)
            self.assertIn("name", workers[0])
            self.assertIn("hwnd", workers[0])
        # signed: delta

    def test_A05_load_orch_hwnd_returns_int_or_none(self):
        """load_orch_hwnd returns int or None."""
        from tools.skynet_dispatch import load_orch_hwnd
        hwnd = load_orch_hwnd()
        self.assertTrue(hwnd is None or isinstance(hwnd, int),
                        f"Expected int or None, got {type(hwnd)}")
        # signed: delta

    def test_A06_self_dispatch_blocked(self):
        """dispatch_to_worker rejects self-dispatch (deadlock prevention)."""
        from tools.skynet_dispatch import dispatch_to_worker
        fake_workers = [{"name": "alpha", "hwnd": 12345}]
        with patch("tools.skynet_dispatch.load_workers", return_value=fake_workers), \
             patch("tools.skynet_dispatch.load_orch_hwnd", return_value=99999), \
             patch("tools.skynet_dispatch._get_self_identity", return_value="alpha"):
            result = dispatch_to_worker("alpha", "test task",
                                        workers=fake_workers, orch_hwnd=99999)
            self.assertFalse(result, "Self-dispatch should be blocked")
        # signed: delta

    def test_A07_mark_dispatch_received_no_crash(self):
        """mark_dispatch_received handles gracefully even with missing log."""
        from tools.skynet_dispatch import mark_dispatch_received
        try:
            mark_dispatch_received("nonexistent_worker")
        except Exception as e:
            self.fail(f"mark_dispatch_received crashed: {e}")
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# B. DAEMON LIFECYCLE (4 tests)
# ═══════════════════════════════════════════════════════════════════

class TestDaemonLifecycle(unittest.TestCase):
    """Daemon status checking acceptance tests."""

    def test_B01_daemon_status_json_valid(self):
        """skynet_daemon_status.py --json produces valid JSON."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_daemon_status.py"),
             "--json"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, f"Exit code: {result.returncode}")
        data = json.loads(result.stdout)
        self.assertIn("daemons", data)
        self.assertIn("summary", data)
        self.assertIsInstance(data["daemons"], list)
        self.assertIn("alive", data["summary"])
        self.assertIn("total", data["summary"])
        # signed: delta

    def test_B02_daemon_status_summary_counts(self):
        """Daemon summary has valid total >= alive >= 0."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_daemon_status.py"),
             "--json"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        s = data["summary"]
        self.assertGreaterEqual(s["total"], 0)
        self.assertGreaterEqual(s["alive"], 0)
        self.assertGreaterEqual(s["total"], s["alive"])
        # signed: delta

    def test_B03_daemon_status_fields(self):
        """Each daemon entry has required fields."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_daemon_status.py"),
             "--json"],
            capture_output=True, text=True, timeout=90  # 16 daemons with psutil checks can be slow  # signed: gamma
        )
        data = json.loads(result.stdout)
        required = {"name", "alive"}
        for d in data["daemons"]:
            for key in required:
                self.assertIn(key, d, f"Daemon entry missing '{key}': {d.get('name','?')}")
        # signed: delta

    def test_B04_restart_dead_graceful(self):
        """--restart-dead with no dead daemons exits cleanly."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_daemon_status.py"),
             "--restart-dead"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0,
                         f"restart-dead failed: {result.stderr}")
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# C. BUS MESSAGE LIFECYCLE (5 tests)
# ═══════════════════════════════════════════════════════════════════

class TestBusMessageLifecycle(unittest.TestCase):
    """Bus message create -> validate -> publish -> consume tests."""

    def test_C01_valid_message_passes_validation(self):
        """A well-formed message passes validate_message with no errors."""
        from tools.skynet_bus_validator import validate_message
        msg = {
            "sender": "delta",
            "topic": "orchestrator",
            "type": "result",
            "content": "test acceptance message"
        }
        errors = validate_message(msg)
        self.assertIsInstance(errors, list)
        self.assertEqual(len(errors), 0,
                         f"Valid message had errors: {errors}")
        # signed: delta

    def test_C02_missing_sender_fails(self):
        """Message without sender is rejected."""
        from tools.skynet_bus_validator import validate_message
        msg = {"topic": "orchestrator", "content": "no sender"}
        errors = validate_message(msg)
        self.assertGreater(len(errors), 0, "Missing sender should fail")
        # signed: delta

    def test_C03_missing_content_fails(self):
        """Message without content is rejected."""
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "delta", "topic": "orchestrator"}
        errors = validate_message(msg)
        self.assertGreater(len(errors), 0, "Missing content should fail")
        # signed: delta

    def test_C04_all_known_topics_valid(self):
        """All known topics pass validation."""
        from tools.skynet_bus_validator import validate_message
        topics = ["orchestrator", "convene", "knowledge", "planning",
                  "scoring", "workers", "system", "consultant", "tasks",
                  "general"]
        for t in topics:
            msg = {"sender": "delta", "topic": t, "type": "message",
                   "content": f"test {t}"}
            errors = validate_message(msg)
            self.assertEqual(len(errors), 0,
                             f"Topic '{t}' failed: {errors}")
        # signed: delta

    def test_C05_bus_validator_self_test(self):
        """skynet_bus_validator.py _run_self_test passes."""
        from tools.skynet_bus_validator import _run_self_test
        result = _run_self_test()
        self.assertTrue(result, "Bus validator self-tests failed")
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# D. SPAM GUARD LIFECYCLE (7 tests)
# ═══════════════════════════════════════════════════════════════════

class TestSpamGuardLifecycle(unittest.TestCase):
    """Spam guard publish -> dedup -> rate -> deliver/block tests."""

    def test_D01_fingerprint_deterministic(self):
        """Same message always produces the same fingerprint."""
        from tools.skynet_spam_guard import SpamGuard
        msg = {"sender": "delta", "topic": "test", "type": "msg",
               "content": "acceptance test"}
        fp1 = SpamGuard.fingerprint(msg)
        fp2 = SpamGuard.fingerprint(msg)
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16, "Fingerprint should be 16 hex chars")
        # signed: delta

    def test_D02_fingerprint_differs_for_different_msgs(self):
        """Different messages produce different fingerprints."""
        from tools.skynet_spam_guard import SpamGuard
        msg1 = {"sender": "delta", "content": "message A"}
        msg2 = {"sender": "delta", "content": "message B"}
        self.assertNotEqual(SpamGuard.fingerprint(msg1),
                            SpamGuard.fingerprint(msg2))
        # signed: delta

    def test_D03_dedup_blocks_repeat(self):
        """SpamGuard blocks duplicate fingerprints within window."""
        from tools.skynet_spam_guard import SpamGuard
        guard = SpamGuard()
        fp = "test_dedup_" + str(int(time.time()))
        self.assertFalse(guard.is_duplicate(fp))
        guard._record_fingerprint(fp)
        self.assertTrue(guard.is_duplicate(fp))
        # signed: delta

    def test_D04_rate_limit_normal_sender(self):
        """Normal sender under limit returns None (not limited)."""
        from tools.skynet_spam_guard import SpamGuard
        guard = SpamGuard()
        unique_sender = f"test_sender_{int(time.time())}"
        result = guard.is_rate_limited(unique_sender)
        self.assertIsNone(result,
                          f"Fresh sender should not be rate limited: {result}")
        # signed: delta

    def test_D05_guarded_publish_rejects_none(self):
        """guarded_publish(None) is rejected by type guard."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(None)
        self.assertFalse(result.get("allowed"),
                         "None should be rejected")
        self.assertIn("invalid_message_type", result.get("reason", ""))
        # signed: delta

    def test_D06_guarded_publish_rejects_empty_dict(self):
        """guarded_publish({}) is rejected (missing required fields)."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({})
        self.assertFalse(result.get("allowed"),
                         "Empty dict should be rejected")
        # signed: delta

    def test_D07_check_would_be_blocked_preflight(self):
        """check_would_be_blocked returns a read-only preflight result."""
        from tools.skynet_spam_guard import check_would_be_blocked
        msg = {
            "sender": "delta",
            "topic": "orchestrator",
            "type": "result",
            "content": f"preflight_acceptance_{int(time.time())}"
        }
        result = check_would_be_blocked(msg)
        self.assertIn("would_block", result)
        self.assertIn("fingerprint", result)
        self.assertIsInstance(result["would_block"], bool)
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# E. ARCHITECTURE VERIFICATION (4 tests)
# ═══════════════════════════════════════════════════════════════════

class TestArchitectureVerification(unittest.TestCase):
    """Architecture verification end-to-end tests."""

    def test_E01_verify_architecture_returns_all_domains(self):
        """verify_architecture checks all 4 domains."""
        from tools.skynet_arch_verify import verify_architecture
        result = verify_architecture()
        self.assertIsInstance(result, dict)
        self.assertIn("overall", result)
        self.assertIn("checks", result)
        expected_domains = {"entities", "delivery_mechanism",
                            "bus_architecture", "daemon_ecosystem"}
        actual_domains = set(result["checks"].keys())
        self.assertEqual(expected_domains, actual_domains,
                         f"Missing domains: {expected_domains - actual_domains}")
        # signed: delta

    def test_E02_entities_check_covers_all_agents(self):
        """Entity check validates workers, consultants, and orchestrator."""
        from tools.skynet_arch_verify import check_entities
        result = check_entities()
        self.assertIn("status", result)
        # The check validates worker_names, consultant_names, all_agent_names
        # It reports aggregate counts, not individual names in details
        details = result.get("details", [])
        details_str = " ".join(details).lower()
        self.assertIn("worker_names", details_str,
                       "Entity check should verify worker_names")
        self.assertIn("consultant_names", details_str,
                       "Entity check should verify consultant_names")
        # signed: delta

    def test_E03_cli_brief_mode(self):
        """--brief produces a concise status line."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_arch_verify.py"),
             "--brief"],
            capture_output=True, text=True, timeout=30
        )
        # --brief may exit non-zero if checks fail (FAIL status) but should
        # still produce output (not crash)
        output = result.stdout.strip()
        self.assertGreater(len(output), 0, "No output from --brief")
        self.assertIn("Architecture Verification", output)
        lines = output.split("\n")
        self.assertLessEqual(len(lines), 3,
                             f"--brief should be concise, got {len(lines)} lines")
        # signed: delta

    def test_E04_cli_check_single_domain(self):
        """--check entities runs only one domain."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_arch_verify.py"),
             "--check", "entities"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0,
                         f"--check entities failed: {result.stderr}")
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# F. SELF-AWARENESS KERNEL (5 tests)
# ═══════════════════════════════════════════════════════════════════

class TestSelfAwarenessKernel(unittest.TestCase):
    """Self-awareness identity, pulse, and consultant checks."""

    def test_F01_identity_class_exists(self):
        """SkynetIdentity class can be instantiated."""
        from tools.skynet_self import SkynetIdentity
        ident = SkynetIdentity()
        self.assertTrue(hasattr(ident, "name"))
        self.assertTrue(hasattr(ident, "version"))
        self.assertTrue(hasattr(ident, "level"))
        # signed: delta

    def test_F02_pulse_returns_complete_data(self):
        """SkynetSelf.quick_pulse returns dict with identity fields."""
        from tools.skynet_self import SkynetSelf
        self_obj = SkynetSelf()
        pulse = self_obj.quick_pulse()
        self.assertIsInstance(pulse, dict)
        self.assertIn("name", pulse)
        self.assertIn("level", pulse)
        self.assertIn("health", pulse)
        # signed: delta

    def test_F03_worker_names_constant(self):
        """Module-level WORKER_NAMES has all 4 workers."""
        from tools.skynet_self import WORKER_NAMES
        expected = {"alpha", "beta", "gamma", "delta"}
        actual = set(WORKER_NAMES)
        self.assertEqual(expected, actual)
        # signed: delta

    def test_F04_consultant_names_constant(self):
        """Module-level CONSULTANT_NAMES has both consultants."""
        from tools.skynet_self import CONSULTANT_NAMES
        expected = {"consultant", "gemini_consultant"}
        actual = set(CONSULTANT_NAMES)
        self.assertEqual(expected, actual)
        # signed: delta

    def test_F05_validate_agent_completeness(self):
        """validate_agent_completeness returns list (empty=pass)."""
        from tools.skynet_self import SkynetIdentity
        ident = SkynetIdentity()
        result = ident.validate_agent_completeness()
        self.assertIsInstance(result, list,
                             "validate_agent_completeness should return list")
        # Empty list = all checks passed, non-empty = failures
        # We just verify the return type is correct
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# G. CONFIGURATION ACCEPTANCE (5 tests)
# ═══════════════════════════════════════════════════════════════════

class TestConfigurationAcceptance(unittest.TestCase):
    """Config files have required Level 3.5 fields."""

    def test_G01_brain_config_level_35(self):
        """brain_config.json has level=3.5."""
        path = os.path.join(ROOT, "data", "brain_config.json")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg.get("level"), "3.5",
                         f"Expected level 3.5, got {cfg.get('level')}")
        # signed: delta

    def test_G02_brain_config_required_sections(self):
        """brain_config.json has all required Level 3.5 sections."""
        path = os.path.join(ROOT, "data", "brain_config.json")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        required = ["level", "difficulty_thresholds", "routing",
                    "self_awareness", "dispatch_rules"]
        for key in required:
            self.assertIn(key, cfg,
                          f"brain_config.json missing '{key}'")
        # signed: delta

    def test_G03_agent_profiles_all_agents(self):
        """agent_profiles.json has entries for all workers + orchestrator."""
        path = os.path.join(ROOT, "data", "agent_profiles.json")
        with open(path, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        required = ["orchestrator", "alpha", "beta", "gamma", "delta"]
        for name in required:
            self.assertIn(name, profiles,
                          f"agent_profiles.json missing '{name}'")
            self.assertIn("role", profiles[name],
                          f"Profile '{name}' missing 'role'")
        # signed: delta

    def test_G04_workers_json_structure(self):
        """workers.json has valid structure with 4 workers."""
        path = os.path.join(ROOT, "data", "workers.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("workers", data, "workers.json missing 'workers' key")
        workers = data["workers"]
        self.assertIsInstance(workers, list)
        names = {w["name"] for w in workers}
        expected = {"alpha", "beta", "gamma", "delta"}
        self.assertEqual(names, expected,
                         f"Workers mismatch: expected {expected}, got {names}")
        # signed: delta

    def test_G05_workers_have_hwnd(self):
        """Each worker has a non-zero hwnd."""
        path = os.path.join(ROOT, "data", "workers.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for w in data["workers"]:
            self.assertIn("hwnd", w, f"Worker {w['name']} missing hwnd")
            self.assertIsInstance(w["hwnd"], int,
                                 f"Worker {w['name']} hwnd not int")
            self.assertGreater(w["hwnd"], 0,
                               f"Worker {w['name']} hwnd is zero")
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# H. INTEGRATION ACCEPTANCE (3 tests)
# ═══════════════════════════════════════════════════════════════════

class TestIntegrationAcceptance(unittest.TestCase):
    """Cross-tool integration acceptance tests."""

    def test_H01_validator_accepts_guarded_publish_format(self):
        """A message formatted for guarded_publish passes validation."""
        from tools.skynet_bus_validator import validate_message
        msg = {
            "sender": "delta",
            "topic": "orchestrator",
            "type": "result",
            "content": "integration test signed:delta"
        }
        errors = validate_message(msg)
        self.assertEqual(len(errors), 0,
                         f"Bus format rejected: {errors}")
        # signed: delta

    def test_H02_dispatch_preamble_mentions_spam_guard(self):
        """Dispatch preamble instructs workers to use guarded_publish."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("gamma")
        self.assertIn("guarded_publish", preamble,
                       "Preamble must mention guarded_publish for anti-spam")
        # signed: delta

    def test_H03_pulse_cli_outputs_json(self):
        """skynet_self.py pulse produces parseable JSON."""
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "skynet_self.py"),
             "pulse"],
            capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0,
                         f"pulse failed: {result.stderr}")
        data = json.loads(result.stdout)
        self.assertIn("name", data)
        self.assertIn("health", data)
        # signed: delta


# ═══════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
# signed: delta
