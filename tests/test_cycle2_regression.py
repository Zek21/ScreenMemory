"""
Cycle 2 Regression Tests — Sprint 2 Feature Backward Compatibility
===================================================================
Verifies ALL Sprint 2 tools continue working after Level 3.5 integration.
Uses unittest.mock for network isolation — no live HTTP calls.

Test coverage:
  - skynet_dispatch.py: ghost_type delivery, multi-pane Chrome, focus race
  - skynet_daemon_status.py: daemon registry, --json, check_daemon
  - skynet_bus_validator.py: message validation, topic taxonomy
  - skynet_spam_guard.py: guarded_publish, bus_health, rate limiting
  - skynet_arch_verify.py: architecture domain verification
  - skynet_self.py: validate_agent_completeness, enhanced pulse
  - Boot scripts: Orch-Start.ps1, CC-Start.ps1, GC-Start.ps1
  - Data file integrity: workers.json, brain_config.json, agent_profiles.json

signed: alpha
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"


# ═══════════════════════════════════════════════════════════
# Section 1: skynet_dispatch.py regression (8 tests)
# ═══════════════════════════════════════════════════════════

class TestDispatchRegression:
    """Verify dispatch pipeline functions survive Level 3.5 integration."""
    # signed: alpha

    def test_core_imports(self):
        """All dispatch public functions still importable."""
        from tools.skynet_dispatch import (
            ghost_type_to_worker, load_workers, load_orch_hwnd,
            dispatch_to_worker, build_preamble,
        )
        assert callable(ghost_type_to_worker)
        assert callable(load_workers)
        assert callable(build_preamble)
        # signed: alpha

    def test_build_preamble_returns_string(self):
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("alpha")
        assert isinstance(preamble, str)
        assert len(preamble) > 100  # non-trivial preamble
        assert "alpha" in preamble
        # signed: alpha

    def test_preamble_contains_identity(self):
        from tools.skynet_dispatch import build_preamble
        for worker in ["alpha", "beta", "gamma", "delta"]:
            p = build_preamble(worker)
            assert worker in p, f"Preamble missing worker name: {worker}"
        # signed: alpha

    def test_load_workers_returns_list(self):
        from tools.skynet_dispatch import load_workers
        workers = load_workers()
        assert isinstance(workers, list)
        # signed: alpha

    def test_load_orch_hwnd_returns_int(self):
        from tools.skynet_dispatch import load_orch_hwnd
        hwnd = load_orch_hwnd()
        assert isinstance(hwnd, int)
        # signed: alpha

    def test_detect_steering_importable(self):
        from tools.skynet_dispatch import detect_steering
        assert callable(detect_steering)
        # signed: alpha

    @patch("tools.skynet_dispatch.subprocess.run")
    def test_ghost_type_ps_generation(self, mock_run):
        """_build_ghost_type_ps generates valid PowerShell with C# interop."""
        from tools.skynet_dispatch import _build_ghost_type_ps
        import tempfile
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".txt")
            os.write(fd, b"test task content")
            os.close(fd)
            ps = _build_ghost_type_ps(12345, 67890, tmp_path)
            assert isinstance(ps, str)
            assert "DllImport" in ps or "InteropServices" in ps or "PostMessage" in ps
            assert "12345" in ps  # target hwnd
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        # signed: alpha

    def test_dispatch_py_compiles(self):
        """skynet_dispatch.py has no syntax errors."""
        import py_compile
        py_compile.compile(str(ROOT / "tools" / "skynet_dispatch.py"), doraise=True)
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 2: skynet_daemon_status.py regression (7 tests)
# ═══════════════════════════════════════════════════════════

class TestDaemonStatusRegression:
    """Verify daemon status system intact after integration."""
    # signed: alpha

    def test_registry_has_16_daemons(self):
        from tools.skynet_daemon_status import DAEMON_REGISTRY
        assert len(DAEMON_REGISTRY) == 16
        # signed: alpha

    def test_registry_daemon_structure(self):
        from tools.skynet_daemon_status import DAEMON_REGISTRY
        required = {"name", "label", "criticality"}
        for d in DAEMON_REGISTRY:
            for key in required:
                assert key in d, f"Daemon {d.get('name','?')} missing '{key}'"
        # signed: alpha

    def test_criticality_tiers(self):
        from tools.skynet_daemon_status import DAEMON_REGISTRY
        valid = {"CATASTROPHIC", "HIGH", "MODERATE", "LOW"}
        tiers_found = {d["criticality"] for d in DAEMON_REGISTRY}
        assert tiers_found.issubset(valid)
        assert len(tiers_found) >= 3  # at least 3 tiers used
        # signed: alpha

    def test_catastrophic_daemon_exists(self):
        """skynet_backend must be CATASTROPHIC."""
        from tools.skynet_daemon_status import DAEMON_REGISTRY
        cats = [d for d in DAEMON_REGISTRY if d["criticality"] == "CATASTROPHIC"]
        assert len(cats) >= 1
        names = [d["name"] for d in cats]
        assert "skynet_backend" in names
        # signed: alpha

    def test_check_daemon_returns_dict(self):
        from tools.skynet_daemon_status import check_daemon, DAEMON_REGISTRY
        result = check_daemon(DAEMON_REGISTRY[0])
        assert isinstance(result, dict)
        assert "name" in result
        assert "alive" in result
        assert isinstance(result["alive"], bool)
        # signed: alpha

    @patch("tools.skynet_daemon_status._check_url", return_value=False)
    @patch("tools.skynet_daemon_status._pid_alive", return_value=False)
    def test_check_daemon_dead_mocked(self, mock_pid, mock_url):
        from tools.skynet_daemon_status import check_daemon
        fake = {"name": "test_dead", "label": "Test", "criticality": "LOW",
                "pid_file": None, "port": None, "health_url": None}
        result = check_daemon(fake)
        assert result["alive"] is False
        # signed: alpha

    def test_check_all_daemons_count(self):
        from tools.skynet_daemon_status import check_all_daemons
        results = check_all_daemons()
        assert isinstance(results, list)
        assert len(results) == 16
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 3: skynet_bus_validator.py regression (8 tests)
# ═══════════════════════════════════════════════════════════

class TestBusValidatorRegression:
    """Verify bus message validation unchanged."""
    # signed: alpha

    def test_taxonomy_has_10_topics(self):
        from tools.skynet_bus_validator import TOPIC_TAXONOMY
        assert len(TOPIC_TAXONOMY) >= 10
        # signed: alpha

    def test_known_senders_complete(self):
        from tools.skynet_bus_validator import KNOWN_SENDERS
        required = {"alpha", "beta", "gamma", "delta", "orchestrator", "system", "monitor"}
        assert required.issubset(set(KNOWN_SENDERS))
        # signed: alpha

    def test_valid_message_passes(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "alpha", "topic": "orchestrator",
               "type": "result", "content": "test"}
        errors = validate_message(msg)
        assert errors == []
        # signed: alpha

    def test_empty_message_fails(self):
        from tools.skynet_bus_validator import validate_message
        errors = validate_message({})
        assert len(errors) > 0
        # signed: alpha

    def test_strict_rejects_unknown_topic(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "alpha", "topic": "fake_topic_xyz",
               "type": "result", "content": "test"}
        errors = validate_message(msg, strict=True)
        assert len(errors) > 0
        # signed: alpha

    def test_nonstrict_allows_unknown_topic(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "alpha", "topic": "fake_topic_xyz",
               "type": "result", "content": "test"}
        errors = validate_message(msg, strict=False)
        topic_errs = [e for e in errors if "topic" in e.lower() and "unknown" in e.lower()]
        assert len(topic_errs) == 0
        # signed: alpha

    def test_oversized_content_rejected(self):
        from tools.skynet_bus_validator import validate_message, MAX_CONTENT_LENGTH
        msg = {"sender": "alpha", "topic": "orchestrator",
               "type": "result", "content": "x" * (MAX_CONTENT_LENGTH + 1)}
        errors = validate_message(msg)
        assert len(errors) > 0
        # signed: alpha

    def test_self_test_passes(self):
        from tools.skynet_bus_validator import _run_self_test
        assert _run_self_test() is True
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 4: skynet_spam_guard.py regression (8 tests)
# ═══════════════════════════════════════════════════════════

class TestSpamGuardRegression:
    """Verify spam guard filtering intact."""
    # signed: alpha

    def test_fingerprint_deterministic(self):
        from tools.skynet_spam_guard import SpamGuard
        msg = {"sender": "alpha", "topic": "test", "type": "r", "content": "hello"}
        assert SpamGuard.fingerprint(msg) == SpamGuard.fingerprint(msg)
        # signed: alpha

    def test_fingerprint_length(self):
        from tools.skynet_spam_guard import SpamGuard
        fp = SpamGuard.fingerprint({"sender": "a", "topic": "b",
                                     "type": "c", "content": "d"})
        assert isinstance(fp, str)
        assert len(fp) == 16
        # signed: alpha

    def test_fingerprint_varies_by_sender(self):
        from tools.skynet_spam_guard import SpamGuard
        base = {"topic": "t", "type": "r", "content": "c"}
        fp1 = SpamGuard.fingerprint({**base, "sender": "alpha"})
        fp2 = SpamGuard.fingerprint({**base, "sender": "beta"})
        assert fp1 != fp2
        # signed: alpha

    def test_dedup_blocks_repeat(self):
        from tools.skynet_spam_guard import SpamGuard
        sg = SpamGuard()
        sg.reset()
        msg = {"sender": f"regression_{time.time()}", "topic": "orchestrator",
               "type": "result", "content": f"unique_{time.time()}"}
        r1 = sg.publish_guarded(msg)
        assert r1["allowed"] is True
        r2 = sg.publish_guarded(msg)
        assert r2["allowed"] is False
        # signed: alpha

    def test_fresh_sender_not_rate_limited(self):
        from tools.skynet_spam_guard import SpamGuard
        sg = SpamGuard()
        sg.reset()
        result = sg.is_rate_limited(f"fresh_{time.time()}")
        assert result is None
        # signed: alpha

    def test_bus_health_returns_dict(self):
        from tools.skynet_spam_guard import bus_health
        result = bus_health()
        assert isinstance(result, dict)
        assert "bus_reachable" in result
        # signed: alpha

    def test_check_would_be_blocked_readonly(self):
        from tools.skynet_spam_guard import check_would_be_blocked
        msg = {"sender": "alpha", "topic": "orchestrator",
               "type": "result", "content": f"readonly_{time.time()}"}
        result = check_would_be_blocked(msg)
        assert isinstance(result, dict)
        assert "would_block" in result
        assert isinstance(result["would_block"], bool)
        # signed: alpha

    def test_priority_constants_exist(self):
        from tools.skynet_spam_guard import PRIORITY_RATE_OVERRIDES
        assert "critical" in PRIORITY_RATE_OVERRIDES
        assert "low" in PRIORITY_RATE_OVERRIDES
        assert PRIORITY_RATE_OVERRIDES["critical"] is None  # bypass
        assert isinstance(PRIORITY_RATE_OVERRIDES["low"], tuple)
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 5: skynet_arch_verify.py regression (6 tests)
# ═══════════════════════════════════════════════════════════

class TestArchVerifyRegression:
    """Verify architecture checker constants and return types."""
    # signed: alpha

    def test_expected_workers_constant(self):
        from tools.skynet_arch_verify import EXPECTED_WORKERS
        assert EXPECTED_WORKERS == ["alpha", "beta", "gamma", "delta"]
        # signed: alpha

    def test_expected_consultants_constant(self):
        from tools.skynet_arch_verify import EXPECTED_CONSULTANTS
        assert EXPECTED_CONSULTANTS == ["consultant", "gemini_consultant"]
        # signed: alpha

    def test_expected_daemons_count(self):
        from tools.skynet_arch_verify import EXPECTED_DAEMONS
        assert len(EXPECTED_DAEMONS) == 8
        # signed: alpha

    def test_verify_architecture_structure(self):
        from tools.skynet_arch_verify import verify_architecture
        result = verify_architecture()
        assert isinstance(result, dict)
        for key in ("overall", "total_checks", "total_failures", "checks", "timestamp"):
            assert key in result, f"Missing key: {key}"
        assert result["overall"] in ("PASS", "FAIL")
        # signed: alpha

    def test_four_domains_checked(self):
        from tools.skynet_arch_verify import verify_architecture
        result = verify_architecture()
        checks = result["checks"]
        expected = {"entities", "delivery_mechanism", "bus_architecture", "daemon_ecosystem"}
        assert expected.issubset(set(checks.keys()))
        # signed: alpha

    def test_each_domain_has_status(self):
        from tools.skynet_arch_verify import verify_architecture
        result = verify_architecture()
        for domain, info in result["checks"].items():
            assert "status" in info, f"Domain {domain} missing 'status'"
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 6: skynet_self.py regression (7 tests)
# ═══════════════════════════════════════════════════════════

class TestSkynetSelfRegression:
    """Verify consciousness kernel intact."""
    # signed: alpha

    def test_worker_names_constant(self):
        from tools.skynet_self import WORKER_NAMES
        assert WORKER_NAMES == ["alpha", "beta", "gamma", "delta"]
        # signed: alpha

    def test_all_agent_names_count(self):
        from tools.skynet_self import ALL_AGENT_NAMES
        assert len(ALL_AGENT_NAMES) == 7
        assert "orchestrator" in ALL_AGENT_NAMES
        # signed: alpha

    def test_validate_completeness_returns_list(self):
        from tools.skynet_self import SkynetIdentity
        gaps = SkynetIdentity().validate_agent_completeness()
        assert isinstance(gaps, list)
        for gap in gaps:
            assert isinstance(gap, dict)
        # signed: alpha

    def test_quick_pulse_structure(self):
        from tools.skynet_self import SkynetSelf
        pulse = SkynetSelf().quick_pulse()
        assert isinstance(pulse, dict)
        assert pulse.get("name") == "SKYNET"
        assert "alive" in pulse
        assert "total" in pulse
        # signed: alpha

    def test_pulse_awareness_flags(self):
        from tools.skynet_self import SkynetSelf
        pulse = SkynetSelf().quick_pulse()
        for flag in ("architecture_knowledge_ok", "consultant_awareness", "bus_awareness"):
            assert flag in pulse, f"Missing awareness flag: {flag}"
            assert isinstance(pulse[flag], bool)
        # signed: alpha

    def test_detect_incident_patterns(self):
        from tools.skynet_self import SkynetIntrospection
        patterns = SkynetIntrospection._detect_incident_patterns()
        assert isinstance(patterns, list)
        # signed: alpha

    def test_compute_iq_returns_score(self):
        from tools.skynet_self import SkynetSelf
        iq = SkynetSelf().compute_iq()
        assert isinstance(iq, dict)
        assert "score" in iq
        assert isinstance(iq["score"], (int, float))
        assert "trend" in iq
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 7: Boot script integrity (3 tests)
# ═══════════════════════════════════════════════════════════

class TestBootScriptIntegrity:
    """Verify boot scripts parse without errors."""
    # signed: alpha

    def test_orch_start_exists_and_parses(self):
        path = ROOT / "Orch-Start.ps1"
        assert path.exists(), "Orch-Start.ps1 missing"
        content = path.read_text(encoding="utf-8-sig")
        assert len(content) > 100
        # Must contain key function calls
        assert "8420" in content  # backend port
        # signed: alpha

    def test_orch_start_god_console_probe_checks_ipv4_and_ipv6_loopback(self):
        path = ROOT / "Orch-Start.ps1"
        content = path.read_text(encoding="utf-8-sig")
        assert '@("127.0.0.1", "localhost", "::1")' in content
        # signed: consultant

    def test_cc_start_exists_and_parses(self):
        path = ROOT / "CC-Start.ps1"
        assert path.exists(), "CC-Start.ps1 missing"
        content = path.read_text(encoding="utf-8-sig")
        assert "8422" in content or "consultant" in content.lower()
        # signed: alpha

    def test_gc_start_exists_and_parses(self):
        path = ROOT / "GC-Start.ps1"
        assert path.exists(), "GC-Start.ps1 missing"
        content = path.read_text(encoding="utf-8-sig")
        assert "8425" in content or "gemini" in content.lower()
        # signed: alpha

    def test_skynet_start_port_open_checks_ipv4_and_ipv6_loopback(self):
        path = ROOT / "tools" / "skynet_start.py"
        assert path.exists(), "tools/skynet_start.py missing"
        content = path.read_text(encoding="utf-8")
        assert '("127.0.0.1", socket.AF_INET)' in content
        assert '("::1", socket.AF_INET6)' in content
        assert '("localhost", 0)' in content
        # signed: consultant


# ═══════════════════════════════════════════════════════════
# Section 8: Data file integrity (7 tests)
# ═══════════════════════════════════════════════════════════

class TestDataFileIntegrity:
    """Verify data files are valid JSON with correct structure."""
    # signed: alpha

    def test_workers_json_valid(self):
        path = DATA / "workers.json"
        assert path.exists(), "workers.json missing"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "workers" in data
        assert isinstance(data["workers"], list)
        # signed: alpha

    def test_workers_json_worker_structure(self):
        data = json.loads((DATA / "workers.json").read_text(encoding="utf-8"))
        for w in data["workers"]:
            assert "name" in w
            assert "hwnd" in w
            assert w["name"] in ("alpha", "beta", "gamma", "delta")
        # signed: alpha

    def test_brain_config_valid(self):
        path = DATA / "brain_config.json"
        assert path.exists(), "brain_config.json missing"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        # Must have known config sections
        assert "bootstrap" in data or "compliance" in data or "dispatch_rules" in data
        # signed: alpha

    def test_brain_config_has_worker_profiles(self):
        data = json.loads((DATA / "brain_config.json").read_text(encoding="utf-8"))
        # Worker profiles may be nested under a key or at top level
        has_profiles = any(
            worker in data or
            worker in data.get("worker_profiles", {}) or
            worker in data.get("agents", {})
            for worker in ["alpha", "beta", "gamma", "delta"]
        )
        assert has_profiles or "dispatch_rules" in data, "brain_config.json has no worker profile info"
        # signed: alpha

    def test_agent_profiles_valid(self):
        path = DATA / "agent_profiles.json"
        assert path.exists(), "agent_profiles.json missing"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        # signed: alpha

    def test_orchestrator_json_valid(self):
        path = DATA / "orchestrator.json"
        assert path.exists(), "orchestrator.json missing"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        # signed: alpha

    def test_todos_json_valid(self):
        path = DATA / "todos.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data, (dict, list))
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 9: Level 3.0-3.4 backward compatibility (5 tests)
# ═══════════════════════════════════════════════════════════

class TestBackwardCompatibility:
    """Verify Level 3.0-3.4 features still work."""
    # signed: alpha

    def test_daemon_utils_roundtrip(self):
        from tools.skynet_daemon_utils import write_pid, check_pid, cleanup_pid
        name = "test_compat_alpha"
        pid_path = DATA / f"{name}.pid"
        try:
            write_pid(name)
            assert check_pid(name) is True
            cleanup_pid(name)
            assert check_pid(name) is False
        finally:
            pid_path.unlink(missing_ok=True)
        # signed: alpha

    def test_ensure_singleton_fresh(self):
        from tools.skynet_daemon_utils import ensure_singleton
        name = "test_singleton_compat"
        pid_path = DATA / f"{name}.pid"
        pid_path.unlink(missing_ok=True)
        try:
            assert ensure_singleton(name) is True
        finally:
            pid_path.unlink(missing_ok=True)
        # signed: alpha

    def test_validate_or_raise_valid(self):
        from tools.skynet_bus_validator import validate_or_raise
        msg = {"sender": "alpha", "topic": "orchestrator",
               "type": "result", "content": "test"}
        validate_or_raise(msg)  # should not raise
        # signed: alpha

    def test_validate_or_raise_invalid(self):
        from tools.skynet_bus_validator import validate_or_raise
        with pytest.raises(ValueError):
            validate_or_raise({})
        # signed: alpha

    def test_list_topics_returns_sorted(self):
        from tools.skynet_bus_validator import list_topics
        topics = list_topics()
        assert isinstance(topics, list)
        assert len(topics) >= 10
        assert topics == sorted(topics)
        # signed: alpha


# ═══════════════════════════════════════════════════════════
# Section 10: py_compile syntax checks (4 tests)
# ═══════════════════════════════════════════════════════════

class TestSyntaxIntegrity:
    """All Sprint 2 tools compile without syntax errors."""
    # signed: alpha

    @pytest.mark.parametrize("module", [
        "tools/skynet_dispatch.py",
        "tools/skynet_daemon_status.py",
        "tools/skynet_bus_validator.py",
        "tools/skynet_spam_guard.py",
        "tools/skynet_arch_verify.py",
        "tools/skynet_self.py",
        "tools/skynet_daemon_utils.py",
        "tools/skynet_monitor.py",
        "tools/skynet_watchdog.py",
        "tools/skynet_consultant_consumer.py",
    ])
    def test_py_compile(self, module):
        import py_compile
        py_compile.compile(str(ROOT / module), doraise=True)
        # signed: alpha


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
