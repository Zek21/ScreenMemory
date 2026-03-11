"""
VLM-based screen analyzer using Ollama (MiniCPM-v / LLaVA).
Performs unified OCR + semantic understanding of screen captures.
"""
import io
import json
import base64
import time
import logging
import subprocess
from typing import Optional, Tuple
from dataclasses import dataclass
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    description: str
    ocr_text: str
    active_app: str
    activity_type: str
    confidence: float
    analysis_ms: float
    model_used: str
    raw_response: str


# Prompts optimized for screen understanding
SCREEN_ANALYSIS_PROMPT = """Describe this screenshot in detail. Include:
1. What application is open
2. What the user is doing
3. Any visible text or content
4. Window titles if visible"""

QUICK_ANALYSIS_PROMPT = """Briefly describe this screenshot in one sentence. What app is open and what is the user doing?"""

# Structured prompt for models that support JSON output (minicpm-v, llava)
STRUCTURED_PROMPT = """Analyze this screenshot and provide a structured description.

Return a JSON object with these fields:
- "description": A concise description of what the user is doing (1-2 sentences)
- "active_app": The name of the primary application visible (e.g., "VS Code", "Chrome", "Terminal")
- "window_title": The visible window title text
- "activity_type": One of: coding, browsing, writing, communication, media, file_management, terminal, other
- "visible_text": Key text content visible on screen (abbreviated, max 200 chars)
- "ocr_text": All readable text on screen, preserving layout where possible (max 500 chars)

Be concise but thorough. Focus on what matters for later search and recall."""


class ScreenAnalyzer:
    """
    Analyzes screen captures using Vision Language Models via Ollama.
    Supports MiniCPM-v (preferred) and LLaVA (fallback).
    """

    def __init__(self, model: str = "minicpm-v",
                 fallback_model: str = "llava:7b",
                 ollama_host: str = "http://localhost:11434",
                 max_tokens: int = 512,
                 timeout: int = 60):
        self.model = model
        self.fallback_model = fallback_model
        self.ollama_host = ollama_host
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._available_models = set()

        self._check_ollama()

    def _check_ollama(self):
        """Check if Ollama is running and which models are available."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{self.ollama_host}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                self._available_models = {
                    m["name"].split(":")[0] for m in data.get("models", [])
                }
                logger.info(f"Ollama models available: {self._available_models}")
        except Exception as e:
            logger.warning(f"Ollama not reachable: {e}")

    def _get_model(self) -> Optional[str]:
        """Get the best available model."""
        # Check primary model
        primary_base = self.model.split(":")[0]
        if primary_base in self._available_models:
            return self.model

        # Check fallback
        fallback_base = self.fallback_model.split(":")[0]
        if fallback_base in self._available_models:
            return self.fallback_model

        # Check for any vision model
        vision_models = {"minicpm-v", "llava", "bakllava", "moondream", "llava-phi3"}
        available_vision = self._available_models & vision_models
        if available_vision:
            return available_vision.pop()

        return None

    def _image_to_base64(self, image: Image.Image, max_size: int = 1024) -> str:
        """Convert PIL Image to base64 string, resizing if needed."""
        # Resize for efficiency while maintaining detail
        w, h = image.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            new_size = (int(w * ratio), int(h * ratio))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def analyze(self, image: Image.Image, detailed: bool = True) -> Optional[AnalysisResult]:
        """
        Analyze a screen capture using the VLM.

        Args:
            image: PIL Image of the screen capture
            detailed: If True, use structured analysis prompt; if False, quick summary

        Returns:
            AnalysisResult with description, OCR text, and metadata
        """
        model = self._get_model()
        if not model:
            logger.error("No vision model available. Pull one with: ollama pull minicpm-v")
            return self._fallback_analyze(image)

        prompt = SCREEN_ANALYSIS_PROMPT if detailed else QUICK_ANALYSIS_PROMPT
        img_b64 = self._image_to_base64(image)

        start = time.perf_counter()

        try:
            response = self._call_ollama(model, prompt, img_b64)
            elapsed_ms = (time.perf_counter() - start) * 1000

            return self._parse_response(response, elapsed_ms, model)

        except Exception as e:
            logger.error(f"VLM analysis failed: {e}")
            return self._fallback_analyze(image)

    def _call_ollama(self, model: str, prompt: str, image_b64: str) -> str:
        """Call Ollama API with image."""
        import urllib.request

        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "options": {
                "num_predict": self.max_tokens,
                "temperature": 0.1,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.ollama_host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
            return data.get("response", "")

    _APP_KEYWORDS = {
        "vs code": "VS Code", "visual studio": "VS Code", "vscode": "VS Code",
        "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
        "terminal": "Terminal", "powershell": "PowerShell", "cmd": "Terminal",
        "word": "Word", "excel": "Excel", "outlook": "Outlook",
        "slack": "Slack", "discord": "Discord", "teams": "Teams",
        "explorer": "File Explorer", "notepad": "Notepad",
    }

    _ACTIVITY_KEYWORDS = {
        "coding": ["code", "programming", "function", "class", "variable", "debug", "IDE"],
        "browsing": ["browser", "web", "search", "google", "website", "url"],
        "writing": ["document", "writing", "text", "word", "typing", "article"],
        "communication": ["chat", "email", "message", "slack", "teams", "discord"],
        "terminal": ["terminal", "command", "shell", "powershell", "bash", "cmd"],
        "media": ["video", "music", "youtube", "spotify", "player"],
    }

    @staticmethod
    def _detect_app(description: str) -> str:
        """Auto-detect active application from description text."""
        desc_lower = description.lower()
        for keyword, app_name in ScreenAnalyzer._APP_KEYWORDS.items():
            if keyword in desc_lower:
                return app_name
        return ""

    @staticmethod
    def _detect_activity(description: str) -> str:
        """Auto-detect activity type from description text."""
        desc_lower = description.lower()
        for atype, keywords in ScreenAnalyzer._ACTIVITY_KEYWORDS.items():
            if any(kw in desc_lower for kw in keywords):
                return atype
        return "other"

    def _parse_response(self, response: str, elapsed_ms: float, model: str) -> AnalysisResult:
        """Parse VLM response into structured AnalysisResult."""
        description, ocr_text, active_app, activity_type = self._extract_json_fields(response)

        if not description:
            description = response.strip()[:500]
        if not active_app:
            active_app = self._detect_app(description)
        if activity_type == "other":
            activity_type = self._detect_activity(description)
        if not ocr_text and len(description) > 50:
            ocr_text = description

        return AnalysisResult(
            description=description, ocr_text=ocr_text,
            active_app=active_app, activity_type=activity_type,
            confidence=0.8 if description else 0.1,
            analysis_ms=elapsed_ms, model_used=model, raw_response=response,
        )

    @staticmethod
    def _extract_json_fields(response: str):
        """Try to parse JSON fields from a VLM response."""
        try:
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(response[json_start:json_end])
                return (
                    parsed.get("description", ""),
                    parsed.get("ocr_text", parsed.get("visible_text", "")),
                    parsed.get("active_app", ""),
                    parsed.get("activity_type", "other"),
                )
        except (json.JSONDecodeError, ValueError):
            pass
        return "", "", "", "other"

    def _fallback_analyze(self, image: Image.Image) -> AnalysisResult:
        """
        Fallback analysis when no VLM is available.
        Uses basic image properties and active window info.
        """
        return AnalysisResult(
            description="Screen capture (no VLM available for analysis)",
            ocr_text="",
            active_app="unknown",
            activity_type="other",
            confidence=0.1,
            analysis_ms=0,
            model_used="none",
            raw_response="",
        )

    def pull_model(self, model: Optional[str] = None):
        """Pull a vision model via Ollama."""
        model = model or self.model
        logger.info(f"Pulling model: {model}")
        try:
            result = subprocess.run(
                ["ollama", "pull", model],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0:
                logger.info(f"Model {model} pulled successfully")
                self._check_ollama()
            else:
                logger.error(f"Failed to pull {model}: {result.stderr}")
        except Exception as e:
            logger.error(f"ollama pull failed: {e}")

    @property
    def is_available(self) -> bool:
        return self._get_model() is not None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    analyzer = ScreenAnalyzer()
    print(f"Available: {analyzer.is_available}")
    print(f"Models: {analyzer._available_models}")

    if not analyzer.is_available:
        print("\nNo vision model found. Pulling minicpm-v...")
        print("Run: ollama pull minicpm-v")
    else:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        print(f"\nAnalyzing screenshot ({img.width}x{img.height})...")
        result = analyzer.analyze(img)
        if result:
            print(f"Description: {result.description}")
            print(f"App: {result.active_app}")
            print(f"Activity: {result.activity_type}")
            print(f"Time: {result.analysis_ms:.0f}ms ({result.model_used})")
