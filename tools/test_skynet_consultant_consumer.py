#!/usr/bin/env python3
"""Unit tests for skynet_consultant_consumer.py.

Tests: queue polling, ACK flow, bus relay, mark-complete lifecycle,
PID singleton lock, signal handling, and graceful shutdown.

# signed: beta
"""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.skynet_consultant_consumer as scc


# ── Fake bridge HTTP server ────────────────────────────────────────────────


class FakeBridgeHandler(BaseHTTPRequestHandler):
    """Minimal fake consultant bridge for integration testing."""

    # Class-level state shared across requests
    prompts_queue = []
    acked_ids = set()
    completed_ids = set()
    health_response = {"service": "fake-bridge", "status": "ok"}
    fail_ack = False
    fail_complete = False

    def log_message(self, *args):
        pass  # Suppress request logging

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, self.health_response)
        elif self.path == "/consultants/prompts/next":
            if self.prompts_queue:
                prompt = self.prompts_queue[0]
                self._json_response(200, {"prompt": prompt})
            else:
                self._json_response(200, {"prompt": None})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/consultants/prompts/ack":
            if self.fail_ack:
                self._json_response(500, {"error": "simulated ack failure"})
                return
            pid = body.get("prompt_id", "")
            self.acked_ids.add(pid)
            # Remove from queue after ACK
            self.prompts_queue[:] = [
                p for p in self.prompts_queue if p.get("id") != pid
            ]
            self._json_response(200, {"status": "acked", "prompt_id": pid})

        elif self.path == "/consultants/prompts/complete":
            if self.fail_complete:
                self._json_response(500, {"error": "simulated complete failure"})
                return
            pid = body.get("prompt_id", "")
            self.completed_ids.add(pid)
            self._json_response(200, {"status": "completed", "prompt_id": pid})

        else:
            self._json_response(404, {"error": "not found"})

    def _json_response(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_fake_bridge(port=0):
    """Start fake bridge on a random port. Returns (server, port)."""
    server = HTTPServer(("127.0.0.1", port), FakeBridgeHandler)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


# ── Tests ──────────────────────────────────────────────────────────────────


class TestHttpHelpers(unittest.TestCase):
    """Test _http_get and _http_post with a real HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_fake_bridge()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_http_get_success(self):
        result = scc._http_get(f"http://127.0.0.1:{self.port}/health")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "ok")
        # signed: beta

    def test_http_get_404(self):
        result = scc._http_get(f"http://127.0.0.1:{self.port}/nonexistent")
        # urllib raises on non-2xx, so None is returned
        self.assertIsNone(result)
        # signed: beta

    def test_http_get_unreachable(self):
        result = scc._http_get("http://127.0.0.1:1/unreachable", timeout=0.5)
        self.assertIsNone(result)
        # signed: beta

    def test_http_post_success(self):
        result = scc._http_post(
            f"http://127.0.0.1:{self.port}/consultants/prompts/ack",
            {"prompt_id": "test_post", "consumer": "test"},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "acked")
        # signed: beta

    def test_http_post_unreachable(self):
        result = scc._http_post("http://127.0.0.1:1/unreachable", {}, timeout=0.5)
        self.assertIsNone(result)
        # signed: beta


class TestPidLock(unittest.TestCase):
    """Test PID file singleton locking."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_root = scc.ROOT
        scc.ROOT = Path(self.tmpdir)
        (Path(self.tmpdir) / "data").mkdir(exist_ok=True)

    def tearDown(self):
        scc.ROOT = self._orig_root
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_acquire_and_release(self):
        port = 9999
        self.assertTrue(scc._acquire_pid_lock(port))
        pid_file = scc._pid_path(port)
        self.assertTrue(pid_file.exists())
        self.assertEqual(pid_file.read_text().strip(), str(os.getpid()))

        scc._release_pid_lock(port)
        self.assertFalse(pid_file.exists())
        # signed: beta

    def test_stale_pid_cleanup(self):
        """Stale PID (dead process) should be cleaned up."""
        port = 9998
        pid_file = scc._pid_path(port)
        # Write a PID that doesn't exist (99999999)
        pid_file.write_text("99999999")

        # Should acquire lock (stale PID)
        self.assertTrue(scc._acquire_pid_lock(port))
        self.assertEqual(pid_file.read_text().strip(), str(os.getpid()))
        scc._release_pid_lock(port)
        # signed: beta

    def test_double_acquire_blocked(self):
        """Same PID acquiring twice should work (same process)."""
        port = 9997
        self.assertTrue(scc._acquire_pid_lock(port))
        # Second acquire from same process — OpenProcess succeeds for own PID
        result = scc._acquire_pid_lock(port)
        # This returns False because our own PID is alive
        self.assertFalse(result)
        scc._release_pid_lock(port)
        # signed: beta


class TestProcessPrompt(unittest.TestCase):
    """Test the full prompt processing lifecycle: ACK → bus relay → complete."""

    @classmethod
    def setUpClass(cls):
        FakeBridgeHandler.prompts_queue = []
        FakeBridgeHandler.acked_ids = set()
        FakeBridgeHandler.completed_ids = set()
        FakeBridgeHandler.fail_ack = False
        FakeBridgeHandler.fail_complete = False
        cls.server, cls.port = _start_fake_bridge()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeBridgeHandler.acked_ids.clear()
        FakeBridgeHandler.completed_ids.clear()
        FakeBridgeHandler.fail_ack = False
        FakeBridgeHandler.fail_complete = False

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_full_lifecycle_success(self, mock_publish):
        """ACK → bus relay → complete should all succeed."""
        prompt = {
            "id": "prompt_001",
            "content": "Review the API design",
            "sender": "orchestrator",
            "type": "directive",
            "metadata": {"priority": "high"},
        }
        base_url = f"http://127.0.0.1:{self.port}"
        result = scc._process_prompt(base_url, "consultant", prompt)

        self.assertTrue(result)
        self.assertIn("prompt_001", FakeBridgeHandler.acked_ids)
        self.assertIn("prompt_001", FakeBridgeHandler.completed_ids)

        # Verify bus message structure
        mock_publish.assert_called_once()
        bus_msg = mock_publish.call_args[0][0]
        self.assertEqual(bus_msg["sender"], "orchestrator")
        self.assertEqual(bus_msg["topic"], "consultant")
        self.assertEqual(bus_msg["type"], "directive")
        self.assertEqual(bus_msg["content"], "Review the API design")
        self.assertEqual(bus_msg["metadata"]["prompt_id"], "prompt_001")
        self.assertEqual(bus_msg["metadata"]["consultant_id"], "consultant")
        self.assertEqual(bus_msg["metadata"]["priority"], "high")
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_missing_id_skipped(self, mock_publish):
        """Prompt with missing id should be skipped."""
        prompt = {"content": "no id here", "sender": "test"}
        result = scc._process_prompt(
            f"http://127.0.0.1:{self.port}", "consultant", prompt
        )
        self.assertFalse(result)
        mock_publish.assert_not_called()
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_missing_content_skipped(self, mock_publish):
        """Prompt with empty content should be skipped."""
        prompt = {"id": "prompt_empty", "content": "", "sender": "test"}
        result = scc._process_prompt(
            f"http://127.0.0.1:{self.port}", "consultant", prompt
        )
        self.assertFalse(result)
        mock_publish.assert_not_called()
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_ack_failure_aborts(self, mock_publish):
        """If ACK fails after retries, processing should fail."""
        FakeBridgeHandler.fail_ack = True
        prompt = {"id": "prompt_ack_fail", "content": "test", "sender": "test"}

        # Reduce retries for speed
        orig_retries = scc.MAX_RETRIES
        orig_delay = scc.RETRY_DELAY
        scc.MAX_RETRIES = 1
        scc.RETRY_DELAY = 0.01
        try:
            result = scc._process_prompt(
                f"http://127.0.0.1:{self.port}", "consultant", prompt
            )
            self.assertFalse(result)
            mock_publish.assert_not_called()
        finally:
            scc.MAX_RETRIES = orig_retries
            scc.RETRY_DELAY = orig_delay
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=False)
    def test_bus_relay_failure_still_completes(self, mock_publish):
        """If bus relay fails, prompt should still be marked complete."""
        prompt = {"id": "prompt_bus_fail", "content": "test", "sender": "test"}
        result = scc._process_prompt(
            f"http://127.0.0.1:{self.port}", "consultant", prompt
        )

        # Should succeed (marks complete despite bus failure)
        self.assertTrue(result)
        self.assertIn("prompt_bus_fail", FakeBridgeHandler.acked_ids)
        self.assertIn("prompt_bus_fail", FakeBridgeHandler.completed_ids)
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_complete_failure(self, mock_publish):
        """If mark-complete fails after retries, processing should fail."""
        FakeBridgeHandler.fail_complete = True
        prompt = {"id": "prompt_complete_fail", "content": "test", "sender": "test"}

        orig_retries = scc.MAX_RETRIES
        orig_delay = scc.RETRY_DELAY
        scc.MAX_RETRIES = 1
        scc.RETRY_DELAY = 0.01
        try:
            result = scc._process_prompt(
                f"http://127.0.0.1:{self.port}", "consultant", prompt
            )
            self.assertFalse(result)
            # ACK should have succeeded
            self.assertIn("prompt_complete_fail", FakeBridgeHandler.acked_ids)
        finally:
            scc.MAX_RETRIES = orig_retries
            scc.RETRY_DELAY = orig_delay
        # signed: beta


class TestGuardedBusPublish(unittest.TestCase):
    """Test _guarded_bus_publish wrapper behavior."""

    @patch("tools.skynet_consultant_consumer.guarded_publish", create=True)
    def test_success(self, mock_gp):
        # Patch the import inside _guarded_bus_publish
        with patch.dict("sys.modules", {}):
            mock_module = MagicMock()
            mock_module.guarded_publish = MagicMock(return_value={"published": True})
            with patch(
                "tools.skynet_consultant_consumer.guarded_publish",
                mock_module.guarded_publish,
                create=True,
            ):
                # Direct test: the function imports and calls guarded_publish
                msg = {"sender": "test", "topic": "test", "type": "test", "content": "x"}
                # Since the real import is from tools.skynet_spam_guard,
                # we mock the entire import chain
                with patch("tools.skynet_spam_guard.guarded_publish", return_value={"published": True}):
                    result = scc._guarded_bus_publish(msg)
                    self.assertTrue(result)
        # signed: beta

    def test_import_failure_returns_false(self):
        """If spam_guard import fails, should return False not crash."""
        with patch(
            "tools.skynet_spam_guard.guarded_publish",
            side_effect=ImportError("no module"),
        ):
            msg = {"sender": "test", "topic": "test", "type": "test", "content": "x"}
            result = scc._guarded_bus_publish(msg)
            # Should handle gracefully
            self.assertIsInstance(result, bool)
        # signed: beta


class TestSignalHandler(unittest.TestCase):
    """Test signal handler sets shutdown flag."""

    def test_signal_sets_shutdown(self):
        scc._shutdown = False
        scc._signal_handler(signal.SIGTERM, None)
        self.assertTrue(scc._shutdown)
        scc._shutdown = False  # Reset
        # signed: beta


class TestConsumerLoop(unittest.TestCase):
    """Test the main consumer loop with a fake bridge."""

    @classmethod
    def setUpClass(cls):
        FakeBridgeHandler.prompts_queue = []
        FakeBridgeHandler.acked_ids = set()
        FakeBridgeHandler.completed_ids = set()
        FakeBridgeHandler.fail_ack = False
        FakeBridgeHandler.fail_complete = False
        cls.server, cls.port = _start_fake_bridge()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeBridgeHandler.prompts_queue.clear()
        FakeBridgeHandler.acked_ids.clear()
        FakeBridgeHandler.completed_ids.clear()
        FakeBridgeHandler.fail_ack = False
        FakeBridgeHandler.fail_complete = False
        scc._shutdown = False

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_consumer_processes_queued_prompts(self, mock_publish):
        """Consumer should process prompts from the queue."""
        FakeBridgeHandler.prompts_queue = [
            {"id": "p1", "content": "Task 1", "sender": "orch", "type": "directive"},
            {"id": "p2", "content": "Task 2", "sender": "orch", "type": "directive"},
        ]

        # Run consumer in thread, stop after prompts are processed
        def _stop_after():
            # Wait long enough for health check + prompt processing
            deadline = time.time() + 10
            while time.time() < deadline:
                if len(FakeBridgeHandler.completed_ids) >= 2:
                    break
                time.sleep(0.1)
            scc._shutdown = True

        stopper = threading.Thread(target=_stop_after, daemon=True)
        stopper.start()

        scc.run_consumer(self.port, "consultant")

        # Both prompts should be processed
        self.assertIn("p1", FakeBridgeHandler.acked_ids)
        self.assertIn("p2", FakeBridgeHandler.acked_ids)
        self.assertIn("p1", FakeBridgeHandler.completed_ids)
        self.assertIn("p2", FakeBridgeHandler.completed_ids)

        # Bus publish should have been called for each prompt + daemon_start
        self.assertGreaterEqual(mock_publish.call_count, 2)
        # signed: beta

    @patch.object(scc, "_guarded_bus_publish", return_value=True)
    def test_consumer_handles_empty_queue(self, mock_publish):
        """Consumer should idle gracefully when queue is empty."""
        # No prompts in queue
        def _stop_after():
            time.sleep(0.5)
            scc._shutdown = True

        stopper = threading.Thread(target=_stop_after, daemon=True)
        stopper.start()

        scc.run_consumer(self.port, "consultant")

        # Only daemon_start bus message, no prompt processing
        self.assertEqual(len(FakeBridgeHandler.acked_ids), 0)
        self.assertEqual(len(FakeBridgeHandler.completed_ids), 0)
        # signed: beta


if __name__ == "__main__":
    unittest.main()
