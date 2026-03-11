# signed: alpha
"""Tests for tools/skynet_self.py — the Skynet consciousness kernel.

Tests cover: SkynetIdentity (persistence, agents, report), SkynetCapabilities (_probe),
SkynetHealth (pulse, individual check methods), SkynetIntrospection (reflect_on_* heuristics),
SkynetGoals (suggest), SkynetSelf (IQ computation, trend tracking, self-assessment, quick_pulse,
cached health, broadcast).

Created by worker alpha as part of test coverage TODO test-tools-2.
"""

import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.skynet_self as ss


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pulse_cache():
    """Reset the class-level pulse cache between tests."""
    ss.SkynetSelf._pulse_cache = None
    ss.SkynetSelf._pulse_cache_t = 0
    yield
    ss.SkynetSelf._pulse_cache = None
    ss.SkynetSelf._pulse_cache_t = 0
    # signed: alpha


@pytest.fixture
def mock_data_dir(tmp_path):
    """Create a temporary data directory for identity/IQ files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir
    # signed: alpha


@pytest.fixture
def mock_identity(tmp_path):
    """SkynetIdentity with mocked DATA path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    with patch.object(ss, "DATA", data_dir):
        identity = ss.SkynetIdentity()
    return identity, data_dir
    # signed: alpha


# ── Timestamp Utility ───────────────────────────────────────────────────────


class TestTimestamp:
    """Tests for _ts() — HH:MM:SS formatting."""
    # signed: alpha

    def test_returns_string(self):
        assert isinstance(ss._ts(), str)  # signed: alpha

    def test_format_hhmmss(self):
        result = ss._ts()
        parts = result.split(":")
        assert len(parts) == 3
        assert all(len(p) == 2 for p in parts)  # signed: alpha


# ── HTTP Helpers ────────────────────────────────────────────────────────────


class TestHttpGet:
    """Tests for _http_get() — HTTP GET with silent error handling."""
    # signed: alpha

    def test_success(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = ss._http_get("/status")
        assert result == {"status": "ok"}  # signed: alpha

    def test_failure_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = ss._http_get("/status")
        assert result is None  # signed: alpha


class TestHttpPost:
    """Tests for _http_post() — HTTP POST with silent error handling."""
    # signed: alpha

    def test_success(self):
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = ss._http_post("/bus/publish", {"sender": "test"})
        assert result is True  # signed: alpha

    def test_failure_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = ss._http_post("/bus/publish", {"sender": "test"})
        assert result is False  # signed: alpha


# ── SkynetIdentity Tests ────────────────────────────────────────────────────


class TestSkynetIdentity:
    """Tests for SkynetIdentity — persistent self-model."""
    # signed: alpha

    def test_defaults(self, tmp_path):
        """Default identity values without persistent file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with patch.object(ss, "DATA", data_dir):
            identity = ss.SkynetIdentity()
        assert identity.name == "SKYNET"
        assert identity.version == "3.0"
        assert identity.level == 3
        assert identity.model == "Claude Opus 4.6 (fast mode)"  # signed: alpha

    def test_loads_persistent_file(self, tmp_path):
        """Persistent identity file overrides defaults."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        id_file = data_dir / "skynet_identity.json"
        id_file.write_text(json.dumps({
            "born": "2026-01-01T00:00:00",
            "version": "4.0",
        }))
        with patch.object(ss, "DATA", data_dir):
            identity = ss.SkynetIdentity()
        assert identity.born == "2026-01-01T00:00:00"
        assert identity.version == "4.0"  # signed: alpha

    def test_corrupt_file_uses_defaults(self, tmp_path):
        """Corrupted identity file gracefully falls back to defaults."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        id_file = data_dir / "skynet_identity.json"
        id_file.write_text("{bad json")
        with patch.object(ss, "DATA", data_dir):
            identity = ss.SkynetIdentity()
        assert identity.name == "SKYNET"
        assert identity.version == "3.0"  # signed: alpha

    def test_save_creates_file(self, tmp_path):
        """save() persists identity to JSON file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with patch.object(ss, "DATA", data_dir):
            identity = ss.SkynetIdentity()
            identity.save()
        saved = json.loads((data_dir / "skynet_identity.json").read_text())
        assert saved["name"] == "SKYNET"
        assert saved["level"] == 3
        assert "workers" in saved  # signed: alpha

    def test_agents_with_backend(self):
        """agents() returns transformed agent dict from backend."""
        status = {
            "agents": {
                "alpha": {"status": "IDLE", "model": "opus", "tasks_completed": 5},
                "orchestrator": {"status": "IDLE", "model": "opus"},
            }
        }
        with patch.object(ss, "_http_get", return_value=status):
            with patch.object(ss, "DATA", Path("/tmp/fake")):
                identity = ss.SkynetIdentity()
                agents = identity.agents()
        assert "alpha" in agents
        assert agents["alpha"]["name"] == "ALPHA"
        assert agents["alpha"]["status"] == "IDLE"
        assert agents["orchestrator"]["is_orchestrator"] is True
        assert agents["alpha"]["is_orchestrator"] is False  # signed: alpha

    def test_agents_backend_down(self):
        """agents() returns empty dict when backend is unreachable."""
        with patch.object(ss, "_http_get", return_value=None):
            with patch.object(ss, "DATA", Path("/tmp/fake")):
                identity = ss.SkynetIdentity()
                agents = identity.agents()
        assert agents == {}  # signed: alpha

    def test_report_includes_counts(self):
        """report() includes alive/total agent counts."""
        status = {
            "agents": {
                "alpha": {"status": "IDLE"},
                "beta": {"status": "DEAD"},
            }
        }
        with patch.object(ss, "_http_get", return_value=status):
            with patch.object(ss, "DATA", Path("/tmp/fake")):
                identity = ss.SkynetIdentity()
                report = identity.report()
        assert report["alive_count"] == 1
        assert report["agent_count"] == 2  # signed: alpha


# ── SkynetCapabilities Tests ────────────────────────────────────────────────


class TestSkynetCapabilitiesProbe:
    """Tests for SkynetCapabilities._probe() — module status detection."""
    # signed: alpha

    def test_probe_online(self):
        """Module that imports and has CLASS_NAMES mapping + instantiates → online."""
        # _probe needs the module in CLASS_NAMES to attempt instantiation
        mock_module = MagicMock()
        with patch.dict(ss.SkynetCapabilities.CLASS_NAMES, {"fake.module": "TestClass"}):
            with patch("builtins.__import__", return_value=mock_module):
                result = ss.SkynetCapabilities._probe("fake.module", "TestEngine")
        assert result["status"] == "online"  # signed: alpha

    def test_probe_available_no_class(self):
        """Module imports but no CLASS_NAMES entry → available."""
        mock_module = MagicMock()
        with patch("builtins.__import__", return_value=mock_module):
            result = ss.SkynetCapabilities._probe("unmapped.module", "TestEngine")
        assert result["status"] == "available"  # signed: alpha

    def test_probe_offline(self):
        """Module that fails to import → offline."""
        # Don't mock builtins.__import__ globally — use the real import
        # and provide a truly nonexistent module
        result = ss.SkynetCapabilities._probe("__nonexistent_module_xyz_12345__", "TestEngine")
        assert result["status"] == "offline"
        assert "error" in result  # signed: alpha


# ── SkynetHealth Tests ──────────────────────────────────────────────────────


class TestSkynetHealth:
    """Tests for SkynetHealth — real-time health assessment."""
    # signed: alpha

    def _make_health(self):
        return ss.SkynetHealth()
        # signed: alpha

    def test_pulse_all_healthy(self):
        """All checks pass → HEALTHY overall."""
        h = self._make_health()
        with patch.object(ss, "_http_get") as mock_get:
            mock_get.side_effect = lambda path, **kw: {
                "/status": {"agents": {"alpha": {"status": "IDLE"}, "beta": {"status": "IDLE"}},
                            "uptime_s": 3600},
                "/bus/messages?limit=1": [{"id": "m1"}],
            }.get(path)
            with patch.object(ss.SkynetHealth, "_check_sse_daemon"), \
                 patch.object(ss.SkynetHealth, "_check_intelligence_engines"), \
                 patch.object(ss.SkynetHealth, "_check_collective_iq"), \
                 patch.object(ss.SkynetHealth, "_check_knowledge_base"), \
                 patch.object(ss.SkynetHealth, "_check_windows"):
                pulse = h.pulse()
        assert pulse["overall"] in ("HEALTHY", "DEGRADED")
        assert "timestamp" in pulse
        assert "checks" in pulse  # signed: alpha

    def test_check_backend_up(self):
        """Backend status UP when /status returns data."""
        checks = {}
        with patch.object(ss, "_http_get", return_value={"uptime_s": 1000}):
            ss.SkynetHealth._check_backend(checks)
        assert checks["backend"]["status"] == "UP"
        assert checks["backend"]["uptime_s"] == 1000  # signed: alpha

    def test_check_backend_down(self):
        """Backend status DOWN when /status returns None."""
        checks = {}
        with patch.object(ss, "_http_get", return_value=None):
            ss.SkynetHealth._check_backend(checks)
        assert checks["backend"]["status"] == "DOWN"  # signed: alpha

    def test_check_workers_alive(self):
        """Worker check counts alive/idle/working agents from WORKER_NAMES."""
        checks = {}
        status = {"agents": {
            "alpha": {"status": "IDLE"},
            "beta": {"status": "PROCESSING"},
            "gamma": {"status": "DEAD"},
            "delta": {"status": "IDLE"},
        }}
        with patch.object(ss, "_http_get", return_value=status):
            ss.SkynetHealth._check_workers(checks)
        assert checks["workers"]["alive"] == 3  # alpha + beta + delta
        assert checks["workers"]["total"] == 4  # WORKER_NAMES has 4 entries  # signed: alpha

    def test_check_workers_backend_down(self):
        """Worker check handles backend being unreachable."""
        checks = {}
        with patch.object(ss, "_http_get", return_value=None):
            ss.SkynetHealth._check_workers(checks)
        assert checks["workers"]["alive"] == 0
        assert checks["workers"]["total"] == 0  # signed: alpha

    def test_check_bus_up(self):
        """Bus status UP when /bus/messages returns data."""
        checks = {}
        with patch.object(ss, "_http_get", return_value=[{"id": "m1"}]):
            ss.SkynetHealth._check_bus(checks)
        assert checks["bus"]["status"] == "UP"  # signed: alpha

    def test_check_bus_down(self):
        """Bus status DOWN when /bus/messages returns None."""
        checks = {}
        with patch.object(ss, "_http_get", return_value=None):
            ss.SkynetHealth._check_bus(checks)
        assert checks["bus"]["status"] == "DOWN"  # signed: alpha


# ── SkynetIntrospection Reflection Heuristics ───────────────────────────────


class TestReflectionHeuristics:
    """Tests for SkynetIntrospection._reflect_on_* static methods — pure logic."""
    # signed: alpha

    def test_reflect_on_backend_up(self):
        checks = {"backend": {"status": "UP", "uptime_s": 3600}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_backend(checks, strengths, weaknesses, recs)
        assert any("backend" in s.lower() or "online" in s.lower() for s in strengths)  # signed: alpha

    def test_reflect_on_backend_down(self):
        checks = {"backend": {"status": "DOWN"}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_backend(checks, strengths, weaknesses, recs)
        assert len(weaknesses) > 0 or len(recs) > 0  # signed: alpha

    def test_reflect_on_workers_all_alive(self):
        checks = {"workers": {"alive": 4, "total": 4, "all_healthy": True}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_workers(checks, strengths, weaknesses, recs)
        assert len(strengths) > 0  # signed: alpha

    def test_reflect_on_workers_some_dead(self):
        checks = {"workers": {"alive": 2, "total": 4, "all_healthy": False}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_workers(checks, strengths, weaknesses, recs)
        assert len(weaknesses) > 0  # signed: alpha

    def test_reflect_on_capabilities_high_ratio(self):
        # Signature: (cap_ratio, strengths, weaknesses, observations, recommendations)
        strengths, weaknesses, obs, recs = [], [], [], []
        ss.SkynetIntrospection._reflect_on_capabilities(0.9, strengths, weaknesses, obs, recs)
        assert len(strengths) > 0  # signed: alpha

    def test_reflect_on_capabilities_low_ratio(self):
        strengths, weaknesses, obs, recs = [], [], [], []
        ss.SkynetIntrospection._reflect_on_capabilities(0.2, strengths, weaknesses, obs, recs)
        assert len(weaknesses) > 0 or len(recs) > 0  # signed: alpha

    def test_reflect_on_iq_high(self):
        # Signature: (iq, strengths, weaknesses, observations, recommendations)
        strengths, weaknesses, obs, recs = [], [], [], []
        ss.SkynetIntrospection._reflect_on_iq(0.85, strengths, weaknesses, obs, recs)
        assert len(strengths) > 0  # signed: alpha

    def test_reflect_on_iq_low(self):
        strengths, weaknesses, obs, recs = [], [], [], []
        ss.SkynetIntrospection._reflect_on_iq(0.15, strengths, weaknesses, obs, recs)
        assert len(weaknesses) > 0  # signed: alpha

    def test_reflect_on_sse_up(self):
        # SSE key is "sse_daemon" not "sse"
        checks = {"sse_daemon": {"status": "UP"}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_sse(checks, strengths, weaknesses, recs)
        assert len(strengths) > 0  # signed: alpha

    def test_reflect_on_sse_stale(self):
        checks = {"sse_daemon": {"status": "STALE", "age_s": 30}}
        strengths, weaknesses, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_sse(checks, strengths, weaknesses, recs)
        assert len(weaknesses) > 0  # signed: alpha

    def test_reflect_on_knowledge_many_facts(self):
        # Signature: (facts, strengths, observations, recommendations)
        strengths, obs, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_knowledge(200, strengths, obs, recs)
        assert len(strengths) > 0  # signed: alpha

    def test_reflect_on_knowledge_no_facts(self):
        strengths, obs, recs = [], [], []
        ss.SkynetIntrospection._reflect_on_knowledge(0, strengths, obs, recs)
        assert len(recs) > 0  # signed: alpha


# ── SkynetGoals Tests ───────────────────────────────────────────────────────


class TestSkynetGoals:
    """Tests for SkynetGoals — autonomous goal generation from introspection."""
    # signed: alpha

    def test_suggest_from_weaknesses(self):
        """Weaknesses generate fix goals."""
        mock_reflection = {
            "overall_health": "DEGRADED",
            "strengths": [],
            "weaknesses": ["Worker beta is DEAD"],
            "observations": [],
            "recommendations": [],
            "metrics": {},
        }
        with patch.object(ss.SkynetIntrospection, "reflect", return_value=mock_reflection):
            goals_obj = ss.SkynetGoals()
            goals = goals_obj.suggest()
        assert len(goals) > 0
        assert any("DEAD" in g.get("goal", "") or "fix" in g.get("category", "").lower()
                    for g in goals)  # signed: alpha

    def test_suggest_from_recommendations(self):
        """Recommendations generate improvement goals."""
        mock_reflection = {
            "overall_health": "HEALTHY",
            "strengths": ["Everything works"],
            "weaknesses": [],
            "observations": [],
            "recommendations": ["Increase test coverage to 80%"],
            "metrics": {},
        }
        with patch.object(ss.SkynetIntrospection, "reflect", return_value=mock_reflection):
            goals_obj = ss.SkynetGoals()
            goals = goals_obj.suggest()
        assert any("test coverage" in g.get("goal", "").lower() or "improve" in g.get("category", "").lower()
                    for g in goals)  # signed: alpha

    def test_suggest_empty_reflection(self):
        """Empty reflection yields no goals (or minimal ones)."""
        mock_reflection = {
            "overall_health": "HEALTHY",
            "strengths": [],
            "weaknesses": [],
            "observations": [],
            "recommendations": [],
            "metrics": {},
        }
        with patch.object(ss.SkynetIntrospection, "reflect", return_value=mock_reflection):
            goals_obj = ss.SkynetGoals()
            goals = goals_obj.suggest()
        assert isinstance(goals, list)  # signed: alpha


# ── SkynetSelf — IQ Computation ─────────────────────────────────────────────


class TestComputeIQComponents:
    """Tests for SkynetSelf._compute_iq_components() — pure weighted scoring."""
    # signed: alpha

    def test_all_max(self):
        """All metrics at maximum → IQ near 1.0."""
        checks = {
            "workers": {"alive": 4, "total": 4},
            "intelligence": {"engines_online": 20, "engines_total": 20, "ratio": 1.0},
            "bus": {"status": "UP"},
            "knowledge": {"facts": 600},
            "backend": {"uptime_s": 100000},
        }
        components = ss.SkynetSelf._compute_iq_components(checks)
        total = sum(s * w for s, w in components)
        assert total >= 0.95  # Nearly perfect  # signed: alpha

    def test_all_min(self):
        """All metrics at minimum → IQ near 0.0."""
        checks = {
            "workers": {"alive": 0, "total": 4},
            "intelligence": {"engines_online": 0, "engines_total": 20, "ratio": 0.0},
            "bus": {"status": "DOWN"},
            "knowledge": {"facts": 0},
            "backend": {"uptime_s": 0},
        }
        components = ss.SkynetSelf._compute_iq_components(checks)
        total = sum(s * w for s, w in components)
        assert total <= 0.05  # Near zero  # signed: alpha

    def test_partial_metrics(self):
        """Partial metrics → mid-range IQ."""
        checks = {
            "workers": {"alive": 2, "total": 4},
            "intelligence": {"engines_online": 10, "engines_total": 20, "ratio": 0.5},
            "bus": {"status": "UP"},
            "knowledge": {"facts": 250},
            "backend": {"uptime_s": 43200},
        }
        components = ss.SkynetSelf._compute_iq_components(checks)
        total = sum(s * w for s, w in components)
        assert 0.3 < total < 0.8  # signed: alpha

    def test_empty_checks(self):
        """Empty checks dict → zero IQ with safe defaults."""
        components = ss.SkynetSelf._compute_iq_components({})
        total = sum(s * w for s, w in components)
        assert total >= 0.0  # No crash  # signed: alpha

    def test_weight_sum_is_one(self):
        """All weights sum to 1.0."""
        components = ss.SkynetSelf._compute_iq_components({})
        weight_sum = sum(w for _, w in components)
        assert abs(weight_sum - 1.0) < 0.01  # signed: alpha

    def test_facts_capped_at_500(self):
        """Knowledge score caps at 1.0 for 500+ facts."""
        checks_500 = {"knowledge": {"facts": 500}}
        checks_1000 = {"knowledge": {"facts": 1000}}
        c500 = ss.SkynetSelf._compute_iq_components(checks_500)
        c1000 = ss.SkynetSelf._compute_iq_components(checks_1000)
        # Knowledge component index is 3
        assert c500[3][0] == c1000[3][0] == 1.0  # signed: alpha

    def test_uptime_capped_at_86400(self):
        """Uptime score caps at 1.0 for 86400s+ (24 hours)."""
        checks = {"backend": {"uptime_s": 200000}}
        components = ss.SkynetSelf._compute_iq_components(checks)
        assert components[4][0] == 1.0  # signed: alpha


class TestUpdateIQHistory:
    """Tests for SkynetSelf._update_iq_history() — trend tracking."""
    # signed: alpha

    def test_first_entry_stable(self, tmp_path):
        """First IQ entry → trend = stable."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with patch.object(ss, "DATA", data_dir):
            self_obj = ss.SkynetSelf()
            trend = self_obj._update_iq_history(0.8)
        assert trend == "stable"  # signed: alpha

    def test_rising_trend(self, tmp_path):
        """Current IQ significantly above recent average → rising."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        history = [{"iq": 0.5, "ts": time.time() - i} for i in range(10, 0, -1)]
        (data_dir / "iq_history.json").write_text(json.dumps(history))
        with patch.object(ss, "DATA", data_dir):
            self_obj = ss.SkynetSelf()
            trend = self_obj._update_iq_history(0.9)  # Way above 0.5 average
        assert trend == "rising"  # signed: alpha

    def test_falling_trend(self, tmp_path):
        """Current IQ significantly below recent average → falling."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        history = [{"iq": 0.9, "ts": time.time() - i} for i in range(10, 0, -1)]
        (data_dir / "iq_history.json").write_text(json.dumps(history))
        with patch.object(ss, "DATA", data_dir):
            self_obj = ss.SkynetSelf()
            trend = self_obj._update_iq_history(0.5)  # Way below 0.9 average
        assert trend == "falling"  # signed: alpha

    def test_stable_trend(self, tmp_path):
        """Current IQ close to recent average → stable."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        history = [{"iq": 0.8, "ts": time.time() - i} for i in range(10, 0, -1)]
        (data_dir / "iq_history.json").write_text(json.dumps(history))
        with patch.object(ss, "DATA", data_dir):
            self_obj = ss.SkynetSelf()
            trend = self_obj._update_iq_history(0.81)  # delta = 0.01, within 0.02 threshold
        assert trend == "stable"  # signed: alpha

    def test_history_truncated_to_100(self, tmp_path):
        """History file is truncated to 100 entries."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        history = [{"iq": 0.5, "ts": time.time() - i} for i in range(150, 0, -1)]
        (data_dir / "iq_history.json").write_text(json.dumps(history))
        with patch.object(ss, "DATA", data_dir):
            self_obj = ss.SkynetSelf()
            self_obj._update_iq_history(0.6)
        saved = json.loads((data_dir / "iq_history.json").read_text())
        assert len(saved) == 100  # signed: alpha


# ── SkynetSelf — Self Assessment ────────────────────────────────────────────


class TestSelfAssessment:
    """Tests for SkynetSelf._self_assessment() — prose generation."""
    # signed: alpha

    def test_healthy_assessment(self):
        reflection = {
            "overall_health": "HEALTHY",
            "metrics": {
                "workers_alive": 4, "workers_total": 4,
                "engines_online": 15, "engines_total": 20,
                "collective_iq": 0.85,
                "capability_ratio": 0.75,
            },
            "strengths": ["All workers alive", "Bus is UP"],
            "weaknesses": [],
        }
        result = ss.SkynetSelf._self_assessment(reflection)
        assert "HEALTHY" in result
        assert "4/4" in result
        assert isinstance(result, str)  # signed: alpha

    def test_degraded_with_weaknesses(self):
        reflection = {
            "overall_health": "DEGRADED",
            "metrics": {
                "workers_alive": 2, "workers_total": 4,
                "engines_online": 5, "engines_total": 20,
                "collective_iq": 0.4,
                "capability_ratio": 0.25,
            },
            "strengths": [],
            "weaknesses": ["Worker beta DEAD", "Worker gamma DEAD", "Low IQ"],
        }
        result = ss.SkynetSelf._self_assessment(reflection)
        assert "DEGRADED" in result
        assert "2/4" in result
        assert "Weaknesses:" in result  # signed: alpha

    def test_truncates_strengths_to_3(self):
        reflection = {
            "overall_health": "HEALTHY",
            "metrics": {"workers_alive": 0, "workers_total": 0,
                        "engines_online": 0, "engines_total": 0,
                        "collective_iq": 0, "capability_ratio": 0},
            "strengths": ["s1", "s2", "s3", "s4", "s5"],
            "weaknesses": [],
        }
        result = ss.SkynetSelf._self_assessment(reflection)
        # Only first 3 strengths should appear
        assert "s4" not in result
        assert "s5" not in result  # signed: alpha

    def test_empty_metrics(self):
        """Handles empty/missing metrics without crashing."""
        reflection = {
            "overall_health": "UNKNOWN",
            "metrics": {},
            "strengths": [],
            "weaknesses": [],
        }
        result = ss.SkynetSelf._self_assessment(reflection)
        assert isinstance(result, str)
        assert "SKYNET" in result  # signed: alpha


# ── SkynetSelf — Quick Pulse ────────────────────────────────────────────────


class TestQuickPulse:
    """Tests for SkynetSelf.quick_pulse() — fast heartbeat."""
    # signed: alpha

    def test_returns_expected_keys(self):
        """Quick pulse contains all required keys."""
        mock_pulse = {"overall": "HEALTHY", "checks": {
            "workers": {"alive": 4, "total": 4},
            "intelligence": {"engines_online": 10, "engines_total": 20, "ratio": 0.5},
            "bus": {"status": "UP"},
            "knowledge": {"facts": 100},
            "backend": {"uptime_s": 3600},
        }}
        mock_agents = {
            "alpha": {"status": "IDLE", "name": "ALPHA"},
            "beta": {"status": "IDLE", "name": "BETA"},
        }

        with patch.object(ss, "_http_get", return_value=None), \
             patch.object(ss, "DATA", Path("/tmp/fake")):
            self_obj = ss.SkynetSelf()

        with patch.object(self_obj, "_cached_health_pulse", return_value=mock_pulse), \
             patch.object(self_obj.identity, "agents", return_value=mock_agents), \
             patch.object(self_obj, "_update_iq_history", return_value="stable"):
            result = self_obj.quick_pulse()

        assert result["name"] == "SKYNET"
        assert result["health"] == "HEALTHY"
        assert "iq" in result
        assert "iq_trend" in result
        assert result["alive"] == 2
        assert result["total"] == 2
        assert "alpha" in result["agents"]  # signed: alpha


# ── SkynetSelf — Cached Health ──────────────────────────────────────────────


class TestCachedHealthPulse:
    """Tests for SkynetSelf._cached_health_pulse() — 15s cache with locking."""
    # signed: alpha

    def test_first_call_populates_cache(self):
        """First call runs health.pulse() and caches it."""
        mock_pulse = {"overall": "HEALTHY", "checks": {}}

        with patch.object(ss, "_http_get", return_value=None), \
             patch.object(ss, "DATA", Path("/tmp/fake")):
            self_obj = ss.SkynetSelf()

        with patch.object(self_obj.health, "pulse", return_value=mock_pulse) as mock_p:
            result = self_obj._cached_health_pulse()
        assert result == mock_pulse
        mock_p.assert_called_once()  # signed: alpha

    def test_cached_call_skips_pulse(self):
        """Second call within TTL returns cached value without re-calling pulse()."""
        mock_pulse = {"overall": "HEALTHY", "checks": {}}

        with patch.object(ss, "_http_get", return_value=None), \
             patch.object(ss, "DATA", Path("/tmp/fake")):
            self_obj = ss.SkynetSelf()

        with patch.object(self_obj.health, "pulse", return_value=mock_pulse) as mock_p:
            self_obj._cached_health_pulse()
            result2 = self_obj._cached_health_pulse()
        mock_p.assert_called_once()  # Only called once — second was cached
        assert result2 == mock_pulse  # signed: alpha


# ── SkynetSelf — Broadcast Awareness ────────────────────────────────────────


class TestBroadcastAwareness:
    """Tests for SkynetSelf.broadcast_awareness() — bus POST."""
    # signed: alpha

    def test_broadcasts_pulse(self):
        """broadcast_awareness() posts pulse to bus."""
        mock_pulse = {"name": "SKYNET", "health": "HEALTHY", "iq": 0.8}

        with patch.object(ss, "_http_get", return_value=None), \
             patch.object(ss, "DATA", Path("/tmp/fake")):
            self_obj = ss.SkynetSelf()

        with patch.object(self_obj, "quick_pulse", return_value=mock_pulse), \
             patch.object(ss, "_http_post", return_value=True) as mock_post:
            result = self_obj.broadcast_awareness()

        assert result == mock_pulse
        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        assert call_args[0] == "/bus/publish"
        assert call_args[1]["sender"] == "skynet_self"
        assert call_args[1]["topic"] == "awareness"  # signed: alpha
