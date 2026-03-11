#!/usr/bin/env python3
"""Tests for episode deduplication and fingerprinting in skynet_episode.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from skynet_episode import (
    compute_fingerprint,
    log_episode,
    query_by_fingerprint,
    _find_existing_by_fingerprint,
    Outcome,
)


class EpisodeDedupTestBase(unittest.TestCase):
    """Base class that redirects EPISODES_DIR to a temp directory."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._patcher = patch("skynet_episode.EPISODES_DIR", self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestFingerprintGeneration(EpisodeDedupTestBase):
    """Test 1: Fingerprint is deterministic SHA256 of worker|task|outcome|result."""

    def test_deterministic_same_input(self):
        fp1 = compute_fingerprint("alpha", "fix bug", "success", "done")
        fp2 = compute_fingerprint("alpha", "fix bug", "success", "done")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)  # SHA256 hex length

    def test_different_content_different_fingerprint(self):
        fp1 = compute_fingerprint("alpha", "fix bug", "success", "done")
        fp2 = compute_fingerprint("alpha", "fix bug", "failure", "done")
        self.assertNotEqual(fp1, fp2)

    def test_different_worker_different_fingerprint(self):
        fp1 = compute_fingerprint("alpha", "task", "success", "ok")
        fp2 = compute_fingerprint("beta", "task", "success", "ok")
        self.assertNotEqual(fp1, fp2)


class TestFirstWriteSucceeds(EpisodeDedupTestBase):
    """Test 2: First log_episode() with unique content writes a new file."""

    def test_first_write_creates_file(self):
        ep = log_episode("build API", "built successfully", "success",
                         worker="gamma")
        self.assertIn("filepath", ep)
        self.assertTrue(Path(ep["filepath"]).exists())
        self.assertNotIn("deduplicated", ep)

    def test_first_write_has_fingerprint_field(self):
        ep = log_episode("scan code", "no issues", "success", worker="delta")
        self.assertIn("fingerprint", ep)
        self.assertEqual(len(ep["fingerprint"]), 64)


class TestDuplicateIsSkipped(EpisodeDedupTestBase):
    """Test 3: Duplicate log_episode() with same content returns existing."""

    def test_duplicate_returns_existing_path(self):
        ep1 = log_episode("fix auth", "fixed", "success", worker="alpha")
        ep2 = log_episode("fix auth", "fixed", "success", worker="alpha")
        self.assertTrue(ep2.get("deduplicated"))
        self.assertEqual(ep1["filepath"], ep2["filepath"])

    def test_duplicate_does_not_create_second_file(self):
        log_episode("task A", "result A", "success", worker="beta")
        log_episode("task A", "result A", "success", worker="beta")
        files = list(self.tmpdir.glob("*.json"))
        self.assertEqual(len(files), 1)


class TestDifferentContentNotDeduplicated(EpisodeDedupTestBase):
    """Test 4: Different content produces a different episode file."""

    def test_different_result_creates_new_file(self):
        ep1 = log_episode("deploy", "v1 deployed", "success", worker="gamma")
        ep2 = log_episode("deploy", "v2 deployed", "success", worker="gamma")
        self.assertNotEqual(ep1["filepath"], ep2["filepath"])
        self.assertNotIn("deduplicated", ep2)

    def test_different_outcome_creates_new_file(self):
        ep1 = log_episode("test suite", "all pass", "success", worker="delta")
        ep2 = log_episode("test suite", "all pass", "failure", worker="delta")
        self.assertNotEqual(ep1["filepath"], ep2["filepath"])


class TestQueryByFingerprint(EpisodeDedupTestBase):
    """Test 5: query_by_fingerprint() finds episodes by hash."""

    def test_query_existing_fingerprint(self):
        ep = log_episode("audit code", "clean", "success", worker="alpha")
        found = query_by_fingerprint(ep["fingerprint"])
        self.assertIsNotNone(found)
        self.assertEqual(found["task"], "audit code")
        self.assertEqual(found["fingerprint"], ep["fingerprint"])

    def test_query_nonexistent_fingerprint(self):
        found = query_by_fingerprint("0" * 64)
        self.assertIsNone(found)


class TestFingerprintFieldPersisted(EpisodeDedupTestBase):
    """Test 6: The fingerprint field is persisted in the JSON file on disk."""

    def test_fingerprint_in_json_on_disk(self):
        ep = log_episode("refactor", "done", "success", worker="gamma")
        with open(ep["filepath"], "r") as f:
            on_disk = json.load(f)
        self.assertIn("fingerprint", on_disk)
        self.assertEqual(on_disk["fingerprint"], ep["fingerprint"])
        expected_fp = compute_fingerprint("gamma", "refactor", "success", "done")
        self.assertEqual(on_disk["fingerprint"], expected_fp)


class TestEmptyAndNoneFields(EpisodeDedupTestBase):
    """Test 7: Edge cases with empty/None fields still produce valid fingerprints."""

    def test_none_worker_uses_unknown(self):
        ep = log_episode("task", "result", "success", worker=None)
        self.assertEqual(ep["worker"], "unknown")
        self.assertIn("fingerprint", ep)

    def test_empty_strings_still_fingerprint(self):
        fp = compute_fingerprint("", "", "", "")
        self.assertEqual(len(fp), 64)
        ep = log_episode("", "", "unknown", worker="")
        self.assertIn("fingerprint", ep)


class TestFindExistingByFingerprint(EpisodeDedupTestBase):
    """Test 8: _find_existing_by_fingerprint() internal function works."""

    def test_returns_none_when_dir_empty(self):
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        result = _find_existing_by_fingerprint("abc123")
        self.assertIsNone(result)

    def test_returns_path_when_match_found(self):
        ep = log_episode("task X", "result X", "success", worker="delta")
        found = _find_existing_by_fingerprint(ep["fingerprint"])
        self.assertIsNotNone(found)
        self.assertTrue(found.exists())


if __name__ == "__main__":
    unittest.main()
