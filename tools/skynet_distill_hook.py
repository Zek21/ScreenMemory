#!/usr/bin/env python3
"""
skynet_distill_hook.py -- Post-task Knowledge Distillation Hook.

LEVEL 4 ACTIVATION: Wires KnowledgeDistiller from core/cognitive/ into
the worker result pipeline so patterns are automatically extracted from
every task completion.

Architecture:
    Worker completes task
    → Result arrives on bus (topic=orchestrator, type=result)
    → distill_result() is called
    → KnowledgeDistiller extracts patterns via rule-based + LLM (if available)
    → Patterns stored in EpisodicMemory → consolidated to SemanticMemory
    → Learnings broadcast via skynet_knowledge.broadcast_learning()
    → Knowledge available for future task context enrichment

Integration points:
    1. skynet_brain_dispatch.py Step 7 (_brain_learn) calls distill_result()
    2. skynet_learner.py process_result() calls distill_result()
    3. Standalone CLI: python tools/skynet_distill_hook.py --scan

Usage:
    from tools.skynet_distill_hook import distill_result, distill_scan_bus
    distill_result("alpha", "fix auth module", "Fixed CORS header in auth.py")
    distill_scan_bus()  # Scan bus for unprocessed results and distill all
"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

logger = logging.getLogger("skynet_distill")

# State file for dedup
DISTILL_STATE_FILE = ROOT / "data" / "distill_state.json"
BUS_URL = "http://localhost:8420"


# ─── Singleton Memory + Distiller ─────────────────────────

_memory_instance = None
_distiller_instance = None


def _get_memory():
    """Lazy-init a shared EpisodicMemory instance for distillation."""
    global _memory_instance
    if _memory_instance is None:
        try:
            from core.cognitive.memory import EpisodicMemory
            _memory_instance = EpisodicMemory(
                working_capacity=7,
                episodic_capacity=500,
            )
            logger.info("EpisodicMemory initialized for distillation (capacity=500)")
        except Exception as e:
            logger.warning(f"Could not init EpisodicMemory: {e}")
    return _memory_instance


def _get_distiller():
    """Lazy-init the KnowledgeDistiller."""
    global _distiller_instance
    if _distiller_instance is None:
        memory = _get_memory()
        if memory is None:
            return None
        try:
            from core.cognitive.knowledge_distill import KnowledgeDistiller
            _distiller_instance = KnowledgeDistiller(
                memory=memory,
                decay_threshold=0.3,
                min_cluster_size=2,
            )
            logger.info("KnowledgeDistiller initialized (threshold=0.3, min_cluster=2)")
        except Exception as e:
            logger.warning(f"Could not init KnowledgeDistiller: {e}")
    return _distiller_instance


# ─── Core Distillation Function ───────────────────────────

def distill_result(
    worker: str,
    task_text: str,
    result_text: str,
    success: Optional[bool] = None,
) -> Dict[str, Any]:
    """Distill a single task result into knowledge patterns.

    Stores the result as an episodic memory entry, then runs the
    KnowledgeDistiller to extract and promote patterns to semantic memory.
    Also broadcasts discoveries via skynet_knowledge.

    Args:
        worker: Worker name that completed the task.
        task_text: Original task description.
        result_text: Result summary from the worker.
        success: Whether the task succeeded (auto-detected if None).

    Returns:
        Dict with distillation results:
        {
            "episodic_stored": bool,
            "patterns_extracted": int,
            "semantic_promoted": int,
            "broadcast": bool,
            "insights": list[str],
        }
    """
    result = {
        "episodic_stored": False,
        "patterns_extracted": 0,
        "semantic_promoted": 0,
        "broadcast": False,
        "insights": [],
        "distill_stats": {},
    }

    memory = _get_memory()
    if memory is None:
        logger.warning("No memory instance available, skipping distillation")
        return result

    # Auto-detect success if not provided
    if success is None:
        try:
            from tools.skynet_learner import detect_success
            success = detect_success(result_text)
        except ImportError:
            success = True  # default to success

    # Step 1: Store as episodic memory entry
    importance = 0.7 if success else 0.9  # failures are more important to remember
    tags = _extract_tags(task_text, result_text, worker)

    try:
        content = f"[{worker}] Task: {task_text[:200]} | Result: {result_text[:300]}"
        memory.store_episodic(
            content=content,
            tags=tags,
            source_action=f"task_completion:{worker}",
            importance=importance,
        )
        result["episodic_stored"] = True
        logger.info(f"Stored episodic memory for {worker}'s result (tags={tags[:3]})")
    except Exception as e:
        logger.warning(f"Episodic store failed: {e}")

    # Step 2: Extract pattern insights using rule-based analysis
    insights = _extract_pattern_insights(task_text, result_text, success, worker)
    result["insights"] = insights
    result["patterns_extracted"] = len(insights)

    # Step 3: Run KnowledgeDistiller to consolidate if enough entries accumulated
    distiller = _get_distiller()
    if distiller and len(memory._episodic) >= 4:
        try:
            distill_stats = distiller.distill()
            result["distill_stats"] = distill_stats
            result["semantic_promoted"] = distill_stats.get("distilled", 0)
            if distill_stats.get("distilled", 0) > 0:
                logger.info(
                    f"Distillation: promoted {distill_stats['distilled']} clusters, "
                    f"freed {distill_stats['freed']} episodic entries"
                )
        except Exception as e:
            logger.warning(f"Distillation error: {e}")

    # Step 4: Store insights in LearningStore for persistent cross-session access
    stored_count = 0
    if insights:
        try:
            from core.learning_store import PersistentLearningSystem
            pls = PersistentLearningSystem()
            category = tags[0] if tags else "general"
            fact_ids = pls.learn_from_task(
                task_description=task_text[:300],
                category=category,
                success=success,
                insights=insights,
            )
            stored_count = len(fact_ids)
            logger.info(f"Stored {stored_count} distilled facts in LearningStore")
        except Exception as e:
            logger.warning(f"LearningStore error: {e}")

    # Step 5: Broadcast top insight to knowledge bus
    if insights:
        try:
            from tools.skynet_knowledge import broadcast_learning
            top_insight = insights[0]
            category = tags[0] if tags else "general"
            ok = broadcast_learning(
                sender=f"distiller:{worker}",
                fact=top_insight,
                category=category,
                tags=tags[:5],
            )
            result["broadcast"] = ok
            if ok:
                logger.info(f"Broadcast distilled insight: {top_insight[:80]}")
        except Exception as e:
            logger.warning(f"Broadcast error: {e}")

    return result


# ─── Pattern Extraction ───────────────────────────────────

def _extract_tags(task_text: str, result_text: str, worker: str) -> List[str]:
    """Extract meaningful tags from task and result text."""
    combined = f"{task_text} {result_text}".lower()
    tags = [worker]

    # Domain tags
    domain_keywords = {
        "infrastructure": ["daemon", "watchdog", "monitor", "backend", "server"],
        "browser": ["chrome", "cdp", "god_mode", "playwright", "browser"],
        "dashboard": ["dashboard", "god_console", "html", "css", "ui"],
        "dispatch": ["dispatch", "routing", "worker", "queue", "pipeline"],
        "security": ["credential", "security", "guard", "auth"],
        "perception": ["capture", "screenshot", "ocr", "vision", "uia"],
        "code": ["refactor", "fix", "bug", "test", "compile", "build"],
    }

    for domain, keywords in domain_keywords.items():
        if any(kw in combined for kw in keywords):
            tags.append(domain)

    # Success/failure tag
    try:
        from tools.skynet_learner import detect_success
        if detect_success(result_text):
            tags.append("success")
        else:
            tags.append("failure")
    except ImportError:
        pass

    return tags[:8]


def _extract_pattern_insights(
    task_text: str,
    result_text: str,
    success: bool,
    worker: str,
) -> List[str]:
    """Extract high-level pattern insights from task completion.

    Goes beyond skynet_learner.extract_insights() by looking for
    cross-cutting patterns, architectural decisions, and reusable strategies.
    """
    insights = []
    combined = f"{task_text} {result_text}"
    combined_lower = combined.lower()

    # 1. Core outcome insight
    task_summary = task_text[:120].replace("\n", " ").strip()
    if success:
        insights.append(f"Pattern: {worker} successfully handled '{task_summary}'")
    else:
        insights.append(f"Failure pattern: {worker} failed on '{task_summary}'")

    # 2. Tool/module usage patterns
    import re
    modules = re.findall(
        r'(?:core|tools|Skynet)/[\w/_.]+\.(?:py|go|json|html)',
        combined,
    )
    if modules:
        unique_modules = list(dict.fromkeys(modules))[:5]
        insights.append(f"Modules involved: {', '.join(unique_modules)}")

    # 3. Architectural pattern detection
    arch_patterns = {
        "singleton": r'singleton|single instance|pid.?file|already running',
        "retry_logic": r'retry|retries|backoff|exponential|attempt',
        "caching": r'cache|ttl|cached|memoize',
        "concurrency": r'parallel|concurrent|async|thread|lock|mutex',
        "api_design": r'endpoint|route|handler|middleware|cors',
        "error_handling": r'try.?catch|except|fallback|graceful|recover',
    }
    detected_patterns = []
    for pattern_name, regex in arch_patterns.items():
        if re.search(regex, combined_lower):
            detected_patterns.append(pattern_name)
    if detected_patterns:
        insights.append(f"Architectural patterns used: {', '.join(detected_patterns)}")

    # 4. Performance insights
    perf_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ms|seconds?|s)\b', combined_lower)
    if perf_match:
        insights.append(f"Performance data point: {perf_match.group(0)}")

    # 5. Failure root cause (if failed)
    if not success:
        cause_match = re.search(
            r'(?:because|root cause|reason|due to|caused by)[:\s]+(.{20,150})',
            combined,
            re.IGNORECASE,
        )
        if cause_match:
            insights.append(f"Root cause: {cause_match.group(1).strip()}")

    # 6. Cross-worker collaboration patterns
    other_workers = {"alpha", "beta", "gamma", "delta"} - {worker}
    mentioned_workers = [w for w in other_workers if w in combined_lower]
    if mentioned_workers:
        insights.append(f"Cross-worker collaboration: {worker} referenced {', '.join(mentioned_workers)}")

    return insights[:6]


# ─── Bus Scanner ──────────────────────────────────────────

def _load_distill_state() -> dict:
    """Load distillation state for dedup."""
    if DISTILL_STATE_FILE.exists():
        try:
            return json.loads(DISTILL_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen_ids": [], "total_distilled": 0, "last_scan": None}


def _save_distill_state(state: dict):
    """Persist distillation state."""
    DISTILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("seen_ids", [])) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    DISTILL_STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str), encoding="utf-8"
    )


def distill_scan_bus(limit: int = 50) -> List[Dict[str, Any]]:
    """Scan bus for task results and distill each one.

    Deduplicates using distill_state.json so results are only processed once.

    Returns:
        List of distillation result dicts.
    """
    import hashlib
    from urllib.request import urlopen

    state = _load_distill_state()
    seen = set(state.get("seen_ids", []))

    try:
        url = f"{BUS_URL}/bus/messages?topic=orchestrator&limit={limit}"
        with urlopen(url, timeout=5) as r:
            messages = json.loads(r.read())
    except Exception as e:
        logger.warning(f"Bus fetch error: {e}")
        return []

    if not isinstance(messages, list):
        return []

    results = []
    for msg in messages:
        if msg.get("type") != "result":
            continue
        if msg.get("sender") in ("learner", "distiller", "brain_dispatch"):
            continue

        # Dedup
        msg_id = msg.get("id", "")
        if not msg_id:
            raw = f"{msg.get('sender', '')}:{msg.get('content', '')[:200]}"
            msg_id = hashlib.md5(raw.encode()).hexdigest()

        if msg_id in seen:
            continue
        seen.add(msg_id)

        worker = msg.get("sender", "unknown")
        content = msg.get("content", "")

        dr = distill_result(
            worker=worker,
            task_text=content[:300],
            result_text=content,
            success=None,
        )
        results.append(dr)

    # Update state
    state["seen_ids"] = list(seen)
    state["total_distilled"] = state.get("total_distilled", 0) + len(results)
    state["last_scan"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_distill_state(state)

    if results:
        logger.info(f"Distilled {len(results)} bus results into knowledge")

    return results


# ─── Stats ────────────────────────────────────────────────

def get_distill_stats() -> dict:
    """Get distillation pipeline statistics."""
    state = _load_distill_state()
    memory = _get_memory()
    distiller = _get_distiller()

    return {
        "total_distilled": state.get("total_distilled", 0),
        "seen_ids_count": len(state.get("seen_ids", [])),
        "last_scan": state.get("last_scan"),
        "memory": {
            "episodic_count": len(memory._episodic) if memory else 0,
            "semantic_count": len(memory._semantic) if memory else 0,
            "working_count": len(memory._working) if memory else 0,
        } if memory else {},
        "distiller": distiller.stats if distiller else {},
    }


# ─── CLI ──────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Skynet Knowledge Distillation Hook (Level 4)"
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan bus for results and distill all unprocessed",
    )
    parser.add_argument(
        "--distill", type=str, metavar="TEXT",
        help="Distill a single result text",
    )
    parser.add_argument(
        "--worker", type=str, default="manual",
        help="Worker name for --distill (default: manual)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show distillation statistics",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max bus messages to scan (default: 50)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[DISTILL] %(message)s",
    )

    if args.stats:
        stats = get_distill_stats()
        print(json.dumps(stats, indent=2, default=str))
        return

    if args.distill:
        result = distill_result(
            worker=args.worker,
            task_text=args.distill,
            result_text=args.distill,
        )
        print(json.dumps(result, indent=2, default=str))
        return

    if args.scan:
        results = distill_scan_bus(limit=args.limit)
        print(f"Distilled {len(results)} results from bus")
        for r in results:
            stored = "✓" if r["episodic_stored"] else "✗"
            broadcast = "✓" if r["broadcast"] else "✗"
            print(
                f"  [{stored}] patterns={r['patterns_extracted']} "
                f"semantic={r['semantic_promoted']} broadcast={broadcast}"
            )
            for insight in r.get("insights", [])[:2]:
                print(f"      → {insight[:100]}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
