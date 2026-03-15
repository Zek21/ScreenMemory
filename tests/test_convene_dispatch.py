"""Focused tests for convene gate, dispatch preamble, and identity guard integration.

Tests the convene-first protocol, preamble construction, and security boundaries
WITHOUT requiring a live Skynet backend (mocks HTTP calls).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


ARCH_BACKED_ENGINE_REPORT = (
    "tools/engine_metrics.py _probe() instantiates analyzer, embedder, ocr, and capture during "
    "collect_engine_metrics() on the /engines path, so constructor cost dominates the metrics probe. "
    "Use import_only in _PROBES for those engines instead of instantiating them on every probe."
)


class TestBuildPreamble(unittest.TestCase):
    """Validate dispatch preamble construction and identity fingerprinting."""

    def setUp(self):
        from tools.skynet_dispatch import build_preamble
        self.build_preamble = build_preamble

    def test_preamble_contains_worker_name(self):
        p = self.build_preamble("alpha")
        self.assertIn("You are worker alpha", p)
        self.assertIn("sender':'alpha'", p)

    def test_preamble_contains_no_steering(self):
        p = self.build_preamble("beta")
        self.assertIn("Do NOT show steering options", p)
        self.assertIn("draft choices", p)

    def test_preamble_contains_identity_mismatch_warning(self):
        p = self.build_preamble("gamma")
        self.assertIn("IDENTITY MISMATCH", p)
        self.assertIn("preamble for gamma", p)

    def test_preamble_contains_bus_instructions(self):
        p = self.build_preamble("delta")
        self.assertIn("guarded_publish", p)  # signed: gamma
        self.assertIn("topic", p)
        self.assertIn("orchestrator", p)

    def test_preamble_contains_skynet_tools(self):
        p = self.build_preamble("alpha")
        self.assertIn("orch_realtime.py status", p)
        self.assertIn("skynet_dispatch.py --idle", p)
        self.assertIn("skynet_brain_dispatch.py", p)

    def test_preamble_contains_architecture_review_rule(self):
        p = self.build_preamble("alpha")
        self.assertIn("ARCHITECTURE REVIEW RULE", p)
        self.assertIn("realistic fix", p)

    def test_preamble_contains_consolidated_digest_rule(self):
        p = self.build_preamble("alpha")
        self.assertIn("elevated_digest", p)
        self.assertIn("30 minutes", p)

    def test_each_worker_gets_unique_preamble(self):
        """Each worker's preamble must reference only that worker's name in key positions."""
        for name in ["alpha", "beta", "gamma", "delta"]:
            p = self.build_preamble(name)
            self.assertIn(f"You are worker {name}", p)
            # Preamble may use dict(sender='name') or {'sender':'name'} format  # signed: beta
            self.assertTrue(
                f"sender='{name}'" in p or f"sender':'{name}'" in p,
                f"Neither sender='{name}' nor sender':'{name}' found in preamble for {name}"
            )
            # Lean preamble uses "for {name} ONLY", old used "for worker {name} ONLY"  # signed: beta
            self.assertTrue(
                f"for {name} ONLY" in p or f"for worker {name} ONLY" in p,
                f"Neither 'for {name} ONLY' nor 'for worker {name} ONLY' found in preamble"
            )
            # Must not contain another worker's identity claim
            for other in ["alpha", "beta", "gamma", "delta"]:
                if other != name:
                    self.assertNotIn(f"You are worker {other}", p)


class TestBuildContextPreamble(unittest.TestCase):
    """Validate context-enriched preamble construction."""

    def setUp(self):
        from tools.skynet_dispatch import build_context_preamble
        self.build_context_preamble = build_context_preamble

    def test_plain_task_without_context(self):
        result = self.build_context_preamble("alpha", "do the thing")
        self.assertIn("You are worker alpha", result)
        self.assertIn("do the thing", result)

    def test_context_with_learnings(self):
        ctx = {
            "relevant_learnings": [
                {"content": "Always run tests before deploying", "confidence": 0.9}
            ]
        }
        result = self.build_context_preamble("beta", "deploy code", ctx)
        self.assertIn("RELEVANT PAST LEARNINGS", result)

    def test_context_with_difficulty(self):
        ctx = {"difficulty": "MODERATE"}
        result = self.build_context_preamble("gamma", "refactor auth", ctx)
        self.assertIn("TASK COMPLEXITY: MODERATE", result)

    def test_strategy_id_included(self):
        ctx = {"strategy_id": "strat-42"}
        result = self.build_context_preamble("delta", "scan files", ctx)
        self.assertIn("strat-42", result)


class TestConveneGate(unittest.TestCase):
    """Validate ConveneGate propose/vote/elevate/reject/expire logic.

    Uses a temp file for gate state to avoid polluting real data.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gate_file = Path(self.tmpdir) / "convene_gate.json"
        self.todos_file = Path(self.tmpdir) / "todos.json"
        self.todos_file.write_text(json.dumps({"todos": [], "version": 1}), encoding="utf-8")
        # Patch GATE_FILE and BUS_PUBLISH to avoid real I/O
        self._patches = [
            patch("skynet_convene.GATE_FILE", self.gate_file),
            patch("skynet_convene.TODOS_FILE", self.todos_file),
            patch("tools.skynet_todos.TODOS_FILE", self.todos_file),
            patch("skynet_convene.requests.post", return_value=MagicMock(ok=True)),
        ]
        for p in self._patches:
            p.start()
        from skynet_convene import ConveneGate
        self.ConveneGate = ConveneGate

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_propose_creates_pending(self):
        gate = self.ConveneGate()
        r = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        self.assertEqual(r["action"], "proposed")
        self.assertIn("gate_id", r)
        self.assertEqual(r["votes"], 1)  # proposer auto-votes YES

    def test_urgent_bypasses_gate(self):
        gate = self.ConveneGate()
        r = gate.propose("alpha", "SYSTEM DOWN", urgent=True)
        self.assertEqual(r["action"], "bypassed")
        self.assertTrue(r["delivered"])

    def test_two_yes_votes_elevate(self):
        gate = self.ConveneGate()
        r1 = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gate_id = r1["gate_id"]
        # Second worker votes YES -> should elevate (threshold=2, proposer auto-voted)
        r2 = gate.vote_gate(gate_id, "beta", approve=True)
        self.assertEqual(r2["action"], "elevated")
        self.assertIn("alpha", r2["voters"])
        self.assertIn("beta", r2["voters"])

    def test_two_no_votes_reject(self):
        gate = self.ConveneGate()
        r1 = gate.propose("alpha", "Trivial rename suggestion in docs only, with no production value.")
        gate_id = r1["gate_id"]
        # Two workers vote NO -> should reject
        r2 = gate.vote_gate(gate_id, "beta", approve=False)
        # beta's NO + alpha's YES = 1 YES, 1 NO -- not enough to reject yet
        if r2["action"] != "rejected":
            r3 = gate.vote_gate(gate_id, "gamma", approve=False)
            self.assertEqual(r3["action"], "rejected")

    def test_vote_on_nonexistent_gate(self):
        gate = self.ConveneGate()
        r = gate.vote_gate("nonexistent_id", "alpha", approve=True)
        self.assertIn("error", r)

    def test_expire_stale_proposals(self):
        gate = self.ConveneGate()
        r = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gate_id = r["gate_id"]
        # Manually backdate the created_at timestamp
        gate._state["pending"][gate_id]["created_at"] = time.time() - 600
        gate._save()
        expired = gate.expire_stale(300)
        self.assertIn(gate_id, expired)
        self.assertEqual(len(gate.get_pending()), 0)

    def test_stats_tracking(self):
        gate = self.ConveneGate()
        gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gate.propose("beta", "CRITICAL worker crash", urgent=True)
        stats = gate.get_stats()
        self.assertEqual(stats["total_proposed"], 2)
        self.assertEqual(stats["total_bypassed"], 1)

    def test_proposer_cannot_double_vote(self):
        """Proposer's auto-YES should not be overridable by re-voting."""
        gate = self.ConveneGate()
        r = gate.propose("alpha", ARCH_BACKED_ENGINE_REPORT)
        gate_id = r["gate_id"]
        # Alpha tries to vote again -- their vote should just stay YES
        r2 = gate.vote_gate(gate_id, "alpha", approve=True)
        # Should still need 1 more vote, not elevate from self-voting twice
        self.assertNotEqual(r2["action"], "elevated")


class TestIdentityGuardIntegration(unittest.TestCase):
    """Validate that preamble text is correctly detected by the identity guard."""

    def test_preamble_blocked_by_guard(self):
        from tools.skynet_dispatch import build_preamble
        from tools.skynet_identity_guard import IdentityGuard

        guard = IdentityGuard("orchestrator")
        preamble = build_preamble("alpha")
        safe, reason = guard.validate(preamble)
        self.assertFalse(safe, "Orchestrator guard should block worker preamble")
        self.assertIn("preamble", reason.lower())

    def test_normal_task_allowed_by_guard(self):
        from tools.skynet_identity_guard import IdentityGuard

        guard = IdentityGuard("orchestrator")
        safe, reason = guard.validate("Audit all files in core/ and report findings")
        self.assertTrue(safe)

    def test_worker_guard_allows_own_preamble(self):
        """Workers should accept their own preamble (not blocked)."""
        from tools.skynet_dispatch import build_preamble
        from tools.skynet_identity_guard import IdentityGuard

        guard = IdentityGuard("alpha")  # worker-mode guard
        preamble = build_preamble("alpha")
        safe, reason = guard.validate(preamble)
        # Worker-mode guard blocks orchestrator commands, not worker preambles
        self.assertTrue(safe)


class TestSelfDispatchGuard(unittest.TestCase):
    """Validate that self-dispatch is blocked."""

    def test_self_identity_returns_env_var(self):
        from tools.skynet_dispatch import _get_self_identity
        with patch.dict(os.environ, {"SKYNET_WORKER_NAME": "delta"}):
            # Need to reload since it's cached at module level
            import importlib
            import tools.skynet_dispatch as sd
            old = sd._SELF_WORKER_NAME
            sd._SELF_WORKER_NAME = "delta"
            try:
                self.assertEqual(sd._get_self_identity(), "delta")
            finally:
                sd._SELF_WORKER_NAME = old


class TestProcessProtectionGuard(unittest.TestCase):
    """Validate the guard_process_kill safety check."""

    def test_guard_returns_true_for_unknown(self):
        from tools.skynet_dispatch import guard_process_kill
        # Unknown process should be safe to kill
        result = guard_process_kill(pid=99999, name="definitely_not_protected.exe", caller="test")
        self.assertTrue(result)


class TestGateMonitorScanLogic(unittest.TestCase):
    """Validate GateMonitor.scan_once filtering logic."""

    @patch("convene_gate.requests.get")
    @patch("convene_gate.requests.post", return_value=MagicMock(ok=True))
    @patch("skynet_convene.requests.post", return_value=MagicMock(ok=True))
    @patch("skynet_convene.GATE_FILE")
    def test_only_worker_to_orchestrator_intercepted(self, mock_gate_file, mock_conv_post, mock_post, mock_get):
        mock_gate_file.exists.return_value = False
        mock_gate_file.__class__ = Path
        tmpdir = tempfile.mkdtemp()
        gate_path = Path(tmpdir) / "convene_gate.json"
        with patch("skynet_convene.GATE_FILE", gate_path), \
             patch("skynet_convene.DATA", Path(tmpdir)):
            mock_get.return_value = MagicMock(
                ok=True,
                json=lambda: [
                    {"id": "1", "sender": "alpha", "topic": "orchestrator", "type": "result", "content": "found bug"},
                    {"id": "2", "sender": "system", "topic": "orchestrator", "type": "alert", "content": "sys alert"},
                    {"id": "3", "sender": "beta", "topic": "workers", "type": "request", "content": "need help"},
                    {"id": "4", "sender": "gamma", "topic": "orchestrator", "type": "urgent", "content": "EMERGENCY"},
                ]
            )
            from convene_gate import GateMonitor
            monitor = GateMonitor()
            actions = monitor.scan_once()
            action_types = [a["action"] for a in actions]
            self.assertIn("intercepted", action_types)  # alpha->orchestrator
            self.assertIn("bypass", action_types)  # gamma urgent
            # system->orchestrator should NOT be intercepted (not a worker)
            intercepted_senders = [a.get("sender") for a in actions if a["action"] == "intercepted"]
            self.assertNotIn("system", intercepted_senders)
            # beta->workers should NOT be intercepted (wrong topic)
            self.assertNotIn("beta", intercepted_senders)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
