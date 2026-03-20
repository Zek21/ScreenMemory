"""
Tests for tools/boot_cleanup.py — Pre-boot stale remnant scanner and cleaner.

Covers: PID file scanning, HWND liveness checks, orchestrator/consultant
state scanning, dispatch log staleness detection, full_scan(), clean(), and
print_report() output paths.

# signed: delta
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools import boot_cleanup as bc


# ── PID helpers ──────────────────────────────────────────────────────────


class TestPidAlive(unittest.TestCase):
    """Tests for _pid_alive()."""

    @patch("tools.boot_cleanup.os.kill")
    def test_pid_alive_with_os_kill(self, mock_kill):
        """os.kill(pid, 0) succeeds → alive."""
        # Force psutil to be unavailable
        with patch.dict("sys.modules", {"psutil": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                mock_kill.return_value = None
                self.assertTrue(bc._pid_alive(1234))

    @patch("tools.boot_cleanup.os.kill", side_effect=OSError)
    def test_pid_dead_with_os_kill(self, _kill):
        """os.kill raises OSError → dead."""
        with patch.dict("sys.modules", {"psutil": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                self.assertFalse(bc._pid_alive(9999))

    def test_pid_alive_with_psutil(self):
        """psutil.pid_exists returns True → alive."""
        mock_psutil = MagicMock()
        mock_psutil.pid_exists.return_value = True
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            self.assertTrue(bc._pid_alive(42))


class TestHwndAlive(unittest.TestCase):
    """Tests for _hwnd_alive()."""

    @patch("ctypes.windll.user32.IsWindow", return_value=1)
    def test_alive_hwnd(self, _is):
        self.assertTrue(bc._hwnd_alive(12345))

    @patch("ctypes.windll.user32.IsWindow", return_value=0)
    def test_dead_hwnd(self, _is):
        self.assertFalse(bc._hwnd_alive(0))


# ── scan_pid_files ───────────────────────────────────────────────────────


class TestScanPidFiles(unittest.TestCase):

    @patch("tools.boot_cleanup._pid_alive")
    @patch("tools.boot_cleanup.DATA")
    def test_alive_pid(self, mock_data, mock_alive):
        """Alive PID → stale=False."""
        pid_file = MagicMock(spec=Path)
        pid_file.name = "monitor.pid"
        pid_file.read_text.return_value = "1234"
        mock_data.glob.return_value = [pid_file]
        mock_alive.return_value = True

        results = bc.scan_pid_files()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["stale"])
        self.assertEqual(results[0]["pid"], 1234)

    @patch("tools.boot_cleanup._pid_alive")
    @patch("tools.boot_cleanup.DATA")
    def test_dead_pid(self, mock_data, mock_alive):
        """Dead PID → stale=True."""
        pid_file = MagicMock(spec=Path)
        pid_file.name = "sse.pid"
        pid_file.read_text.return_value = "9999"
        mock_data.glob.return_value = [pid_file]
        mock_alive.return_value = False

        results = bc.scan_pid_files()
        self.assertTrue(results[0]["stale"])

    @patch("tools.boot_cleanup.DATA")
    def test_corrupt_pid_file(self, mock_data):
        """Non-integer PID file → stale with error."""
        pid_file = MagicMock(spec=Path)
        pid_file.name = "bad.pid"
        pid_file.read_text.return_value = "not_a_number"
        mock_data.glob.return_value = [pid_file]

        results = bc.scan_pid_files()
        self.assertTrue(results[0]["stale"])
        self.assertIn("error", results[0])

    @patch("tools.boot_cleanup.DATA")
    def test_no_pid_files(self, mock_data):
        """No PID files → empty list."""
        mock_data.glob.return_value = []
        self.assertEqual(bc.scan_pid_files(), [])


# ── scan_worker_hwnds ───────────────────────────────────────────────────


class TestScanWorkerHwnds(unittest.TestCase):

    @patch("tools.boot_cleanup._hwnd_alive")
    @patch("tools.boot_cleanup.DATA")
    def test_alive_workers(self, mock_data, mock_alive):
        """Workers with alive HWNDs → stale=False."""
        workers_json = {"workers": [
            {"name": "alpha", "hwnd": 111},
            {"name": "beta", "hwnd": 222},
        ]}
        wf = MagicMock()
        wf.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=wf)
        mock_alive.return_value = True

        with patch("builtins.open", mock_open(read_data=json.dumps(workers_json))):
            results = bc.scan_worker_hwnds()

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertFalse(r["stale"])

    @patch("tools.boot_cleanup._hwnd_alive", return_value=False)
    @patch("tools.boot_cleanup.DATA")
    def test_dead_workers(self, mock_data, _alive):
        """Dead HWNDs → stale=True."""
        workers_json = {"workers": [{"name": "gamma", "hwnd": 333}]}
        wf = MagicMock()
        wf.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=wf)

        with patch("builtins.open", mock_open(read_data=json.dumps(workers_json))):
            results = bc.scan_worker_hwnds()

        self.assertTrue(results[0]["stale"])

    @patch("tools.boot_cleanup.DATA")
    def test_missing_workers_json(self, mock_data):
        """No workers.json → empty list."""
        wf = MagicMock()
        wf.exists.return_value = False
        mock_data.__truediv__ = MagicMock(return_value=wf)
        self.assertEqual(bc.scan_worker_hwnds(), [])

    @patch("tools.boot_cleanup._hwnd_alive")
    @patch("tools.boot_cleanup.DATA")
    def test_zero_hwnd_not_stale(self, mock_data, mock_alive):
        """Worker with hwnd=0 (unassigned) → stale=False."""
        workers_json = {"workers": [{"name": "delta", "hwnd": 0}]}
        wf = MagicMock()
        wf.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=wf)

        with patch("builtins.open", mock_open(read_data=json.dumps(workers_json))):
            results = bc.scan_worker_hwnds()

        self.assertFalse(results[0]["stale"])
        mock_alive.assert_not_called()


# ── scan_orchestrator ────────────────────────────────────────────────────


class TestScanOrchestrator(unittest.TestCase):

    @patch("tools.boot_cleanup._hwnd_alive", return_value=True)
    @patch("tools.boot_cleanup.DATA")
    def test_alive_orchestrator(self, mock_data, _alive):
        of = MagicMock()
        of.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=of)

        with patch("builtins.open", mock_open(read_data=json.dumps({"hwnd": 555}))):
            result = bc.scan_orchestrator()

        self.assertTrue(result["alive"])
        self.assertFalse(result["stale"])

    @patch("tools.boot_cleanup.DATA")
    def test_missing_orchestrator_json(self, mock_data):
        of = MagicMock()
        of.exists.return_value = False
        mock_data.__truediv__ = MagicMock(return_value=of)

        result = bc.scan_orchestrator()
        self.assertFalse(result["exists"])
        self.assertTrue(result["stale"])


# ── scan_dispatch_log ────────────────────────────────────────────────────


class TestScanDispatchLog(unittest.TestCase):

    @patch("tools.boot_cleanup.time.time", return_value=10000.0)
    @patch("tools.boot_cleanup.DATA")
    def test_stale_entries_detected(self, mock_data, _time):
        """Old entries without result → stale."""
        entries = [
            {"worker": "alpha", "task": "do stuff", "timestamp": 1.0, "result_received": False},
        ]
        dl = MagicMock()
        dl.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=dl)

        with patch("builtins.open", mock_open(read_data=json.dumps(entries))):
            result = bc.scan_dispatch_log(max_age_hours=1)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["worker"], "alpha")

    @patch("tools.boot_cleanup.time.time", return_value=10000.0)
    @patch("tools.boot_cleanup.DATA")
    def test_completed_entries_not_stale(self, mock_data, _time):
        """Entry with result_received=True → not stale."""
        entries = [
            {"worker": "beta", "task": "done", "timestamp": 1.0, "result_received": True},
        ]
        dl = MagicMock()
        dl.exists.return_value = True
        mock_data.__truediv__ = MagicMock(return_value=dl)

        with patch("builtins.open", mock_open(read_data=json.dumps(entries))):
            result = bc.scan_dispatch_log(max_age_hours=1)

        self.assertEqual(len(result), 0)

    @patch("tools.boot_cleanup.DATA")
    def test_missing_dispatch_log(self, mock_data):
        dl = MagicMock()
        dl.exists.return_value = False
        mock_data.__truediv__ = MagicMock(return_value=dl)
        self.assertEqual(bc.scan_dispatch_log(), [])


# ── full_scan & clean ────────────────────────────────────────────────────


class TestFullScan(unittest.TestCase):

    @patch("tools.boot_cleanup.scan_dispatch_log", return_value=[])
    @patch("tools.boot_cleanup.scan_consultant_state", return_value=[])
    @patch("tools.boot_cleanup.scan_orchestrator", return_value={"exists": True, "stale": False})
    @patch("tools.boot_cleanup.scan_worker_hwnds", return_value=[])
    @patch("tools.boot_cleanup.scan_pid_files", return_value=[])
    def test_full_scan_structure(self, *_):
        result = bc.full_scan()
        self.assertIn("pid_files", result)
        self.assertIn("workers", result)
        self.assertIn("orchestrator", result)
        self.assertIn("consultants", result)
        self.assertIn("stale_dispatches", result)


class TestClean(unittest.TestCase):

    def test_clean_stale_pid(self):
        """Stale PID files get unlinked."""
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        with patch.object(bc, "DATA") as mock_data:
            mock_data.__truediv__ = MagicMock(return_value=mock_path)
            scan = {
                "pid_files": [{"file": "dead.pid", "pid": 999, "stale": True}],
                "stale_dispatches": [],
            }
            cleaned = bc.clean(scan)
        self.assertEqual(cleaned, 1)
        mock_path.unlink.assert_called_once()

    def test_clean_nothing_when_all_alive(self):
        scan = {
            "pid_files": [{"file": "live.pid", "pid": 1, "stale": False}],
            "stale_dispatches": [],
        }
        self.assertEqual(bc.clean(scan), 0)


# ── print_report ─────────────────────────────────────────────────────────


class TestPrintReport(unittest.TestCase):

    def test_report_returns_stale_count(self):
        """print_report returns the count of stale items."""
        results = {
            "pid_files": [{"file": "x.pid", "pid": 1, "stale": True, "alive": False}],
            "workers": [{"name": "gamma", "hwnd": 333, "stale": True, "alive": False}],
            "orchestrator": {"exists": True, "stale": True, "alive": False, "hwnd": 99},
            "consultants": [],
            "stale_dispatches": [],
        }
        count = bc.print_report(results, do_clean=False)
        self.assertEqual(count, 3)  # 1 stale pid + 1 stale worker + 1 stale orchestrator

    def test_report_clean_zero(self):
        results = {
            "pid_files": [],
            "workers": [],
            "orchestrator": {"exists": True, "stale": False, "alive": True, "hwnd": 1},
            "consultants": [],
            "stale_dispatches": [],
        }
        count = bc.print_report(results)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
