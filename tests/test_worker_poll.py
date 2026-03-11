import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
