import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch


class TestConsultantPromptQueue(unittest.TestCase):
    def setUp(self):
        import tools.skynet_consultant_bridge as bridge

        self.bridge = bridge
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prompt_file = Path(self.tmpdir.name) / "consultant_prompt_queue.json"
        self.state_file = Path(self.tmpdir.name) / "consultant_state.json"
        self.task_file = Path(self.tmpdir.name) / "consultant_task_state.json"
        self.registry_file = Path(self.tmpdir.name) / "consultant_registry.json"

        self.old_id = bridge.CONSULTANT_ID
        self.old_prompt = bridge.PROMPT_FILE
        self.old_state = bridge.STATE_FILE
        self.old_task = bridge.TASK_FILE
        self.old_registry = bridge.REGISTRY_FILE

        bridge.CONSULTANT_ID = "consultant"
        bridge.PROMPT_FILE = self.prompt_file
        bridge.STATE_FILE = self.state_file
        bridge.TASK_FILE = self.task_file
        bridge.REGISTRY_FILE = self.registry_file

    def tearDown(self):
        self.bridge.CONSULTANT_ID = self.old_id
        self.bridge.PROMPT_FILE = self.old_prompt
        self.bridge.STATE_FILE = self.old_state
        self.bridge.TASK_FILE = self.old_task
        self.bridge.REGISTRY_FILE = self.old_registry
        self.tmpdir.cleanup()

    def test_queue_ack_complete_lifecycle(self):
        queued = self.bridge.queue_prompt("orchestrator", "review the latest convene report")
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(self.bridge._prompt_stats()["pending"], 1)

        next_prompt = self.bridge.get_next_prompt()
        self.assertEqual(next_prompt["id"], queued["id"])

        acknowledged = self.bridge.acknowledge_prompt(queued["id"], consumer="codex_consultant")
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertEqual(self.bridge._prompt_stats()["acknowledged"], 1)

        completed = self.bridge.complete_prompt(queued["id"], result="done", status="completed")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.bridge._prompt_stats()["completed"], 1)

    def test_live_state_reports_prompt_transport(self):
        self.bridge.queue_prompt("orchestrator", "prompt one")
        with patch.object(self.bridge, "_load_worker_snapshot", return_value={
            "source": "realtime.json",
            "summary": {"total": 4, "available": 3, "busy": 1, "offline": 0},
            "workers": {},
            "available_workers": ["alpha", "beta", "gamma"],
            "busy_workers": ["delta"],
            "offline_workers": [],
        }):
            state = self.bridge.build_live_state(api_port=8422)
        self.assertTrue(state["accepts_prompts"])
        self.assertTrue(state["routable"])
        self.assertEqual(state["prompt_transport"], "bridge_queue")
        self.assertEqual(state["prompt_queue"]["pending"], 1)
        self.assertEqual(state["worker_snapshot"]["summary"]["available"], 3)

    def test_live_state_preserves_valid_consultant_hwnd(self):
        self.state_file.write_text(json.dumps({
            "hwnd": 424242,
            "prompt_transport": "ghost_type",
            "requires_hwnd": True,
        }), encoding="utf-8")
        with patch.object(self.bridge, "_load_worker_snapshot", return_value={
            "source": "realtime.json",
            "summary": {"total": 4, "available": 4, "busy": 0, "offline": 0},
            "workers": {},
            "available_workers": ["alpha", "beta", "gamma", "delta"],
            "busy_workers": [],
            "offline_workers": [],
        }), patch.object(self.bridge, "_window_alive", return_value=True), \
             patch.object(self.bridge, "_reserved_hwnds", return_value={}):
            state = self.bridge.build_live_state(api_port=8422)
        self.assertEqual(state["hwnd"], 424242)
        self.assertTrue(state["requires_hwnd"])
        self.assertEqual(state["prompt_transport"], "ghost_type")
        self.assertEqual(state["hwnd_validation"], "accepted")

    def test_live_state_rejects_reserved_skynet_hwnd(self):
        self.state_file.write_text(json.dumps({
            "hwnd": 8132014,
            "prompt_transport": "ghost_type",
            "requires_hwnd": True,
        }), encoding="utf-8")
        with patch.object(self.bridge, "_load_worker_snapshot", return_value={
            "source": "realtime.json",
            "summary": {"total": 4, "available": 4, "busy": 0, "offline": 0},
            "workers": {},
            "available_workers": ["alpha", "beta", "gamma", "delta"],
            "busy_workers": [],
            "offline_workers": [],
        }), patch.object(self.bridge, "_window_alive", return_value=True), \
             patch.object(self.bridge, "_reserved_hwnds", return_value={8132014: "orchestrator"}):
            state = self.bridge.build_live_state(api_port=8422)
        self.assertEqual(state["hwnd"], 0)
        self.assertFalse(state["requires_hwnd"])
        self.assertEqual(state["prompt_transport"], "bridge_queue")
        self.assertEqual(state["hwnd_validation"], "reserved_skynet_window")

    def test_registry_refresh_tracks_truthful_transport(self):
        self.bridge._refresh_consultant_registry({
            "display_name": "Codex Consultant",
            "model": "GPT-5 Codex",
            "hwnd": 424242,
            "api_port": 8422,
            "status": "LIVE",
            "prompt_transport": "ghost_type",
            "requires_hwnd": True,
            "routable": True,
        })
        registry = json.loads(self.registry_file.read_text(encoding="utf-8"))
        self.assertEqual(len(registry["consultants"]), 1)
        entry = registry["consultants"][0]
        self.assertEqual(entry["name"], "consultant")
        self.assertEqual(entry["hwnd"], 424242)
        self.assertEqual(entry["transport"], "ghost_type")
        self.assertTrue(entry["requires_hwnd"])

    def test_acknowledge_updates_task_state_and_publishes_claim(self):
        queued = self.bridge.queue_prompt("orchestrator", "take this task")
        with patch.object(self.bridge, "_load_worker_snapshot", return_value={
            "source": "realtime.json",
            "summary": {"total": 4, "available": 4, "busy": 0, "offline": 0},
            "workers": {},
            "available_workers": ["alpha", "beta", "gamma", "delta"],
            "busy_workers": [],
            "offline_workers": [],
        }), patch.object(self.bridge, "_publish_consultant_event", return_value=True) as post_mock:
            acknowledged = self.bridge.acknowledge_prompt(queued["id"], consumer="gemini_consultant")
        self.assertEqual(acknowledged["status"], "acknowledged")
        task_state = self.bridge._load_task_state()
        self.assertEqual(task_state["status"], "CLAIMED")
        self.assertEqual(task_state["prompt_id"], queued["id"])
        self.assertIn("take this task", task_state["task"])
        self.assertTrue(post_mock.called)

    def test_delegate_prompt_auto_selects_available_worker(self):
        queued = self.bridge.queue_prompt("orchestrator", "delegate this to the best worker")
        workers = {
            "source": "realtime.json",
            "summary": {"total": 4, "available": 2, "busy": 2, "offline": 0},
            "workers": {
                "alpha": {"status": "IDLE", "available": True},
                "beta": {"status": "WORKING", "available": False},
                "gamma": {"status": "IDLE", "available": True},
            },
            "available_workers": ["alpha", "gamma"],
            "busy_workers": ["beta"],
            "offline_workers": [],
        }
        with patch.object(self.bridge, "_load_worker_snapshot", return_value=workers), \
             patch.object(self.bridge, "_publish_consultant_event", return_value=True), \
             patch("tools.skynet_dispatch.dispatch_to_worker", return_value=True), \
             patch("tools.skynet_dispatch.load_workers", return_value=[]), \
             patch("tools.skynet_dispatch.load_orch_hwnd", return_value=123):
            result = self.bridge.delegate_prompt(queued["id"])
        self.assertTrue(result["success"])
        self.assertEqual(result["worker"], "alpha")
        task_state = self.bridge._load_task_state()
        self.assertEqual(task_state["status"], "DELEGATED")
        self.assertEqual(task_state["assigned_worker"], "alpha")

    def test_publish_consultant_event_stringifies_metadata(self):
        with patch("tools.skynet_spam_guard.guarded_publish", return_value={"allowed": True, "published": True}) as mock_gp:  # signed: gamma
            ok = self.bridge._publish_consultant_event(
                "task_claim",
                "Gemini Consultant accepted a task",
                metadata={"available_workers": ["alpha", "gamma"], "worker_count": 2, "routable": True},
            )
        self.assertTrue(ok)
        payload = mock_gp.call_args.args[0]
        self.assertEqual(payload["metadata"]["available_workers"], "[\"alpha\", \"gamma\"]")
        self.assertEqual(payload["metadata"]["worker_count"], "2")
        self.assertEqual(payload["metadata"]["routable"], "True")

    def test_atomic_write_retries_transient_permission_error(self):
        target = Path(self.tmpdir.name) / "bridge_state.json"
        real_replace = self.bridge.os.replace
        attempts = {"count": 0}

        def flaky_replace(src, dst):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        with patch.object(self.bridge.os, "replace", side_effect=flaky_replace), \
             patch.object(self.bridge.time, "sleep") as sleep_mock:
            self.bridge._atomic_write(target, {"status": "LIVE"})

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "LIVE")
        sleep_mock.assert_called_once()

    def test_heartbeat_loop_survives_transient_write_failure(self):
        calls = {"count": 0}

        def flaky_write(path, payload):
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError(13, "Access is denied")

        with patch.object(self.bridge, "_atomic_write", side_effect=flaky_write), \
             patch.object(self.bridge, "build_live_state", return_value={"status": "LIVE"}), \
             patch.object(self.bridge.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
             patch.object(self.bridge, "_refresh_consultant_registry"), \
             patch.object(self.bridge.sys, "stderr"):
            with self.assertRaises(KeyboardInterrupt):
                self.bridge._heartbeat_loop(2.0, 8.0, 8425)

        self.assertEqual(calls["count"], 2)


class TestConsultantDeliveryRouting(unittest.TestCase):
    def setUp(self):
        import tools.skynet_delivery as delivery

        self.delivery = delivery
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmpdir.name) / "consultant_state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_deliver_to_consultant_uses_bridge_queue(self):
        self.state_file.write_text(json.dumps({
            "id": "consultant",
            "live": True,
            "accepts_prompts": True,
            "api_url": "http://localhost:8422/consultants",
        }), encoding="utf-8")

        with patch.object(self.delivery, "_consultant_state_file", return_value=self.state_file), \
             patch.object(self.delivery, "_json_post", return_value={"status": "queued", "prompt": {"id": "prompt_1"}}), \
             patch.object(self.delivery, "_bus_post", return_value=True):
            result = self.delivery.deliver_to_consultant("consultant", "Investigate bridge drift")

        self.assertFalse(result["success"])
        self.assertEqual(result["method"], self.delivery.DeliveryMethod.HYBRID.value)
        self.assertIn("prompt_1", result["detail"])

    def test_consultant_routability_depends_on_live_bridge_state(self):
        self.state_file.write_text(json.dumps({
            "id": "consultant",
            "live": True,
            "accepts_prompts": True,
            "api_url": "http://localhost:8422/consultants",
        }), encoding="utf-8")
        with patch.object(self.delivery, "_consultant_state_file", return_value=self.state_file):
            self.assertTrue(self.delivery.is_routable("consultant"))

        self.state_file.write_text(json.dumps({
            "id": "consultant",
            "live": False,
            "api_url": "http://localhost:8422/consultants",
        }), encoding="utf-8")
        with patch.object(self.delivery, "_consultant_state_file", return_value=self.state_file):
            self.assertFalse(self.delivery.is_routable("consultant"))

    def test_consultant_routability_rejects_reserved_hwnd_without_bridge(self):
        self.state_file.write_text(json.dumps({
            "id": "consultant",
            "hwnd": 8132014,
            "live": False,
            "accepts_prompts": False,
        }), encoding="utf-8")
        with patch.object(self.delivery, "_consultant_state_file", return_value=self.state_file), \
             patch.object(self.delivery, "_reserved_skynet_hwnds", return_value={8132014}), \
             patch.object(self.delivery, "_is_window", return_value=True):
            self.assertFalse(self.delivery.is_routable("consultant"))


class TestDispatchSpecialTargets(unittest.TestCase):
    def test_dispatch_routes_orchestrator_via_delivery_layer(self):
        import tools.skynet_dispatch as dispatch

        with patch("tools.skynet_delivery.deliver_to_orchestrator", return_value={"success": True, "detail": "ok"}):
            ok = dispatch.dispatch_to_worker("orchestrator", "review the queue", workers=[], orch_hwnd=123)
        self.assertTrue(ok)

    def test_dispatch_routes_consultant_via_bridge_queue(self):
        import tools.skynet_dispatch as dispatch

        with patch("tools.skynet_delivery.deliver_to_consultant", return_value={"success": True, "detail": "queued"}):
            ok = dispatch.dispatch_to_worker("consultant", "investigate drift", workers=[], orch_hwnd=123)
        self.assertTrue(ok)


class TestGhostTypeTruthfulness(unittest.TestCase):
    def setUp(self):
        import tools.skynet_dispatch as dispatch

        self.dispatch = dispatch
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.tmpdir.name) / "dispatch.lock"
        self.old_lock_file = dispatch.DISPATCH_LOCK_FILE
        dispatch.DISPATCH_LOCK_FILE = self.lock_file
        # Mock IsWindow so fake HWNDs pass validation
        self._is_window_patcher = patch.object(dispatch.user32, "IsWindow", return_value=True)
        self._is_window_patcher.start()

    def tearDown(self):
        self._is_window_patcher.stop()
        self.dispatch.DISPATCH_LOCK_FILE = self.old_lock_file
        self.tmpdir.cleanup()

    def test_ghost_type_rejects_stderr_even_with_ok_marker(self):
        result = SimpleNamespace(
            stdout="OK_ATTACHED\n",
            stderr="Add-Type : compilation errors occurred",
            returncode=0,
        )
        with patch.object(self.dispatch.subprocess, "run", return_value=result), \
             patch.object(self.dispatch.time, "sleep", return_value=None):
            ok = self.dispatch.ghost_type_to_worker(111, "truth check", 222)
        self.assertFalse(ok)

    def test_ghost_type_requires_zero_returncode(self):
        result = SimpleNamespace(
            stdout="OK_ATTACHED\n",
            stderr="",
            returncode=1,
        )
        with patch.object(self.dispatch.subprocess, "run", return_value=result), \
             patch.object(self.dispatch.time, "sleep", return_value=None):
            ok = self.dispatch.ghost_type_to_worker(111, "truth check", 222)
        self.assertFalse(ok)

    def test_ghost_type_accepts_clean_success(self):
        result = SimpleNamespace(
            stdout="OK_FALLBACK\n",
            stderr="",
            returncode=0,
        )
        with patch.object(self.dispatch.subprocess, "run", return_value=result), \
             patch.object(self.dispatch.time, "sleep", return_value=None):
            ok = self.dispatch.ghost_type_to_worker(111, "truth check", 222)
        self.assertTrue(ok)


class TestGodConsoleConsultants(unittest.TestCase):
    def test_console_aggregates_multiple_consultant_bridges(self):
        import god_console

        payloads = {
            "http://localhost:8422/consultants": {
                "consultant": {
                    "id": "consultant",
                    "display_name": "Codex Consultant",
                    "live": True,
                    "status": "LIVE",
                    "heartbeat_age_s": 0.5,
                    "stale_after_s": 8,
                }
            },
            "http://localhost:8425/consultants": {
                "consultant": {
                    "id": "gemini_consultant",
                    "display_name": "Gemini Consultant",
                    "live": True,
                    "status": "LIVE",
                    "heartbeat_age_s": 0.3,
                    "stale_after_s": 8,
                }
            },
        }

        original_cache = dict(god_console._cache)
        try:
            god_console._cache["consultants"] = None
            god_console._cache["consultants_t"] = 0
            with patch.object(god_console, "_CONSULTANT_PORTS", (8422, 8425)), \
                 patch.object(god_console, "_fetch_backend", side_effect=lambda url, timeout=2, retries=0: payloads.get(url, {})):
                consultants = god_console._cached_consultants()
        finally:
            god_console._cache.update(original_cache)

        self.assertIn("consultant", consultants)
        self.assertIn("gemini_consultant", consultants)
        self.assertTrue(consultants["gemini_consultant"]["live"])


class TestWatchdogConsultantBridge(unittest.TestCase):
    def test_restart_consultant_bridge_starts_and_verifies_live_endpoint(self):
        import tools.skynet_watchdog as watchdog

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "consultant_bridge.pid"
            pid_file.write_text("44360", encoding="utf-8")
            config = {
                "service_name": "consultant_bridge",
                "label": "Codex Consultant bridge",
                "consultant_id": "consultant",
                "api_port": 8422,
                "pid_file": pid_file,
                "extra_args": [],
            }
            fake_proc = SimpleNamespace(pid=55123)
            with patch.object(watchdog, "_pid_alive", return_value=False), \
                 patch.object(watchdog.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 patch.object(watchdog.time, "sleep", return_value=None), \
                 patch.object(watchdog, "_consultant_bridge_is_healthy", return_value=True), \
                 patch.object(watchdog, "_post_restart_alert") as alert_mock, \
                 patch.object(watchdog, "_refresh_protected_registry") as refresh_mock, \
                 patch.object(watchdog, "_log_incident") as incident_mock:
                self.assertTrue(watchdog.restart_consultant_bridge(config))

        args = popen_mock.call_args.args[0]
        self.assertIn("tools\\skynet_consultant_bridge.py", " ".join(args))
        alert_mock.assert_called_once()
        refresh_mock.assert_called_once()
        incident_mock.assert_called_once()


class TestConsultantBootstrapTruth(unittest.TestCase):
    def test_cc_start_requires_bridge_health_before_live_claim(self):
        script = Path("CC-Start.ps1").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8422/health", script)
        self.assertIn("passed /health", script)  # signed: gamma
        self.assertIn("port 8422", script)

    def test_cc_start_god_console_probe_checks_ipv4_and_ipv6_loopback(self):
        script = Path("CC-Start.ps1").read_text(encoding="utf-8")
        self.assertIn('@("127.0.0.1", "localhost", "::1")', script)
        self.assertIn("function Test-GodConsoleTruth", script)
        self.assertIn("http://127.0.0.1:8421/leadership", script)
        self.assertIn("http://localhost:8421/dashboard/data", script)
        self.assertIn("failed live truth verification within 15s", script)

    def test_gc_start_requires_bridge_health_before_live_claim(self):
        script = Path("GC-Start.ps1").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8425/health", script)
        self.assertIn("started and passed /health + live heartbeat truth on port 8425", script)
        self.assertIn("failed live truth verification within 40s", script)
        self.assertIn("Gemini Consultant", script)
        self.assertIn("Gemini 3.1 Pro (Preview)", script)
        self.assertIn("function Quote-Arg", script)
        self.assertIn("-ArgumentList $bridgeArgString", script)

    def test_gc_start_god_console_probe_checks_ipv4_and_ipv6_loopback(self):
        script = Path("GC-Start.ps1").read_text(encoding="utf-8")
        self.assertIn('@("127.0.0.1", "localhost", "::1")', script)
        self.assertIn("function Test-GodConsoleTruth", script)
        self.assertIn("http://127.0.0.1:8421/leadership", script)
        self.assertIn("http://localhost:8421/dashboard/data", script)
        self.assertIn("failed live truth verification within 15s", script)


if __name__ == "__main__":
    unittest.main()
