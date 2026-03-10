#!/usr/bin/env python3
"""test_intelligence_pipeline.py — End-to-end diagnostic for the full intelligence stack.

Tests that all engines instantiate and the orchestrator pipeline actually
invokes the cognitive engines (planner, reflexion, memory).

Usage:
    python tools/test_intelligence_pipeline.py
    Exit code 0 = all engines online, 1 = one or more failed.
"""

import sys
import time
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

PASS = "\033[92mONLINE\033[0m"
FAIL = "\033[91mOFFLINE\033[0m"
AVAIL = "\033[93mAVAILABLE\033[0m"

results = {}
errors = []


def test_engine(name, factory_fn):
    """Try to instantiate an engine. Returns (instance, status)."""
    try:
        t0 = time.perf_counter()
        instance = factory_fn()
        elapsed = (time.perf_counter() - t0) * 1000
        results[name] = {"status": "ONLINE", "ms": round(elapsed, 1)}
        return instance
    except Exception as e:
        results[name] = {"status": "OFFLINE", "error": str(e)[:120]}
        errors.append(f"{name}: {e}")
        return None


# ── 1. Core Engines ──

print("=" * 60)
print("  SKYNET INTELLIGENCE PIPELINE — END-TO-END TEST")
print("=" * 60)
print()

# DAAORouter
router = test_engine("DAAORouter", lambda: __import__("core.difficulty_router", fromlist=["DAAORouter"]).DAAORouter())

# HybridRetriever
retriever = test_engine("HybridRetriever", lambda: __import__("core.hybrid_retrieval", fromlist=["HybridRetriever"]).HybridRetriever())

# InputGuard
guard = test_engine("InputGuard", lambda: __import__("core.input_guard", fromlist=["InputGuard"]).InputGuard())

# AgentFactory
factory = test_engine("AgentFactory", lambda: __import__("core.agent_factory", fromlist=["AgentFactory"]).AgentFactory())

# DAGExecutor
executor = test_engine("DAGExecutor", lambda: __import__("core.dag_engine", fromlist=["DAGExecutor"]).DAGExecutor())

# DAGBuilder
builder = test_engine("DAGBuilder", lambda: __import__("core.dag_engine", fromlist=["DAGBuilder"]).DAGBuilder())


# ── 2. Cognitive Engines ──

# EpisodicMemory
memory = test_engine("EpisodicMemory", lambda: __import__("core.cognitive.memory", fromlist=["EpisodicMemory"]).EpisodicMemory())

# HierarchicalPlanner
planner = test_engine("HierarchicalPlanner", lambda: __import__("core.cognitive.planner", fromlist=["HierarchicalPlanner"]).HierarchicalPlanner())

# ReflexionEngine
reflexion = test_engine("ReflexionEngine", lambda: __import__("core.cognitive.reflexion", fromlist=["ReflexionEngine"]).ReflexionEngine())

# DynaActFilter
dynaact = test_engine("DynaActFilter", lambda: __import__("core.cognitive.reflexion", fromlist=["DynaActFilter"]).DynaActFilter())


# ── 3. Orchestrator (ties everything together) ──

orchestrator = test_engine("Orchestrator", lambda: __import__("core.orchestrator", fromlist=["Orchestrator"]).Orchestrator())


# ── 4. Functional Tests ──

print("\n--- FUNCTIONAL TESTS ---\n")

func_results = {}

# Router estimate
if router:
    try:
        plan = router.route("build a REST API", budget=1.0)
        func_results["router.route()"] = f"difficulty={plan.difficulty.level.name}, operator={plan.operator.value}"
    except Exception as e:
        func_results["router.route()"] = f"FAILED: {e}"

# Planner plan
if planner:
    try:
        plan = planner.create_plan("search for AI papers")
        func_results["planner.create_plan()"] = f"{len(plan.subtasks)} subtasks"
    except Exception as e:
        func_results["planner.create_plan()"] = f"FAILED: {e}"

# Memory store + retrieve
if memory:
    try:
        memory.store_episodic("test query about REST API", tags=["test", "api"])
        hits = memory.retrieve("REST API", limit=3)
        func_results["memory.store+retrieve()"] = f"stored 1, retrieved {len(hits)}"
    except Exception as e:
        func_results["memory.store+retrieve()"] = f"FAILED: {e}"

# Reflexion on_failure
if reflexion:
    try:
        from core.cognitive.reflexion import FailureContext
        fc = FailureContext(
            action_type="test", action_target="api_endpoint",
            error_message="connection refused", error_type="timeout",
        )
        ref = reflexion.on_failure(fc)
        func_results["reflexion.on_failure()"] = f"critique={ref.critique[:60]}..."
    except Exception as e:
        func_results["reflexion.on_failure()"] = f"FAILED: {e}"

# Orchestrator process (full pipeline)
if orchestrator:
    try:
        r = orchestrator.process("analyze codebase structure")
        steps = [p["step"] for p in r["pipeline"]]
        has_cognitive = "cognitive_plan" in steps
        func_results["orchestrator.process()"] = (
            f"status={r['status']}, steps={len(steps)}, "
            f"cognitive_plan={'YES' if has_cognitive else 'NO'}"
        )
    except Exception as e:
        func_results["orchestrator.process()"] = f"FAILED: {e}"

# Check orchestrator cognitive stats
if orchestrator:
    try:
        s = orchestrator.stats
        cog = s.get("cognitive", {})
        func_results["orchestrator.cognitive"] = (
            f"planner={cog.get('planner', '?')}, "
            f"reflexion={'online' if isinstance(cog.get('reflexion'), dict) else 'offline'}, "
            f"memory={'online' if isinstance(cog.get('episodic_memory'), dict) else 'offline'}"
        )
    except Exception as e:
        func_results["orchestrator.cognitive"] = f"FAILED: {e}"


# ── 5. Report ──

print("\n--- ENGINE STATUS TABLE ---\n")
print(f"{'Engine':<25} {'Status':<10} {'Time':>8}  {'Detail'}")
print("-" * 70)

all_online = True
for name, info in results.items():
    status = info["status"]
    ms = info.get("ms", "")
    detail = info.get("error", "")
    if status == "ONLINE":
        tag = PASS
    elif status == "AVAILABLE":
        tag = AVAIL
    else:
        tag = FAIL
        all_online = False
    ms_str = f"{ms:.0f}ms" if isinstance(ms, (int, float)) else ""
    print(f"  {name:<23} {tag:<20} {ms_str:>8}  {detail[:40]}")

print(f"\n--- FUNCTIONAL TEST RESULTS ---\n")
for test_name, result_text in func_results.items():
    status_icon = "PASS" if "FAILED" not in result_text else "FAIL"
    color = "\033[92m" if status_icon == "PASS" else "\033[91m"
    print(f"  {color}{status_icon}\033[0m  {test_name}: {result_text}")

# Summary
online_count = sum(1 for v in results.values() if v["status"] == "ONLINE")
total = len(results)
func_pass = sum(1 for v in func_results.values() if "FAILED" not in v)
func_total = len(func_results)

print(f"\n{'=' * 60}")
print(f"  ENGINES: {online_count}/{total} online")
print(f"  FUNCTIONAL: {func_pass}/{func_total} pass")
print(f"  COGNITIVE WIRED: {'YES' if orchestrator and orchestrator._planner and orchestrator._reflexion and orchestrator._episodic_memory else 'NO'}")
print(f"{'=' * 60}")

if errors:
    print(f"\nErrors:")
    for e in errors:
        print(f"  - {e}")

sys.exit(0 if all_online else 1)
