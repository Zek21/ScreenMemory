#!/usr/bin/env python3
"""
skynet_api.py -- Unified Orchestrator API Layer.

Single entry point for the orchestrator to get everything pre-digested.
Replaces raw bus polling, file reading, and manual decomposition with
clean, concise, ready-to-act intelligence.

Usage:
    python tools/skynet_api.py status              # Full system snapshot
    python tools/skynet_api.py think "goal"        # Difficulty + decomposition + assignments
    python tools/skynet_api.py digest              # Synthesized bus results summary
    python tools/skynet_api.py dispatch "goal"     # Full auto: think + dispatch
    python tools/skynet_api.py health              # Deep engine + learning + IQ health
"""

import argparse
import io
import json
import sys
import time

# Force UTF-8 output on Windows to avoid cp1252 crashes with Unicode arrows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

BUS_URL = "http://localhost:8420"
PROFILES_FILE = ROOT / "data" / "agent_profiles.json"
IQ_HISTORY = ROOT / "data" / "iq_history.json"
BRAIN_CONFIG = ROOT / "data" / "brain_config.json"
DISPATCH_LOG = ROOT / "data" / "dispatch_log.json"
LEARNER_STATE = ROOT / "data" / "learner_state.json"


# ─── Helpers ───────────────────────────────────────────

def _http_get(url: str, timeout: int = 5) -> Any:
    try:
        with urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, data: dict, timeout: int = 5) -> bool:
    try:
        req = Request(url, data=json.dumps(data).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _load_json(path: Path) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _fmt_uptime(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _worker_names() -> List[str]:
    return ["alpha", "beta", "gamma", "delta"]


def _get_profiles() -> dict:
    data = _load_json(PROFILES_FILE)
    if not data or not isinstance(data, dict):
        return {}
    return data


def _specialty_match(worker: str, domain_tags: List[str]) -> float:
    """Score how well a worker matches domain tags (0.0-1.0)."""
    # Map DAAORouter domain tags to worker specialization terms
    DOMAIN_ALIASES = {
        "system": ["backend", "infrastructure", "protocols", "crash-resilience", "monitoring", "Go"],
        "code": ["architecture", "frontend", "backend", "testing", "protocols", "api"],
        "web": ["frontend", "dashboard", "UI", "HTML", "CSS", "api", "endpoints"],
        "analysis": ["auditing", "validation", "testing", "documentation"],
        "finance": ["auditing", "validation"],
    }
    profiles = _get_profiles()
    prof = profiles.get(worker, {})
    specs = [s.lower() for s in prof.get("specializations", prof.get("specialties", []))]
    if not specs or not domain_tags:
        return 0.25  # neutral baseline

    # Expand domain tags with aliases
    expanded = set()
    for tag in domain_tags:
        expanded.add(tag.lower())
        for alias in DOMAIN_ALIASES.get(tag.lower(), []):
            expanded.add(alias.lower())

    matches = sum(1 for s in specs if s in expanded)
    return min(1.0, matches / max(len(specs), 1))


# ─── STATUS ────────────────────────────────────────────

def _status_workers(agents, profiles):
    """Build the workers table section. Returns list of lines."""
    lines = ["## Workers",
             "| Worker | State | Role | Tasks Done | Current |",
             "|--------|-------|------|-----------|---------|"]
    for name in _worker_names():
        agent = agents.get(name, {})
        prof = profiles.get(name, {})
        state = agent.get("status", "UNKNOWN")
        role = prof.get("role", "--")
        tasks = agent.get("tasks_completed", 0)
        current = agent.get("current_task", "--")[:60] or "--"
        lines.append(f"| {name.upper()} | {state} | {role} | {tasks} | {current} |")
    return lines


def _status_todos():
    """Build the TODOs section. Returns list of lines and (pending, active) counts."""
    lines = ["", "## Pending TODOs"]
    pending = active = 0
    try:
        from skynet_todos import get_summary
        summary = get_summary()
        pending = summary.get("pending", 0)
        active = summary.get("active", 0)
        lines.append(f"**Total:** {summary.get('total', 0)} | Pending: {pending} | Active: {active} | Done: {summary.get('done', 0)}")
        if pending + active > 0:
            for wname, wdata in summary.get("by_worker", {}).items():
                open_items = [i for i in wdata.get("items", []) if i.get("status") in ("pending", "active")]
                for item in open_items[:3]:
                    lines.append(f"  - **{wname.upper()}** [{item.get('priority','normal')}]: {item.get('task','?')[:80]}")
    except Exception as e:
        lines.append(f"  (TODO system unavailable: {e})")
    return lines, pending, active


def _status_results(msgs):
    """Build the recent results section. Returns list of lines."""
    lines = ["", "## Recent Results (last 10)"]
    results = [m for m in msgs if m.get("type") == "result" and m.get("topic") == "orchestrator"]
    for r in results[:10]:
        sender = r.get("sender", "?").upper()
        content = r.get("content", "")[:100]
        ts = r.get("timestamp", "")
        if "T" in ts:
            ts = ts.split("T")[1][:8]
        lines.append(f"  - [{ts}] **{sender}**: {content}")
    return lines


def _status_intelligence():
    """Build the intelligence health section. Returns list of lines."""
    lines = ["", "## Intelligence Health"]
    try:
        from core.learning_store import PersistentLearningSystem
        pls = PersistentLearningSystem()
        stats = pls.store.stats()
        lines.append(f"  - Learning Store: **{stats.get('total_facts', 0)}** facts, avg confidence {stats.get('average_confidence', 0):.2f}")
    except Exception:
        lines.append("  - Learning Store: unavailable")
    iq_data = _load_json(IQ_HISTORY)
    if iq_data and isinstance(iq_data, list) and len(iq_data) > 0:
        latest_iq = iq_data[-1].get("iq", 0)
        trend = "UP" if len(iq_data) >= 2 and iq_data[-1].get("iq", 0) > iq_data[-2].get("iq", 0) else "STABLE"
        lines.append(f"  - Collective IQ: **{latest_iq:.4f}** {trend} ({len(iq_data)} measurements)")
    learner = _load_json(LEARNER_STATE)
    if learner:
        lines.append(f"  - Learner: {learner.get('total_processed', 0)} results processed, {learner.get('total_learnings', 0)} facts extracted")
    return lines


def _status_recommendations(agents, msgs, pending, active):
    """Build the recommended actions section. Returns list of lines."""
    lines = ["", "## Recommended Actions"]
    idle = [n for n in _worker_names() if agents.get(n, {}).get("status") == "IDLE"]
    if idle and pending + active > 0:
        lines.append(f"  - [!] {len(idle)} idle workers with {pending + active} pending tasks -- dispatch immediately")
    elif idle:
        lines.append(f"  - [~] {len(idle)} idle workers, no pending tasks -- consider proposing improvements")
    alerts = [m for m in msgs if m.get("type") in ("alert", "monitor_alert")]
    if alerts:
        lines.append(f"  - [!] {len(alerts)} unread alerts on bus -- check for issues")
    return lines


def cmd_status() -> str:
    """Full system snapshot: workers, TODOs, unread results, intelligence health."""
    status = _http_get(f"{BUS_URL}/status")
    agents = status.get("agents", {}) if status else {}
    uptime = status.get("uptime_s", 0) if status else 0
    profiles = _get_profiles()
    msgs = _http_get(f"{BUS_URL}/bus/messages?limit=50") or []

    lines = ["# Skynet Status", "",
             f"**Backend:** {'ONLINE' if status else 'OFFLINE'} | Uptime: {_fmt_uptime(uptime)}", ""]
    lines.extend(_status_workers(agents, profiles))
    todo_lines, pending, active = _status_todos()
    lines.extend(todo_lines)
    lines.extend(_status_results(msgs))
    lines.extend(_status_intelligence())
    lines.extend(_status_recommendations(agents, msgs, pending, active))
    return "\n".join(lines)


# ─── THINK ─────────────────────────────────────────────

def _think_assessment(goal):
    """Build difficulty assessment section. Returns (lines, difficulty, domains, brain_or_none)."""
    lines = []
    try:
        from skynet_brain import SkynetBrain
        brain = SkynetBrain()
        assessment = brain.assess(goal)
        difficulty = assessment.get("difficulty", "UNKNOWN")
        confidence = assessment.get("confidence", 0)
        score = assessment.get("complexity_score", 0)
        domains = assessment.get("domain_tags", [])
        operator = assessment.get("operator", "DIRECT")
        lines.extend([
            "## Assessment",
            f"  - **Difficulty:** {difficulty} (confidence: {confidence:.0%}, score: {score:.2f})",
            f"  - **Operator:** {operator}",
            f"  - **Domains:** {', '.join(domains) if domains else 'general'}",
            "",
        ])
        return lines, difficulty, domains, brain
    except Exception as e:
        return [f"## Assessment\n  (Brain unavailable: {e})\n"], "UNKNOWN", [], None


def _think_learnings(goal):
    """Build relevant learnings section. Returns list of lines."""
    lines = ["## Relevant Learnings"]
    try:
        from core.learning_store import PersistentLearningSystem
        pls = PersistentLearningSystem()
        facts = pls.store.recall(goal, top_k=5)
        if facts:
            for i, fact in enumerate(facts, 1):
                conf = fact.confidence if hasattr(fact, "confidence") else 0
                cat = fact.category if hasattr(fact, "category") else "?"
                content = fact.content if hasattr(fact, "content") else str(fact)
                lines.append(f"  {i}. [{cat}] {content[:120]} (conf: {conf:.2f})")
        else:
            lines.append("  (no relevant learnings found)")
    except Exception as e:
        lines.append(f"  (LearningStore unavailable: {e})")
    lines.append("")
    return lines


def _think_suitability(domains):
    """Build worker suitability table. Returns (lines, scored_list, idle_workers)."""
    status = _http_get(f"{BUS_URL}/status")
    agents = status.get("agents", {}) if status else {}
    idle_workers = [n for n in _worker_names() if agents.get(n, {}).get("status") == "IDLE"]
    profiles = _get_profiles()

    lines = ["## Worker Suitability",
             "| Worker | State | Expertise Match | Specializations |",
             "|--------|-------|----------------|-----------------|"]
    scored = []
    for name in _worker_names():
        state = agents.get(name, {}).get("status", "UNKNOWN")
        match = _specialty_match(name, domains)
        specs = profiles.get(name, {}).get("specializations",
                profiles.get(name, {}).get("specialties", []))[:4]
        scored.append((name, state, match, specs))
        bar = "█" * int(match * 10) + "░" * (10 - int(match * 10))
        lines.append(f"| {name.upper()} | {state} | {bar} {match:.0%} | {', '.join(specs)} |")
    lines.append("")
    return lines, scored, idle_workers


def cmd_think(goal: str) -> str:
    """Assess difficulty, decompose, assign workers, inject learnings."""
    lines = ["# Skynet Think", f"**Goal:** {goal}", ""]

    assess_lines, difficulty, domains, brain = _think_assessment(goal)
    lines.extend(assess_lines)
    lines.extend(_think_learnings(goal))
    suit_lines, scored, idle_workers = _think_suitability(domains)
    lines.extend(suit_lines)

    # Task decomposition
    lines.append("## Task Decomposition")
    subtasks = []
    best = sorted([(n, s, m) for n, s, m, _ in scored if s == "IDLE"], key=lambda x: -x[2])
    try:
        if brain:
            plan = brain.think(goal)
            subtasks = plan.subtasks if hasattr(plan, "subtasks") else []
        if subtasks:
            for i, st in enumerate(subtasks, 1):
                worker = st.assigned_worker if hasattr(st, "assigned_worker") else "auto"
                task_text = st.task_text if hasattr(st, "task_text") else str(st)
                lines.append(f"  **{i}. -> {worker.upper()}:** {task_text[:200]}")
        else:
            lines.append("  Single task -- dispatch to best-matching idle worker")
            if best:
                lines.append(f"  **Recommended:** {best[0][0].upper()} (match: {best[0][2]:.0%})")
    except Exception as e:
        lines.append(f"  (Decomposition failed: {e})")
        if idle_workers:
            best = sorted(idle_workers, key=lambda w: -_specialty_match(w, domains))
            lines.append(f"  **Fallback recommendation:** {best[0].upper()}")
    lines.append("")

    # Dispatch commands
    lines.append("## Dispatch Commands")
    try:
        if subtasks and len(subtasks) > 1:
            lines.extend(["```bash", f'python tools/skynet_dispatch.py --smart --task "{goal}"',
                          "# Or for parallel dispatch:", f'python tools/skynet_api.py dispatch "{goal}"', "```"])
        else:
            best_worker = best[0][0] if best else (idle_workers[0] if idle_workers else "alpha")
            lines.extend(["```bash", f'python tools/skynet_dispatch.py --worker {best_worker} --task "{goal[:200]}"', "```"])
    except Exception:
        lines.extend(["```bash", f'python tools/skynet_dispatch.py --smart --task "{goal[:200]}"', "```"])

    return "\n".join(lines)


# ─── DIGEST ────────────────────────────────────────────

def _digest_worker_results(results):
    """Build per-worker results summary. Returns list of lines."""
    lines = [f"## Task Results ({len(results)} total)"]
    if not results:
        lines.append("  (no results on bus)")
        return lines
    by_worker = {}
    for r in results:
        by_worker.setdefault(r.get("sender", "unknown"), []).append(r)
    for worker, worker_results in sorted(by_worker.items()):
        lines.append(f"\n### {worker.upper()} ({len(worker_results)} results)")
        for r in worker_results[-5:]:
            content = r.get("content", "")[:120]
            ts = r.get("timestamp", "")
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            c_lower = content.lower()
            status = "[OK]" if any(w in c_lower for w in ["fixed", "created", "completed", "ok", "done", "success"]) else "[INFO]"
            if any(w in c_lower for w in ["failed", "error", "broken"]):
                status = "[FAIL]"
            lines.append(f"  {status} [{ts}] {content}")
    return lines


def _digest_patterns(results):
    """Detect patterns in results. Returns list of lines."""
    lines = ["\n## Patterns Detected"]
    if not results:
        lines.append("  (insufficient data)")
        return lines
    workers_active = set(r.get("sender", "") for r in results)
    lines.append(f"  - Active workers: {', '.join(w.upper() for w in sorted(workers_active))}")
    success_count = sum(1 for r in results
                       if any(w in r.get("content", "").lower() for w in ["fixed", "created", "completed", "ok"]))
    lines.append(f"  - Success rate: {success_count}/{len(results)} ({success_count/max(len(results),1)*100:.0f}%)")
    from skynet_learner import categorize_task
    domains = {}
    for r in results:
        cat, _ = categorize_task(r.get("content", ""))
        domains[cat] = domains.get(cat, 0) + 1
    if domains:
        top_domains = sorted(domains.items(), key=lambda x: -x[1])[:5]
        lines.append(f"  - Top domains: {', '.join(f'{d}({c})' for d, c in top_domains)}")
    return lines


def _digest_remaining():
    """Build what-remains section. Returns list of lines."""
    lines = ["\n## What Remains"]
    try:
        from skynet_todos import get_summary
        summary = get_summary()
        pending = summary.get("pending", 0)
        active = summary.get("active", 0)
        if pending + active > 0:
            lines.append(f"  - **{pending + active}** open TODO items across workers")
            for wname, wdata in summary.get("by_worker", {}).items():
                open_items = [i for i in wdata.get("items", []) if i.get("status") in ("pending", "active")]
                for item in open_items[:3]:
                    lines.append(f"    - {wname.upper()}: {item.get('task', '?')[:80]}")
        else:
            lines.append("  - All TODOs complete [OK]")
    except Exception:
        lines.append("  - (TODO system unavailable)")
    return lines


def cmd_digest() -> str:
    """Synthesized summary of recent bus results."""
    msgs = _http_get(f"{BUS_URL}/bus/messages?limit=100") or []
    results = [m for m in msgs if m.get("type") == "result"]
    alerts = [m for m in msgs if m.get("type") in ("alert", "monitor_alert")]

    lines = ["# Skynet Digest", ""]
    lines.extend(_digest_worker_results(results))
    if alerts:
        lines.append(f"\n## Alerts ({len(alerts)})")
        for a in alerts[-10:]:
            lines.append(f"  [!] [{a.get('sender', '?')}] {a.get('content', '')[:120]}")
    lines.extend(_digest_patterns(results))
    lines.extend(_digest_remaining())
    return "\n".join(lines)


# ─── DISPATCH ──────────────────────────────────────────

def _dispatch_subtasks(subtasks, idle_workers):
    """Dispatch multiple subtasks to workers. Returns list of lines."""
    from skynet_dispatch import dispatch_to_worker, enrich_task
    lines = ["## Dispatching Subtasks"]
    dispatched = 0
    for i, st in enumerate(subtasks):
        worker = st.assigned_worker if hasattr(st, "assigned_worker") else idle_workers[i % len(idle_workers)]
        task_text = st.task_text if hasattr(st, "task_text") else str(st)
        try:
            enriched = enrich_task(worker, task_text)
            ok = dispatch_to_worker(worker, enriched)
            lines.append(f"  {'[OK]' if ok else '[FAIL]'} **{worker.upper()}**: {task_text[:100]}")
            if ok:
                dispatched += 1
        except Exception as e:
            lines.append(f"  [FAIL] **{worker.upper()}**: dispatch failed ({e})")
    lines.append(f"\n**Dispatched:** {dispatched}/{len(subtasks)}")
    return lines


def _dispatch_single(goal, idle_workers, brain):
    """Smart-dispatch a single goal. Returns list of lines."""
    from skynet_dispatch import dispatch_to_worker, enrich_task, smart_dispatch
    lines = ["## Smart Dispatch"]
    try:
        result = smart_dispatch(goal)
        if isinstance(result, dict):
            for worker, ok in result.items():
                lines.append(f"  {'[OK]' if ok else '[FAIL]'} **{worker.upper()}**: {goal[:100]}")
        else:
            lines.append(f"  Result: {result}")
    except Exception:
        try:
            assessment = brain.assess(goal) if brain else {}
            domains = assessment.get("domain_tags", [])
        except Exception:
            domains = []
        best = sorted(idle_workers, key=lambda w: -_specialty_match(w, domains))
        worker = best[0]
        try:
            enriched = enrich_task(worker, goal)
            ok = dispatch_to_worker(worker, enriched)
            lines.append(f"  {'[OK]' if ok else '[FAIL]'} **{worker.upper()}**: {goal[:100]}")
        except Exception as e2:
            lines.append(f"  [FAIL] Dispatch failed: {e2}")
    return lines


def cmd_dispatch(goal: str) -> str:
    """Full auto: think + decompose + dispatch to workers."""
    lines = ["# Skynet Auto-Dispatch", f"**Goal:** {goal}", ""]

    status = _http_get(f"{BUS_URL}/status")
    agents = status.get("agents", {}) if status else {}
    idle_workers = [n for n in _worker_names() if agents.get(n, {}).get("status") == "IDLE"]

    if not idle_workers:
        lines.append("[!] **No idle workers available.** Task queued for next available worker.")
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "skynet_api", "topic": "orchestrator", "type": "info",
                "content": f"AUTO_DISPATCH_QUEUED: No idle workers for: {goal[:100]}"
            })
        except ImportError:
            _http_post(f"{BUS_URL}/bus/publish", {
                "sender": "skynet_api", "topic": "orchestrator", "type": "info",
                "content": f"AUTO_DISPATCH_QUEUED: No idle workers for: {goal[:100]}"
            })
        # signed: gamma
        return "\n".join(lines)

    brain = None
    subtasks = []
    try:
        from skynet_brain import SkynetBrain
        brain = SkynetBrain()
        plan = brain.think(goal)
        subtasks = plan.subtasks if hasattr(plan, "subtasks") else []
        difficulty = plan.difficulty if hasattr(plan, "difficulty") else "UNKNOWN"
        lines.extend([f"**Difficulty:** {difficulty}", f"**Subtasks:** {len(subtasks)}",
                      f"**Idle workers:** {', '.join(w.upper() for w in idle_workers)}", ""])
    except Exception as e:
        lines.append(f"Brain unavailable ({e}), using direct dispatch")

    if subtasks and len(subtasks) > 1:
        lines.extend(_dispatch_subtasks(subtasks, idle_workers))
    else:
        lines.extend(_dispatch_single(goal, idle_workers, brain))
    return "\n".join(lines)


# ─── HEALTH ────────────────────────────────────────────

def _health_engines():
    """Probe engine instantiation health. Returns list of lines."""
    engines_to_test = [
        ("DAAORouter", "core.difficulty_router", "DAAORouter"),
        ("LearningStore", "core.learning_store", "LearningStore"),
        ("PersistentLearningSystem", "core.learning_store", "PersistentLearningSystem"),
        ("SelfEvolutionSystem", "core.self_evolution", "SelfEvolutionSystem"),
        ("HybridRetriever", "core.hybrid_retrieval", "HybridRetriever"),
        ("InputGuard", "core.input_guard", "InputGuard"),
    ]
    lines = ["## Engine Health",
             "| Engine | Status | Detail |",
             "|--------|--------|--------|"]
    for label, module, cls_name in engines_to_test:
        try:
            mod = __import__(module, fromlist=[cls_name])
            cls = getattr(mod, cls_name)
            instance = cls()
            lines.append(f"| {label} | [OK] online | instantiated OK |")
            del instance
        except Exception as e:
            err = str(e)[:60]
            try:
                __import__(module, fromlist=[cls_name])
                lines.append(f"| {label} | [!] available | import OK, init failed: {err} |")
            except Exception:
                lines.append(f"| {label} | [FAIL] offline | {err} |")
    lines.append("")
    return lines


def _health_learning_store():
    """Probe learning store stats. Returns list of lines."""
    lines = ["## Learning Store"]
    try:
        from core.learning_store import PersistentLearningSystem
        pls = PersistentLearningSystem()
        stats = pls.store.stats()
        lines.append(f"  - Total facts: **{stats.get('total_facts', 0)}**")
        lines.append(f"  - Avg confidence: {stats.get('average_confidence', 0):.2f}")
        by_cat = stats.get("by_category", {})
        if by_cat:
            cats = sorted(by_cat.items(), key=lambda x: -x[1])[:6]
            lines.append(f"  - Categories: {', '.join(f'{c}({n})' for c, n in cats)}")
        top_domains = pls.expertise.strongest_domains(5)
        if top_domains:
            lines.append(f"  - Top expertise: {', '.join(f'{d}({s:.1f})' for d, s in top_domains)}")
    except Exception as e:
        lines.append(f"  (unavailable: {e})")
    lines.append("")
    return lines


def _health_iq():
    """Build IQ trend section. Returns list of lines."""
    lines = ["## Collective IQ"]
    iq_data = _load_json(IQ_HISTORY)
    if iq_data and isinstance(iq_data, list):
        latest = iq_data[-1].get("iq", 0)
        first = iq_data[0].get("iq", 0)
        delta = latest - first
        trend = "UP improving" if delta > 0.01 else ("DOWN declining" if delta < -0.01 else "STABLE")
        lines.append(f"  - Current: **{latest:.4f}** | Initial: {first:.4f} | Change: {delta:+.4f} {trend}")
        lines.append(f"  - Measurements: {len(iq_data)}")
        if len(iq_data) >= 10:
            last10_avg = sum(e.get("iq", 0) for e in iq_data[-10:]) / 10
            lines.append(f"  - Last 10 avg: {last10_avg:.4f}")
    else:
        lines.append("  (no IQ history)")
    lines.append("")
    return lines


def _health_workers(status):
    """Build worker responsiveness section. Returns list of lines."""
    lines = ["## Worker Responsiveness"]
    if status:
        agents = status.get("agents", {})
        for name in _worker_names():
            agent = agents.get(name, {})
            state = agent.get("status", "UNKNOWN")
            icon = "[OK]" if state in ("IDLE", "PROCESSING") else "[!]"
            lines.append(f"  {icon} **{name.upper()}**: {state}")
    lines.append("")
    return lines


def _health_learner_daemon():
    """Build learner daemon section. Returns list of lines."""
    lines = ["## Learner Daemon"]
    learner = _load_json(LEARNER_STATE)
    if learner:
        lines.extend([
            f"  - Processed: {learner.get('total_processed', 0)} results",
            f"  - Learnings extracted: {learner.get('total_learnings', 0)}",
            f"  - Evolution updates: {learner.get('total_evolution_updates', 0)}",
            f"  - Last run: {learner.get('last_run', 'never')}",
        ])
    else:
        lines.append("  (not running or no state file)")
    return lines


def _health_dispatch_stats():
    """Build dispatch statistics section. Returns list of lines."""
    dispatch = _load_json(DISPATCH_LOG)
    if dispatch and isinstance(dispatch, list):
        successes = sum(1 for d in dispatch if d.get("success"))
        results_received = sum(1 for d in dispatch if d.get("result_received"))
        return [
            f"\n## Dispatch Stats",
            f"  - Total dispatches: {len(dispatch)}",
            f"  - Success rate: {successes}/{len(dispatch)} ({successes/max(len(dispatch),1)*100:.0f}%)",
            f"  - Results received: {results_received}/{len(dispatch)}",
        ]
    return []


def cmd_health() -> str:
    """Deep health check: engines, learning, IQ, workers."""
    status = _http_get(f"{BUS_URL}/status")
    lines = ["# Skynet Health Report", "",
             f"## Backend: {'[OK] ONLINE' if status else '[FAIL] OFFLINE'}"]
    if status:
        lines.append(f"  Uptime: {_fmt_uptime(status.get('uptime_s', 0))}")
    lines.append("")
    lines.extend(_health_engines())
    lines.extend(_health_learning_store())
    lines.extend(_health_iq())
    lines.extend(_health_workers(status))
    lines.extend(_health_learner_daemon())
    lines.extend(_health_dispatch_stats())
    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Unified Orchestrator API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Commands:
  status              Full system snapshot
  think "goal"        Difficulty + decomposition + assignments
  digest              Synthesized bus results summary
  dispatch "goal"     Full auto: think + dispatch to workers
  health              Deep engine + learning + IQ health check""",
    )
    parser.add_argument("command", nargs="?",
                        choices=["status", "think", "digest", "dispatch", "health"],
                        help="Command to execute")
    parser.add_argument("goal", nargs="?", default="", help="Goal text for think/dispatch")
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "status":
        print(cmd_status())
    elif args.command == "think":
        if not args.goal:
            print("Error: 'think' requires a goal argument")
            sys.exit(1)
        print(cmd_think(args.goal))
    elif args.command == "digest":
        print(cmd_digest())
    elif args.command == "dispatch":
        if not args.goal:
            print("Error: 'dispatch' requires a goal argument")
            sys.exit(1)
        print(cmd_dispatch(args.goal))
    elif args.command == "health":
        print(cmd_health())


if __name__ == "__main__":
    main()
