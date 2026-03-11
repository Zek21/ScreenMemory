"""Skynet Smart Router -- keyword-aware task routing with performance scoring.

Detects task domain from keywords and ranks available workers by specialty
match combined with historical performance data from data/worker_performance.json.

Usage:
    python tools/skynet_smart_router.py route "build a dashboard panel"
    python tools/skynet_smart_router.py rank "fix backend crash"
    python tools/skynet_smart_router.py record alpha 12000 success "built dashboard"
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
PERF_FILE = ROOT / "data" / "worker_performance.json"
PROFILES_FILE = ROOT / "data" / "agent_profiles.json"

ALL_WORKERS = ["alpha", "beta", "gamma", "delta"]

# Keyword → domain mapping for task classification
KEYWORD_DOMAINS: dict[str, list[str]] = {
    "frontend": ["dashboard", "html", "css", "ui", "panel", "sidebar", "canvas", "chart",
                  "widget", "layout", "card", "modal", "overlay", "button", "display"],
    "backend": ["server", "endpoint", "api", "route", "http", "port", "proxy",
                "handler", "middleware", "request", "response", "cors", "socket"],
    "testing": ["test", "pytest", "assert", "unittest", "coverage", "validate",
                "verify", "spec", "fixture", "mock", "stub"],
    "architecture": ["refactor", "redesign", "architecture", "pattern", "module",
                     "structure", "organize", "decompose", "pipeline", "dag"],
    "documentation": ["doc", "readme", "markdown", "comment", "agents.md",
                      "protocol", "specification", "write-up", "report"],
    "code_edit": ["fix", "edit", "change", "update", "modify", "patch", "implement",
                  "build", "create", "add", "write", "code"],
    "code_review": ["review", "audit", "cross-validate", "inspect", "check",
                    "analyze", "scan", "evaluate", "critique"],
    "config": ["config", "json", "yaml", "settings", "environment", "env", ".json"],
    "monitoring": ["monitor", "watchdog", "daemon", "health", "heartbeat", "alert",
                   "status", "pulse", "sse", "stream"],
    "performance": ["performance", "benchmark", "latency", "throughput", "optimize",
                    "speed", "cache", "profil"],
    "Go": ["go ", "golang", ".go", "skynet.exe", "skynet/server"],
    "integration": ["integrate", "connect", "bridge", "wire", "link", "hook",
                    "pipe", "chain"],
    "visualization": ["chart", "graph", "sparkline", "bar", "plot", "render",
                      "canvas", "animation", "particle"],
    "validation": ["validate", "verify", "check", "assert", "lint", "syntax",
                   "compile", "ensure"],
    "auditing": ["audit", "compliance", "truth", "integrity", "security",
                 "incident", "forensic"],
}


def _load_performance() -> dict:
    """Load worker performance data from disk."""
    if PERF_FILE.exists():
        try:
            return json.loads(PERF_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "updated": None, "workers": {}}


def _save_performance(data: dict) -> None:
    """Atomically save performance data."""
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    PERF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PERF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PERF_FILE)


def detect_domains(task_text: str) -> dict[str, float]:
    """Detect task domains from keyword hits. Returns {domain: score}."""
    text = task_text.lower()
    hits: dict[str, float] = {}
    for domain, keywords in KEYWORD_DOMAINS.items():
        matches = sum(1 for kw in keywords if kw in text)
        if matches > 0:
            hits[domain] = min(1.0, matches * 0.3)
    return hits


def rank_workers(task_text: str, workers: Optional[list[str]] = None) -> list[dict]:
    """Rank workers by suitability for a task.

    Returns list of {worker, score, domains_matched, specialty_scores}
    sorted by descending score.
    """
    workers = workers or ALL_WORKERS
    domains = detect_domains(task_text)
    perf = _load_performance()
    perf_workers = perf.get("workers", {})

    rankings = []
    for w in workers:
        wp = perf_workers.get(w, {})
        specialties = wp.get("specialties", {})

        # Compute domain-weighted specialty score
        score = 0.0
        matched = {}
        for domain, weight in domains.items():
            specialty_val = specialties.get(domain, 0.3)  # default low if unknown
            contribution = weight * specialty_val
            score += contribution
            matched[domain] = round(specialty_val, 2)

        # Performance bonus: success rate
        completed = wp.get("tasks_completed", 0)
        failed = wp.get("tasks_failed", 0)
        total = completed + failed
        if total > 0:
            success_rate = completed / total
            score *= (0.8 + 0.2 * success_rate)  # up to 20% bonus

        # Normalize score
        if domains:
            score = score / max(1, len(domains))

        rankings.append({
            "worker": w,
            "score": round(score, 4),
            "domains_matched": matched,
            "tasks_completed": completed,
            "tasks_failed": failed,
        })

    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings


def route_task(task_text: str, workers: Optional[list[str]] = None) -> str:
    """Route a task to the best worker. Returns worker name."""
    ranked = rank_workers(task_text, workers)
    if not ranked:
        return "alpha"  # fallback
    return ranked[0]["worker"]


def record_metrics(
    worker: str,
    duration_ms: float,
    outcome: str,
    task_summary: str = "",
) -> dict:
    """Record task performance metrics for a worker.

    Args:
        worker: Worker name (alpha/beta/gamma/delta)
        duration_ms: Task duration in milliseconds
        outcome: 'success' or 'failure'
        task_summary: Brief task description
    """
    perf = _load_performance()
    workers = perf.setdefault("workers", {})
    wp = workers.setdefault(worker, {
        "tasks_completed": 0,
        "tasks_failed": 0,
        "avg_duration_ms": 0,
        "specialties": {},
        "recent_tasks": [],
    })

    if outcome == "success":
        wp["tasks_completed"] = wp.get("tasks_completed", 0) + 1
    else:
        wp["tasks_failed"] = wp.get("tasks_failed", 0) + 1

    # Rolling average duration
    total = wp.get("tasks_completed", 0) + wp.get("tasks_failed", 0)
    old_avg = wp.get("avg_duration_ms", 0)
    wp["avg_duration_ms"] = round(((old_avg * (total - 1)) + duration_ms) / total, 1) if total > 0 else duration_ms

    # Keep last 20 tasks
    recent = wp.setdefault("recent_tasks", [])
    recent.append({
        "task": task_summary[:120] if task_summary else "",
        "duration_ms": round(duration_ms, 1),
        "outcome": outcome,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    wp["recent_tasks"] = recent[-20:]

    # Update specialty scores based on outcome and detected domains
    domains = detect_domains(task_summary)
    for domain in domains:
        old_score = wp.get("specialties", {}).setdefault(domain, 0.5)
        if outcome == "success":
            wp["specialties"][domain] = min(1.0, round(old_score + 0.02, 4))
        else:
            wp["specialties"][domain] = max(0.1, round(old_score - 0.05, 4))

    _save_performance(perf)
    return {
        "worker": worker,
        "tasks_completed": wp["tasks_completed"],
        "tasks_failed": wp["tasks_failed"],
        "avg_duration_ms": wp["avg_duration_ms"],
    }


def get_leaderboard() -> list[dict]:
    """Return workers ranked by overall performance score."""
    perf = _load_performance()
    board = []
    for name, wp in perf.get("workers", {}).items():
        completed = wp.get("tasks_completed", 0)
        failed = wp.get("tasks_failed", 0)
        total = completed + failed
        success_rate = (completed / total * 100) if total > 0 else 0
        specialties = wp.get("specialties", {})
        top_specialties = sorted(specialties.items(), key=lambda x: x[1], reverse=True)[:3]
        board.append({
            "worker": name,
            "tasks_completed": completed,
            "tasks_failed": failed,
            "success_rate": round(success_rate, 1),
            "avg_duration_ms": wp.get("avg_duration_ms", 0),
            "top_specialties": [{"domain": d, "score": s} for d, s in top_specialties],
        })
    board.sort(key=lambda x: (x["tasks_completed"], x["success_rate"]), reverse=True)
    return board


def get_worker_performance(worker: str) -> Optional[dict]:
    """Get detailed performance data for a specific worker."""
    perf = _load_performance()
    wp = perf.get("workers", {}).get(worker)
    if not wp:
        return None
    completed = wp.get("tasks_completed", 0)
    failed = wp.get("tasks_failed", 0)
    total = completed + failed
    return {
        "worker": worker,
        "tasks_completed": completed,
        "tasks_failed": failed,
        "success_rate": round((completed / total * 100) if total > 0 else 0, 1),
        "avg_duration_ms": wp.get("avg_duration_ms", 0),
        "specialties": wp.get("specialties", {}),
        "recent_tasks": wp.get("recent_tasks", [])[-10:],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: skynet_smart_router.py <route|rank|record|leaderboard|worker> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "route":
        task = " ".join(sys.argv[2:]) or "general task"
        best = route_task(task)
        print(f"Best worker: {best}")
        ranked = rank_workers(task)
        for r in ranked:
            print(f"  {r['worker']}: {r['score']:.4f}  domains={r['domains_matched']}")

    elif cmd == "rank":
        task = " ".join(sys.argv[2:]) or "general task"
        ranked = rank_workers(task)
        for r in ranked:
            print(f"  {r['worker']}: {r['score']:.4f}  domains={r['domains_matched']}")

    elif cmd == "record":
        if len(sys.argv) < 5:
            print("Usage: skynet_smart_router.py record <worker> <duration_ms> <success|failure> [summary]")
            sys.exit(1)
        worker = sys.argv[2]
        dur = float(sys.argv[3])
        outcome = sys.argv[4]
        summary = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
        result = record_metrics(worker, dur, outcome, summary)
        print(json.dumps(result, indent=2))

    elif cmd == "leaderboard":
        board = get_leaderboard()
        print(json.dumps(board, indent=2))

    elif cmd == "worker":
        if len(sys.argv) < 3:
            print("Usage: skynet_smart_router.py worker <name>")
            sys.exit(1)
        data = get_worker_performance(sys.argv[2])
        if data:
            print(json.dumps(data, indent=2))
        else:
            print(f"No data for worker {sys.argv[2]}")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
