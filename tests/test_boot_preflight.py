"""
Tests for tools/boot_preflight.py — Pre-flight validation for Skynet boot.

Covers: JSONC parsing, boolean setting checks, port scanning, workers.json
validation, ghost-type fix detection, guard bypass checks, empty file
detection, and the run_preflight() orchestrator.

# signed: delta
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools import boot_preflight as bp


# ── parse_jsonc ──────────────────────────────────────────────────────────


class TestParseJsonc(unittest.TestCase):
    """Tests for parse_jsonc() — VS Code-style JSON with comments."""

    def test_plain_json(self):
        data = bp.parse_jsonc('{"a": 1, "b": true}')
        self.assertEqual(data, {"a": 1, "b": True})

    def test_line_comments(self):
        raw = '{\n  // this is a comment\n  "key": "value"\n}'
        data = bp.parse_jsonc(raw)
        self.assertEqual(data["key"], "value")

    def test_block_comments(self):
        raw = '{\n  /* block */\n  "x": 42\n}'
        data = bp.parse_jsonc(raw)
        self.assertEqual(data["x"], 42)

    def test_trailing_commas(self):
        raw = '{"a": 1, "b": 2,}'
        data = bp.parse_jsonc(raw)
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_mixed(self):
        raw = '{\n  // comment\n  "key": true, /* inline */ "other": false,\n}'
        data = bp.parse_jsonc(raw)
        self.assertTrue(data["key"])
        self.assertFalse(data["other"])


# ── check_port ───────────────────────────────────────────────────────────


class TestCheckPort(unittest.TestCase):

    @patch("socket.socket")
    def test_port_open(self, mock_sock_cls):
        """Open port → passed=True."""
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect.return_value = None

        result = bp.check_port(8420, "Backend")
        self.assertTrue(result["passed"])
        self.assertEqual(result["name"], "Backend")

    @patch("socket.socket")
    def test_port_closed(self, mock_sock_cls):
        """Closed port → passed=False."""
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("refused")

        result = bp.check_port(9999, "Nothing")
        self.assertFalse(result["passed"])

    @patch("socket.socket")
    def test_backend_port_is_critical(self, mock_sock_cls):
        """Port 8420 has CRITICAL severity."""
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect.side_effect = OSError

        result = bp.check_port(8420, "Backend")
        self.assertEqual(result["severity"], "CRITICAL")

    @patch("socket.socket")
    def test_non_backend_port_is_high(self, mock_sock_cls):
        """Non-8420 port has HIGH severity."""
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect.side_effect = OSError

        result = bp.check_port(8421, "GOD Console")
        self.assertEqual(result["severity"], "HIGH")


# ── check_workers_json ───────────────────────────────────────────────────


class TestCheckWorkersJson(unittest.TestCase):

    @patch("ctypes.windll.user32.IsWindow", return_value=1)
    @patch("os.path.exists", return_value=True)
    def test_all_alive(self, _exists, _is):
        workers = {"workers": [
            {"name": "alpha", "hwnd": 100},
            {"name": "beta", "hwnd": 200},
        ]}
        with patch("builtins.open", mock_open(read_data=json.dumps(workers))):
            result = bp.check_workers_json()
        self.assertTrue(result["passed"])
        self.assertEqual(result["alive"], 2)
        self.assertEqual(result["dead"], 0)

    @patch("ctypes.windll.user32.IsWindow", return_value=0)
    @patch("os.path.exists", return_value=True)
    def test_all_dead(self, _exists, _is):
        workers = {"workers": [{"name": "gamma", "hwnd": 300}]}
        with patch("builtins.open", mock_open(read_data=json.dumps(workers))):
            result = bp.check_workers_json()
        self.assertEqual(result["dead"], 1)
        self.assertEqual(result["alive"], 0)

    @patch("os.path.exists", return_value=False)
    def test_no_workers_json(self, _exists):
        result = bp.check_workers_json()
        self.assertTrue(result["passed"])
        self.assertIn("fresh boot", result.get("note", ""))

    @patch("os.path.exists", return_value=True)
    def test_corrupt_json(self, _exists):
        with patch("builtins.open", mock_open(read_data="NOT JSON")):
            result = bp.check_workers_json()
        self.assertFalse(result["passed"])
        self.assertIn("error", result)

    @patch("ctypes.windll.user32.IsWindow", return_value=1)
    @patch("os.path.exists", return_value=True)
    def test_duplicate_hwnds(self, _exists, _is):
        """Duplicate HWNDs → passed=False."""
        workers = {"workers": [
            {"name": "alpha", "hwnd": 100},
            {"name": "beta", "hwnd": 100},  # duplicate
        ]}
        with patch("builtins.open", mock_open(read_data=json.dumps(workers))):
            result = bp.check_workers_json()
        self.assertFalse(result["passed"])
        self.assertTrue(result["duplicates"])


# ── check_ghost_type_fixes ───────────────────────────────────────────────


class TestCheckGhostTypeFixes(unittest.TestCase):

    @patch("os.path.exists", return_value=True)
    def test_all_fixes_present(self, _exists):
        content = "HardwareEnter screen reader Chrome_RenderWidgetHostHWND FindRender"
        with patch("builtins.open", mock_open(read_data=content)):
            result = bp.check_ghost_type_fixes()
        self.assertTrue(result["passed"])

    @patch("os.path.exists", return_value=True)
    def test_missing_fix(self, _exists):
        content = "HardwareEnter screen reader"  # missing Chrome_RenderWidgetHostHWND
        with patch("builtins.open", mock_open(read_data=content)):
            result = bp.check_ghost_type_fixes()
        self.assertFalse(result["passed"])

    @patch("os.path.exists", return_value=False)
    def test_missing_dispatch_file(self, _exists):
        result = bp.check_ghost_type_fixes()
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "CRITICAL")


# ── check_guard_bypass_fixes ─────────────────────────────────────────────


class TestCheckGuardBypassFixes(unittest.TestCase):

    @patch("os.path.exists", return_value=True)
    def test_all_fixes_present(self, _exists):
        content = "Escape key press Ctrl+Backspace to clear"
        with patch("builtins.open", mock_open(read_data=content)):
            result = bp.check_guard_bypass_fixes()
        self.assertTrue(result["passed"])

    @patch("os.path.exists", return_value=False)
    def test_missing_file(self, _exists):
        result = bp.check_guard_bypass_fixes()
        self.assertFalse(result["passed"])


# ── check_empty_files ────────────────────────────────────────────────────


class TestCheckEmptyFiles(unittest.TestCase):

    @patch("os.path.getsize", return_value=0)
    @patch("os.walk")
    def test_detects_empty(self, mock_walk, _size):
        mock_walk.return_value = [(bp.DATA, [], ["stale.md"])]
        result = bp.check_empty_files()
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["empty_files"]), 1)

    @patch("os.walk")
    def test_no_empty(self, mock_walk):
        mock_walk.return_value = [(bp.DATA, [], [])]
        result = bp.check_empty_files()
        self.assertTrue(result["passed"])


# ── check_boolean_setting ────────────────────────────────────────────────


class TestCheckBooleanSetting(unittest.TestCase):

    @patch("os.path.exists", return_value=True)
    def test_pass_when_correct(self, _exists):
        settings = json.dumps({"mykey": True})
        with patch("builtins.open", mock_open(read_data=settings)):
            result = bp.check_boolean_setting(
                key="mykey", desired=True, prereq_id="T1",
                name="Test", severity="HIGH"
            )
        self.assertTrue(result["passed"])

    @patch("os.path.exists", return_value=True)
    def test_fail_when_wrong(self, _exists):
        settings = json.dumps({"mykey": False})
        with patch("builtins.open", mock_open(read_data=settings)):
            result = bp.check_boolean_setting(
                key="mykey", desired=True, prereq_id="T2",
                name="Test", severity="HIGH"
            )
        self.assertFalse(result["passed"])

    @patch("os.path.exists", return_value=False)
    def test_missing_files(self, _exists):
        """Both settings files missing → MISSING status."""
        result = bp.check_boolean_setting(
            key="x", desired=True, prereq_id="T3",
            name="Test", severity="LOW"
        )
        for d in result["details"]:
            self.assertEqual(d["status"], "MISSING")


# ── run_preflight ────────────────────────────────────────────────────────


class TestRunPreflight(unittest.TestCase):

    @patch("tools.boot_preflight.check_empty_files")
    @patch("tools.boot_preflight.check_workers_json")
    @patch("tools.boot_preflight.check_guard_bypass_fixes")
    @patch("tools.boot_preflight.check_ghost_type_fixes")
    @patch("tools.boot_preflight.check_port")
    @patch("tools.boot_preflight.check_chat_restore_setting")
    @patch("tools.boot_preflight.check_isolation_option")
    def test_all_pass(self, m_iso, m_chat, m_port, m_ghost, m_guard, m_wkr, m_empty):
        """All checks pass → exit 0."""
        for m in (m_iso, m_chat, m_ghost, m_guard, m_wkr, m_empty):
            m.return_value = {"passed": True, "name": "X", "severity": "LOW"}
        m_port.return_value = {"passed": True, "name": "Port", "severity": "HIGH"}

        code = bp.run_preflight(quiet=True)
        self.assertEqual(code, 0)

    @patch("tools.boot_preflight.check_empty_files")
    @patch("tools.boot_preflight.check_workers_json")
    @patch("tools.boot_preflight.check_guard_bypass_fixes")
    @patch("tools.boot_preflight.check_ghost_type_fixes")
    @patch("tools.boot_preflight.check_port")
    @patch("tools.boot_preflight.check_chat_restore_setting")
    @patch("tools.boot_preflight.check_isolation_option")
    def test_critical_fail(self, m_iso, m_chat, m_port, m_ghost, m_guard, m_wkr, m_empty):
        """Critical failure → exit 1."""
        m_iso.return_value = {"passed": False, "name": "Iso", "severity": "CRITICAL"}
        for m in (m_chat, m_ghost, m_guard, m_wkr, m_empty):
            m.return_value = {"passed": True, "name": "X", "severity": "LOW"}
        m_port.return_value = {"passed": True, "name": "Port", "severity": "HIGH"}

        code = bp.run_preflight(quiet=True)
        self.assertEqual(code, 1)

    @patch("tools.boot_preflight.check_empty_files")
    @patch("tools.boot_preflight.check_workers_json")
    @patch("tools.boot_preflight.check_guard_bypass_fixes")
    @patch("tools.boot_preflight.check_ghost_type_fixes")
    @patch("tools.boot_preflight.check_port")
    @patch("tools.boot_preflight.check_chat_restore_setting")
    @patch("tools.boot_preflight.check_isolation_option")
    def test_json_output(self, m_iso, m_chat, m_port, m_ghost, m_guard, m_wkr, m_empty):
        """--json outputs valid JSON."""
        for m in (m_iso, m_chat, m_ghost, m_guard, m_wkr, m_empty):
            m.return_value = {"passed": True, "name": "X", "severity": "LOW"}
        m_port.return_value = {"passed": True, "name": "Port", "severity": "HIGH"}

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            bp.run_preflight(json_output=True)

        output = buf.getvalue()
        data = json.loads(output)
        self.assertTrue(data["passed"])
        self.assertIsInstance(data["results"], list)


# ── set_jsonc_bool_key ───────────────────────────────────────────────────


class TestSetJsoncBoolKey(unittest.TestCase):

    def test_replaces_existing_key(self):
        """Existing boolean key is replaced in-place."""
        raw = '{\n  "mykey": false\n}'
        m = mock_open(read_data=raw)
        with patch("builtins.open", m):
            bp.set_jsonc_bool_key("fake.json", "mykey", True)

        written = "".join(c.args[0] for c in m().write.call_args_list)
        self.assertIn('"mykey"', written)
        self.assertIn("true", written)

    def test_adds_missing_key(self):
        """Missing key is appended before closing brace."""
        raw = '{\n  "other": 1\n}'
        m = mock_open(read_data=raw)
        with patch("builtins.open", m):
            bp.set_jsonc_bool_key("fake.json", "newkey", False)

        written = "".join(c.args[0] for c in m().write.call_args_list)
        self.assertIn('"newkey": false', written)


# ── load_boot_config ─────────────────────────────────────────────────────


class TestLoadBootConfig(unittest.TestCase):

    def test_loads_valid(self):
        config = {"workers": 4}
        with patch("builtins.open", mock_open(read_data=json.dumps(config))):
            result = bp.load_boot_config()
        self.assertEqual(result["workers"], 4)

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_missing_file(self, _open):
        result = bp.load_boot_config()
        self.assertEqual(result, {})

    @patch("builtins.open", mock_open(read_data="NOT JSON"))
    def test_corrupt_file(self, *_):
        result = bp.load_boot_config()
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
