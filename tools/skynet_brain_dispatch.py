#!/usr/bin/env python3
"""
skynet_brain_dispatch.py -- Integration layer connecting SkynetBrain
intelligence to the existing Skynet dispatch infrastructure.

Bridges skynet_brain.py (planning/reasoning) with skynet_dispatch.py
(UIA window typing) and orch_realtime.py (zero-network result waiting).

Usage:
    python skynet_brain_dispatch.py "review the auth module"
    python skynet_brain_dispatch.py --plan-only "fix all failing tests"
    python skynet_brain_dispatch.py --dry-run "build a REST API"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

SKYNET_URL = "http://localhost:8420"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_GOLD = "\033[93m"
C_CYAN = "\033[96m"
C_PURPLE = "\033[95m"


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg, level="SYS"):
    prefix = {"OK": "✅", "ERR": "❌", "WARN": "⚠️", "SYS": "⚙️"}.get(level, "⚙️")
    print(f"[{_ts()}] {prefix} {msg}")


def _bus_post(sender, topic, msg_type, content):
    from tools.shared.bus import bus_post_fields
    return bus_post_fields(sender, topic, msg_type, content)


def _get_status():
    """Get current worker status from Skynet."""
    try:
        from urllib.request import urlopen
        data = json.loads(urlopen(f"{SKYNET_URL}/status", timeout=3).read())
        return data.get("agents", {})
    except Exception:
        return {}


def _get_idle_workers(agents=None):
    """Return list of IDLE worker names (excludes orchestrator)."""
    if agents is None:
        agents = _get_status()
    return [n for n in WORKER_NAMES if agents.get(n, {}).get("status") == "IDLE"]


def _get_knowledge(limit=20):
    """Fetch recent knowledge learnings from bus."""
    try:
        from urllib.request import urlopen
        msgs = json.loads(urlopen(f"{SKYNET_URL}/bus/messages?limit={limit}&topic=knowledge", timeout=3).read())
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


# ── Context Enrichment ────────────────────────────────────────────

def context_enrich(task: str, learnings: list = None, past_results: list = None) -> str:
    """Build an enriched task string with relevant context.

    Keeps each section to max 500 chars to avoid overwhelming workers.
    """
    parts = [task]

    if learnings:
        relevant = [l.get("content", "") for l in learnings if l.get("content")][:5]
        if relevant:
            ctx = "\n".join(f"- {r[:100]}" for r in relevant)[:500]
            parts.append(f"\nRELEVANT LEARNINGS:\n{ctx}")

    if past_results:
        relevant = [r.get("content", "") for r in past_results if r.get("content")][:3]
        if relevant:
            ctx = "\n".join(f"- {r[:150]}" for r in relevant)[:500]
            parts.append(f"\nRELEVANT CONTEXT:\n{ctx}")

    # Infer expected output from task keywords
    task_lower = task.lower()
    if any(k in task_lower for k in ["audit", "review", "check", "scan"]):
        parts.append("\nEXPECTED OUTPUT: Findings list with severity and file locations.")
    elif any(k in task_lower for k in ["fix", "patch", "repair", "resolve"]):
        parts.append("\nEXPECTED OUTPUT: Files changed, what was fixed, verification status.")
    elif any(k in task_lower for k in ["build", "create", "implement", "add"]):
        parts.append("\nEXPECTED OUTPUT: New file paths, key functions/classes created, test status.")
    elif any(k in task_lower for k in ["test", "validate", "verify"]):
        parts.append("\nEXPECTED OUTPUT: Pass/fail count, failing test details if any.")

    return "\n".join(parts)


# ── Worker Routing ────────────────────────────────────────────────

def route_to_best_worker(subtask: str, workers_state: dict = None, expertise: dict = None) -> str:
    """Pick the best worker for a subtask.

    Scoring: IDLE filter → expertise match → lowest task count → round-robin.
    """
    if workers_state is None:
        workers_state = _get_status()

    idle = [n for n in WORKER_NAMES if workers_state.get(n, {}).get("status") == "IDLE"]
    if not idle:
        idle = WORKER_NAMES  # fallback to all if none idle

    expertise = expertise or {}
    subtask_lower = subtask.lower()

    scored = []
    for w in idle:
        score = 0
        # Expertise match
        worker_skills = expertise.get(w, [])
        for skill in worker_skills:
            if skill.lower() in subtask_lower:
                score += 10
        # Lower task count = better (less loaded)
        tasks_done = workers_state.get(w, {}).get("tasks_completed", 0)
        score -= tasks_done
        scored.append((w, score))

    scored.sort(key=lambda x: (-x[1], x[0]))  # highest score first, alpha tiebreak
    return scored[0][0]


# ── Plan Representation ──────────────────────────────────────────

def _brain_think(goal: str) -> dict:
    """Generate an intelligent execution plan.

    Tries to import SkynetBrain first. Falls back to rule-based decomposition
    using SkynetOrchestrator's decompose_task if brain is unavailable.
    """
    try:
        from skynet_brain import SkynetBrain
        from dataclasses import asdict
        brain = SkynetBrain()
        plan = brain.think(goal)
        result = asdict(plan)
        # Normalize subtask field names for smart_dispatch compatibility
        for st in result.get("subtasks", []):
            if "task_text" in st and "task" not in st:
                st["task"] = st.pop("task_text")
            if "assigned_worker" in st and "worker" not in st:
                st["worker"] = st.pop("assigned_worker")
        return result
    except ImportError:
        pass

    # Fallback: rule-based decomposition via SkynetOrchestrator
    agents = _get_status()
    idle = _get_idle_workers(agents)

    goal_lower = goal.lower()

    # Classify difficulty
    if len(goal) > 300 or any(k in goal_lower for k in ["all", "every", "entire", "comprehensive"]):
        difficulty = "hard"
    elif len(goal) > 100 or any(k in goal_lower for k in ["review", "audit", "fix", "build"]):
        difficulty = "medium"
    else:
        difficulty = "easy"

    # Decompose into subtasks
    subtasks = []

    # Check for explicit worker assignments: "alpha: X, beta: Y"
    import re
    explicit = re.findall(
        r'\b(alpha|beta|gamma|delta)\s*:\s*(.+?)(?=\b(?:alpha|beta|gamma|delta)\s*:|$)',
        goal, re.IGNORECASE)

    if explicit:
        for worker, task in explicit:
            subtasks.append({
                "worker": worker.lower(),
                "task": task.strip(),
                "depends_on": None,
            })
    else:
        # Check for multi-path patterns
        paths = re.findall(r'(?:core/|tools/|Skynet/|tests/|ui/|docs/)\S*', goal)
        if len(paths) >= 2 and len(idle) >= 2:
            base = re.split(r'(?:core/|tools/|Skynet/|tests/|ui/|docs/)', goal)[0].strip()
            for i, path in enumerate(paths):
                worker = idle[i % len(idle)]
                subtasks.append({
                    "worker": worker,
                    "task": f"{base} {path}".strip(),
                    "depends_on": None,
                })
        elif difficulty == "easy" or len(idle) <= 1:
            worker = idle[0] if idle else "alpha"
            subtasks.append({"worker": worker, "task": goal, "depends_on": None})
        else:
            # Medium/hard: split across available workers
            n = min(len(idle), 4 if difficulty == "hard" else 2)
            for i in range(n):
                subtasks.append({
                    "worker": idle[i],
                    "task": goal if n == 1 else f"{goal} (worker {i+1}/{n}, focus on your assigned portion)",
                    "depends_on": None,
                })

    # Check for dependencies
    has_deps = any(k in goal_lower for k in ["then", "after that", "once done", "followed by"])

    return {
        "goal": goal,
        "difficulty": difficulty,
        "subtasks": subtasks,
        "has_dependencies": has_deps,
        "reasoning": f"Rule-based: {difficulty} task, {len(subtasks)} subtask(s), {len(idle)} idle workers",
        "worker_count": len(subtasks),
    }


def _brain_synthesize(plan: dict, results: dict) -> str:
    """Synthesize results into a summary."""
    # Fallback synthesis (plan is always a dict from _brain_think)
    lines = [f"# Brain Dispatch Report", f"**Goal:** {plan.get('goal', '?')}", ""]
    success = 0
    for st in plan.get("subtasks", []):
        w = st.get("worker", "?")
        r = results.get(w)
        if r and isinstance(r, dict):
            content = r.get("content", "")[:300]
            status = "✅"
            success += 1
        elif r and isinstance(r, str):
            content = r[:300]
            status = "✅"
            success += 1
        else:
            content = "No result"
            status = "❌"
        lines.append(f"## {w.upper()} {status}")
        lines.append(f"**Task:** {st.get('task', '?')[:120]}")
        lines.append(f"**Result:** {content}")
        lines.append("")

    total = len(plan.get("subtasks", []))
    lines.append(f"**Summary:** {success}/{total} succeeded")

    # Also learn from results
    try:
        from skynet_brain import SkynetBrain
        brain = SkynetBrain()
        brain.learn(plan, results, success == total)
    except Exception:
        pass

    return "\n".join(lines)


def _brain_learn(plan: dict, results: dict, success: bool):
    """Record learnings from dispatch outcome and run knowledge distillation."""
    difficulty = plan.get("difficulty", "?")
    n_workers = len(plan.get("subtasks", []))
    goal_short = plan.get("goal", "")[:80]
    _bus_post("brain_dispatch", "knowledge", "learning",
             f"{'SUCCESS' if success else 'FAILURE'}: {difficulty} task ({n_workers} workers) -- {goal_short}")

    # Level 4: Auto-distill each worker result through KnowledgeDistiller
    try:
        from tools.skynet_distill_hook import distill_result
        for st in plan.get("subtasks", []):
            worker = st.get("worker", "unknown")
            task_text = st.get("task", "")[:300]
            result_data = results.get(worker)
            if result_data:
                result_text = (
                    result_data.get("content", "")
                    if isinstance(result_data, dict)
                    else str(result_data)
                )[:500]
                dr = distill_result(worker, task_text, result_text, success)
                if dr.get("patterns_extracted", 0) > 0:
                    log(f"Distilled {dr['patterns_extracted']} patterns from {worker}", "OK")
    except Exception as e:
        log(f"Distillation hook error: {e}", "WARN")


# ── Smart Dispatch Pipeline ──────────────────────────────────────

def smart_dispatch(goal: str, wait_timeout: int = 120) -> dict:
    """Full intelligent dispatch pipeline.

    think → enrich → dispatch → wait → synthesize → learn → return
    """
    t0 = time.time()
    log(f"Brain dispatch: {goal[:100]}", "SYS")

    # Step 1: Think — generate plan
    plan = _brain_think(goal)
    subtasks = plan.get("subtasks", [])
    difficulty = plan.get("difficulty", "?")
    reasoning = plan.get("reasoning", "")

    print(f"\n{C_GOLD}{C_BOLD}PLAN{C_RESET}")
    print(f"  Difficulty:  {C_BOLD}{difficulty}{C_RESET}")
    print(f"  Subtasks:    {len(subtasks)}")
    print(f"  Workers:     {', '.join(st['worker'] for st in subtasks)}")
    print(f"  Dependencies:{' yes' if plan.get('has_dependencies') else ' none'}")
    # Show cognitive strategy if present
    cog_strategy = plan.get("cognitive_strategy") or "direct"
    if cog_strategy != "direct":
        print(f"  {C_PURPLE}Cognitive:   {cog_strategy.upper()}{C_RESET}")
    print(f"  Reasoning:   {C_DIM}{reasoning}{C_RESET}\n")

    if not subtasks:
        log("No subtasks generated", "ERR")
        return {"success": False, "error": "Empty plan", "elapsed_s": time.time() - t0}

    # Step 2: Enrich tasks with context
    learnings = _get_knowledge(20)
    for st in subtasks:
        st["task"] = context_enrich(st["task"], learnings)

    # Step 3: Consume old results
    from orch_realtime import consume_all
    consume_all()

    # Step 4: Dispatch
    from skynet_dispatch import dispatch_to_worker, dispatch_parallel

    # Wire cognitive strategy into dispatch context for learner correlation
    os.environ["SKYNET_STRATEGY"] = cog_strategy

    dispatch_results = {}
    if len(subtasks) == 1:
        st = subtasks[0]
        log(f"Single dispatch → {st['worker'].upper()}", "SYS")
        ok = dispatch_to_worker(st["worker"], st["task"])
        dispatch_results[st["worker"]] = ok
    elif plan.get("has_dependencies"):
        # Sequential dispatch for dependent tasks
        log(f"Sequential dispatch ({len(subtasks)} steps)", "SYS")
        for i, st in enumerate(subtasks):
            log(f"Step {i+1}/{len(subtasks)} → {st['worker'].upper()}", "SYS")
            ok = dispatch_to_worker(st["worker"], st["task"])
            dispatch_results[st["worker"]] = ok
            if ok and i < len(subtasks) - 1:
                # Wait for this step before dispatching next
                from orch_realtime import wait
                result = wait(st["worker"], timeout=wait_timeout // len(subtasks))
                if result:
                    # Feed result into next task's context
                    next_st = subtasks[i + 1]
                    prev_content = result.get("content", "")[:300]
                    next_st["task"] = f"{next_st['task']}\n\nPREVIOUS STEP RESULT ({st['worker']}):\n{prev_content}"
    else:
        # Parallel dispatch
        tasks_by_worker = {st["worker"]: st["task"] for st in subtasks}
        log(f"Parallel dispatch → {list(tasks_by_worker.keys())}", "SYS")
        dispatch_results = dispatch_parallel(tasks_by_worker)

    failed = [w for w, ok in dispatch_results.items() if not ok]
    if failed:
        log(f"Dispatch failed for: {failed}", "WARN")

    dispatched_workers = [w for w, ok in dispatch_results.items() if ok]
    if not dispatched_workers:
        log("All dispatches failed", "ERR")
        return {"success": False, "error": "All dispatches failed", "elapsed_s": time.time() - t0}

    # Step 5: Wait for results
    from orch_realtime import wait, wait_all
    if len(dispatched_workers) == 1:
        result = wait(dispatched_workers[0], timeout=wait_timeout)
        results = {dispatched_workers[0]: result} if result else {}
    else:
        results = wait_all(dispatched_workers, timeout=wait_timeout)

    # Step 6: Synthesize
    synthesis = _brain_synthesize(plan, results)
    print(f"\n{C_CYAN}{C_BOLD}SYNTHESIS{C_RESET}")
    print(synthesis)

    # Step 7: Learn
    success = len(results) == len(dispatched_workers) and all(results.values())
    _brain_learn(plan, results, success)

    elapsed = time.time() - t0
    log(f"Brain dispatch complete in {elapsed:.1f}s (success={success})", "OK" if success else "WARN")

    return {
        "success": success,
        "plan": plan,
        "dispatch_results": dispatch_results,
        "results": {k: (v.get("content", "") if isinstance(v, dict) else str(v)) for k, v in results.items()},
        "synthesis": synthesis,
        "elapsed_s": elapsed,
    }


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Brain Dispatch")
    parser.add_argument("goal", nargs="?", help="Goal/task to dispatch")
    parser.add_argument("--plan-only", action="store_true", help="Show plan without dispatching")
    parser.add_argument("--dry-run", action="store_true", help="Show enriched plan without dispatching")
    parser.add_argument("--timeout", type=int, default=120, help="Wait timeout in seconds")
    args = parser.parse_args()

    if not args.goal:
        parser.print_help()
        sys.exit(1)

    if args.plan_only:
        plan = _brain_think(args.goal)
        print(f"\n{C_GOLD}{C_BOLD}PLAN (no dispatch){C_RESET}")
        print(json.dumps(plan, indent=2))
        return

    if args.dry_run:
        plan = _brain_think(args.goal)
        learnings = _get_knowledge(20)
        print(f"\n{C_GOLD}{C_BOLD}DRY RUN{C_RESET}")
        print(f"  Difficulty:  {plan.get('difficulty')}")
        print(f"  Subtasks:    {len(plan.get('subtasks', []))}")
        print(f"  Dependencies:{' yes' if plan.get('has_dependencies') else ' none'}")
        print(f"  Reasoning:   {plan.get('reasoning')}\n")
        for i, st in enumerate(plan.get("subtasks", [])):
            enriched = context_enrich(st["task"], learnings)
            print(f"  {C_BOLD}Subtask {i+1} → {st['worker'].upper()}{C_RESET}")
            print(f"  {enriched[:200]}")
            print()
        return

    result = smart_dispatch(args.goal, wait_timeout=args.timeout)
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
