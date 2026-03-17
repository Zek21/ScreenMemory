#!/usr/bin/env python3
"""
skynet_post_task.py — Complete post-task lifecycle for Skynet workers.

Replaces ad-hoc bus posting with the full 7-phase intelligence protocol
that every worker MUST execute after completing ANY task.

Phases:
  1. Report result to bus via guarded_publish
  2. Knowledge capture via broadcast_learning + LearningStore
  3. Cognitive reflection via ReflexionEngine (failure analysis or success recording)
  4. Strategy evolution via SelfEvolutionSystem
  5. Collective sync via sync_strategies + absorb_bottlenecks
  6. Score check via get_score
  7. TODO check via can_stop / pending_count (zero-stop law)

Usage:
  # As library (primary usage):
  from tools.skynet_post_task import execute_post_task_lifecycle
  next_task = execute_post_task_lifecycle("alpha", "fix bug in X", "Fixed: added null check", success=True)

  # CLI:
  python tools/skynet_post_task.py --worker alpha --task "fix bug" --result "Fixed it" --success
  python tools/skynet_post_task.py --worker alpha --task "fix bug" --result "Failed" --failed
  python tools/skynet_post_task.py --test

# signed: beta
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_FILE = PROJECT_ROOT / "data" / "post_task_log.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("skynet_post_task")


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class PhaseResult:
    """Result of a single lifecycle phase."""
    phase: int
    name: str
    success: bool
    elapsed_ms: float
    detail: Any = None
    error: str = ""


@dataclass
class LifecycleResult:
    """Aggregate result of the full post-task lifecycle."""
    worker: str
    task: str
    task_success: bool
    phases: List[PhaseResult] = field(default_factory=list)
    total_elapsed_ms: float = 0.0
    next_task: Optional[str] = None  # non-None means worker should continue
    all_phases_ok: bool = False

    def summary(self) -> str:
        ok = sum(1 for p in self.phases if p.success)
        total = len(self.phases)
        status = "ALL OK" if self.all_phases_ok else f"{ok}/{total} OK"
        lines = [f"Post-task lifecycle [{self.worker}]: {status} ({self.total_elapsed_ms:.0f}ms)"]
        for p in self.phases:
            mark = "✓" if p.success else "✗"
            err = f" — {p.error}" if p.error else ""
            det = ""
            if p.detail and not p.error:
                det = f" — {p.detail}" if isinstance(p.detail, str) else ""
            lines.append(f"  {mark} Phase {p.phase}: {p.name} ({p.elapsed_ms:.0f}ms){err}{det}")
        if self.next_task:
            lines.append(f"  → Next task pending: {self.next_task}")
        return "\n".join(lines)


# ── Phase Implementations ────────────────────────────────────────

def _phase_1_report_result(worker: str, result_summary: str, success: bool) -> PhaseResult:
    """Phase 1: Report result to bus via guarded_publish."""  # signed: beta
    t0 = time.perf_counter()
    try:
        from tools.skynet_spam_guard import guarded_publish

        msg = {
            "sender": worker,
            "topic": "orchestrator",
            "type": "result",
            "content": f"{'DONE' if success else 'FAILED'}: {result_summary} signed:{worker}",
        }
        pub_result = guarded_publish(msg)
        allowed = pub_result.get("allowed", False)
        published = pub_result.get("published", False)
        reason = pub_result.get("reason", "")

        if allowed and published:
            detail = "published to bus"
        elif allowed and not published:
            detail = f"allowed but publish failed: {reason}"
        else:
            detail = f"blocked by spam guard: {reason}"

        return PhaseResult(
            phase=1, name="Report Result", success=allowed,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            detail=detail,
        )
    except Exception as e:
        return PhaseResult(
            phase=1, name="Report Result", success=False,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


def _phase_2_knowledge_capture(worker: str, task: str, result_summary: str,
                                success: bool) -> PhaseResult:
    """Phase 2: Capture knowledge via broadcast_learning + LearningStore."""  # signed: beta
    t0 = time.perf_counter()
    details = []

    # Step 1: Broadcast learning to bus
    try:
        from tools.skynet_knowledge import broadcast_learning

        category = "optimization" if success else "bug"
        fact = f"Task '{task}': {'succeeded' if success else 'failed'} — {result_summary}"
        tags = [worker, "post-task", "success" if success else "failure"]
        broadcast_ok = broadcast_learning(worker, fact, category, tags)
        details.append(f"broadcast={'ok' if broadcast_ok else 'failed'}")
    except Exception as e:
        details.append(f"broadcast=error({type(e).__name__})")
        log.warning("Phase 2 broadcast_learning failed: %s", e)

    # Step 2: Persist to LearningStore for cross-session retrieval
    try:
        from core.learning_store import LearningStore

        store = LearningStore()
        content = f"[{worker}] Task: {task} | Outcome: {'success' if success else 'failure'} | {result_summary}"
        fact_id = store.learn(
            content=content,
            category=category if 'category' in dir() else "pattern",
            source=worker,
            tags=[worker, "post-task"],
        )
        details.append(f"stored=fact_{fact_id[:8]}")
    except Exception as e:
        details.append(f"store=error({type(e).__name__})")
        log.warning("Phase 2 LearningStore failed: %s", e)

    return PhaseResult(
        phase=2, name="Knowledge Capture", success=True,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        detail=", ".join(details),
    )


def _phase_3_cognitive_reflection(worker: str, task: str, result_summary: str,
                                   success: bool) -> PhaseResult:
    """Phase 3: Cognitive reflection via ReflexionEngine."""  # signed: beta
    t0 = time.perf_counter()
    details = []

    try:
        from core.cognitive.reflexion import ReflexionEngine, FailureContext

        # Initialize with LearningStore for cross-session persistence
        learning_store = None
        try:
            from core.learning_store import LearningStore
            learning_store = LearningStore()
        except Exception:
            pass

        engine = ReflexionEngine(learning_store=learning_store)

        if not success:
            # On failure: full reflection cycle
            failure = FailureContext(
                action_type="task_execution",
                action_target=task,
                action_value=result_summary,
                expected_outcome="successful completion",
                actual_outcome=result_summary,
                error_message=result_summary,
                error_type="task_failure",
            )
            reflection = engine.on_failure(failure)
            details.append(f"reflection={reflection.id[:8]}")
            details.append(f"lesson='{reflection.lesson[:60]}'" if reflection.lesson else "no_lesson")
            details.append(f"confidence={reflection.confidence:.2f}")
        else:
            # On success: retrieve relevant past reflections to reinforce
            past = engine.get_relevant_reflections(
                action_type="task_execution",
                target=task,
                context=result_summary,
                limit=3,
            )
            details.append(f"past_reflections={len(past)}")
            if past:
                # Reinforce past reflections that predicted this success pattern
                for r in past:
                    r.applied_count += 1
                    r.confidence = min(1.0, r.confidence + 0.05)
                details.append("reinforced")

    except Exception as e:
        details.append(f"error({type(e).__name__}: {e})")
        log.warning("Phase 3 ReflexionEngine failed: %s", e)

    return PhaseResult(
        phase=3, name="Cognitive Reflection", success=True,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        detail=", ".join(details) if details else "no reflection",
    )


def _phase_4_strategy_evolution(worker: str, task: str, success: bool) -> PhaseResult:
    """Phase 4: Evolve strategies via SelfEvolutionSystem."""  # signed: beta
    t0 = time.perf_counter()
    details = []

    try:
        from core.self_evolution import SelfEvolutionSystem

        evo = SelfEvolutionSystem()

        # Record task outcome for fitness tracking
        try:
            task_result = {
                "task_type": "code",
                "success": success,
                "description": task,
                "worker": worker,
                "timestamp": time.time(),
            }
            fitness = evo.record_task(task_result)
            details.append(f"fitness={fitness:.3f}" if isinstance(fitness, float) else f"recorded")
        except Exception as e:
            details.append(f"record=error({type(e).__name__})")

        # Evolve the code strategy population
        try:
            evo.engine.evolve_generation("code")
            details.append("evolved=code")
        except Exception as e:
            details.append(f"evolve=error({type(e).__name__})")

    except Exception as e:
        details.append(f"error({type(e).__name__}: {e})")
        log.warning("Phase 4 SelfEvolutionSystem failed: %s", e)

    return PhaseResult(
        phase=4, name="Strategy Evolution", success=True,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        detail=", ".join(details) if details else "skipped",
    )


def _phase_5_collective_sync(worker: str) -> PhaseResult:
    """Phase 5: Sync strategies and absorb bottlenecks from peers."""  # signed: beta
    t0 = time.perf_counter()
    details = []

    # Step 1: Sync strategies
    try:
        from tools.skynet_collective import sync_strategies
        result = sync_strategies(worker)
        broadcast = result.get("broadcast", 0)
        merged = result.get("merged", 0)
        details.append(f"sync(broadcast={broadcast},merged={merged})")
    except Exception as e:
        details.append(f"sync=error({type(e).__name__})")
        log.warning("Phase 5 sync_strategies failed: %s", e)

    # Step 2: Absorb bottlenecks from peers
    try:
        from tools.skynet_collective import absorb_bottlenecks
        result = absorb_bottlenecks(worker)
        weak = result.get("weak_categories_found", [])
        evolved = result.get("evolved", [])
        details.append(f"absorb(weak={len(weak)},evolved={len(evolved)})")
    except Exception as e:
        details.append(f"absorb=error({type(e).__name__})")
        log.warning("Phase 5 absorb_bottlenecks failed: %s", e)

    return PhaseResult(
        phase=5, name="Collective Sync", success=True,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        detail=", ".join(details) if details else "skipped",
    )


def _phase_6_score_check(worker: str) -> PhaseResult:
    """Phase 6: Check current score and trajectory."""  # signed: beta
    t0 = time.perf_counter()
    try:
        from tools.skynet_scoring import get_score

        score_data = get_score(worker)
        if score_data is None:
            return PhaseResult(
                phase=6, name="Score Check", success=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                detail="no score entry",
            )

        total = score_data.get("total", 0.0)
        awards = score_data.get("awards", 0)
        deductions = score_data.get("deductions", 0)
        detail = f"total={total:+.3f} (awards={awards}, deductions={deductions})"

        if total < 0:
            log.warning("Score for %s is NEGATIVE (%.3f) — recovery tasks needed", worker, total)
            detail += " ⚠ NEGATIVE"

        return PhaseResult(
            phase=6, name="Score Check", success=True,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            detail=detail,
        )
    except Exception as e:
        return PhaseResult(
            phase=6, name="Score Check", success=True,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


def _phase_7_todo_check(worker: str) -> PhaseResult:
    """Phase 7: Zero-stop law enforcement — check if worker can go idle."""  # signed: beta
    t0 = time.perf_counter()
    next_task = None

    try:
        from tools.skynet_todos import can_stop, pending_count

        pending = pending_count(worker, include_claimable=True)
        stop_ok = can_stop(worker, include_claimable=True)

        if stop_ok:
            detail = f"can_stop=True (pending={pending})"
        else:
            detail = f"can_stop=False (pending={pending}) — MUST CONTINUE"
            next_task = f"{pending} pending TODO(s) for {worker}"
            log.info("Zero-stop law: %s has %d pending items — cannot idle", worker, pending)

        return PhaseResult(
            phase=7, name="TODO Check", success=True,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            detail=detail,
        ), next_task

    except Exception as e:
        return PhaseResult(
            phase=7, name="TODO Check", success=True,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        ), None


# ── Main Lifecycle Entry Point ───────────────────────────────────

def execute_post_task_lifecycle(
    worker_name: str,
    task_description: str,
    result_summary: str,
    success: bool = True,
    skip_phases: Optional[List[int]] = None,
) -> Optional[str]:
    """Execute the full 7-phase post-task lifecycle.

    Args:
        worker_name: Worker identifier (e.g. "alpha", "beta")
        task_description: Brief description of the completed task
        result_summary: Summary of what was accomplished (or what failed)
        success: Whether the task succeeded
        skip_phases: Optional list of phase numbers to skip (1-7)

    Returns:
        None if worker can safely idle, or a string describing pending
        work that requires continued execution (zero-stop law).
    """  # signed: beta
    t_start = time.perf_counter()
    skip = set(skip_phases or [])
    lifecycle = LifecycleResult(
        worker=worker_name,
        task=task_description,
        task_success=success,
    )

    log.info("Starting post-task lifecycle for %s (task=%s, success=%s)",
             worker_name, task_description[:50], success)

    # Phase 1: Report result to bus
    if 1 not in skip:
        r = _phase_1_report_result(worker_name, result_summary, success)
        lifecycle.phases.append(r)
        log.info("Phase 1 [Report Result]: %s (%.0fms)", "OK" if r.success else "FAIL", r.elapsed_ms)

    # Phase 2: Knowledge capture
    if 2 not in skip:
        r = _phase_2_knowledge_capture(worker_name, task_description, result_summary, success)
        lifecycle.phases.append(r)
        log.info("Phase 2 [Knowledge Capture]: %s", r.detail)

    # Phase 3: Cognitive reflection
    if 3 not in skip:
        r = _phase_3_cognitive_reflection(worker_name, task_description, result_summary, success)
        lifecycle.phases.append(r)
        log.info("Phase 3 [Cognitive Reflection]: %s", r.detail)

    # Phase 4: Strategy evolution
    if 4 not in skip:
        r = _phase_4_strategy_evolution(worker_name, task_description, success)
        lifecycle.phases.append(r)
        log.info("Phase 4 [Strategy Evolution]: %s", r.detail)

    # Phase 5: Collective sync
    if 5 not in skip:
        r = _phase_5_collective_sync(worker_name)
        lifecycle.phases.append(r)
        log.info("Phase 5 [Collective Sync]: %s", r.detail)

    # Phase 6: Score check
    if 6 not in skip:
        r = _phase_6_score_check(worker_name)
        lifecycle.phases.append(r)
        log.info("Phase 6 [Score Check]: %s", r.detail or r.error)

    # Phase 7: TODO check (zero-stop law)
    if 7 not in skip:
        r, next_task = _phase_7_todo_check(worker_name)
        lifecycle.phases.append(r)
        lifecycle.next_task = next_task
        log.info("Phase 7 [TODO Check]: %s", r.detail or r.error)

    # Finalize
    lifecycle.total_elapsed_ms = (time.perf_counter() - t_start) * 1000
    lifecycle.all_phases_ok = all(p.success for p in lifecycle.phases)

    # Persist lifecycle log
    _persist_log(lifecycle)

    log.info(lifecycle.summary())
    return lifecycle.next_task


def _persist_log(lifecycle: LifecycleResult) -> None:
    """Append lifecycle result to the persistent log file."""  # signed: beta
    try:
        entry = {
            "worker": lifecycle.worker,
            "task": lifecycle.task,
            "task_success": lifecycle.task_success,
            "all_phases_ok": lifecycle.all_phases_ok,
            "total_elapsed_ms": round(lifecycle.total_elapsed_ms, 1),
            "next_task": lifecycle.next_task,
            "timestamp": time.time(),
            "phases": [
                {
                    "phase": p.phase,
                    "name": p.name,
                    "success": p.success,
                    "elapsed_ms": round(p.elapsed_ms, 1),
                    "detail": str(p.detail) if p.detail else None,
                    "error": p.error or None,
                }
                for p in lifecycle.phases
            ],
        }

        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Read existing log (array of entries)
        entries = []
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                if not isinstance(entries, list):
                    entries = []
            except (json.JSONDecodeError, ValueError):
                entries = []

        # Keep last 500 entries to prevent unbounded growth
        entries.append(entry)
        if len(entries) > 500:
            entries = entries[-500:]

        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

    except Exception as e:
        log.warning("Failed to persist lifecycle log: %s", e)


# ── CLI ──────────────────────────────────────────────────────────

def _run_self_test() -> bool:
    """Run internal self-tests to verify all phases initialize correctly."""  # signed: beta
    print("=" * 60)
    print("skynet_post_task.py — Self-Test Suite")
    print("=" * 60)

    passed = 0
    failed = 0
    tests = []

    # Test 1: PhaseResult creation
    def test_phase_result():
        r = PhaseResult(phase=1, name="Test", success=True, elapsed_ms=1.5, detail="ok")
        assert r.phase == 1
        assert r.success is True
        assert r.elapsed_ms == 1.5
    tests.append(("PhaseResult creation", test_phase_result))

    # Test 2: LifecycleResult summary
    def test_lifecycle_summary():
        lr = LifecycleResult(worker="test", task="test task", task_success=True)
        lr.phases.append(PhaseResult(1, "R", True, 1.0))
        lr.phases.append(PhaseResult(2, "K", False, 2.0, error="err"))
        lr.all_phases_ok = False
        lr.total_elapsed_ms = 3.0
        s = lr.summary()
        assert "1/2 OK" in s
        assert "err" in s
    tests.append(("LifecycleResult summary", test_lifecycle_summary))

    # Test 3: guarded_publish import
    def test_spam_guard_import():
        from tools.skynet_spam_guard import guarded_publish
        assert callable(guarded_publish)
    tests.append(("guarded_publish import", test_spam_guard_import))

    # Test 4: broadcast_learning import
    def test_knowledge_import():
        from tools.skynet_knowledge import broadcast_learning
        assert callable(broadcast_learning)
    tests.append(("broadcast_learning import", test_knowledge_import))

    # Test 5: ReflexionEngine import
    def test_reflexion_import():
        from core.cognitive.reflexion import ReflexionEngine, FailureContext
        assert callable(ReflexionEngine)
        assert callable(FailureContext)
    tests.append(("ReflexionEngine import", test_reflexion_import))

    # Test 6: SelfEvolutionSystem import
    def test_evolution_import():
        from core.self_evolution import SelfEvolutionSystem
        assert callable(SelfEvolutionSystem)
    tests.append(("SelfEvolutionSystem import", test_evolution_import))

    # Test 7: sync_strategies import
    def test_collective_import():
        from tools.skynet_collective import sync_strategies, absorb_bottlenecks
        assert callable(sync_strategies)
        assert callable(absorb_bottlenecks)
    tests.append(("collective import", test_collective_import))

    # Test 8: get_score import
    def test_scoring_import():
        from tools.skynet_scoring import get_score
        assert callable(get_score)
    tests.append(("get_score import", test_scoring_import))

    # Test 9: can_stop import
    def test_todos_import():
        from tools.skynet_todos import can_stop, pending_count
        assert callable(can_stop)
        assert callable(pending_count)
    tests.append(("can_stop/pending_count import", test_todos_import))

    # Test 10: LearningStore import
    def test_learning_store_import():
        from core.learning_store import LearningStore
        assert callable(LearningStore)
    tests.append(("LearningStore import", test_learning_store_import))

    # Test 11: Phase 1 executes (may fail on network but shouldn't crash)
    def test_phase_1():
        r = _phase_1_report_result("test_worker", "test result", True)
        assert isinstance(r, PhaseResult)
        assert r.phase == 1
        assert r.name == "Report Result"
    tests.append(("Phase 1 execution", test_phase_1))

    # Test 12: Phase 6 score check doesn't crash
    def test_phase_6():
        r = _phase_6_score_check("nonexistent_test_worker")
        assert isinstance(r, PhaseResult)
        assert r.phase == 6
    tests.append(("Phase 6 execution", test_phase_6))

    # Test 13: Phase 7 todo check doesn't crash
    def test_phase_7():
        r, next_task = _phase_7_todo_check("nonexistent_test_worker")
        assert isinstance(r, PhaseResult)
        assert r.phase == 7
    tests.append(("Phase 7 execution", test_phase_7))

    # Test 14: skip_phases parameter
    def test_skip_phases():
        # Skip all phases except 6 and 7 (lightweight, no side effects)
        result = execute_post_task_lifecycle(
            "test_worker", "test", "test result",
            success=True, skip_phases=[1, 2, 3, 4, 5],
        )
        # Should complete without error
        assert result is None or isinstance(result, str)
    tests.append(("skip_phases parameter", test_skip_phases))

    # Test 15: Persist log creates valid JSON
    def test_persist_log():
        lr = LifecycleResult(worker="test", task="test", task_success=True,
                             total_elapsed_ms=5.0, all_phases_ok=True)
        lr.phases.append(PhaseResult(1, "Test", True, 1.0, detail="ok"))
        _persist_log(lr)
        assert LOG_FILE.exists()
        with open(LOG_FILE, "r") as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) > 0
        assert data[-1]["worker"] == "test"
    tests.append(("persist log", test_persist_log))

    # Test 16: Full lifecycle with all phases skipped returns None
    def test_full_skip():
        result = execute_post_task_lifecycle(
            "test_worker", "noop", "noop",
            skip_phases=[1, 2, 3, 4, 5, 6, 7],
        )
        assert result is None
    tests.append(("full skip lifecycle", test_full_skip))

    # Run all tests
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name} — {type(e).__name__}: {e}")
            traceback.print_exc()

    print("-" * 60)
    print(f"Results: {passed}/{passed + failed} passed, {failed} failed")
    print("=" * 60)
    return failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Skynet post-task lifecycle executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_post_task.py --worker alpha --task "fix bug" --result "Fixed null check"
  python tools/skynet_post_task.py --worker beta --task "audit" --result "Failed" --failed
  python tools/skynet_post_task.py --test
  python tools/skynet_post_task.py --worker gamma --task "test" --result "ok" --skip 3 4
        """,
    )
    parser.add_argument("--worker", type=str, help="Worker name (alpha/beta/gamma/delta)")
    parser.add_argument("--task", type=str, help="Task description")
    parser.add_argument("--result", type=str, help="Result summary")
    parser.add_argument("--success", action="store_true", default=True, help="Task succeeded (default)")
    parser.add_argument("--failed", action="store_true", help="Task failed")
    parser.add_argument("--skip", type=int, nargs="*", help="Phase numbers to skip (1-7)")
    parser.add_argument("--test", action="store_true", help="Run self-tests")

    args = parser.parse_args()

    if args.test:
        ok = _run_self_test()
        sys.exit(0 if ok else 1)

    if not args.worker or not args.task or not args.result:
        parser.error("--worker, --task, and --result are required (or use --test)")

    success = not args.failed
    next_task = execute_post_task_lifecycle(
        worker_name=args.worker,
        task_description=args.task,
        result_summary=args.result,
        success=success,
        skip_phases=args.skip,
    )

    if next_task:
        print(f"\n⚠ ZERO-STOP LAW: Cannot idle — {next_task}")
        sys.exit(2)  # Exit code 2 = pending work
    else:
        print("\n✓ All clear — worker may idle")
        sys.exit(0)


if __name__ == "__main__":
    main()
