"""Structured task context for Skynet workers.
# signed: delta

Workers today receive a bare task string. TaskContext wraps every dispatch
with full lifecycle state: goal, plan, intermediate results, reflections,
file changes, test outcomes, and timing.  Workers call build_prompt() to
get a context-rich prompt that includes all prior artifacts, enabling
multi-phase tasks to carry knowledge across phases without relying on
conversational memory.

Contexts are checkpointed to data/task_contexts/{id}.json so a crashed
or re-dispatched worker can resume exactly where the previous attempt
left off.

Usage:
    python tools/skynet_task_context.py create <task_id> "goal description"
    python tools/skynet_task_context.py show <task_id>
    python tools/skynet_task_context.py prompt <task_id>
    python tools/skynet_task_context.py add-result <task_id> <phase> "data"
    python tools/skynet_task_context.py checkpoint <task_id> <phase>
    python tools/skynet_task_context.py list [--status STATUS]
    python tools/skynet_task_context.py cleanup [--age HOURS]

Python API:
    from tools.skynet_task_context import TaskContext
    ctx = TaskContext.create("fix_auth_01", "Fix auth middleware XSS bug")
    ctx.plan = ["Analyze vulnerability", "Implement fix", "Write test"]
    ctx.add_result("analyze", {"root_cause": "unsanitized input at L42"})
    ctx.add_file_change("core/auth.py", "edit", "Added input sanitization")
    ctx.checkpoint("analyze")
    prompt = ctx.build_prompt()  # Full context string for worker dispatch

    # Later, resume from checkpoint:
    ctx2 = TaskContext.resume("fix_auth_01")
"""
# signed: delta

import json
import os
import sys
import time
import argparse
import copy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXTS_DIR = REPO_ROOT / "data" / "task_contexts"


# ── Data Model ───────────────────────────────────────────────────────
# signed: delta

@dataclass
class IntermediateResult:
    """A result captured during one phase of task execution."""
    phase: str
    data: Any
    timestamp: str = ""
    worker: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class FileChange:
    """Record of a file modification made during the task."""
    file: str
    change_type: str     # edit, create, delete, rename
    description: str = ""
    lines_changed: int = 0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class TestResult:
    """Outcome of a test/validation run."""
    test_type: str       # py_compile, pytest, smoke_test, manual
    passed: bool = False
    output: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class TaskContext:
    """Full lifecycle context for a Skynet worker task.

    Carries everything a worker needs to understand what happened before
    and what remains to be done, enabling multi-phase execution and
    crash recovery via checkpoint/resume.
    """
    task_id: str
    goal: str
    plan: List[str] = field(default_factory=list)
    intermediate_results: List[IntermediateResult] = field(default_factory=list)
    reflections: List[str] = field(default_factory=list)
    file_changes: List[FileChange] = field(default_factory=list)
    test_results: List[TestResult] = field(default_factory=list)
    status: str = "created"         # created, active, checkpointed, completed, failed
    assignee: str = ""              # worker name
    priority: int = 5               # 1=highest, 10=lowest
    tags: List[str] = field(default_factory=list)
    parent_task_id: str = ""        # for sub-task chains
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    last_checkpoint_at: str = ""
    last_checkpoint_phase: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    # ── Factory Methods ──────────────────────────────────────────

    @classmethod
    def create(
        cls,
        task_id: str,
        goal: str,
        assignee: str = "",
        plan: Optional[List[str]] = None,
        priority: int = 5,
        tags: Optional[List[str]] = None,
        parent_task_id: str = "",
    ) -> "TaskContext":
        """Create a new task context and save initial checkpoint.

        Args:
            task_id:         Unique identifier for this task.
            goal:            Human-readable goal description.
            assignee:        Worker name (optional at creation time).
            plan:            Ordered list of phase/step descriptions.
            priority:        1-10 priority (default 5).
            tags:            Categorization tags.
            parent_task_id:  Parent task for sub-task chains.

        Returns:
            New TaskContext instance, already persisted to disk.
        """
        ctx = cls(
            task_id=task_id,
            goal=goal,
            assignee=assignee,
            plan=plan or [],
            priority=priority,
            tags=tags or [],
            parent_task_id=parent_task_id,
            status="created",
        )
        ctx._save()
        return ctx  # signed: delta

    @classmethod
    def resume(cls, task_id: str) -> "TaskContext":
        """Load a task context from its checkpoint file.

        Args:
            task_id: The task ID to resume.

        Returns:
            TaskContext restored from disk.

        Raises:
            FileNotFoundError: If no checkpoint exists for this task_id.
        """
        path = CONTEXTS_DIR / f"{task_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"No checkpoint found for task '{task_id}' at {path}"
            )

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Reconstruct nested dataclasses from dicts
        ctx = cls(
            task_id=raw["task_id"],
            goal=raw["goal"],
            plan=raw.get("plan", []),
            status=raw.get("status", "checkpointed"),
            assignee=raw.get("assignee", ""),
            priority=raw.get("priority", 5),
            tags=raw.get("tags", []),
            parent_task_id=raw.get("parent_task_id", ""),
            metadata=raw.get("metadata", {}),
            created_at=raw.get("created_at", ""),
            started_at=raw.get("started_at", ""),
            completed_at=raw.get("completed_at", ""),
            last_checkpoint_at=raw.get("last_checkpoint_at", ""),
            last_checkpoint_phase=raw.get("last_checkpoint_phase", ""),
            reflections=raw.get("reflections", []),
        )

        # Restore intermediate results
        for r in raw.get("intermediate_results", []):
            ctx.intermediate_results.append(IntermediateResult(
                phase=r["phase"], data=r["data"],
                timestamp=r.get("timestamp", ""), worker=r.get("worker", ""),
            ))

        # Restore file changes
        for fc in raw.get("file_changes", []):
            ctx.file_changes.append(FileChange(
                file=fc["file"], change_type=fc["change_type"],
                description=fc.get("description", ""),
                lines_changed=fc.get("lines_changed", 0),
                timestamp=fc.get("timestamp", ""),
            ))

        # Restore test results
        for tr in raw.get("test_results", []):
            ctx.test_results.append(TestResult(
                test_type=tr["test_type"], passed=tr.get("passed", False),
                output=tr.get("output", ""), timestamp=tr.get("timestamp", ""),
            ))

        return ctx  # signed: delta

    # ── Mutation Methods ─────────────────────────────────────────

    def add_result(
        self,
        phase: str,
        data: Any,
        worker: str = "",
    ) -> IntermediateResult:
        """Record an intermediate result for a task phase.

        Args:
            phase:  Name of the phase (e.g. "analyze", "implement", "test").
            data:   Any JSON-serializable data (string, dict, list).
            worker: Worker that produced this result.

        Returns:
            The created IntermediateResult.
        """
        result = IntermediateResult(
            phase=phase, data=data,
            worker=worker or self.assignee,
        )
        self.intermediate_results.append(result)
        if self.status == "created":
            self.status = "active"
            if not self.started_at:
                self.started_at = datetime.now(timezone.utc).isoformat()
        return result  # signed: delta

    def add_file_change(
        self,
        file: str,
        change_type: str,
        description: str = "",
        lines_changed: int = 0,
    ) -> FileChange:
        """Record a file modification made during the task."""
        fc = FileChange(
            file=file, change_type=change_type,
            description=description, lines_changed=lines_changed,
        )
        self.file_changes.append(fc)
        return fc

    def add_test_result(
        self,
        test_type: str,
        passed: bool,
        output: str = "",
    ) -> TestResult:
        """Record a test/validation outcome."""
        tr = TestResult(
            test_type=test_type, passed=passed, output=output,
        )
        self.test_results.append(tr)
        return tr

    def add_reflection(self, reflection: str) -> None:
        """Add a reflection/lesson learned during execution."""
        self.reflections.append(reflection)

    def complete(self, final_result: Any = None) -> None:
        """Mark the task as completed."""
        self.status = "completed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if final_result is not None:
            self.add_result("final", final_result)
        self._save()  # signed: delta

    def fail(self, reason: str = "") -> None:
        """Mark the task as failed."""
        self.status = "failed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if reason:
            self.add_reflection(f"FAILURE: {reason}")
        self._save()

    # ── Checkpoint / Persistence ─────────────────────────────────

    def checkpoint(self, phase: str = "") -> Path:
        """Save current state to data/task_contexts/{task_id}.json.

        Args:
            phase: Current phase label (stored for resume context).

        Returns:
            Path to the checkpoint file.
        """
        self.last_checkpoint_at = datetime.now(timezone.utc).isoformat()
        if phase:
            self.last_checkpoint_phase = phase
        if self.status == "created":
            self.status = "active"
        return self._save()  # signed: delta

    def _save(self) -> Path:
        """Atomically persist context to disk."""
        CONTEXTS_DIR.mkdir(parents=True, exist_ok=True)
        path = CONTEXTS_DIR / f"{self.task_id}.json"
        tmp = str(path) + ".tmp"

        data = self._to_dict()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, str(path))
        return path

    def _to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (handles nested dataclasses)."""
        d = {
            "task_id": self.task_id,
            "goal": self.goal,
            "plan": self.plan,
            "intermediate_results": [asdict(r) for r in self.intermediate_results],
            "reflections": self.reflections,
            "file_changes": [asdict(fc) for fc in self.file_changes],
            "test_results": [asdict(tr) for tr in self.test_results],
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "tags": self.tags,
            "parent_task_id": self.parent_task_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_checkpoint_at": self.last_checkpoint_at,
            "last_checkpoint_phase": self.last_checkpoint_phase,
        }
        return d

    # ── Prompt Builder ───────────────────────────────────────────
    # signed: delta

    def build_prompt(
        self,
        include_results: bool = True,
        include_files: bool = True,
        include_tests: bool = True,
        include_reflections: bool = True,
        max_result_chars: int = 2000,
    ) -> str:
        """Generate a context-rich prompt for worker dispatch.

        Instead of a bare task string, workers receive full context:
        goal, plan progress, intermediate findings, file changes made,
        test outcomes, and lessons learned.

        Args:
            include_results:     Include intermediate results.
            include_files:       Include file change log.
            include_tests:       Include test outcomes.
            include_reflections: Include reflection notes.
            max_result_chars:    Truncate result data beyond this length.

        Returns:
            Multi-section prompt string ready for dispatch.
        """
        sections = []

        # Header
        sections.append(f"═══ TASK CONTEXT: {self.task_id} ═══")
        sections.append(f"GOAL: {self.goal}")
        sections.append(f"STATUS: {self.status}")
        if self.assignee:
            sections.append(f"ASSIGNEE: {self.assignee}")
        if self.priority != 5:
            sections.append(f"PRIORITY: {self.priority}/10")

        # Plan with progress markers
        if self.plan:
            sections.append("")
            sections.append("── PLAN ──")
            completed_phases = {r.phase for r in self.intermediate_results}
            for i, step in enumerate(self.plan, 1):
                # Mark steps that have results as done
                marker = "✓" if step.lower() in completed_phases or \
                    any(step.lower() in r.phase.lower() for r in self.intermediate_results) \
                    else "○"
                sections.append(f"  {marker} {i}. {step}")

        # Intermediate results
        if include_results and self.intermediate_results:
            sections.append("")
            sections.append("── PRIOR RESULTS ──")
            for r in self.intermediate_results:
                data_str = json.dumps(r.data, default=str) if not isinstance(r.data, str) else r.data
                if len(data_str) > max_result_chars:
                    data_str = data_str[:max_result_chars] + "... (truncated)"
                worker_tag = f" [{r.worker}]" if r.worker else ""
                sections.append(f"  [{r.phase}]{worker_tag}: {data_str}")

        # File changes
        if include_files and self.file_changes:
            sections.append("")
            sections.append("── FILE CHANGES ──")
            for fc in self.file_changes:
                lines = f" ({fc.lines_changed} lines)" if fc.lines_changed else ""
                desc = f" — {fc.description}" if fc.description else ""
                sections.append(f"  [{fc.change_type}] {fc.file}{lines}{desc}")

        # Test results
        if include_tests and self.test_results:
            sections.append("")
            sections.append("── TEST RESULTS ──")
            for tr in self.test_results:
                status = "PASS" if tr.passed else "FAIL"
                output = f" — {tr.output[:200]}" if tr.output else ""
                sections.append(f"  [{status}] {tr.test_type}{output}")

        # Reflections
        if include_reflections and self.reflections:
            sections.append("")
            sections.append("── REFLECTIONS / LESSONS ──")
            for ref in self.reflections:
                sections.append(f"  • {ref}")

        # Current phase guidance
        if self.last_checkpoint_phase:
            sections.append("")
            sections.append(f"── RESUME FROM: {self.last_checkpoint_phase} ──")
            sections.append(
                "Continue from where the previous attempt left off. "
                "The results above show what was already accomplished."
            )

        sections.append("")
        sections.append("═══ END CONTEXT ═══")

        return "\n".join(sections)  # signed: delta

    # ── Summary ──────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return a compact summary of this context."""
        return {
            "task_id": self.task_id,
            "goal": self.goal[:100],
            "status": self.status,
            "assignee": self.assignee,
            "phases_completed": len(self.intermediate_results),
            "files_changed": len(self.file_changes),
            "tests_run": len(self.test_results),
            "tests_passed": sum(1 for t in self.test_results if t.passed),
            "reflections": len(self.reflections),
            "created_at": self.created_at,
            "last_checkpoint": self.last_checkpoint_phase or "(none)",
        }


# ── Utility Functions ────────────────────────────────────────────────
# signed: delta

def list_contexts(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all saved task contexts with optional filtering.

    Args:
        status:   Filter by status (created/active/completed/failed).
        assignee: Filter by worker name.

    Returns:
        List of context summaries.
    """
    if not CONTEXTS_DIR.exists():
        return []

    results = []
    for path in sorted(CONTEXTS_DIR.glob("*.json")):
        try:
            ctx = TaskContext.resume(path.stem)
            if status and ctx.status != status:
                continue
            if assignee and ctx.assignee != assignee:
                continue
            results.append(ctx.summary())
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            continue

    return results


def cleanup_contexts(max_age_hours: float = 72.0) -> int:
    """Remove completed/failed context files older than max_age_hours.

    Args:
        max_age_hours: Age threshold (default 72 hours).

    Returns:
        Number of files removed.
    """
    if not CONTEXTS_DIR.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for path in CONTEXTS_DIR.glob("*.json"):
        try:
            ctx = TaskContext.resume(path.stem)
            if ctx.status not in ("completed", "failed"):
                continue
            # Use completed_at or file mtime
            ts = ctx.completed_at
            if ts:
                from datetime import datetime as dt
                epoch = dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            else:
                epoch = path.stat().st_mtime
            if epoch < cutoff:
                path.unlink()
                removed += 1
        except Exception:
            continue

    return removed


# ── CLI ──────────────────────────────────────────────────────────────
# signed: delta

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet task context manager"
    )
    sub = parser.add_subparsers(dest="command")

    # create
    cr = sub.add_parser("create", help="Create a new task context")
    cr.add_argument("task_id", help="Unique task identifier")
    cr.add_argument("goal", help="Goal description")
    cr.add_argument("--assignee", default="", help="Worker name")
    cr.add_argument("--plan", nargs="*", default=[], help="Plan steps")
    cr.add_argument("--priority", type=int, default=5, help="Priority 1-10")

    # show
    sh = sub.add_parser("show", help="Show task context details")
    sh.add_argument("task_id", help="Task ID to show")

    # prompt
    pr = sub.add_parser("prompt", help="Build dispatch prompt from context")
    pr.add_argument("task_id", help="Task ID")

    # add-result
    ar = sub.add_parser("add-result", help="Add an intermediate result")
    ar.add_argument("task_id", help="Task ID")
    ar.add_argument("phase", help="Phase name")
    ar.add_argument("data", help="Result data (string or JSON)")
    ar.add_argument("--worker", default="", help="Worker name")

    # checkpoint
    cp = sub.add_parser("checkpoint", help="Save checkpoint")
    cp.add_argument("task_id", help="Task ID")
    cp.add_argument("phase", help="Current phase label")

    # list
    ls = sub.add_parser("list", help="List all task contexts")
    ls.add_argument("--status", default=None, help="Filter by status")
    ls.add_argument("--assignee", default=None, help="Filter by worker")

    # cleanup
    cl = sub.add_parser("cleanup", help="Remove old completed/failed contexts")
    cl.add_argument("--age", type=float, default=72.0, help="Max age in hours")

    args = parser.parse_args()

    if args.command == "create":
        ctx = TaskContext.create(
            args.task_id, args.goal,
            assignee=args.assignee,
            plan=args.plan,
            priority=args.priority,
        )
        print(f"Created task context: {ctx.task_id}")
        print(f"  Goal: {ctx.goal}")
        print(f"  Saved to: {CONTEXTS_DIR / f'{ctx.task_id}.json'}")

    elif args.command == "show":
        ctx = TaskContext.resume(args.task_id)
        print(json.dumps(ctx._to_dict(), indent=2, default=str))

    elif args.command == "prompt":
        ctx = TaskContext.resume(args.task_id)
        print(ctx.build_prompt())

    elif args.command == "add-result":
        ctx = TaskContext.resume(args.task_id)
        # Try to parse data as JSON, fall back to string
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError:
            data = args.data
        ctx.add_result(args.phase, data, worker=args.worker)
        ctx.checkpoint(args.phase)
        print(f"Added result for phase '{args.phase}' and checkpointed.")

    elif args.command == "checkpoint":
        ctx = TaskContext.resume(args.task_id)
        path = ctx.checkpoint(args.phase)
        print(f"Checkpointed at phase '{args.phase}' → {path}")

    elif args.command == "list":
        contexts = list_contexts(status=args.status, assignee=args.assignee)
        if not contexts:
            print("No task contexts found.")
        else:
            print(f"{'ID':<25} {'Status':<12} {'Assignee':<10} {'Phases':<7} {'Files':<6} {'Goal'}")
            print("-" * 90)
            for c in contexts:
                print(
                    f"{c['task_id']:<25} {c['status']:<12} "
                    f"{c['assignee'] or '-':<10} {c['phases_completed']:<7} "
                    f"{c['files_changed']:<6} {c['goal']}"
                )

    elif args.command == "cleanup":
        removed = cleanup_contexts(max_age_hours=args.age)
        print(f"Removed {removed} old context file(s).")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
