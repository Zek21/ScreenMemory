# signed: gamma
"""Comprehensive tests for tools/skynet_self_heal.py — Skynet Self-Healing Infrastructure."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_self_heal import (
    _load_json,
    _save_json,
    _ts,
    _is_pid_alive,
    _is_port_open,
    _http_get,
    check_backend,
    check_god_console,
    check_workers_hwnd,
    check_daemons,
    check_disk,
    check_logs,
    fix_stale_pids,
    fix_archive_logs,
    fix_compact_bus_archive,
    fix_restart_backend,
    fix_restart_god_console,
    run_system_checks,
    run_system_fixes,
    save_health_report,
    detect_stuck_tasks,
    auto_heal,
    health_report,
    run_continuous,
    _get_worker_states,
    _get_dispatch_log,
    _log_and_broadcast_heals,
    ALL_WORKERS,
    KNOWN_DAEMONS,
    BACKEND_PORT,
    GOD_CONSOLE_PORT,
    STUCK_THRESHOLDS,
    DAEMON_INTERVAL,
    BUS_ARCHIVE_MAX_MB,
)


# ── Utility Function Tests ──────────────────────────────────────────

class TestLoadSaveJson:
    def test_load_json_valid(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        assert _load_json(f) == {"key": "value"}

    def test_load_json_missing(self, tmp_path):
        assert _load_json(tmp_path / "missing.json") == {}

    def test_load_json_corrupt(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{invalid", encoding="utf-8")
        assert _load_json(f) == {}

    def test_load_json_list(self, tmp_path):
        f = tmp_path / "arr.json"
        f.write_text('[1, 2, 3]', encoding="utf-8")
        assert _load_json(f) == [1, 2, 3]

    def test_save_json_creates_file(self, tmp_path):
        target = tmp_path / "sub" / "out.json"
        _save_json(target, {"a": 1})
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_save_json_atomic(self, tmp_path):
        target = tmp_path / "atomic.json"
        _save_json(target, {"first": True})
        _save_json(target, {"second": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"second": True}
        assert not (tmp_path / "atomic.tmp").exists()


class TestTimestamp:
    def test_ts_format(self):
        ts = _ts()
        assert "T" in ts
        assert len(ts) >= 19  # ISO format with seconds


class TestPidAlive:
    @patch("tools.skynet_self_heal.ctypes")
    def test_pid_alive_true(self, mock_ctypes):
        mock_ctypes.windll.kernel32.OpenProcess.return_value = 123
        mock_ctypes.c_ulong.return_value = MagicMock(value=259)  # STILL_ACTIVE
        mock_ctypes.windll.kernel32.GetExitCodeProcess = MagicMock()
        mock_ctypes.windll.kernel32.CloseHandle = MagicMock()
        # The function reads code.value after GetExitCodeProcess
        # We need the ctypes.byref call to work
        result = _is_pid_alive(1234)
        mock_ctypes.windll.kernel32.OpenProcess.assert_called_once()

    @patch("tools.skynet_self_heal.ctypes")
    def test_pid_alive_handle_zero(self, mock_ctypes):
        mock_ctypes.windll.kernel32.OpenProcess.return_value = 0
        assert _is_pid_alive(9999) is False

    def test_pid_alive_exception(self):
        with patch("tools.skynet_self_heal.ctypes", side_effect=Exception("no ctypes")):
            assert _is_pid_alive(1) is False


class TestPortOpen:
    @patch("tools.skynet_self_heal.socket.create_connection")
    def test_port_open_true(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        assert _is_port_open(8420) is True

    @patch("tools.skynet_self_heal.socket.create_connection",
           side_effect=ConnectionRefusedError)
    def test_port_open_refused(self, mock_conn):
        assert _is_port_open(8420) is False

    @patch("tools.skynet_self_heal.socket.create_connection",
           side_effect=TimeoutError)
    def test_port_open_timeout(self, mock_conn):
        assert _is_port_open(8420) is False


class TestHttpGet:
    def test_http_get_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": "ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _http_get("http://localhost:8420/status")
            assert result == {"status": "ok"}

    def test_http_get_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            assert _http_get("http://bad:9999") is None


# ── Check Functions ──────────────────────────────────────────────────

class TestCheckBackend:
    @patch("tools.skynet_self_heal._http_get", return_value={"uptime_s": 3600, "version": "1.2"})
    @patch("tools.skynet_self_heal._is_port_open", return_value=True)
    def test_backend_alive(self, mock_port, mock_http):
        result = check_backend()
        assert result["status"] == "ALIVE"
        assert result["details"]["uptime_s"] == 3600
        assert result["details"]["version"] == "1.2"

    @patch("tools.skynet_self_heal._is_port_open", return_value=False)
    def test_backend_dead(self, mock_port):
        result = check_backend()
        assert result["status"] == "DEAD"
        assert result["port"] == BACKEND_PORT


class TestCheckGodConsole:
    @patch("tools.skynet_self_heal._http_get", return_value={"status": "healthy"})
    @patch("tools.skynet_self_heal._is_port_open", return_value=True)
    def test_console_alive(self, mock_port, mock_http):
        result = check_god_console()
        assert result["status"] == "ALIVE"

    @patch("tools.skynet_self_heal._is_port_open", return_value=False)
    def test_console_dead(self, mock_port):
        result = check_god_console()
        assert result["status"] == "DEAD"


class TestCheckWorkersHwnd:
    @patch("tools.skynet_self_heal.ctypes")
    def test_workers_mixed(self, mock_ctypes, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        health = {
            "alpha": {"hwnd": 111, "status": "IDLE", "model": "opus"},
            "beta": {"hwnd": 222, "status": "PROCESSING", "model": "opus"},
            "gamma": {"hwnd": 0, "status": "UNKNOWN", "model": "unknown"},
            "delta": {"hwnd": 444, "status": "IDLE", "model": "opus"},
        }
        (tmp_path / "worker_health.json").write_text(
            json.dumps(health), encoding="utf-8")
        # IsWindow returns True for 111, 222, 444
        mock_ctypes.windll.user32.IsWindow.side_effect = lambda h: h in (111, 222, 444)
        result = check_workers_hwnd()
        assert result["alive"] == 3
        assert "gamma" in result["dead"]

    def test_workers_no_health_file(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        result = check_workers_hwnd()
        assert "error" in result


class TestCheckDaemons:
    @patch("tools.skynet_self_heal._is_pid_alive")
    def test_daemons_mixed(self, mock_alive, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        # Create some PID files
        known = {
            "daemon_a": tmp_path / "daemon_a.pid",
            "daemon_b": tmp_path / "daemon_b.pid",
            "daemon_c": tmp_path / "daemon_c.pid",
        }
        monkeypatch.setattr(sh, "KNOWN_DAEMONS", known)
        (tmp_path / "daemon_a.pid").write_text("100", encoding="utf-8")
        (tmp_path / "daemon_b.pid").write_text("200", encoding="utf-8")
        # daemon_c has no PID file → missing

        mock_alive.side_effect = lambda pid: pid == 100
        result = check_daemons()
        assert result["alive"] == 1
        assert "daemon_b" in result["stale"]
        assert "daemon_c" in result["missing"]

    @patch("tools.skynet_self_heal._is_pid_alive", return_value=False)
    def test_corrupt_pid_file(self, mock_alive, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        known = {"corrupt": tmp_path / "corrupt.pid"}
        monkeypatch.setattr(sh, "KNOWN_DAEMONS", known)
        (tmp_path / "corrupt.pid").write_text("not_a_number", encoding="utf-8")
        result = check_daemons()
        assert "corrupt" in result["stale"]
        assert result["daemons"]["corrupt"]["status"] == "CORRUPT_PID"


class TestCheckDisk:
    def test_check_disk_basic(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "ROOT", tmp_path)
        monkeypatch.setattr(sh, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "test.json").write_text("{}", encoding="utf-8")
        result = check_disk()
        assert "data_dir_mb" in result
        assert "disk_free_gb" in result

    def test_check_disk_large_archive(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "ROOT", tmp_path)
        monkeypatch.setattr(sh, "DATA", tmp_path / "data")
        (tmp_path / "data").mkdir()
        archive = tmp_path / "data" / "bus_archive.jsonl"
        archive.write_text("x" * (15 * 1024 * 1024), encoding="utf-8")
        result = check_disk()
        assert result["bus_archive_needs_compact"] is True


class TestCheckLogs:
    def test_logs_basic(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "LOGS", tmp_path)
        (tmp_path / "recent.log").write_text("log data", encoding="utf-8")
        result = check_logs()
        assert result["total_files"] == 1

    def test_no_logs_dir(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "LOGS", tmp_path / "nonexistent")
        result = check_logs()
        assert result["total_files"] == 0


# ── Fix Functions ────────────────────────────────────────────────────

class TestFixStalePids:
    def test_remove_stale_pids(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        known = {"stale_daemon": tmp_path / "stale_daemon.pid"}
        monkeypatch.setattr(sh, "KNOWN_DAEMONS", known)
        monkeypatch.setattr(sh, "DATA", tmp_path)
        (tmp_path / "stale_daemon.pid").write_text("999", encoding="utf-8")
        daemon_result = {"stale": ["stale_daemon"]}
        fixes = fix_stale_pids(daemon_result)
        assert len(fixes) == 1
        assert fixes[0]["action"] == "removed_stale_pid"
        assert not (tmp_path / "stale_daemon.pid").exists()

    def test_no_stale_pids(self):
        fixes = fix_stale_pids({"stale": []})
        assert fixes == []


class TestFixArchiveLogs:
    def test_archive_old_logs(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "LOGS", tmp_path)
        (tmp_path / "old.log").write_text("old data", encoding="utf-8")
        log_result = {"old_logs": ["old.log"], "old_images": []}
        fixes = fix_archive_logs(log_result)
        assert len(fixes) == 1
        assert fixes[0]["action"] == "archived_log"
        assert (tmp_path / "archive" / "old.log").exists()
        assert not (tmp_path / "old.log").exists()

    def test_no_old_logs(self):
        fixes = fix_archive_logs({"old_logs": [], "old_images": []})
        assert fixes == []


class TestFixCompactBusArchive:
    def test_compact_large_archive(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        archive = tmp_path / "bus_archive.jsonl"
        lines = [f'{{"msg": {i}}}' for i in range(7000)]
        archive.write_text("\n".join(lines), encoding="utf-8")
        disk_result = {"bus_archive_needs_compact": True}
        fixes = fix_compact_bus_archive(disk_result)
        assert len(fixes) == 1
        assert fixes[0]["action"] == "compacted_bus_archive"
        assert fixes[0]["lines_after"] == 5000

    def test_no_compact_needed(self):
        fixes = fix_compact_bus_archive({"bus_archive_needs_compact": False})
        assert fixes == []

    def test_compact_small_archive(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        archive = tmp_path / "bus_archive.jsonl"
        archive.write_text("line1\nline2\n", encoding="utf-8")
        disk_result = {"bus_archive_needs_compact": True}
        fixes = fix_compact_bus_archive(disk_result)
        assert fixes[0]["action"] == "bus_archive_ok"


class TestFixRestartBackend:
    @patch("tools.skynet_self_heal._is_port_open", return_value=True)
    @patch("tools.skynet_self_heal.subprocess.Popen")
    def test_restart_success(self, mock_popen, mock_port, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "SKYNET_DIR", tmp_path)
        monkeypatch.setattr(sh, "time", MagicMock())
        (tmp_path / "skynet.exe").write_text("fake", encoding="utf-8")
        mock_popen.return_value.pid = 5555
        fixes = fix_restart_backend()
        assert fixes[0]["action"] == "restarted_backend"
        assert fixes[0]["pid"] == 5555

    def test_restart_no_exe(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "SKYNET_DIR", tmp_path)
        fixes = fix_restart_backend()
        assert fixes[0]["action"] == "skip_backend_restart"


class TestFixRestartGodConsole:
    @patch("tools.skynet_self_heal._is_port_open", return_value=True)
    @patch("tools.skynet_self_heal.subprocess.Popen")
    def test_restart_success(self, mock_popen, mock_port, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "ROOT", tmp_path)
        monkeypatch.setattr(sh, "time", MagicMock())
        (tmp_path / "god_console.py").write_text("pass", encoding="utf-8")
        mock_popen.return_value.pid = 7777
        fixes = fix_restart_god_console()
        assert fixes[0]["action"] == "restarted_god_console"

    def test_restart_no_script(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "ROOT", tmp_path)
        fixes = fix_restart_god_console()
        assert fixes[0]["action"] == "skip_god_console_restart"


# ── Stuck Task Detection ────────────────────────────────────────────

class TestDetectStuckTasks:
    def test_detect_stuck_worker(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "realtime.json")
        now = time.time()
        rt = {"workers": {
            "alpha": {"state": "PROCESSING", "since": now - 300, "task": "big task"},
            "beta": {"state": "IDLE", "since": now - 10, "task": ""},
        }}
        (tmp_path / "realtime.json").write_text(
            json.dumps(rt), encoding="utf-8")
        stuck = detect_stuck_tasks(threshold_s=180)
        assert len(stuck) == 1
        assert stuck[0]["worker"] == "alpha"
        assert stuck[0]["stuck_seconds"] > 100

    def test_no_stuck_tasks(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "realtime.json")
        now = time.time()
        rt = {"workers": {
            "alpha": {"state": "IDLE", "since": now - 5, "task": ""},
        }}
        (tmp_path / "realtime.json").write_text(
            json.dumps(rt), encoding="utf-8")
        assert detect_stuck_tasks() == []

    def test_severity_critical(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "realtime.json")
        now = time.time()
        rt = {"workers": {
            "gamma": {"state": "PROCESSING", "since": now - 600, "task": "huge"},
        }}
        (tmp_path / "realtime.json").write_text(
            json.dumps(rt), encoding="utf-8")
        stuck = detect_stuck_tasks(threshold_s=180)
        assert stuck[0]["severity"] == "critical"

    def test_since_zero_ignored(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "realtime.json")
        rt = {"workers": {
            "alpha": {"state": "PROCESSING", "since": 0, "task": ""},
        }}
        (tmp_path / "realtime.json").write_text(
            json.dumps(rt), encoding="utf-8")
        assert detect_stuck_tasks() == []

    def test_empty_realtime(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "realtime.json")
        assert detect_stuck_tasks() == []


# ── Auto Heal ────────────────────────────────────────────────────────

class TestAutoHeal:
    @patch("tools.skynet_self_heal._log_and_broadcast_heals")
    @patch("tools.skynet_self_heal._try_cancel_worker", return_value=(True, None))
    @patch("tools.skynet_self_heal.detect_stuck_tasks")
    def test_auto_heal_cancels(self, mock_detect, mock_cancel, mock_log):
        mock_detect.return_value = [{
            "worker": "alpha", "stuck_seconds": 250,
            "severity": "warning", "task": "slow task",
        }]
        actions = auto_heal()
        assert len(actions) == 1
        assert actions[0]["action"] == "cancelled"
        assert actions[0]["result"] == "success"
        mock_log.assert_called_once()

    @patch("tools.skynet_self_heal.detect_stuck_tasks")
    def test_auto_heal_dry_run(self, mock_detect):
        mock_detect.return_value = [{
            "worker": "beta", "stuck_seconds": 200,
            "severity": "warning", "task": "test",
        }]
        actions = auto_heal(dry_run=True)
        assert len(actions) == 1
        assert actions[0]["action"] == "would_cancel"
        assert actions[0]["result"] == "dry_run"

    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    def test_auto_heal_nothing_stuck(self, mock_detect):
        actions = auto_heal()
        assert actions == []

    @patch("tools.skynet_self_heal._log_and_broadcast_heals")
    @patch("tools.skynet_self_heal._try_cancel_worker", return_value=(False, "UIA failed"))
    @patch("tools.skynet_self_heal.detect_stuck_tasks")
    def test_auto_heal_cancel_fails(self, mock_detect, mock_cancel, mock_log):
        mock_detect.return_value = [{
            "worker": "gamma", "stuck_seconds": 300,
            "severity": "critical", "task": "stuck",
        }]
        actions = auto_heal()
        assert actions[0]["action"] == "cancel_failed"
        assert actions[0]["result"] == "manual_intervention_needed"
        assert actions[0]["cancel_error"] == "UIA failed"


# ── Health Report ────────────────────────────────────────────────────

class TestHealthReport:
    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    @patch("tools.skynet_self_heal._get_worker_states")
    def test_healthy_report(self, mock_states, mock_stuck, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "HEAL_LOG", tmp_path / "heal.json")
        monkeypatch.setattr(sh, "WORKER_PERF", tmp_path / "perf.json")
        mock_states.return_value = {
            "alpha": {"state": "IDLE", "since": 0, "task": ""},
            "beta": {"state": "PROCESSING", "since": 0, "task": "work"},
        }
        report = health_report()
        assert "worker_states" in report
        assert "recommendations" in report
        assert report["dispatch_stats"]["total_completed"] == 0

    @patch("tools.skynet_self_heal.detect_stuck_tasks")
    @patch("tools.skynet_self_heal._get_worker_states")
    def test_report_with_stuck(self, mock_states, mock_stuck, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "HEAL_LOG", tmp_path / "heal.json")
        monkeypatch.setattr(sh, "WORKER_PERF", tmp_path / "perf.json")
        mock_states.return_value = {"alpha": {"state": "PROCESSING"}}
        mock_stuck.return_value = [{"worker": "alpha", "stuck_seconds": 300}]
        report = health_report()
        assert any("stuck" in r.lower() for r in report["recommendations"])

    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    @patch("tools.skynet_self_heal._get_worker_states")
    def test_report_underutilized(self, mock_states, mock_stuck, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "HEAL_LOG", tmp_path / "heal.json")
        monkeypatch.setattr(sh, "WORKER_PERF", tmp_path / "perf.json")
        mock_states.return_value = {
            "alpha": {"state": "IDLE"},
            "beta": {"state": "IDLE"},
            "gamma": {"state": "IDLE"},
        }
        report = health_report()
        assert any("underutilized" in r.lower() for r in report["recommendations"])


# ── Worker States / Dispatch Log ─────────────────────────────────────

class TestWorkerStates:
    def test_get_worker_states(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "rt.json")
        rt = {"workers": {
            "alpha": {"state": "IDLE", "since": 100, "task": ""},
            "beta": {"state": "PROCESSING", "since": 200, "task": "coding"},
        }}
        (tmp_path / "rt.json").write_text(json.dumps(rt), encoding="utf-8")
        states = _get_worker_states()
        assert states["alpha"]["state"] == "IDLE"
        assert states["beta"]["task"] == "coding"

    def test_get_worker_states_missing(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "REALTIME_FILE", tmp_path / "no.json")
        assert _get_worker_states() == {}

    def test_get_dispatch_log_list(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DISPATCH_LOG", tmp_path / "dispatch.json")
        (tmp_path / "dispatch.json").write_text(
            '[{"worker": "alpha"}]', encoding="utf-8")
        log = _get_dispatch_log()
        assert len(log) == 1

    def test_get_dispatch_log_dict(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DISPATCH_LOG", tmp_path / "dispatch.json")
        (tmp_path / "dispatch.json").write_text(
            '{"dispatches": [{"w": "a"}, {"w": "b"}]}', encoding="utf-8")
        log = _get_dispatch_log()
        assert len(log) == 2


# ── Log and Broadcast ────────────────────────────────────────────────

class TestLogAndBroadcast:
    @patch("tools.skynet_spam_guard.guarded_publish", return_value=True)
    def test_log_heals(self, mock_gp, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "HEAL_LOG", tmp_path / "heal.json")
        actions = [{"worker": "alpha", "action": "cancelled"}]
        _log_and_broadcast_heals(actions)
        log = json.loads((tmp_path / "heal.json").read_text(encoding="utf-8"))
        assert len(log) == 1
        assert log[0]["actions"] == actions

    def test_log_survives_bus_failure(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "HEAL_LOG", tmp_path / "heal.json")
        with patch("tools.skynet_spam_guard.guarded_publish",
                   side_effect=Exception("bus down")):
            _log_and_broadcast_heals([{"worker": "x", "action": "test"}])
        # File should still be written
        assert (tmp_path / "heal.json").exists()


# ── Save Health Report ───────────────────────────────────────────────

class TestSaveHealthReport:
    def test_save_report(self, tmp_path, monkeypatch):
        import tools.skynet_self_heal as sh
        monkeypatch.setattr(sh, "DATA", tmp_path)
        report = {"summary": {"total_issues": 0}, "timestamp": "2026-01-01"}
        out = save_health_report(report, fixes=[{"action": "test"}])
        assert out.exists()
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["fixes_applied"] == [{"action": "test"}]
        assert "generated_at" in saved


# ── Run Continuous ───────────────────────────────────────────────────

class TestRunContinuous:
    @patch("tools.skynet_self_heal.time.sleep")
    @patch("tools.skynet_self_heal.auto_heal", return_value=[])
    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    def test_runs_iterations(self, mock_detect, mock_heal, mock_sleep):
        run_continuous(interval_s=0.01, max_iterations=3)
        assert mock_detect.call_count == 3

    @patch("tools.skynet_self_heal.time.sleep")
    @patch("tools.skynet_self_heal.auto_heal")
    @patch("tools.skynet_self_heal.detect_stuck_tasks")
    def test_heals_when_stuck(self, mock_detect, mock_heal, mock_sleep):
        mock_detect.return_value = [{"worker": "alpha", "stuck_seconds": 200}]
        mock_heal.return_value = [{"worker": "alpha", "action": "cancelled",
                                   "result": "success"}]
        run_continuous(interval_s=0.01, max_iterations=1)
        mock_heal.assert_called_once()

    @patch("tools.skynet_self_heal.time.sleep")
    @patch("tools.skynet_self_heal.detect_stuck_tasks",
           side_effect=Exception("network error"))
    def test_survives_errors(self, mock_detect, mock_sleep):
        run_continuous(interval_s=0.01, max_iterations=2)
        assert mock_detect.call_count == 2  # survived 1st error, ran 2nd


# ── System Check + Fix Orchestration ─────────────────────────────────

class TestRunSystemChecks:
    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    @patch("tools.skynet_self_heal.check_logs", return_value={"total_files": 0, "total_mb": 0, "old_logs": [], "old_images": []})
    @patch("tools.skynet_self_heal.check_disk", return_value={"data_dir_mb": 10})
    @patch("tools.skynet_self_heal.check_daemons", return_value={"total": 5, "alive": 5, "stale": [], "missing": [], "daemons": {}})
    @patch("tools.skynet_self_heal.check_workers_hwnd", return_value={"total": 4, "alive": 4, "dead": [], "workers": {}})
    @patch("tools.skynet_self_heal.check_god_console", return_value={"name": "god_console", "port": 8421, "status": "ALIVE", "details": {}})
    @patch("tools.skynet_self_heal.check_backend", return_value={"name": "backend", "port": 8420, "status": "ALIVE", "details": {}})
    def test_healthy_system(self, *mocks):
        report = run_system_checks()
        assert report["summary"]["total_issues"] == 0
        assert report["summary"]["backend"] == "ALIVE"

    @patch("tools.skynet_self_heal.detect_stuck_tasks", return_value=[])
    @patch("tools.skynet_self_heal.check_logs", return_value={"total_files": 0, "total_mb": 0, "old_logs": [], "old_images": []})
    @patch("tools.skynet_self_heal.check_disk", return_value={"data_dir_mb": 10})
    @patch("tools.skynet_self_heal.check_daemons", return_value={"total": 5, "alive": 3, "stale": ["monitor", "watchdog"], "missing": [], "daemons": {}})
    @patch("tools.skynet_self_heal.check_workers_hwnd", return_value={"total": 4, "alive": 2, "dead": ["gamma", "delta"], "workers": {}})
    @patch("tools.skynet_self_heal.check_god_console", return_value={"name": "god_console", "port": 8421, "status": "DEAD", "details": {}})
    @patch("tools.skynet_self_heal.check_backend", return_value={"name": "backend", "port": 8420, "status": "DEAD", "details": {}})
    def test_unhealthy_system(self, *mocks):
        report = run_system_checks()
        assert report["summary"]["critical"] >= 1
        assert report["summary"]["high"] >= 1
        assert report["summary"]["total_issues"] >= 4


class TestRunSystemFixes:
    def test_applies_fixes(self):
        report = {
            "daemons": {"stale": []},
            "logs": {"old_logs": [], "old_images": []},
            "disk": {"bus_archive_needs_compact": False},
            "backend": {"status": "ALIVE"},
            "god_console": {"status": "ALIVE"},
        }
        fixes = run_system_fixes(report)
        assert fixes == []  # nothing to fix


# ── Constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_all_workers(self):
        assert ALL_WORKERS == ["alpha", "beta", "gamma", "delta"]

    def test_backend_port(self):
        assert BACKEND_PORT == 8420

    def test_god_console_port(self):
        assert GOD_CONSOLE_PORT == 8421

    def test_daemon_interval(self):
        assert DAEMON_INTERVAL == 300

    def test_stuck_thresholds(self):
        assert STUCK_THRESHOLDS["simple"] == 120
        assert STUCK_THRESHOLDS["standard"] == 180
        assert STUCK_THRESHOLDS["complex"] == 300

    def test_known_daemons_count(self):
        assert len(KNOWN_DAEMONS) >= 10
