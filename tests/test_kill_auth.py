"""Integration tests for skynet_kill_auth.py consensus voting flow.

Tests the propose, vote, approve, reject, and protected-process paths
using mock bus and file I/O (no real network or process termination).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import skynet_kill_auth as ka


class TestKillAuthHelpers(unittest.TestCase):
    """Test low-level helpers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_kill_log = ka.KILL_LOG
        self._orig_pending = ka.PENDING_FILE
        self._orig_critical = ka.CRITICAL_FILE
        ka.KILL_LOG = Path(self.tmpdir) / "kill_log.json"
        ka.PENDING_FILE = Path(self.tmpdir) / "kill_pending.json"
        ka.CRITICAL_FILE = Path(self.tmpdir) / "critical_processes.json"

    def tearDown(self):
        ka.KILL_LOG = self._orig_kill_log
        ka.PENDING_FILE = self._orig_pending
        ka.CRITICAL_FILE = self._orig_critical
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_protected_no_file(self):
        """No critical_processes.json -> not protected."""
        protected, reason = ka._is_protected(pid=1234, name="test.exe")
        self.assertFalse(protected)
        self.assertEqual(reason, "")

    def test_is_protected_by_name(self):
        """Protected names should be caught."""
        ka.CRITICAL_FILE.write_text(json.dumps({
            "protected_names": ["skynet.exe", "god_console.py", "watchdog"],
            "processes": [],
        }), encoding="utf-8")
        protected, reason = ka._is_protected(name="god_console.py")
        self.assertTrue(protected)
        self.assertIn("god_console.py", reason)

    def test_is_protected_by_pid(self):
        """Protected PIDs should be caught."""
        ka.CRITICAL_FILE.write_text(json.dumps({
            "protected_names": [],
            "processes": [{"pid": 9999, "name": "skynet.exe", "role": "backend"}],
        }), encoding="utf-8")
        protected, reason = ka._is_protected(pid=9999)
        self.assertTrue(protected)
        self.assertIn("backend", reason)

    def test_not_protected(self):
        """Non-protected process should pass."""
        ka.CRITICAL_FILE.write_text(json.dumps({
            "protected_names": ["skynet.exe"],
            "processes": [],
        }), encoding="utf-8")
        protected, reason = ka._is_protected(name="random_tool.exe")
        self.assertFalse(protected)

    def test_kill_log_append(self):
        """Kill log should accumulate entries."""
        ka._append_kill_log({"request_id": "test1", "decision": "DENIED"})
        ka._append_kill_log({"request_id": "test2", "decision": "AUTHORIZED"})
        logs = json.loads(ka.KILL_LOG.read_text(encoding="utf-8"))
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["request_id"], "test1")
        self.assertEqual(logs[1]["decision"], "AUTHORIZED")

    def test_kill_log_cap_at_500(self):
        """Kill log should cap at 500 entries."""
        for i in range(510):
            ka._append_kill_log({"request_id": f"test_{i}"})
        logs = json.loads(ka.KILL_LOG.read_text(encoding="utf-8"))
        self.assertEqual(len(logs), 500)
        self.assertEqual(logs[0]["request_id"], "test_10")

    def test_pending_crud(self):
        """Pending request add/get/remove cycle."""
        req = {"request_id": "kill_abc", "pid": 123, "status": "voting"}
        ka._add_pending(req)
        fetched = ka._get_pending("kill_abc")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["pid"], 123)

        ka._remove_pending("kill_abc")
        self.assertIsNone(ka._get_pending("kill_abc"))


class TestRequestKill(unittest.TestCase):
    """Test worker-side kill request posting."""

    @patch("skynet_kill_auth._bus_post")
    def test_request_kill_returns_id(self, mock_post):
        mock_post.return_value = True
        rid = ka.request_kill(pid=1234, name="test.exe", reason="duplicate", requester="gamma")
        self.assertTrue(rid.startswith("kill_"))
        self.assertIn("gamma", rid)
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        self.assertEqual(msg["sender"], "gamma")
        self.assertEqual(msg["type"], "kill_request")

    @patch("skynet_kill_auth._bus_post")
    def test_request_kill_bus_failure(self, mock_post):
        mock_post.return_value = False
        rid = ka.request_kill(pid=5555, name="foo.exe", reason="test", requester="alpha")
        self.assertTrue(rid.startswith("kill_"))
        mock_post.assert_called_once()


class TestVoteKill(unittest.TestCase):
    """Test worker-side vote posting."""

    @patch("skynet_kill_auth._bus_post")
    def test_vote_safe(self, mock_post):
        mock_post.return_value = True
        result = ka.vote_kill("kill_123", "beta", safe=True, reason="not using it")
        self.assertTrue(result)
        msg = mock_post.call_args[0][0]
        self.assertEqual(msg["type"], "kill_consensus_vote")
        content = json.loads(msg["content"])
        self.assertTrue(content["safe"])
        self.assertEqual(content["worker"], "beta")

    @patch("skynet_kill_auth._bus_post")
    def test_vote_unsafe(self, mock_post):
        mock_post.return_value = True
        result = ka.vote_kill("kill_123", "alpha", safe=False, reason="I depend on it")
        self.assertTrue(result)
        content = json.loads(mock_post.call_args[0][0]["content"])
        self.assertFalse(content["safe"])


class TestBroadcastConsensus(unittest.TestCase):
    """Test orchestrator-side consensus broadcast."""

    @patch("skynet_kill_auth._bus_post")
    def test_broadcast(self, mock_post):
        mock_post.return_value = True
        ok = ka.broadcast_consensus_check("kill_99", 1234, "test.exe", "dup", "gamma")
        self.assertTrue(ok)
        msg = mock_post.call_args[0][0]
        self.assertEqual(msg["topic"], "workers")
        self.assertEqual(msg["type"], "kill_consensus_check")
        content = json.loads(msg["content"])
        self.assertEqual(content["request_id"], "kill_99")
        self.assertIn("deadline", content)


class TestCollectVotes(unittest.TestCase):
    """Test vote collection with mocked bus."""

    @patch("skynet_kill_auth._bus_poll")
    def test_all_safe_unanimous(self, mock_poll):
        """All 4 workers vote safe -> approved."""
        votes = []
        for w in ka.WORKERS:
            votes.append({
                "type": "kill_consensus_vote",
                "content": json.dumps({
                    "request_id": "kill_test",
                    "worker": w,
                    "safe": True,
                    "reason": "ok",
                    "timestamp": "2026-01-01T00:00:00",
                }),
            })
        mock_poll.return_value = votes
        all_safe, result_votes = ka.collect_votes("kill_test", timeout=2)
        self.assertTrue(all_safe)
        self.assertEqual(len(result_votes), 4)
        for w in ka.WORKERS:
            self.assertTrue(result_votes[w]["safe"])

    @patch("skynet_kill_auth._bus_poll")
    def test_one_blocks(self, mock_poll):
        """One worker votes unsafe -> denied."""
        votes = []
        for w in ka.WORKERS:
            safe = (w != "delta")
            votes.append({
                "type": "kill_consensus_vote",
                "content": json.dumps({
                    "request_id": "kill_test2",
                    "worker": w,
                    "safe": safe,
                    "reason": "blocked" if not safe else "ok",
                    "timestamp": "2026-01-01T00:00:00",
                }),
            })
        mock_poll.return_value = votes
        all_safe, result_votes = ka.collect_votes("kill_test2", timeout=2)
        self.assertFalse(all_safe)
        self.assertFalse(result_votes["delta"]["safe"])

    @patch("skynet_kill_auth._bus_poll")
    def test_missing_votes_block(self, mock_poll):
        """Missing votes count as block."""
        votes = [{
            "type": "kill_consensus_vote",
            "content": json.dumps({
                "request_id": "kill_test3",
                "worker": "alpha",
                "safe": True,
                "reason": "ok",
                "timestamp": "2026-01-01T00:00:00",
            }),
        }]
        mock_poll.return_value = votes
        all_safe, result_votes = ka.collect_votes("kill_test3", timeout=1)
        self.assertFalse(all_safe)
        # alpha voted, rest should be NO RESPONSE
        self.assertTrue(result_votes["alpha"]["safe"])
        self.assertFalse(result_votes["beta"]["safe"])
        self.assertIn("NO RESPONSE", result_votes["beta"]["reason"])


class TestProcessKillRequest(unittest.TestCase):
    """Test full orchestrator authorization flow."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_kill_log = ka.KILL_LOG
        self._orig_pending = ka.PENDING_FILE
        self._orig_critical = ka.CRITICAL_FILE
        ka.KILL_LOG = Path(self.tmpdir) / "kill_log.json"
        ka.PENDING_FILE = Path(self.tmpdir) / "kill_pending.json"
        ka.CRITICAL_FILE = Path(self.tmpdir) / "critical_processes.json"

    def tearDown(self):
        ka.KILL_LOG = self._orig_kill_log
        ka.PENDING_FILE = self._orig_pending
        ka.CRITICAL_FILE = self._orig_critical
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("skynet_kill_auth.collect_votes")
    @patch("skynet_kill_auth.broadcast_consensus_check")
    @patch("skynet_kill_auth._bus_post")
    def test_approve_flow(self, mock_post, mock_broadcast, mock_collect):
        """Full approval: non-protected + all vote safe -> AUTHORIZED."""
        mock_broadcast.return_value = True
        mock_post.return_value = True
        mock_collect.return_value = (True, {
            w: {"safe": True, "reason": "ok", "timestamp": ""} for w in ka.WORKERS
        })

        authorized, reason, votes = ka.process_kill_request({
            "request_id": "kill_approve_test",
            "pid": 8888,
            "name": "harmless.exe",
            "reason": "test cleanup",
            "requester": "gamma",
        })

        self.assertTrue(authorized)
        self.assertIn("safe", reason)
        logs = json.loads(ka.KILL_LOG.read_text(encoding="utf-8"))
        self.assertEqual(logs[0]["decision"], "AUTHORIZED")

    @patch("skynet_kill_auth.collect_votes")
    @patch("skynet_kill_auth.broadcast_consensus_check")
    @patch("skynet_kill_auth._bus_post")
    def test_reject_flow(self, mock_post, mock_broadcast, mock_collect):
        """Rejection: one voter blocks -> DENIED."""
        mock_broadcast.return_value = True
        mock_post.return_value = True
        mock_collect.return_value = (False, {
            "alpha": {"safe": True, "reason": "ok", "timestamp": ""},
            "beta": {"safe": True, "reason": "ok", "timestamp": ""},
            "gamma": {"safe": False, "reason": "I need this", "timestamp": ""},
            "delta": {"safe": True, "reason": "ok", "timestamp": ""},
        })

        authorized, reason, votes = ka.process_kill_request({
            "request_id": "kill_reject_test",
            "pid": 7777,
            "name": "needed.exe",
            "reason": "cleanup",
            "requester": "alpha",
        })

        self.assertFalse(authorized)
        self.assertIn("gamma", reason)
        logs = json.loads(ka.KILL_LOG.read_text(encoding="utf-8"))
        self.assertEqual(logs[0]["decision"], "DENIED")

    @patch("skynet_kill_auth._bus_post")
    def test_protected_instant_deny(self, mock_post):
        """Protected process -> instant DENIED without voting."""
        mock_post.return_value = True
        ka.CRITICAL_FILE.write_text(json.dumps({
            "protected_names": ["skynet.exe", "god_console.py"],
            "processes": [],
        }), encoding="utf-8")

        authorized, reason, votes = ka.process_kill_request({
            "request_id": "kill_protected_test",
            "pid": 1111,
            "name": "god_console.py",
            "reason": "seems stuck",
            "requester": "beta",
        })

        self.assertFalse(authorized)
        self.assertIn("PROTECTED", reason.upper()) or self.assertIn("Protected", reason)
        self.assertEqual(votes, {})
        logs = json.loads(ka.KILL_LOG.read_text(encoding="utf-8"))
        self.assertEqual(logs[0]["decision"], "DENIED")
        self.assertIn("god_console", logs[0]["deny_reason"])


class TestEndToEnd(unittest.TestCase):
    """Simulate full end-to-end flow without real bus."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_kill_log = ka.KILL_LOG
        self._orig_pending = ka.PENDING_FILE
        self._orig_critical = ka.CRITICAL_FILE
        ka.KILL_LOG = Path(self.tmpdir) / "kill_log.json"
        ka.PENDING_FILE = Path(self.tmpdir) / "kill_pending.json"
        ka.CRITICAL_FILE = Path(self.tmpdir) / "critical_processes.json"

    def tearDown(self):
        ka.KILL_LOG = self._orig_kill_log
        ka.PENDING_FILE = self._orig_pending
        ka.CRITICAL_FILE = self._orig_critical
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("skynet_kill_auth._bus_poll")
    @patch("skynet_kill_auth._bus_post")
    def test_full_approve_e2e(self, mock_post, mock_poll):
        """E2E: request -> broadcast -> all vote safe -> authorized."""
        mock_post.return_value = True

        # Worker requests kill
        rid = ka.request_kill(pid=5000, name="dup.exe", reason="duplicate", requester="delta")

        # Mock all 4 workers voting safe
        mock_poll.return_value = [
            {"type": "kill_consensus_vote", "content": json.dumps({
                "request_id": rid, "worker": w, "safe": True,
                "reason": "ok", "timestamp": "2026-01-01T00:00:00",
            })}
            for w in ka.WORKERS
        ]

        authorized, reason, votes = ka.process_kill_request({
            "request_id": rid, "pid": 5000, "name": "dup.exe",
            "reason": "duplicate", "requester": "delta",
        })

        self.assertTrue(authorized)
        self.assertEqual(len(votes), 4)

    @patch("skynet_kill_auth._bus_poll")
    @patch("skynet_kill_auth._bus_post")
    def test_full_deny_e2e(self, mock_post, mock_poll):
        """E2E: request -> one blocks -> denied."""
        mock_post.return_value = True

        rid = ka.request_kill(pid=6000, name="service.exe", reason="cleanup", requester="alpha")

        votes_data = []
        for w in ka.WORKERS:
            safe = (w != "beta")
            votes_data.append({"type": "kill_consensus_vote", "content": json.dumps({
                "request_id": rid, "worker": w, "safe": safe,
                "reason": "I use this" if not safe else "ok",
                "timestamp": "2026-01-01T00:00:00",
            })})
        mock_poll.return_value = votes_data

        authorized, reason, votes = ka.process_kill_request({
            "request_id": rid, "pid": 6000, "name": "service.exe",
            "reason": "cleanup", "requester": "alpha",
        })

        self.assertFalse(authorized)
        self.assertIn("beta", reason)


if __name__ == "__main__":
    unittest.main()
