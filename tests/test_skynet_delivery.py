"""Tests for tools/skynet_delivery.py — unified delivery registry.

Covers: DeliveryTarget/DeliveryMethod enums, HWND validation, worker/orchestrator
HWND loading, consultant state, ghost_type pipeline, bus posting, delivery routing,
log persistence, scoring helpers, routing registry, elevated digest, pull_pending_work,
edge cases and error handling.
"""  # signed: alpha

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_delivery import (
    DeliveryTarget,
    DeliveryMethod,
    HWNDValidationError,
    validate_hwnd,
    validate_hwnd_strict,
    _read_json_file,
    _load_worker_hwnd,
    _consultant_state_file,
    _load_consultant_state,
    _log_delivery,
    _sanitize_target_label,
    _score_title_and_content,
    _deliver_to_worker_hwnd,
    _deliver_to_orch_hwnd,
    _deliver_to_consultant_ghost_type,
    _deliver_to_consultant_bridge,
    deliver,
    deliver_to_orchestrator,
    deliver_to_consultant,
    deliver_consultant_result,
    deliver_elevated_report,
    deliver_self_invoke,
    deliver_elevated_digest,
    pull_pending_work,
    get_routing_info,
    is_routable,
    list_routable_targets,
    ROUTING_REGISTRY,
    _format_elevated_digest,
    _ghost_type,
    _bus_post,
    _json_post,
)


# ===========================================================================
# 1. Enums
# ===========================================================================

class TestDeliveryTarget(unittest.TestCase):
    """Test DeliveryTarget enum values."""

    def test_enum_values(self):
        self.assertEqual(DeliveryTarget.ORCHESTRATOR.value, "orchestrator")
        self.assertEqual(DeliveryTarget.WORKER.value, "worker")
        self.assertEqual(DeliveryTarget.CONSULTANT.value, "consultant")
        self.assertEqual(DeliveryTarget.BUS.value, "bus")

    def test_enum_members(self):
        members = list(DeliveryTarget)
        self.assertEqual(len(members), 4)


class TestDeliveryMethod(unittest.TestCase):
    """Test DeliveryMethod enum values."""

    def test_enum_values(self):
        self.assertEqual(DeliveryMethod.DIRECT_PROMPT.value, "direct_prompt")
        self.assertEqual(DeliveryMethod.CONSULTANT_BRIDGE.value, "consultant_bridge")
        self.assertEqual(DeliveryMethod.BUS_POST.value, "bus_post")
        self.assertEqual(DeliveryMethod.HYBRID.value, "hybrid")


# ===========================================================================
# 2. HWND Validation
# ===========================================================================

class TestValidateHwnd(unittest.TestCase):
    """Test validate_hwnd security layer."""

    @patch("tools.skynet_delivery._check_vscode_title")
    @patch("tools.skynet_delivery._check_vscode_process")
    @patch("tools.skynet_delivery._is_window")
    def test_valid_hwnd(self, mock_is_window, mock_proc, mock_title):
        mock_is_window.return_value = True
        mock_proc.return_value = (1234, "Code - Insiders.exe", True)
        mock_title.return_value = ("ScreenMemory - Visual Studio Code", True)
        result = validate_hwnd(12345, "worker:alpha")
        self.assertTrue(result["valid"])
        self.assertEqual(result["hwnd"], 12345)
        self.assertEqual(result["pid"], 1234)
        self.assertTrue(all(result["checks"].values()))

    def test_zero_hwnd(self):
        result = validate_hwnd(0, "orchestrator")
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["nonzero"])

    @patch("tools.skynet_delivery._is_window")
    def test_dead_hwnd(self, mock_is_window):
        mock_is_window.return_value = False
        result = validate_hwnd(99999)
        self.assertFalse(result["valid"])
        self.assertTrue(result["checks"]["nonzero"])
        self.assertFalse(result["checks"]["is_window"])

    @patch("tools.skynet_delivery._check_vscode_title")
    @patch("tools.skynet_delivery._check_vscode_process")
    @patch("tools.skynet_delivery._is_window")
    def test_non_vscode_process(self, mock_is_window, mock_proc, mock_title):
        mock_is_window.return_value = True
        mock_proc.return_value = (1234, "notepad.exe", False)
        mock_title.return_value = ("Notepad", False)
        result = validate_hwnd(11111)
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["is_vscode_process"])


class TestValidateHwndStrict(unittest.TestCase):
    """Test validate_hwnd_strict raises on failure."""

    def test_raises_on_zero_hwnd(self):
        with self.assertRaises(HWNDValidationError):
            validate_hwnd_strict(0, "test")

    @patch("tools.skynet_delivery._check_vscode_title")
    @patch("tools.skynet_delivery._check_vscode_process")
    @patch("tools.skynet_delivery._is_window")
    def test_passes_on_valid(self, mock_is_window, mock_proc, mock_title):
        mock_is_window.return_value = True
        mock_proc.return_value = (100, "Code - Insiders.exe", True)
        mock_title.return_value = ("Visual Studio Code", True)
        result = validate_hwnd_strict(555)
        self.assertTrue(result["valid"])


# ===========================================================================
# 3. JSON File Reading
# ===========================================================================

class TestReadJsonFile(unittest.TestCase):
    """Test _read_json_file helper."""

    def test_reads_valid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump({"key": "value"}, f)
            f.flush()
            path = Path(f.name)
        try:
            result = _read_json_file(path)
            self.assertEqual(result["key"], "value")
        finally:
            path.unlink()

    def test_missing_file_returns_empty(self):
        result = _read_json_file(Path("nonexistent_file_xyz.json"))
        self.assertEqual(result, {})

    def test_non_dict_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
            f.flush()
            path = Path(f.name)
        try:
            result = _read_json_file(path)
            self.assertEqual(result, {})
        finally:
            path.unlink()


# ===========================================================================
# 4. Worker HWND Loading
# ===========================================================================

class TestLoadWorkerHwnd(unittest.TestCase):
    """Test _load_worker_hwnd from workers.json."""

    @patch("tools.skynet_delivery.WORKERS_FILE")
    def test_finds_worker_in_list_format(self, mock_file):
        mock_file.read_text.return_value = json.dumps({
            "workers": [
                {"name": "alpha", "hwnd": 12345},
                {"name": "beta", "hwnd": 67890},
            ]
        })
        self.assertEqual(_load_worker_hwnd("alpha"), 12345)
        self.assertEqual(_load_worker_hwnd("beta"), 67890)

    @patch("tools.skynet_delivery.WORKERS_FILE")
    def test_worker_not_found(self, mock_file):
        mock_file.read_text.return_value = json.dumps({
            "workers": [{"name": "alpha", "hwnd": 111}]
        })
        self.assertEqual(_load_worker_hwnd("gamma"), 0)

    @patch("tools.skynet_delivery.WORKERS_FILE")
    def test_handles_dict_format(self, mock_file):
        mock_file.read_text.return_value = json.dumps({
            "alpha": {"hwnd": 444},
            "beta": {"hwnd": 555},
        })
        self.assertEqual(_load_worker_hwnd("alpha"), 444)

    @patch("tools.skynet_delivery.WORKERS_FILE")
    def test_file_read_error(self, mock_file):
        mock_file.read_text.side_effect = Exception("corrupted")
        self.assertEqual(_load_worker_hwnd("alpha"), 0)


# ===========================================================================
# 5. Consultant State
# ===========================================================================

class TestConsultantState(unittest.TestCase):
    """Test consultant state file helpers."""

    def test_state_file_path_consultant(self):
        path = _consultant_state_file("consultant")
        self.assertTrue(str(path).endswith("consultant_state.json"))

    def test_state_file_path_gemini(self):
        path = _consultant_state_file("gemini_consultant")
        self.assertTrue(str(path).endswith("gemini_consultant_state.json"))

    @patch("tools.skynet_delivery._consultant_state_file")
    def test_load_state_missing_file(self, mock_path):
        mock_path.return_value = Path("nonexistent_consultant.json")
        result = _load_consultant_state("test_consultant")
        self.assertEqual(result, {})

    @patch("tools.skynet_delivery._consultant_state_file")
    def test_load_state_valid(self, mock_path):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump({"hwnd": 999, "live": True, "api_url": "http://localhost:8422"}, f)
            f.flush()
            mock_path.return_value = Path(f.name)
        try:
            result = _load_consultant_state("test")
            self.assertEqual(result["hwnd"], 999)
            self.assertTrue(result["live"])
        finally:
            Path(f.name).unlink()


# ===========================================================================
# 6. Delivery Logging
# ===========================================================================

class TestLogDelivery(unittest.TestCase):
    """Test _log_delivery persistence."""

    def test_creates_log_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "delivery_log.json"
            with patch("tools.skynet_delivery.DELIVERY_LOG", log_path):
                _log_delivery("test_target", "direct_prompt", True, 15.3, "ok")
            self.assertTrue(log_path.exists())
            data = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["target"], "test_target")
            self.assertTrue(data[0]["success"])
            self.assertEqual(data[0]["latency_ms"], 15.3)

    def test_appends_to_existing_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "delivery_log.json"
            log_path.write_text("[]", encoding="utf-8")
            with patch("tools.skynet_delivery.DELIVERY_LOG", log_path):
                _log_delivery("t1", "m1", True, 1.0)
                _log_delivery("t2", "m2", False, 2.0)
            data = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)

    def test_truncates_to_200_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "delivery_log.json"
            existing = [{"target": f"t{i}", "method": "m", "success": True,
                         "latency_ms": 0, "detail": "", "timestamp": ""}
                        for i in range(199)]
            log_path.write_text(json.dumps(existing), encoding="utf-8")
            with patch("tools.skynet_delivery.DELIVERY_LOG", log_path):
                _log_delivery("new", "m", True, 0)
                _log_delivery("newer", "m", True, 0)
            data = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 200)
            self.assertEqual(data[-1]["target"], "newer")


# ===========================================================================
# 7. Sanitize Target Label
# ===========================================================================

class TestSanitizeTargetLabel(unittest.TestCase):
    """Test _sanitize_target_label."""

    def test_simple_label(self):
        self.assertEqual(_sanitize_target_label("worker:alpha", 100), "worker_alpha")

    def test_empty_label_uses_hwnd(self):
        result = _sanitize_target_label("", 42)
        self.assertEqual(result, "hwnd_42")

    def test_special_chars_replaced(self):
        result = _sanitize_target_label("a/b\\c<d>e", 1)
        self.assertNotIn("/", result)
        self.assertNotIn("\\", result)
        self.assertNotIn("<", result)

    def test_long_label_truncated(self):
        long_label = "x" * 200
        result = _sanitize_target_label(long_label, 1)
        self.assertLessEqual(len(result), 64)


# ===========================================================================
# 8. Score Title and Content
# ===========================================================================

class TestScoreTitleAndContent(unittest.TestCase):
    """Test _score_title_and_content scoring logic."""

    def test_no_markers_zero_score(self):
        score, strong = _score_title_and_content("random title", "random content")
        self.assertEqual(score, 0)
        self.assertFalse(strong)

    def test_session_marker_in_title(self):
        score, strong = _score_title_and_content("skynet-start session", "")
        self.assertGreater(score, 0)
        self.assertTrue(strong)

    def test_identity_marker_in_content(self):
        score, strong = _score_title_and_content("", "skynet orchestrator live")
        self.assertGreater(score, 0)
        self.assertTrue(strong)

    def test_title_hint_weak_signal(self):
        score, strong = _score_title_and_content("skynet project", "nothing")
        # "skynet" is in _ORCH_TITLE_HINTS → +1
        self.assertGreaterEqual(score, 1)


# ===========================================================================
# 9. Ghost Type Pipeline
# ===========================================================================

class TestGhostType(unittest.TestCase):
    """Test _ghost_type delivery with mocked dependencies."""

    @patch("tools.skynet_delivery._capture_prefire_screenshot")
    @patch("tools.skynet_delivery.validate_hwnd")
    def test_validation_failure_blocks(self, mock_validate, mock_screenshot):
        mock_validate.return_value = {
            "valid": False, "checks": {"nonzero": True, "is_window": False},
            "pid": 0, "process_name": ""
        }
        with patch("tools.skynet_delivery._log_delivery"):
            result = _ghost_type(12345, "hello", 0, "test")
        self.assertFalse(result)

    @patch("tools.skynet_delivery._capture_prefire_screenshot")
    @patch("tools.skynet_delivery.validate_hwnd")
    def test_missing_screenshot_blocks(self, mock_validate, mock_screenshot):
        mock_validate.return_value = {"valid": True, "checks": {}}
        mock_screenshot.return_value = ""  # no screenshot
        with patch("tools.skynet_delivery._log_delivery"):
            result = _ghost_type(12345, "hello", 0, "test")
        self.assertFalse(result)

    @patch("skynet_dispatch.ghost_type_to_worker")
    @patch("tools.skynet_delivery._capture_prefire_screenshot")
    @patch("tools.skynet_delivery.validate_hwnd")
    def test_successful_delivery(self, mock_validate, mock_screenshot, mock_gt):
        mock_validate.return_value = {"valid": True, "checks": {}}
        mock_screenshot.return_value = "/path/to/screenshot.png"
        mock_gt.return_value = True
        with patch("tools.skynet_delivery._log_delivery"):
            result = _ghost_type(12345, "task text", 100, "worker:alpha")
        self.assertTrue(result)
        mock_gt.assert_called_once()

    @patch("tools.skynet_delivery._capture_prefire_screenshot")
    @patch("tools.skynet_delivery.validate_hwnd")
    def test_ghost_type_import_error(self, mock_validate, mock_screenshot):
        mock_validate.return_value = {"valid": True, "checks": {}}
        mock_screenshot.return_value = "/path.png"
        with patch("tools.skynet_delivery._log_delivery"):
            with patch.dict("sys.modules", {"skynet_dispatch": None}):
                # Force ImportError on ghost_type_to_worker import
                result = _ghost_type(12345, "text", 0)
        self.assertFalse(result)


# ===========================================================================
# 10. Bus Post
# ===========================================================================

class TestBusPost(unittest.TestCase):
    """Test _bus_post with mocked backends."""

    @patch("tools.skynet_spam_guard.guarded_publish")
    def test_spam_guard_success(self, mock_gp):
        mock_gp.return_value = {"allowed": True}
        result = _bus_post("alpha", "orchestrator", "result", "done")
        self.assertTrue(result)

    @patch("tools.skynet_spam_guard.guarded_publish")
    def test_spam_guard_blocked(self, mock_gp):
        mock_gp.return_value = {"allowed": False}
        result = _bus_post("alpha", "orchestrator", "result", "done")
        self.assertFalse(result)

    def test_all_fallbacks_fail(self):
        with patch.dict("sys.modules", {
            "tools.skynet_spam_guard": None,
            "shared.bus": None
        }):
            with patch("urllib.request.urlopen", side_effect=Exception("no server")):
                # Everything fails → returns False
                result = _bus_post("s", "t", "ty", "c")
                self.assertFalse(result)


# ===========================================================================
# 11. Worker Delivery
# ===========================================================================

class TestDeliverToWorkerHwnd(unittest.TestCase):
    """Test _deliver_to_worker_hwnd."""

    def test_no_worker_name(self):
        result = _deliver_to_worker_hwnd("content", None)
        self.assertFalse(result["success"])
        self.assertIn("No worker_name", result["detail"])

    @patch("tools.skynet_delivery._ghost_type")
    @patch("tools.skynet_delivery._load_orch_hwnd")
    @patch("tools.skynet_delivery._load_worker_hwnd")
    def test_no_hwnd(self, mock_load, mock_orch, mock_gt):
        mock_load.return_value = 0
        result = _deliver_to_worker_hwnd("content", "alpha")
        self.assertFalse(result["success"])
        self.assertIn("No HWND", result["detail"])

    @patch("tools.skynet_delivery._ghost_type")
    @patch("tools.skynet_delivery._load_orch_hwnd")
    @patch("tools.skynet_delivery._load_worker_hwnd")
    def test_successful_delivery(self, mock_load, mock_orch, mock_gt):
        mock_load.return_value = 11111
        mock_orch.return_value = 22222
        mock_gt.return_value = True
        result = _deliver_to_worker_hwnd("task", "beta")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "direct_prompt")
        self.assertIn("beta", result["target"])


# ===========================================================================
# 12. Orchestrator Delivery
# ===========================================================================

class TestDeliverToOrchHwnd(unittest.TestCase):
    """Test _deliver_to_orch_hwnd."""

    @patch("tools.skynet_delivery._load_orch_hwnd")
    def test_no_orch_hwnd(self, mock_load):
        mock_load.return_value = 0
        result = _deliver_to_orch_hwnd("content")
        self.assertFalse(result["success"])
        self.assertIn("No orchestrator HWND", result["detail"])

    @patch("tools.skynet_delivery._ghost_type")
    @patch("tools.skynet_delivery._focus_shared_orchestrator_pane")
    @patch("tools.skynet_delivery._resolve_orchestrator_render_hwnd")
    @patch("tools.skynet_delivery._load_orch_hwnd")
    def test_successful_delivery(self, mock_load, mock_render, mock_focus, mock_gt):
        mock_load.return_value = 33333
        mock_render.return_value = 44444
        mock_focus.return_value = True
        mock_gt.return_value = True
        result = _deliver_to_orch_hwnd("status update")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "direct_prompt")


# ===========================================================================
# 13. Consultant Ghost Type Delivery
# ===========================================================================

class TestDeliverToConsultantGhostType(unittest.TestCase):
    """Test _deliver_to_consultant_ghost_type."""

    @patch("tools.skynet_delivery._consultant_hwnd_is_valid")
    @patch("tools.skynet_delivery._load_consultant_state")
    def test_no_valid_hwnd(self, mock_state, mock_valid):
        mock_state.return_value = {"hwnd": 0}
        mock_valid.return_value = False
        result = _deliver_to_consultant_ghost_type("prompt", "consultant")
        self.assertFalse(result["success"])
        self.assertEqual(result["delivery_status"], "failed")

    @patch("tools.skynet_delivery._ghost_type")
    @patch("tools.skynet_delivery._load_orch_hwnd")
    @patch("tools.skynet_delivery._consultant_hwnd_is_valid")
    @patch("tools.skynet_delivery._load_consultant_state")
    def test_successful_ghost_type(self, mock_state, mock_valid, mock_orch, mock_gt):
        mock_state.return_value = {"hwnd": 55555}
        mock_valid.return_value = True
        mock_orch.return_value = 22222
        mock_gt.return_value = True
        result = _deliver_to_consultant_ghost_type("prompt", "gemini_consultant")
        self.assertTrue(result["success"])
        self.assertEqual(result["delivery_status"], "delivered")


# ===========================================================================
# 14. Consultant Bridge Delivery (Full Pipeline)
# ===========================================================================

class TestDeliverToConsultantBridge(unittest.TestCase):
    """Test _deliver_to_consultant_bridge — ghost_type primary, bridge fallback."""

    def test_no_consultant_id(self):
        result = _deliver_to_consultant_bridge("text", None, "sender", "type", False)
        self.assertFalse(result["success"])
        self.assertIn("No consultant_id", result["detail"])

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery._json_post")
    @patch("tools.skynet_delivery._load_consultant_state")
    @patch("tools.skynet_delivery._deliver_to_consultant_ghost_type")
    def test_ghost_type_success_is_delivered(self, mock_gt, mock_state, mock_json, mock_bus):
        mock_gt.return_value = {
            "success": True, "delivery_status": "delivered",
            "target": "consultant:consultant"
        }
        mock_state.return_value = {"api_url": "", "live": False, "accepts_prompts": False}
        mock_json.return_value = None
        mock_bus.return_value = True
        result = _deliver_to_consultant_bridge("prompt", "consultant", "orch", "directive", False)
        self.assertTrue(result["success"])
        self.assertEqual(result["delivery_status"], "delivered")

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery._json_post")
    @patch("tools.skynet_delivery._load_consultant_state")
    @patch("tools.skynet_delivery._deliver_to_consultant_ghost_type")
    def test_bridge_queued_not_success(self, mock_gt, mock_state, mock_json, mock_bus):
        mock_gt.return_value = {"success": False, "delivery_status": "failed"}
        mock_state.return_value = {
            "api_url": "http://localhost:8422/consultants",
            "live": True, "accepts_prompts": True
        }
        mock_json.return_value = {"status": "queued", "prompt": {"id": "p123"}}
        mock_bus.return_value = True
        result = _deliver_to_consultant_bridge("prompt", "consultant", "orch", "directive", False)
        # TRUTH: queued != delivered → success=False
        self.assertFalse(result["success"])
        self.assertEqual(result["delivery_status"], "queued")

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery._json_post")
    @patch("tools.skynet_delivery._load_consultant_state")
    @patch("tools.skynet_delivery._deliver_to_consultant_ghost_type")
    def test_bridge_delivered_is_success(self, mock_gt, mock_state, mock_json, mock_bus):
        mock_gt.return_value = {"success": False, "delivery_status": "failed"}
        mock_state.return_value = {
            "api_url": "http://localhost:8422/consultants",
            "live": True, "accepts_prompts": True
        }
        mock_json.return_value = {"status": "delivered", "prompt": {"id": "p456"}}
        mock_bus.return_value = True
        result = _deliver_to_consultant_bridge("prompt", "consultant", "orch", "directive", False)
        self.assertTrue(result["success"])
        self.assertEqual(result["delivery_status"], "delivered")

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery._json_post")
    @patch("tools.skynet_delivery._load_consultant_state")
    @patch("tools.skynet_delivery._deliver_to_consultant_ghost_type")
    def test_both_failed(self, mock_gt, mock_state, mock_json, mock_bus):
        mock_gt.return_value = {"success": False, "delivery_status": "failed"}
        mock_state.return_value = {"api_url": "", "live": False, "accepts_prompts": False}
        mock_json.return_value = None
        mock_bus.return_value = False
        result = _deliver_to_consultant_bridge("prompt", "consultant", "orch", "directive", False)
        self.assertFalse(result["success"])
        self.assertEqual(result["delivery_status"], "failed")
        self.assertEqual(result["method"], "failed")


# ===========================================================================
# 15. Unified deliver() Entry Point
# ===========================================================================

class TestDeliver(unittest.TestCase):
    """Test deliver() routing to correct backend."""

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._deliver_to_orch_hwnd")
    def test_orchestrator_target(self, mock_orch, mock_log):
        mock_orch.return_value = {"target": "orchestrator", "method": "direct_prompt",
                                  "success": True, "detail": "ok"}
        result = deliver(DeliveryTarget.ORCHESTRATOR, "hello")
        self.assertTrue(result["success"])
        self.assertIn("latency_ms", result)
        mock_orch.assert_called_once_with("hello")

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._deliver_to_worker_hwnd")
    def test_worker_target(self, mock_worker, mock_log):
        mock_worker.return_value = {"target": "worker:alpha", "method": "direct_prompt",
                                    "success": True, "detail": "ok"}
        result = deliver(DeliveryTarget.WORKER, "task", worker_name="alpha")
        mock_worker.assert_called_once_with("task", "alpha")

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._deliver_to_consultant_bridge")
    def test_consultant_target(self, mock_consultant, mock_log):
        mock_consultant.return_value = {"target": "consultant:gemini", "method": "hybrid",
                                        "success": True, "detail": "ok"}
        result = deliver(DeliveryTarget.CONSULTANT, "query",
                        consultant_id="gemini_consultant")
        mock_consultant.assert_called_once()

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._bus_post")
    def test_bus_target(self, mock_bus, mock_log):
        mock_bus.return_value = True
        result = deliver(DeliveryTarget.BUS, "note", bus_topic="knowledge")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "bus_post")
        self.assertIn("knowledge", result["target"])

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._bus_post")
    def test_bus_default_topic(self, mock_bus, mock_log):
        mock_bus.return_value = True
        result = deliver(DeliveryTarget.BUS, "note")
        self.assertIn("general", result["target"])

    @patch("tools.skynet_delivery._log_delivery")
    def test_latency_measured(self, mock_log):
        with patch("tools.skynet_delivery._deliver_to_orch_hwnd") as mock_orch:
            mock_orch.return_value = {"target": "orch", "method": "m",
                                      "success": True, "detail": ""}
            result = deliver(DeliveryTarget.ORCHESTRATOR, "x")
        self.assertIn("latency_ms", result)
        self.assertIsInstance(result["latency_ms"], float)


# ===========================================================================
# 16. Convenience Wrappers
# ===========================================================================

class TestConvenienceWrappers(unittest.TestCase):
    """Test deliver_to_orchestrator, deliver_to_consultant, etc."""

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery.deliver")
    def test_deliver_to_orchestrator(self, mock_deliver, mock_bus):
        mock_deliver.return_value = {"success": True}
        result = deliver_to_orchestrator("status", sender="monitor")
        mock_deliver.assert_called_once_with(DeliveryTarget.ORCHESTRATOR, "status")
        mock_bus.assert_called_once()  # also_bus=True default

    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery.deliver")
    def test_deliver_to_orchestrator_no_bus(self, mock_deliver, mock_bus):
        mock_deliver.return_value = {"success": True}
        deliver_to_orchestrator("x", also_bus=False)
        mock_bus.assert_not_called()

    @patch("tools.skynet_delivery.deliver")
    def test_deliver_to_consultant(self, mock_deliver):
        mock_deliver.return_value = {"success": True}
        deliver_to_consultant("gemini_consultant", "question")
        mock_deliver.assert_called_once()
        call_kwargs = mock_deliver.call_args
        self.assertEqual(call_kwargs[1]["consultant_id"], "gemini_consultant")

    @patch("tools.skynet_delivery.deliver")
    @patch("tools.skynet_delivery._bus_post")
    def test_deliver_consultant_result(self, mock_bus, mock_deliver):
        mock_deliver.return_value = {"success": True}
        deliver_consultant_result("consultant", "analysis done")
        mock_bus.assert_called_once()
        # Content formatted with [CONSULTANT RESULT ...]
        call_args = mock_deliver.call_args
        self.assertIn("CONSULTANT RESULT", call_args[0][1])

    @patch("tools.skynet_delivery.deliver")
    @patch("tools.skynet_delivery._bus_post")
    def test_deliver_elevated_report(self, mock_bus, mock_deliver):
        mock_deliver.return_value = {"success": True}
        deliver_elevated_report("gate_1", "alpha", "important finding", ["beta", "gamma"])
        mock_bus.assert_called_once()
        call_args = mock_deliver.call_args
        self.assertIn("CONVENE-ELEVATED", call_args[0][1])


# ===========================================================================
# 17. Self-Invoke Delivery
# ===========================================================================

class TestDeliverSelfInvoke(unittest.TestCase):
    """Test deliver_self_invoke for idle worker wakeup."""

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._load_worker_hwnd")
    def test_no_hwnd(self, mock_load, mock_log):
        mock_load.return_value = 0
        result = deliver_self_invoke("alpha", "wake up")
        self.assertFalse(result["success"])
        self.assertIn("No HWND", result["detail"])
        self.assertIn("latency_ms", result)

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._bus_post")
    @patch("tools.skynet_delivery._ghost_type")
    @patch("tools.skynet_delivery._load_orch_hwnd")
    @patch("tools.skynet_delivery._load_worker_hwnd")
    def test_successful_invoke(self, mock_wh, mock_oh, mock_gt, mock_bus, mock_log):
        mock_wh.return_value = 11111
        mock_oh.return_value = 22222
        mock_gt.return_value = True
        mock_bus.return_value = True
        result = deliver_self_invoke("beta", "new task", sender="overseer")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "direct_prompt")
        mock_bus.assert_called_once()


# ===========================================================================
# 18. Elevated Digest
# ===========================================================================

class TestElevatedDigest(unittest.TestCase):
    """Test _format_elevated_digest and deliver_elevated_digest."""

    def test_format_empty_entries(self):
        bus_content, prompt = _format_elevated_digest([], 1800)
        self.assertIn("count=0", bus_content)

    def test_format_with_entries(self):
        entries = [
            {"gate_id": "g1", "proposer": "alpha", "report": "found bug",
             "voters": ["beta", "gamma"], "vote_total": 3, "vote_count": 2},
            {"gate_id": "g2", "proposer": "delta", "report": "performance issue",
             "voters": ["alpha"], "repeat_count": 2},
        ]
        bus_content, prompt = _format_elevated_digest(entries, 3600)
        self.assertIn("count=2", bus_content)
        self.assertIn("found bug", bus_content)
        self.assertIn("performance issue", prompt)

    @patch("tools.skynet_delivery.deliver")
    @patch("tools.skynet_delivery._bus_post")
    def test_deliver_empty_digest(self, mock_bus, mock_deliver):
        result = deliver_elevated_digest([])
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        mock_deliver.assert_not_called()

    @patch("tools.skynet_delivery.deliver")
    @patch("tools.skynet_delivery._bus_post")
    def test_deliver_with_entries(self, mock_bus, mock_deliver):
        mock_deliver.return_value = {"success": True, "method": "direct_prompt"}
        entries = [{"gate_id": "g1", "proposer": "alpha", "report": "test",
                     "voters": ["beta"]}]
        result = deliver_elevated_digest(entries)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["delivery_type"], "elevated_digest")
        mock_bus.assert_called_once()
        mock_deliver.assert_called_once()


# ===========================================================================
# 19. Pull Pending Work
# ===========================================================================

class TestPullPendingWork(unittest.TestCase):
    """Test pull_pending_work from bus + todos."""

    @patch("tools.skynet_delivery.DATA_DIR")
    @patch("urllib.request.urlopen")
    def test_no_work_returns_none(self, mock_urlopen, mock_data_dir):
        mock_urlopen.side_effect = Exception("no bus")
        mock_data_dir.__truediv__ = lambda self, x: Path("nonexistent_path") / x
        result = pull_pending_work("alpha")
        self.assertIsNone(result)

    def test_todos_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            todos_path = Path(tmpdir) / "todos.json"
            todos_path.write_text(json.dumps({
                "todos": [
                    {"status": "pending", "assignee": "alpha",
                     "task": "fix bug", "priority": 1},
                    {"status": "done", "assignee": "alpha",
                     "task": "old task"},
                ]
            }), encoding="utf-8")
            with patch("tools.skynet_delivery.DATA_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", side_effect=Exception("no bus")):
                    result = pull_pending_work("alpha")
            self.assertEqual(result, "fix bug")

    def test_directive_highest_priority(self):
        """Directives (priority=0) beat todo items (priority>=1)."""
        import io
        bus_response = json.dumps({
            "messages": [
                {"topic": "workers", "type": "directive", "route": "alpha",
                 "content": "urgent directive"},
            ]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = bus_response
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            todos_path = Path(tmpdir) / "todos.json"
            todos_path.write_text(json.dumps({
                "todos": [
                    {"status": "pending", "assignee": "alpha",
                     "task": "lower priority", "priority": 5},
                ]
            }), encoding="utf-8")
            with patch("tools.skynet_delivery.DATA_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", return_value=mock_resp):
                    result = pull_pending_work("alpha")
        self.assertEqual(result, "urgent directive")


# ===========================================================================
# 20. Routing Registry
# ===========================================================================

class TestRoutingRegistry(unittest.TestCase):
    """Test ROUTING_REGISTRY and routing helpers."""

    def test_all_workers_registered(self):
        for name in ["alpha", "beta", "gamma", "delta"]:
            self.assertIn(name, ROUTING_REGISTRY)
            info = ROUTING_REGISTRY[name]
            self.assertEqual(info["method"], DeliveryMethod.DIRECT_PROMPT)
            self.assertTrue(info["routable"])

    def test_orchestrator_registered(self):
        self.assertIn("orchestrator", ROUTING_REGISTRY)
        self.assertEqual(ROUTING_REGISTRY["orchestrator"]["hwnd_source"], "orchestrator.json")

    def test_consultants_registered(self):
        for name in ["consultant", "gemini_consultant"]:
            self.assertIn(name, ROUTING_REGISTRY)
            info = ROUTING_REGISTRY[name]
            self.assertEqual(info["fallback"], DeliveryMethod.CONSULTANT_BRIDGE)

    def test_get_routing_info_known(self):
        info = get_routing_info("alpha")
        self.assertTrue(info["routable"])
        self.assertEqual(info["hwnd_source"], "workers.json")

    def test_get_routing_info_unknown(self):
        info = get_routing_info("nonexistent")
        self.assertEqual(info, {})

    def test_is_routable_workers(self):
        # Workers are always routable (no consultant check needed)
        with patch("tools.skynet_delivery._load_consultant_state"):
            self.assertTrue(is_routable("alpha"))

    @patch("tools.skynet_delivery._consultant_hwnd_is_valid")
    @patch("tools.skynet_delivery._load_consultant_state")
    def test_is_routable_consultant_with_hwnd(self, mock_state, mock_valid):
        mock_state.return_value = {"live": False, "api_url": "", "accepts_prompts": False}
        mock_valid.return_value = True
        self.assertTrue(is_routable("consultant"))

    @patch("tools.skynet_delivery._consultant_hwnd_is_valid")
    @patch("tools.skynet_delivery._load_consultant_state")
    def test_is_routable_consultant_bridge_only(self, mock_state, mock_valid):
        mock_state.return_value = {"live": True, "api_url": "http://x", "accepts_prompts": True}
        mock_valid.return_value = False
        self.assertTrue(is_routable("consultant"))

    @patch("tools.skynet_delivery._consultant_hwnd_is_valid")
    @patch("tools.skynet_delivery._load_consultant_state")
    def test_not_routable_no_hwnd_no_bridge(self, mock_state, mock_valid):
        mock_state.return_value = {"live": False, "api_url": "", "accepts_prompts": False}
        mock_valid.return_value = False
        self.assertFalse(is_routable("consultant"))

    def test_registry_has_seven_entries(self):
        # orchestrator + 4 workers + 2 consultants = 7
        self.assertEqual(len(ROUTING_REGISTRY), 7)


# ===========================================================================
# 21. JSON Post Helper
# ===========================================================================

class TestJsonPost(unittest.TestCase):
    """Test _json_post HTTP helper."""

    def test_network_error_returns_none(self):
        result = _json_post("http://localhost:99999/fake", {"key": "val"}, timeout=1.0)
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_successful_post(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _json_post("http://localhost:8420/test", {"data": 1})
        self.assertEqual(result, {"status": "ok"})

    @patch("urllib.request.urlopen")
    def test_non_dict_response_returns_none(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([1, 2, 3]).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _json_post("http://localhost:8420/test", {})
        self.assertIsNone(result)


# ===========================================================================
# 22. Edge Cases
# ===========================================================================

class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error handling."""

    def test_empty_content_delivery(self):
        result = _deliver_to_worker_hwnd("", "alpha")
        # Should still process (empty content is valid, may fail at HWND level)
        self.assertIn("success", result)

    @patch("tools.skynet_delivery._log_delivery")
    @patch("tools.skynet_delivery._deliver_to_orch_hwnd")
    def test_deliver_returns_latency(self, mock_orch, mock_log):
        mock_orch.return_value = {"target": "orch", "method": "m",
                                  "success": False, "detail": ""}
        result = deliver(DeliveryTarget.ORCHESTRATOR, "x")
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_hwnd_validation_error_message(self):
        try:
            raise HWNDValidationError("test error message")
        except HWNDValidationError as e:
            self.assertIn("test error", str(e))

    def test_delivery_method_values_unique(self):
        values = [m.value for m in DeliveryMethod]
        self.assertEqual(len(values), len(set(values)))

    def test_delivery_target_values_unique(self):
        values = [t.value for t in DeliveryTarget]
        self.assertEqual(len(values), len(set(values)))


if __name__ == "__main__":
    unittest.main()
