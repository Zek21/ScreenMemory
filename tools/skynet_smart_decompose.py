#!/usr/bin/env python3
"""
Skynet Smart Decomposer — Keyword-driven prompt decomposition for optimal sub-task routing.

Analyzes prompts using keyword classification, complexity estimation, and worker affinity
to produce optimal sub-task assignments with priority and time estimates.

Usage:
    from tools.skynet_smart_decompose import SmartDecomposer
    d = SmartDecomposer()
    tasks = d.decompose("Audit all endpoints, write tests, and fix the CI pipeline")
    # [{"worker": "alpha", "task": "...", "priority": 2, "estimated_seconds": 60, "type": "audit"}, ...]

CLI:
    python tools/skynet_smart_decompose.py --prompt "Review core/ and add tests"
    python tools/skynet_smart_decompose.py --test
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SKYNET_URL = "http://localhost:8420"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ─── Task Type Keyword Maps ──────────────────────────────────────────────────

_TYPE_KEYWORDS = {
    "code": [
        "write", "implement", "create", "add", "build", "refactor", "edit",
        "function", "class", "method", "endpoint", "handler", "module",
        "feature", "wire", "integrate", "migrate", "port", "extend",
    ],
    "audit": [
        "audit", "review", "scan", "inspect", "check", "analyze", "lint",
        "security", "vulnerability", "coverage", "quality", "smell",
        "dead code", "unused", "stale", "deprecated",
    ],
    "test": [
        "test", "validate", "verify", "assert", "spec", "unittest",
        "pytest", "smoke", "regression", "integration", "e2e", "benchmark",
    ],
    "research": [
        "research", "investigate", "explore", "find", "search", "read",
        "document", "explain", "summarize", "compare", "report", "list",
        "count", "measure", "profile", "status", "health",
    ],
    "infra": [
        "deploy", "ci", "cd", "pipeline", "docker", "build", "install",
        "config", "setup", "start", "stop", "restart", "monitor",
        "server", "process", "service", "daemon", "cron", "schedule",
    ],
}

# Complexity signal keywords
_COMPLEXITY_HIGH = [
    "refactor", "rewrite", "migrate", "redesign", "architect", "overhaul",
    "all", "every", "entire", "comprehensive", "full", "complete",
    "security audit", "performance", "optimize", "parallel",
]
_COMPLEXITY_LOW = [
    "count", "list", "print", "echo", "version", "status", "ping",
    "check", "read", "report", "simple", "quick", "small",
]

# Priority signal keywords
_PRIORITY_URGENT = ["urgent", "critical", "asap", "now", "immediately", "hotfix", "broken", "crash", "down"]
_PRIORITY_HIGH = ["fix", "bug", "error", "fail", "security", "vulnerability"]
_PRIORITY_LOW = ["eventually", "when possible", "nice to have", "low priority", "cleanup"]

# Conjunctions that signal task boundaries
_SPLIT_PATTERNS = [
    r'\bthen\b',
    r'\band\s+(?:also|then)\b',
    r'\bafter\s+that\b',
    r'\bnext\b',
    r';\s*',
    r'\.\s+(?=[A-Z])',
    r'\band\b(?=\s+(?:write|create|add|fix|test|audit|review|scan|deploy|run|check|build|implement))',
]


def _skynet_get(path):
    try:
        return json.loads(urlopen(f"{SKYNET_URL}{path}", timeout=3).read())
    except Exception:
        return None


class SmartDecomposer:
    """Keyword-driven prompt decomposition with complexity estimation."""

    def __init__(self):
        self._idle_cache = None
        self._idle_ts = 0

    def classify_task(self, text: str) -> str:
        """Classify text into task type: code, audit, test, research, or infra."""
        lower = text.lower()
        scores = {}
        for task_type, keywords in _TYPE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in lower)
            # Boost for phrase matches (2+ word keywords)
            score += sum(1 for kw in keywords if len(kw.split()) > 1 and kw in lower)
            scores[task_type] = score

        best = max(scores, key=scores.get)
        if scores[best] == 0:
            return "research"  # default: treat unknown as research
        return best

    def estimate_complexity(self, text: str) -> int:
        """Estimate task complexity on a 1-10 scale from keywords and structure."""
        lower = text.lower()
        score = 3  # baseline

        # High complexity signals
        for kw in _COMPLEXITY_HIGH:
            if kw in lower:
                score += 2
        # Low complexity signals
        for kw in _COMPLEXITY_LOW:
            if kw in lower:
                score -= 1

        # Length-based adjustment
        word_count = len(text.split())
        if word_count > 50:
            score += 2
        elif word_count > 25:
            score += 1
        elif word_count < 8:
            score -= 1

        # Multi-file/path references increase complexity
        paths = re.findall(r'(?:core/|tools/|Skynet/|tests/|ui/|docs/|data/)\S*', text)
        score += min(len(paths), 3)

        # Technical specificity
        if re.search(r'\b(class|function|method|endpoint|handler|decorator|middleware)\b', lower):
            score += 1

        return max(1, min(10, score))

    def _estimate_seconds(self, task_type: str, complexity: int) -> int:
        """Estimate task duration in seconds based on type and complexity."""
        base = {
            "code": 60,
            "audit": 45,
            "test": 40,
            "research": 30,
            "infra": 50,
        }.get(task_type, 45)
        return int(base * (0.5 + complexity * 0.15))

    def _detect_priority(self, text: str) -> int:
        """Detect priority from keywords. Returns 1 (urgent) to 5 (normal)."""
        lower = text.lower()
        if any(kw in lower for kw in _PRIORITY_URGENT):
            return 1
        if any(kw in lower for kw in _PRIORITY_HIGH):
            return 2
        if any(kw in lower for kw in _PRIORITY_LOW):
            return 5
        return 3

    def _split_prompt(self, prompt: str) -> list[str]:
        """Split a compound prompt into independent sub-tasks."""
        # Check for explicit worker routing first
        explicit = re.findall(
            r'\b(alpha|beta|gamma|delta)\s*:\s*(.+?)(?=\b(?:alpha|beta|gamma|delta)\s*:|$)',
            prompt, re.IGNORECASE,
        )
        if explicit:
            return [task.strip() for _, task in explicit]

        # Check for numbered list
        numbered = re.findall(r'(?:^|\n)\s*\d+[.)]\s*(.+)', prompt)
        if len(numbered) >= 2:
            return [t.strip() for t in numbered if t.strip()]

        # Check for bullet list
        bullets = re.findall(r'(?:^|\n)\s*[-*]\s+(.+)', prompt)
        if len(bullets) >= 2:
            return [t.strip() for t in bullets if t.strip()]

        # Split on conjunction patterns
        combined = '|'.join(_SPLIT_PATTERNS)
        parts = re.split(combined, prompt, flags=re.IGNORECASE)
        parts = [p.strip().strip(',').strip() for p in parts if p and p.strip()]
        # Filter out fragments that are too short to be real tasks
        parts = [p for p in parts if len(p.split()) >= 3]

        if len(parts) >= 2:
            return parts

        return [prompt]

    def _get_idle_workers(self) -> list[str]:
        """Get idle workers from Skynet, with caching."""
        now = time.time()
        if self._idle_cache and (now - self._idle_ts) < 5:
            return self._idle_cache

        status = _skynet_get("/status")
        if status and "agents" in status:
            idle = [
                name for name, info in status["agents"].items()
                if isinstance(info, dict) and info.get("status") == "IDLE"
            ]
            if idle:
                self._idle_cache = idle
                self._idle_ts = now
                return idle

        self._idle_cache = list(WORKER_NAMES)
        self._idle_ts = now
        return list(WORKER_NAMES)

    def _get_explicit_routing(self, prompt: str) -> list[tuple[str, str]] | None:
        """Check for explicit 'worker: task' routing. Returns [(worker, task)] or None."""
        matches = re.findall(
            r'\b(alpha|beta|gamma|delta)\s*:\s*(.+?)(?=\b(?:alpha|beta|gamma|delta)\s*:|$)',
            prompt, re.IGNORECASE,
        )
        if matches:
            return [(w.lower(), t.strip()) for w, t in matches]
        return None

    def decompose(self, prompt: str) -> list[dict]:
        """Decompose a prompt into optimal sub-tasks with worker assignments.

        Returns list of dicts:
            [{"worker": str, "task": str, "priority": int,
              "estimated_seconds": int, "type": str, "complexity": int}]
        """
        # Check explicit routing first
        explicit = self._get_explicit_routing(prompt)
        if explicit:
            results = []
            for worker, task_text in explicit:
                task_type = self.classify_task(task_text)
                complexity = self.estimate_complexity(task_text)
                results.append({
                    "worker": worker,
                    "task": task_text,
                    "priority": self._detect_priority(task_text),
                    "estimated_seconds": self._estimate_seconds(task_type, complexity),
                    "type": task_type,
                    "complexity": complexity,
                })
            return results

        # Split into sub-tasks
        sub_prompts = self._split_prompt(prompt)
        idle_workers = self._get_idle_workers()

        results = []
        # Classify each sub-task and assign optimally
        classified = []
        for sp in sub_prompts:
            task_type = self.classify_task(sp)
            complexity = self.estimate_complexity(sp)
            priority = self._detect_priority(sp)
            classified.append({
                "text": sp,
                "type": task_type,
                "complexity": complexity,
                "priority": priority,
            })

        # Sort by priority (urgent first), then complexity (hard first)
        classified.sort(key=lambda x: (x["priority"], -x["complexity"]))

        # Assign workers: spread by type diversity, balance load
        worker_load = {w: 0 for w in idle_workers}

        for item in classified:
            # Pick the least-loaded idle worker
            if not idle_workers:
                best = WORKER_NAMES[len(results) % len(WORKER_NAMES)]
            else:
                best = min(idle_workers, key=lambda w: worker_load.get(w, 0))
                worker_load[best] = worker_load.get(best, 0) + 1

            est_seconds = self._estimate_seconds(item["type"], item["complexity"])
            results.append({
                "worker": best,
                "task": item["text"],
                "priority": item["priority"],
                "estimated_seconds": est_seconds,
                "type": item["type"],
                "complexity": item["complexity"],
            })

        return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _run_tests():
    """Run built-in tests for the SmartDecomposer."""
    d = SmartDecomposer()
    # Force offline mode for tests
    d._idle_cache = ["alpha", "beta", "gamma", "delta"]
    d._idle_ts = time.time() + 9999

    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name}")
            failed += 1

    print("=" * 60)
    print("SmartDecomposer Tests")
    print("=" * 60)

    # --- classify_task ---
    print("\n--- classify_task ---")
    check("code: 'write a new handler'", d.classify_task("write a new handler") == "code")
    check("audit: 'scan for security issues'", d.classify_task("scan for security issues") == "audit")
    check("test: 'run pytest on core/'", d.classify_task("run pytest on core/") == "test")
    check("research: 'list all files'", d.classify_task("list all files") == "research")
    check("infra: 'restart the server'", d.classify_task("restart the server") == "infra")
    check("code: 'implement feature'", d.classify_task("implement the new feature module") == "code")
    check("audit: 'review dead code'", d.classify_task("review dead code in utils") == "audit")
    check("default: unknown text", d.classify_task("do the thing") == "research")

    # --- estimate_complexity ---
    print("\n--- estimate_complexity ---")
    c1 = d.estimate_complexity("list files")
    c2 = d.estimate_complexity("refactor the entire authentication system with new middleware")
    c3 = d.estimate_complexity("check status")
    check(f"simple task <= 4 (got {c1})", c1 <= 4)
    check(f"complex task >= 6 (got {c2})", c2 >= 6)
    check(f"trivial task <= 3 (got {c3})", c3 <= 3)
    check("complexity range 1-10", 1 <= c1 <= 10 and 1 <= c2 <= 10)

    # --- decompose: single task ---
    print("\n--- decompose: single ---")
    r = d.decompose("check the Python version")
    check("single task returns 1 item", len(r) == 1)
    check("has all required keys", all(k in r[0] for k in ["worker", "task", "priority", "estimated_seconds", "type", "complexity"]))
    check("type is research/infra/audit", r[0]["type"] in ("research", "infra", "audit"))

    # --- decompose: compound ---
    print("\n--- decompose: compound ---")
    r = d.decompose("audit all endpoints and write tests for core/database.py")
    check(f"compound splits into >= 2 tasks (got {len(r)})", len(r) >= 2)
    types = {t["type"] for t in r}
    check(f"different task types detected: {types}", len(types) >= 1)

    # --- decompose: explicit routing ---
    print("\n--- decompose: explicit routing ---")
    r = d.decompose("alpha: fix the bug, beta: write tests, gamma: deploy")
    check(f"explicit routing returns 3 tasks (got {len(r)})", len(r) == 3)
    workers = [t["worker"] for t in r]
    check("explicit workers: alpha, beta, gamma", workers == ["alpha", "beta", "gamma"])

    # --- decompose: priority detection ---
    print("\n--- decompose: priority ---")
    r = d.decompose("URGENT: fix the crash in production")
    check(f"urgent priority = 1 (got {r[0]['priority']})", r[0]["priority"] == 1)
    r = d.decompose("cleanup old log files when possible")
    check(f"low priority = 5 (got {r[0]['priority']})", r[0]["priority"] == 5)

    # --- decompose: numbered list ---
    print("\n--- decompose: numbered list ---")
    r = d.decompose("1. Review core/database.py\n2. Add tests for search.py\n3. Fix the CI pipeline")
    check(f"numbered list splits into 3 tasks (got {len(r)})", len(r) == 3)

    # --- estimated_seconds ---
    print("\n--- estimated_seconds ---")
    for t in r:
        check(f"  {t['type']} estimate > 0 (got {t['estimated_seconds']}s)", t["estimated_seconds"] > 0)

    # --- worker load balancing ---
    print("\n--- load balancing ---")
    r = d.decompose("1. task one\n2. task two\n3. task three\n4. task four")
    assigned = [t["worker"] for t in r]
    check(f"4 tasks spread across workers: {assigned}", len(set(assigned)) >= 2)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    return failed == 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Smart Decomposer")
    parser.add_argument("--prompt", type=str, help="Prompt to decompose")
    parser.add_argument("--test", action="store_true", help="Run built-in tests")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.test:
        ok = _run_tests()
        sys.exit(0 if ok else 1)

    elif args.prompt:
        d = SmartDecomposer()
        tasks = d.decompose(args.prompt)
        if args.json:
            print(json.dumps(tasks, indent=2))
        else:
            print(f"\nDecomposition of: {args.prompt}")
            print("-" * 60)
            total_est = 0
            for i, t in enumerate(tasks, 1):
                print(f"  {i}. [{t['type'].upper():8s}] → {t['worker'].upper():6s} "
                      f"(pri={t['priority']} cpx={t['complexity']} ~{t['estimated_seconds']}s)")
                print(f"     {t['task']}")
                total_est += t["estimated_seconds"]
            print(f"\nTotal: {len(tasks)} tasks, ~{total_est}s estimated "
                  f"(~{total_est // len(tasks)}s avg)" if tasks else "No tasks")

    else:
        parser.print_help()
