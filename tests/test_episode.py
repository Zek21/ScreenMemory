"""Unit tests for skynet_episode and skynet_verifier modules."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repo root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.skynet_episode import Outcome, log_episode, list_episodes, load_episode
from tools.skynet_verifier import (
    SimpleVerifier,
    VerifierBase,
    clear_verifiers,
    register_verifier,
    verify_episode,
)


class TestOutcomeEnum(unittest.TestCase):
    """Verify Outcome enum semantics."""

    def test_values(self):
        self.assertEqual(Outcome.SUCCESS.value, "success")
        self.assertEqual(Outcome.FAILURE.value, "failure")
        self.assertEqual(Outcome.UNKNOWN.value, "unknown")

    def test_string_conversion(self):
        self.assertEqual(Outcome("success"), Outcome.SUCCESS)
        self.assertEqual(Outcome("failure"), Outcome.FAILURE)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            Outcome("invalid")


class TestLogEpisode(unittest.TestCase):
    """Verify episode logging writes correct JSON."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("tools.skynet_episode.EPISODES_DIR")
    def test_basic_log(self, mock_dir):
        mock_dir.__class__ = Path
        episodes_path = Path(self.tmpdir)
        mock_dir.mkdir = episodes_path.mkdir
        mock_dir.exists = episodes_path.exists
        mock_dir.__truediv__ = episodes_path.__truediv__

        with patch("tools.skynet_episode.EPISODES_DIR", episodes_path):
            ep = log_episode(
                task="run tests",
                result="All 5 tests passed",
                outcome="success",
                strategy_id="strat-001",
                worker="delta",
            )

        self.assertEqual(ep["task"], "run tests")
        self.assertEqual(ep["outcome"], "success")
        self.assertEqual(ep["worker"], "delta")
        self.assertEqual(ep["strategy_id"], "strat-001")
        self.assertIn("filepath", ep)
        self.assertTrue(os.path.isfile(ep["filepath"]))

        with open(ep["filepath"]) as f:
            stored = json.load(f)
        self.assertEqual(stored["task"], "run tests")

    @patch("tools.skynet_episode.EPISODES_DIR")
    def test_outcome_enum_accepted(self, _mock):
        episodes_path = Path(self.tmpdir)
        with patch("tools.skynet_episode.EPISODES_DIR", episodes_path):
            ep = log_episode(
                task="deploy",
                result="deployed ok",
                outcome=Outcome.SUCCESS,
                worker="alpha",
            )
        self.assertEqual(ep["outcome"], "success")

    @patch("tools.skynet_episode.EPISODES_DIR")
    def test_metadata_stored(self, _mock):
        episodes_path = Path(self.tmpdir)
        with patch("tools.skynet_episode.EPISODES_DIR", episodes_path):
            ep = log_episode(
                task="scan",
                result="found 3 issues",
                outcome="unknown",
                worker="beta",
                metadata={"issues_found": 3},
            )
        self.assertEqual(ep["metadata"]["issues_found"], 3)


class TestSimpleVerifier(unittest.TestCase):
    """Verify keyword-based classification."""

    def setUp(self):
        self.v = SimpleVerifier()

    def test_failure_on_traceback(self):
        ep = {"result": "Traceback (most recent call last): KeyError"}
        self.assertEqual(self.v.verify(ep), Outcome.FAILURE)

    def test_failure_on_exception(self):
        ep = {"result": "Exception raised during execution"}
        self.assertEqual(self.v.verify(ep), Outcome.FAILURE)

    def test_success_on_done(self):
        ep = {"result": "Task completed. Done."}
        self.assertEqual(self.v.verify(ep), Outcome.SUCCESS)

    def test_success_on_passed(self):
        ep = {"result": "All tests passed"}
        self.assertEqual(self.v.verify(ep), Outcome.SUCCESS)

    def test_unknown_on_ambiguous(self):
        ep = {"result": "Processed 42 items in 3.2s"}
        self.assertEqual(self.v.verify(ep), Outcome.UNKNOWN)

    def test_failure_takes_precedence(self):
        ep = {"result": "Done but an exception occurred"}
        self.assertEqual(self.v.verify(ep), Outcome.FAILURE)


class TestVerifyEpisode(unittest.TestCase):
    """Verify consensus logic across multiple verifiers."""

    def setUp(self):
        clear_verifiers()

    def tearDown(self):
        clear_verifiers()

    def test_single_verifier_success(self):
        register_verifier(SimpleVerifier())
        ep = {"result": "All done successfully"}
        self.assertEqual(verify_episode(ep), Outcome.SUCCESS)

    def test_failure_wins_consensus(self):
        class AlwaysSuccess(VerifierBase):
            def verify(self, episode):
                return Outcome.SUCCESS

        class AlwaysFailure(VerifierBase):
            def verify(self, episode):
                return Outcome.FAILURE

        register_verifier(AlwaysSuccess())
        register_verifier(AlwaysFailure())
        ep = {"result": "some result"}
        self.assertEqual(verify_episode(ep), Outcome.FAILURE)

    def test_explicit_verifiers_list(self):
        ep = {"result": "finished ok"}
        result = verify_episode(ep, verifiers=[SimpleVerifier()])
        self.assertEqual(result, Outcome.SUCCESS)

    def test_empty_verifiers_returns_unknown(self):
        ep = {"result": "whatever"}
        result = verify_episode(ep, verifiers=[])
        self.assertEqual(result, Outcome.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
