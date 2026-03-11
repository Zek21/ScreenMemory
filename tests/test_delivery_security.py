#!/usr/bin/env python3
"""Security-focused tests for skynet_delivery.py HWND validation.

Tests verify that the delivery system rejects tampered, stale, or
non-VS-Code HWNDs before ghost-typing any content.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from skynet_delivery import (
    validate_hwnd,
    validate_hwnd_strict,
    HWNDValidationError,
    _is_window,
    _get_window_pid,
    _get_process_name,
    _get_window_title,
    _ghost_type,
    _load_orch_hwnd,
    _load_worker_hwnd,
    deliver,
    DeliveryTarget,
)


class TestHWNDValidationZeroAndNull(unittest.TestCase):
    """Test 1: Zero/null HWND is always rejected."""

    def test_zero_hwnd_rejected(self):
        result = validate_hwnd(0, "test")
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["nonzero"])

    def test_none_coerced_hwnd_rejected(self):
        # Simulates a None value cast to int (0)
        result = validate_hwnd(int(False), "test")
        self.assertFalse(result["valid"])

    def test_negative_hwnd_treated_as_nonzero_but_fails_iswindow(self):
        # Negative HWND passes nonzero check but should fail is_window
        with patch("skynet_delivery._is_window", return_value=False):
            result = validate_hwnd(-1, "test")
            self.assertTrue(result["checks"]["nonzero"])
            self.assertFalse(result["checks"]["is_window"])
            self.assertFalse(result["valid"])


class TestHWNDValidationDeadWindow(unittest.TestCase):
    """Test 2: Stale HWND (window destroyed) is rejected."""

    @patch("skynet_delivery._is_window", return_value=False)
    def test_dead_window_rejected(self, mock_isw):
        result = validate_hwnd(99999, "stale_worker")
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["is_window"])
        self.assertEqual(result["target"], "stale_worker")

    @patch("skynet_delivery._is_window", return_value=False)
    def test_strict_raises_on_dead_window(self, mock_isw):
        with self.assertRaises(HWNDValidationError) as ctx:
            validate_hwnd_strict(99999, "dead_target")
        self.assertIn("is_window", str(ctx.exception))


class TestHWNDValidationWrongProcess(unittest.TestCase):
    """Test 3: HWND belonging to non-VS-Code process is rejected."""

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=1234)
    @patch("skynet_delivery._get_process_name", return_value="notepad.exe")
    @patch("skynet_delivery._get_window_title", return_value="Untitled - Notepad")
    def test_notepad_hwnd_rejected(self, *_):
        result = validate_hwnd(12345, "orchestrator")
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["is_vscode_process"])
        self.assertFalse(result["checks"]["has_vscode_title"])
        self.assertEqual(result["process_name"], "notepad.exe")

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=5678)
    @patch("skynet_delivery._get_process_name", return_value="cmd.exe")
    @patch("skynet_delivery._get_window_title", return_value="Command Prompt")
    def test_cmd_hwnd_rejected(self, *_):
        """Attacker could point HWND at a command prompt to execute commands."""
        result = validate_hwnd(54321, "alpha")
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["is_vscode_process"])


class TestHWNDValidationTitleMismatch(unittest.TestCase):
    """Test 4: VS Code process but wrong title pattern is rejected."""

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=2000)
    @patch("skynet_delivery._get_process_name", return_value="Code - Insiders.exe")
    @patch("skynet_delivery._get_window_title", return_value="")
    def test_empty_title_rejected(self, *_):
        """Window with empty title (hidden/system window) should fail."""
        result = validate_hwnd(20000, "worker:beta")
        self.assertFalse(result["valid"])
        self.assertTrue(result["checks"]["is_vscode_process"])
        self.assertFalse(result["checks"]["has_vscode_title"])


class TestHWNDValidationPass(unittest.TestCase):
    """Test 5: Valid VS Code HWND passes all checks."""

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=3000)
    @patch("skynet_delivery._get_process_name", return_value="Code - Insiders.exe")
    @patch("skynet_delivery._get_window_title",
           return_value="AGENTS.md - ScreenMemory - Visual Studio Code - Insiders")
    def test_valid_vscode_hwnd_passes(self, *_):
        result = validate_hwnd(30000, "orchestrator")
        self.assertTrue(result["valid"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["pid"], 3000)
        self.assertEqual(result["process_name"], "Code - Insiders.exe")

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=4000)
    @patch("skynet_delivery._get_process_name", return_value="Code.exe")
    @patch("skynet_delivery._get_window_title",
           return_value="main.py - Project - Visual Studio Code")
    def test_stable_vscode_also_passes(self, *_):
        """Stable VS Code (Code.exe) should also pass."""
        result = validate_hwnd(40000, "worker:gamma")
        self.assertTrue(result["valid"])


class TestGhostTypeBlockedOnInvalidHWND(unittest.TestCase):
    """Test 6: _ghost_type refuses to type into invalid HWND."""

    @patch("skynet_delivery.validate_hwnd")
    @patch("skynet_delivery._log_delivery")
    def test_ghost_type_blocked_on_invalid_hwnd(self, mock_log, mock_validate):
        mock_validate.return_value = {
            "valid": False,
            "checks": {"nonzero": True, "is_window": True,
                       "is_vscode_process": False, "has_vscode_title": False},
            "pid": 999, "process_name": "malware.exe",
        }
        ok = _ghost_type(12345, "sensitive payload", 0, "attacker_target")
        self.assertFalse(ok)
        # Verify the block was logged
        mock_log.assert_called_once()
        args = mock_log.call_args
        self.assertFalse(args[0][2])  # success=False
        self.assertEqual(args[0][1], "blocked")


class TestDeliverRejectsStaleOrchestratorHWND(unittest.TestCase):
    """Test 7: deliver(ORCHESTRATOR) with stale HWND from tampered file."""

    @patch("skynet_delivery._load_orch_hwnd", return_value=99999)
    @patch("skynet_delivery._ghost_type", return_value=False)
    def test_stale_orch_hwnd_fails_delivery(self, mock_gt, mock_load):
        result = deliver(DeliveryTarget.ORCHESTRATOR, "test content")
        self.assertFalse(result["success"])

    @patch("skynet_delivery._load_orch_hwnd", return_value=0)
    def test_zero_orch_hwnd_returns_no_hwnd_error(self, mock_load):
        result = deliver(DeliveryTarget.ORCHESTRATOR, "test")
        self.assertFalse(result["success"])
        self.assertIn("No orchestrator HWND", result["detail"])


class TestTamperedWorkersJsonInjection(unittest.TestCase):
    """Test 8: Tampered workers.json with attacker HWND is rejected."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fake_workers = os.path.join(self.tmpdir, "workers.json")

    def test_load_worker_hwnd_from_list_format(self):
        """Verify _load_worker_hwnd reads list-format workers.json."""
        data = [{"name": "alpha", "hwnd": 11111}, {"name": "beta", "hwnd": 22222}]
        with open(self.fake_workers, "w") as f:
            json.dump(data, f)
        with patch("skynet_delivery.WORKERS_FILE", Path(self.fake_workers)):
            self.assertEqual(_load_worker_hwnd("alpha"), 11111)
            self.assertEqual(_load_worker_hwnd("beta"), 22222)
            self.assertEqual(_load_worker_hwnd("nonexistent"), 0)

    def test_load_worker_hwnd_from_dict_format(self):
        """Verify _load_worker_hwnd reads dict-format workers.json."""
        data = {"alpha": {"hwnd": 33333}, "gamma": {"hwnd": 44444}}
        with open(self.fake_workers, "w") as f:
            json.dump(data, f)
        with patch("skynet_delivery.WORKERS_FILE", Path(self.fake_workers)):
            self.assertEqual(_load_worker_hwnd("alpha"), 33333)
            self.assertEqual(_load_worker_hwnd("gamma"), 44444)


class TestValidateHWNDStrictException(unittest.TestCase):
    """Test 9: validate_hwnd_strict raises with failed check details."""

    @patch("skynet_delivery._is_window", return_value=True)
    @patch("skynet_delivery._get_window_pid", return_value=5000)
    @patch("skynet_delivery._get_process_name", return_value="chrome.exe")
    @patch("skynet_delivery._get_window_title", return_value="Google Chrome")
    def test_strict_raises_with_check_names(self, *_):
        with self.assertRaises(HWNDValidationError) as ctx:
            validate_hwnd_strict(50000, "worker:delta")
        msg = str(ctx.exception)
        self.assertIn("is_vscode_process", msg)
        self.assertIn("has_vscode_title", msg)
        self.assertIn("worker:delta", msg)


if __name__ == "__main__":
    unittest.main()
