"""tests/test_security_audit.py -- Tests for skynet_security_audit.py"""

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_security_audit import (
    AuditResult,
    audit_dispatch_pipeline,
    audit_bus_messages,
    audit_worker_registry,
    audit_config_files,
    full_audit,
    _is_vscode_hwnd,
    _load_json,
    BUS_MAX_PAYLOAD_BYTES,
    BUS_SUSPICIOUS_PATTERNS,
)


class TestAuditResult(unittest.TestCase):
    def test_ok_increments_passed(self):
        r = AuditResult()
        r.ok("test_check", "detail")
        self.assertEqual(r.passed, 1)
        self.assertEqual(r.failed, 0)

    def test_fail_increments_failed(self):
        r = AuditResult()
        r.fail("test_check", "detail")
        self.assertEqual(r.failed, 1)

    def test_warn_increments_warnings(self):
        r = AuditResult()
        r.warn("test_check", "detail")
        self.assertEqual(r.warnings, 1)

    def test_crit_increments_critical(self):
        r = AuditResult()
        r.crit("test_check", "detail")
        self.assertEqual(r.critical, 1)

    def test_to_dict_structure(self):
        r = AuditResult()
        r.ok("c1")
        r.fail("c2")
        r.warn("c3")
        r.crit("c4")
        d = r.to_dict()
        self.assertEqual(d["passed"], 1)
        self.assertEqual(d["failed"], 1)
        self.assertEqual(d["warnings"], 1)
        self.assertEqual(d["critical"], 1)
        self.assertEqual(d["total"], 4)
        self.assertEqual(len(d["details"]), 4)

    def test_details_have_correct_status(self):
        r = AuditResult()
        r.ok("c1", "ok_detail")
        r.fail("c2", "fail_detail")
        d = r.to_dict()
        self.assertEqual(d["details"][0]["status"], "PASS")
        self.assertEqual(d["details"][1]["status"], "FAIL")


class TestLoadJson(unittest.TestCase):
    def test_valid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value"}, f)
            f.flush()
            path = Path(f.name)
        try:
            result = _load_json(path)
            self.assertEqual(result, {"key": "value"})
        finally:
            os.unlink(path)

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json{{{")
            f.flush()
            path = Path(f.name)
        try:
            result = _load_json(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_missing_file(self):
        result = _load_json(Path("nonexistent_file.json"))
        self.assertIsNone(result)


class TestIsVscodeHwnd(unittest.TestCase):
    def test_zero_hwnd_is_invalid(self):
        valid, pid, proc, title = _is_vscode_hwnd(0)
        self.assertFalse(valid)

    def test_bogus_hwnd_is_invalid(self):
        valid, pid, proc, title = _is_vscode_hwnd(999999999)
        self.assertFalse(valid)


class TestAuditDispatchPipeline(unittest.TestCase):
    def test_runs_without_error(self):
        result = audit_dispatch_pipeline()
        self.assertIsInstance(result, AuditResult)
        self.assertGreater(result.passed + result.failed + result.warnings + result.critical, 0)

    def test_checks_validate_hwnd(self):
        result = audit_dispatch_pipeline()
        checks = [d["check"] for d in result.to_dict()["details"]]
        self.assertIn("dispatch_hwnd_validation", checks)

    def test_checks_clipboard_pattern(self):
        result = audit_dispatch_pipeline()
        checks = [d["check"] for d in result.to_dict()["details"]]
        self.assertIn("clipboard_save_restore", checks)

    def test_checks_self_dispatch_guard(self):
        result = audit_dispatch_pipeline()
        checks = [d["check"] for d in result.to_dict()["details"]]
        self.assertIn("self_dispatch_guard", checks)


class TestAuditBusMessages(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_empty_bus(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = audit_bus_messages(10)
        self.assertIsInstance(result, AuditResult)
        self.assertGreaterEqual(result.passed, 1)

    @patch("urllib.request.urlopen")
    def test_suspicious_content_detected(self, mock_urlopen):
        msgs = [{"sender": "alpha", "topic": "test", "content": "eval(dangerous_code)"}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(msgs).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = audit_bus_messages(10)
        warns = [d for d in result.to_dict()["details"] if d["status"] == "WARN"]
        suspicious = [w for w in warns if "suspicious" in w["check"].lower()
                      or "injection" in w.get("detail", "").lower()]
        self.assertGreater(len(suspicious), 0)

    @patch("urllib.request.urlopen")
    def test_oversized_payload_detected(self, mock_urlopen):
        big_content = "x" * (BUS_MAX_PAYLOAD_BYTES + 1000)
        msgs = [{"sender": "alpha", "topic": "test", "content": big_content}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(msgs).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = audit_bus_messages(10)
        d = result.to_dict()
        warn_checks = [det["check"] for det in d["details"] if det["status"] == "WARN"]
        self.assertIn("bus_oversized_payloads", warn_checks)

    @patch("urllib.request.urlopen")
    def test_unknown_sender_detected(self, mock_urlopen):
        msgs = [{"sender": "evil_hacker", "topic": "test", "content": "hello"}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(msgs).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = audit_bus_messages(10)
        d = result.to_dict()
        warn_checks = [det["check"] for det in d["details"] if det["status"] == "WARN"]
        self.assertIn("bus_unknown_senders", warn_checks)

    @patch("urllib.request.urlopen")
    def test_bus_unreachable(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = audit_bus_messages(10)
        d = result.to_dict()
        self.assertGreater(d["warnings"], 0)


class TestAuditWorkerRegistry(unittest.TestCase):
    def test_runs_without_crash(self):
        result = audit_worker_registry()
        self.assertIsInstance(result, AuditResult)

    @patch("tools.skynet_security_audit.WORKERS_FILE")
    def test_missing_file(self, mock_path):
        mock_path.exists.return_value = False
        result = audit_worker_registry()
        self.assertGreater(result.critical, 0)

    def test_detects_duplicate_names(self):
        workers = [
            {"name": "alpha", "hwnd": 1, "x": 0, "y": 0, "w": 500, "h": 500},
            {"name": "alpha", "hwnd": 2, "x": 0, "y": 0, "w": 500, "h": 500},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"workers": workers}, f)
            path = Path(f.name)
        try:
            with patch("tools.skynet_security_audit.WORKERS_FILE", path):
                result = audit_worker_registry()
                fails = [d for d in result.to_dict()["details"]
                         if d["status"] == "FAIL" and "duplicate" in d["check"].lower()]
                self.assertGreater(len(fails), 0)
        finally:
            os.unlink(path)

    def test_detects_zero_hwnd(self):
        workers = [{"name": "test", "hwnd": 0, "x": 0, "y": 0, "w": 500, "h": 500}]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"workers": workers}, f)
            path = Path(f.name)
        try:
            with patch("tools.skynet_security_audit.WORKERS_FILE", path):
                result = audit_worker_registry()
                fails = [d for d in result.to_dict()["details"]
                         if d["status"] == "FAIL" and "hwnd" in d["check"].lower()]
                self.assertGreater(len(fails), 0)
        finally:
            os.unlink(path)


class TestAuditConfigFiles(unittest.TestCase):
    def test_runs_without_crash(self):
        result = audit_config_files()
        self.assertIsInstance(result, AuditResult)

    def test_checks_brain_config(self):
        result = audit_config_files()
        checks = [d["check"] for d in result.to_dict()["details"]]
        brain_checks = [c for c in checks if "brain" in c.lower() or "config" in c.lower()]
        self.assertGreater(len(brain_checks), 0)


class TestFullAudit(unittest.TestCase):
    @patch("tools.skynet_security_audit.audit_bus_messages")
    def test_returns_dict(self, mock_bus):
        mock_bus.return_value = AuditResult()
        result = full_audit()
        self.assertIsInstance(result, dict)
        self.assertIn("passed", result)
        self.assertIn("failed", result)
        self.assertIn("components", result)

    @patch("tools.skynet_security_audit.audit_bus_messages")
    def test_has_all_components(self, mock_bus):
        mock_bus.return_value = AuditResult()
        result = full_audit()
        self.assertIn("dispatch", result["components"])
        self.assertIn("bus", result["components"])
        self.assertIn("registry", result["components"])
        self.assertIn("config", result["components"])

    @patch("tools.skynet_security_audit.audit_bus_messages")
    def test_auto_fix_flag(self, mock_bus):
        mock_bus.return_value = AuditResult()
        result = full_audit(auto_fix=True)
        self.assertIsInstance(result, dict)


class TestSuspiciousPatterns(unittest.TestCase):
    def test_patterns_are_valid_regex(self):
        import re
        for pattern in BUS_SUSPICIOUS_PATTERNS:
            try:
                re.compile(pattern)
            except re.error:
                self.fail(f"Invalid regex pattern: {pattern}")


if __name__ == "__main__":
    unittest.main()
