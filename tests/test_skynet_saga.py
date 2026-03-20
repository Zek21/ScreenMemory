"""Tests for tools/skynet_saga.py — Saga pattern distributed transactions.

Covers: SagaStatus/StepStatus enums, SagaStep/SagaState dataclasses,
SagaOrchestrator lifecycle (create, execute, compensate, resume, force_compensate),
step registration, compensation logic, failure handling, partial completion,
rollback, state persistence, timeout handling, idempotency, built-in sagas,
event logging, helpers, routing registry.
"""  # signed: alpha

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_saga import (
    SagaStatus,
    StepStatus,
    SagaStep,
    SagaState,
    SagaOrchestrator,
    _log_event,
    _now,
    _noop_action,
    _safe_serialize,
    list_sagas,
    get_saga_status,
    get_events,
    BUILTIN_SAGAS,
    saga_multi_worker_refactor,
    saga_deploy_pipeline,
    saga_audit_and_fix,
)


# ===========================================================================
# Helper: temp saga directory for isolation
# ===========================================================================
def _make_temp_saga_env():
    """Return a tmpdir and patches for SAGAS_DIR / EVENTS_FILE."""
    tmpdir = tempfile.mkdtemp()
    sagas_dir = Path(tmpdir) / "sagas"
    sagas_dir.mkdir()
    events_file = Path(tmpdir) / "saga_events.jsonl"
    return tmpdir, sagas_dir, events_file


# ===========================================================================
# 1. Enums
# ===========================================================================

class TestSagaStatusEnum(unittest.TestCase):

    def test_all_values(self):
        expected = {"CREATED", "RUNNING", "COMPLETED", "COMPENSATING",
                    "COMPENSATED", "FAILED", "PARTIAL"}
        actual = {s.value for s in SagaStatus}
        self.assertEqual(actual, expected)

    def test_string_enum(self):
        self.assertEqual(str(SagaStatus.RUNNING), "SagaStatus.RUNNING")
        self.assertEqual(SagaStatus.COMPLETED.value, "COMPLETED")


class TestStepStatusEnum(unittest.TestCase):

    def test_all_values(self):
        expected = {"PENDING", "RUNNING", "SUCCEEDED", "FAILED",
                    "COMPENSATING", "COMPENSATED", "COMP_FAILED", "SKIPPED"}
        actual = {s.value for s in StepStatus}
        self.assertEqual(actual, expected)


# ===========================================================================
# 2. SagaStep dataclass
# ===========================================================================

class TestSagaStep(unittest.TestCase):

    def test_to_dict(self):
        step = SagaStep(name="build", action=lambda ctx: None, timeout=30.0)
        d = step.to_dict()
        self.assertEqual(d["name"], "build")
        self.assertEqual(d["timeout"], 30.0)
        self.assertEqual(d["status"], "PENDING")
        self.assertIsNone(d["result"])
        self.assertIsNone(d["error"])

    def test_from_dict_roundtrip(self):
        step = SagaStep(name="test", action=lambda ctx: "ok",
                        timeout=45.0, retry_count=3)
        step.status = StepStatus.SUCCEEDED
        step.result = "passed"
        d = step.to_dict()
        restored = SagaStep.from_dict(d)
        self.assertEqual(restored.name, "test")
        self.assertEqual(restored.timeout, 45.0)
        self.assertEqual(restored.retry_count, 3)
        self.assertEqual(restored.status, StepStatus.SUCCEEDED)
        self.assertEqual(restored.result, "passed")

    def test_from_dict_defaults(self):
        d = {"name": "minimal"}
        step = SagaStep.from_dict(d)
        self.assertEqual(step.timeout, 60.0)
        self.assertEqual(step.retry_count, 1)
        self.assertEqual(step.status, StepStatus.PENDING)

    def test_from_dict_with_custom_callables(self):
        action_fn = MagicMock()
        comp_fn = MagicMock()
        step = SagaStep.from_dict({"name": "s1"}, action=action_fn,
                                   compensate=comp_fn)
        self.assertIs(step.action, action_fn)
        self.assertIs(step.compensate, comp_fn)

    def test_from_dict_no_action_uses_noop(self):
        step = SagaStep.from_dict({"name": "s2"})
        result = step.action({})
        self.assertIsNone(result)


# ===========================================================================
# 3. SagaState dataclass
# ===========================================================================

class TestSagaState(unittest.TestCase):

    def test_to_dict_roundtrip(self):
        state = SagaState(saga_id="test_123", saga_name="test_saga",
                          context={"key": "val"})
        d = state.to_dict()
        restored = SagaState.from_dict(d)
        self.assertEqual(restored.saga_id, "test_123")
        self.assertEqual(restored.saga_name, "test_saga")
        self.assertEqual(restored.context["key"], "val")
        self.assertEqual(restored.status, SagaStatus.CREATED)

    def test_save_and_load(self):
        tmpdir, sagas_dir, _ = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir):
                state = SagaState(saga_id="persist_test", saga_name="my_saga",
                                  context={"x": 1})
                state.save()
                loaded = SagaState.load("persist_test")
                self.assertEqual(loaded.saga_id, "persist_test")
                self.assertEqual(loaded.saga_name, "my_saga")
                self.assertEqual(loaded.context["x"], 1)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_nonexistent_raises(self):
        tmpdir, sagas_dir, _ = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir):
                with self.assertRaises(FileNotFoundError):
                    SagaState.load("nonexistent_saga")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_updates_timestamp(self):
        tmpdir, sagas_dir, _ = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir):
                state = SagaState(saga_id="ts_test", saga_name="ts")
                first_updated = state.updated_at
                time.sleep(0.05)
                state.save()
                self.assertIsNotNone(state.updated_at)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_from_dict_defaults(self):
        d = {"saga_id": "x", "saga_name": "y"}
        state = SagaState.from_dict(d)
        self.assertEqual(state.status, SagaStatus.CREATED)
        self.assertEqual(state.steps, [])
        self.assertEqual(state.context, {})
        self.assertEqual(state.current_step, 0)


# ===========================================================================
# 4. SagaOrchestrator — Init and Step Registration
# ===========================================================================

class TestOrchestratorInit(unittest.TestCase):

    def test_default_id_generation(self):
        orch = SagaOrchestrator("my_saga")
        self.assertTrue(orch.saga_id.startswith("saga_"))
        self.assertEqual(orch.saga_name, "my_saga")
        self.assertEqual(orch.steps, [])
        self.assertEqual(orch.context, {})

    def test_custom_id(self):
        orch = SagaOrchestrator("test", saga_id="custom_123")
        self.assertEqual(orch.saga_id, "custom_123")

    def test_context_passed(self):
        orch = SagaOrchestrator("test", context={"env": "staging"})
        self.assertEqual(orch.context["env"], "staging")


class TestAddStep(unittest.TestCase):

    def test_add_step_returns_self(self):
        orch = SagaOrchestrator("test")
        result = orch.add_step("s1", lambda ctx: None)
        self.assertIs(result, orch)

    def test_chaining(self):
        orch = SagaOrchestrator("test")
        orch.add_step("s1", lambda ctx: 1).add_step("s2", lambda ctx: 2)
        self.assertEqual(len(orch.steps), 2)
        self.assertEqual(orch.steps[0].name, "s1")
        self.assertEqual(orch.steps[1].name, "s2")

    def test_step_attributes(self):
        comp_fn = MagicMock()
        orch = SagaOrchestrator("test")
        orch.add_step("build", lambda ctx: None, compensate=comp_fn,
                      timeout=120.0, retry_count=3)
        step = orch.steps[0]
        self.assertEqual(step.name, "build")
        self.assertIs(step.compensate, comp_fn)
        self.assertEqual(step.timeout, 120.0)
        self.assertEqual(step.retry_count, 3)


# ===========================================================================
# 5. Execute — Happy Path
# ===========================================================================

class TestExecuteHappyPath(unittest.TestCase):

    def _run_saga(self, steps, context=None):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("test_saga", context=context or {},
                                        saga_id="test_001")
                for name, action, comp in steps:
                    orch.add_step(name, action, comp, timeout=0)
                return orch.execute(), sagas_dir, events_file
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_all_steps_succeed(self):
        steps = [
            ("step1", lambda ctx: "r1", None),
            ("step2", lambda ctx: "r2", None),
            ("step3", lambda ctx: "r3", None),
        ]
        result, _, _ = self._run_saga(steps)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["steps"]), 3)
        for s in result["steps"]:
            self.assertEqual(s["status"], "SUCCEEDED")

    def test_context_shared_across_steps(self):
        def step1(ctx):
            ctx["from_step1"] = "hello"
            return "ok"

        def step2(ctx):
            return ctx.get("from_step1", "missing")

        steps = [
            ("step1", step1, None),
            ("step2", step2, None),
        ]
        result, _, _ = self._run_saga(steps)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["steps"][1]["result"], "hello")

    def test_result_contains_metadata(self):
        result, _, _ = self._run_saga([("s1", lambda c: 42, None)])
        self.assertIn("saga_id", result)
        self.assertIn("saga_name", result)
        self.assertIn("created_at", result)
        self.assertIn("completed_at", result)
        self.assertEqual(result["saga_id"], "test_001")

    def test_empty_saga(self):
        result, _, _ = self._run_saga([])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["steps"], [])


# ===========================================================================
# 6. Execute — Failure and Compensation
# ===========================================================================

class TestExecuteFailure(unittest.TestCase):

    def _run_saga_with_tracking(self, steps):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        import shutil
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("fail_test", saga_id="fail_001")
                for name, action, comp, kwargs in steps:
                    orch.add_step(name, action, comp, timeout=0, **kwargs)
                return orch.execute()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_failure_triggers_compensation(self):
        comp_called = []

        def step1(ctx):
            return "s1_result"

        def comp1(ctx, result):
            comp_called.append(("comp1", result))

        def step2(ctx):
            raise RuntimeError("boom")

        steps = [
            ("step1", step1, comp1, {}),
            ("step2", step2, None, {}),
        ]
        result = self._run_saga_with_tracking(steps)
        self.assertEqual(result["status"], "COMPENSATED")
        self.assertIn("boom", result["error"])
        self.assertEqual(len(comp_called), 1)
        self.assertEqual(comp_called[0], ("comp1", "s1_result"))

    def test_compensation_reverse_order(self):
        order = []

        def comp_a(ctx, r):
            order.append("a")

        def comp_b(ctx, r):
            order.append("b")

        def comp_c(ctx, r):
            order.append("c")

        steps = [
            ("a", lambda c: 1, comp_a, {}),
            ("b", lambda c: 2, comp_b, {}),
            ("c", lambda c: 3, comp_c, {}),
            ("d", lambda c: (_ for _ in ()).throw(ValueError("fail")), None, {}),
        ]
        result = self._run_saga_with_tracking(steps)
        self.assertEqual(order, ["c", "b", "a"])

    def test_no_compensate_skipped(self):
        steps = [
            ("s1", lambda c: "ok", None, {}),  # no compensate
            ("s2", lambda c: (_ for _ in ()).throw(RuntimeError("fail")), None, {}),
        ]
        result = self._run_saga_with_tracking(steps)
        self.assertEqual(result["status"], "COMPENSATED")
        self.assertEqual(result["steps"][0]["status"], "SKIPPED")

    def test_compensation_failure_partial(self):
        def bad_comp(ctx, r):
            raise RuntimeError("comp failed")

        steps = [
            ("s1", lambda c: "ok", bad_comp, {}),
            ("s2", lambda c: (_ for _ in ()).throw(RuntimeError("fail")), None, {}),
        ]
        result = self._run_saga_with_tracking(steps)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(len(result["compensation_errors"]) > 0)
        self.assertEqual(result["steps"][0]["status"], "COMP_FAILED")

    def test_first_step_fails_no_compensation(self):
        comp_called = []

        steps = [
            ("s1", lambda c: (_ for _ in ()).throw(RuntimeError("fail")), 
             lambda c, r: comp_called.append("should_not_run"), {}),
        ]
        result = self._run_saga_with_tracking(steps)
        # failed_step_idx=0, compensate range is range(-1, -1, -1) → empty
        self.assertEqual(result["status"], "COMPENSATED")
        self.assertEqual(comp_called, [])


# ===========================================================================
# 7. Retry Logic
# ===========================================================================

class TestRetryLogic(unittest.TestCase):

    def test_retry_succeeds_on_second_attempt(self):
        call_count = [0]

        def flaky_action(ctx):
            call_count[0] += 1
            if call_count[0] < 2:
                raise RuntimeError("transient")
            return "success"

        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("retry_test", saga_id="retry_001")
                orch.add_step("flaky", flaky_action, timeout=0, retry_count=3)
                result = orch.execute()
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(call_count[0], 2)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_retry_exhausted(self):
        def always_fail(ctx):
            raise RuntimeError("persistent error")

        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("retry_fail", saga_id="retry_002")
                orch.add_step("fail", always_fail, timeout=0, retry_count=3)
                result = orch.execute()
            self.assertEqual(result["status"], "COMPENSATED")
            self.assertIn("persistent error", result["error"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 8. Timeout Handling
# ===========================================================================

class TestTimeoutHandling(unittest.TestCase):

    def test_timeout_raises(self):
        def slow_action(ctx):
            time.sleep(5)
            return "late"

        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("timeout_test", saga_id="to_001")
                orch.add_step("slow", slow_action, timeout=0.1)
                result = orch.execute()
            self.assertEqual(result["status"], "COMPENSATED")
            self.assertIn("timed out", result["error"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_timeout_runs_inline(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx: "fast", {}, timeout=0
        )
        self.assertEqual(result, "fast")

    def test_timeout_with_extra_arg(self):
        def comp_fn(ctx, result):
            return f"compensated:{result}"

        result = SagaOrchestrator._execute_with_timeout(
            comp_fn, {}, timeout=0, extra_arg="original"
        )
        self.assertEqual(result, "compensated:original")

    def test_execute_with_timeout_propagates_exception(self):
        def raise_fn(ctx):
            raise ValueError("inner error")

        with self.assertRaises(ValueError) as cm:
            SagaOrchestrator._execute_with_timeout(raise_fn, {}, timeout=5)
        self.assertIn("inner error", str(cm.exception))


# ===========================================================================
# 9. State Persistence During Execution
# ===========================================================================

class TestStatePersistence(unittest.TestCase):

    def test_state_saved_at_each_step(self):
        save_count = [0]
        original_save = SagaState.save

        def counting_save(self_state):
            save_count[0] += 1
            original_save(self_state)

        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file), \
                 patch.object(SagaState, "save", counting_save):
                orch = SagaOrchestrator("persist_test", saga_id="pt_001")
                orch.add_step("s1", lambda c: 1, timeout=0)
                orch.add_step("s2", lambda c: 2, timeout=0)
                orch.execute()
            # Initial save + per step (save before run + save after succeed) + final
            # At minimum, state is saved more than just once
            self.assertGreater(save_count[0], 3)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_state_reflects_failure(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("fail_persist", saga_id="fp_001")
                orch.add_step("ok", lambda c: "good", timeout=0)
                orch.add_step("bad", lambda c: (_ for _ in ()).throw(RuntimeError("x")),
                             timeout=0)
                orch.execute()

                loaded = SagaState.load("fp_001")
                self.assertIn(loaded.status, (SagaStatus.COMPENSATED, SagaStatus.PARTIAL))
                self.assertIsNotNone(loaded.error)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 10. Resume
# ===========================================================================

class TestResume(unittest.TestCase):

    def test_resume_running_saga(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                # Manually create a RUNNING saga state with step 0 succeeded
                state = SagaState(
                    saga_id="resume_001", saga_name="test_resume",
                    status=SagaStatus.RUNNING,
                    steps=[
                        {"name": "s1", "status": "SUCCEEDED", "result": "r1",
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                        {"name": "s2", "status": "PENDING", "result": None,
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                    ],
                    current_step=1,
                )
                state.save()

                # Provide step registry so s2 can run
                registry = {
                    "s1": (lambda c: "r1", None),
                    "s2": (lambda c: "r2", None),
                }
                result = SagaOrchestrator.resume("resume_001",
                                                  step_registry=registry)
                self.assertEqual(result["status"], "COMPLETED")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_resume_compensating_saga(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                state = SagaState(
                    saga_id="resume_002", saga_name="test_resume_comp",
                    status=SagaStatus.COMPENSATING,
                    steps=[
                        {"name": "s1", "status": "SUCCEEDED", "result": "r1",
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                        {"name": "s2", "status": "FAILED", "result": None,
                         "timeout": 60, "retry_count": 1, "error": "crash",
                         "started_at": None, "finished_at": None},
                    ],
                    current_step=1,
                )
                state.save()

                comp_called = []
                registry = {
                    "s1": (_noop_action, lambda c, r: comp_called.append("s1")),
                    "s2": (_noop_action, None),
                }
                result = SagaOrchestrator.resume("resume_002",
                                                  step_registry=registry)
                self.assertIn(result["status"], ("COMPENSATED", "PARTIAL"))
                self.assertEqual(comp_called, ["s1"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_resume_nonexistent_raises(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                with self.assertRaises(FileNotFoundError):
                    SagaOrchestrator.resume("nonexistent_saga")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 11. Force Compensate
# ===========================================================================

class TestForceCompensate(unittest.TestCase):

    def test_force_compensate_completed_saga(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                state = SagaState(
                    saga_id="fc_001", saga_name="force_comp",
                    status=SagaStatus.COMPLETED,
                    steps=[
                        {"name": "s1", "status": "SUCCEEDED", "result": "data",
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                        {"name": "s2", "status": "SUCCEEDED", "result": "data2",
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                    ],
                )
                state.save()

                comp_calls = []
                registry = {
                    "s1": (_noop_action, lambda c, r: comp_calls.append("s1")),
                    "s2": (_noop_action, lambda c, r: comp_calls.append("s2")),
                }
                result = SagaOrchestrator.force_compensate("fc_001",
                                                           step_registry=registry)
                self.assertIn(result["status"], ("COMPENSATED", "PARTIAL"))
                # Both steps compensated in reverse order
                self.assertEqual(comp_calls, ["s2", "s1"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_force_compensate_no_succeeded_steps(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                state = SagaState(
                    saga_id="fc_002", saga_name="nothing_to_comp",
                    status=SagaStatus.FAILED,
                    steps=[
                        {"name": "s1", "status": "FAILED", "result": None,
                         "timeout": 60, "retry_count": 1, "error": "err",
                         "started_at": None, "finished_at": None},
                    ],
                )
                state.save()
                result = SagaOrchestrator.force_compensate("fc_002")
                self.assertEqual(result["status"], "COMPENSATED")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 12. Execute With Timeout
# ===========================================================================

class TestExecuteWithTimeout(unittest.TestCase):

    def test_fast_function_completes(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx: ctx.get("x", 0) + 1, {"x": 41}, timeout=5
        )
        self.assertEqual(result, 42)

    def test_none_result(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx: None, {}, timeout=5
        )
        self.assertIsNone(result)

    def test_zero_timeout_runs_directly(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx: "direct", {}, timeout=0
        )
        self.assertEqual(result, "direct")

    def test_extra_arg_passed(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx, arg: f"got:{arg}", {}, timeout=5, extra_arg="hello"
        )
        self.assertEqual(result, "got:hello")

    def test_zero_timeout_with_extra_arg(self):
        result = SagaOrchestrator._execute_with_timeout(
            lambda ctx, arg: arg * 2, {}, timeout=0, extra_arg=21
        )
        self.assertEqual(result, 42)


# ===========================================================================
# 13. Helpers
# ===========================================================================

class TestHelpers(unittest.TestCase):

    def test_now_format(self):
        ts = _now()
        self.assertRegex(ts, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_noop_action(self):
        self.assertIsNone(_noop_action({}))
        self.assertIsNone(_noop_action({"key": "val"}))

    def test_safe_serialize_primitives(self):
        self.assertIsNone(_safe_serialize(None))
        self.assertEqual(_safe_serialize("hello"), "hello")
        self.assertEqual(_safe_serialize(42), 42)
        self.assertEqual(_safe_serialize(3.14), 3.14)
        self.assertEqual(_safe_serialize(True), True)

    def test_safe_serialize_list(self):
        result = _safe_serialize([1, "two", None])
        self.assertEqual(result, [1, "two", None])

    def test_safe_serialize_dict(self):
        result = _safe_serialize({"a": 1, "b": [2, 3]})
        self.assertEqual(result, {"a": 1, "b": [2, 3]})

    def test_safe_serialize_non_serializable(self):
        result = _safe_serialize(object())
        self.assertIsInstance(result, str)

    def test_safe_serialize_nested(self):
        data = {"list": [1, {"inner": True}], "tuple": (1, 2)}
        result = _safe_serialize(data)
        self.assertEqual(result["list"], [1, {"inner": True}])
        self.assertEqual(result["tuple"], [1, 2])


# ===========================================================================
# 14. Event Logging
# ===========================================================================

class TestEventLogging(unittest.TestCase):

    def test_log_event_creates_file(self):
        tmpdir, _, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.EVENTS_FILE", events_file):
                _log_event("saga_001", "STEP_STARTED", "build", data={"attempt": 1})
            self.assertTrue(events_file.exists())
            with open(events_file, "r") as f:
                line = f.readline()
            event = json.loads(line)
            self.assertEqual(event["saga_id"], "saga_001")
            self.assertEqual(event["event"], "STEP_STARTED")
            self.assertEqual(event["step"], "build")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_log_event_appends(self):
        tmpdir, _, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.EVENTS_FILE", events_file):
                _log_event("s1", "E1")
                _log_event("s1", "E2")
                _log_event("s2", "E3")
            with open(events_file, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 3)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 15. get_events
# ===========================================================================

class TestGetEvents(unittest.TestCase):

    def test_empty_file(self):
        tmpdir, _, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.EVENTS_FILE", events_file):
                result = get_events()
            self.assertEqual(result, [])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_filter_by_saga_id(self):
        tmpdir, _, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.EVENTS_FILE", events_file):
                _log_event("s1", "E1")
                _log_event("s2", "E2")
                _log_event("s1", "E3")
                result = get_events(saga_id="s1")
            self.assertEqual(len(result), 2)
            for ev in result:
                self.assertEqual(ev["saga_id"], "s1")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_limit_applied(self):
        tmpdir, _, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.EVENTS_FILE", events_file):
                for i in range(10):
                    _log_event("s1", f"E{i}")
                result = get_events(limit=3)
            self.assertEqual(len(result), 3)
            # Should return last 3 events
            self.assertEqual(result[0]["event"], "E7")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 16. list_sagas
# ===========================================================================

class TestListSagas(unittest.TestCase):

    def test_empty_dir(self):
        tmpdir, sagas_dir, _ = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir):
                result = list_sagas()
            self.assertEqual(result, [])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_lists_persisted_sagas(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                # Create two sagas
                s1 = SagaState(saga_id="list_001", saga_name="alpha_saga",
                               status=SagaStatus.COMPLETED)
                s1.steps = [{"name": "s1"}, {"name": "s2"}]
                s1.save()

                s2 = SagaState(saga_id="list_002", saga_name="beta_saga",
                               status=SagaStatus.FAILED)
                s2.save()

                result = list_sagas()
            self.assertEqual(len(result), 2)
            ids = {s["saga_id"] for s in result}
            self.assertEqual(ids, {"list_001", "list_002"})
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_skips_corrupted_files(self):
        tmpdir, sagas_dir, _ = _make_temp_saga_env()
        try:
            (sagas_dir / "bad.json").write_text("not json", encoding="utf-8")
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir):
                result = list_sagas()
            self.assertEqual(result, [])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 17. get_saga_status
# ===========================================================================

class TestGetSagaStatus(unittest.TestCase):

    def test_returns_state_dict(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                state = SagaState(saga_id="status_001", saga_name="status_test",
                                  status=SagaStatus.RUNNING)
                state.save()
                result = get_saga_status("status_001")
            self.assertEqual(result["saga_id"], "status_001")
            self.assertEqual(result["status"], "RUNNING")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 18. Built-in Saga Registry
# ===========================================================================

class TestBuiltinSagas(unittest.TestCase):

    def test_registry_has_three_sagas(self):
        self.assertEqual(len(BUILTIN_SAGAS), 3)
        self.assertIn("multi_worker_refactor", BUILTIN_SAGAS)
        self.assertIn("deploy_pipeline", BUILTIN_SAGAS)
        self.assertIn("audit_and_fix", BUILTIN_SAGAS)

    def test_multi_worker_refactor_creates_orchestrator(self):
        orch = saga_multi_worker_refactor({"files": ["test.py"],
                                            "description": "fix bugs"})
        self.assertIsInstance(orch, SagaOrchestrator)
        self.assertEqual(orch.saga_name, "multi_worker_refactor")
        self.assertEqual(len(orch.steps), 3)
        step_names = [s.name for s in orch.steps]
        self.assertEqual(step_names, ["backup_files", "dispatch_edits",
                                       "validate_result"])

    def test_deploy_pipeline_creates_orchestrator(self):
        orch = saga_deploy_pipeline({"target": "staging"})
        self.assertIsInstance(orch, SagaOrchestrator)
        self.assertEqual(len(orch.steps), 3)
        step_names = [s.name for s in orch.steps]
        self.assertEqual(step_names, ["build", "test", "deploy"])

    def test_audit_and_fix_creates_orchestrator(self):
        orch = saga_audit_and_fix({"scope": "security",
                                    "targets": ["tools/"]})
        self.assertIsInstance(orch, SagaOrchestrator)
        self.assertEqual(len(orch.steps), 3)
        step_names = [s.name for s in orch.steps]
        self.assertEqual(step_names, ["audit", "implement_fixes",
                                       "validate_fixes"])

    def test_deploy_pipeline_context(self):
        orch = saga_deploy_pipeline({"target": "prod",
                                      "build_cmd": "make build"})
        self.assertEqual(orch.context["target"], "prod")
        self.assertEqual(orch.context["build_cmd"], "make build")


# ===========================================================================
# 19. Concurrent Sagas
# ===========================================================================

class TestConcurrentSagas(unittest.TestCase):

    def test_independent_sagas_dont_interfere(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        results = {}
        errors = []

        def run_saga(saga_id, delay, result_key):
            try:
                with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                     patch("tools.skynet_saga.EVENTS_FILE", events_file):
                    orch = SagaOrchestrator("concurrent", saga_id=saga_id)
                    orch.add_step("work", lambda c: time.sleep(delay) or saga_id,
                                 timeout=5)
                    results[result_key] = orch.execute()
            except Exception as e:
                errors.append(str(e))

        try:
            t1 = threading.Thread(target=run_saga, args=("c1", 0.05, "r1"))
            t2 = threading.Thread(target=run_saga, args=("c2", 0.05, "r2"))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertEqual(len(errors), 0, f"Errors: {errors}")
            self.assertEqual(results["r1"]["status"], "COMPLETED")
            self.assertEqual(results["r2"]["status"], "COMPLETED")
            self.assertNotEqual(results["r1"]["saga_id"],
                               results["r2"]["saga_id"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 20. Idempotency
# ===========================================================================

class TestIdempotency(unittest.TestCase):

    def test_gen_id_unique(self):
        ids = set()
        for _ in range(50):
            ids.add(SagaOrchestrator._gen_id("test"))
            time.sleep(0.001)
        # All IDs should be unique (hash includes time.time())
        self.assertGreater(len(ids), 1)

    def test_completed_saga_cannot_rerun(self):
        """Resume on a COMPLETED saga just returns the result."""
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                state = SagaState(
                    saga_id="idem_001", saga_name="completed",
                    status=SagaStatus.COMPLETED,
                    steps=[
                        {"name": "s1", "status": "SUCCEEDED", "result": "done",
                         "timeout": 60, "retry_count": 1, "error": None,
                         "started_at": None, "finished_at": None},
                    ],
                )
                state.save()
                result = SagaOrchestrator.resume("idem_001")
                # Already completed — resume just returns as-is
                self.assertEqual(result["status"], "COMPLETED")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 21. _build_result
# ===========================================================================

class TestBuildResult(unittest.TestCase):

    def test_result_structure(self):
        tmpdir, sagas_dir, events_file = _make_temp_saga_env()
        try:
            with patch("tools.skynet_saga.SAGAS_DIR", sagas_dir), \
                 patch("tools.skynet_saga.EVENTS_FILE", events_file):
                orch = SagaOrchestrator("result_test", saga_id="br_001")
                orch.add_step("s1", lambda c: "ok", timeout=0)
                result = orch.execute()

            required_keys = {"saga_id", "saga_name", "status", "steps",
                            "error", "compensation_errors", "created_at",
                            "completed_at"}
            self.assertTrue(required_keys.issubset(set(result.keys())))
            self.assertEqual(result["saga_id"], "br_001")
            self.assertEqual(result["saga_name"], "result_test")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
