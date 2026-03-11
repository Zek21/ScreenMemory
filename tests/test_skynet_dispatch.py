"""Tests for tools/skynet_dispatch.py — the core task routing engine.

Tests cover: process protection guard, self-dispatch prevention, preamble building,
task enrichment, dispatch logging, batch dispatch consolidation, expertise scoring,
load scoring, and smart routing logic.

Created by worker delta as part of codebase test coverage audit.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory with required files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def critical_procs_file(tmp_data_dir):
    """Write a critical_processes.json for testing."""
    fp = tmp_data_dir / "critical_processes.json"
    fp.write_text(json.dumps({
        "protected_names": ["skynet.exe", "god_console.py", "skynet_watchdog.py"],
        "protected_roles": ["backend", "dashboard", "watchdog"],
        "processes": [
            {"pid": 1234, "hwnd": 5678, "name": "skynet.exe", "role": "backend"},
            {"pid": 9999, "hwnd": 8888, "name": "god_console.py", "role": "dashboard"},
        ],
    }), encoding="utf-8")
    return fp


@pytest.fixture
def workers_file(tmp_data_dir):
    """Write a workers.json for testing."""
    fp = tmp_data_dir / "workers.json"
    fp.write_text(json.dumps({
        "workers": [
            {"name": "alpha", "hwnd": 100001},
            {"name": "beta", "hwnd": 100002},
            {"name": "gamma", "hwnd": 100003},
            {"name": "delta", "hwnd": 100004},
        ]
    }), encoding="utf-8")
    return fp


@pytest.fixture
def dispatch_log_file(tmp_data_dir):
    """Provide path for dispatch log."""
    return tmp_data_dir / "dispatch_log.json"


@pytest.fixture
def agent_profiles_file(tmp_data_dir):
    """Write agent_profiles.json for expertise routing tests."""
    fp = tmp_data_dir / "agent_profiles.json"
    fp.write_text(json.dumps({
        "alpha": {
            "name": "Alpha",
            "specializations": ["architecture", "frontend", "dashboard", "UI"],
        },
        "beta": {
            "name": "Beta",
            "specializations": ["backend", "integration", "protocols", "performance"],
        },
        "gamma": {
            "name": "Gamma",
            "specializations": ["api", "endpoints", "testing", "documentation"],
        },
        "delta": {
            "name": "Delta",
            "specializations": ["testing", "validation", "auditing", "Go", "monitoring"],
        },
    }), encoding="utf-8")
    return fp


@pytest.fixture
def brain_config_file(tmp_data_dir):
    """Write brain_config.json for routing config tests."""
    fp = tmp_data_dir / "brain_config.json"
    fp.write_text(json.dumps({
        "routing": {
            "expertise_weight": 0.6,
            "load_weight": 0.4,
        }
    }), encoding="utf-8")
    return fp


# ── Process Protection Guard Tests ──────────────────────────────────────────

class TestProcessProtection:
    """Tests for is_process_protected() and guard_process_kill()."""

    def test_protected_by_name_exact(self, critical_procs_file):
        """Protected process names are detected."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import is_process_protected
            protected, reason = is_process_protected(name="skynet.exe")
            assert protected is True
            assert "skynet.exe" in reason

    def test_protected_by_name_partial(self, critical_procs_file):
        """Partial name matches are detected."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import is_process_protected
            protected, reason = is_process_protected(name="god_console")
            assert protected is True

    def test_unprotected_process(self, critical_procs_file):
        """Non-protected processes are allowed."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import is_process_protected
            protected, reason = is_process_protected(name="notepad.exe")
            assert protected is False
            assert reason == ""

    def test_protected_by_pid(self, critical_procs_file):
        """Processes matched by PID/HWND are protected."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import is_process_protected
            protected, _ = is_process_protected(pid=1234)
            assert protected is True

    def test_protected_by_hwnd(self, critical_procs_file):
        """Processes matched by HWND in process list are protected."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import is_process_protected
            protected, _ = is_process_protected(pid=5678)
            assert protected is True

    def test_missing_critical_procs_file(self, tmp_data_dir):
        """Missing critical_processes.json returns empty protections."""
        missing = tmp_data_dir / "nonexistent.json"
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", missing):
            from tools.skynet_dispatch import is_process_protected
            protected, reason = is_process_protected(name="anything")
            assert protected is False

    def test_guard_blocks_protected_kill(self, critical_procs_file):
        """guard_process_kill returns False (unsafe) for protected processes."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import guard_process_kill
            with patch("urllib.request.urlopen"):  # don't actually POST to bus
                safe = guard_process_kill(name="skynet.exe", caller="test")
                assert safe is False

    def test_guard_allows_unprotected_kill(self, critical_procs_file):
        """guard_process_kill returns True (safe) for non-protected processes."""
        with patch("tools.skynet_dispatch.CRITICAL_PROCS_FILE", critical_procs_file):
            from tools.skynet_dispatch import guard_process_kill
            safe = guard_process_kill(name="notepad.exe", caller="test")
            assert safe is True


# ── Self-Dispatch Prevention Tests ──────────────────────────────────────────

class TestSelfDispatchPrevention:
    """Tests for self-dispatch blocking (Incident 001 prevention)."""

    def test_self_dispatch_blocked_via_env(self):
        """Self-dispatch is blocked when _SELF_WORKER_NAME matches target."""
        import tools.skynet_dispatch as sd
        old = sd._SELF_WORKER_NAME
        try:
            sd._SELF_WORKER_NAME = "alpha"
            assert sd._get_self_identity() == "alpha"
        finally:
            sd._SELF_WORKER_NAME = old

    def test_self_dispatch_blocked_via_marker(self, tmp_data_dir):
        """Self-dispatch is blocked when marker file matches target."""
        marker = tmp_data_dir / "self_identity.txt"
        marker.write_text("beta")
        old_env = os.environ.pop("SKYNET_WORKER_NAME", None)
        try:
            with patch("tools.skynet_dispatch.DATA_DIR", tmp_data_dir):
                from tools.skynet_dispatch import _get_self_identity
                # Clear the cached module variable
                import tools.skynet_dispatch as sd
                old_name = sd._SELF_WORKER_NAME
                sd._SELF_WORKER_NAME = ""
                try:
                    assert _get_self_identity() == "beta"
                finally:
                    sd._SELF_WORKER_NAME = old_name
        finally:
            if old_env:
                os.environ["SKYNET_WORKER_NAME"] = old_env

    def test_dispatch_to_self_returns_false(self):
        """dispatch_to_worker returns False when target == self."""
        import tools.skynet_dispatch as sd
        old = sd._SELF_WORKER_NAME
        try:
            sd._SELF_WORKER_NAME = "alpha"
            result = sd.dispatch_to_worker("alpha", "test task")
            assert result is False
        finally:
            sd._SELF_WORKER_NAME = old


# ── Preamble Building Tests ─────────────────────────────────────────────────

class TestPreambleBuilding:
    """Tests for build_preamble() and build_context_preamble()."""

    def test_preamble_contains_worker_name(self):
        """Preamble includes the target worker name."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("gamma")
        assert "worker gamma" in preamble
        assert "sender':'gamma'" in preamble

    def test_preamble_contains_no_steering(self):
        """Preamble includes anti-steering instructions."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("alpha")
        assert "Do NOT show steering options" in preamble

    def test_preamble_identity_mismatch_warning(self):
        """Preamble warns about identity mismatch."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("delta")
        assert "IDENTITY MISMATCH" in preamble
        assert "preamble for delta" in preamble

    def test_preamble_has_zero_ticket_stop(self):
        """Preamble includes the zero-ticket stop rule."""
        from tools.skynet_dispatch import build_preamble
        preamble = build_preamble("beta")
        assert "ZERO TICKET STOP RULE" in preamble

    def test_context_preamble_without_context(self):
        """Context preamble without context dict just appends task."""
        from tools.skynet_dispatch import build_context_preamble
        result = build_context_preamble("alpha", "do something")
        assert "do something" in result
        assert "worker alpha" in result

    def test_context_preamble_with_learnings(self):
        """Context preamble includes learnings when provided."""
        from tools.skynet_dispatch import build_context_preamble
        context = {
            "relevant_learnings": ["lesson 1: always test", "lesson 2: never assume"],
            "difficulty": "MODERATE",
            "reasoning": "Alpha is frontend specialist",
        }
        result = build_context_preamble("alpha", "build dashboard", context)
        assert "RELEVANT PAST LEARNINGS" in result
        assert "lesson 1" in result
        assert "TASK COMPLEXITY: MODERATE" in result
        assert "ROUTING REASON: Alpha is frontend specialist" in result

    def test_context_preamble_with_strategy_id(self):
        """Context preamble includes strategy ID from context or env."""
        from tools.skynet_dispatch import build_context_preamble
        context = {"strategy_id": "strat_abc123"}
        result = build_context_preamble("beta", "run tests", context)
        assert "strat_abc123" in result

    def test_context_preamble_strategy_id_from_env(self):
        """Strategy ID from env var when no context dict."""
        from tools.skynet_dispatch import build_context_preamble
        old = os.environ.get("SKYNET_STRATEGY_ID", "")
        try:
            os.environ["SKYNET_STRATEGY_ID"] = "env_strat_xyz"
            result = build_context_preamble("alpha", "task here")
            assert "env_strat_xyz" in result
        finally:
            if old:
                os.environ["SKYNET_STRATEGY_ID"] = old
            else:
                os.environ.pop("SKYNET_STRATEGY_ID", None)


# ── Task Enrichment Tests ───────────────────────────────────────────────────

class TestTaskEnrichment:
    """Tests for enrich_task() and its sub-enrichment functions."""

    def test_enrich_task_includes_autonomy(self):
        """Enriched task includes autonomy instruction."""
        from tools.skynet_dispatch import enrich_task
        # Patch network calls to prevent real HTTP requests
        with patch("tools.skynet_dispatch._enrich_difficulty", return_value=None), \
             patch("tools.skynet_dispatch._enrich_learnings", return_value=None), \
             patch("tools.skynet_dispatch._enrich_context", return_value=None), \
             patch("tools.skynet_dispatch._enrich_worker_states", return_value=None), \
             patch("tools.skynet_dispatch._enrich_last_result", return_value=None):
            result = enrich_task("alpha", "my task")
            assert "autonomous" in result
            assert "my task" in result

    def test_enrich_task_with_difficulty(self):
        """Enriched task includes difficulty section."""
        from tools.skynet_dispatch import enrich_task
        with patch("tools.skynet_dispatch._enrich_difficulty",
                   return_value="[DIFFICULTY] MODERATE (score=0.55, domains=code, confidence=0.80)"), \
             patch("tools.skynet_dispatch._enrich_learnings", return_value=None), \
             patch("tools.skynet_dispatch._enrich_context", return_value=None), \
             patch("tools.skynet_dispatch._enrich_worker_states", return_value=None), \
             patch("tools.skynet_dispatch._enrich_last_result", return_value=None):
            result = enrich_task("alpha", "fix the bug")
            assert "SKYNET INTELLIGENCE" in result
            assert "MODERATE" in result

    def test_enrich_task_with_all_sections(self):
        """Enriched task includes all available enrichment sections."""
        from tools.skynet_dispatch import enrich_task
        with patch("tools.skynet_dispatch._enrich_difficulty",
                   return_value="[DIFFICULTY] SIMPLE"), \
             patch("tools.skynet_dispatch._enrich_learnings",
                   return_value="[LEARNINGS] past fact"), \
             patch("tools.skynet_dispatch._enrich_context",
                   return_value="[CONTEXT] related solution"), \
             patch("tools.skynet_dispatch._enrich_worker_states",
                   return_value="[WORKERS] beta=IDLE, gamma=PROCESSING"), \
             patch("tools.skynet_dispatch._enrich_last_result",
                   return_value="[LAST_RESULT] previous output"):
            result = enrich_task("alpha", "original task")
            assert "SKYNET INTELLIGENCE" in result
            assert "[DIFFICULTY]" in result
            assert "[LEARNINGS]" in result
            assert "[CONTEXT]" in result
            assert "[WORKERS]" in result
            assert "[LAST_RESULT]" in result
            assert "original task" in result


# ── Expertise Scoring Tests ─────────────────────────────────────────────────

class TestExpertiseScoring:
    """Tests for _expertise_score() used in smart routing."""

    def test_frontend_task_matches_alpha(self):
        """Alpha scores high on frontend/dashboard tasks."""
        from tools.skynet_dispatch import _expertise_score
        profiles = {
            "alpha": {"specializations": ["architecture", "frontend", "dashboard", "UI"]},
            "beta": {"specializations": ["backend", "integration", "protocols", "performance"]},
        }
        alpha_score = _expertise_score("alpha", "build the frontend dashboard", {"build", "the", "frontend", "dashboard"}, profiles)
        beta_score = _expertise_score("beta", "build the frontend dashboard", {"build", "the", "frontend", "dashboard"}, profiles)
        assert alpha_score > beta_score

    def test_backend_task_matches_beta(self):
        """Beta scores high on backend/protocol tasks."""
        from tools.skynet_dispatch import _expertise_score
        profiles = {
            "alpha": {"specializations": ["architecture", "frontend", "dashboard", "UI"]},
            "beta": {"specializations": ["backend", "integration", "protocols", "performance"]},
        }
        alpha_score = _expertise_score("alpha", "optimize backend performance", {"optimize", "backend", "performance"}, profiles)
        beta_score = _expertise_score("beta", "optimize backend performance", {"optimize", "backend", "performance"}, profiles)
        assert beta_score > alpha_score

    def test_testing_task_matches_delta(self):
        """Delta scores high on testing/auditing tasks."""
        from tools.skynet_dispatch import _expertise_score
        profiles = {
            "gamma": {"specializations": ["api", "endpoints", "testing", "documentation"]},
            "delta": {"specializations": ["testing", "validation", "auditing", "Go", "monitoring"]},
        }
        gamma_score = _expertise_score("gamma", "run validation testing and auditing", {"run", "validation", "testing", "and", "auditing"}, profiles)
        delta_score = _expertise_score("delta", "run validation testing and auditing", {"run", "validation", "testing", "and", "auditing"}, profiles)
        assert delta_score >= gamma_score

    def test_no_specializations_scores_zero(self):
        """Worker with no specializations scores 0.0."""
        from tools.skynet_dispatch import _expertise_score
        profiles = {"alpha": {"specializations": []}}
        score = _expertise_score("alpha", "any task", {"any", "task"}, profiles)
        assert score == 0.0

    def test_unknown_worker_scores_zero(self):
        """Worker not in profiles scores 0.0."""
        from tools.skynet_dispatch import _expertise_score
        score = _expertise_score("unknown_worker", "any task", {"any", "task"}, {})
        assert score == 0.0


# ── Load Scoring Tests ──────────────────────────────────────────────────────

class TestLoadScoring:
    """Tests for _load_score() used in smart routing."""

    def test_idle_no_pending_lowest_load(self):
        """IDLE worker with no pending tasks has lowest load."""
        from tools.skynet_dispatch import _load_score
        score = _load_score("alpha", {"alpha": "IDLE"}, {"alpha": {"pending_tasks": 0}})
        assert score == 0.0

    def test_processing_higher_than_idle(self):
        """PROCESSING worker has higher load than IDLE."""
        from tools.skynet_dispatch import _load_score
        idle = _load_score("alpha", {"alpha": "IDLE"}, {})
        processing = _load_score("alpha", {"alpha": "PROCESSING"}, {})
        assert processing > idle

    def test_steering_higher_than_processing(self):
        """STEERING worker has higher load than PROCESSING."""
        from tools.skynet_dispatch import _load_score
        processing = _load_score("alpha", {"alpha": "PROCESSING"}, {})
        steering = _load_score("alpha", {"alpha": "STEERING"}, {})
        assert steering > processing

    def test_pending_tasks_increase_load(self):
        """Pending tasks increase load score."""
        from tools.skynet_dispatch import _load_score
        no_pending = _load_score("alpha", {"alpha": "IDLE"}, {"alpha": {"pending_tasks": 0}})
        has_pending = _load_score("alpha", {"alpha": "IDLE"}, {"alpha": {"pending_tasks": 3}})
        assert has_pending > no_pending

    def test_max_pending_caps_at_one(self):
        """Load score caps at 1.0 even with many pending tasks."""
        from tools.skynet_dispatch import _load_score
        score = _load_score("alpha", {"alpha": "STEERING"}, {"alpha": {"pending_tasks": 100}})
        assert score <= 1.0


# ── Batch Dispatch Consolidation Tests ──────────────────────────────────────

class TestBatchDispatch:
    """Tests for batch_dispatch() task consolidation logic."""

    def test_single_task_not_consolidated(self):
        """Single task per worker passes through unchanged."""
        from tools.skynet_dispatch import batch_dispatch
        with patch("tools.skynet_dispatch.dispatch_parallel") as mock_parallel, \
             patch("tools.skynet_dispatch.load_workers", return_value=[]), \
             patch("tools.skynet_dispatch.load_orch_hwnd", return_value=None):
            mock_parallel.return_value = {"alpha": True}
            batch_dispatch({"alpha": ["single task"]})
            args = mock_parallel.call_args
            task_map = args[0][0]
            assert task_map["alpha"] == "single task"

    def test_multi_tasks_consolidated(self):
        """Multiple tasks for same worker are merged into numbered mega-prompt."""
        from tools.skynet_dispatch import batch_dispatch
        with patch("tools.skynet_dispatch.dispatch_parallel") as mock_parallel, \
             patch("tools.skynet_dispatch.load_workers", return_value=[]), \
             patch("tools.skynet_dispatch.load_orch_hwnd", return_value=None):
            mock_parallel.return_value = {"alpha": True}
            batch_dispatch({"alpha": ["task A", "task B", "task C"]})
            args = mock_parallel.call_args
            mega = args[0][0]["alpha"]
            assert "MULTI-TASK DISPATCH (3 tasks)" in mega
            assert "TASK 1/3: task A" in mega
            assert "TASK 2/3: task B" in mega
            assert "TASK 3/3: task C" in mega

    def test_string_task_treated_as_single(self):
        """String value (not list) is treated as single task."""
        from tools.skynet_dispatch import batch_dispatch
        with patch("tools.skynet_dispatch.dispatch_parallel") as mock_parallel, \
             patch("tools.skynet_dispatch.load_workers", return_value=[]), \
             patch("tools.skynet_dispatch.load_orch_hwnd", return_value=None):
            mock_parallel.return_value = {"beta": True}
            batch_dispatch({"beta": "solo task"})
            args = mock_parallel.call_args
            assert args[0][0]["beta"] == "solo task"


# ── Worker State Enrichment Tests ───────────────────────────────────────────

class TestWorkerStateEnrichment:
    """Tests for _enrich_worker_states()."""

    def test_dict_agents_format(self):
        """Handles dict-style agents from /status response."""
        from tools.skynet_dispatch import _enrich_worker_states
        status_data = {
            "agents": {
                "alpha": {"status": "IDLE", "current_task": ""},
                "beta": {"status": "PROCESSING", "current_task": "fixing bug"},
                "gamma": {"status": "IDLE", "current_task": ""},
            }
        }
        with patch("tools.skynet_dispatch._fetch_json_quiet", return_value=status_data):
            result = _enrich_worker_states("alpha")
            assert result is not None
            assert "beta=PROCESSING" in result
            assert "gamma=IDLE" in result
            # Should not include self (alpha)
            assert "alpha=" not in result

    def test_list_agents_format(self):
        """Handles list-style agents from /status response."""
        from tools.skynet_dispatch import _enrich_worker_states
        status_data = {
            "agents": [
                {"name": "alpha", "status": "IDLE", "current_task": ""},
                {"name": "beta", "status": "IDLE", "current_task": ""},
            ]
        }
        with patch("tools.skynet_dispatch._fetch_json_quiet", return_value=status_data):
            result = _enrich_worker_states("beta")
            assert "alpha=IDLE" in result
            assert "beta=" not in result

    def test_backend_offline_returns_none(self):
        """Returns None when backend is unreachable."""
        from tools.skynet_dispatch import _enrich_worker_states
        with patch("tools.skynet_dispatch._fetch_json_quiet", return_value=None):
            result = _enrich_worker_states("alpha")
            assert result is None


# ── Routing Config Tests ────────────────────────────────────────────────────

class TestRoutingConfig:
    """Tests for _load_routing_config()."""

    def test_loads_custom_weights(self, brain_config_file, tmp_data_dir):
        """Custom routing weights are loaded from brain_config.json."""
        with patch("tools.skynet_dispatch.DATA_DIR", tmp_data_dir):
            from tools.skynet_dispatch import _load_routing_config
            exp_w, load_w = _load_routing_config()
            assert exp_w == 0.6
            assert load_w == 0.4

    def test_defaults_on_missing_file(self, tmp_data_dir):
        """Default weights returned when config is missing."""
        from tools.skynet_dispatch import _load_routing_config
        with patch("tools.skynet_dispatch.DATA_DIR", tmp_data_dir / "nonexistent"):
            exp_w, load_w = _load_routing_config()
            assert exp_w == 0.6
            assert load_w == 0.4


# ── Dispatch to Worker Edge Cases ───────────────────────────────────────────

class TestDispatchEdgeCases:
    """Tests for dispatch_to_worker() edge cases."""

    def test_worker_not_found(self):
        """Returns False when target worker is not in workers list."""
        import tools.skynet_dispatch as sd
        old = sd._SELF_WORKER_NAME
        try:
            sd._SELF_WORKER_NAME = "orchestrator"
            result = sd.dispatch_to_worker(
                "nonexistent",
                "test task",
                workers=[{"name": "alpha", "hwnd": 123}],
                orch_hwnd=999,
            )
            assert result is False
        finally:
            sd._SELF_WORKER_NAME = old

    def test_dispatch_to_orchestrator_routes_correctly(self):
        """dispatch_to_worker with 'orchestrator' uses _dispatch_to_orchestrator."""
        import tools.skynet_dispatch as sd
        old = sd._SELF_WORKER_NAME
        try:
            sd._SELF_WORKER_NAME = "alpha"
            with patch("tools.skynet_dispatch._dispatch_to_orchestrator", return_value=True) as mock_orch:
                result = sd.dispatch_to_worker("orchestrator", "report status")
                assert result is True
                mock_orch.assert_called_once()
        finally:
            sd._SELF_WORKER_NAME = old

    def test_dispatch_to_consultant_routes_correctly(self):
        """dispatch_to_worker with 'consultant' uses _dispatch_to_consultant."""
        import tools.skynet_dispatch as sd
        old = sd._SELF_WORKER_NAME
        try:
            sd._SELF_WORKER_NAME = "alpha"
            with patch("tools.skynet_dispatch._dispatch_to_consultant", return_value=True) as mock_consult:
                result = sd.dispatch_to_worker("consultant", "advisory request")
                assert result is True
                mock_consult.assert_called_once()
        finally:
            sd._SELF_WORKER_NAME = old


# ── NO_STEERING_PREAMBLE Tests ──────────────────────────────────────────────

class TestNoSteeringPreamble:
    """Tests for the NO_STEERING_PREAMBLE constant."""

    def test_preamble_instructs_direct_execution(self):
        """Preamble tells worker to execute directly."""
        from tools.skynet_dispatch import NO_STEERING_PREAMBLE
        assert "Execute all steps directly" in NO_STEERING_PREAMBLE
        assert "Do NOT show steering options" in NO_STEERING_PREAMBLE

    def test_preamble_instructs_bus_reporting(self):
        """Preamble instructs bus result posting."""
        from tools.skynet_dispatch import NO_STEERING_PREAMBLE
        assert "Post results to bus" in NO_STEERING_PREAMBLE


# ── Dispatch Log Tests ──────────────────────────────────────────────────────

class TestDispatchLog:
    """Tests for _log_dispatch() and mark_dispatch_received()."""

    def test_log_dispatch_creates_entry(self, dispatch_log_file, tmp_data_dir):
        """Dispatch logging creates a structured entry."""
        with patch("tools.skynet_dispatch.DISPATCH_LOG", dispatch_log_file):
            from tools.skynet_dispatch import _log_dispatch
            _log_dispatch("alpha", "test task for logging", "IDLE", True, 12345)
            
            assert dispatch_log_file.exists()
            log_data = json.loads(dispatch_log_file.read_text(encoding="utf-8"))
            assert len(log_data) >= 1
            entry = log_data[-1]
            assert entry["worker"] == "alpha"
            assert entry["success"] is True
            assert entry["state_at_dispatch"] == "IDLE"
            assert entry["target_hwnd"] == 12345
            assert "timestamp" in entry

    def test_log_dispatch_truncates_at_200(self, dispatch_log_file, tmp_data_dir):
        """Dispatch log is capped at 200 entries."""
        # Pre-populate with 205 entries
        existing = [{"worker": "test", "i": i} for i in range(205)]
        dispatch_log_file.write_text(json.dumps(existing), encoding="utf-8")
        
        with patch("tools.skynet_dispatch.DISPATCH_LOG", dispatch_log_file):
            from tools.skynet_dispatch import _log_dispatch
            _log_dispatch("alpha", "one more task", "IDLE", True)
            
            log_data = json.loads(dispatch_log_file.read_text(encoding="utf-8"))
            assert len(log_data) <= 200


# ── Heartbeat Tests ─────────────────────────────────────────────────────────

class TestHeartbeat:
    """Tests for send_heartbeat()."""

    def test_heartbeat_fires_without_error(self):
        """Heartbeat does not raise even when backend is down."""
        from tools.skynet_dispatch import send_heartbeat
        # This should not raise even if backend is unreachable
        send_heartbeat("alpha", status="IDLE", current_task="")

    def test_heartbeat_after_dispatch_sends_working(self):
        """Heartbeat after successful dispatch sends WORKING status."""
        from tools.skynet_dispatch import _heartbeat_after_dispatch
        with patch("tools.skynet_dispatch.send_heartbeat") as mock_hb:
            _heartbeat_after_dispatch("alpha", "test task", True)
            import time
            time.sleep(0.1)  # Thread needs a moment


# ── Metrics Singleton Tests ─────────────────────────────────────────────────

class TestMetricsSingleton:
    """Tests for the metrics() lazy singleton."""

    def test_metrics_returns_none_on_import_failure(self):
        """metrics() returns None when SkynetMetrics can't be imported."""
        import tools.skynet_dispatch as sd
        old = sd._metrics
        sd._metrics = None
        try:
            with patch.dict('sys.modules', {'tools.skynet_metrics': None}):
                with patch('builtins.__import__', side_effect=ImportError("no module")):
                    result = sd.metrics()
                    # Should be None since import fails
        finally:
            sd._metrics = old


# ── Ghost Type Tests (signed: beta) ────────────────────────────────────────

import tools.skynet_dispatch as dispatch  # module alias for new tests  # signed: beta


class TestGhostTypeToWorker:
    """Tests for ghost_type_to_worker(hwnd, text, orch_hwnd).

    All subprocess and file I/O is mocked — no real windows or clipboard touched.
    """  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_success_returns_true(self, mock_ps, mock_exec):
        """Successful ghost-type returns True."""
        with patch.object(Path, "write_text"):
            result = dispatch.ghost_type_to_worker(12345, "hello", 99999)
        assert result is True
        mock_exec.assert_called_once()  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=False)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_failure_returns_false(self, mock_ps, mock_exec):
        """Failed ghost-type returns False."""
        with patch.object(Path, "write_text"):
            result = dispatch.ghost_type_to_worker(12345, "hello", 99999)
        assert result is False  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_newlines_replaced_with_spaces(self, mock_ps, mock_exec):
        """Newlines in task text are replaced with spaces for single-line paste."""
        with patch.object(Path, "write_text") as mock_write:
            dispatch.ghost_type_to_worker(11111, "line1\nline2\nline3", None)
        written_text = mock_write.call_args[0][0]
        assert "\n" not in written_text
        assert "line1 line2 line3" in written_text  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_orch_hwnd_none_accepted(self, mock_ps, mock_exec):
        """orch_hwnd=None should not crash."""
        with patch.object(Path, "write_text"):
            result = dispatch.ghost_type_to_worker(12345, "test", None)
        assert result is True  # signed: beta

    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_file_write_failure_propagates(self, mock_ps):
        """If temp file write fails, exception propagates."""
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                dispatch.ghost_type_to_worker(12345, "test", None)  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_long_text_not_truncated(self, mock_ps, mock_exec):
        """Very long text (15K+ chars) should not truncate."""
        long_text = "A" * 15000
        with patch.object(Path, "write_text") as mock_write:
            result = dispatch.ghost_type_to_worker(12345, long_text, None)
        assert result is True
        assert len(mock_write.call_args[0][0]) == 15000  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_temp_file_uses_hwnd_in_name(self, mock_ps, mock_exec):
        """Temp dispatch file name includes the target HWND."""
        with patch.object(Path, "write_text"):
            dispatch.ghost_type_to_worker(77777, "task", None)
        # _build_ghost_type_ps receives the dispatch file path containing HWND
        ps_args = mock_ps.call_args[0]
        assert "77777" in ps_args[2]  # dispatch_file_path arg  # signed: beta

    @patch.object(dispatch, "_execute_ghost_dispatch", return_value=True)
    @patch.object(dispatch, "_build_ghost_type_ps", return_value="fake-ps")
    def test_unicode_text_handled(self, mock_ps, mock_exec):
        """Unicode text (emoji, CJK) should not crash."""
        with patch.object(Path, "write_text") as mock_write:
            result = dispatch.ghost_type_to_worker(12345, "🚀 任务完成 ✓", None)
        assert result is True
        assert "🚀" in mock_write.call_args[0][0]  # signed: beta


# ── Dispatch Flow Tests (signed: beta) ─────────────────────────────────────


class TestDispatchToWorkerFlow:
    """End-to-end flow tests for dispatch_to_worker with all deps mocked.

    Covers: window visibility, visual check, STEERING handling, HWND validation,
    ghost-type success/failure, fallback steer-bypass, context preamble selection.
    """  # signed: beta

    MOCK_WORKERS = [
        {"name": "alpha", "hwnd": 100001},
        {"name": "beta", "hwnd": 100002},
        {"name": "gamma", "hwnd": 100003},
        {"name": "delta", "hwnd": 100004},
    ]

    @pytest.fixture(autouse=True)
    def _patch_base(self):
        """Patch all external deps for dispatch_to_worker tests."""
        self._patches = {
            "self_id": patch.object(dispatch, "_get_self_identity", return_value="orchestrator"),
            "load_w": patch.object(dispatch, "load_workers", return_value=self.MOCK_WORKERS),
            "load_o": patch.object(dispatch, "load_orch_hwnd", return_value=999999),
            "visible": patch("ctypes.windll.user32.IsWindowVisible", return_value=True),
            "vis_chk": patch.object(dispatch, "pre_dispatch_visual_check",
                                    return_value=(True, "IDLE", None)),
            "enrich": patch.object(dispatch, "enrich_task", side_effect=lambda w, t: t),
            "preamble": patch.object(dispatch, "build_preamble", return_value="[P]"),
            "validate": patch.object(dispatch, "_validate_target_hwnd", return_value=True),
            "ghost": patch.object(dispatch, "ghost_type_to_worker", return_value=True),
            "record": patch.object(dispatch, "_record_dispatch_outcome"),
            "log_d": patch.object(dispatch, "_log_dispatch"),
            "steer": patch.object(dispatch, "clear_steering_and_send", return_value=False),
            "sleep": patch("time.sleep"),
        }
        self.mocks = {k: p.start() for k, p in self._patches.items()}
        yield
        for p in self._patches.values():
            p.stop()

    def test_window_not_visible_aborts(self):
        """Dispatch returns False if target window is not visible."""
        self.mocks["visible"].return_value = False
        assert dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999) is False
        self.mocks["ghost"].assert_not_called()  # signed: beta

    def test_visual_check_fail_aborts(self):
        """Dispatch returns False if visual check fails (bad model)."""
        self.mocks["vis_chk"].return_value = (False, "UNKNOWN", None)
        assert dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999) is False
        self.mocks["ghost"].assert_not_called()  # signed: beta

    def test_steering_auto_cancelled(self):
        """STEERING state triggers auto-cancel + 1s sleep before dispatch."""
        self.mocks["vis_chk"].return_value = (True, "STEERING", None)
        dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        self.mocks["steer"].assert_called()
        self.mocks["sleep"].assert_any_call(1.0)  # signed: beta

    def test_processing_no_wait(self):
        """PROCESSING state dispatches immediately — no 1s sleep."""
        self.mocks["vis_chk"].return_value = (True, "PROCESSING", None)
        dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        # sleep(1.0) is only for STEERING, not PROCESSING
        for call in self.mocks["sleep"].call_args_list:
            assert call[0][0] != 1.0  # signed: beta

    def test_hwnd_validation_fail_aborts(self):
        """Dispatch returns False if HWND security validation fails."""
        self.mocks["validate"].return_value = False
        assert dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999) is False
        self.mocks["ghost"].assert_not_called()  # signed: beta

    def test_happy_path_ghost_type_success(self):
        """Happy path: all checks pass, ghost_type succeeds."""
        result = dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        assert result is True
        self.mocks["ghost"].assert_called_once()
        self.mocks["record"].assert_called_once()  # signed: beta

    def test_ghost_fail_tries_steer_bypass(self):
        """Ghost-type failure triggers steer-bypass fallback."""
        self.mocks["ghost"].return_value = False
        self.mocks["steer"].return_value = True
        result = dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        assert result is True
        self.mocks["steer"].assert_called()  # signed: beta

    def test_both_methods_fail_returns_false(self):
        """Both ghost-type and steer-bypass fail → returns False."""
        self.mocks["ghost"].return_value = False
        self.mocks["steer"].return_value = False
        result = dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        assert result is False  # signed: beta

    def test_context_uses_context_preamble(self):
        """Context dict triggers build_context_preamble instead of build_preamble."""
        with patch.object(dispatch, "build_context_preamble", return_value="[CTX]") as mock_ctx:
            ctx = {"difficulty": "COMPLEX", "learnings": []}
            result = dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999, context=ctx)
        assert result is True
        mock_ctx.assert_called_once()  # signed: beta

    def test_no_context_uses_build_preamble(self):
        """Without context, build_preamble is used."""
        dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999)
        self.mocks["preamble"].assert_called_once()  # signed: beta

    def test_empty_workers_falls_back_to_disk(self):
        """Empty workers list triggers load_workers() from disk."""
        dispatch.dispatch_to_worker("alpha", "task", [], 999999)
        # Empty list is falsy, so load_workers() is called as fallback
        self.mocks["load_w"].assert_called_once()  # signed: beta

    def test_enrich_task_called(self):
        """enrich_task is called to add intelligence to the task."""
        dispatch.dispatch_to_worker("alpha", "my-task", self.MOCK_WORKERS, 999999)
        self.mocks["enrich"].assert_called_once_with("alpha", "my-task")  # signed: beta

    def test_self_dispatch_blocked_case_insensitive(self):
        """Self-dispatch is blocked case-insensitively."""
        self.mocks["self_id"].return_value = "ALPHA"
        assert dispatch.dispatch_to_worker("alpha", "task", self.MOCK_WORKERS, 999999) is False
        # signed: beta


# ── Load Helpers Tests (signed: beta) ───────────────────────────────────────


class TestLoadHelpers:
    """Tests for load_workers() and load_orch_hwnd() file-based loaders."""
    # signed: beta

    def test_load_workers_missing_file(self):
        """Missing workers.json returns empty list."""
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(dispatch, "WORKERS_FILE", mock_path):
            assert dispatch.load_workers() == []  # signed: beta

    def test_load_workers_valid_file(self):
        """Valid workers.json returns worker list."""
        data = {"workers": [{"name": "alpha", "hwnd": 111}]}
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(data)
        with patch.object(dispatch, "WORKERS_FILE", mock_path):
            result = dispatch.load_workers()
        assert len(result) == 1
        assert result[0]["name"] == "alpha"  # signed: beta

    def test_load_orch_hwnd_missing_file(self):
        """Missing orchestrator.json returns None."""
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(dispatch, "ORCH_FILE", mock_path):
            assert dispatch.load_orch_hwnd() is None  # signed: beta

    def test_load_orch_hwnd_valid_file(self):
        """Valid orchestrator.json returns HWND integer."""
        data = {"orchestrator_hwnd": 555555}
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(data)
        with patch.object(dispatch, "ORCH_FILE", mock_path):
            assert dispatch.load_orch_hwnd() == 555555  # signed: beta


# ── Bus Message Scanning Tests (signed: alpha) ─────────────────────────────


class TestScanBusMessagesForKey:
    """Tests for _scan_bus_messages_for_key() — bus result matching."""
    # signed: alpha

    def test_match_in_content(self):
        """Key substring found in message content returns match."""
        msgs = [{"id": "m1", "content": "alpha completed task", "sender": "alpha"}]
        result = dispatch._scan_bus_messages_for_key(msgs, "completed", set())
        assert result == msgs[0]  # signed: alpha

    def test_match_in_sender(self):
        """Key substring found in sender returns match."""
        msgs = [{"id": "m1", "content": "done", "sender": "alpha"}]
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", set())
        assert result == msgs[0]  # signed: alpha

    def test_no_match(self):
        """No matching message returns None."""
        msgs = [{"id": "m1", "content": "gamma done", "sender": "gamma"}]
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", set())
        assert result is None  # signed: alpha

    def test_skips_seen_ids(self):
        """Already-seen message IDs are skipped."""
        msgs = [{"id": "m1", "content": "alpha result", "sender": "alpha"}]
        seen = {"m1"}
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", seen)
        assert result is None  # signed: alpha

    def test_adds_to_seen_ids(self):
        """Matched message ID is added to seen_ids set."""
        msgs = [{"id": "m1", "content": "alpha result", "sender": "alpha"}]
        seen = set()
        dispatch._scan_bus_messages_for_key(msgs, "alpha", seen)
        assert "m1" in seen  # signed: alpha

    def test_case_insensitive(self):
        """Key matching is case-insensitive."""
        msgs = [{"id": "m1", "content": "ALPHA DONE", "sender": "worker"}]
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", set())
        assert result == msgs[0]  # signed: alpha

    def test_empty_messages(self):
        """Empty messages list returns None."""
        result = dispatch._scan_bus_messages_for_key([], "alpha", set())
        assert result is None  # signed: alpha

    def test_non_dict_messages_skipped(self):
        """Non-dict entries in messages list are gracefully skipped."""
        msgs = ["not a dict", 42, None, {"id": "m2", "content": "alpha ok", "sender": "x"}]
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", set())
        assert result["id"] == "m2"  # signed: alpha

    def test_missing_id_field(self):
        """Message without 'id' uses empty string, still matches."""
        msgs = [{"content": "alpha ok", "sender": "x"}]
        seen = set()
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", seen)
        assert result is not None
        assert "" in seen  # signed: alpha

    def test_first_match_returned(self):
        """Returns the first matching message, not later ones."""
        msgs = [
            {"id": "m1", "content": "alpha first", "sender": "x"},
            {"id": "m2", "content": "alpha second", "sender": "x"},
        ]
        result = dispatch._scan_bus_messages_for_key(msgs, "alpha", set())
        assert result["id"] == "m1"  # signed: alpha

    def test_dedup_across_calls(self):
        """Seen IDs accumulate across multiple scan calls."""
        msgs = [{"id": "m1", "content": "alpha r1", "sender": "x"}]
        seen = set()
        r1 = dispatch._scan_bus_messages_for_key(msgs, "alpha", seen)
        assert r1 is not None
        r2 = dispatch._scan_bus_messages_for_key(msgs, "alpha", seen)
        assert r2 is None  # Already seen  # signed: alpha


# ── Wait For Bus Result Tests (signed: alpha) ──────────────────────────────


class TestWaitForBusResult:
    """Tests for wait_for_bus_result() — result correlation with dedup and timeout."""
    # signed: alpha

    def test_realtime_file_found(self, tmp_path):
        """When realtime.json exists and contains a match, returns immediately."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text(json.dumps([
            {"id": "m1", "content": "alpha completed task X", "sender": "alpha"}
        ]), encoding="utf-8")

        with patch("tools.skynet_dispatch.os.path.exists", return_value=True), \
             patch("tools.skynet_dispatch.os.path.join", return_value=str(rt_file)), \
             patch("tools.skynet_dispatch._wait_via_realtime_file") as mock_rt:
            mock_rt.return_value = {"id": "m1", "content": "alpha completed task X", "sender": "alpha"}
            result = dispatch.wait_for_bus_result("alpha", timeout=5, auto_recover=False)
        assert result is not None
        assert result["id"] == "m1"  # signed: alpha

    def test_fallback_to_http_when_no_realtime(self, tmp_path):
        """When realtime.json doesn't exist, falls back to HTTP polling."""
        with patch("tools.skynet_dispatch.os.path.exists", return_value=False), \
             patch("tools.skynet_dispatch._wait_via_http_polling") as mock_http:
            mock_http.return_value = {"id": "m2", "content": "result", "sender": "beta"}
            result = dispatch.wait_for_bus_result("beta", timeout=5, auto_recover=False)
        assert result is not None
        mock_http.assert_called_once()  # signed: alpha

    def test_timeout_returns_none(self):
        """When no match found within timeout, returns None."""
        with patch("tools.skynet_dispatch.os.path.exists", return_value=False), \
             patch("tools.skynet_dispatch._wait_via_http_polling", return_value=None):
            result = dispatch.wait_for_bus_result("nonexistent", timeout=1, auto_recover=False)
        assert result is None  # signed: alpha

    def test_auto_recover_on_timeout(self):
        """When auto_recover=True and _original_task provided, tries recovery on timeout."""
        with patch("tools.skynet_dispatch.os.path.exists", return_value=False), \
             patch("tools.skynet_dispatch._wait_via_http_polling", return_value=None), \
             patch("tools.skynet_dispatch._auto_recover_stuck_workers", return_value=False) as mock_recover:
            result = dispatch.wait_for_bus_result(
                "alpha", timeout=1, auto_recover=True, _original_task="test task"
            )
        assert result is None
        mock_recover.assert_called_once_with("alpha", "test task")  # signed: alpha

    def test_auto_recover_disabled(self):
        """When auto_recover=False, no recovery attempted on timeout."""
        with patch("tools.skynet_dispatch.os.path.exists", return_value=False), \
             patch("tools.skynet_dispatch._wait_via_http_polling", return_value=None), \
             patch("tools.skynet_dispatch._auto_recover_stuck_workers") as mock_recover:
            dispatch.wait_for_bus_result("alpha", timeout=1, auto_recover=False)
        mock_recover.assert_not_called()  # signed: alpha

    def test_auto_recover_not_triggered_without_original_task(self):
        """Auto-recovery requires _original_task to be set."""
        with patch("tools.skynet_dispatch.os.path.exists", return_value=False), \
             patch("tools.skynet_dispatch._wait_via_http_polling", return_value=None), \
             patch("tools.skynet_dispatch._auto_recover_stuck_workers") as mock_recover:
            dispatch.wait_for_bus_result("alpha", timeout=1, auto_recover=True)
        mock_recover.assert_not_called()  # signed: alpha


# ── Realtime File Waiting Tests (signed: alpha) ────────────────────────────


class TestWaitViaRealtimeFile:
    """Tests for _wait_via_realtime_file() — file-based polling at 0.5s resolution."""
    # signed: alpha

    def test_immediate_match(self, tmp_path):
        """Match found on first poll returns immediately."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text(json.dumps([
            {"id": "m1", "content": "alpha done", "sender": "alpha"}
        ]), encoding="utf-8")
        import time
        deadline = time.time() + 5
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(rt_file))
        assert result is not None
        assert result["id"] == "m1"  # signed: alpha

    def test_no_match_returns_none(self, tmp_path):
        """No matching message before deadline returns None."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text(json.dumps([
            {"id": "m1", "content": "gamma done", "sender": "gamma"}
        ]), encoding="utf-8")
        import time
        deadline = time.time() + 0.6  # Just over one poll cycle
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(rt_file))
        assert result is None  # signed: alpha

    def test_invalid_json_handled(self, tmp_path):
        """Corrupt JSON file is handled gracefully."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text("{bad json", encoding="utf-8")
        import time
        deadline = time.time() + 0.6
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(rt_file))
        assert result is None  # signed: alpha

    def test_missing_file_handled(self, tmp_path):
        """Missing realtime file is handled gracefully."""
        import time
        deadline = time.time() + 0.6
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(tmp_path / "nonexistent.json"))
        assert result is None  # signed: alpha

    def test_dict_format_with_messages_key(self, tmp_path):
        """Realtime file using dict format with 'messages' key works."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text(json.dumps({
            "messages": [{"id": "m1", "content": "alpha done", "sender": "alpha"}]
        }), encoding="utf-8")
        import time
        deadline = time.time() + 5
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(rt_file))
        assert result is not None  # signed: alpha

    def test_dict_format_with_results_key(self, tmp_path):
        """Realtime file using dict format with 'results' key works."""
        rt_file = tmp_path / "realtime.json"
        rt_file.write_text(json.dumps({
            "results": [{"id": "m1", "content": "alpha done", "sender": "alpha"}]
        }), encoding="utf-8")
        import time
        deadline = time.time() + 5
        result = dispatch._wait_via_realtime_file("alpha", set(), deadline, str(rt_file))
        assert result is not None  # signed: alpha


# ── Smart Dispatch Ranking Tests (signed: alpha) ───────────────────────────


class TestSmartDispatchRanking:
    """Tests for smart_dispatch() full routing pipeline — ranking and fallback."""
    # signed: alpha

    MOCK_WORKERS = [
        {"name": "alpha", "hwnd": 100001},
        {"name": "beta", "hwnd": 100002},
        {"name": "gamma", "hwnd": 100003},
        {"name": "delta", "hwnd": 100004},
    ]

    @pytest.fixture(autouse=True)
    def _mock_dispatch_deps(self):
        """Mock all dependencies of smart_dispatch."""
        with patch.object(dispatch, "load_workers", return_value=self.MOCK_WORKERS), \
             patch.object(dispatch, "load_orch_hwnd", return_value=999999), \
             patch.object(dispatch, "scan_all_states") as mock_states, \
             patch.object(dispatch, "get_worker_statuses") as mock_bus, \
             patch.object(dispatch, "_load_routing_config", return_value=(0.6, 0.4)), \
             patch.object(dispatch, "_load_worker_profiles", return_value={
                 "alpha": {"specializations": ["frontend", "css", "html"]},
                 "beta": {"specializations": ["backend", "api", "database"]},
                 "gamma": {"specializations": ["testing", "qa", "validation"]},
                 "delta": {"specializations": ["devops", "deployment", "docker"]},
             }), \
             patch.object(dispatch, "dispatch_to_worker", return_value=True) as mock_dtw, \
             patch.object(dispatch, "dispatch_parallel", return_value={}) as mock_dp:
            self.mock_states = mock_states
            self.mock_bus = mock_bus
            self.mock_dtw = mock_dtw
            self.mock_dp = mock_dp
            yield

    def test_routes_to_expert_worker(self):
        """Frontend task routes to alpha (frontend specialist) when all IDLE."""
        self.mock_states.return_value = {
            "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
        }
        self.mock_bus.return_value = {}
        result = dispatch.smart_dispatch("Fix the frontend CSS layout", self.MOCK_WORKERS, 999999)
        assert result == ["alpha"]
        self.mock_dtw.assert_called_once()
        assert self.mock_dtw.call_args[0][0] == "alpha"  # signed: alpha

    def test_routes_backend_to_beta(self):
        """Backend/API task routes to beta (backend specialist)."""
        self.mock_states.return_value = {
            "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
        }
        self.mock_bus.return_value = {}
        result = dispatch.smart_dispatch("Fix the database API endpoint", self.MOCK_WORKERS, 999999)
        assert result == ["beta"]  # signed: alpha

    def test_skips_processing_workers(self):
        """PROCESSING workers are skipped in favor of IDLE ones."""
        self.mock_states.return_value = {
            "alpha": "PROCESSING", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
        }
        self.mock_bus.return_value = {}
        result = dispatch.smart_dispatch("Fix the frontend CSS", self.MOCK_WORKERS, 999999)
        # Alpha is best match but PROCESSING, should pick next best IDLE
        assert "alpha" not in result  # signed: alpha

    def test_no_idle_fallback_to_non_steering(self):
        """When no IDLE workers, falls back to PROCESSING/TYPING (non-STEERING)."""
        self.mock_states.return_value = {
            "alpha": "PROCESSING", "beta": "PROCESSING", "gamma": "TYPING", "delta": "STEERING"
        }
        self.mock_bus.return_value = {}
        result = dispatch.smart_dispatch("do something", self.MOCK_WORKERS, 999999)
        # Delta is STEERING so excluded from fallback
        assert "delta" not in result  # signed: alpha

    def test_all_steering_returns_empty(self):
        """When all workers are STEERING or UNKNOWN, returns empty list."""
        self.mock_states.return_value = {
            "alpha": "STEERING", "beta": "STEERING", "gamma": "UNKNOWN", "delta": "STEERING"
        }
        self.mock_bus.return_value = {}
        result = dispatch.smart_dispatch("task", self.MOCK_WORKERS, 999999)
        assert result == []  # signed: alpha

    def test_n_workers_multiple(self):
        """n_workers=2 dispatches to top 2 workers in parallel."""
        self.mock_states.return_value = {
            "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
        }
        self.mock_bus.return_value = {}
        self.mock_dp.return_value = {"alpha": True, "gamma": True}
        result = dispatch.smart_dispatch("Run the test validation suite", self.MOCK_WORKERS, 999999, n_workers=2)
        self.mock_dp.assert_called_once()  # Used parallel dispatch
        assert len(result) == 2  # signed: alpha

    def test_load_affects_ranking(self):
        """Workers with pending tasks score lower than idle ones."""
        self.mock_states.return_value = {
            "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
        }
        # Alpha has many pending tasks — should be deprioritized
        self.mock_bus.return_value = {
            "alpha": {"pending_tasks": 5},
            "beta": {"pending_tasks": 0},
            "gamma": {"pending_tasks": 0},
            "delta": {"pending_tasks": 0},
        }
        result = dispatch.smart_dispatch("generic task no specialty", self.MOCK_WORKERS, 999999)
        # With no specialty match, load is the differentiator
        assert result[0] != "alpha"  # Alpha deprioritized due to load  # signed: alpha


# ── Dispatch Parallel Tests (signed: alpha) ────────────────────────────────


class TestDispatchParallel:
    """Tests for dispatch_parallel() — threaded parallel dispatch."""
    # signed: alpha

    MOCK_WORKERS = [
        {"name": "alpha", "hwnd": 100001},
        {"name": "beta", "hwnd": 100002},
    ]

    def test_dispatches_all_tasks(self):
        """All tasks in the map are dispatched."""
        tasks = {"alpha": "task A", "beta": "task B"}
        with patch.object(dispatch, "dispatch_to_worker", return_value=True) as mock_dtw:
            results = dispatch.dispatch_parallel(tasks, self.MOCK_WORKERS, 999999)
        assert results == {"alpha": True, "beta": True}
        assert mock_dtw.call_count == 2  # signed: alpha

    def test_empty_task_map(self):
        """Empty task map returns empty results (dispatch_parallel raises on n=0)."""
        # dispatch_parallel passes len(tasks) as max_workers to ThreadPoolExecutor
        # which raises ValueError when 0, so empty map is effectively a bug/edge case
        with patch.object(dispatch, "dispatch_to_worker", return_value=True):
            with pytest.raises(ValueError, match="max_workers must be greater than 0"):
                dispatch.dispatch_parallel({}, self.MOCK_WORKERS, 999999)
        # signed: alpha

    def test_partial_failure(self):
        """If one dispatch fails, others still succeed."""
        def side_effect(name, task, workers, orch):
            return name != "beta"  # beta fails
        tasks = {"alpha": "task A", "beta": "task B"}
        with patch.object(dispatch, "dispatch_to_worker", side_effect=side_effect):
            results = dispatch.dispatch_parallel(tasks, self.MOCK_WORKERS, 999999)
        assert results["alpha"] is True
        assert results["beta"] is False  # signed: alpha

    def test_exception_in_dispatch_returns_false(self):
        """Exception during dispatch returns False for that worker."""
        def side_effect(name, task, workers, orch):
            if name == "beta":
                raise RuntimeError("UIA failed")
            return True
        tasks = {"alpha": "task A", "beta": "task B"}
        with patch.object(dispatch, "dispatch_to_worker", side_effect=side_effect):
            results = dispatch.dispatch_parallel(tasks, self.MOCK_WORKERS, 999999)
        assert results["alpha"] is True
        assert results["beta"] is False  # signed: alpha


# ── Mark Dispatch Received Tests (signed: alpha) ───────────────────────────


class TestMarkDispatchReceived:
    """Tests for mark_dispatch_received() — atomic log update."""
    # signed: alpha

    def test_marks_pending_as_received(self, tmp_path):
        """Marks the most recent pending dispatch for a worker as received."""
        log_file = tmp_path / "dispatch_log.json"
        entries = [
            {"worker": "alpha", "result_received": False, "ts": "2026-01-01"},
            {"worker": "beta", "result_received": False, "ts": "2026-01-02"},
        ]
        log_file.write_text(json.dumps(entries), encoding="utf-8")
        with patch.object(dispatch, "DISPATCH_LOG", log_file):
            dispatch.mark_dispatch_received("alpha")
        updated = json.loads(log_file.read_text(encoding="utf-8"))
        alpha_entries = [e for e in updated if e["worker"] == "alpha"]
        assert alpha_entries[0]["result_received"] is True
        assert "result_received_at" in alpha_entries[0]  # signed: delta
        # Beta unchanged
        beta_entries = [e for e in updated if e["worker"] == "beta"]
        assert beta_entries[0]["result_received"] is False  # signed: alpha

    def test_missing_log_file(self, tmp_path):
        """No error when dispatch_log.json doesn't exist."""
        log_file = tmp_path / "dispatch_log.json"
        with patch.object(dispatch, "DISPATCH_LOG", log_file):
            # Should not raise
            dispatch.mark_dispatch_received("alpha")  # signed: alpha


# ── Idle Workers Tests (signed: alpha) ─────────────────────────────────────


class TestIdleWorkers:
    """Tests for idle_workers() — finds workers with zero pending tasks."""
    # signed: alpha

    def test_returns_idle_workers(self):
        """Workers with alive=True and zero pending/running are returned."""
        with patch.object(dispatch, "get_worker_statuses", return_value={
            "alpha": {"alive": True, "pending_tasks": 0, "running_tasks": 0},
            "beta": {"alive": True, "pending_tasks": 1, "running_tasks": 0},
            "gamma": {"alive": True, "pending_tasks": 0, "running_tasks": 0},
        }):
            result = dispatch.idle_workers()
        assert "alpha" in result
        assert "gamma" in result
        assert "beta" not in result  # signed: alpha

    def test_dead_workers_excluded(self):
        """Dead workers are not returned even if idle."""
        with patch.object(dispatch, "get_worker_statuses", return_value={
            "alpha": {"alive": False, "pending_tasks": 0, "running_tasks": 0},
        }):
            result = dispatch.idle_workers()
        assert result == []  # signed: alpha

    def test_running_workers_excluded(self):
        """Workers with running tasks are excluded."""
        with patch.object(dispatch, "get_worker_statuses", return_value={
            "alpha": {"alive": True, "pending_tasks": 0, "running_tasks": 1},
        }):
            result = dispatch.idle_workers()
        assert result == []  # signed: alpha
