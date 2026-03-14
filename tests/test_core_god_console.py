"""
Tests for core/god_console.py — GodConsole, classify_risk, data models.

Tests cover:
- classify_risk() keyword matching and risk levels
- GodConsole approval workflow (add, approve, reject, get)
- set_directive() creates directives correctly
- DATA_DIR uses relative path (not hardcoded)
- PendingApproval expiry logic
- GodDirective serialization round-trip

# signed: beta
"""
import os
import time
import tempfile
import unittest
from pathlib import Path


class TestClassifyRisk(unittest.TestCase):
    """Test the classify_risk function for keyword-based risk classification."""

    def test_critical_production_keyword(self):
        """Actions containing 'production' should be CRITICAL."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("deploy to production server")
        self.assertEqual(result, RiskLevel.CRITICAL)
        # signed: beta

    def test_critical_delete_keyword(self):
        """Actions containing 'delete' should be CRITICAL."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("delete user records")
        self.assertEqual(result, RiskLevel.CRITICAL)
        # signed: beta

    def test_critical_api_call_keyword(self):
        """Actions containing 'api_call' should be CRITICAL."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("make external_api call")
        self.assertEqual(result, RiskLevel.CRITICAL)
        # signed: beta

    def test_high_deploy_keyword(self):
        """Actions with 'deploy' (not 'production') should be HIGH."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("deploy staging environment")
        self.assertEqual(result, RiskLevel.HIGH)
        # signed: beta

    def test_medium_build_keyword(self):
        """Actions with 'build' should be MEDIUM."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("build the project")
        self.assertEqual(result, RiskLevel.MEDIUM)
        # signed: beta

    def test_low_read_keyword(self):
        """Actions with 'read' should be LOW."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("read configuration file")
        self.assertEqual(result, RiskLevel.LOW)
        # signed: beta

    def test_unrecognized_defaults_to_medium(self):
        """Unrecognized actions should default to MEDIUM."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("do something random and unknown")
        self.assertEqual(result, RiskLevel.MEDIUM)
        # signed: beta

    def test_case_insensitive(self):
        """Risk classification should be case-insensitive."""
        from core.god_console import classify_risk, RiskLevel
        result = classify_risk("DELETE ALL RECORDS NOW")
        self.assertEqual(result, RiskLevel.CRITICAL)
        # signed: beta


class TestDataDir(unittest.TestCase):
    """Test that DATA_DIR is computed from __file__, not hardcoded."""

    def test_data_dir_is_relative(self):
        """DATA_DIR should be derived from __file__, not a hardcoded absolute path."""
        from core.god_console import DATA_DIR
        # DATA_DIR should be <repo>/data, derived from core/god_console.py path
        self.assertTrue(DATA_DIR.name == "data")
        # It should NOT be hardcoded to a specific drive path
        god_console_file = Path(__file__).resolve().parent.parent / "core" / "god_console.py"
        expected = god_console_file.resolve().parent.parent / "data"
        self.assertEqual(DATA_DIR.resolve(), expected.resolve())
        # signed: beta


class TestGodConsoleApprovalWorkflow(unittest.TestCase):
    """Test the GodConsole approval queue workflow with a temp database."""

    def setUp(self):
        """Create a temp DB for each test."""
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        """Remove temp DB."""
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _make_console(self):
        from core.god_console import GodConsole
        return GodConsole(db_path=self.db_path)

    def test_add_approval_returns_id(self):
        """add_approval should return a string ID starting with 'approval_'."""
        god = self._make_console()
        aid = god.add_approval("deploy_prod", "alpha")
        self.assertTrue(aid.startswith("approval_"))
        # signed: beta

    def test_add_approval_auto_classifies_risk(self):
        """When risk_level is None, add_approval should auto-classify."""
        from core.god_console import RiskLevel
        god = self._make_console()
        aid = god.add_approval("delete all records", "beta")
        approval = god.get_approval(aid)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.risk_level, RiskLevel.CRITICAL.value)
        # signed: beta

    def test_approve_resolves_pending(self):
        """Approving a pending approval should set status to 'approved'."""
        from core.god_console import ApprovalStatus
        god = self._make_console()
        aid = god.add_approval("read logs", "gamma")
        result = god.approve(aid)
        self.assertTrue(result)
        approval = god.get_approval(aid)
        self.assertEqual(approval.status, ApprovalStatus.APPROVED.value)
        # signed: beta

    def test_reject_resolves_pending_with_reason(self):
        """Rejecting should set status to 'rejected' with a reason."""
        from core.god_console import ApprovalStatus
        god = self._make_console()
        aid = god.add_approval("deploy_prod", "delta")
        result = god.reject(aid, reason="too risky")
        self.assertTrue(result)
        approval = god.get_approval(aid)
        self.assertEqual(approval.status, ApprovalStatus.REJECTED.value)
        self.assertEqual(approval.rejection_reason, "too risky")
        # signed: beta

    def test_approve_nonexistent_returns_false(self):
        """Approving a nonexistent ID should return False."""
        god = self._make_console()
        result = god.approve("approval_does_not_exist")
        self.assertFalse(result)
        # signed: beta

    def test_get_pending_returns_only_pending(self):
        """get_pending should only return approvals with status='pending'."""
        god = self._make_console()
        id1 = god.add_approval("read file", "alpha")
        id2 = god.add_approval("build project", "beta")
        god.approve(id1)  # no longer pending

        pending = god.get_pending()
        pending_ids = [p.id for p in pending]
        self.assertNotIn(id1, pending_ids)
        self.assertIn(id2, pending_ids)
        # signed: beta

    def test_double_approve_returns_false(self):
        """Approving an already-approved item should return False."""
        god = self._make_console()
        aid = god.add_approval("scan logs", "alpha")
        god.approve(aid)
        result = god.approve(aid)
        self.assertFalse(result)
        # signed: beta


class TestGodConsoleDirectives(unittest.TestCase):
    """Test directive management."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _make_console(self):
        from core.god_console import GodConsole
        return GodConsole(db_path=self.db_path)

    def test_set_directive_returns_id(self):
        """set_directive should return a string ID starting with 'dir_'."""
        god = self._make_console()
        did = god.set_directive("Increase test coverage to 80%", priority=2)
        self.assertTrue(did.startswith("dir_"))
        # signed: beta

    def test_set_directive_clamps_priority(self):
        """Priority should be clamped to 1-10 range."""
        god = self._make_console()
        # Priority 0 -> clamped to 1
        did = god.set_directive("Test", priority=0)
        # Priority 99 -> clamped to 10
        did2 = god.set_directive("Test2", priority=99)
        # We can verify by checking the DB directly
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT priority FROM directives WHERE id=?", (did,)).fetchone()
            self.assertEqual(row["priority"], 1)
            row2 = conn.execute("SELECT priority FROM directives WHERE id=?", (did2,)).fetchone()
            self.assertEqual(row2["priority"], 10)
        # signed: beta

    def test_complete_directive(self):
        """complete_directive should set status to 'completed'."""
        from core.god_console import DirectiveStatus
        god = self._make_console()
        did = god.set_directive("Fix all bugs", priority=1)
        result = god.complete_directive(did)
        self.assertTrue(result)
        # Verify via DB
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM directives WHERE id=?", (did,)).fetchone()
            self.assertEqual(row[0], DirectiveStatus.COMPLETED.value)
        # signed: beta

    def test_cancel_directive(self):
        """cancel_directive should set status to 'cancelled'."""
        from core.god_console import DirectiveStatus
        god = self._make_console()
        did = god.set_directive("Optional task", priority=8)
        result = god.cancel_directive(did)
        self.assertTrue(result)
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM directives WHERE id=?", (did,)).fetchone()
            self.assertEqual(row[0], DirectiveStatus.CANCELLED.value)
        # signed: beta

    def test_add_sub_task(self):
        """add_sub_task should append to directive's sub_tasks list."""
        god = self._make_console()
        did = god.set_directive("Improve docs", priority=3)
        god.add_sub_task(did, "Write README")
        god.add_sub_task(did, "Add docstrings")
        import sqlite3, json
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT sub_tasks FROM directives WHERE id=?", (did,)).fetchone()
            tasks = json.loads(row[0])
            self.assertEqual(len(tasks), 2)
            self.assertIn("Write README", tasks)
            self.assertIn("Add docstrings", tasks)
        # signed: beta


class TestPendingApprovalModel(unittest.TestCase):
    """Test PendingApproval dataclass logic."""

    def test_is_expired_when_stale(self):
        """is_expired should return True when age exceeds auto_expire_seconds."""
        from core.god_console import PendingApproval, ApprovalStatus
        approval = PendingApproval(
            id="test_1",
            action="deploy",
            agent_id="alpha",
            risk_level="high",
            detail="test",
            timestamp=time.time() - 7200,  # 2 hours ago
            status=ApprovalStatus.PENDING.value,
            auto_expire_seconds=3600,
        )
        self.assertTrue(approval.is_expired)
        # signed: beta

    def test_is_not_expired_when_fresh(self):
        """is_expired should return False when within auto_expire_seconds."""
        from core.god_console import PendingApproval, ApprovalStatus
        approval = PendingApproval(
            id="test_2",
            action="deploy",
            agent_id="alpha",
            risk_level="high",
            detail="test",
            timestamp=time.time(),
            status=ApprovalStatus.PENDING.value,
            auto_expire_seconds=3600,
        )
        self.assertFalse(approval.is_expired)
        # signed: beta

    def test_is_expired_false_for_resolved(self):
        """is_expired should return False for already-resolved approvals."""
        from core.god_console import PendingApproval, ApprovalStatus
        approval = PendingApproval(
            id="test_3",
            action="deploy",
            agent_id="alpha",
            risk_level="high",
            detail="test",
            timestamp=time.time() - 7200,
            status=ApprovalStatus.APPROVED.value,
            auto_expire_seconds=3600,
        )
        self.assertFalse(approval.is_expired)
        # signed: beta

    def test_to_dict_roundtrip(self):
        """to_dict -> from_dict should produce equivalent object."""
        from core.god_console import PendingApproval, ApprovalStatus
        original = PendingApproval(
            id="rt_1", action="read logs", agent_id="beta",
            risk_level="low", detail="audit", timestamp=time.time(),
            status=ApprovalStatus.PENDING.value,
        )
        d = original.to_dict()
        recovered = PendingApproval.from_dict(d)
        self.assertEqual(original.id, recovered.id)
        self.assertEqual(original.action, recovered.action)
        self.assertEqual(original.risk_level, recovered.risk_level)
        # signed: beta


class TestGodDirectiveModel(unittest.TestCase):
    """Test GodDirective dataclass serialization."""

    def test_to_dict_serializes_sub_tasks(self):
        """to_dict should JSON-serialize sub_tasks list."""
        from core.god_console import GodDirective
        import json
        d = GodDirective(
            id="dir_1", goal="test", priority=5,
            created_at=time.time(), sub_tasks=["a", "b"],
        )
        serialized = d.to_dict()
        self.assertIsInstance(serialized["sub_tasks"], str)
        self.assertEqual(json.loads(serialized["sub_tasks"]), ["a", "b"])
        # signed: beta

    def test_from_dict_deserializes_sub_tasks(self):
        """from_dict should parse JSON sub_tasks back to list."""
        from core.god_console import GodDirective
        import json
        d = {
            "id": "dir_2", "goal": "improve", "priority": 3,
            "created_at": time.time(), "sub_tasks": json.dumps(["x", "y"]),
        }
        directive = GodDirective.from_dict(d)
        self.assertIsInstance(directive.sub_tasks, list)
        self.assertEqual(directive.sub_tasks, ["x", "y"])
        # signed: beta


if __name__ == "__main__":
    unittest.main()
