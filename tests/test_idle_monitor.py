# signed: consultant
"""Regression tests for the idle monitor self-invoke prompt."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.skynet_idle_monitor as idle_monitor


def test_self_invoke_task_requires_joint_consultant_dashboard_verification():
    prompt = idle_monitor.SELF_INVOKE_TASK

    assert "Codex Consultant" in prompt
    assert "Gemini Consultant" in prompt
    assert "data/consultant_state.json" in prompt
    assert "data/gemini_consultant_state.json" in prompt
    assert "http://localhost:8421/consultants" in prompt
    assert "http://localhost:8421/leadership" in prompt
    assert "http://localhost:8421/dashboard/data" in prompt
    assert "http://localhost:8420/bus/messages?limit=30" in prompt
    assert "BOTH consultants" in prompt
    assert "reporting to Skynet" in prompt
