#!/usr/bin/env python3
"""
Comprehensive tests for tools/skynet_task_queue.py -- Pull-based task queue.

Tests cover: post_task, list_tasks, claim_task, complete_task, grab_next,
error handling, priority sorting, race conditions, CLI main().

# signed: delta
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_task_queue import (
    SKYNET,
    _get,
    _post,
    claim_task,
    complete_task,
    grab_next,
    list_tasks,
    main,
    post_task,
)


class _FakeResponse:
    """Minimal context-manager-compatible HTTP response stub."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _make_resp(obj):
    return _FakeResponse(json.dumps(obj).encode())


# ── _post / _get helpers ──────────────────────────────────


class TestPost(unittest.TestCase):
    """Tests for the internal _post helper."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t1"})
        result = _post("/bus/tasks", {"task": "hello"})
        self.assertEqual(result, {"task_id": "t1"})
        # Verify request was made with correct URL
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertTrue(req.full_url.endswith("/bus/tasks"))
        self.assertEqual(req.method, "POST")

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_post_network_error(self, _mock):
        result = _post("/bus/tasks", {"task": "hello"})
        self.assertIsNone(result)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_malformed_json_returns_none(self, mock_urlopen):
        """Malformed JSON is caught by the except block and returns None."""
        mock_urlopen.return_value = _FakeResponse(b"not json")
        result = _post("/bus/tasks", {})
        self.assertIsNone(result)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_empty_body_ok(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({})
        result = _post("/test", {})
        self.assertEqual(result, {})


class TestGet(unittest.TestCase):
    """Tests for the internal _get helper."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_get_list_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp([{"id": "t1", "task": "a"}])
        result = _get("/bus/tasks")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_get_dict_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "ok"})
        result = _get("/status")
        self.assertIsInstance(result, dict)

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("refused"))
    def test_get_network_error(self, _mock):
        result = _get("/bus/tasks")
        self.assertIsNone(result)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_get_empty_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp([])
        result = _get("/bus/tasks")
        self.assertEqual(result, [])


# ── post_task ─────────────────────────────────────────────


class TestPostTask(unittest.TestCase):
    """Tests for post_task()."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_returns_id(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "abc-123"})
        tid = post_task("fix the bug")
        self.assertEqual(tid, "abc-123")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_with_source_and_priority(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t2"})
        tid = post_task("deploy", source="alpha", priority=2)
        self.assertEqual(tid, "t2")
        # Verify the request body contains correct fields
        call_args = mock_urlopen.call_args
        body = json.loads(call_args[0][0].data)
        self.assertEqual(body["task"], "deploy")
        self.assertEqual(body["source"], "alpha")
        self.assertEqual(body["priority"], 2)

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("err"))
    def test_post_task_network_error_returns_none(self, _mock):
        tid = post_task("hello")
        self.assertIsNone(tid)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_missing_task_id_in_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "ok"})
        tid = post_task("hello")
        self.assertIsNone(tid)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_empty_string(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t-empty"})
        tid = post_task("")
        self.assertEqual(tid, "t-empty")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_default_source_and_priority(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t3"})
        post_task("test")
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["source"], "orchestrator")
        self.assertEqual(body["priority"], 0)


# ── list_tasks ────────────────────────────────────────────


class TestListTasks(unittest.TestCase):
    """Tests for list_tasks()."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_list_tasks_pending(self, mock_urlopen):
        tasks = [{"id": "t1", "task": "a", "status": "pending"}]
        mock_urlopen.return_value = _make_resp(tasks)
        result = list_tasks()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t1")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_list_tasks_all(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp([])
        list_tasks(show_all=True)
        url = mock_urlopen.call_args[0][0]
        self.assertIn("all=true", url)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_list_tasks_pending_url(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp([])
        list_tasks(show_all=False)
        url = mock_urlopen.call_args[0][0]
        self.assertNotIn("all=true", url)

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("err"))
    def test_list_tasks_network_error_returns_empty(self, _mock):
        result = list_tasks()
        self.assertEqual(result, [])

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_list_tasks_non_list_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"error": "bad"})
        result = list_tasks()
        self.assertEqual(result, [])

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_list_tasks_empty_queue(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp([])
        result = list_tasks()
        self.assertEqual(result, [])


# ── claim_task ────────────────────────────────────────────


class TestClaimTask(unittest.TestCase):
    """Tests for claim_task()."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_claim_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "claimed"})
        ok = claim_task("t1", "alpha")
        self.assertTrue(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_claim_already_taken(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "already_claimed"})
        ok = claim_task("t1", "beta")
        self.assertFalse(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("err"))
    def test_claim_network_error(self, _mock):
        ok = claim_task("t1", "alpha")
        self.assertFalse(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_claim_sends_correct_body(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "claimed"})
        claim_task("task-42", "gamma")
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["task_id"], "task-42")
        self.assertEqual(body["worker"], "gamma")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_claim_nonexistent_task(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"status": "not_found"})
        ok = claim_task("nonexistent", "alpha")
        self.assertFalse(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_claim_empty_response(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({})
        ok = claim_task("t1", "alpha")
        self.assertFalse(ok)


# ── complete_task ─────────────────────────────────────────


class TestCompleteTask(unittest.TestCase):
    """Tests for complete_task()."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_complete_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"ok": True})
        ok = complete_task("t1", "alpha", "done!")
        self.assertTrue(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_complete_failed_status(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"ok": True})
        complete_task("t1", "alpha", "error occurred", failed=True)
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["status"], "failed")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_complete_success_status(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"ok": True})
        complete_task("t1", "alpha", "all good", failed=False)
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["status"], "completed")

    @patch("tools.skynet_task_queue.urllib.request.urlopen", side_effect=Exception("err"))
    def test_complete_network_error(self, _mock):
        ok = complete_task("t1", "alpha", "result")
        self.assertFalse(ok)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_complete_sends_correct_body(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"ok": True})
        complete_task("t99", "beta", "my result", failed=False)
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["task_id"], "t99")
        self.assertEqual(body["worker"], "beta")
        self.assertEqual(body["result"], "my result")
        self.assertEqual(body["status"], "completed")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_complete_empty_result(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"ok": True})
        ok = complete_task("t1", "alpha", "")
        self.assertTrue(ok)


# ── grab_next ─────────────────────────────────────────────


class TestGrabNext(unittest.TestCase):
    """Tests for grab_next()."""

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_success(self, mock_list, mock_claim):
        mock_list.return_value = [
            {"id": "t1", "task": "low", "priority": 0},
            {"id": "t2", "task": "high", "priority": 2},
        ]
        mock_claim.return_value = True
        result = grab_next("alpha")
        self.assertIsNotNone(result)
        # Should grab highest priority first
        self.assertEqual(result["id"], "t2")

    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_empty_queue(self, mock_list):
        mock_list.return_value = []
        result = grab_next("alpha")
        self.assertIsNone(result)

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_priority_sort(self, mock_list, mock_claim):
        mock_list.return_value = [
            {"id": "t1", "task": "low", "priority": 0},
            {"id": "t2", "task": "critical", "priority": 2},
            {"id": "t3", "task": "high", "priority": 1},
        ]
        mock_claim.return_value = True
        result = grab_next("beta")
        self.assertEqual(result["id"], "t2")

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_first_claim_fails_second_succeeds(self, mock_list, mock_claim):
        """Simulates race condition: first task already claimed by another worker."""
        mock_list.return_value = [
            {"id": "t1", "task": "first", "priority": 2},
            {"id": "t2", "task": "second", "priority": 1},
        ]
        mock_claim.side_effect = [False, True]
        result = grab_next("gamma")
        self.assertEqual(result["id"], "t2")

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_all_claims_fail(self, mock_list, mock_claim):
        """All tasks already claimed — returns None."""
        mock_list.return_value = [
            {"id": "t1", "task": "taken", "priority": 1},
            {"id": "t2", "task": "also taken", "priority": 0},
        ]
        mock_claim.return_value = False
        result = grab_next("delta")
        self.assertIsNone(result)

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_single_task(self, mock_list, mock_claim):
        mock_list.return_value = [{"id": "t1", "task": "only one", "priority": 0}]
        mock_claim.return_value = True
        result = grab_next("alpha")
        self.assertEqual(result["id"], "t1")

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_missing_priority_defaults_zero(self, mock_list, mock_claim):
        """Tasks without priority field should default to 0."""
        mock_list.return_value = [
            {"id": "t1", "task": "no priority"},
            {"id": "t2", "task": "has priority", "priority": 1},
        ]
        mock_claim.return_value = True
        result = grab_next("alpha")
        self.assertEqual(result["id"], "t2")


# ── CLI main() ────────────────────────────────────────────


class TestMain(unittest.TestCase):
    """Tests for the CLI main() function."""

    @patch("tools.skynet_task_queue.list_tasks")
    def test_main_list_empty(self, mock_list):
        mock_list.return_value = []
        with patch("sys.argv", ["skynet_task_queue.py", "list"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("No tasks", captured.getvalue())

    @patch("tools.skynet_task_queue.list_tasks")
    def test_main_list_with_tasks(self, mock_list):
        mock_list.return_value = [
            {"id": "t1", "status": "pending", "priority": 0, "task": "do stuff"}
        ]
        with patch("sys.argv", ["skynet_task_queue.py", "list"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            output = captured.getvalue()
            self.assertIn("t1", output)
            self.assertIn("do stuff", output)

    @patch("tools.skynet_task_queue.list_tasks")
    def test_main_list_with_claimed_task(self, mock_list):
        mock_list.return_value = [
            {"id": "t1", "status": "claimed", "priority": 1, "task": "work",
             "claimed_by": "alpha"}
        ]
        with patch("sys.argv", ["skynet_task_queue.py", "list"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("claimed by alpha", captured.getvalue())

    @patch("tools.skynet_task_queue.post_task")
    def test_main_add_success(self, mock_post):
        mock_post.return_value = "t-new"
        with patch("sys.argv", ["skynet_task_queue.py", "add", "new task"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Queued", captured.getvalue())

    @patch("tools.skynet_task_queue.post_task")
    def test_main_add_failure(self, mock_post):
        mock_post.return_value = None
        with patch("sys.argv", ["skynet_task_queue.py", "add", "bad task"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Failed", captured.getvalue())

    @patch("tools.skynet_task_queue.claim_task")
    def test_main_claim_success(self, mock_claim):
        mock_claim.return_value = True
        with patch("sys.argv", ["skynet_task_queue.py", "claim", "t1", "alpha"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Claimed", captured.getvalue())

    @patch("tools.skynet_task_queue.claim_task")
    def test_main_claim_failure(self, mock_claim):
        mock_claim.return_value = False
        with patch("sys.argv", ["skynet_task_queue.py", "claim", "t1", "beta"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Failed", captured.getvalue())

    @patch("tools.skynet_task_queue.complete_task")
    def test_main_done_success(self, mock_complete):
        mock_complete.return_value = True
        with patch("sys.argv", ["skynet_task_queue.py", "done", "t1", "alpha", "result text"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Completed", captured.getvalue())

    @patch("tools.skynet_task_queue.complete_task")
    def test_main_done_failed_flag(self, mock_complete):
        mock_complete.return_value = True
        with patch("sys.argv", ["skynet_task_queue.py", "done", "t1", "alpha", "--failed"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
        mock_complete.assert_called_once_with("t1", "alpha", "", failed=True)

    @patch("tools.skynet_task_queue.grab_next")
    def test_main_grab_success(self, mock_grab):
        mock_grab.return_value = {"id": "t1", "task": "grabbed task"}
        with patch("sys.argv", ["skynet_task_queue.py", "grab", "alpha"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("Grabbed", captured.getvalue())

    @patch("tools.skynet_task_queue.grab_next")
    def test_main_grab_empty(self, mock_grab):
        mock_grab.return_value = None
        with patch("sys.argv", ["skynet_task_queue.py", "grab", "alpha"]):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                main()
            self.assertIn("No pending", captured.getvalue())


# ── Integration-style edge cases ──────────────────────────


class TestEdgeCases(unittest.TestCase):
    """Edge case and boundary tests."""

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_skynet_url_constant(self, _mock):
        self.assertEqual(SKYNET, "http://localhost:8420")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_negative_priority(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t-neg"})
        tid = post_task("test", priority=-1)
        self.assertEqual(tid, "t-neg")
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["priority"], -1)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_large_priority(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t-big"})
        tid = post_task("test", priority=999)
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertEqual(body["priority"], 999)

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_unicode_content(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t-uni"})
        tid = post_task("Fix the \u2014 bug and \u2026 issue")
        self.assertEqual(tid, "t-uni")

    @patch("tools.skynet_task_queue.urllib.request.urlopen")
    def test_post_task_very_long_task(self, mock_urlopen):
        mock_urlopen.return_value = _make_resp({"task_id": "t-long"})
        long_task = "x" * 10000
        tid = post_task(long_task)
        self.assertEqual(tid, "t-long")

    @patch("tools.skynet_task_queue.claim_task")
    @patch("tools.skynet_task_queue.list_tasks")
    def test_grab_next_all_same_priority(self, mock_list, mock_claim):
        """When all tasks have same priority, grab first successfully claimed."""
        mock_list.return_value = [
            {"id": "t1", "task": "a", "priority": 0},
            {"id": "t2", "task": "b", "priority": 0},
            {"id": "t3", "task": "c", "priority": 0},
        ]
        mock_claim.side_effect = [False, False, True]
        result = grab_next("delta")
        self.assertEqual(result["id"], "t3")


if __name__ == "__main__":
    unittest.main()
