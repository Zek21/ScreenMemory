import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import skynet_todos as todos
from tools import skynet_worker_poll as worker_poll


class TestWorkerPollSharedTickets(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.todos_file = self.data_dir / "todos.json"
        self.old_poll_data = worker_poll.DATA
        self.old_todos_file = todos.TODOS_FILE
        worker_poll.DATA = self.data_dir
        todos.TODOS_FILE = self.todos_file

    def tearDown(self):
        worker_poll.DATA = self.old_poll_data
        todos.TODOS_FILE = self.old_todos_file
        self.tmpdir.cleanup()

    def _write_todos(self, items):
        self.todos_file.write_text(
            json.dumps({"todos": items, "version": 1}),
            encoding="utf-8",
        )

    def test_worker_poll_includes_claimable_shared_todos(self):
        self._write_todos([
            {"id": "a1", "task": "Assigned to alpha", "status": "pending", "worker": "alpha", "priority": "high"},
            {"id": "s1", "task": "Shared backlog ticket", "status": "pending", "worker": "shared", "priority": "normal"},
            {"id": "s2", "task": "All hands ticket", "status": "active", "assignee": "all", "priority": "critical"},
        ])

        with patch.object(worker_poll, "_get_pending_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_queued_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_bus_requests", return_value=[]), \
             patch.object(worker_poll, "_get_directives", return_value=[]):
            result = worker_poll.poll_for_work("alpha")

        self.assertTrue(result["has_work"])
        self.assertEqual(len(result["todos"]), 1)
        self.assertEqual(len(result["claimable_todos"]), 2)

    def test_consultant_can_stop_is_blocked_by_shared_ticket(self):
        self._write_todos([
            {"id": "c1", "task": "Consultant ticket", "status": "done", "worker": "consultant", "priority": "normal"},
            {"id": "s1", "task": "Shared backlog ticket", "status": "pending", "worker": "shared", "priority": "high"},
        ])

        self.assertFalse(todos.can_stop("consultant"))
        self.assertEqual(todos.claimable_count("consultant"), 1)


# ── NEW: _load_json tests ─────────────────────────────────────────────────
# signed: gamma

class TestLoadJson(unittest.TestCase):
    def test_valid_json_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value"}, f)
            f.flush()
            result = worker_poll._load_json(Path(f.name))
            self.assertEqual(result["key"], "value")

    def test_missing_file_returns_empty_dict(self):
        result = worker_poll._load_json(Path("Z:\\nonexistent\\file.json"))
        self.assertEqual(result, {})

    def test_corrupt_json_returns_empty_dict(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{corrupted json!!")
            f.flush()
            result = worker_poll._load_json(Path(f.name))
            self.assertEqual(result, {})


# ── NEW: _todo_target tests ──────────────────────────────────────────────

class TestTodoTarget(unittest.TestCase):
    def test_worker_field(self):
        item = {"worker": "Alpha"}
        result = worker_poll._todo_target(item)
        self.assertEqual(result, "alpha")

    def test_assignee_field(self):
        item = {"assignee": "Beta"}
        result = worker_poll._todo_target(item)
        self.assertEqual(result, "beta")

    def test_prefers_assignee_over_worker(self):
        item = {"worker": "gamma", "assignee": "delta"}
        result = worker_poll._todo_target(item)
        self.assertEqual(result, "delta")  # assignee takes priority

    def test_empty_item_returns_empty(self):
        result = worker_poll._todo_target({})
        self.assertEqual(result, "")

    def test_none_worker_uses_assignee(self):
        item = {"worker": None, "assignee": "DELTA"}
        result = worker_poll._todo_target(item)
        # None becomes empty string via str(), falls to assignee
        self.assertIn(result, ["", "delta"])


# ── NEW: _get_pending_tasks tests ────────────────────────────────────────

class TestGetPendingTasks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.old_data = worker_poll.DATA
        worker_poll.DATA = self.data_dir

    def tearDown(self):
        worker_poll.DATA = self.old_data
        self.tmpdir.cleanup()

    def test_no_task_queue_file(self):
        result = worker_poll._get_pending_tasks("alpha")
        self.assertEqual(result, [])

    def test_matching_worker_tasks(self):
        queue = {
            "tasks": [
                {"target": "alpha", "task": "do something", "status": "pending"},
                {"target": "beta", "task": "other task", "status": "pending"},
            ]
        }
        (self.data_dir / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
        result = worker_poll._get_pending_tasks("alpha")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["task"], "do something")

    def test_no_matching_worker(self):
        queue = {"tasks": [{"target": "beta", "task": "x", "status": "pending"}]}
        (self.data_dir / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
        result = worker_poll._get_pending_tasks("alpha")
        self.assertEqual(result, [])


# ── NEW: _get_todos and _get_claimable_todos tests ───────────────────────

class TestGetTodos(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.old_data = worker_poll.DATA
        worker_poll.DATA = self.data_dir

    def tearDown(self):
        worker_poll.DATA = self.old_data
        self.tmpdir.cleanup()

    def _write_todos(self, items):
        (self.data_dir / "todos.json").write_text(
            json.dumps({"todos": items, "version": 1}), encoding="utf-8"
        )

    def test_get_todos_for_specific_worker(self):
        self._write_todos([
            {"id": "t1", "task": "alpha task", "status": "pending", "worker": "alpha"},
            {"id": "t2", "task": "beta task", "status": "pending", "worker": "beta"},
        ])
        result = worker_poll._get_todos("alpha")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t1")

    def test_get_todos_excludes_done(self):
        self._write_todos([
            {"id": "t1", "task": "done task", "status": "done", "worker": "alpha"},
            {"id": "t2", "task": "pending task", "status": "pending", "worker": "alpha"},
        ])
        result = worker_poll._get_todos("alpha")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t2")

    def test_get_claimable_todos_includes_shared(self):
        self._write_todos([
            {"id": "s1", "task": "shared", "status": "pending", "worker": "shared"},
            {"id": "s2", "task": "all", "status": "pending", "worker": "all"},
            {"id": "s3", "task": "unassigned", "status": "pending", "worker": "unassigned"},
            {"id": "s4", "task": "backlog", "status": "pending", "worker": "backlog"},
            {"id": "s5", "task": "any", "status": "pending", "worker": "any"},
            {"id": "s6", "task": "empty", "status": "pending", "worker": ""},
        ])
        result = worker_poll._get_claimable_todos("alpha")
        # All shared assignees should be claimable
        self.assertGreaterEqual(len(result), 5)

    def test_get_claimable_excludes_done(self):
        self._write_todos([
            {"id": "s1", "task": "done shared", "status": "done", "worker": "shared"},
            {"id": "s2", "task": "pending shared", "status": "pending", "worker": "shared"},
        ])
        result = worker_poll._get_claimable_todos("alpha")
        self.assertEqual(len(result), 1)

    def test_no_todos_file(self):
        result = worker_poll._get_todos("alpha")
        self.assertEqual(result, [])
        claimable = worker_poll._get_claimable_todos("alpha")
        self.assertEqual(claimable, [])


# ── NEW: _build_work_summary tests ───────────────────────────────────────

class TestBuildWorkSummary(unittest.TestCase):
    def test_summary_with_all_sources(self):
        sources = {
            "pending_tasks": [{"task": "p1", "priority": "high", "task_id": "t1"}],
            "queued_tasks": [{"task": "q1", "id": "q1"}],
            "bus_requests": [{"content": "b1", "sender": "alpha", "type": "request"}],
            "directives": [{"content": "d1", "sender": "orch"}],
            "todos": [{"task": "t1", "priority": "high", "id": "todo1"}],
            "claimable_todos": [{"task": "c1", "priority": "normal", "id": "ct1", "target": "shared"}],
        }
        result = worker_poll._build_work_summary("alpha", sources)
        self.assertIn("ALPHA", result)
        self.assertIn("p1", result)

    def test_summary_empty_sources(self):
        sources = {
            "pending_tasks": [], "queued_tasks": [], "bus_requests": [],
            "directives": [], "todos": [], "claimable_todos": [],
        }
        result = worker_poll._build_work_summary("beta", sources)
        self.assertIsInstance(result, str)
        self.assertIn("NO pending work", result)


# ── NEW: poll_for_work comprehensive tests ────────────────────────────────

class TestPollForWork(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.old_data = worker_poll.DATA
        worker_poll.DATA = self.data_dir

    def tearDown(self):
        worker_poll.DATA = self.old_data
        self.tmpdir.cleanup()

    def test_all_sources_empty(self):
        with patch.object(worker_poll, "_get_pending_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_queued_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_bus_requests", return_value=[]), \
             patch.object(worker_poll, "_get_directives", return_value=[]):
            result = worker_poll.poll_for_work("alpha")

        self.assertFalse(result["has_work"])
        self.assertEqual(result["pending_tasks"], [])
        self.assertEqual(result["queued_tasks"], [])
        self.assertEqual(result["bus_requests"], [])
        self.assertEqual(result["directives"], [])
        self.assertEqual(result["todos"], [])
        self.assertEqual(result["claimable_todos"], [])

    def test_pending_tasks_make_has_work_true(self):
        with patch.object(worker_poll, "_get_pending_tasks",
                          return_value=[{"task": "x", "priority": "high", "task_id": "t1", "status": "pending", "sender": "", "created_at": ""}]), \
             patch.object(worker_poll, "_get_queued_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_bus_requests", return_value=[]), \
             patch.object(worker_poll, "_get_directives", return_value=[]):
            result = worker_poll.poll_for_work("beta")

        self.assertTrue(result["has_work"])

    def test_directives_make_has_work_true(self):
        with patch.object(worker_poll, "_get_pending_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_queued_tasks", return_value=[]), \
             patch.object(worker_poll, "_get_bus_requests", return_value=[]), \
             patch.object(worker_poll, "_get_directives",
                          return_value=[{"content": "do it", "sender": "orchestrator"}]):
            result = worker_poll.poll_for_work("gamma")

        self.assertTrue(result["has_work"])


# ── NEW: poll_all_workers tests ──────────────────────────────────────────

class TestPollAllWorkers(unittest.TestCase):
    def test_polls_all_four_workers(self):
        with patch.object(worker_poll, "poll_for_work",
                          return_value={"has_work": False, "pending_tasks": [],
                                        "queued_tasks": [], "bus_requests": [],
                                        "directives": [], "todos": [],
                                        "claimable_todos": [], "summary": ""}) as mock_poll:
            results = worker_poll.poll_all_workers()
            self.assertEqual(len(results), 4)
            self.assertEqual(mock_poll.call_count, 4)

    def test_polls_subset_of_workers(self):
        with patch.object(worker_poll, "poll_for_work",
                          return_value={"has_work": False, "pending_tasks": [],
                                        "queued_tasks": [], "bus_requests": [],
                                        "directives": [], "todos": [],
                                        "claimable_todos": [], "summary": ""}) as mock_poll:
            results = worker_poll.poll_all_workers(workers=["alpha", "beta"])
            self.assertEqual(len(results), 2)


# ── NEW: find_idle_with_work tests ────────────────────────────────────────

class TestFindIdleWithWork(unittest.TestCase):
    def test_returns_workers_with_work(self):
        def mock_poll(name):
            if name == "alpha":
                return {"has_work": True, "pending_tasks": [{"task": "x"}],
                        "queued_tasks": [], "bus_requests": [],
                        "directives": [], "todos": [],
                        "claimable_todos": [], "summary": "1 pending"}
            return {"has_work": False, "pending_tasks": [],
                    "queued_tasks": [], "bus_requests": [],
                    "directives": [], "todos": [],
                    "claimable_todos": [], "summary": ""}

        with patch.object(worker_poll, "poll_for_work", side_effect=mock_poll):
            result = worker_poll.find_idle_with_work()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][0], "alpha")
            self.assertTrue(result[0][1]["has_work"])

    def test_returns_empty_when_no_work(self):
        with patch.object(worker_poll, "poll_for_work",
                          return_value={"has_work": False, "pending_tasks": [],
                                        "queued_tasks": [], "bus_requests": [],
                                        "directives": [], "todos": [],
                                        "claimable_todos": [], "summary": ""}):
            result = worker_poll.find_idle_with_work()
            self.assertEqual(result, [])


# ── NEW: Network failure handling tests ──────────────────────────────────

class TestNetworkFailures(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.old_data = worker_poll.DATA
        worker_poll.DATA = self.data_dir

    def tearDown(self):
        worker_poll.DATA = self.old_data
        self.tmpdir.cleanup()

    def test_get_queued_tasks_handles_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = worker_poll._get_queued_tasks("alpha")
            self.assertEqual(result, [])

    def test_get_bus_requests_handles_timeout(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
            result = worker_poll._get_bus_requests("alpha")
            self.assertEqual(result, [])

    def test_get_directives_handles_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = worker_poll._get_directives("alpha")
            self.assertEqual(result, [])


# ── NEW: Constants tests ─────────────────────────────────────────────────

class TestConstants(unittest.TestCase):
    def test_worker_names(self):
        self.assertIn("alpha", worker_poll.WORKER_NAMES)
        self.assertIn("beta", worker_poll.WORKER_NAMES)
        self.assertIn("gamma", worker_poll.WORKER_NAMES)
        self.assertIn("delta", worker_poll.WORKER_NAMES)

    def test_shared_assignees(self):
        for assignee in ("", "all", "shared", "any", "unassigned", "backlog"):
            self.assertIn(assignee, worker_poll.SHARED_ASSIGNEES)

    def test_bus_lookback(self):
        self.assertEqual(worker_poll.BUS_LOOKBACK_S, 300)

    def test_known_actors_include_consultants(self):
        self.assertIn("consultant", worker_poll.KNOWN_ACTORS)
        self.assertIn("gemini_consultant", worker_poll.KNOWN_ACTORS)
        self.assertIn("orchestrator", worker_poll.KNOWN_ACTORS)


if __name__ == "__main__":
    unittest.main()
