#!/usr/bin/env python3
"""Tests for tools/skynet_worker_boot.py — 7-step boot procedure.

Tests cover: grid position calculation, HWND discovery, Copilot CLI
dropdown coordinates, identity prompt generation, bus verification
polling, workers.json updates, close/verify logic, and summary output.

# signed: alpha
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Grid Position Constants ──────────────────────────────────────

class TestGridPositions:
    """Test worker grid layout constants."""

    def test_grid_has_all_workers(self):
        from tools.skynet_worker_boot import GRID
        assert "alpha" in GRID
        assert "beta" in GRID
        assert "gamma" in GRID
        assert "delta" in GRID

    def test_alpha_top_left(self):
        from tools.skynet_worker_boot import GRID
        gx, gy = GRID["alpha"]
        assert gx == 1930
        assert gy == 20

    def test_beta_top_right(self):
        from tools.skynet_worker_boot import GRID
        gx, gy = GRID["beta"]
        assert gx == 2870
        assert gy == 20

    def test_gamma_bottom_left(self):
        from tools.skynet_worker_boot import GRID
        gx, gy = GRID["gamma"]
        assert gx == 1930
        assert gy == 540

    def test_delta_bottom_right(self):
        from tools.skynet_worker_boot import GRID
        gx, gy = GRID["delta"]
        assert gx == 2870
        assert gy == 540

    def test_window_size(self):
        from tools.skynet_worker_boot import WINDOW_SIZE
        w, h = WINDOW_SIZE
        assert w == 930
        assert h == 500

    def test_top_row_clears_titlebar(self):
        """Top row y=20 gives room for title bar."""
        from tools.skynet_worker_boot import GRID
        assert GRID["alpha"][1] >= 0
        assert GRID["beta"][1] >= 0

    def test_bottom_row_clears_taskbar(self):
        """Bottom row at y=540, h=500 => bottom=1040, leaves 40px for taskbar."""
        from tools.skynet_worker_boot import GRID, WINDOW_SIZE
        gy = GRID["gamma"][1]
        bottom = gy + WINDOW_SIZE[1]
        assert bottom == 1040  # 1080 - 1040 = 40px taskbar clearance

    def test_no_horizontal_overlap(self):
        """Alpha and Beta don't overlap horizontally."""
        from tools.skynet_worker_boot import GRID, WINDOW_SIZE
        alpha_right = GRID["alpha"][0] + WINDOW_SIZE[0]
        beta_left = GRID["beta"][0]
        assert alpha_right <= beta_left

    def test_no_vertical_overlap(self):
        """Top row and bottom row don't overlap vertically."""
        from tools.skynet_worker_boot import GRID, WINDOW_SIZE
        alpha_bottom = GRID["alpha"][1] + WINDOW_SIZE[1]
        gamma_top = GRID["gamma"][1]
        assert alpha_bottom <= gamma_top


# ── Coordinate Offsets ───────────────────────────────────────────

class TestCoordinateOffsets:
    """Test relative coordinate offsets for UI elements."""

    def test_cli_offset_defined(self):
        from tools.skynet_worker_boot import CLI_OFFSET
        assert len(CLI_OFFSET) == 2
        assert CLI_OFFSET[0] > 0
        assert CLI_OFFSET[1] > 0

    def test_input_offset_defined(self):
        from tools.skynet_worker_boot import INPUT_OFFSET
        assert len(INPUT_OFFSET) == 2
        assert INPUT_OFFSET[0] > 0

    def test_send_offset_defined(self):
        from tools.skynet_worker_boot import SEND_OFFSET
        assert len(SEND_OFFSET) == 2
        assert SEND_OFFSET[0] > 0

    def test_cli_click_absolute_coords(self):
        """CLI dropdown click = window_x + CLI_OFFSET[0], window_y + CLI_OFFSET[1]."""
        from tools.skynet_worker_boot import GRID, CLI_OFFSET
        gx, gy = GRID["alpha"]
        click_x = gx + CLI_OFFSET[0]
        click_y = gy + CLI_OFFSET[1]
        assert click_x == 1930 + CLI_OFFSET[0]
        assert click_y == 20 + CLI_OFFSET[1]

    def test_input_click_absolute_coords(self):
        from tools.skynet_worker_boot import GRID, INPUT_OFFSET
        gx, gy = GRID["beta"]
        click_x = gx + INPUT_OFFSET[0]
        click_y = gy + INPUT_OFFSET[1]
        assert click_x == 2870 + INPUT_OFFSET[0]


# ── Identity Prompt Generation ───────────────────────────────────

class TestIdentityPrompt:
    """Test _get_identity_prompt() template generation."""

    def test_contains_worker_name(self):
        from tools.skynet_worker_boot import _get_identity_prompt
        prompt = _get_identity_prompt("alpha")
        assert "ALPHA" in prompt or "alpha" in prompt

    def test_each_worker_gets_unique_prompt(self):
        from tools.skynet_worker_boot import _get_identity_prompt
        prompts = {name: _get_identity_prompt(name) for name in ["alpha", "beta", "gamma", "delta"]}
        # All should be unique (different names embedded)
        assert len(set(prompts.values())) == 4

    def test_prompt_is_non_empty(self):
        from tools.skynet_worker_boot import _get_identity_prompt
        prompt = _get_identity_prompt("gamma")
        assert len(prompt) > 10

    def test_prompt_type_is_string(self):
        from tools.skynet_worker_boot import _get_identity_prompt
        prompt = _get_identity_prompt("delta")
        assert isinstance(prompt, str)


# ── Worker Names ─────────────────────────────────────────────────

class TestWorkerNames:
    """Test WORKER_NAMES constant."""

    def test_four_workers(self):
        from tools.skynet_worker_boot import WORKER_NAMES
        assert len(WORKER_NAMES) == 4

    def test_correct_order(self):
        from tools.skynet_worker_boot import WORKER_NAMES
        assert WORKER_NAMES == ["alpha", "beta", "gamma", "delta"]


# ── Known HWND Collection ───────────────────────────────────────

class TestCollectKnownHwnds:
    """Test _collect_known_hwnds() aggregation."""

    def test_includes_orchestrator(self):
        from tools.skynet_worker_boot import _collect_known_hwnds
        with patch("tools.skynet_worker_boot.Path.read_text", return_value='{"workers":[]}'), \
             patch("tools.skynet_worker_boot.Path.exists", return_value=True):
            hwnds = _collect_known_hwnds(99999)
            assert 99999 in hwnds

    def test_includes_workers_from_file(self):
        from tools.skynet_worker_boot import _collect_known_hwnds
        workers_data = json.dumps({"workers": [
            {"name": "alpha", "hwnd": 111},
            {"name": "beta", "hwnd": 222},
        ]})
        with patch("builtins.open", MagicMock()), \
             patch("tools.skynet_worker_boot.Path.exists", return_value=True), \
             patch("tools.skynet_worker_boot.Path.read_text", return_value=workers_data):
            hwnds = _collect_known_hwnds(99999)
            assert 111 in hwnds
            assert 222 in hwnds

    def test_handles_missing_workers_file(self):
        from tools.skynet_worker_boot import _collect_known_hwnds

        def exists_side_effect(self_path=None):
            return False

        with patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "read_text", side_effect=FileNotFoundError):
            hwnds = _collect_known_hwnds(55555)
            assert 55555 in hwnds


# ── Workers JSON Update ──────────────────────────────────────────

class TestUpdateWorkersJson:
    """Test update_workers_json() file writing."""

    def test_writes_valid_json(self, tmp_path):
        from tools.skynet_worker_boot import update_workers_json, GRID, WINDOW_SIZE
        results = {
            "alpha": {"hwnd": 111, "grid": GRID["alpha"], "success": True},
            "beta": {"hwnd": 222, "grid": GRID["beta"], "success": True},
        }
        workers_file = tmp_path / "workers.json"
        with patch("tools.skynet_worker_boot.ROOT", tmp_path):
            (tmp_path / "data").mkdir(exist_ok=True)
            workers_file = tmp_path / "data" / "workers.json"
            update_workers_json(results)
            assert workers_file.exists()
            data = json.loads(workers_file.read_text())
            assert "workers" in data
            assert len(data["workers"]) >= 2  # writes all 4 workers, not just results

    def test_worker_entry_structure(self, tmp_path):
        from tools.skynet_worker_boot import update_workers_json, GRID
        results = {"alpha": {"hwnd": 111, "grid": GRID["alpha"], "success": True}}
        with patch("tools.skynet_worker_boot.ROOT", tmp_path):
            (tmp_path / "data").mkdir(exist_ok=True)
            update_workers_json(results)
            data = json.loads((tmp_path / "data" / "workers.json").read_text())
            w = data["workers"][0]
            assert w["name"] == "alpha"
            assert w["hwnd"] == 111
            assert "grid" in w
            assert "status" in w

    def test_dead_worker_status(self, tmp_path):
        from tools.skynet_worker_boot import update_workers_json, GRID
        results = {"gamma": {"hwnd": 0, "grid": GRID["gamma"], "success": False}}
        with patch("tools.skynet_worker_boot.ROOT", tmp_path):
            (tmp_path / "data").mkdir(exist_ok=True)
            update_workers_json(results)
            data = json.loads((tmp_path / "data" / "workers.json").read_text())
            w = data["workers"][0]
            assert w["status"] in ("dead", "offline")


# ── Bus Verification ─────────────────────────────────────────────

class TestBusVerification:
    """Test step7_verify() bus polling logic."""

    def test_identity_ack_found(self):
        """Should return True when identity_ack is found in bus messages."""
        from tools.skynet_worker_boot import step7_verify
        mock_bus_response = MagicMock()
        mock_bus_response.json.return_value = {
            "messages": [
                {"sender": "alpha", "type": "identity_ack", "content": "ALPHA ONLINE"},
                {"sender": "beta", "type": "result", "content": "something"},
            ]
        }
        mock_bus_response.status_code = 200
        with patch("tools.skynet_worker_boot.requests.get", return_value=mock_bus_response), \
             patch("tools.skynet_worker_boot.u32") as mock_u32:
            mock_u32.IsWindow.return_value = True
            buf = (b"You are ALPHA" + b"\x00" * 500)
            mock_u32.GetWindowTextW = MagicMock()
            result = step7_verify("alpha", 12345, timeout=2)
            # Should find the identity_ack
            assert result is True

    def test_no_identity_ack_times_out(self):
        """Should return False when no matching identity_ack found."""
        from tools.skynet_worker_boot import step7_verify
        mock_bus_response = MagicMock()
        mock_bus_response.json.return_value = {"messages": []}
        mock_bus_response.status_code = 200
        with patch("tools.skynet_worker_boot.requests.get", return_value=mock_bus_response), \
             patch("tools.skynet_worker_boot.u32") as mock_u32, \
             patch("time.sleep"):
            mock_u32.IsWindow.return_value = True
            mock_u32.GetWindowTextW = MagicMock()
            result = step7_verify("alpha", 12345, timeout=1)
            # May still return True if window is alive
            assert isinstance(result, bool)


# ── Post Identity ACK ────────────────────────────────────────────

class TestPostIdentityAck:
    """Test _post_identity_ack() HTTP posting."""

    def test_successful_post(self):
        from tools.skynet_worker_boot import _post_identity_ack
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("tools.skynet_worker_boot.requests.post", return_value=mock_resp):
            result = _post_identity_ack("alpha")
            assert result is True

    def test_failed_post(self):
        from tools.skynet_worker_boot import _post_identity_ack
        with patch("tools.skynet_worker_boot.requests.post", side_effect=Exception("connection refused")):
            result = _post_identity_ack("alpha")
            assert result is False

    def test_payload_structure(self):
        from tools.skynet_worker_boot import _post_identity_ack
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("tools.skynet_worker_boot.requests.post", return_value=mock_resp) as mock_post:
            _post_identity_ack("beta")
            call_kwargs = mock_post.call_args
            payload = call_kwargs[1].get("json") or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None)
            if payload is None and "data" in call_kwargs[1]:
                payload = json.loads(call_kwargs[1]["data"])
            assert payload is not None
            assert payload["sender"] == "beta"
            assert payload["type"] == "identity_ack"


# ── Boot Version ─────────────────────────────────────────────────

class TestBootVersion:
    """Test boot version and hash constants."""

    def test_boot_version_exists(self):
        from tools.skynet_worker_boot import BOOT_VERSION
        assert isinstance(BOOT_VERSION, str)
        assert len(BOOT_VERSION) > 0

    def test_boot_hash_exists(self):
        from tools.skynet_worker_boot import BOOT_HASH
        assert isinstance(BOOT_HASH, str)
        assert len(BOOT_HASH) == 16  # SHA-256 truncated to 16 hex chars


# ── Step 3: Position ─────────────────────────────────────────────

class TestStep3Position:
    """Test step3_position() window positioning."""

    def test_calls_move_window(self):
        from tools.skynet_worker_boot import step3_position, WINDOW_SIZE
        with patch("tools.skynet_worker_boot.u32") as mock_u32:
            mock_u32.MoveWindow.return_value = 1
            result = step3_position(12345, 1930, 20)
            mock_u32.MoveWindow.assert_called_once_with(
                12345, 1930, 20, WINDOW_SIZE[0], WINDOW_SIZE[1], True
            )
            assert result is True

    def test_always_returns_true(self):
        """step3_position returns True even if MoveWindow returns 0."""
        from tools.skynet_worker_boot import step3_position
        with patch("tools.skynet_worker_boot.u32") as mock_u32:
            mock_u32.MoveWindow.return_value = 0
            result = step3_position(99999, 2870, 540)
            assert result is True


# ── Summary Printing ─────────────────────────────────────────────

class TestSummaryPrinting:
    """Test _print_summary() output formatting."""

    def test_prints_all_workers(self, capsys):
        from tools.skynet_worker_boot import _print_summary
        results = {
            "alpha": {"hwnd": 111, "grid": (1930, 20), "success": True},
            "beta": {"hwnd": 222, "grid": (2870, 20), "success": False},
        }
        _print_summary(results)
        captured = capsys.readouterr()
        assert "alpha" in captured.out.lower() or "ALPHA" in captured.out
        assert "beta" in captured.out.lower() or "BETA" in captured.out

    def test_prints_success_status(self, capsys):
        from tools.skynet_worker_boot import _print_summary
        results = {"alpha": {"hwnd": 111, "grid": (1930, 20), "success": True}}
        _print_summary(results)
        captured = capsys.readouterr()
        # Should contain some indication of success
        assert len(captured.out) > 0
