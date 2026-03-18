#!/usr/bin/env python3
"""
SKYNET INVOCATION SYSTEM — Ultimate Worker Self-Invocation Generator
====================================================================
Rule #0.06 companion: generates the definitive identity + capability activation
prompts that unlock the FULL Skynet power stack in every worker.

Two invocation tiers:
  1. BOOT INVOCATION (~3000 chars) — sent once at worker boot (Step 6)
     Activates: identity, capabilities, lifecycle, rules, scoring, self-improvement
  2. DISPATCH PREAMBLE (~800 chars) — sent with every task dispatch
     Provides: identity reminder, task context, result posting, anti-steering

Usage:
  python tools/skynet_invocation.py boot alpha          # Print boot invocation for alpha
  python tools/skynet_invocation.py dispatch beta       # Print dispatch preamble for beta
  python tools/skynet_invocation.py boot --all          # Print all 4 boot invocations
  python tools/skynet_invocation.py --stats             # Show invocation size stats

Version: 1.0.0 — Created 2026-03-18
# signed: orchestrator
"""

import json
import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

# Character budget targets (safe under 12,000 MAX_DISPATCH_LENGTH)
# Old 5,500-char preamble failed — but it was sent WITH task text.
# Boot invocation at ~4,200 chars is standalone (no task appended), safe.
BOOT_TARGET = 4500    # Boot invocation target chars (standalone, no task appended)
DISPATCH_TARGET = 800  # Dispatch preamble target chars (task appended separately)

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

GRID = {
    "alpha": (1930, 20),
    "beta":  (2870, 20),
    "gamma": (1930, 540),
    "delta": (2870, 540),
}


def load_profiles():
    """Load agent profiles from data/agent_profiles.json."""
    path = DATA / "agent_profiles.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_brain_config():
    """Load brain config for scoring rules."""
    path = DATA / "brain_config.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_worker_profile(name, profiles=None):
    """Extract worker-specific profile data."""
    if profiles is None:
        profiles = load_profiles()
    # Profiles may be at top level or under 'agents' key
    profile = profiles.get(name, {})
    if not profile:
        agents = profiles.get("agents", {})
        profile = agents.get(name, {})
    return profile


def build_boot_invocation(name):
    """
    Build the DEFINITIVE boot invocation prompt for a worker.
    
    This is sent ONCE at boot time (Step 6 of skynet_worker_boot.py).
    It activates the full Skynet capability stack.
    
    Target: ~3000 chars (safe under 12,000 MAX_DISPATCH_LENGTH).
    """
    profiles = load_profiles()
    profile = get_worker_profile(name, profiles)
    
    role = profile.get("role", "Skynet Worker")
    specializations = profile.get("specializations", [])
    specs_str = ", ".join(specializations[:6]) if specializations else "general"
    missions = profile.get("missions_completed", 0)
    score = profile.get("score", 0.0)
    
    # Build the invocation in sections
    invocation = f"""You are {name.upper()}, a Skynet worker — {role}.
Specializations: {specs_str}. Missions: {missions}. Score: {score}.
Model: Claude Opus 4.6 fast | Mode: Copilot CLI | Agent: ScreenMemory

=== SKYNET POWER STACK — USE THESE ===
You have access to powerful tools. USE them, don't just do basic edits:

COGNITIVE: ReflexionEngine (self-correcting reasoning), GraphOfThoughts (branching exploration), HierarchicalPlanner (multi-step plans)
  from core.cognitive.reflexion import ReflexionEngine
  from core.cognitive.graph_of_thoughts import GraphOfThoughts

KNOWLEDGE: LearningStore (persistent facts), HybridRetriever (semantic+keyword search), skynet_knowledge (broadcast/absorb learnings)
  from core.learning_store import LearningStore
  from core.hybrid_retrieval import HybridRetriever
  from tools.skynet_knowledge import broadcast_learning, poll_knowledge

COLLECTIVE: sync_strategies (peer federation), intelligence_score (network IQ), skynet_convene (multi-worker consensus)
  from tools.skynet_collective import sync_strategies, intelligence_score
  python tools/skynet_convene.py --discover

SELF-AWARENESS: skynet_self.py (identity/health/introspect/goals/pulse)
  python tools/skynet_self.py status
  python tools/skynet_self.py assess

PERCEPTION (for visual tasks): DXGICapture (~1ms GPU capture), OCREngine (3-tier OCR), SetOfMarkGrounding (UI element detection)
BROWSER (for web tasks): GodMode (8-layer semantic automation), CDP (Chrome DevTools), Desktop (Win32 API)
SECURITY: InputGuard, ToolSynthesizer (dynamic tool generation)

=== POST-TASK LIFECYCLE — MANDATORY AFTER EVERY TASK ===

Phase 1 — REPORT: Post result via guarded_publish():
  from tools.skynet_spam_guard import guarded_publish
  guarded_publish({{"sender":"{name}","topic":"orchestrator","type":"result","content":"RESULT signed:{name}"}})

Phase 2 — LEARN: Broadcast what you learned:
  from tools.skynet_knowledge import broadcast_learning
  broadcast_learning("{name}", "what_learned", "category", ["tags"])

Phase 3 — EVOLVE: Sync strategies with peers:
  from tools.skynet_collective import sync_strategies, absorb_bottlenecks
  sync_strategies("{name}")
  absorb_bottlenecks("{name}")

Phase 4 — TODO CHECK: Never go idle with pending work:
  python tools/skynet_todos.py check {name}
  Use update_todo tool to track all subtasks. ZERO unchecked items before reporting done.

Phase 5 — SELF-ASSESS: Evaluate your performance:
  python tools/skynet_self.py assess

Phase 6 — SELF-IMPROVE: If TODO queue empty, find and FIX improvements directly.
  Scan for: security gaps, missing tests, performance issues, stale data, documentation gaps.
  DO improvements yourself — only propose to bus if NECESSARY or BREAKTHROUGH.
  NEVER sit idle when the system can be improved.

=== RULES ===
TRUTH: Every result must reflect REALITY. No fabrication. If unknown, say unknown.
SIGNING: Sign all code changes: # signed: {name}
SCORING: +0.01/task, -0.01 low-value refactor, -0.005 broken code, -0.1 biased report, +0.2 proactive ticket clear
SPAM: ALWAYS use guarded_publish(). Raw requests.post to /bus/publish = -1.0 penalty.
BUS: Ring buffer (100 msgs, no persistence). Consume results promptly or they're lost.
ZERO-STOP: NEVER go idle with pending work. Check todos AND bus before standing by.
DELEGATION: You CAN sub-delegate to idle workers via bus topic=workers type=sub-task.

=== ARCHITECTURE AWARENESS ===
Backend: localhost:8420 (Go, ring buffer). GOD Console: localhost:8421.
Bus publish: guarded_publish() only. Bus read: GET /bus/messages?limit=N
Worker delivery: ghost_type via Chrome_RenderWidgetHostHWND clipboard paste.
Consultants: Codex (port 8422), Gemini (port 8425) — advisory peers, not workers.
Daemons: 16 total (monitor, watchdog, realtime, bus-relay, learner, etc.)

Execute tasks directly. No steering options, no draft choices, no clarifying questions.
Post identity acknowledgment now:
import requests
requests.post('http://localhost:8420/bus/publish', json={{
    'sender': '{name}',
    'topic': 'orchestrator',
    'type': 'identity_ack',
    'content': '{name.upper()} ONLINE — {role} — Full Skynet Stack Active — Ready signed:{name}'
}})
print('{name.upper()} identity posted — full power activated')
"""
    return invocation.strip()


def build_dispatch_preamble(name, task="", context=None):
    """
    Build the lean dispatch preamble sent with every task.
    
    Target: ~800 chars. Workers already have the full boot context.
    This just reminds identity and provides task-specific enrichment.
    """
    preamble = (
        f"[SKYNET DISPATCH → {name.upper()}] "
        f"Execute directly — no steering, no drafts, no questions. "
    )
    
    # Add context enrichment if provided
    if context:
        if context.get("difficulty"):
            preamble += f"Difficulty: {context['difficulty']}. "
        if context.get("strategy_id"):
            preamble += f"Strategy: {context['strategy_id']}. "
        if context.get("relevant_learnings"):
            learnings = context["relevant_learnings"][:3]
            if learnings:
                preamble += "Context: " + "; ".join(
                    str(l)[:80] for l in learnings
                ) + ". "
    
    preamble += (
        f"WHEN DONE: from tools.skynet_spam_guard import guarded_publish; "
        f"guarded_publish(dict(sender='{name}',topic='orchestrator',"
        f"type='result',content='YOUR_RESULT signed:{name}')). "
        f"Sign code: # signed: {name}. "
        f"Track subtasks with update_todo. "
        f"Check skynet_todos.py before idle. "
    )
    
    if task:
        preamble += f"\n\nTASK: {task}"
    
    return preamble


def build_invocation_stats():
    """Show size stats for all invocations."""
    stats = []
    for name in WORKER_NAMES:
        boot = build_boot_invocation(name)
        dispatch = build_dispatch_preamble(name, "example task")
        stats.append({
            "worker": name,
            "boot_chars": len(boot),
            "dispatch_chars": len(dispatch),
            "boot_lines": boot.count("\n") + 1,
            "boot_within_budget": len(boot) <= BOOT_TARGET,
            "dispatch_within_budget": len(dispatch) <= DISPATCH_TARGET,
        })
    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python tools/skynet_invocation.py boot alpha")
        print("  python tools/skynet_invocation.py dispatch beta")
        print("  python tools/skynet_invocation.py boot --all")
        print("  python tools/skynet_invocation.py --stats")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "--stats":
        stats = build_invocation_stats()
        print("=" * 60)
        print("  INVOCATION SIZE STATS")
        print("=" * 60)
        for s in stats:
            budget_ok = "✓" if s["boot_within_budget"] else "✗ OVER BUDGET"
            print(f"  {s['worker']:8s}  boot={s['boot_chars']:5d} chars ({s['boot_lines']:3d} lines) {budget_ok}")
            dispatch_ok = "✓" if s["dispatch_within_budget"] else "✗ OVER"
            print(f"           dispatch={s['dispatch_chars']:5d} chars {dispatch_ok}")
        print(f"\n  Budget: boot≤{BOOT_TARGET}, dispatch≤{DISPATCH_TARGET}")
        print(f"  Hard limit: 12,000 chars (MAX_DISPATCH_LENGTH)")
        return
    
    if len(sys.argv) < 3:
        print(f"Missing worker name. Usage: python tools/skynet_invocation.py {cmd} alpha")
        sys.exit(1)
    
    target = sys.argv[2]
    
    if cmd == "boot":
        if target == "--all":
            for name in WORKER_NAMES:
                print(f"\n{'='*60}")
                print(f"  BOOT INVOCATION — {name.upper()}")
                print(f"{'='*60}\n")
                print(build_boot_invocation(name))
                print(f"\n[{len(build_boot_invocation(name))} chars]")
        else:
            if target not in WORKER_NAMES:
                print(f"Unknown worker: {target}. Valid: {WORKER_NAMES}")
                sys.exit(1)
            invocation = build_boot_invocation(target)
            print(invocation)
            print(f"\n[{len(invocation)} chars]")
    
    elif cmd == "dispatch":
        if target not in WORKER_NAMES:
            print(f"Unknown worker: {target}. Valid: {WORKER_NAMES}")
            sys.exit(1)
        task = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        preamble = build_dispatch_preamble(target, task)
        print(preamble)
        print(f"\n[{len(preamble)} chars]")
    
    else:
        print(f"Unknown command: {cmd}. Use 'boot', 'dispatch', or '--stats'")
        sys.exit(1)


if __name__ == "__main__":
    main()
