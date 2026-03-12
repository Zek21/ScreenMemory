import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class TestConsultantHwndProbe(unittest.TestCase):
    @patch("tools.skynet_consultant_hwnd._enum_vscode_hwnds", return_value=[424242])
    @patch("tools.skynet_consultant_hwnd.validate_hwnd")
    @patch("tools.skynet_consultant_hwnd._get_candidate_window_text")
    @patch("tools.skynet_consultant_hwnd._get_window_scan_flags")
    @patch("tools.skynet_consultant_hwnd._reserved_skynet_hwnds", return_value=set())
    def test_probe_accepts_marker_backed_codex_window(
        self, _mock_reserved, mock_scan, mock_text, mock_validate, _mock_enum
    ):
        import tools.skynet_consultant_hwnd as helper

        mock_validate.return_value = {
            "valid": True,
            "title": "Dedicated consultant - ScreenMemory - Visual Studio Code - Insiders",
            "checks": {"nonzero": True, "is_window": True, "is_vscode_process": True, "has_vscode_title": True},
        }
        mock_text.return_value = (
            "cc-start\n"
            "You are the Codex Consultant\n"
            "sender: consultant\n"
            "signed:consultant"
        )
        mock_scan.return_value = {
            "agent": "Delegate Session - Copilot CLI",
            "model": "Pick Model, Claude Opus 4.6 (fast mode)",
            "agent_ok": True,
            "model_ok": True,
        }

        result = helper.discover_consultant_hwnd("consultant")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["hwnd"], 424242)
        self.assertIn("content:cc-start", result["best_candidate"]["markers"])

    @patch("tools.skynet_consultant_hwnd._enum_vscode_hwnds", return_value=[525252])
    @patch("tools.skynet_consultant_hwnd.validate_hwnd")
    @patch("tools.skynet_consultant_hwnd._get_candidate_window_text", return_value="")
    @patch("tools.skynet_consultant_hwnd._get_window_scan_flags")
    @patch("tools.skynet_consultant_hwnd._reserved_skynet_hwnds", return_value=set())
    def test_probe_rejects_title_only_gc_start_reference(
        self, _mock_reserved, mock_scan, _mock_text, mock_validate, _mock_enum
    ):
        import tools.skynet_consultant_hwnd as helper

        mock_validate.return_value = {
            "valid": True,
            "title": "Understanding gc-start command usage - ScreenMemory - Visual Studio Code - Insiders",
            "checks": {"nonzero": True, "is_window": True, "is_vscode_process": True, "has_vscode_title": True},
        }
        mock_scan.return_value = {
            "agent": "Delegate Session - Copilot CLI",
            "model": "Pick Model, Claude Opus 4.6 (fast mode)",
            "agent_ok": True,
            "model_ok": True,
        }

        result = helper.discover_consultant_hwnd("gemini_consultant")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["hwnd"], 0)

    @patch("tools.skynet_consultant_hwnd._enum_vscode_hwnds", return_value=[8132014])
    @patch("tools.skynet_consultant_hwnd.validate_hwnd")
    @patch("tools.skynet_consultant_hwnd._get_candidate_window_text", return_value="")
    @patch("tools.skynet_consultant_hwnd._get_window_scan_flags", return_value={
        "agent": "Delegate Session - Local",
        "model": "Pick Model, Gemini 3.1 Pro (Preview)",
        "agent_ok": False,
        "model_ok": False,
    })
    @patch("tools.skynet_consultant_hwnd._reserved_skynet_hwnds", return_value={8132014: "orchestrator"})
    @patch("tools.skynet_consultant_hwnd._best_band_identity", return_value={
        "strong": True,
        "score": 9,
        "slot": "middle",
        "markers": ["pane_model:pick model, gemini 3.1 pro (preview)", "pane_agent:autopilot (preview)"],
        "reject_markers": [],
        "model": "Pick Model, Gemini 3.1 Pro (Preview)",
        "session_target": "Set Session Target - Local",
        "permissions": "Set Permissions - Autopilot (Preview)",
        "names_excerpt": "Pick Model, Gemini 3.1 Pro (Preview)\nAutopilot (Preview)",
    })
    def test_probe_reports_shared_gemini_surface_in_reserved_window(
        self, _mock_bands, _mock_reserved, _mock_scan, _mock_text, mock_validate, _mock_enum
    ):
        import tools.skynet_consultant_hwnd as helper

        mock_validate.return_value = {
            "valid": True,
            "title": "Shared ScreenMemory - Visual Studio Code - Insiders",
            "checks": {"nonzero": True, "is_window": True, "is_vscode_process": True, "has_vscode_title": True},
        }

        result = helper.discover_consultant_hwnd("gemini_consultant")

        self.assertFalse(result["accepted"])
        self.assertTrue(result["visible_surface"])
        self.assertEqual(result["shared_parent_hwnd"], 8132014)
        self.assertEqual(result["pane_slot"], "middle")
        self.assertEqual(result["pane_model"], "Pick Model, Gemini 3.1 Pro (Preview)")

    @patch("tools.skynet_consultant_hwnd._enum_vscode_hwnds", return_value=[700700])
    @patch("tools.skynet_consultant_hwnd.validate_hwnd")
    @patch("tools.skynet_consultant_hwnd._get_candidate_window_text", return_value="")
    @patch("tools.skynet_consultant_hwnd._get_window_scan_flags", return_value={
        "agent": "",
        "model": "",
        "agent_ok": False,
        "model_ok": False,
    })
    @patch("tools.skynet_consultant_hwnd._reserved_skynet_hwnds", return_value=set())
    @patch("tools.skynet_consultant_hwnd._best_band_identity", return_value={
        "strong": True,
        "score": 8,
        "slot": "right",
        "markers": ["pane_header:codex", "pane_action:run cc-start bootstrap"],
        "reject_markers": [],
        "model": "",
        "session_target": "",
        "permissions": "",
        "names_excerpt": "CODEX\nRun CC-Start bootstrap",
    })
    def test_probe_accepts_cdx_pane_signal_without_content_marker(
        self, _mock_bands, _mock_reserved, _mock_scan, _mock_text, mock_validate, _mock_enum
    ):
        import tools.skynet_consultant_hwnd as helper

        mock_validate.return_value = {
            "valid": True,
            "title": "Shared ScreenMemory - Visual Studio Code - Insiders",
            "checks": {"nonzero": True, "is_window": True, "is_vscode_process": True, "has_vscode_title": True},
        }

        result = helper.discover_consultant_hwnd("consultant")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["hwnd"], 700700)
        self.assertEqual(result["pane_slot"], "right")

    @patch("tools.skynet_consultant_hwnd._scan_window_bands", return_value=[
        {
            "slot": "middle",
            "haystack": "\n".join([
                "Pick Model, Gemini 3.1 Pro (Preview)",
                "Set Session Target - Local",
                "Set Permissions - Autopilot (Preview)",
            ]).lower(),
            "model": "Pick Model, Gemini 3.1 Pro (Preview)",
            "session_target": "Set Session Target - Local",
            "permissions": "Set Permissions - Autopilot (Preview)",
            "names": [
                "Pick Model, Gemini 3.1 Pro (Preview)",
                "Set Session Target - Local",
                "Set Permissions - Autopilot (Preview)",
            ],
        },
        {
            "slot": "right",
            "haystack": "\n".join([
                "Gemini Consultant",
                "GC-Start",
                "Autopilot (Preview)",
            ]).lower(),
            "model": "",
            "session_target": "",
            "permissions": "",
            "names": [
                "Gemini Consultant",
                "GC-Start",
                "Autopilot (Preview)",
            ],
        },
    ])
    def test_best_band_identity_prefers_real_gemini_controls_over_transcript_mentions(self, _mock_scan):
        import tools.skynet_consultant_hwnd as helper

        result = helper._best_band_identity(8132014, "gemini_consultant")

        self.assertEqual(result["slot"], "middle")
        self.assertEqual(result["model"], "Pick Model, Gemini 3.1 Pro (Preview)")
        self.assertIn("pane_signal:model_control", result["markers"])
        self.assertIn("pane_signal:session_target", result["markers"])
        self.assertIn("pane_signal:permissions", result["markers"])

    @patch("tools.skynet_consultant_hwnd._scan_window_bands", return_value=[
        {
            "slot": "left",
            "haystack": "\n".join([
                "Pick Model, Claude Opus 4.6 (fast mode)",
                "Set Permissions - Bypass Approvals",
            ]).lower(),
            "model": "Pick Model, Claude Opus 4.6 (fast mode)",
            "session_target": "",
            "permissions": "Set Permissions - Bypass Approvals",
            "names": [
                "Pick Model, Claude Opus 4.6 (fast mode)",
                "Set Permissions - Bypass Approvals",
            ],
        }
    ])
    def test_best_band_identity_ignores_non_matching_worker_controls(self, _mock_scan):
        import tools.skynet_consultant_hwnd as helper

        result = helper._best_band_identity(334246, "gemini_consultant")

        self.assertEqual(result["score"], 0)
        self.assertNotIn("pane_signal:model_control", result["markers"])
        self.assertNotIn("pane_signal:permissions", result["markers"])


class TestConsultantHwndOpen(unittest.TestCase):
    def test_open_records_candidate_without_binding(self):
        import tools.skynet_consultant_hwnd as helper

        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_file = Path(tmpdir) / "consultant_window_candidates.json"
            fake_script = Path(tmpdir) / "new_chat.ps1"
            fake_script.write_text("# noop", encoding="utf-8")
            run_result = SimpleNamespace(
                returncode=0,
                stdout="OK HWND=22222 pos=1930,20 size=930x500",
                stderr="",
            )

            with patch.object(helper, "CANDIDATE_FILE", candidate_file), \
                 patch.object(helper, "NEW_CHAT_SCRIPT", fake_script), \
                 patch.object(helper.subprocess, "run", return_value=run_result) as run_mock, \
                 patch.object(helper, "_enum_vscode_hwnds", return_value=[]), \
                 patch.object(helper, "_get_window_title", return_value="Consultant Candidate - ScreenMemory - Visual Studio Code - Insiders"), \
                 patch.object(helper, "_get_window_scan_flags", return_value={
                     "agent": "Delegate Session - Copilot CLI",
                     "model": "Pick Model, Claude Opus 4.6 (fast mode)",
                     "agent_ok": True,
                     "model_ok": True,
                 }), \
                 patch.object(helper, "discover_consultant_hwnd", return_value={
                     "accepted": False,
                     "hwnd": 0,
                     "best_candidate": None,
                     "candidates": [],
                 }):
                result = helper.open_candidate_window("consultant")

            self.assertTrue(result["success"])
            launch_args = run_mock.call_args.args[0]
            self.assertIn("-Layout", launch_args)
            self.assertIn("consultant", launch_args)
            registry = json.loads(candidate_file.read_text(encoding="utf-8"))
            self.assertEqual(registry["candidates"][0]["hwnd"], 22222)
            self.assertEqual(registry["candidates"][0]["binding_status"], "candidate_only")

    def test_open_rejects_blocked_existing_empty_window(self):
        import tools.skynet_consultant_hwnd as helper

        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_file = Path(tmpdir) / "consultant_window_candidates.json"
            fake_script = Path(tmpdir) / "new_chat.ps1"
            fake_script.write_text("# noop", encoding="utf-8")
            run_result = SimpleNamespace(
                returncode=0,
                stdout="BLOCKED: Chat window HWND=22222 has no first prompt yet. Use it before opening another.",
                stderr="",
            )

            with patch.object(helper, "CANDIDATE_FILE", candidate_file), \
                 patch.object(helper, "NEW_CHAT_SCRIPT", fake_script), \
                 patch.object(helper.subprocess, "run", return_value=run_result), \
                 patch.object(helper, "_enum_vscode_hwnds", return_value=[22222]), \
                 patch.object(helper, "discover_consultant_hwnd", return_value={
                     "accepted": False,
                     "hwnd": 0,
                     "best_candidate": None,
                     "candidates": [],
                 }):
                result = helper.open_candidate_window("consultant")

            self.assertFalse(result["success"])
            self.assertTrue(result["blocked"])
            self.assertFalse(candidate_file.exists())


class TestConsultantBridgeDiscovery(unittest.TestCase):
    def test_build_live_state_uses_probe_discovery_when_state_hwnd_missing(self):
        import tools.skynet_consultant_bridge as bridge

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "consultant_state.json"
            task_file = Path(tmpdir) / "consultant_task_state.json"
            registry_file = Path(tmpdir) / "consultant_registry.json"
            state_file.write_text(json.dumps({}), encoding="utf-8")

            old_state = bridge.STATE_FILE
            old_task = bridge.TASK_FILE
            old_registry = bridge.REGISTRY_FILE
            bridge.STATE_FILE = state_file
            bridge.TASK_FILE = task_file
            bridge.REGISTRY_FILE = registry_file
            try:
                with patch.object(bridge, "_load_worker_snapshot", return_value={
                    "source": "realtime.json",
                    "summary": {"total": 4, "available": 4, "busy": 0, "offline": 0},
                    "workers": {},
                    "available_workers": ["alpha", "beta", "gamma", "delta"],
                    "busy_workers": [],
                    "offline_workers": [],
                }), \
                     patch.object(bridge, "_load_score_summary", return_value={"total": 0.0}), \
                     patch.object(bridge, "_latest_consultant_message", return_value=None), \
                     patch.object(bridge, "_window_alive", return_value=True), \
                     patch.object(bridge, "_reserved_hwnds", return_value={}), \
                     patch("tools.skynet_consultant_hwnd.discover_consultant_hwnd", return_value={
                         "accepted": True,
                         "hwnd": 565656,
                         "best_candidate": {
                             "markers": ["content:cc-start", "content:sender: consultant"],
                             "score": 11,
                         },
                     }):
                    state = bridge.build_live_state(api_port=8422)
            finally:
                bridge.STATE_FILE = old_state
                bridge.TASK_FILE = old_task
                bridge.REGISTRY_FILE = old_registry

        self.assertEqual(state["hwnd"], 565656)
        self.assertEqual(state["prompt_transport"], "ghost_type")
        self.assertEqual(state["hwnd_source"], "discovery")
        self.assertIn("content:cc-start", state["hwnd_markers"])

    def test_build_view_exposes_shared_surface_when_reserved_window_contains_consultant(self):
        import tools.skynet_consultant_bridge as bridge

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "consultant_state.json"
            task_file = Path(tmpdir) / "consultant_task_state.json"
            registry_file = Path(tmpdir) / "consultant_registry.json"
            state_file.write_text(json.dumps({}), encoding="utf-8")

            old_state = bridge.STATE_FILE
            old_task = bridge.TASK_FILE
            old_registry = bridge.REGISTRY_FILE
            bridge.STATE_FILE = state_file
            bridge.TASK_FILE = task_file
            bridge.REGISTRY_FILE = registry_file
            try:
                with patch.object(bridge, "_window_alive", return_value=False), \
                     patch.object(bridge, "_reserved_hwnds", return_value={8132014: "orchestrator"}), \
                     patch("tools.skynet_consultant_hwnd.discover_consultant_hwnd", return_value={
                         "accepted": False,
                         "hwnd": 0,
                         "visible_surface": True,
                         "shared_parent_hwnd": 8132014,
                         "pane_slot": "right",
                         "pane_model": "",
                         "pane_session_target": "",
                         "pane_permissions": "",
                         "pane_markers": ["pane_header:codex", "pane_action:run cc-start bootstrap"],
                         "best_candidate": {
                             "markers": ["right:pane_header:codex", "right:pane_action:run cc-start bootstrap"],
                             "score": 8,
                         },
                     }):
                    view = bridge.get_consultant_view()
            finally:
                bridge.STATE_FILE = old_state
                bridge.TASK_FILE = old_task
                bridge.REGISTRY_FILE = old_registry

        self.assertEqual(view["hwnd"], 0)
        self.assertTrue(view["visible_surface"])
        self.assertEqual(view["shared_parent_hwnd"], 8132014)
        self.assertEqual(view["pane_slot"], "right")

    def test_refresh_registry_persists_shared_surface_metadata(self):
        import tools.skynet_consultant_bridge as bridge

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "consultant_state.json"
            registry_file = Path(tmpdir) / "consultant_registry.json"
            state_file.write_text(json.dumps({}), encoding="utf-8")

            old_state = bridge.STATE_FILE
            old_registry = bridge.REGISTRY_FILE
            bridge.STATE_FILE = state_file
            bridge.REGISTRY_FILE = registry_file
            try:
                bridge._refresh_consultant_registry({
                    "display_name": "Codex Consultant",
                    "hwnd": 0,
                    "visible_surface": True,
                    "shared_parent_hwnd": 8132014,
                    "pane_slot": "right",
                    "pane_model": "",
                    "pane_session_target": "",
                    "pane_permissions": "",
                    "pane_markers": ["pane_header:codex", "pane_action:run cc-start bootstrap"],
                    "model": "GPT-5 Codex",
                    "api_port": 8422,
                    "status": "LIVE",
                    "prompt_transport": "bridge_queue",
                    "requires_hwnd": False,
                    "routable": True,
                })
                registry = json.loads(registry_file.read_text(encoding="utf-8"))
            finally:
                bridge.STATE_FILE = old_state
                bridge.REGISTRY_FILE = old_registry

        entry = registry["consultants"][0]
        self.assertTrue(entry["visible_surface"])
        self.assertEqual(entry["shared_parent_hwnd"], 8132014)
        self.assertEqual(entry["pane_slot"], "right")
        self.assertEqual(entry["pane_markers"], ["pane_header:codex", "pane_action:run cc-start bootstrap"])

    def test_run_daemon_once_does_not_write_offline_snapshot(self):
        import tools.skynet_consultant_bridge as bridge

        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "bridge_refresh.pid"
            with patch.object(bridge, "_existing_daemon_alive", return_value=False), \
                 patch.object(bridge, "build_live_state", return_value={"status": "LIVE", "transport": "cc-start-bridge"}), \
                 patch.object(bridge, "_atomic_write"), \
                 patch.object(bridge, "_refresh_consultant_registry"), \
                 patch.object(bridge, "_write_offline_snapshot") as offline_mock:
                result = bridge.run_daemon(pid_file=pid_file, once=True, announce=False)

        self.assertEqual(result, 0)
        self.assertFalse(offline_mock.called)


if __name__ == "__main__":
    unittest.main()
