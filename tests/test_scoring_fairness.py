"""Tests for Convention 3 scoring fairness fixes in skynet_scoring.py.

Covers:
- Consultant base score initialization (Fix 1)
- Advisory contribution awards (Fix 2a)
- Cross-system review awards (Fix 2b)
- Zero-ticket bonus cooldown enforcement (Fix 3)
- Illegitimate deduction reset (Fix 4)
- Field normalization for new award types

signed: gamma
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_scoring as scoring


class FairnessTestBase(unittest.TestCase):
    """Base class with temp file setup for scoring tests."""

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
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmpdir.cleanup()

    def _read_scores(self) -> dict:
        return json.loads(self.scores_file.read_text(encoding="utf-8"))


# ─── Fix 1: Consultant Base Scores ──────────────────────────────

class TestConsultantBaseScores(FairnessTestBase):
    """Verify _ensure_worker grants base=6.0 to consultants."""

    def test_consultant_gets_base_score_on_first_creation(self):
        """Consultants should start with base=6.0 like workers."""
        result = scoring.award_advisory_contribution(
            "consultant", "test-task-1", "orchestrator"
        )
        self.assertEqual(result["total"], 6.05)  # 6.0 base + 0.05 award
        data = self._read_scores()
        self.assertEqual(data["scores"]["consultant"].get("base"), 6.0)

    def test_gemini_consultant_gets_base_score(self):
        """Gemini consultant also gets base=6.0."""
        result = scoring.award_cross_system_review(
            "gemini_consultant", "test-task-2", "alpha"
        )
        self.assertEqual(result["total"], 6.02)  # 6.0 base + 0.02 award
        data = self._read_scores()
        self.assertEqual(data["scores"]["gemini_consultant"].get("base"), 6.0)

    def test_worker_gets_base_score(self):
        """Workers should also get base=6.0 on first creation."""
        scoring.deduct_for_refactor("alpha", "test-refactor-1")
        data = self._read_scores()
        self.assertEqual(data["scores"]["alpha"].get("base"), 6.0)
        self.assertEqual(data["scores"]["alpha"]["total"], 5.99)  # 6.0 - 0.01

    def test_orchestrator_gets_base_score(self):
        """Orchestrator gets base=6.0 too."""
        scoring.award_bug_report("orchestrator", "bug-orch-1", "alpha")
        data = self._read_scores()
        self.assertEqual(data["scores"]["orchestrator"].get("base"), 6.0)

    def test_non_agent_does_not_get_base_score(self):
        """Random senders should NOT get base=6.0."""
        scoring.deduct_for_refactor("random_bot", "test-refactor-2")
        data = self._read_scores()
        self.assertNotIn("base", data["scores"]["random_bot"])
        self.assertEqual(data["scores"]["random_bot"]["total"], -0.01)

    def test_base_score_not_reapplied_on_existing_entry(self):
        """Base=6.0 should only be applied once, on first creation."""
        # First creation gives base
        scoring.award_advisory_contribution("consultant", "task-a", "alpha")
        data1 = self._read_scores()
        total1 = data1["scores"]["consultant"]["total"]

        # Second operation should NOT add base again
        scoring.award_advisory_contribution("consultant", "task-b", "beta")
        data2 = self._read_scores()
        total2 = data2["scores"]["consultant"]["total"]
        self.assertAlmostEqual(total2, total1 + 0.05, places=4)


# ─── Fix 2a: Advisory Contribution Awards ───────────────────────

class TestAdvisoryContribution(FairnessTestBase):
    """Verify award_advisory_contribution works correctly."""

    def test_default_award_amount(self):
        """Default award is +0.05."""
        result = scoring.award_advisory_contribution(
            "consultant", "advisory-1", "orchestrator"
        )
        data = self._read_scores()
        hist = [h for h in data["history"] if h["action"] == "advisory_contribution"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["amount"], 0.05)
        self.assertEqual(result["advisory_contribution_awards"], 1)

    def test_custom_award_amount(self):
        """Custom amount overrides default."""
        result = scoring.award_advisory_contribution(
            "consultant", "advisory-2", "alpha", amount=0.10
        )
        data = self._read_scores()
        hist = [h for h in data["history"] if h["action"] == "advisory_contribution"]
        self.assertEqual(hist[0]["amount"], 0.10)
        # base=6.0 + 0.10
        self.assertAlmostEqual(result["total"], 6.10, places=4)

    def test_requires_independent_validator(self):
        """Cannot self-validate advisory contributions."""
        with self.assertRaises(ValueError):
            scoring.award_advisory_contribution(
                "consultant", "advisory-3", "consultant"
            )

    def test_history_record_has_protocol_field(self):
        """History entry includes convention_3_fairness protocol."""
        scoring.award_advisory_contribution("consultant", "advisory-4", "beta")
        data = self._read_scores()
        hist = [h for h in data["history"] if h["action"] == "advisory_contribution"]
        self.assertEqual(hist[0]["protocol"], "convention_3_fairness")
        self.assertEqual(hist[0]["finding"], "consultant_advisory_accepted")
        self.assertIn("timestamp", hist[0])

    def test_increments_awards_counter(self):
        """Awards counter should increment."""
        scoring.award_advisory_contribution("consultant", "adv-a", "alpha")
        scoring.award_advisory_contribution("consultant", "adv-b", "beta")
        data = self._read_scores()
        self.assertEqual(data["scores"]["consultant"]["awards"], 2)
        self.assertEqual(data["scores"]["consultant"]["advisory_contribution_awards"], 2)

    def test_works_for_non_consultant_agents(self):
        """Any agent can receive advisory contribution awards."""
        result = scoring.award_advisory_contribution("gamma", "adv-g", "orchestrator")
        self.assertAlmostEqual(result["total"], 6.05, places=4)


# ─── Fix 2b: Cross-System Review Awards ─────────────────────────

class TestCrossSystemReview(FairnessTestBase):
    """Verify award_cross_system_review works correctly."""

    def test_default_award_amount(self):
        """Default award is +0.02."""
        result = scoring.award_cross_system_review(
            "gemini_consultant", "review-1", "delta"
        )
        data = self._read_scores()
        hist = [h for h in data["history"] if h["action"] == "cross_system_review"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["amount"], 0.02)
        self.assertEqual(result["cross_system_review_awards"], 1)

    def test_custom_award_amount(self):
        """Custom amount overrides default."""
        result = scoring.award_cross_system_review(
            "gemini_consultant", "review-2", "alpha", amount=0.03
        )
        self.assertAlmostEqual(result["total"], 6.03, places=4)

    def test_requires_independent_validator(self):
        """Cannot self-validate reviews."""
        with self.assertRaises(ValueError):
            scoring.award_cross_system_review(
                "gemini_consultant", "review-3", "gemini_consultant"
            )

    def test_history_record_structure(self):
        """History entry has correct structure."""
        scoring.award_cross_system_review("consultant", "review-4", "gamma")
        data = self._read_scores()
        hist = [h for h in data["history"] if h["action"] == "cross_system_review"]
        rec = hist[0]
        self.assertEqual(rec["worker"], "consultant")
        self.assertEqual(rec["protocol"], "convention_3_fairness")
        self.assertEqual(rec["finding"], "cross_system_review_validated")
        self.assertEqual(rec["task_id"], "review-4")
        self.assertEqual(rec["validator"], "gamma")

    def test_multiple_reviews_accumulate(self):
        """Multiple reviews properly accumulate."""
        scoring.award_cross_system_review("consultant", "r1", "alpha")
        scoring.award_cross_system_review("consultant", "r2", "beta")
        scoring.award_cross_system_review("consultant", "r3", "gamma")
        data = self._read_scores()
        self.assertEqual(data["scores"]["consultant"]["cross_system_review_awards"], 3)
        # 6.0 base + 3 * 0.02 = 6.06
        self.assertAlmostEqual(data["scores"]["consultant"]["total"], 6.06, places=4)


# ─── Fix 3: Zero-Ticket Bonus Cooldown ──────────────────────────

class TestZTBCooldown(FairnessTestBase):
    """Verify _ztb_cooldown_ok and cooldown enforcement in award_zero_ticket_clear."""

    def test_first_ztb_allowed(self):
        """First ZTB should always be allowed."""
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            result = scoring.award_zero_ticket_clear("ztb-first-1", "delta", "god")
        self.assertEqual(result["orchestrator"]["zero_ticket_bonus_awards"], 1)

    def test_rapid_second_ztb_blocked(self):
        """Second ZTB within 300s should be blocked."""
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            scoring.award_zero_ticket_clear("ztb-rapid-1", "delta", "god")
            with self.assertRaises(ValueError) as ctx:
                scoring.award_zero_ticket_clear("ztb-rapid-2", "delta", "god")
        self.assertIn("cooldown", str(ctx.exception).lower())

    def test_ztb_allowed_after_cooldown_expires(self):
        """ZTB should be allowed after 300s cooldown passes."""
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            scoring.award_zero_ticket_clear("ztb-cd-1", "delta", "god")

        # Backdate the ZTB history entry to 301 seconds ago
        data = self._read_scores()
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        for entry in data["history"]:
            if entry.get("action") == "zero_ticket_bonus":
                entry["timestamp"] = old_ts
        self.scores_file.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            result = scoring.award_zero_ticket_clear("ztb-cd-2", "delta", "god")
        self.assertEqual(result["orchestrator"]["zero_ticket_bonus_awards"], 2)

    def test_cooldown_per_agent_independent(self):
        """Cooldown tracks orchestrator and actor separately."""
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            scoring.award_zero_ticket_clear("ztb-ind-1", "alpha", "god")

        # Backdate only orchestrator entries but NOT alpha entries
        data = self._read_scores()
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        for entry in data["history"]:
            if (entry.get("action") == "zero_ticket_bonus"
                    and entry.get("worker") == "orchestrator"):
                entry["timestamp"] = old_ts
        self.scores_file.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

        # Should still fail because alpha is within cooldown
        with patch.object(scoring, "_all_tickets_cleared", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                scoring.award_zero_ticket_clear("ztb-ind-2", "alpha", "god")
        self.assertIn("alpha", str(ctx.exception))

    def test_ztb_cooldown_ok_no_prior_ztb(self):
        """_ztb_cooldown_ok returns True when no prior ZTB exists."""
        data = {"scores": {}, "history": [], "version": scoring.SCHEMA_VERSION}
        self.assertTrue(scoring._ztb_cooldown_ok(data, "alpha"))

    def test_ztb_cooldown_ok_malformed_timestamp(self):
        """_ztb_cooldown_ok returns True on malformed timestamps."""
        data = {
            "scores": {},
            "history": [
                {"worker": "alpha", "action": "zero_ticket_bonus",
                 "timestamp": "not-a-date"}
            ],
            "version": scoring.SCHEMA_VERSION,
        }
        self.assertTrue(scoring._ztb_cooldown_ok(data, "alpha"))


# ─── Fix 4: Reset Illegitimate Deductions ────────────────────────

class TestResetIllegitimateDeductions(FairnessTestBase):
    """Verify reset_illegitimate_deductions reverses boot-window spam."""

    def _setup_boot_spam(self, agent: str, count: int = 3,
                         boot_offset_s: int = 60):
        """Create fake boot-window spam deductions for testing."""
        base_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        data = {
            "scores": {
                agent: {
                    "total": -(count * 0.02),
                    "awards": 0,
                    "deductions": count,
                    **{k: 0 for k in [
                        "refactor_deductions", "refactor_reversals",
                        "bias_penalties", "proactive_ticket_clears",
                        "autonomous_pull_awards", "bug_reports_filed",
                        "bug_report_confirmations", "bug_cross_validations",
                        "zero_ticket_bonus_awards", "uncleared_work_deductions",
                        "tool_bypass_deductions", "repeat_offense_deductions",
                        "cleanup_help_awards", "cleanup_cv_awards",
                        "invalid_cleanup_awards", "advisory_contribution_awards",
                        "cross_system_review_awards",
                    ]}
                }
            },
            "history": [],
            "version": scoring.SCHEMA_VERSION,
        }

        # First entry establishes boot time
        data["history"].append({
            "worker": agent,
            "action": "identity_ack",
            "amount": 0,
            "timestamp": base_ts.isoformat(),
        })

        # Spam deductions within boot window
        for i in range(count):
            ts = base_ts + timedelta(seconds=30 + i * 10)
            data["history"].append({
                "worker": agent,
                "action": "deduct",
                "amount": -0.02,
                "forced": True,
                "validator": "spam_guard",
                "timestamp": ts.isoformat(),
                "task_id": f"spam_{i}",
            })

        self.scores_file.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        return data

    def test_reverses_boot_window_deductions(self):
        """Forced spam deductions within boot window should be reversed."""
        self._setup_boot_spam("consultant", count=3)
        result = scoring.reset_illegitimate_deductions("consultant")
        self.assertEqual(result["reversed_count"], 3)
        self.assertAlmostEqual(result["reversed_amount"], 0.06, places=4)
        # Original total was -0.06, reversal adds 0.06 back
        self.assertAlmostEqual(result["new_total"], 0.0, places=4)

    def test_preserves_deductions_outside_boot_window(self):
        """Deductions after the boot window should NOT be reversed."""
        base_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        data = {
            "scores": {
                "consultant": {
                    "total": -0.04,
                    "awards": 0, "deductions": 2,
                    **{k: 0 for k in [
                        "refactor_deductions", "refactor_reversals",
                        "bias_penalties", "proactive_ticket_clears",
                        "autonomous_pull_awards", "bug_reports_filed",
                        "bug_report_confirmations", "bug_cross_validations",
                        "zero_ticket_bonus_awards", "uncleared_work_deductions",
                        "tool_bypass_deductions", "repeat_offense_deductions",
                        "cleanup_help_awards", "cleanup_cv_awards",
                        "invalid_cleanup_awards", "advisory_contribution_awards",
                        "cross_system_review_awards",
                    ]}
                }
            },
            "history": [
                # Boot entry
                {"worker": "consultant", "action": "boot",
                 "amount": 0, "timestamp": base_ts.isoformat()},
                # Within window (30s after boot)
                {"worker": "consultant", "action": "deduct",
                 "amount": -0.02, "forced": True, "validator": "spam_guard",
                 "timestamp": (base_ts + timedelta(seconds=30)).isoformat()},
                # Outside window (600s after boot)
                {"worker": "consultant", "action": "deduct",
                 "amount": -0.02, "forced": True, "validator": "spam_guard",
                 "timestamp": (base_ts + timedelta(seconds=600)).isoformat()},
            ],
            "version": scoring.SCHEMA_VERSION,
        }
        self.scores_file.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

        result = scoring.reset_illegitimate_deductions("consultant")
        self.assertEqual(result["reversed_count"], 1)  # Only the one within window
        self.assertAlmostEqual(result["reversed_amount"], 0.02, places=4)

    def test_no_deductions_to_reverse(self):
        """Returns zero when no deductions exist."""
        result = scoring.reset_illegitimate_deductions("consultant")
        self.assertEqual(result["reversed_count"], 0)
        self.assertAlmostEqual(result["reversed_amount"], 0.0, places=4)

    def test_non_forced_deductions_not_reversed(self):
        """Only forced=true deductions are candidates for reversal."""
        base_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        data = {
            "scores": {"consultant": {
                "total": -0.02, "awards": 0, "deductions": 1,
                **{k: 0 for k in [
                    "refactor_deductions", "refactor_reversals",
                    "bias_penalties", "proactive_ticket_clears",
                    "autonomous_pull_awards", "bug_reports_filed",
                    "bug_report_confirmations", "bug_cross_validations",
                    "zero_ticket_bonus_awards", "uncleared_work_deductions",
                    "tool_bypass_deductions", "repeat_offense_deductions",
                    "cleanup_help_awards", "cleanup_cv_awards",
                    "invalid_cleanup_awards", "advisory_contribution_awards",
                    "cross_system_review_awards",
                ]}
            }},
            "history": [
                {"worker": "consultant", "action": "boot",
                 "amount": 0, "timestamp": base_ts.isoformat()},
                # NOT forced — should not be reversed
                {"worker": "consultant", "action": "deduct",
                 "amount": -0.02, "forced": False, "validator": "spam_guard",
                 "timestamp": (base_ts + timedelta(seconds=30)).isoformat()},
            ],
            "version": scoring.SCHEMA_VERSION,
        }
        self.scores_file.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

        result = scoring.reset_illegitimate_deductions("consultant")
        self.assertEqual(result["reversed_count"], 0)

    def test_custom_boot_window(self):
        """Custom boot_window_seconds parameter works."""
        self._setup_boot_spam("consultant", count=3, boot_offset_s=60)
        # With very short window (5s), none should be within it since
        # deductions start at 30s offset
        result = scoring.reset_illegitimate_deductions("consultant",
                                                        boot_window_seconds=5)
        self.assertEqual(result["reversed_count"], 0)

    def test_history_audit_trail(self):
        """Reversal creates an audit trail in history."""
        self._setup_boot_spam("consultant", count=2)
        scoring.reset_illegitimate_deductions("consultant")
        data = self._read_scores()
        reversal = [h for h in data["history"]
                    if h["action"] == "illegitimate_deduction_reversal"]
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal[0]["protocol"], "convention_3_fairness")
        self.assertEqual(reversal[0]["reversed_count"], 2)
        self.assertIn("convention3_boot_amnesty", reversal[0]["task_id"])


# ─── Field Normalization ─────────────────────────────────────────

class TestFieldNormalization(FairnessTestBase):
    """Verify _normalize_worker_entry includes new fairness fields."""

    def test_normalize_includes_advisory_contribution_awards(self):
        entry = scoring._normalize_worker_entry({})
        self.assertIn("advisory_contribution_awards", entry)
        self.assertEqual(entry["advisory_contribution_awards"], 0)

    def test_normalize_includes_cross_system_review_awards(self):
        entry = scoring._normalize_worker_entry({})
        self.assertIn("cross_system_review_awards", entry)
        self.assertEqual(entry["cross_system_review_awards"], 0)

    def test_normalize_preserves_existing_values(self):
        entry = scoring._normalize_worker_entry({
            "advisory_contribution_awards": 5,
            "cross_system_review_awards": 3,
        })
        self.assertEqual(entry["advisory_contribution_awards"], 5)
        self.assertEqual(entry["cross_system_review_awards"], 3)


# ─── Constants ───────────────────────────────────────────────────

class TestFairnessConstants(unittest.TestCase):
    """Verify Convention 3 constants are properly defined."""

    def test_default_advisory_contribution_award(self):
        self.assertEqual(scoring.DEFAULT_ADVISORY_CONTRIBUTION_AWARD, 0.05)

    def test_default_cross_system_review_award(self):
        self.assertEqual(scoring.DEFAULT_CROSS_SYSTEM_REVIEW_AWARD, 0.02)

    def test_ztb_cooldown_seconds(self):
        self.assertEqual(scoring.ZTB_COOLDOWN_SECONDS, 300)

    def test_base_score_agents_includes_consultants(self):
        self.assertIn("consultant", scoring.BASE_SCORE_AGENTS)
        self.assertIn("gemini_consultant", scoring.BASE_SCORE_AGENTS)

    def test_base_score_agents_includes_workers(self):
        for w in ("alpha", "beta", "gamma", "delta"):
            self.assertIn(w, scoring.BASE_SCORE_AGENTS)

    def test_base_score_agents_includes_orchestrator(self):
        self.assertIn("orchestrator", scoring.BASE_SCORE_AGENTS)

    def test_worker_roles_frozenset(self):
        self.assertIsInstance(scoring.WORKER_ROLES, frozenset)
        self.assertEqual(scoring.WORKER_ROLES,
                         frozenset({"alpha", "beta", "gamma", "delta"}))


if __name__ == "__main__":
    unittest.main()
