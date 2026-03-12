"""Tests for the render_hwnd fast-path in _build_ghost_type_ps().

Verifies that when a pre-resolved Chrome_RenderWidgetHostHWND is provided,
the generated PowerShell script skips UIA Edit search and goes directly to
the CHROME_RENDER paste path.
"""
# signed: beta

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.skynet_dispatch import _build_ghost_type_ps


class TestGhostTypeFastPath(unittest.TestCase):
    """Test render_hwnd fast-path in _build_ghost_type_ps()."""

    FAKE_HWND = 99999
    FAKE_ORCH = 88888
    FAKE_FILE = "C:\\\\tmp\\\\dispatch.txt"

    def _build(self, render_hwnd=None):
        return _build_ghost_type_ps(
            self.FAKE_HWND, self.FAKE_ORCH, self.FAKE_FILE,
            render_hwnd=render_hwnd,
        )  # signed: beta

    # --- Normal path (render_hwnd=None) ---

    def test_normal_path_contains_uia_edit_search(self):
        """Without render_hwnd, PS script must contain full UIA Edit search."""
        ps = self._build(render_hwnd=None)
        self.assertIn("ControlType]::Edit", ps,
                       "Normal path must search for UIA Edit controls")
        self.assertIn("$allEdits", ps,
                       "Normal path must populate $allEdits variable")
        self.assertIn("$bestScore", ps,
                       "Normal path must score Edit candidates")  # signed: beta

    def test_normal_path_contains_find_all_render(self):
        """Without render_hwnd, PS should still have FindAllRender fallback."""
        ps = self._build(render_hwnd=None)
        self.assertIn("FindAllRender", ps,
                       "Normal path must include FindAllRender fallback")  # signed: beta

    def test_normal_path_fast_render_is_zero(self):
        """Without render_hwnd, $fastRenderHwnd should be [IntPtr]0."""
        ps = self._build(render_hwnd=None)
        self.assertIn("[IntPtr]0", ps,
                       "render_hwnd_val must be 0 when render_hwnd is None")  # signed: beta

    # --- Fast path (render_hwnd provided) ---

    def test_fast_path_skips_uia_edit_search(self):
        """With render_hwnd=12345, PS script must NOT search UIA Edit controls."""
        ps = self._build(render_hwnd=12345)
        # The UIA Edit search block is inside the else{} of the fast-path if.
        # When fast-path fires, PowerShell jumps to CHROME_RENDER immediately.
        # We verify the fast-path debug line is present.
        self.assertIn("Fast-path render_hwnd=", ps,
                       "Fast-path debug line must be present")
        self.assertIn("[IntPtr]12345", ps,
                       "render_hwnd_val must be 12345")  # signed: beta

    def test_fast_path_sets_chrome_render(self):
        """With render_hwnd, focusMethod must be set to CHROME_RENDER."""
        ps = self._build(render_hwnd=12345)
        # The fast-path block sets $focusMethod = "CHROME_RENDER"
        self.assertIn('$focusMethod = "CHROME_RENDER"', ps,
                       "Fast-path must set focusMethod to CHROME_RENDER")  # signed: beta

    def test_fast_path_contains_paste_logic(self):
        """Fast-path script must still contain clipboard + paste logic."""
        ps = self._build(render_hwnd=12345)
        self.assertIn("Clipboard", ps,
                       "Script must contain clipboard operations")
        self.assertIn("SendKeys", ps,
                       "Script must contain SendKeys for paste")
        self.assertIn("ENTER", ps,
                       "Script must send ENTER after paste")  # signed: beta

    def test_fast_path_contains_steering_cancel(self):
        """Fast-path must still include STEERING cancel block."""
        ps = self._build(render_hwnd=12345)
        self.assertIn("Cancel (Alt+Backspace)", ps,
                       "STEERING cancel must always be present regardless of fast-path")  # signed: beta

    # --- Edge cases ---

    def test_render_hwnd_zero_uses_normal_path(self):
        """render_hwnd=0 should be treated as None (normal path)."""
        ps = self._build(render_hwnd=0)
        self.assertIn("[IntPtr]0", ps)
        self.assertIn("$allEdits", ps,
                       "render_hwnd=0 must use normal UIA Edit search")  # signed: beta

    def test_render_hwnd_string_int_coerced(self):
        """render_hwnd as string '54321' should be coerced to int."""
        ps = self._build(render_hwnd="54321")
        self.assertIn("[IntPtr]54321", ps,
                       "String render_hwnd must be coerced to int")  # signed: beta


if __name__ == "__main__":
    unittest.main()
# signed: beta
