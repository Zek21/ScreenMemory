#!/usr/bin/env python3
"""
Reflexion hook for the Skynet dispatch pipeline.

Integrates ReflexionEngine into live dispatch so workers automatically
learn from failures:
  - Post-dispatch: on FAILURE, generates verbal self-critique via ReflexionEngine
  - Pre-dispatch:  queries LearningStore for past failures and injects context
  - Persistent:    all reflections stored in LearningStore (survives restarts)

Usage:
  # In dispatch pipeline (programmatic)
  from tools.skynet_reflexion_hook import reflexion_hook, pre_dispatch_context
  context = pre_dispatch_context(task_text)          # before dispatch
  reflexion_hook("alpha", task, result, success)      # after result

  # CLI
  python tools/skynet_reflexion_hook.py --status
  python tools/skynet_reflexion_hook.py --query "ring buffer"
  python tools/skynet_reflexion_hook.py --recent 10
"""
# signed: alpha

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REFLEXION_LOG = DATA / "reflexion_log.json"
MAX_LOG_ENTRIES = 500
MAX_CONTEXT_CHARS = 800  # cap injected context to avoid bloating prompts

# ── Lazy engine singletons ────────────────────────────────────────── # signed: alpha

_reflexion_engine = None
_learning_store = None
_init_attempted = False
_init_error: Optional[str] = None


def _ensure_engines() -> bool:
    """Lazily initialise ReflexionEngine + LearningStore. Returns True on success."""
    global _reflexion_engine, _learning_store, _init_attempted, _init_error

    if _init_attempted:
        return _reflexion_engine is not None

    _init_attempted = True
    try:
        sys.path.insert(0, str(ROOT))
        from core.cognitive.reflexion import ReflexionEngine, FailureContext, Reflection  # noqa: F401
        from core.learning_store import LearningStore

        _learning_store = LearningStore()
        _reflexion_engine = ReflexionEngine(learning_store=_learning_store)
        return True
    except Exception as exc:
        _init_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Reflexion engine unavailable: %s — hook will degrade gracefully", _init_error)
        return False


# ── Data classes ──────────────────────────────────────────────────── # signed: alpha

@dataclass
class ReflexionEntry:
    """Persistent log entry for a reflexion event."""
    timestamp: str
    worker: str
    task_summary: str
    success: bool
    critique: str = ""
    lesson: str = ""
    action_adjustment: str = ""
    confidence: float = 0.0
    side_effects: List[str] = field(default_factory=list)
    domain: str = ""


# ── Core hook functions ───────────────────────────────────────────── # signed: alpha

def reflexion_hook(
    worker_name: str,
    task: str,
    result_content: str,
    success: bool,
    *,
    domain: str = "",
    files_involved: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Called after a dispatch result is collected.

    On FAILURE:
      1. Builds FailureContext from task + result
      2. Calls ReflexionEngine.on_failure() for verbal self-critique
      3. Analyses side-effects (Hindsight Experience Replay)
      4. Stores critique in LearningStore
      5. Appends to reflexion_log.json

    On SUCCESS:
      1. Reinforces any past reflections that were applied
      2. Appends success record to log

    Returns dict with hook outcome, or None if engines unavailable.
    """
    if not _ensure_engines():
        logger.debug("Reflexion engines not available — skipping hook")
        return None

    from core.cognitive.reflexion import FailureContext  # guaranteed import after _ensure_engines

    now = datetime.now(timezone.utc).isoformat()
    task_summary = task[:200] if task else ""
    entry = ReflexionEntry(
        timestamp=now,
        worker=worker_name,
        task_summary=task_summary,
        success=success,
        domain=domain,
    )

    outcome: Dict[str, Any] = {
        "worker": worker_name,
        "success": success,
        "reflexion_generated": False,
        "side_effects_found": 0,
    }

    if not success:
        # Build failure context from dispatch data
        failure = FailureContext(
            action_type="dispatch_task",
            action_target=worker_name,
            action_value=task_summary,
            expected_outcome="Task completion with result posted to bus",
            actual_outcome=result_content[:500] if result_content else "No result received",
            error_message=_extract_error(result_content),
            error_type=_classify_error(result_content),
            domain=domain,
            files_involved=files_involved or [],
            subtask=task_summary,
        )

        try:
            reflection = _reflexion_engine.on_failure(failure)
            entry.critique = reflection.critique
            entry.lesson = reflection.lesson
            entry.action_adjustment = reflection.action_adjustment
            entry.confidence = reflection.confidence
            outcome["reflexion_generated"] = True
            outcome["critique"] = reflection.critique
            outcome["lesson"] = reflection.lesson

            # Hindsight Experience Replay — extract value from failures
            side_effects = _reflexion_engine.analyze_side_effects(failure)
            entry.side_effects = side_effects
            outcome["side_effects_found"] = len(side_effects)
        except Exception as exc:
            logger.warning("Reflexion on_failure raised: %s", exc)
            entry.critique = f"Reflexion engine error: {exc}"

        # Also store the failure in LearningStore directly for query recall
        try:
            _learning_store.learn(
                content=f"DISPATCH FAILURE [{worker_name}]: {entry.critique} | Lesson: {entry.lesson}",
                category="reflexion",
                source=f"reflexion_hook:{worker_name}",
                tags=["dispatch_failure", worker_name, domain] if domain else ["dispatch_failure", worker_name],
            )
        except Exception as exc:
            logger.warning("LearningStore.learn raised: %s", exc)

    else:
        # Success — reinforce any relevant past reflections
        try:
            past = _reflexion_engine.get_relevant_reflections(
                action_type="dispatch_task",
                target=worker_name,
                context=task_summary,
                limit=3,
            )
            outcome["reflections_reinforced"] = len(past)
        except Exception as exc:
            logger.debug("Reinforcement query failed: %s", exc)

    # Persist to reflexion log
    _append_log(entry)
    return outcome


def pre_dispatch_context(task: str, worker_name: str = "", top_k: int = 3) -> str:
    """
    Query past failure reflections relevant to this task.

    Returns a compact context string for injection into the dispatch
    preamble, or empty string if nothing relevant is found.
    """
    if not _ensure_engines():
        return ""

    try:
        ctx = _reflexion_engine.get_pre_task_context(
            task_description=task,
            action_type="dispatch_task",
            target=worker_name,
            top_k=top_k,
        )
        if ctx and len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[:MAX_CONTEXT_CHARS] + "\n... (truncated)"
        return ctx
    except Exception as exc:
        logger.debug("pre_dispatch_context failed: %s", exc)
        return ""


def query_learnings(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Search LearningStore for reflexion-category facts matching query."""
    if not _ensure_engines():
        return []

    try:
        facts = _learning_store.recall(query, top_k=top_k)
        return [
            {
                "fact_id": f.fact_id,
                "content": f.content,
                "confidence": f.confidence,
                "category": f.category,
                "reinforcement_count": f.reinforcement_count,
            }
            for f in facts
        ]
    except Exception as exc:
        logger.warning("query_learnings failed: %s", exc)
        return []


def get_status() -> Dict[str, Any]:
    """Return reflexion hook status and statistics."""
    status: Dict[str, Any] = {
        "engines_available": _ensure_engines(),
        "init_error": _init_error,
        "log_entries": 0,
        "failures_recorded": 0,
        "successes_recorded": 0,
        "learning_store_stats": None,
        "reflexion_engine_stats": None,
    }

    # Read log file
    entries = _read_log()
    status["log_entries"] = len(entries)
    status["failures_recorded"] = sum(1 for e in entries if not e.get("success", True))
    status["successes_recorded"] = sum(1 for e in entries if e.get("success", False))

    if _learning_store:
        try:
            status["learning_store_stats"] = _learning_store.stats()
        except Exception:
            pass

    if _reflexion_engine:
        try:
            status["reflexion_engine_stats"] = _reflexion_engine.stats()
        except Exception:
            pass

    return status


# ── Helpers ───────────────────────────────────────────────────────── # signed: alpha

def _extract_error(result: str) -> str:
    """Extract error message from result content."""
    if not result:
        return "No result content"
    lower = result.lower()
    for marker in ["error:", "failed:", "exception:", "traceback"]:
        idx = lower.find(marker)
        if idx >= 0:
            return result[idx : idx + 300]
    return result[:300] if len(result) > 300 else result


def _classify_error(result: str) -> str:
    """Classify the error type from result content."""
    if not result:
        return "no_result"
    lower = result.lower()
    if "timeout" in lower:
        return "timeout"
    if "import" in lower and "error" in lower:
        return "import_error"
    if "permission" in lower:
        return "permission_error"
    if "not found" in lower or "no such file" in lower:
        return "not_found"
    if "compile" in lower or "syntax" in lower:
        return "compile_error"
    if "stuck" in lower or "processing" in lower:
        return "stuck_worker"
    if "fail" in lower or "error" in lower:
        return "generic_failure"
    return "unknown"


def _append_log(entry: ReflexionEntry) -> None:
    """Append entry to reflexion_log.json with size cap."""
    entries = _read_log()
    entries.append(asdict(entry))
    # Trim to max size
    if len(entries) > MAX_LOG_ENTRIES:
        entries = entries[-MAX_LOG_ENTRIES:]
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = REFLEXION_LOG.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, default=str)
        tmp.replace(REFLEXION_LOG)
    except Exception as exc:
        logger.warning("Failed to write reflexion log: %s", exc)


def _read_log() -> List[Dict[str, Any]]:
    """Read reflexion log entries."""
    if not REFLEXION_LOG.exists():
        return []
    try:
        with open(REFLEXION_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────── # signed: alpha

def main():
    parser = argparse.ArgumentParser(description="Skynet Reflexion Hook — dispatch learning from failures")
    parser.add_argument("--status", action="store_true", help="Show reflexion system status and stats")
    parser.add_argument("--query", type=str, help="Search LearningStore for relevant past failures")
    parser.add_argument("--recent", type=int, default=0, help="Show N most recent reflexion log entries")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results for --query (default: 5)")
    args = parser.parse_args()

    if args.status:
        status = get_status()
        print(json.dumps(status, indent=2, default=str))
        return

    if args.query:
        results = query_learnings(args.query, top_k=args.top_k)
        if results:
            for i, r in enumerate(results, 1):
                print(f"\n--- Result {i} (confidence: {r['confidence']:.2f}) ---")
                print(r["content"])
        else:
            print("No matching learnings found.")
        return

    if args.recent > 0:
        entries = _read_log()
        recent = entries[-args.recent:] if len(entries) > args.recent else entries
        for e in recent:
            status_icon = "✓" if e.get("success") else "✗"
            print(f"[{e.get('timestamp', '?')}] {status_icon} {e.get('worker', '?')}: {e.get('task_summary', '')[:80]}")
            if e.get("critique"):
                print(f"  Critique: {e['critique'][:120]}")
            if e.get("lesson"):
                print(f"  Lesson: {e['lesson'][:120]}")
        if not recent:
            print("No reflexion entries yet.")
        return

    # Default: show brief status
    status = get_status()
    avail = "✓" if status["engines_available"] else "✗"
    print(f"Reflexion engines: {avail}")
    print(f"Log entries: {status['log_entries']} ({status['failures_recorded']} failures, {status['successes_recorded']} successes)")
    if status["init_error"]:
        print(f"Init error: {status['init_error']}")
    if status.get("learning_store_stats"):
        ls = status["learning_store_stats"]
        print(f"LearningStore: {ls.get('total_facts', 0)} facts, avg confidence {ls.get('average_confidence', 0):.2f}")


if __name__ == "__main__":
    main()
