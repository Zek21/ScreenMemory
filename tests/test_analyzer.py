"""Tests for core/analyzer.py - ScreenAnalyzer VLM-based analysis.
# signed: alpha
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image

from core.analyzer import ScreenAnalyzer, AnalysisResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dummy_image(w=800, h=600):
    return Image.new("RGB", (w, h), color=(200, 200, 200))


def _mock_ollama_tags(models=None):
    """Return a mock urllib response for /api/tags."""
    if models is None:
        models = [{"name": "minicpm-v:latest"}]
    data = json.dumps({"models": models}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_analyzer(available_models=None):
    """Create a ScreenAnalyzer with mocked Ollama check."""
    with patch("urllib.request.urlopen", return_value=_mock_ollama_tags(available_models)):
        return ScreenAnalyzer()


# ---------------------------------------------------------------------------
# Tests for model selection / fallback
# ---------------------------------------------------------------------------
class TestModelSelection:
    def test_primary_model_selected(self):
        """Should select primary model when available."""
        analyzer = _make_analyzer([{"name": "minicpm-v:latest"}])
        assert analyzer._get_model() == "minicpm-v"  # signed: alpha

    def test_fallback_model_selected(self):
        """Should fall back to llava when primary unavailable."""
        analyzer = _make_analyzer([{"name": "llava:7b"}])
        assert analyzer._get_model() == "llava:7b"  # signed: alpha

    def test_any_vision_model_selected(self):
        """Should find any known vision model as last resort."""
        analyzer = _make_analyzer([{"name": "moondream:latest"}])
        model = analyzer._get_model()
        assert model == "moondream"  # signed: alpha

    def test_no_model_returns_none(self):
        """Should return None when no vision model available."""
        analyzer = _make_analyzer([{"name": "llama3:latest"}])
        assert analyzer._get_model() is None  # signed: alpha

    def test_ollama_unreachable(self):
        """Should handle Ollama being unreachable gracefully."""
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            analyzer = ScreenAnalyzer()
        assert analyzer._get_model() is None
        assert analyzer.is_available is False  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for _parse_response / _extract_json_fields
# ---------------------------------------------------------------------------
class TestResponseParsing:
    def test_valid_json_response(self):
        """Should extract fields from valid JSON in response."""
        analyzer = _make_analyzer()
        response = json.dumps({
            "description": "User is editing Python code in VS Code",
            "active_app": "VS Code",
            "activity_type": "coding",
            "ocr_text": "def main():",
        })
        result = analyzer._parse_response(response, 150.0, "minicpm-v")
        assert result.description == "User is editing Python code in VS Code"
        assert result.active_app == "VS Code"
        assert result.activity_type == "coding"
        assert result.ocr_text == "def main():"
        assert result.analysis_ms == 150.0  # signed: alpha

    def test_malformed_json_falls_back(self):
        """Should handle malformed JSON by using raw text as description."""
        analyzer = _make_analyzer()
        response = '{"description": "broken json'
        result = analyzer._parse_response(response, 100.0, "minicpm-v")
        assert result.description == response.strip()[:500]
        assert result.confidence == 0.8  # signed: alpha

    def test_empty_response(self):
        """Should handle empty response string."""
        analyzer = _make_analyzer()
        result = analyzer._parse_response("", 50.0, "minicpm-v")
        assert result.description == ""
        assert result.confidence == 0.1  # signed: alpha

    def test_json_embedded_in_text(self):
        """Should extract JSON embedded in surrounding text."""
        analyzer = _make_analyzer()
        response = 'Here is the analysis:\n{"description": "Chrome browser", "active_app": "Chrome", "activity_type": "browsing", "ocr_text": ""}\nDone.'
        result = analyzer._parse_response(response, 200.0, "llava:7b")
        assert result.active_app == "Chrome"
        assert result.activity_type == "browsing"  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for analyze() main function
# ---------------------------------------------------------------------------
class TestAnalyze:
    def test_analyze_with_vlm(self):
        """analyze() should call Ollama and return AnalysisResult."""
        analyzer = _make_analyzer([{"name": "minicpm-v:latest"}])

        vlm_response = json.dumps({
            "description": "VS Code with Python file open",
            "active_app": "VS Code",
            "activity_type": "coding",
            "ocr_text": "import os",
        })
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": vlm_response}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = analyzer.analyze(_dummy_image())

        assert result is not None
        assert isinstance(result, AnalysisResult)
        assert result.active_app == "VS Code"
        assert result.model_used == "minicpm-v"  # signed: alpha

    def test_analyze_no_model_uses_fallback(self):
        """analyze() should return fallback result when no model available."""
        analyzer = _make_analyzer([{"name": "llama3:latest"}])
        result = analyzer.analyze(_dummy_image())
        assert result is not None
        assert result.model_used == "none"
        assert result.confidence == 0.1  # signed: alpha

    def test_analyze_api_error_uses_fallback(self):
        """analyze() should fall back gracefully on API errors."""
        analyzer = _make_analyzer([{"name": "minicpm-v:latest"}])

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
            result = analyzer.analyze(_dummy_image())

        assert result is not None
        assert result.model_used == "none"
        assert result.confidence == 0.1  # signed: alpha


# ---------------------------------------------------------------------------
# Tests for app/activity detection helpers
# ---------------------------------------------------------------------------
class TestDetectionHelpers:
    def test_detect_app_vscode(self):
        assert ScreenAnalyzer._detect_app("User is coding in VS Code") == "VS Code"

    def test_detect_app_chrome(self):
        assert ScreenAnalyzer._detect_app("Chrome browser open") == "Chrome"

    def test_detect_app_unknown(self):
        assert ScreenAnalyzer._detect_app("some random app") == ""

    def test_detect_activity_coding(self):
        assert ScreenAnalyzer._detect_activity("editing a function in IDE") == "coding"

    def test_detect_activity_browsing(self):
        assert ScreenAnalyzer._detect_activity("searching on google") == "browsing"

    def test_detect_activity_other(self):
        assert ScreenAnalyzer._detect_activity("doing nothing recognizable") == "other"
    # signed: alpha


# ---------------------------------------------------------------------------
# Tests for image_to_base64
# ---------------------------------------------------------------------------
class TestImageEncoding:
    def test_base64_encoding(self):
        """Should produce valid base64 string."""
        analyzer = _make_analyzer()
        img = _dummy_image(200, 100)
        b64 = analyzer._image_to_base64(img)
        assert isinstance(b64, str)
        assert len(b64) > 0
        import base64
        decoded = base64.b64decode(b64)
        assert len(decoded) > 0  # signed: alpha

    def test_resize_large_image(self):
        """Should resize images larger than max_size."""
        analyzer = _make_analyzer()
        img = _dummy_image(4000, 3000)
        b64 = analyzer._image_to_base64(img, max_size=512)
        assert isinstance(b64, str)
        assert len(b64) > 0  # signed: alpha
