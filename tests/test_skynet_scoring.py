import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_scoring as scoring


class TestSkynetScoringProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.scores_file = Path(self.tmpdir.name) / "worker_scores.json"
        self.brain_config_file = Path(self.tmpdir.name) / "brain_config.json"
        self.brain_config_file.write_text(
            json.dumps(
                {
                    "dispatch_rules": {
                        "scoring_protocol": {
                        "award_per_task": 0.01,
                        "failed_validation_deduction": 0.005,
                        "bug_report_award": 0.01,
                        "bug_report_confirmation_award": 0.01,
                        "bug_cross_validation_award": 0.01,
                        "refactor_deduction": 0.01,
                        "refactor_necessary_reversal": 0.01,
                        "biased_refactor_report_deduction": 0.1,
                        "proactive_ticket_clear_award": 0.2,
                        "autonomous_pull_award": 0.2,
                        "ticket_zero_bonus_award": 1.0,
                        "require_independent_refactor_validation": True,
                    }
                }
                }
            ),
            encoding="utf-8",
        )
        self.patches = [
            patch.object(scoring, "SCORES_FILE", self.scores_file),
            patch.object(scoring, "BRAIN_CONFIG_FILE", self.brain_config_file),
            patch.object(scoring, "_bus_post", return_value=True),
        ]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.tmpdir.cleanup()

    def test_refactor_deduction_applies_default_penalty(self):
        result = scoring.deduct_for_refactor("alpha", "refactor-task-1")
        # alpha is in BASE_SCORE_AGENTS, so starts with base=6.0
        self.assertEqual(result["total"], 5.99)
        self.assertEqual(result["deductions"], 1)
        self.assertEqual(result["refactor_deductions"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], scoring.SCHEMA_VERSION)
        self.assertEqual(data["history"][-1]["action"], "refactor_deduct")

    def test_unbiased_result_cancels_refactor_deduction(self):
        scoring.deduct_for_refactor("alpha", "refactor-task-2")
        result = scoring.cancel_refactor_deduction_if_necessary(
            "alpha", "refactor-task-2", "beta"
        )
        # alpha starts at 6.0, -0.01 deduct, +0.01 reversal = 6.0
        self.assertEqual(result["total"], 6.0)
        self.assertEqual(result["awards"], 1)
        self.assertEqual(result["refactor_reversals"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-1]["action"], "refactor_reversal")
        self.assertTrue(data["history"][-1]["unbiased"])

    def test_necessary_refactor_requires_independent_validator(self):
        scoring.deduct_for_refactor("alpha", "refactor-task-3")
        with self.assertRaises(ValueError):
            scoring.cancel_refactor_deduction_if_necessary(
                "alpha", "refactor-task-3", "alpha"
            )

    def test_biased_refactor_report_costs_point_one(self):
        scoring.deduct_for_refactor("alpha", "refactor-task-4")
        result = scoring.deduct_for_biased_refactor_report(
            "alpha", "refactor-task-4", "gamma"
        )
        # alpha starts at 6.0, -0.01 refactor, -0.10 bias = 5.89
        self.assertEqual(result["total"], 5.89)
        self.assertEqual(result["deductions"], 2)
        self.assertEqual(result["bias_penalties"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-1]["action"], "biased_refactor_report")

    def test_refactor_reversal_requires_prior_deduction(self):
        with self.assertRaises(ValueError):
            scoring.cancel_refactor_deduction_if_necessary(
                "alpha", "missing-refactor-task", "beta"
            )

    def test_proactive_ticket_clear_awards_point_two(self):
        result = scoring.award_proactive_ticket_clear(
            "consultant", "ticket-clear-1", "god"
        )
        # consultant is in BASE_SCORE_AGENTS, starts at 6.0
        self.assertEqual(result["total"], 6.2)
        self.assertEqual(result["awards"], 1)
        self.assertEqual(result["proactive_ticket_clears"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], scoring.SCHEMA_VERSION)
        self.assertEqual(data["history"][-1]["action"], "proactive_ticket_clear")

    def test_autonomous_pull_awards_point_two(self):
        result = scoring.award_autonomous_pull(
            "alpha", "ticket-pull-1", "orchestrator"
        )
        # alpha starts at 6.0
        self.assertEqual(result["total"], 6.2)
        self.assertEqual(result["awards"], 1)
        self.assertEqual(result["autonomous_pull_awards"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-1]["action"], "autonomous_pull_award")

    def test_proactive_ticket_clear_requires_independent_validator(self):
        with self.assertRaises(ValueError):
            scoring.award_proactive_ticket_clear(
                "orchestrator", "ticket-clear-2", "orchestrator"
            )

    def test_bug_report_filing_awards_point_zero_one(self):
        result = scoring.award_bug_report("alpha", "bug-1", "orchestrator")
        # alpha starts at 6.0
        self.assertEqual(result["total"], 6.01)
        self.assertEqual(result["awards"], 1)
        self.assertEqual(result["bug_reports_filed"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-1]["action"], "bug_report_filed")

    def test_bug_confirmation_rewards_reporter_and_validator(self):
        scoring.award_bug_report("alpha", "bug-2", "orchestrator")
        result = scoring.confirm_bug_report("alpha", "bug-2", "beta")

        # alpha: 6.0 base + 0.01 report + 0.01 confirmation = 6.02
        self.assertEqual(result["reporter"]["total"], 6.02)
        self.assertEqual(result["reporter"]["bug_report_confirmations"], 1)
        # beta: 6.0 base + 0.01 cross-validation = 6.01
        self.assertEqual(result["validator"]["total"], 6.01)
        self.assertEqual(result["validator"]["bug_cross_validations"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-2]["action"], "bug_report_confirmed")
        self.assertEqual(data["history"][-1]["action"], "bug_cross_validation_award")

    def test_bug_confirmation_requires_prior_report(self):
        with self.assertRaises(ValueError):
            scoring.confirm_bug_report("alpha", "bug-missing", "beta")

    def test_zero_ticket_bonus_awards_orchestrator_and_last_worker(self):
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            result = scoring.award_zero_ticket_clear("todo-last-1", "delta", "god")

        # orchestrator: 6.0 base + 1.0 ZTB = 7.0
        self.assertEqual(result["orchestrator"]["total"], 7.0)
        self.assertEqual(result["orchestrator"]["zero_ticket_bonus_awards"], 1)
        # delta: 6.0 base + 1.0 ZTB = 7.0
        self.assertEqual(result["last_worker"]["total"], 7.0)
        self.assertEqual(result["last_worker"]["zero_ticket_bonus_awards"], 1)

        data = json.loads(self.scores_file.read_text(encoding="utf-8"))
        self.assertEqual(data["history"][-2]["worker"], "orchestrator")
        self.assertEqual(data["history"][-1]["worker"], "delta")
        self.assertTrue(all(r["action"] == "zero_ticket_bonus" for r in data["history"][-2:]))

    def test_zero_ticket_bonus_requires_empty_queue(self):
        with patch.object(scoring, "_all_tickets_cleared", return_value=False):
            with self.assertRaises(ValueError):
                scoring.award_zero_ticket_clear("todo-last-2", "alpha", "god")


if __name__ == "__main__":
    unittest.main()
