import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class TestAgentTelemetry(unittest.TestCase):
    def setUp(self):
        import tools.skynet_agent_telemetry as telemetry

        self.telemetry = telemetry
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

        self.old_data_dir = telemetry.DATA_DIR
        self.old_workers_file = telemetry.WORKERS_FILE
        self.old_out_file = telemetry.OUT_FILE
        self.old_manual_file = telemetry.MANUAL_FILE
        self.old_pid_file = telemetry.PID_FILE
        self.old_cached_snapshot = telemetry._cached_snapshot
        self.old_cached_snapshot_t = telemetry._cached_snapshot_t

        telemetry.DATA_DIR = self.root
        telemetry.WORKERS_FILE = self.root / "workers.json"
        telemetry.OUT_FILE = self.root / "agent_telemetry.json"
        telemetry.MANUAL_FILE = self.root / "agent_telemetry_manual.json"
        telemetry.PID_FILE = self.root / "agent_telemetry.pid"
        telemetry._cached_snapshot = None
        telemetry._cached_snapshot_t = 0.0

    def tearDown(self):
        self.telemetry.DATA_DIR = self.old_data_dir
        self.telemetry.WORKERS_FILE = self.old_workers_file
        self.telemetry.OUT_FILE = self.old_out_file
        self.telemetry.MANUAL_FILE = self.old_manual_file
        self.telemetry.PID_FILE = self.old_pid_file
        self.telemetry._cached_snapshot = self.old_cached_snapshot
        self.telemetry._cached_snapshot_t = self.old_cached_snapshot_t
        self.tmpdir.cleanup()

    def _write_workers(self):
        payload = {
            "workers": [{"name": "alpha", "hwnd": 111}],
            "orchestrator_hwnd": 222,
        }
        self.telemetry.WORKERS_FILE.write_text(json.dumps(payload), encoding="utf-8")

    def test_collect_snapshot_merges_uia_backend_and_consultants(self):
        self._write_workers()
        (self.root / "gemini_consultant_state.json").write_text(json.dumps({
            "id": "gemini_consultant",
            "status": "LIVE",
            "live": True,
            "api_url": None,
            "prompt_transport": "bridge_queue",
        }), encoding="utf-8")
        (self.root / "gemini_consultant_task_state.json").write_text(json.dumps({
            "status": "CLAIMED",
            "task": "Review consultant protocol",
        }), encoding="utf-8")

        backend = {
            "agents": {
                "alpha": {"current_task": "Run pytest", "model": "Claude Opus"},
                "orchestrator": {"current_task": "", "model": "Claude Opus"},
            },
            "orch_thinking": [{"id": "th1", "text": "Reviewing live telemetry design"}],
        }
        fake_engine = SimpleNamespace(
            scan_all=lambda hwnds, max_workers=5: {
                "alpha": SimpleNamespace(hwnd=111, state="TYPING", edit_value="pytest -q", model="Claude Opus"),
                "orchestrator": SimpleNamespace(hwnd=222, state="IDLE", edit_value="", model="Claude Opus"),
            }
        )

        with patch.object(self.telemetry, "_fetch_json", side_effect=lambda url, timeout=1.5: backend if url == self.telemetry.SKYNET_STATUS_URL else None), \
             patch("tools.uia_engine.get_engine", return_value=fake_engine):
            snapshot = self.telemetry.collect_snapshot()

        alpha = snapshot["agents"]["alpha"]
        orch = snapshot["agents"]["orchestrator"]
        gemini = snapshot["agents"]["gemini_consultant"]

        self.assertEqual(alpha["doing"], "Run pytest")
        self.assertEqual(alpha["typing_visible"], "pytest -q")
        self.assertEqual(alpha["typing_source"], "uia_edit_value")
        self.assertEqual(orch["thinking_summary"], "Reviewing live telemetry design")
        self.assertEqual(orch["thinking_source"], "public_orchestrator_feed")
        self.assertEqual(gemini["doing"], "Review consultant protocol")
        self.assertEqual(gemini["typing_source"], "not_observable")
        self.assertTrue(self.telemetry.OUT_FILE.exists())

    def test_stale_manual_report_is_ignored(self):
        base = {"alpha": self.telemetry._build_base_entry("alpha", "worker")}
        self.telemetry.MANUAL_FILE.write_text(json.dumps({
            "agents": {
                "alpha": {
                    "agent_id": "alpha",
                    "thinking_summary": "old thought",
                    "updated_at": "2020-01-01T00:00:00+00:00",
                    "stale_after_s": 5,
                    "source": "self_report",
                }
            }
        }), encoding="utf-8")

        merged = self.telemetry._merge_manual(base)
        self.assertEqual(merged["alpha"]["thinking_summary"], "unknown")
        self.assertEqual(merged["alpha"]["thinking_source"], "explicit_only")

    def test_publish_manual_updates_manual_store(self):
        result = self.telemetry.publish_manual(
            agent_id="beta",
            doing="Review failing tests",
            thinking_summary="Comparing failure signatures",
            ttl_s=12,
        )

        self.assertEqual(result["agent_id"], "beta")
        store = self.telemetry._load_manual_store()
        self.assertIn("beta", store["agents"])
        self.assertEqual(store["agents"]["beta"]["doing"], "Review failing tests")
        self.assertEqual(store["agents"]["beta"]["thinking_summary"], "Comparing failure signatures")


if __name__ == "__main__":
    unittest.main()
