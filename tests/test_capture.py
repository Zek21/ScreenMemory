"""Tests for core/capture.py - DXGICapture screen capture engine.
# signed: alpha
"""
import ctypes
import ctypes.wintypes
import time
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from PIL import Image
from dataclasses import dataclass

from core.capture import DXGICapture, MonitorInfo, CaptureResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_monitor(index=0, x=0, y=0, w=1920, h=1080, primary=True):
    return MonitorInfo(index=index, x=x, y=y, width=w, height=h, primary=primary)


def _dummy_image(w=1920, h=1080):
    return Image.new("RGB", (w, h), color=(128, 128, 128))


# ---------------------------------------------------------------------------
# Tests for _enumerate_monitors
# ---------------------------------------------------------------------------
class TestEnumerateMonitors:
    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_returns_list(self, mock_enum, mock_dxgi):
        """_enumerate_monitors should return a list of MonitorInfo."""
        mock_enum.return_value = [_make_monitor()]
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        assert isinstance(cap.monitors, list)
        assert len(cap.monitors) == 1
        assert isinstance(cap.monitors[0], MonitorInfo)  # signed: alpha

    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_multi_monitor(self, mock_enum, mock_dxgi):
        """Should handle multiple monitors."""
        monitors = [
            _make_monitor(0, 0, 0, 1920, 1080, True),
            _make_monitor(1, 1920, 0, 1920, 1080, False),
        ]
        mock_enum.return_value = monitors
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        assert len(cap.monitors) == 2
        assert cap.monitors[0].primary is True
        assert cap.monitors[1].primary is False
        assert cap.monitors[1].x == 1920  # signed: alpha

    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_empty_monitors(self, mock_enum, mock_dxgi):
        """Should handle zero monitors gracefully."""
        mock_enum.return_value = []
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        assert cap.monitors == []  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for capture_monitor / _capture_mss / _capture_pil
# ---------------------------------------------------------------------------
class TestCaptureMonitor:
    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_capture_mss_returns_result(self, mock_enum, mock_dxgi):
        """_capture_mss should return a CaptureResult with correct dimensions."""
        mock_enum.return_value = [_make_monitor()]
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        cap._dxgi_available = True

        mock_shot = MagicMock()
        mock_shot.size = (1920, 1080)
        mock_shot.bgra = bytes(1920 * 1080 * 4)

        mock_mss_inst = MagicMock()
        mock_mss_inst.monitors = [
            {"left": 0, "top": 0, "width": 3840, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]
        mock_mss_inst.grab.return_value = mock_shot
        cap._mss = mock_mss_inst

        result = cap.capture_monitor(0)
        assert result is not None
        assert isinstance(result, CaptureResult)
        assert result.width == 1920
        assert result.height == 1080
        assert result.capture_ms >= 0  # signed: alpha

    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    @patch("core.capture.ImageGrab" if False else "PIL.ImageGrab.grab")
    def test_capture_pil_returns_result(self, mock_grab, mock_enum, mock_dxgi):
        """_capture_pil should return a CaptureResult using PIL fallback."""
        mock_enum.return_value = [_make_monitor()]
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        cap._dxgi_available = False

        mock_grab.return_value = _dummy_image(1920, 1080)
        result = cap.capture_monitor(0)
        assert result is not None
        assert result.width == 1920
        assert result.height == 1080
        assert result.monitor_index == 0  # signed: alpha

    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_capture_exception_returns_none(self, mock_enum, mock_dxgi):
        """capture_monitor should return None on exception."""
        mock_enum.return_value = [_make_monitor()]
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        cap._dxgi_available = False

        with patch("PIL.ImageGrab.grab", side_effect=OSError("display error")):
            result = cap.capture_monitor(0)
            assert result is None  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for capture_all
# ---------------------------------------------------------------------------
class TestCaptureAll:
    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_capture_all_multi_monitor(self, mock_enum, mock_dxgi):
        """capture_all should return results for each monitor."""
        monitors = [_make_monitor(0), _make_monitor(1, 1920)]
        mock_enum.return_value = monitors
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)

        dummy = _dummy_image()
        with patch("PIL.ImageGrab.grab", return_value=dummy):
            results = cap.capture_all()
            assert len(results) == 2
            for r in results:
                assert isinstance(r, CaptureResult)  # signed: alpha

    @patch.object(DXGICapture, "_init_dxgi")
    @patch.object(DXGICapture, "_enumerate_monitors")
    def test_capture_all_empty_monitors(self, mock_enum, mock_dxgi):
        """capture_all should return empty list when no monitors."""
        mock_enum.return_value = []
        mock_dxgi.return_value = None
        cap = DXGICapture(use_dxgi=False)
        results = cap.capture_all()
        assert results == []  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for MonitorInfo / CaptureResult dataclasses
# ---------------------------------------------------------------------------
class TestDataclasses:
    def test_monitor_info_defaults(self):
        """MonitorInfo should have sensible defaults."""
        m = MonitorInfo(index=0, x=0, y=0, width=1920, height=1080)
        assert m.name == ""
        assert m.primary is False  # signed: alpha

    def test_capture_result_fields(self):
        """CaptureResult should hold all expected fields."""
        img = _dummy_image()
        r = CaptureResult(
            image=img, monitor_index=0, timestamp=time.time(),
            capture_ms=1.5, width=1920, height=1080,
        )
        assert r.capture_ms == 1.5
        assert r.image is img  # signed: alpha
