#!/usr/bin/env python3
"""skynet_autonomous_worker.py — Self-directing worker loop for Skynet.

When a worker finishes all dispatched tasks and has nothing in its queue,
this module drives autonomous task discovery, claiming, execution tracking,
and self-generated improvement work.  It integrates with:

  - skynet_todos.py    — TODO CRUD, claim, can_stop
  - skynet_worker_poll — multi-source work discovery (task queue, bus, Go queue)
  - skynet_scoring     — score loading, autonomous-pull awards
  - skynet_spam_guard  — bus publishing (guarded)
  - skynet_knowledge   — learning broadcast
  - agent_profiles     — worker specialties for task matching

Priority order for find_next_task():
  1. Assigned TODOs (status=pending/active, assignee=self)
  2. Claimable shared TODOs (unassigned, status=pending) — highest priority first
  3. Bus requests (topic=workers mentioning this worker)
  4. Go backend task queue (/bus/tasks unclaimed)
  5. Self-generated improvement tasks (codebase scan for real issues)

CLI:
    python tools/skynet_autonomous_worker.py --worker alpha --dry-run
    python tools/skynet_autonomous_worker.py --worker beta --once
    python tools/skynet_autonomous_worker.py --generate-todos
    python tools/skynet_autonomous_worker.py --status alpha

# signed: delta
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))

WORKER_NAMES = ("alpha", "beta", "gamma", "delta")
LOOP_INTERVAL_S = 10  # seconds between poll cycles
IDLE_IMPROVEMENT_INTERVAL_S = 300  # generate improvement task every 5 min idle

# Worker specialty map — used for task matching when self-generating
WORKER_SPECIALTIES = {
    "alpha": ["frontend", "builder", "ui", "dashboard", "html", "css", "javascript"],
    "beta": ["protocol", "infrastructure", "boot", "daemon", "startup", "powershell"],
    "gamma": ["wiring", "documentation", "integration", "bus", "persistence", "go"],
    "delta": ["testing", "validation", "audit", "security", "architecture", "review"],
}


def _load_json(path: Path, default=None):
    """Load JSON safely, return default on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default if default is not None else {}


def _log(worker: str, msg: str, level: str = "INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{worker.upper()}] [{level}] {msg}")


class AutonomousWorker:
    """Self-directing worker that finds, claims, and tracks tasks autonomously.

    Integrates with Skynet's TODO system, bus, scoring, and knowledge layers
    to keep workers productive when no orchestrator dispatch is pending.
    """

    def __init__(self, worker_name: str):
        if worker_name.lower() not in WORKER_NAMES:
            raise ValueError(f"Unknown worker: {worker_name}. Must be one of {WORKER_NAMES}")
        self.name = worker_name.lower()
        self.specialties = WORKER_SPECIALTIES.get(self.name, [])
        self.score = self._load_score()
        self.current_task_id: Optional[str] = None
        self.tasks_completed: int = 0
        self.tasks_claimed: int = 0
        self.last_improvement_time: float = 0.0
        self._idle_since: float = 0.0

    # ── Score & Profile ──────────────────────────────────────────────

    def _load_score(self) -> float:
        """Load this worker's current score from worker_scores.json."""
        try:
            from tools.skynet_scoring import get_score
            s = get_score(self.name)
            return s.get("total", 0.0) if s else 0.0
        except Exception:
            data = _load_json(DATA_DIR / "worker_scores.json")
            scores = data.get("scores", {})
            entry = scores.get(self.name, {})
            return entry.get("total", 0.0)

    def _load_profile(self) -> dict:
        """Load agent profile for specialty matching."""
        data = _load_json(DATA_DIR / "agent_profiles.json")
        return data.get(self.name, {}) if isinstance(data.get(self.name), dict) else {}

    # ── Task Discovery ───────────────────────────────────────────────

    def find_next_task(self) -> Optional[dict]:
        """Find the highest-priority task from all sources.

        Priority:
          1. Assigned TODOs (pending/active, assignee=self)
          2. Claimable shared TODOs (highest priority first)
          3. Bus requests targeting this worker
          4. Go backend task queue
          5. None (caller should self-generate)
        """
        # Source 1: Assigned TODOs
        task = self._check_assigned_todos()
        if task:
            _log(self.name, f"Found assigned TODO: [{task['id']}] {task['task'][:60]}")
            return task

        # Source 2: Claimable shared TODOs
        task = self._check_claimable_todos()
        if task:
            _log(self.name, f"Found claimable TODO: [{task['id']}] {task['task'][:60]}")
            return task

        # Source 3 & 4: Bus requests + Go queue (via poll_for_work)
        task = self._check_bus_and_queue()
        if task:
            _log(self.name, f"Found bus/queue task: {task.get('task','')[:60]}")
            return task

        return None

    def _check_assigned_todos(self) -> Optional[dict]:
        """Check for TODOs directly assigned to this worker."""
        try:
            from tools.skynet_todos import list_todos
            items = list_todos(self.name, status=None)
            pending = [t for t in items if t.get("status") in ("pending", "active")]
            if not pending:
                return None
            # Sort: active first, then by priority
            priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
            pending.sort(key=lambda t: (
                0 if t.get("status") == "active" else 1,
                priority_rank.get(t.get("priority", "normal"), 2),
            ))
            item = pending[0]
            return {
                "id": item.get("id", ""),
                "task": item.get("title", item.get("task", "")),
                "priority": item.get("priority", "normal"),
                "source": "assigned_todo",
                "status": item.get("status", "pending"),
            }
        except Exception as e:
            _log(self.name, f"Error checking assigned TODOs: {e}", "WARN")
            return None

    def _check_claimable_todos(self) -> Optional[dict]:
        """Check for shared/unassigned TODOs this worker can claim.

        Prefers tasks matching worker specialties when multiple are available.
        """
        try:
            from tools.skynet_todos import list_todos, SHARED_ASSIGNEES
            items = list_todos(self.name, include_claimable=True)
            # Filter to only shared/unassigned pending items
            _todo_target = lambda t: str(
                t.get("assignee", t.get("worker", "")) or ""
            ).strip().lower()
            claimable = [
                t for t in items
                if t.get("status") == "pending"
                and _todo_target(t) in SHARED_ASSIGNEES
            ]
            if not claimable:
                return None

            # Score each by: priority + specialty match
            priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}

            def _specialty_score(t):
                text = (t.get("title", "") + " " + t.get("task", "")).lower()
                return sum(1 for s in self.specialties if s in text)

            claimable.sort(key=lambda t: (
                priority_rank.get(t.get("priority", "normal"), 2),
                -_specialty_score(t),  # more matches = better (negative for ascending)
                t.get("created_at", ""),  # oldest first
            ))
            item = claimable[0]
            return {
                "id": item.get("id", ""),
                "task": item.get("title", item.get("task", "")),
                "priority": item.get("priority", "normal"),
                "source": "claimable_todo",
                "status": item.get("status", "pending"),
                "specialty_match": _specialty_score(item),
            }
        except Exception as e:
            _log(self.name, f"Error checking claimable TODOs: {e}", "WARN")
            return None

    def _check_bus_and_queue(self) -> Optional[dict]:
        """Check bus messages and Go backend queue for pending work."""
        try:
            from tools.skynet_worker_poll import poll_for_work
            result = poll_for_work(self.name)
            if not result.get("has_work"):
                return None

            # Priority: bus_requests > directives > queued_tasks > pending_tasks
            for source_key in ("directives", "bus_requests", "queued_tasks", "pending_tasks"):
                items = result.get(source_key, [])
                if items:
                    item = items[0]
                    return {
                        "id": item.get("id", item.get("task_id", uuid.uuid4().hex[:8])),
                        "task": item.get("task", item.get("content", "")),
                        "priority": item.get("priority", "normal"),
                        "source": source_key,
                        "sender": item.get("sender", ""),
                    }
            return None
        except Exception as e:
            _log(self.name, f"Error polling bus/queue: {e}", "WARN")
            return None

    # ── Task Claiming ────────────────────────────────────────────────

    def claim_task(self, task: dict) -> bool:
        """Atomically claim a task — set assignee, post to bus.

        For TODOs: updates assignee + status in todos.json.
        For bus/queue tasks: posts claim to bus so others don't duplicate.
        Returns True if claim succeeded.
        """
        task_id = task.get("id", "")
        source = task.get("source", "")

        if source in ("assigned_todo", "claimable_todo"):
            return self._claim_todo(task_id)
        else:
            return self._claim_bus_task(task)

    def _claim_todo(self, todo_id: str) -> bool:
        """Claim a TODO via skynet_todos.claim_todo."""
        try:
            from tools.skynet_todos import claim_todo
            result = claim_todo(todo_id, self.name)
            if result:
                self.current_task_id = todo_id
                self.tasks_claimed += 1
                self._announce_claim(todo_id, result.get("task", ""))
                _log(self.name, f"Claimed TODO [{todo_id}]")
                return True
            _log(self.name, f"Failed to claim TODO [{todo_id}] — may be taken", "WARN")
            return False
        except Exception as e:
            _log(self.name, f"Error claiming TODO [{todo_id}]: {e}", "WARN")
            return False

    def _claim_bus_task(self, task: dict) -> bool:
        """Announce claim of a bus/queue task to prevent duplication."""
        task_id = task.get("id", uuid.uuid4().hex[:8])
        self.current_task_id = task_id
        self.tasks_claimed += 1
        self._announce_claim(task_id, task.get("task", ""))
        _log(self.name, f"Claimed bus task [{task_id}]")
        return True

    def _announce_claim(self, task_id: str, task_desc: str):
        """Post task_claim to bus so other workers don't duplicate."""
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": self.name,
                "topic": "workers",
                "type": "task_claim",
                "content": f"CLAIMED: [{task_id}] {task_desc[:100]} signed:{self.name}",
            })
        except Exception:
            pass  # Bus unavailable — claim is still local

    # ── Task Completion ──────────────────────────────────────────────

    def complete_task(self, task_id: str, result: str, success: bool = True):
        """Mark task done, award score, broadcast learning, find next task.

        Args:
            task_id: The ID of the completed task.
            result: Brief result summary.
            success: Whether the task succeeded.
        """
        # Update TODO status
        self._mark_todo_done(task_id, result)

        # Award score for autonomous pull
        if success:
            self._award_autonomous_pull(task_id)

        # Post result to bus
        self._post_result(task_id, result, success)

        # Broadcast learning
        self._broadcast_learning(task_id, result)

        self.tasks_completed += 1
        self.current_task_id = None
        self.score = self._load_score()  # refresh

        _log(self.name, f"Completed [{task_id}] (total={self.tasks_completed})")

    def _mark_todo_done(self, task_id: str, result: str):
        """Mark a TODO as done in todos.json."""
        try:
            from tools.skynet_todos import mark_done, update_status
            item = mark_done(self.name, task_id)
            if not item:
                # Try without worker filter (shared tasks)
                update_status(task_id, "done", completed_by=self.name)
        except Exception as e:
            _log(self.name, f"Error marking [{task_id}] done: {e}", "WARN")

    def _award_autonomous_pull(self, task_id: str):
        """Award autonomous pull points via scoring system."""
        try:
            from tools.skynet_scoring import award_autonomous_pull
            award_autonomous_pull(self.name, task_id, validator=self.name)
        except Exception as e:
            _log(self.name, f"Score award failed: {e}", "WARN")

    def _post_result(self, task_id: str, result: str, success: bool):
        """Post completion result to bus."""
        try:
            from tools.skynet_spam_guard import guarded_publish
            status = "DONE" if success else "FAILED"
            guarded_publish({
                "sender": self.name,
                "topic": "orchestrator",
                "type": "result",
                "content": (
                    f"AUTONOMOUS_{status}: [{task_id}] {result[:200]} "
                    f"signed:{self.name}"
                ),
            })
        except Exception:
            pass

    def _broadcast_learning(self, task_id: str, result: str):
        """Share what was learned during the task."""
        try:
            from tools.skynet_knowledge import broadcast_learning
            broadcast_learning(
                self.name,
                f"Autonomous task [{task_id}]: {result[:150]}",
                "pattern",
                ["autonomous", "self-directed"],
            )
        except Exception:
            pass

    # ── Improvement Generation ───────────────────────────────────────

    def generate_improvement_task(self) -> Optional[dict]:
        """Scan codebase for real improvement opportunities matching specialties.

        Returns a task dict or None if nothing found.
        Uses static analysis of the codebase to find genuine issues.
        """
        now = time.time()
        if now - self.last_improvement_time < IDLE_IMPROVEMENT_INTERVAL_S:
            return None
        self.last_improvement_time = now

        generators = [
            self._find_missing_tests,
            self._find_stale_data_files,
            self._find_undocumented_modules,
            self._find_todo_comments,
        ]

        for gen in generators:
            task = gen()
            if task:
                return task
        return None

    def _find_missing_tests(self) -> Optional[dict]:
        """Find Python modules in tools/ that lack corresponding test files."""
        tools_dir = ROOT / "tools"
        if not tools_dir.exists():
            return None

        for py_file in sorted(tools_dir.glob("skynet_*.py")):
            if py_file.name.startswith("test_"):
                continue
            test_name = f"test_{py_file.stem}.py"
            test_file = tools_dir / test_name
            if not test_file.exists():
                # Check if any test file covers this module
                has_coverage = any(
                    (tools_dir / f).exists()
                    for f in [test_name, f"test_{py_file.stem.replace('skynet_','')}.py"]
                )
                if not has_coverage:
                    return self._make_improvement_task(
                        f"Write tests for {py_file.name}",
                        f"Module tools/{py_file.name} has no test file. "
                        f"Create tools/{test_name} with unit tests for core functions.",
                        "high",
                    )
        return None

    def _find_stale_data_files(self) -> Optional[dict]:
        """Find data/ files that haven't been updated in >24h and may be stale."""
        data_dir = ROOT / "data"
        if not data_dir.exists():
            return None

        stale_threshold = time.time() - 86400  # 24 hours
        for json_file in sorted(data_dir.glob("*.json")):
            try:
                mtime = json_file.stat().st_mtime
                if mtime < stale_threshold:
                    # Only flag operational state files, not config
                    if json_file.name in (
                        "worker_health.json", "realtime.json",
                        "dispatch_log.json", "bus_archive.jsonl",
                    ):
                        return self._make_improvement_task(
                            f"Investigate stale data/{json_file.name}",
                            f"data/{json_file.name} hasn't been updated in >24h "
                            f"(mtime: {time.ctime(mtime)}). Check if the "
                            f"producing daemon is running and healthy.",
                            "normal",
                        )
            except OSError:
                continue
        return None

    def _find_undocumented_modules(self) -> Optional[dict]:
        """Find Python modules lacking docstrings."""
        tools_dir = ROOT / "tools"
        for py_file in sorted(tools_dir.glob("skynet_*.py")):
            if py_file.name.startswith("test_"):
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
                lines = text.split("\n", 5)
                # Check if file starts with a docstring (triple-quote within first 3 lines)
                has_docstring = any('"""' in line or "'''" in line for line in lines[:3])
                if not has_docstring:
                    return self._make_improvement_task(
                        f"Add module docstring to {py_file.name}",
                        f"tools/{py_file.name} is missing a module-level docstring. "
                        f"Add documentation explaining purpose, API, and CLI usage.",
                        "low",
                    )
            except OSError:
                continue
        return None

    def _find_todo_comments(self) -> Optional[dict]:
        """Find TODO/FIXME/HACK comments in source code."""
        tools_dir = ROOT / "tools"
        markers = ("# TODO:", "# FIXME:", "# HACK:", "# XXX:")
        for py_file in sorted(tools_dir.glob("skynet_*.py")):
            if py_file.name.startswith("test_"):
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    for marker in markers:
                        if stripped.startswith(marker) or f" {marker}" in stripped:
                            comment = stripped[stripped.index(marker) + len(marker):].strip()
                            if len(comment) > 10:  # skip trivial TODOs
                                return self._make_improvement_task(
                                    f"Address {marker.strip('# :')} in {py_file.name}:{i}",
                                    f"Found {marker.strip()} at tools/{py_file.name}:{i}: "
                                    f"{comment[:120]}",
                                    "normal",
                                )
            except OSError:
                continue
        return None

    def _make_improvement_task(self, title: str, description: str,
                               priority: str) -> dict:
        """Create an improvement task dict and add it to todos.json."""
        task_id = uuid.uuid4().hex[:8]
        task = {
            "id": task_id,
            "task": f"{title}: {description}",
            "title": title,
            "priority": priority,
            "source": "self_generated",
            "generated_by": self.name,
        }

        # Persist to todos.json
        try:
            from tools.skynet_todos import add_todo
            added = add_todo(self.name, task["task"], priority)
            task["id"] = added["id"]
        except Exception as e:
            _log(self.name, f"Failed to persist improvement task: {e}", "WARN")

        return task

    # ── Status & Introspection ───────────────────────────────────────

    def status(self) -> dict:
        """Return current autonomous worker state."""
        self.score = self._load_score()

        # Check pending work
        pending = 0
        try:
            from tools.skynet_todos import pending_count
            pending = pending_count(self.name, include_claimable=True)
        except Exception:
            pass

        can_stop = pending == 0

        return {
            "worker": self.name,
            "score": self.score,
            "specialties": self.specialties,
            "current_task": self.current_task_id,
            "tasks_completed": self.tasks_completed,
            "tasks_claimed": self.tasks_claimed,
            "pending_count": pending,
            "can_stop": can_stop,
            "idle_since": self._idle_since if self._idle_since else None,
        }

    # ── Main Loop ────────────────────────────────────────────────────

    def run_once(self, dry_run: bool = False) -> Optional[dict]:
        """Run a single iteration of the autonomous loop.

        Returns the task found (or None). In dry_run mode, tasks are
        found but not claimed or executed.
        """
        # Step 1: Find next task
        task = self.find_next_task()

        if not task:
            # Step 2: Self-generate improvement task
            task = self.generate_improvement_task()
            if task:
                _log(self.name, f"Self-generated: {task.get('title', task.get('task',''))[:60]}")

        if not task:
            if not self._idle_since:
                self._idle_since = time.time()
            _log(self.name, "No tasks found — truly idle")
            return None

        self._idle_since = 0.0

        if dry_run:
            _log(self.name, f"[DRY RUN] Would claim: [{task['id']}] {task.get('task','')[:60]}")
            return task

        # Step 3: Claim the task
        claimed = self.claim_task(task)
        if not claimed:
            _log(self.name, f"Failed to claim [{task['id']}] — trying next", "WARN")
            return None

        return task

    def run_loop(self, max_iterations: int = 0, dry_run: bool = False):
        """Run the autonomous loop continuously.

        Args:
            max_iterations: Stop after N iterations (0 = infinite).
            dry_run: Find tasks but don't claim or execute them.
        """
        _log(self.name, f"Starting autonomous loop (dry_run={dry_run})")
        iteration = 0

        while True:
            iteration += 1
            if max_iterations and iteration > max_iterations:
                break

            try:
                task = self.run_once(dry_run=dry_run)
                if task:
                    _log(self.name, f"Iteration {iteration}: found task [{task['id']}]")
                else:
                    _log(self.name, f"Iteration {iteration}: idle")
            except KeyboardInterrupt:
                _log(self.name, "Loop interrupted by user")
                break
            except Exception as e:
                _log(self.name, f"Iteration {iteration} error: {e}", "ERROR")

            time.sleep(LOOP_INTERVAL_S)

        _log(self.name, f"Loop ended after {iteration} iterations "
             f"(claimed={self.tasks_claimed}, completed={self.tasks_completed})")


# ═══════════════════════════════════════════════════════════════════
# TODO Generator — creates real tasks from system analysis
# ═══════════════════════════════════════════════════════════════════

def generate_real_todos() -> list:
    """Analyze the actual Skynet codebase and generate real, actionable TODOs.

    These are based on verified gaps found during architecture audits,
    cross-validation sprints, and known system weaknesses.
    """
    from tools.skynet_todos import add_todo, list_todos

    # Check existing to avoid duplicates
    existing = list_todos()
    existing_tasks = {
        t.get("title", t.get("task", "")).lower()[:60]
        for t in existing if t.get("status") in ("pending", "active")
    }

    todos = [
        {
            "worker": "shared",
            "task": (
                "Add unit tests for skynet_spam_guard.py: test guarded_publish "
                "dedup window, rate limiting, fingerprint computation, "
                "category-specific windows (DEAD 120s, knowledge 1800s), "
                "and SpamGuard score penalty integration"
            ),
            "priority": "high",
        },
        {
            "worker": "shared",
            "task": (
                "Add unit tests for skynet_consultant_consumer.py: test queue "
                "polling, ACK flow, bus relay, mark-complete lifecycle, "
                "and graceful shutdown with PID cleanup"
            ),
            "priority": "normal",
        },
        {
            "worker": "shared",
            "task": (
                "Fix sync.Map migration in Skynet/server.go: agentViews was "
                "partially migrated to sync.Map but SSE streaming path and "
                "handleStatus still use the old RWMutex pattern. Complete "
                "the migration or revert to consistent locking"
            ),
            "priority": "high",
        },
        {
            "worker": "shared",
            "task": (
                "Add WebSocket reconnection logic to dashboard.html: the WS "
                "client connects once and never retries on disconnect. Add "
                "exponential backoff reconnect (1s, 2s, 4s, max 30s) with "
                "visual indicator showing connection state"
            ),
            "priority": "normal",
        },
        {
            "worker": "shared",
            "task": (
                "Audit and fix skynet_bus_persist.py JSONL rotation: the "
                "archiver appends to bus_archive.jsonl indefinitely with no "
                "rotation or size cap. Add daily rotation (bus_archive_YYYYMMDD.jsonl) "
                "and configurable max file size (default 50MB)"
            ),
            "priority": "normal",
        },
        {
            "worker": "shared",
            "task": (
                "Add health endpoint to god_console.py: /health should return "
                "JSON with uptime, connected clients count, SSE stream count, "
                "engine probe cache age, and last error. Currently only / and "
                "/dashboard exist — no machine-readable health check"
            ),
            "priority": "high",
        },
        {
            "worker": "shared",
            "task": (
                "Fix skynet_monitor.py DispatchResilience lazy-init: "
                "_get_resilience() tries to import DispatchResilience from "
                "skynet_dispatch_resilience.py which does not exist yet. Add "
                "graceful fallback that uses the existing _track_dispatch_failure "
                "from skynet_dispatch.py when resilience module is absent"
            ),
            "priority": "high",
        },
        {
            "worker": "shared",
            "task": (
                "Create skynet_daemon_health_dashboard.py: aggregate health "
                "from all 16 daemons (PID alive, uptime, last heartbeat, "
                "error count) into a single JSON endpoint consumable by "
                "the GOD Console dashboard. Currently daemon health is "
                "scattered across individual PID files with no aggregation"
            ),
            "priority": "normal",
        },
        {
            "worker": "shared",
            "task": (
                "Add clipboard isolation to ghost_type_to_worker: concurrent "
                "dispatches can corrupt the shared Windows clipboard. Implement "
                "a per-dispatch temp file approach that writes content directly "
                "to the target input via SendMessage WM_SETTEXT or similar "
                "non-clipboard mechanism, with clipboard as fallback"
            ),
            "priority": "high",
        },
        {
            "worker": "shared",
            "task": (
                "Write integration tests for skynet_todos.py: test atomic "
                "claim race conditions (two workers claiming same TODO), "
                "auto_claim priority ordering, bulk_update idempotency, "
                "zero-ticket bonus award trigger, and cleanup of old items. "
                "Use threading to test concurrent claim_todo calls"
            ),
            "priority": "normal",
        },
    ]

    added = []
    for t in todos:
        task_lower = t["task"].lower()[:60]
        if task_lower in existing_tasks:
            _log("system", f"Skipping duplicate: {t['task'][:50]}...")
            continue
        try:
            item = add_todo(t["worker"], t["task"], t["priority"])
            added.append(item)
            _log("system", f"Added [{item['id']}] ({t['priority']}): {t['task'][:60]}...")
        except Exception as e:
            _log("system", f"Failed to add TODO: {e}", "ERROR")

    return added


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Autonomous Worker Loop — self-directing task discovery"
    )
    parser.add_argument("--worker", "-w", choices=WORKER_NAMES,
                        help="Worker name to run as")
    parser.add_argument("--dry-run", action="store_true",
                        help="Find tasks without claiming or executing")
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration then exit")
    parser.add_argument("--loop", type=int, default=0, metavar="N",
                        help="Run N iterations (0=infinite)")
    parser.add_argument("--status", metavar="WORKER",
                        help="Show autonomous worker status")
    parser.add_argument("--generate-todos", action="store_true",
                        help="Generate real TODO items from system analysis")

    args = parser.parse_args()

    if args.generate_todos:
        added = generate_real_todos()
        print(f"\nGenerated {len(added)} new TODO items.")
        for item in added:
            print(f"  [{item['id']}] ({item['priority']}) {item['task'][:70]}...")
        return

    if args.status:
        worker = AutonomousWorker(args.status)
        status = worker.status()
        print(json.dumps(status, indent=2, default=str))
        return

    if not args.worker:
        parser.print_help()
        print("\nError: --worker is required for run modes")
        sys.exit(1)

    worker = AutonomousWorker(args.worker)

    if args.once or args.dry_run:
        task = worker.run_once(dry_run=args.dry_run)
        if task:
            print(f"\nTask found: [{task['id']}]")
            print(f"  Source:   {task.get('source', 'unknown')}")
            print(f"  Priority: {task.get('priority', 'normal')}")
            print(f"  Task:     {task.get('task', '')[:200]}")
            if task.get("specialty_match"):
                print(f"  Specialty match: {task['specialty_match']} keyword(s)")
        else:
            print("\nNo tasks found. Worker is truly idle.")
        status = worker.status()
        print(f"\nScore: {status['score']:.2f} | "
              f"Pending: {status['pending_count']} | "
              f"Can stop: {status['can_stop']}")
    elif args.loop:
        worker.run_loop(max_iterations=args.loop, dry_run=False)
    else:
        # Default: continuous loop
        worker.run_loop(max_iterations=0, dry_run=False)


if __name__ == "__main__":
    main()
