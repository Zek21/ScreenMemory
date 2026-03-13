#!/usr/bin/env python3
"""Tests for the resolved incident pattern tracking system in tools/skynet_self.py.

Tests cover:
  1. get_resolved_patterns() returns empty dict when no file exists
  2. acknowledge_incident_pattern() creates the file and stores pattern data
  3. _should_alert() returns True for unresolved patterns
  4. _should_alert() returns False for resolved patterns
  5. _should_alert() returns True when incident count exceeds acknowledged count

Since _should_alert is a nested function inside _detect_incident_patterns,
we test its behaviour indirectly via _detect_incident_patterns and also
replicate its logic in a standalone helper for direct unit testing.
"""
# signed: gamma

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.skynet_self as skynet_self


class TestGetResolvedPatterns(unittest.TestCase):
    """Test get_resolved_patterns() function."""

    def test_returns_empty_dict_when_file_missing(self):
        """get_resolved_patterns returns {} when resolved_patterns.json does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "resolved_patterns.json"
            with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', fake_path):
                result = skynet_self.get_resolved_patterns()
                self.assertEqual(result, {})
                self.assertIsInstance(result, dict)
    # signed: gamma

    def test_returns_data_when_file_exists(self):
        """get_resolved_patterns returns stored data when file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "resolved_patterns.json"
            data = {"recurring_hwnd_failures": {
                "acknowledged_at": "2026-03-12T10:00:00",
                "reason": "fixed",
                "incident_count_at_ack": 3,
            }}
            fake_path.write_text(json.dumps(data))
            with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', fake_path):
                result = skynet_self.get_resolved_patterns()
                self.assertEqual(result, data)
    # signed: gamma

    def test_returns_empty_dict_on_corrupt_json(self):
        """get_resolved_patterns returns {} on corrupted JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "resolved_patterns.json"
            fake_path.write_text("{bad json!!")
            with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', fake_path):
                result = skynet_self.get_resolved_patterns()
                self.assertEqual(result, {})
    # signed: gamma


class TestAcknowledgeIncidentPattern(unittest.TestCase):
    """Test acknowledge_incident_pattern() function."""

    def test_creates_file_and_stores_pattern(self):
        """acknowledge_incident_pattern creates the file with correct pattern data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "resolved_patterns.json"
            self.assertFalse(fake_path.exists())
            with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', fake_path):
                result = skynet_self.acknowledge_incident_pattern(
                    "recurring_hwnd_failures", reason="test_resolve"
                )
                self.assertTrue(result)
                self.assertTrue(fake_path.exists())
                stored = json.loads(fake_path.read_text())
                self.assertIn("recurring_hwnd_failures", stored)
                entry = stored["recurring_hwnd_failures"]
                self.assertEqual(entry["reason"], "test_resolve")
                self.assertIn("acknowledged_at", entry)
                self.assertIsNone(entry["incident_count_at_ack"])
    # signed: gamma

    def test_appends_to_existing_patterns(self):
        """acknowledge_incident_pattern preserves existing entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "resolved_patterns.json"
            existing = {"old_pattern": {
                "acknowledged_at": "2026-01-01T00:00:00",
                "reason": "old",
                "incident_count_at_ack": 1,
            }}
            fake_path.write_text(json.dumps(existing))
            with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', fake_path):
                result = skynet_self.acknowledge_incident_pattern(
                    "new_pattern", reason="new_reason"
                )
                self.assertTrue(result)
                stored = json.loads(fake_path.read_text())
                self.assertIn("old_pattern", stored)
                self.assertIn("new_pattern", stored)
    # signed: gamma

    def test_returns_false_on_write_failure(self):
        """acknowledge_incident_pattern returns False when write fails."""
        with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE') as mock_file:
            mock_file.exists.return_value = False
            with patch.object(skynet_self, 'get_resolved_patterns', return_value={}):
                mock_file.write_text.side_effect = PermissionError("no write")
                result = skynet_self.acknowledge_incident_pattern("test", "reason")
                self.assertFalse(result)
    # signed: gamma


class TestShouldAlert(unittest.TestCase):
    """Test _should_alert logic from _detect_incident_patterns.

    Since _should_alert is a nested closure inside _detect_incident_patterns,
    we replicate its exact logic here for direct unit testing, and also test
    it indirectly via _detect_incident_patterns with crafted incidents.
    """

    @staticmethod
    def _should_alert_logic(name: str, count: int, resolved: dict) -> bool:
        """Exact replica of the _should_alert nested function logic."""
        if name not in resolved:
            return True
        ack = resolved[name]
        prev_count = ack.get("incident_count_at_ack")
        if prev_count is not None and count > prev_count:
            return True
        return False
    # signed: gamma

    def test_returns_true_for_unresolved_pattern(self):
        """_should_alert returns True when pattern is not in resolved dict."""
        resolved = {}
        self.assertTrue(
            self._should_alert_logic("recurring_hwnd_failures", 3, resolved)
        )
    # signed: gamma

    def test_returns_true_for_unresolved_with_other_patterns(self):
        """_should_alert returns True when other patterns are resolved but not this one."""
        resolved = {"some_other_pattern": {
            "acknowledged_at": "2026-01-01",
            "reason": "fixed",
            "incident_count_at_ack": 2,
        }}
        self.assertTrue(
            self._should_alert_logic("recurring_hwnd_failures", 3, resolved)
        )
    # signed: gamma

    def test_returns_false_for_resolved_pattern(self):
        """_should_alert returns False when pattern is resolved and count hasn't grown."""
        resolved = {"recurring_hwnd_failures": {
            "acknowledged_at": "2026-03-12T10:00:00",
            "reason": "fixed",
            "incident_count_at_ack": 5,
        }}
        self.assertFalse(
            self._should_alert_logic("recurring_hwnd_failures", 5, resolved)
        )
    # signed: gamma

    def test_returns_false_for_resolved_with_lower_count(self):
        """_should_alert returns False when current count is below acked count."""
        resolved = {"recurring_hwnd_failures": {
            "acknowledged_at": "2026-03-12T10:00:00",
            "reason": "fixed",
            "incident_count_at_ack": 5,
        }}
        self.assertFalse(
            self._should_alert_logic("recurring_hwnd_failures", 3, resolved)
        )
    # signed: gamma

    def test_returns_false_for_resolved_with_none_count(self):
        """_should_alert returns False when resolved with incident_count_at_ack=None."""
        resolved = {"recurring_hwnd_failures": {
            "acknowledged_at": "2026-03-12T10:00:00",
            "reason": "fixed",
            "incident_count_at_ack": None,
        }}
        self.assertFalse(
            self._should_alert_logic("recurring_hwnd_failures", 3, resolved)
        )
    # signed: gamma

    def test_returns_true_when_count_exceeds_acknowledged(self):
        """_should_alert returns True when incident count exceeds acknowledged count."""
        resolved = {"recurring_hwnd_failures": {
            "acknowledged_at": "2026-03-12T10:00:00",
            "reason": "fixed",
            "incident_count_at_ack": 3,
        }}
        self.assertTrue(
            self._should_alert_logic("recurring_hwnd_failures", 5, resolved)
        )
    # signed: gamma

    def test_returns_true_when_count_exceeds_by_one(self):
        """_should_alert returns True when count is exactly one more than acked."""
        resolved = {"recurring_hwnd_failures": {
            "acknowledged_at": "2026-03-12T10:00:00",
            "reason": "fixed",
            "incident_count_at_ack": 3,
        }}
        self.assertTrue(
            self._should_alert_logic("recurring_hwnd_failures", 4, resolved)
        )
    # signed: gamma


class TestDetectIncidentPatternsIntegration(unittest.TestCase):
    """Integration tests verifying _should_alert works within _detect_incident_patterns."""

    def _make_incidents(self, keyword: str, count: int) -> list:
        """Create a list of fake incidents containing the given keyword."""
        return [
            {"id": i, "what_happened": f"Issue with {keyword} #{i}",
             "root_cause": "test", "fix_applied": "test", "title": "test"}
            for i in range(count)
        ]
    # signed: gamma

    def test_detect_patterns_alerts_for_unresolved(self):
        """_detect_incident_patterns includes unresolved patterns in output."""
        incidents = self._make_incidents("hwnd", 3)
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_file = Path(tmpdir) / "incidents.json"
            inc_file.write_text(json.dumps(incidents))
            res_file = Path(tmpdir) / "resolved_patterns.json"
            # No resolved file exists -> should alert
            with patch.object(skynet_self, 'DATA', Path(tmpdir)):
                with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', res_file):
                    patterns = skynet_self.SkynetIntrospection._detect_incident_patterns()
                    pattern_names = [p["pattern"] for p in patterns]
                    self.assertIn("recurring_hwnd_failures", pattern_names)
    # signed: gamma

    def test_detect_patterns_suppresses_resolved(self):
        """_detect_incident_patterns excludes resolved patterns with matching count."""
        incidents = self._make_incidents("hwnd", 3)
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_file = Path(tmpdir) / "incidents.json"
            inc_file.write_text(json.dumps(incidents))
            resolved = {"recurring_hwnd_failures": {
                "acknowledged_at": "2026-03-12T10:00:00",
                "reason": "fixed",
                "incident_count_at_ack": 5,
            }}
            res_file = Path(tmpdir) / "resolved_patterns.json"
            res_file.write_text(json.dumps(resolved))
            with patch.object(skynet_self, 'DATA', Path(tmpdir)):
                with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', res_file):
                    patterns = skynet_self.SkynetIntrospection._detect_incident_patterns()
                    pattern_names = [p["pattern"] for p in patterns]
                    self.assertNotIn("recurring_hwnd_failures", pattern_names)
    # signed: gamma

    def test_detect_patterns_alerts_when_count_exceeds_ack(self):
        """_detect_incident_patterns includes pattern when count exceeds acked count."""
        incidents = self._make_incidents("hwnd", 5)
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_file = Path(tmpdir) / "incidents.json"
            inc_file.write_text(json.dumps(incidents))
            resolved = {"recurring_hwnd_failures": {
                "acknowledged_at": "2026-03-12T10:00:00",
                "reason": "fixed",
                "incident_count_at_ack": 3,
            }}
            res_file = Path(tmpdir) / "resolved_patterns.json"
            res_file.write_text(json.dumps(resolved))
            with patch.object(skynet_self, 'DATA', Path(tmpdir)):
                with patch.object(skynet_self, '_RESOLVED_PATTERNS_FILE', res_file):
                    patterns = skynet_self.SkynetIntrospection._detect_incident_patterns()
                    pattern_names = [p["pattern"] for p in patterns]
                    self.assertIn("recurring_hwnd_failures", pattern_names)
    # signed: gamma


if __name__ == "__main__":
    unittest.main()
# signed: gamma
