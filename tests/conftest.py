"""Shared pytest fixtures for the ScreenMemory test suite."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "core"))


@pytest.fixture
def tmp_db_path(tmp_path):
    """Provide a temporary database path for test isolation."""
    return str(tmp_path / "test_screen_memory.db")


@pytest.fixture
def mock_llm_response():
    """Standard mock LLM response for pipeline tests."""
    return {
        "text": "Mock LLM response for testing",
        "tokens_used": 100,
        "model": "test-model",
    }


@pytest.fixture
def sample_query():
    """A representative query for testing."""
    return "Analyze the code in main.py and suggest improvements"


@pytest.fixture
def complex_query():
    """A multi-domain, multi-hop query for COMPLEX/ADVERSARIAL testing."""
    return (
        "Step by step, compare the market performance of the top 5 tech stocks, "
        "analyze their financial statements, then write a Python function that "
        "must compute risk-adjusted returns given these constraints exactly."
    )


@pytest.fixture
def trivial_query():
    """A minimal query for TRIVIAL classification."""
    return "hello"
