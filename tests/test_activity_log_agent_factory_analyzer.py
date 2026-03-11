"""Unit tests for core modules: activity_log, agent_factory, analyzer.

Covers ActivityLogger (logging, timers, stats, session summary),
AgentRegistry/AgentFactory (registration, creation, teams, lifecycle),
and ScreenAnalyzer (response parsing, fallback, model selection).
"""

import json
import os
import sys
import time
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))


# ============================================================
# ACTIVITY LOGGER TESTS
# ============================================================

from core.activity_log import ActivityLogger, get_logger, _logger


class TestActivityLoggerInit:
    def test_creates_log_directory(self, tmp_path):
        log_dir = tmp_path / "test_logs"
        logger = ActivityLogger(log_dir=str(log_dir), console=False, file=True)
        assert log_dir.exists()

    def test_initializes_counters_empty(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        assert logger._counters == {}
        assert logger._timers == {}
        assert logger._errors == []

    def test_session_start_time_set(self, tmp_path):
        before = time.time()
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        after = time.time()
        assert before <= logger._session_start <= after

    def test_jsonl_path_in_log_dir(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        assert logger._jsonl_path == tmp_path / "activity.jsonl"

    def test_session_log_file_created(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=True)
        assert logger._text_path.parent == tmp_path
        assert "session_" in logger._text_path.name


class TestActivityLoggerLog:
    def test_log_increments_counter(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("CAPTURE", "frame_acquired")
        logger.log("CAPTURE", "frame_acquired")
        assert logger._counters["CAPTURE.frame_acquired"] == 2

    def test_log_different_actions_tracked_separately(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("CAPTURE", "frame_acquired")
        logger.log("VLM", "analysis_complete")
        assert logger._counters["CAPTURE.frame_acquired"] == 1
        assert logger._counters["VLM.analysis_complete"] == 1

    def test_log_error_level_tracked(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("DB", "connection_failed", level="ERROR", detail="timeout")
        assert len(logger._errors) == 1
        assert logger._errors[0]["component"] == "DB"
        assert logger._errors[0]["action"] == "connection_failed"
        assert logger._errors[0]["detail"] == "timeout"

    def test_log_info_not_tracked_as_error(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("CAPTURE", "frame_acquired", level="INFO")
        assert len(logger._errors) == 0

    def test_log_warn_not_tracked_as_error(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("EMBED", "skipped", level="WARN")
        assert len(logger._errors) == 0

    def test_log_writes_jsonl(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=True)
        logger.log("CAPTURE", "frame_acquired", detail="monitor=0")
        with open(logger._jsonl_path) as f:
            entry = json.loads(f.readline())
        assert entry["component"] == "CAPTURE"
        assert entry["action"] == "frame_acquired"
        assert entry["detail"] == "monitor=0"
        assert entry["level"] == "INFO"
        assert "ts" in entry

    def test_log_writes_data_to_jsonl(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=True)
        logger.log("CAPTURE", "frame_acquired", data={"width": 1920, "height": 1080})
        with open(logger._jsonl_path) as f:
            entry = json.loads(f.readline())
        assert entry["data"]["width"] == 1920

    def test_log_console_output(self, tmp_path, capsys):
        logger = ActivityLogger(log_dir=str(tmp_path), console=True, file=False)
        logger.log("VLM", "analysis_complete", detail="model=moondream")
        captured = capsys.readouterr()
        assert "VLM" in captured.out
        assert "analysis_complete" in captured.out

    def test_log_no_console_when_disabled(self, tmp_path, capsys):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("VLM", "analysis_complete")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_log_multiple_errors_tracked(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        for i in range(10):
            logger.log("DB", f"error_{i}", level="ERROR", detail=f"detail {i}")
        assert len(logger._errors) == 10


class TestActivityLoggerTimers:
    def test_timer_start_returns_float(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        start = logger.timer_start("test_op")
        assert isinstance(start, float)

    def test_timer_end_returns_elapsed_ms(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        start = logger.timer_start("test_op")
        elapsed = logger.timer_end("test_op", start)
        assert elapsed >= 0
        assert isinstance(elapsed, float)

    def test_timer_records_in_timers_dict(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        start = logger.timer_start("capture")
        logger.timer_end("capture", start)
        assert "capture" in logger._timers
        assert len(logger._timers["capture"]) == 1

    def test_multiple_timer_entries(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        for _ in range(5):
            start = logger.timer_start("vlm")
            logger.timer_end("vlm", start)
        assert len(logger._timers["vlm"]) == 5


class TestActivityLoggerStats:
    def test_get_stats_runtime(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        stats = logger.get_stats()
        assert stats["runtime_seconds"] >= 0

    def test_get_stats_counters(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("CAPTURE", "acquired", level="INFO")
        stats = logger.get_stats()
        assert stats["counters"]["CAPTURE.acquired"] == 1

    def test_get_stats_timers(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        start = logger.timer_start("op")
        logger.timer_end("op", start)
        stats = logger.get_stats()
        assert "op" in stats["timers"]
        assert stats["timers"]["op"]["count"] == 1
        assert stats["timers"]["op"]["avg_ms"] >= 0

    def test_get_stats_error_count(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        logger.log("DB", "fail", level="ERROR", detail="boom")
        stats = logger.get_stats()
        assert stats["error_count"] == 1

    def test_get_stats_recent_errors_capped(self, tmp_path):
        logger = ActivityLogger(log_dir=str(tmp_path), console=False, file=False)
        for i in range(10):
            logger.log("DB", f"err_{i}", level="ERROR", detail=f"d{i}")
        stats = logger.get_stats()
        assert len(stats["recent_errors"]) == 5

    def test_print_session_summary_runs(self, tmp_path, capsys):
        logger = ActivityLogger(log_dir=str(tmp_path), console=True, file=False)
        logger.log("CAPTURE", "acquired")
        start = logger.timer_start("op")
        logger.timer_end("op", start)
        logger.log("DB", "fail", level="ERROR", detail="oops")
        logger.print_session_summary()
        captured = capsys.readouterr()
        assert "SESSION SUMMARY" in captured.out
        assert "Runtime:" in captured.out


class TestGetLogger:
    def test_get_logger_returns_instance(self):
        import core.activity_log as mod
        old = mod._logger
        mod._logger = None
        try:
            logger = get_logger()
            assert isinstance(logger, ActivityLogger)
        finally:
            mod._logger = old

    def test_get_logger_singleton(self):
        import core.activity_log as mod
        old = mod._logger
        mod._logger = None
        try:
            a = get_logger()
            b = get_logger()
            assert a is b
        finally:
            mod._logger = old


# ============================================================
# AGENT FACTORY TESTS
# ============================================================

from core.agent_factory import (
    AgentCapability,
    AgentSpec,
    AgentInstance,
    AgentRegistry,
    AgentFactory,
)


class TestAgentCapability:
    def test_all_capabilities_have_string_values(self):
        for cap in AgentCapability:
            assert isinstance(cap.value, str)

    def test_known_capabilities(self):
        expected = {"reasoning", "planning", "code_execution", "web_navigation",
                    "data_analysis", "vision", "tool_use", "critique", "synthesis"}
        actual = {c.value for c in AgentCapability}
        assert actual == expected


class TestAgentSpec:
    def test_defaults(self):
        spec = AgentSpec(
            role="test",
            system_prompt="test prompt",
            capabilities=[AgentCapability.REASONING],
        )
        assert spec.preferred_backend == "qwen3:8b"
        assert spec.max_tokens == 2048
        assert spec.temperature == 0.7
        assert spec.tool_bindings == []
        assert spec.can_write is False

    def test_custom_values(self):
        spec = AgentSpec(
            role="writer",
            system_prompt="write stuff",
            capabilities=[AgentCapability.TOOL_USE],
            preferred_backend="gpt-4",
            max_tokens=4096,
            temperature=0.9,
            can_write=True,
        )
        assert spec.role == "writer"
        assert spec.preferred_backend == "gpt-4"
        assert spec.can_write is True


class TestAgentInstance:
    def test_age_seconds(self):
        instance = AgentInstance(
            id="a1", spec=AgentSpec(role="r", system_prompt="p", capabilities=[]),
            memory_namespace="ns",
        )
        time.sleep(0.05)
        assert instance.age_seconds >= 0.04

    def test_to_dict(self):
        spec = AgentSpec(role="reasoner", system_prompt="p", capabilities=[])
        instance = AgentInstance(id="a1", spec=spec, memory_namespace="ns_test")
        d = instance.to_dict()
        assert d["id"] == "a1"
        assert d["role"] == "reasoner"
        assert d["namespace"] == "ns_test"
        assert d["status"] == "idle"
        assert d["executions"] == 0
        assert d["tokens_used"] == 0

    def test_default_status_idle(self):
        spec = AgentSpec(role="r", system_prompt="p", capabilities=[])
        instance = AgentInstance(id="a1", spec=spec, memory_namespace="ns")
        assert instance.status == "idle"


class TestAgentRegistry:
    def test_default_roles_registered(self):
        reg = AgentRegistry()
        expected_roles = {"reasoner", "planner", "specialist", "validator",
                          "tool_executor", "proposer", "critic", "judge",
                          "code_specialist", "finance_specialist", "web_specialist",
                          "system_specialist", "analysis_specialist"}
        actual = set(reg.list_roles())
        assert expected_roles.issubset(actual)

    def test_has_role(self):
        reg = AgentRegistry()
        assert reg.has_role("reasoner") is True
        assert reg.has_role("nonexistent") is False

    def test_get_returns_spec(self):
        reg = AgentRegistry()
        spec = reg.get("reasoner")
        assert spec is not None
        assert spec.role == "reasoner"
        assert AgentCapability.REASONING in spec.capabilities

    def test_get_missing_returns_none(self):
        reg = AgentRegistry()
        assert reg.get("missing_role") is None

    def test_register_custom_role(self):
        reg = AgentRegistry()
        custom = AgentSpec(
            role="custom_agent",
            system_prompt="I am custom",
            capabilities=[AgentCapability.VISION],
        )
        reg.register(custom)
        assert reg.has_role("custom_agent")
        assert reg.get("custom_agent").role == "custom_agent"

    def test_register_overwrites_existing(self):
        reg = AgentRegistry()
        new_spec = AgentSpec(
            role="reasoner",
            system_prompt="NEW prompt",
            capabilities=[AgentCapability.PLANNING],
        )
        reg.register(new_spec)
        assert reg.get("reasoner").system_prompt == "NEW prompt"

    def test_validator_low_temperature(self):
        reg = AgentRegistry()
        spec = reg.get("validator")
        assert spec.temperature <= 0.2

    def test_tool_executor_can_write(self):
        reg = AgentRegistry()
        spec = reg.get("tool_executor")
        assert spec.can_write is True


class TestAgentFactory:
    def test_create_agent_known_role(self):
        factory = AgentFactory()
        agent = factory.create_agent("reasoner")
        assert agent.spec.role == "reasoner"
        assert agent.status == "idle"
        assert agent.id.startswith("agent_")

    def test_create_agent_unknown_role_creates_generic(self):
        factory = AgentFactory()
        agent = factory.create_agent("imaginary_role")
        assert agent.spec.role == "imaginary_role"
        assert AgentCapability.REASONING in agent.spec.capabilities

    def test_create_agent_backend_override(self):
        factory = AgentFactory()
        agent = factory.create_agent("reasoner", backend_override="gpt-4o")
        assert agent.spec.preferred_backend == "gpt-4o"
        assert agent.spec.role == "reasoner"

    def test_create_agent_custom_namespace(self):
        factory = AgentFactory()
        agent = factory.create_agent("planner", memory_namespace="custom_ns")
        assert agent.memory_namespace == "custom_ns"

    def test_create_agent_auto_namespace(self):
        factory = AgentFactory()
        agent = factory.create_agent("planner")
        assert "planner" in agent.memory_namespace

    def test_create_agent_increments_counter(self):
        factory = AgentFactory()
        a1 = factory.create_agent("reasoner")
        a2 = factory.create_agent("planner")
        assert a1.id != a2.id
        assert factory._total_created == 2

    def test_create_agent_tracked_in_active(self):
        factory = AgentFactory()
        agent = factory.create_agent("reasoner")
        assert agent.id in factory._active_agents

    def test_create_team(self):
        factory = AgentFactory()
        team = factory.create_team(["reasoner", "planner", "validator"])
        assert len(team) == 3
        roles = [a.spec.role for a in team]
        assert "reasoner" in roles
        assert "planner" in roles
        assert "validator" in roles

    def test_create_team_shared_namespace(self):
        factory = AgentFactory()
        team = factory.create_team(["reasoner", "critic"], shared_namespace="project_x")
        for agent in team:
            assert agent.memory_namespace.startswith("project_x/")

    def test_destroy_agent(self):
        factory = AgentFactory()
        agent = factory.create_agent("reasoner")
        agent_id = agent.id
        factory.destroy_agent(agent)
        assert agent.status == "destroyed"
        assert agent_id not in factory._active_agents
        assert factory._total_destroyed == 1

    def test_destroy_team(self):
        factory = AgentFactory()
        team = factory.create_team(["reasoner", "planner"])
        factory.destroy_team(team)
        assert len(factory.get_active_agents()) == 0
        assert factory._total_destroyed == 2

    def test_get_active_agents(self):
        factory = AgentFactory()
        factory.create_agent("reasoner")
        factory.create_agent("planner")
        active = factory.get_active_agents()
        assert len(active) == 2

    def test_stats(self):
        factory = AgentFactory()
        factory.create_agent("reasoner")
        factory.create_agent("planner")
        a3 = factory.create_agent("critic")
        factory.destroy_agent(a3)
        stats = factory.stats
        assert stats["active"] == 2
        assert stats["total_created"] == 3
        assert stats["total_destroyed"] == 1
        assert "reasoner" in stats["active_roles"]
        assert "planner" in stats["active_roles"]
        assert "critic" not in stats["active_roles"]

    def test_destroy_already_destroyed_is_safe(self):
        factory = AgentFactory()
        agent = factory.create_agent("reasoner")
        factory.destroy_agent(agent)
        factory.destroy_agent(agent)  # should not raise
        assert factory._total_destroyed == 1  # only counted once


# ============================================================
# SCREEN ANALYZER TESTS
# ============================================================

from core.analyzer import ScreenAnalyzer, AnalysisResult, SCREEN_ANALYSIS_PROMPT, QUICK_ANALYSIS_PROMPT


class TestAnalysisResult:
    def test_dataclass_fields(self):
        result = AnalysisResult(
            description="Test desc",
            ocr_text="Hello world",
            active_app="VS Code",
            activity_type="coding",
            confidence=0.95,
            analysis_ms=150.0,
            model_used="minicpm-v",
            raw_response="raw",
        )
        assert result.description == "Test desc"
        assert result.ocr_text == "Hello world"
        assert result.active_app == "VS Code"
        assert result.activity_type == "coding"
        assert result.confidence == 0.95
        assert result.analysis_ms == 150.0


class TestScreenAnalyzerInit:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_default_params(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        assert analyzer.model == "minicpm-v"
        assert analyzer.fallback_model == "llava:7b"
        assert analyzer.ollama_host == "http://localhost:11434"
        assert analyzer.max_tokens == 512
        assert analyzer.timeout == 60

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_custom_params(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer(
            model="llava:13b",
            fallback_model="moondream",
            ollama_host="http://remote:11434",
            max_tokens=1024,
            timeout=120,
        )
        assert analyzer.model == "llava:13b"
        assert analyzer.fallback_model == "moondream"
        assert analyzer.max_tokens == 1024


class TestScreenAnalyzerModelSelection:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_primary_model_preferred(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer(model="minicpm-v")
        analyzer._available_models = {"minicpm-v", "llava"}
        assert analyzer._get_model() == "minicpm-v"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_fallback_when_primary_missing(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer(model="minicpm-v", fallback_model="llava:7b")
        analyzer._available_models = {"llava"}
        assert analyzer._get_model() == "llava:7b"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_any_vision_model_fallback(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer(model="minicpm-v", fallback_model="llava:7b")
        analyzer._available_models = {"moondream"}
        assert analyzer._get_model() == "moondream"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_no_model_returns_none(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        analyzer._available_models = {"some-text-model"}
        assert analyzer._get_model() is None

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_is_available_true(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        analyzer._available_models = {"minicpm-v"}
        assert analyzer.is_available is True

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_is_available_false(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        analyzer._available_models = set()
        assert analyzer.is_available is False


class TestScreenAnalyzerParseResponse:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_parse_json_response(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = json.dumps({
            "description": "User editing Python code",
            "active_app": "VS Code",
            "activity_type": "coding",
            "ocr_text": "def hello():",
        })
        result = analyzer._parse_response(resp, 100.0, "minicpm-v")
        assert result.description == "User editing Python code"
        assert result.active_app == "VS Code"
        assert result.activity_type == "coding"
        assert result.ocr_text == "def hello():"
        assert result.analysis_ms == 100.0

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_parse_plain_text_response(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "The user is browsing a website in Chrome browser"
        result = analyzer._parse_response(resp, 200.0, "moondream")
        assert result.description == resp
        assert result.model_used == "moondream"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_auto_detect_app_from_description(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "The user is using VS Code to write Python code"
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.active_app == "VS Code"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_auto_detect_chrome(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "Chrome browser showing Google search results"
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.active_app == "Chrome"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_auto_detect_activity_coding(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "A programming IDE with a function definition visible"
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.activity_type == "coding"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_auto_detect_activity_browsing(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "A web browser with a search page open"
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.activity_type == "browsing"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_auto_detect_activity_terminal(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = "A terminal window running a shell command"
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.activity_type == "terminal"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_empty_description_low_confidence(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        result = analyzer._parse_response("", 0.0, "test")
        assert result.confidence == 0.1

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_json_embedded_in_text(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = 'Here is the analysis: {"description": "Coding in VS Code", "active_app": "VS Code", "activity_type": "coding"} done.'
        result = analyzer._parse_response(resp, 75.0, "minicpm-v")
        assert result.description == "Coding in VS Code"
        assert result.active_app == "VS Code"

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_ocr_text_fallback_from_visible_text(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        resp = json.dumps({
            "description": "Testing",
            "active_app": "Notepad",
            "visible_text": "Hello from notepad",
        })
        result = analyzer._parse_response(resp, 50.0, "test")
        assert result.ocr_text == "Hello from notepad"


class TestScreenAnalyzerFallback:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_fallback_analyze(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), "red")
            result = analyzer._fallback_analyze(img)
            assert result.confidence == 0.1
            assert result.model_used == "none"
            assert result.active_app == "unknown"
            assert result.activity_type == "other"
        except ImportError:
            pytest.skip("PIL not available")


class TestScreenAnalyzerImageConversion:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_image_to_base64(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), "blue")
            b64 = analyzer._image_to_base64(img)
            assert isinstance(b64, str)
            assert len(b64) > 0
            # Verify it's valid base64
            import base64
            decoded = base64.b64decode(b64)
            assert len(decoded) > 0
        except ImportError:
            pytest.skip("PIL not available")

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_image_to_base64_resizes_large(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        try:
            from PIL import Image
            img = Image.new("RGB", (4000, 3000), "green")
            b64 = analyzer._image_to_base64(img, max_size=512)
            assert isinstance(b64, str)
            assert len(b64) > 0
        except ImportError:
            pytest.skip("PIL not available")

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_image_to_base64_small_image_unchanged(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        try:
            from PIL import Image
            img = Image.new("RGB", (200, 200), "white")
            b64 = analyzer._image_to_base64(img, max_size=1024)
            assert isinstance(b64, str)
        except ImportError:
            pytest.skip("PIL not available")


class TestScreenAnalyzerAnalyze:
    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    def test_analyze_no_model_returns_fallback(self, mock_check):
        mock_check.return_value = None
        analyzer = ScreenAnalyzer()
        analyzer._available_models = set()
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), "red")
            result = analyzer.analyze(img)
            assert result is not None
            assert result.confidence == 0.1
            assert result.model_used == "none"
        except ImportError:
            pytest.skip("PIL not available")

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    @patch("core.analyzer.ScreenAnalyzer._call_ollama")
    def test_analyze_success(self, mock_call, mock_check):
        mock_check.return_value = None
        mock_call.return_value = json.dumps({
            "description": "User coding in VS Code",
            "active_app": "VS Code",
            "activity_type": "coding",
            "ocr_text": "import os",
        })
        analyzer = ScreenAnalyzer()
        analyzer._available_models = {"minicpm-v"}
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), "blue")
            result = analyzer.analyze(img, detailed=True)
            assert result.description == "User coding in VS Code"
            assert result.active_app == "VS Code"
            assert mock_call.called
        except ImportError:
            pytest.skip("PIL not available")

    @patch("core.analyzer.ScreenAnalyzer._check_ollama")
    @patch("core.analyzer.ScreenAnalyzer._call_ollama")
    def test_analyze_exception_returns_fallback(self, mock_call, mock_check):
        mock_check.return_value = None
        mock_call.side_effect = Exception("Connection refused")
        analyzer = ScreenAnalyzer()
        analyzer._available_models = {"minicpm-v"}
        try:
            from PIL import Image
            img = Image.new("RGB", (100, 100), "red")
            result = analyzer.analyze(img)
            assert result is not None
            assert result.confidence == 0.1
        except ImportError:
            pytest.skip("PIL not available")


# ============================================================
# PROMPTS TESTS
# ============================================================

class TestPrompts:
    def test_screen_analysis_prompt_not_empty(self):
        assert len(SCREEN_ANALYSIS_PROMPT) > 50

    def test_quick_analysis_prompt_not_empty(self):
        assert len(QUICK_ANALYSIS_PROMPT) > 20

    def test_prompts_different(self):
        assert SCREEN_ANALYSIS_PROMPT != QUICK_ANALYSIS_PROMPT
