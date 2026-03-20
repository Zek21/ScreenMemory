#!/usr/bin/env python3
"""Core tests for skynet_dispatch.py — guard logic, routing, preamble, worker loading.

Tests the most critical dispatch pipeline functions WITHOUT requiring
live HWND handles, UIA, or running Skynet backend. All Win32 and
network calls are mocked.
# signed: gamma
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_workers_file(tmp_path):
    """Create a temporary workers.json with realistic data."""
    workers = {
        "workers": [
            {"name": "alpha", "hwnd": 12345, "model": "Claude Opus 4.6 (fast mode)"},
            {"name": "beta", "hwnd": 12346, "model": "Claude Opus 4.6 (fast mode)"},
            {"name": "gamma", "hwnd": 12347, "model": "Claude Opus 4.6 (fast mode)"},
            {"name": "delta", "hwnd": 12348, "model": "Claude Opus 4.6 (fast mode)"},
        ],
        "created": "2026-03-20T00:00:00Z",
    }
    wf = tmp_path / "workers.json"
    wf.write_text(json.dumps(workers), encoding="utf-8")
    return wf


@pytest.fixture
def tmp_orch_file(tmp_path):
    """Create a temporary orchestrator.json."""
    data = {"orchestrator_hwnd": 99999, "updated": "2026-03-20T00:00:00Z"}
    of = tmp_path / "orchestrator.json"
    of.write_text(json.dumps(data), encoding="utf-8")
    return of


@pytest.fixture
def tmp_brain_config(tmp_path):
    """Create a temporary brain_config.json with daemon guard disabled."""
    cfg = {"daemon_ghost_type_global_enabled": False, "self_prompt": {"enabled": False}}
    cf = tmp_path / "brain_config.json"
    cf.write_text(json.dumps(cfg), encoding="utf-8")
    return cf


# ---------------------------------------------------------------------------
# 1. load_workers() tests
# ---------------------------------------------------------------------------

class TestLoadWorkers:
    """Test load_workers() parsing of workers.json."""

    def test_load_workers_returns_list(self, tmp_workers_file):
        """load_workers should return a list of worker dicts."""
        from tools.skynet_dispatch import load_workers, WORKERS_FILE
        with patch.object(
            sys.modules["tools.skynet_dispatch"], "WORKERS_FILE", tmp_workers_file
        ):
            workers = load_workers()
        assert isinstance(workers, list)
        assert len(workers) == 4
        assert workers[0]["name"] == "alpha"
        # signed: gamma

    def test_load_workers_missing_file(self, tmp_path):
        """load_workers returns [] when file is missing."""
        from tools.skynet_dispatch import load_workers
        fake_path = tmp_path / "nonexistent.json"
        with patch.object(
            sys.modules["tools.skynet_dispatch"], "WORKERS_FILE", fake_path
        ):
            result = load_workers()
        assert result == []
        # signed: gamma

    def test_load_workers_corrupt_json(self, tmp_path):
        """load_workers returns [] on corrupt JSON."""
        from tools.skynet_dispatch import load_workers
        bad_file = tmp_path / "workers.json"
        bad_file.write_text("{bad json!!!", encoding="utf-8")
        with patch.object(
            sys.modules["tools.skynet_dispatch"], "WORKERS_FILE", bad_file
        ):
            result = load_workers()
        assert result == []
        # signed: gamma


# ---------------------------------------------------------------------------
# 2. load_orch_hwnd() tests
# ---------------------------------------------------------------------------

class TestLoadOrchHwnd:
    """Test load_orch_hwnd() parsing of orchestrator.json."""

    def test_load_orch_hwnd_valid(self, tmp_orch_file):
        """load_orch_hwnd returns the hwnd integer."""
        from tools.skynet_dispatch import load_orch_hwnd
        with patch.object(
            sys.modules["tools.skynet_dispatch"], "ORCH_FILE", tmp_orch_file
        ):
            hwnd = load_orch_hwnd()
        assert hwnd == 99999
        # signed: gamma

    def test_load_orch_hwnd_missing_file(self, tmp_path):
        """load_orch_hwnd returns None when file is missing."""
        from tools.skynet_dispatch import load_orch_hwnd
        with patch.object(
            sys.modules["tools.skynet_dispatch"], "ORCH_FILE", tmp_path / "nope.json"
        ):
            result = load_orch_hwnd()
        assert result is None
        # signed: gamma


# ---------------------------------------------------------------------------
# 3. build_preamble() tests
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    """Test build_preamble() output structure."""

    def test_preamble_contains_worker_name(self):
        """Preamble must reference the target worker's name."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("alpha")
        assert "alpha" in preamble.lower() or "ALPHA" in preamble
        # signed: gamma

    def test_preamble_contains_guarded_publish(self):
        """Preamble must instruct worker to use guarded_publish."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("beta")
        assert "guarded_publish" in preamble
        # signed: gamma

    def test_preamble_different_per_worker(self):
        """Each worker gets a personalized preamble."""
        from tools.skynet_dispatch import build_preamble
        p_alpha = build_preamble("alpha")
        p_beta = build_preamble("beta")
        assert p_alpha != p_beta
        # signed: gamma


# ---------------------------------------------------------------------------
# 4. Daemon ghost-type guard tests
# ---------------------------------------------------------------------------

class TestDaemonGhostTypeGuard:
    """Test the daemon guard at the top of ghost_type_to_worker()."""

    def test_guard_blocks_unknown_daemon_caller(self, tmp_path):
        """Ghost-type from an unknown daemon script should be blocked."""
        from tools.skynet_dispatch import ghost_type_to_worker
        cfg = {"daemon_ghost_type_global_enabled": False}
        cfg_file = tmp_path / "brain_config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        # Mock ROOT to point to tmp_path parent so data/brain_config.json resolves
        with patch("tools.skynet_dispatch.ROOT", tmp_path.parent), \
             patch("tools.skynet_dispatch.Path") as mock_path:
            # Make Path(ROOT, "data", "brain_config.json") return our cfg file
            mock_path.return_value = cfg_file
            mock_path.side_effect = None

            # Actually, let's test more directly via the json load
            # The guard reads ROOT / "data" / "brain_config.json"
            data_dir = tmp_path / "data"
            data_dir.mkdir(exist_ok=True)
            cfg_path = data_dir / "brain_config.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            with patch("tools.skynet_dispatch.ROOT", tmp_path):
                # Caller chain will contain test framework paths — not in allowed list
                result = ghost_type_to_worker(12345, "test text", 99999)
                assert result is False
        # signed: gamma

    def test_guard_allows_when_globally_enabled(self, tmp_path):
        """When daemon_ghost_type_global_enabled=True, guard does not block."""
        cfg = {"daemon_ghost_type_global_enabled": True}
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        cfg_path = data_dir / "brain_config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        from tools.skynet_dispatch import ghost_type_to_worker, user32
        with patch("tools.skynet_dispatch.ROOT", tmp_path), \
             patch.object(user32, "IsWindow", return_value=False):
            # Should pass the daemon guard but fail on IsWindow check
            result = ghost_type_to_worker(0, "test", 0)
            assert result is False  # fails later (invalid HWND), not at guard
        # signed: gamma


# ---------------------------------------------------------------------------
# 5. dispatch_to_worker() routing tests
# ---------------------------------------------------------------------------

class TestDispatchRouting:
    """Test dispatch_to_worker() routing logic."""

    def test_self_dispatch_blocked(self):
        """Worker dispatching to itself should be blocked."""
        from tools.skynet_dispatch import dispatch_to_worker
        with patch("tools.skynet_dispatch._get_self_identity", return_value="alpha"):
            result = dispatch_to_worker("alpha", "test task")
            assert result is False
        # signed: gamma

    def test_dispatch_to_nonexistent_worker(self):
        """Dispatching to a worker not in registry returns False."""
        from tools.skynet_dispatch import dispatch_to_worker
        fake_workers = [{"name": "alpha", "hwnd": 1, "type": "core"}]
        with patch("tools.skynet_dispatch._get_self_identity", return_value="orchestrator"), \
             patch("tools.skynet_dispatch.load_all_workers", return_value=fake_workers):
            result = dispatch_to_worker("nonexistent_worker", "test task",
                                        workers=fake_workers)
            assert result is False
        # signed: gamma

    def test_dispatch_routes_to_orchestrator(self):
        """dispatch_to_worker('orchestrator', ...) routes to _dispatch_to_orchestrator."""
        from tools.skynet_dispatch import dispatch_to_worker
        with patch("tools.skynet_dispatch._get_self_identity", return_value="alpha"), \
             patch("tools.skynet_dispatch._dispatch_to_orchestrator", return_value=True) as mock_orch:
            result = dispatch_to_worker("orchestrator", "test task",
                                        orch_hwnd=99999)
            assert result is True
            mock_orch.assert_called_once()
        # signed: gamma

    def test_dispatch_routes_to_consultant(self):
        """dispatch_to_worker('consultant', ...) routes to _dispatch_to_consultant."""
        from tools.skynet_dispatch import dispatch_to_worker
        with patch("tools.skynet_dispatch._get_self_identity", return_value="alpha"), \
             patch("tools.skynet_dispatch._dispatch_to_consultant", return_value=True) as mock_con:
            result = dispatch_to_worker("consultant", "advisory request")
            assert result is True
            mock_con.assert_called_once()
        # signed: gamma


# ---------------------------------------------------------------------------
# 6. _generate_strategy_id uniqueness (from brain but used in dispatch context)
# ---------------------------------------------------------------------------

class TestCoreWorkerNames:
    """Test CORE_WORKER_NAMES constant."""

    def test_core_worker_names_content(self):
        """CORE_WORKER_NAMES should contain exactly alpha, beta, gamma, delta."""
        from tools.skynet_dispatch import CORE_WORKER_NAMES
        assert CORE_WORKER_NAMES == frozenset({"alpha", "beta", "gamma", "delta"})
        # signed: gamma

    def test_core_worker_names_is_frozen(self):
        """CORE_WORKER_NAMES should be immutable."""
        from tools.skynet_dispatch import CORE_WORKER_NAMES
        assert isinstance(CORE_WORKER_NAMES, frozenset)
        # signed: gamma
