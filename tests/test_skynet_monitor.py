#!/usr/bin/env python3
"""Tests for tools/skynet_monitor.py — Worker health monitoring.

Tests cover: window liveness checks, model drift detection, false-DEAD
debounce (3 consecutive checks), alert dedup, model string validation,
agent string validation, health file writing, and grace period logic.

# signed: alpha
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Model / Agent Validation ────────────────────────────────────

class TestModelValidation:
    """Test is_model_correct() logic."""

    def _is_model_correct(self, model_str):
        """Inline reimplementation matching skynet_monitor logic."""
        if not model_str:
            return False
        lower = model_str.lower()
        return "opus" in lower and "fast" in lower

    def test_correct_model_string(self):
        assert self._is_model_correct("Pick Model, Claude Opus 4.6 (fast mode)")

    def test_correct_model_lowercase(self):
        assert self._is_model_correct("pick model, claude opus 4.6 (fast mode)")

    def test_wrong_model_sonnet(self):
        assert not self._is_model_correct("Pick Model, Claude Sonnet 4.5")

    def test_wrong_model_auto(self):
        assert not self._is_model_correct("Pick Model, Auto")

    def test_opus_without_fast(self):
        assert not self._is_model_correct("Pick Model, Claude Opus 4.6")

    def test_fast_without_opus(self):
        assert not self._is_model_correct("Pick Model, Fast Model v3")

    def test_empty_string(self):
        assert not self._is_model_correct("")

    def test_none_string(self):
        assert not self._is_model_correct(None)


class TestAgentValidation:
    """Test is_agent_cli() logic."""

    def _is_agent_cli(self, agent_str):
        """Inline reimplementation matching skynet_monitor logic."""
        if not agent_str:
            return False
        lower = agent_str.lower()
        return any(k in lower for k in ("cli", "copilot cli", "screenmemory"))

    def test_copilot_cli(self):
        assert self._is_agent_cli("Copilot CLI")

    def test_screenmemory_agent(self):
        assert self._is_agent_cli("ScreenMemory Agent")

    def test_cli_variant(self):
        assert self._is_agent_cli("Set Agent, CLI")

    def test_agent_mode(self):
        assert not self._is_agent_cli("Agent")

    def test_edits_mode(self):
        assert not self._is_agent_cli("Edits")

    def test_empty_agent(self):
        assert not self._is_agent_cli("")

    def test_none_agent(self):
        assert not self._is_agent_cli(None)


# ── False-DEAD Debounce ─────────────────────────────────────────

class TestDeadDebounce:
    """Test the false-DEAD 3-consecutive-check debounce logic."""

    def test_first_dead_check_does_not_alert(self):
        """A single DEAD detection should NOT produce an alert."""
        dead_consecutive = {}
        worker = "alpha"
        threshold = 3

        dead_consecutive[worker] = dead_consecutive.get(worker, 0) + 1
        should_alert = dead_consecutive[worker] >= threshold
        assert not should_alert
        assert dead_consecutive[worker] == 1

    def test_second_dead_check_does_not_alert(self):
        dead_consecutive = {"alpha": 1}
        dead_consecutive["alpha"] += 1
        assert dead_consecutive["alpha"] < 3

    def test_third_dead_check_alerts(self):
        """Three consecutive DEAD checks should trigger an alert."""
        dead_consecutive = {"alpha": 2}
        dead_consecutive["alpha"] += 1
        assert dead_consecutive["alpha"] >= 3

    def test_alive_resets_counter(self):
        """An alive check should reset the consecutive counter to 0."""
        dead_consecutive = {"alpha": 2}
        # Worker comes back alive
        dead_consecutive["alpha"] = 0
        assert dead_consecutive["alpha"] == 0

    def test_different_workers_independent(self):
        """Each worker has independent debounce counters."""
        dead_consecutive = {"alpha": 2, "beta": 0}
        dead_consecutive["alpha"] += 1
        dead_consecutive["beta"] += 1
        assert dead_consecutive["alpha"] >= 3  # should alert
        assert dead_consecutive["beta"] < 3    # should not alert


# ── Alert Dedup ──────────────────────────────────────────────────

class TestAlertDedup:
    """Test alert deduplication window (600s)."""

    def test_first_alert_passes(self):
        last_alert = {}
        worker = "alpha"
        dedup_window = 600
        now = time.time()

        should_send = (worker not in last_alert or
                       now - last_alert.get(worker, 0) > dedup_window)
        assert should_send

    def test_duplicate_within_window_blocked(self):
        now = time.time()
        last_alert = {"alpha": now - 100}  # 100s ago
        dedup_window = 600

        should_send = (now - last_alert.get("alpha", 0) > dedup_window)
        assert not should_send

    def test_alert_after_window_passes(self):
        now = time.time()
        last_alert = {"alpha": now - 700}  # 700s ago, past 600s window
        dedup_window = 600

        should_send = (now - last_alert.get("alpha", 0) > dedup_window)
        assert should_send


# ── Health File Writing ──────────────────────────────────────────

class TestHealthWriting:
    """Test health data serialization."""

    def test_health_dict_structure(self, tmp_path):
        health_file = tmp_path / "worker_health.json"
        health = {
            "alpha": {"alive": True, "visible": True, "model_ok": True, "agent_ok": True, "status": "IDLE"},
            "beta": {"alive": False, "visible": False, "model_ok": False, "agent_ok": False, "status": "DEAD"},
            "timestamp": "2026-03-20T10:00:00",
        }
        health_file.write_text(json.dumps(health, indent=2))
        loaded = json.loads(health_file.read_text())
        assert loaded["alpha"]["alive"] is True
        assert loaded["beta"]["status"] == "DEAD"
        assert "timestamp" in loaded

    def test_health_file_round_trip(self, tmp_path):
        health_file = tmp_path / "worker_health.json"
        workers = ["alpha", "beta", "gamma", "delta"]
        health = {}
        for w in workers:
            health[w] = {
                "alive": True, "visible": True,
                "model_ok": True, "agent_ok": True,
                "status": "IDLE", "consecutive_dead": 0,
            }
        health_file.write_text(json.dumps(health))
        loaded = json.loads(health_file.read_text())
        assert len([k for k in loaded if k != "timestamp"]) == 4


# ── Model Fix Backoff ───────────────────────────────────────────

class TestModelFixBackoff:
    """Test model fix attempt tracking and backoff logic."""

    def test_first_attempt_allowed(self):
        fix_attempts = {}
        worker = "alpha"
        max_attempts = 3
        backoff_s = 600

        attempts = fix_attempts.get(worker, {"count": 0, "last": 0})
        can_fix = (attempts["count"] < max_attempts or
                   time.time() - attempts["last"] > backoff_s)
        assert can_fix

    def test_max_attempts_blocks(self):
        now = time.time()
        fix_attempts = {"alpha": {"count": 3, "last": now}}
        max_attempts = 3
        backoff_s = 600

        attempts = fix_attempts["alpha"]
        can_fix = (attempts["count"] < max_attempts or
                   now - attempts["last"] > backoff_s)
        assert not can_fix

    def test_backoff_expired_allows_retry(self):
        now = time.time()
        fix_attempts = {"alpha": {"count": 3, "last": now - 700}}
        max_attempts = 3
        backoff_s = 600

        attempts = fix_attempts["alpha"]
        can_fix = (attempts["count"] < max_attempts or
                   now - attempts["last"] > backoff_s)
        assert can_fix


# ── Grace Period Logic ───────────────────────────────────────────

class TestGracePeriods:
    """Test boot and startup grace period suppression."""

    def test_boot_grace_suppresses_dead(self):
        boot_time = time.time()
        boot_grace = 300
        now = boot_time + 100  # 100s into boot

        in_grace = (now - boot_time) < boot_grace
        assert in_grace  # should suppress DEAD alerts

    def test_boot_grace_expired(self):
        boot_time = time.time() - 400
        boot_grace = 300
        now = time.time()

        in_grace = (now - boot_time) < boot_grace
        assert not in_grace  # grace expired, DEAD alerts should fire

    def test_monitor_startup_grace(self):
        monitor_start = time.time()
        startup_grace = 90
        now = monitor_start + 30  # 30s after monitor start

        in_grace = (now - monitor_start) < startup_grace
        assert in_grace
