#!/usr/bin/env python3
"""Tests for core/tool_synthesizer.py — tool generation, validation, registry.

Tests cover: ToolSpec dataclass, ToolValidator (syntax, safety, scoring),
ToolRegistry (save, load, search, delete), ToolSynthesizer (generate, execute),
ToolComposer (compose, decompose). Mock all AI calls and subprocess.

# signed: alpha
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── ToolSpec Tests ───────────────────────────────────────────────

class TestToolSpec:
    """Test ToolSpec dataclass."""

    def test_creation(self):
        from core.tool_synthesizer import ToolSpec
        spec = ToolSpec(
            tool_id="t1", name="my_tool", description="does stuff",
            parameters=[{"name": "x", "type": "int", "required": True}],
            return_type="int", source_code="def tool_function(x): return x",
            safety_score=0.9
        )
        assert spec.tool_id == "t1"
        assert spec.usage_count == 0
        assert spec.version == 1
        assert spec.category == "general"

    def test_to_dict(self):
        from core.tool_synthesizer import ToolSpec
        spec = ToolSpec("t1", "test", "desc", [], "str", "code", 0.8)
        d = spec.to_dict()
        assert d["tool_id"] == "t1"
        assert "parameters" in d
        assert "source_code" in d

    def test_from_dict_roundtrip(self):
        from core.tool_synthesizer import ToolSpec
        spec = ToolSpec("t1", "test", "desc", [{"name": "x"}], "int", "code", 0.8,
                        usage_count=5, version=2, category="math")
        d = spec.to_dict()
        spec2 = ToolSpec.from_dict(d)
        assert spec2.tool_id == spec.tool_id
        assert spec2.usage_count == 5
        assert spec2.version == 2
        assert spec2.category == "math"


# ── ToolValidator Tests ──────────────────────────────────────────

class TestToolValidator:
    """Test ToolValidator syntax, safety, and scoring."""

    def _make_validator(self):
        from core.tool_synthesizer import ToolValidator
        return ToolValidator()

    def test_valid_syntax(self):
        v = self._make_validator()
        ok, msg = v.validate_syntax("def foo(x): return x + 1")
        assert ok is True

    def test_invalid_syntax(self):
        v = self._make_validator()
        ok, msg = v.validate_syntax("def foo(x return")
        assert ok is False
        assert len(msg) > 0

    def test_safe_code(self):
        v = self._make_validator()
        code = "import math\ndef tool_function(x):\n    return math.sqrt(x)"
        ok, issues = v.validate_safety(code)
        assert ok is True
        assert issues == []

    def test_dangerous_os_system(self):
        v = self._make_validator()
        code = "import os\nos.system('rm -rf /')"
        ok, issues = v.validate_safety(code)
        assert ok is False
        assert len(issues) > 0

    def test_dangerous_eval(self):
        v = self._make_validator()
        code = "result = eval(user_input)"
        ok, issues = v.validate_safety(code)
        assert ok is False

    def test_dangerous_subprocess(self):
        v = self._make_validator()
        code = "import subprocess\nsubprocess.run(['cmd'])"
        ok, issues = v.validate_safety(code)
        assert ok is False

    def test_safety_score_perfect(self):
        v = self._make_validator()
        code = "def tool_function(x): return x + 1"
        score = v.compute_safety_score(code)
        assert 0.0 <= score <= 1.0
        assert score > 0.7  # Simple safe code

    def test_safety_score_bad_syntax(self):
        v = self._make_validator()
        score = v.compute_safety_score("def foo( return")
        assert score < 0.8  # Syntax penalty

    def test_safety_score_dangerous(self):
        v = self._make_validator()
        code = "import os\nos.system('rm -rf /')"
        score = v.compute_safety_score(code)
        assert score < 1.0  # Should be penalized at least somewhat
        # Verify safety check fails
        ok, issues = v.validate_safety(code)
        assert ok is False


# ── ToolRegistry Tests ───────────────────────────────────────────

class TestToolRegistry:
    """Test ToolRegistry SQLite storage."""

    def _make_registry(self, tmp_path):
        from core.tool_synthesizer import ToolRegistry
        db = str(tmp_path / "test_tools.db")
        return ToolRegistry(db_path=db)

    def _make_spec(self, tool_id="t1", name="test_tool"):
        from core.tool_synthesizer import ToolSpec
        return ToolSpec(
            tool_id=tool_id, name=name, description="test tool",
            parameters=[], return_type="str",
            source_code="def tool_function(): return 'hello'",
            safety_score=0.9
        )

    def test_save_and_load(self, tmp_path):
        reg = self._make_registry(tmp_path)
        spec = self._make_spec()
        reg.save(spec)
        loaded = reg.load("t1")
        assert loaded is not None
        assert loaded.tool_id == "t1"
        assert loaded.name == "test_tool"

    def test_load_nonexistent(self, tmp_path):
        reg = self._make_registry(tmp_path)
        loaded = reg.load("nonexistent")
        assert loaded is None

    def test_load_by_name(self, tmp_path):
        reg = self._make_registry(tmp_path)
        reg.save(self._make_spec(tool_id="t1", name="unique_name"))
        loaded = reg.load_by_name("unique_name")
        assert loaded is not None
        assert loaded.name == "unique_name"

    def test_list_all(self, tmp_path):
        reg = self._make_registry(tmp_path)
        reg.save(self._make_spec("t1", "tool_a"))
        reg.save(self._make_spec("t2", "tool_b"))
        all_tools = reg.list_all()
        assert len(all_tools) == 2

    def test_list_by_category(self, tmp_path):
        from core.tool_synthesizer import ToolSpec
        reg = self._make_registry(tmp_path)
        spec_math = ToolSpec("t1", "math_tool", "math", [], "int", "code", 0.9, category="math")
        spec_text = ToolSpec("t2", "text_tool", "text", [], "str", "code", 0.9, category="text")
        reg.save(spec_math)
        reg.save(spec_text)
        math_tools = reg.list_all(category="math")
        assert len(math_tools) == 1
        assert math_tools[0].category == "math"

    def test_delete(self, tmp_path):
        reg = self._make_registry(tmp_path)
        reg.save(self._make_spec())
        reg.delete("t1")
        loaded = reg.load("t1")
        assert loaded is None

    def test_increment_usage(self, tmp_path):
        reg = self._make_registry(tmp_path)
        reg.save(self._make_spec())
        reg.increment_usage("t1")
        loaded = reg.load("t1")
        assert loaded.usage_count == 1


# ── ToolSynthesizer Tests ────────────────────────────────────────

class TestToolSynthesizer:
    """Test ToolSynthesizer with mocked Ollama API."""

    def _make_synthesizer(self, tmp_path):
        from core.tool_synthesizer import ToolSynthesizer
        with patch.object(Path, "mkdir", return_value=None):
            synth = ToolSynthesizer.__new__(ToolSynthesizer)
            from core.tool_synthesizer import ToolValidator, ToolRegistry
            synth.validator = ToolValidator()
            synth.registry = ToolRegistry(str(tmp_path / "tools.db"))
            synth.ollama_url = "http://localhost:11434"
            synth.model = "test-model"
        return synth

    def test_generate_name(self):
        from core.tool_synthesizer import ToolSynthesizer
        name = ToolSynthesizer._generate_name(None, "Calculate the sum of two numbers")
        assert isinstance(name, str)
        assert len(name) > 0
        assert " " not in name  # snake_case

    def test_extract_code_from_response(self):
        from core.tool_synthesizer import ToolSynthesizer
        response = "```python\ndef tool_function(x):\n    return x + 1\n```"
        synth = ToolSynthesizer.__new__(ToolSynthesizer)
        code = synth._extract_code_from_response(response)
        assert "def tool_function" in code

    def test_build_generation_prompt(self):
        from core.tool_synthesizer import ToolSynthesizer
        synth = ToolSynthesizer.__new__(ToolSynthesizer)
        prompt = synth._build_generation_prompt("add two numbers", [])
        assert "tool_function" in prompt
        assert isinstance(prompt, str)

    def test_get_tool_from_registry(self, tmp_path):
        synth = self._make_synthesizer(tmp_path)
        from core.tool_synthesizer import ToolSpec
        spec = ToolSpec("t1", "my_func", "desc", [], "int",
                        "def tool_function(): return 1", 0.9)
        synth.registry.save(spec)
        loaded = synth.get_tool("my_func")
        assert loaded is not None

    def test_list_tools(self, tmp_path):
        synth = self._make_synthesizer(tmp_path)
        tools = synth.list_tools()
        assert isinstance(tools, list)


# ── ToolComposer Tests ───────────────────────────────────────────

class TestToolComposer:
    """Test ToolComposer pipeline composition and decomposition."""

    def test_decompose_and_split(self):
        from core.tool_synthesizer import ToolComposer
        composer = ToolComposer.__new__(ToolComposer)
        parts = composer.decompose("parse the data and clean it, then save it")
        assert len(parts) >= 2

    def test_decompose_single_task(self):
        from core.tool_synthesizer import ToolComposer
        composer = ToolComposer.__new__(ToolComposer)
        parts = composer.decompose("calculate fibonacci")
        assert len(parts) >= 1
