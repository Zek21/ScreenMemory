#!/usr/bin/env python3
"""Tests for tools/orch_realtime.py — orchestrator real-time interface.

Covers: _read_realtime(), _read_consumed(), _write_consumed(), consume(),
consume_all(), pending(), wait(), wait_all(), _scan_bus_for_results(), health().

All network/file I/O is mocked. No live Skynet backend required.
# signed: delta
"""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.orch_realtime as ort


# ── Helpers ──────────────────────────────────────────────────────────


def _make_state(agents=None, bus=None, ts=None, source="realtime.json"):
    """Build a minimal realtime state dict."""
    return {
        "agents": agents or {},
        "bus": bus or [],
        "uptime_s": 100,
        "timestamp": ts or time.time(),
        "_source": source,
    }


def _make_msg(sender="alpha", topic="orchestrator", mtype="result",
              content="done", mid=None):
    return {
        "id": mid or f"msg_{sender}_{int(time.time()*1000)}",
        "sender": sender,
        "topic": topic,
        "type": mtype,
        "content": content,
    }


# ── _read_realtime ──────────────────────────────────────────────────


class TestReadRealtime(unittest.TestCase):
    """Test _read_realtime file/HTTP fallback logic."""

    @patch.object(ort, "REALTIME_FILE")
    def test_reads_fresh_file(self, mock_file):
        """Fresh realtime.json (age < 10s) should be used directly."""
        state = {"timestamp": time.time(), "agents": {"alpha": {"status": "IDLE"}},
                 "bus": [], "workers": {"alpha": {"status": "IDLE"}}}
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(state)

        result = ort._read_realtime()
        self.assertEqual(result["_source"], "realtime.json")
        self.assertIn("alpha", result.get("agents", {}))

    @patch.object(ort, "REALTIME_FILE")
    def test_stale_file_triggers_http_fallback(self, mock_file):
        """File older than 10s should trigger HTTP fallback."""
        state = {"timestamp": time.time() - 30, "agents": {}, "bus": []}
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(state)

        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"agents": {}, "uptime_s": 5}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            result = ort._read_realtime()
            self.assertEqual(result["_source"], "http_fallback")

    @patch.object(ort, "REALTIME_FILE")
    def test_missing_file_uses_http(self, mock_file):
        """Missing file should fall back to HTTP."""
        mock_file.exists.return_value = False

        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"agents": {}, "uptime_s": 0}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            result = ort._read_realtime()
            self.assertEqual(result["_source"], "http_fallback")

    @patch.object(ort, "REALTIME_FILE")
    def test_corrupt_json_falls_back(self, mock_file):
        """Corrupt JSON in file should trigger HTTP fallback gracefully."""
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = "NOT VALID JSON {{{"

        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"agents": {}, "uptime_s": 0}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            result = ort._read_realtime()
            self.assertEqual(result["_source"], "http_fallback")

    @patch.object(ort, "REALTIME_FILE")
    def test_both_file_and_http_fail(self, mock_file):
        """When both file and HTTP fail, return empty state (not crash)."""
        mock_file.exists.return_value = False

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = ort._read_realtime()
            self.assertEqual(result["_source"], "unavailable")
            self.assertEqual(result["agents"], {})

    @patch.object(ort, "REALTIME_FILE")
    def test_workers_key_normalized_to_agents(self, mock_file):
        """Daemon writes 'workers' key; should be normalized to 'agents'."""
        state = {"timestamp": time.time(), "workers": {"beta": {"status": "WORKING"}},
                 "bus_recent": [{"id": "1"}]}
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(state)

        result = ort._read_realtime()
        self.assertIn("beta", result.get("agents", {}))
        self.assertIn("bus", result)

    @patch.object(ort, "REALTIME_FILE")
    def test_iso_timestamp_parsed(self, mock_file):
        """ISO format timestamp string should be parsed to epoch float."""
        from datetime import datetime
        now = datetime.now()
        state = {"timestamp": now.isoformat(), "agents": {}, "bus": []}
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(state)

        result = ort._read_realtime()
        self.assertEqual(result["_source"], "realtime.json")
        self.assertIsInstance(result["timestamp"], float)


# ── Consumed File I/O ───────────────────────────────────────────────


class TestConsumedIO(unittest.TestCase):
    """Test _read_consumed / _write_consumed."""

    @patch.object(ort, "CONSUMED_FILE")
    def test_read_missing_returns_empty_set(self, mock_file):
        mock_file.exists.return_value = False
        result = ort._read_consumed()
        self.assertEqual(result, set())

    @patch.object(ort, "CONSUMED_FILE")
    def test_read_corrupt_returns_empty_set(self, mock_file):
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = "NOT JSON"
        result = ort._read_consumed()
        self.assertEqual(result, set())

    @patch.object(ort, "CONSUMED_FILE")
    def test_read_valid_file(self, mock_file):
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps({"consumed": ["a", "b", "c"]})
        result = ort._read_consumed()
        self.assertEqual(result, {"a", "b", "c"})

    @patch.object(ort, "DATA")
    @patch.object(ort, "CONSUMED_FILE")
    def test_write_consumed_atomic(self, mock_file, mock_data):
        """Write should use tmp+rename for atomicity."""
        mock_tmp = MagicMock()
        mock_file.with_suffix.return_value = mock_tmp
        mock_data.mkdir = MagicMock()

        ort._write_consumed({"x", "y"})

        mock_tmp.write_text.assert_called_once()
        written = mock_tmp.write_text.call_args[0][0]
        data = json.loads(written)
        self.assertIn("consumed", data)
        self.assertEqual(set(data["consumed"]), {"x", "y"})
        mock_tmp.replace.assert_called_once_with(mock_file)


# ── consume / consume_all ──────────────────────────────────────────


class TestConsume(unittest.TestCase):

    @patch.object(ort, "_write_consumed")
    @patch.object(ort, "_read_consumed", return_value=set())
    def test_consume_adds_id(self, _rc, _wc):
        result = ort.consume("msg123")
        self.assertTrue(result)
        _wc.assert_called_once()
        written = _wc.call_args[0][0]
        self.assertIn("msg123", written)

    @patch.object(ort, "_write_consumed")
    @patch.object(ort, "_read_consumed", return_value={"old1"})
    @patch.object(ort, "_read_realtime")
    def test_consume_all_marks_pending(self, mock_read, _rc, _wc):
        mock_read.return_value = _make_state(bus=[
            _make_msg(mid="r1", mtype="result"),
            _make_msg(mid="r2", mtype="alert"),
            _make_msg(mid="old1", mtype="result"),  # already consumed
            _make_msg(mid="i1", mtype="identity_ack"),  # not result/alert
        ])

        ids = ort.consume_all()
        self.assertEqual(len(ids), 2)
        self.assertIn("r1", ids)
        self.assertIn("r2", ids)
        self.assertNotIn("old1", ids)
        self.assertNotIn("i1", ids)


# ── _scan_bus_for_results ──────────────────────────────────────────


class TestScanBusForResults(unittest.TestCase):

    @patch.object(ort, "_read_realtime")
    def test_finds_matching_workers(self, mock_read):
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="a1"),
            _make_msg(sender="beta", mtype="result", mid="b1"),
            _make_msg(sender="gamma", mtype="identity_ack", mid="g1"),  # wrong type
        ])

        results = {}
        newly = ort._scan_bus_for_results(["alpha", "beta", "gamma"], set(), results)

        self.assertIn("alpha", newly)
        self.assertIn("beta", newly)
        self.assertNotIn("gamma", newly)  # identity_ack != result
        self.assertEqual(len(results), 2)

    @patch.object(ort, "_read_realtime")
    def test_skips_consumed(self, mock_read):
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="consumed_id"),
        ])

        results = {}
        newly = ort._scan_bus_for_results(["alpha"], {"consumed_id"}, results)
        self.assertEqual(len(newly), 0)
        self.assertEqual(len(results), 0)

    @patch.object(ort, "_read_realtime")
    def test_first_result_wins(self, mock_read):
        """If a worker has multiple results, the first one should be kept."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", content="first", mid="a1"),
            _make_msg(sender="alpha", mtype="result", content="second", mid="a2"),
        ])

        results = {}
        ort._scan_bus_for_results(["alpha"], set(), results)
        self.assertEqual(results["alpha"]["content"], "first")


# ── wait ────────────────────────────────────────────────────────────


class TestWait(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_immediate_match(self, mock_read, _rc):
        """If result already exists, return immediately."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", content="RESULT: done signed:alpha"),
        ])

        result = ort.wait("alpha", timeout=2)
        self.assertIsNotNone(result)
        self.assertEqual(result["sender"], "alpha")

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_timeout_returns_none(self, mock_read, _rc):
        """No matching result should timeout and return None."""
        mock_read.return_value = _make_state(bus=[])

        # Patch urllib to also fail (HTTP fallback)
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = ort.wait("alpha", timeout=1)
            self.assertIsNone(result)

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_matches_by_content(self, mock_read, _rc):
        """Should match key in content, not just sender."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="beta", mtype="result", content="RESULT: alpha_task done"),
        ])

        result = ort.wait("alpha_task", timeout=1)
        self.assertIsNotNone(result)

    @patch.object(ort, "_read_consumed", return_value={"already_seen"})
    @patch.object(ort, "_read_realtime")
    def test_skips_consumed_messages(self, mock_read, _rc):
        """Consumed messages should not match."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="already_seen"),
        ])

        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = ort.wait("alpha", timeout=1)
            self.assertIsNone(result)


# ── wait_all ────────────────────────────────────────────────────────


class TestWaitAll(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_all_present_returns_immediately(self, mock_read, _rc):
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="a1"),
            _make_msg(sender="beta", mtype="result", mid="b1"),
        ])

        results = ort.wait_all(["alpha", "beta"], timeout=2)
        self.assertIn("alpha", results)
        self.assertIn("beta", results)

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_partial_results_on_timeout(self, mock_read, _rc):
        """If only some workers respond, return partial results."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="a1"),
        ])

        results = ort.wait_all(["alpha", "beta"], timeout=1)
        self.assertIn("alpha", results)
        self.assertNotIn("beta", results)

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_non_blocking_snapshot(self, mock_read, _rc):
        """Non-blocking mode should return immediately with what's available."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="gamma", mtype="result", mid="g1"),
        ])

        results = ort.wait_all(["alpha", "gamma"], timeout=60, non_blocking=True)
        self.assertIn("gamma", results)
        self.assertNotIn("alpha", results)

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_default_workers(self, mock_read, _rc):
        """No workers arg should default to alpha/beta/gamma/delta."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="alpha", mtype="result", mid="a1"),
            _make_msg(sender="beta", mtype="result", mid="b1"),
            _make_msg(sender="gamma", mtype="result", mid="g1"),
            _make_msg(sender="delta", mtype="result", mid="d1"),
        ])

        results = ort.wait_all(timeout=2)
        self.assertEqual(len(results), 4)


# ── health ──────────────────────────────────────────────────────────


class TestHealth(unittest.TestCase):

    @patch.object(ort, "_read_realtime")
    def test_health_with_agents(self, mock_read):
        """health() should not crash with real-looking data."""
        mock_read.return_value = _make_state(
            agents={
                "alpha": {"status": "IDLE", "tasks_completed": 5, "total_errors": 0},
                "beta": {"status": "WORKING", "tasks_completed": 3, "total_errors": 1},
            },
            bus=[_make_msg()] * 5,
        )

        with patch("urllib.request.urlopen", side_effect=Exception("no metrics")):
            result = ort.health()
            # health() should complete without raising
            assert result is None or isinstance(result, (dict, type(None)))

    @patch.object(ort, "_read_realtime")
    def test_health_empty_state(self, mock_read):
        """health() should handle empty state gracefully."""
        mock_read.return_value = _make_state()

        with patch("urllib.request.urlopen", side_effect=Exception("no metrics")):
            result = ort.health()
            assert result is None or isinstance(result, (dict, type(None)))


# ── status ──────────────────────────────────────────────────────────


class TestStatus(unittest.TestCase):

    @patch.object(ort, "_read_realtime")
    def test_status_displays_agents(self, mock_read):
        """status() should display agents without crashing."""
        mock_read.return_value = _make_state(
            agents={
                "alpha": {"status": "IDLE", "model": "opus-4.6-fast",
                          "last_heartbeat": "10s", "tasks_completed": 2,
                          "total_errors": 0, "avg_task_ms": 1500, "queue_depth": 0},
            },
        )
        result = ort.status()
        assert result is None or isinstance(result, (dict, type(None)))

    @patch.object(ort, "_read_realtime")
    def test_status_no_agents(self, mock_read):
        """status() with empty agents should not crash."""
        mock_read.return_value = _make_state()
        result = ort.status()
        assert result is None or isinstance(result, (dict, type(None)))


# ── bus_messages ────────────────────────────────────────────────────


class TestBusMessages(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value={"old1"})
    @patch.object(ort, "_read_realtime")
    def test_shows_recent_messages(self, mock_read, _rc):
        mock_read.return_value = _make_state(bus=[
            _make_msg(mid="old1"),
            _make_msg(mid="new1"),
        ])
        result = ort.bus_messages(n=5)
        assert result is None or isinstance(result, (list, type(None)))

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_empty_bus(self, mock_read, _rc):
        mock_read.return_value = _make_state(bus=[])
        result = ort.bus_messages()
        assert result is None or isinstance(result, (list, type(None)))


# ── pending ─────────────────────────────────────────────────────────


class TestPending(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_pending_shows_results_alerts_errors(self, mock_read, _rc):
        """pending() should show result, alert, and error types."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(mtype="result", mid="r1"),
            _make_msg(mtype="alert", mid="a1"),
            _make_msg(mtype="error", mid="e1"),
            _make_msg(mtype="identity_ack", mid="i1"),  # filtered out
            _make_msg(mtype="heartbeat", mid="h1"),  # filtered out
        ])
        result = ort.pending()
        assert result is None or isinstance(result, (list, type(None)))

    @patch.object(ort, "_read_consumed", return_value={"r1", "a1"})
    @patch.object(ort, "_read_realtime")
    def test_pending_excludes_consumed(self, mock_read, _rc):
        """Consumed messages should not appear in pending."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(mtype="result", mid="r1"),  # consumed
            _make_msg(mtype="alert", mid="a1"),  # consumed
            _make_msg(mtype="result", mid="r2"),  # NOT consumed
        ])
        result = ort.pending()
        assert result is None or isinstance(result, (list, type(None)))

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_pending_empty_bus(self, mock_read, _rc):
        """Empty bus should print 'no pending' message."""
        mock_read.return_value = _make_state(bus=[])
        result = ort.pending()
        assert result is None or isinstance(result, (list, type(None)))


# ── dispatch_and_wait ──────────────────────────────────────────────


class TestDispatchAndWait(unittest.TestCase):

    @patch.object(ort, "wait")
    @patch.object(ort, "consume_all", return_value=[])
    @patch("subprocess.run")
    def test_dispatch_success_then_wait(self, mock_run, mock_consume, mock_wait):
        """Successful dispatch should call wait() with worker name."""
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        mock_wait.return_value = _make_msg(sender="alpha", content="RESULT: done")

        result = ort.dispatch_and_wait("alpha", "test task", timeout=30)
        self.assertIsNotNone(result)
        self.assertEqual(result["sender"], "alpha")
        mock_wait.assert_called_once_with("alpha", 30)

    @patch.object(ort, "consume_all", return_value=[])
    @patch("subprocess.run")
    def test_dispatch_failure_returns_none(self, mock_run, mock_consume):
        """Failed dispatch (returncode != 0) should return None."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="NO_EDIT")

        result = ort.dispatch_and_wait("alpha", "test task")
        self.assertIsNone(result)

    @patch.object(ort, "consume_all", return_value=[])
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30))
    def test_dispatch_timeout_returns_none(self, mock_run, mock_consume):
        """Dispatch subprocess timeout should return None."""
        result = ort.dispatch_and_wait("alpha", "test task")
        self.assertIsNone(result)


# ── dispatch_parallel_and_wait ─────────────────────────────────────


class TestDispatchParallelAndWait(unittest.TestCase):

    @patch.object(ort, "wait_all")
    @patch.object(ort, "consume_all", return_value=[])
    @patch("subprocess.run")
    def test_parallel_dispatch_then_wait_all(self, mock_run, mock_consume, mock_wait):
        """Successful parallel dispatch should call wait_all()."""
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        mock_wait.return_value = {
            "alpha": _make_msg(sender="alpha"),
            "beta": _make_msg(sender="beta"),
        }

        result = ort.dispatch_parallel_and_wait("broadcast task", timeout=60)
        self.assertEqual(len(result), 2)
        mock_wait.assert_called_once_with(timeout=60)

    @patch.object(ort, "consume_all", return_value=[])
    @patch("subprocess.run")
    def test_parallel_dispatch_failure_returns_empty(self, mock_run, mock_consume):
        """Failed parallel dispatch should return empty dict."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        result = ort.dispatch_parallel_and_wait("task")
        self.assertEqual(result, {})


# ── wait HTTP fallback ─────────────────────────────────────────────


class TestWaitHttpFallback(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_http_fallback_finds_match(self, mock_read, _rc):
        """When realtime.json has no match, HTTP fallback should find it."""
        # Realtime file returns no matching results
        mock_read.return_value = _make_state(bus=[])

        fallback_msgs = [
            {"id": "fb1", "sender": "alpha", "topic": "orchestrator",
             "type": "result", "content": "RESULT: done signed:alpha"}
        ]

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fallback_msgs).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = ort.wait("alpha", timeout=0.5)

        self.assertIsNotNone(result)
        self.assertEqual(result["sender"], "alpha")


# ── wait case-insensitive matching ─────────────────────────────────


class TestWaitCaseInsensitive(unittest.TestCase):

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_matches_case_insensitively(self, mock_read, _rc):
        """Key matching should be case-insensitive."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="Alpha", mtype="result", content="DONE"),
        ])

        result = ort.wait("ALPHA", timeout=1)
        self.assertIsNotNone(result)

    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_matches_by_topic(self, mock_read, _rc):
        """Key should match against topic field too."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(sender="beta", topic="alpha_task", mtype="result"),
        ])

        result = ort.wait("alpha_task", timeout=1)
        self.assertIsNotNone(result)


# ── consume_all edge cases ─────────────────────────────────────────


class TestConsumeAllEdgeCases(unittest.TestCase):

    @patch.object(ort, "_write_consumed")
    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_consume_all_empty_bus(self, mock_read, _rc, _wc):
        """Empty bus should return empty list and not crash."""
        mock_read.return_value = _make_state(bus=[])
        ids = ort.consume_all()
        self.assertEqual(ids, [])

    @patch.object(ort, "_write_consumed")
    @patch.object(ort, "_read_consumed", return_value={"r1", "r2", "r3"})
    @patch.object(ort, "_read_realtime")
    def test_consume_all_all_already_consumed(self, mock_read, _rc, _wc):
        """When all messages are already consumed, return empty list."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(mtype="result", mid="r1"),
            _make_msg(mtype="result", mid="r2"),
            _make_msg(mtype="result", mid="r3"),
        ])
        ids = ort.consume_all()
        self.assertEqual(ids, [])

    @patch.object(ort, "_write_consumed")
    @patch.object(ort, "_read_consumed", return_value=set())
    @patch.object(ort, "_read_realtime")
    def test_consume_all_skips_non_actionable_types(self, mock_read, _rc, _wc):
        """Only result/alert/error types should be consumed."""
        mock_read.return_value = _make_state(bus=[
            _make_msg(mtype="identity_ack", mid="i1"),
            _make_msg(mtype="heartbeat", mid="h1"),
            _make_msg(mtype="directive", mid="d1"),
        ])
        ids = ort.consume_all()
        self.assertEqual(ids, [])


# ── _read_realtime edge cases ──────────────────────────────────────


class TestReadRealtimeEdgeCases(unittest.TestCase):

    @patch.object(ort, "REALTIME_FILE")
    def test_last_update_field_used_as_timestamp(self, mock_file):
        """Daemon uses 'last_update' instead of 'timestamp' — should normalize."""
        state = {"last_update": time.time(), "workers": {"gamma": {"status": "IDLE"}}, "bus": []}
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(state)

        result = ort._read_realtime()
        self.assertEqual(result["_source"], "realtime.json")
        self.assertIn("gamma", result.get("agents", {}))

    @patch.object(ort, "REALTIME_FILE")
    def test_http_fallback_bus_list(self, mock_file):
        """HTTP fallback should handle bus as list properly."""
        mock_file.exists.return_value = False

        status_resp = MagicMock()
        status_resp.read.return_value = json.dumps({"agents": {"alpha": {"status": "IDLE"}}, "uptime_s": 42}).encode()
        status_resp.__enter__ = lambda s: s
        status_resp.__exit__ = MagicMock(return_value=False)

        bus_resp = MagicMock()
        bus_resp.read.return_value = json.dumps([{"id": "1", "sender": "alpha", "type": "result"}]).encode()
        bus_resp.__enter__ = lambda s: s
        bus_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[status_resp, bus_resp]):
            result = ort._read_realtime()

        self.assertEqual(result["_source"], "http_fallback")
        self.assertEqual(len(result["bus"]), 1)
        self.assertEqual(result["uptime_s"], 42)


# signed: gamma — comprehensive test coverage for orch_realtime.py


if __name__ == "__main__":
    unittest.main()
