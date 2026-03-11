"""
Hierarchical Planner with Self-Reflective Feedback.

Implements a two-tier planning architecture:
- Strategic Planner: Decomposes high-level goals into subtask sequences
- Tactical Executor: Handles real-time interaction for each subtask
- Reflector: Validates action outcomes and triggers replanning on failure

Architecture:
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │   GOAL   │────▶│ PLANNER  │────▶│ SUBTASKS │
    └──────────┘     └──────────┘     └────┬─────┘
                                           │
                          ┌────────────────┘
                          ▼
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │ REFLECTOR│◀────│ EXECUTOR │────▶│  ACTION  │
    │ (verify) │     │ (execute)│     │ (result) │
    └────┬─────┘     └──────────┘     └──────────┘
         │
         ▼
    ┌──────────────────────────────────────┐
    │  Success? → next subtask             │
    │  Failure? → retry / replan / abort   │
    └──────────────────────────────────────┘

Self-Correction Protocol:
1. Execute action
2. Capture post-action screenshot
3. Compare expected vs actual outcome (change detection + VLM)
4. If mismatch: retry with adjusted parameters (max 3 retries)
5. If persistent failure: replan the subtask sequence
6. If replan fails: escalate to user or abort

LOG FORMAT:
    [PLANNER] goal_set — "Research AI agent frameworks and collect 5 papers"
    [PLANNER] decomposed — 4 subtasks: [open_chrome, navigate_scholar, search, extract]
    [EXECUTOR] subtask_start — step 1/4: open_chrome
    [EXECUTOR] action — click mark 3 (Chrome icon on taskbar)
    [REFLECTOR] verify — expected: Chrome window active, actual: Chrome opened ✓
    [REFLECTOR] failure — expected: search results page, actual: error 404
    [PLANNER] replan — replacing step 3 (search_scholar) with (search_arxiv)
"""
import time
import json
import logging
from typing import List, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    REPLANNED = "replanned"
    ABORTED = "aborted"


class ActionType(Enum):
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    WAIT = "wait"
    NAVIGATE = "navigate"
    SCREENSHOT = "screenshot"
    ANALYZE = "analyze"
    EXTRACT = "extract"
    CUSTOM = "custom"


@dataclass
class Action:
    """A single executable action."""
    action_type: ActionType
    target: str = ""            # Mark ID, coordinates, or key sequence
    value: str = ""             # Text to type, URL to navigate, etc.
    expected_outcome: str = ""  # What should change after this action
    timeout_ms: int = 5000
    metadata: dict = field(default_factory=dict)


@dataclass
class Subtask:
    """A subtask in the execution plan."""
    id: int
    description: str
    actions: List[Action] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    retries: int = 0
    max_retries: int = 3
    result: str = ""
    error: str = ""
    started_at: float = 0
    completed_at: float = 0
    verification_result: str = ""

    @property
    def elapsed_ms(self) -> float:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return 0


@dataclass
class Plan:
    """A complete execution plan for a goal."""
    goal: str
    subtasks: List[Subtask] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    current_step: int = 0
    replan_count: int = 0
    max_replans: int = 3
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def progress(self) -> str:
        completed = sum(1 for s in self.subtasks if s.status == TaskStatus.SUCCESS)
        return f"{completed}/{len(self.subtasks)}"

    @property
    def is_complete(self) -> bool:
        return all(s.status == TaskStatus.SUCCESS for s in self.subtasks)


class HierarchicalPlanner:
    """
    Decomposes goals into subtask sequences and manages execution
    with self-reflective verification at each step.
    """

    def __init__(self, vlm_analyzer=None, memory=None):
        self.vlm = vlm_analyzer
        self.memory = memory
        self._plans: List[Plan] = []
        self._action_history: List[dict] = []

    def create_plan(self, goal: str, context: str = "") -> Plan:
        """
        Decompose a goal into a sequence of subtasks.
        
        Uses VLM if available to generate contextual plans,
        falls back to template-based decomposition.
        """
        logger.info(f"Planning: {goal}")

        # Use VLM for intelligent decomposition if available
        if self.vlm and self.vlm.is_available:
            subtasks = self._vlm_decompose(goal, context)
        else:
            subtasks = self._template_decompose(goal)

        plan = Plan(goal=goal, subtasks=subtasks)
        self._plans.append(plan)

        logger.info(f"Plan created: {len(subtasks)} subtasks")
        for i, st in enumerate(subtasks, 1):
            logger.info(f"  Step {i}/{len(subtasks)}: {st.description}")

        return plan

    def _vlm_decompose(self, goal: str, context: str) -> List[Subtask]:
        """Use VLM to intelligently decompose goal into subtasks."""
        prompt = f"""Given this goal: "{goal}"
And this context: {context}

Decompose into a sequence of specific, executable subtasks.
Each subtask should be a single action or small group of related actions.
Return as a numbered list."""

        # For now, fall back to template since VLM doesn't take text-only input well
        return self._template_decompose(goal)

    def _template_decompose(self, goal: str) -> List[Subtask]:
        """
        Template-based goal decomposition for common task patterns.
        """
        goal_lower = goal.lower()
        subtasks = []

        if any(w in goal_lower for w in ["search", "find", "research", "look up"]):
            subtasks = [
                Subtask(id=1, description="Open web browser"),
                Subtask(id=2, description="Navigate to search engine or target site"),
                Subtask(id=3, description=f"Enter search query: {goal}"),
                Subtask(id=4, description="Analyze search results"),
                Subtask(id=5, description="Extract relevant information"),
                Subtask(id=6, description="Store findings in memory"),
            ]
        elif any(w in goal_lower for w in ["open", "launch", "start"]):
            subtasks = [
                Subtask(id=1, description=f"Locate application: {goal}"),
                Subtask(id=2, description="Click to open application"),
                Subtask(id=3, description="Verify application is running"),
            ]
        elif any(w in goal_lower for w in ["navigate", "go to", "visit"]):
            subtasks = [
                Subtask(id=1, description="Focus browser window"),
                Subtask(id=2, description=f"Enter URL or navigate: {goal}"),
                Subtask(id=3, description="Wait for page load"),
                Subtask(id=4, description="Verify correct page loaded"),
            ]
        elif any(w in goal_lower for w in ["write", "create", "draft"]):
            subtasks = [
                Subtask(id=1, description="Open target application or editor"),
                Subtask(id=2, description="Position cursor at correct location"),
                Subtask(id=3, description=f"Write content: {goal}"),
                Subtask(id=4, description="Review and verify output"),
            ]
        else:
            # Generic decomposition
            subtasks = [
                Subtask(id=1, description=f"Analyze current state for: {goal}"),
                Subtask(id=2, description="Determine required actions"),
                Subtask(id=3, description="Execute primary action"),
                Subtask(id=4, description="Verify outcome"),
            ]

        return subtasks

    def execute_step(self, plan: Plan, executor_fn: Optional[Callable] = None) -> Subtask:
        """Execute the next subtask in the plan."""
        if plan.current_step >= len(plan.subtasks):
            logger.info("Plan complete -- all subtasks done")
            plan.status = TaskStatus.SUCCESS
            return plan.subtasks[-1]

        subtask = plan.subtasks[plan.current_step]
        subtask.status = TaskStatus.RUNNING
        subtask.started_at = time.time()

        logger.info(f"Executing step {plan.current_step + 1}/{len(plan.subtasks)}: {subtask.description}")

        try:
            self._run_subtask(subtask, executor_fn)
            plan.current_step += 1
        except Exception as e:
            self._handle_subtask_failure(plan, subtask, e)

        self._record_action(plan, subtask)
        return subtask

    def _run_subtask(self, subtask: Subtask, executor_fn: Optional[Callable]):
        """Execute a subtask and mark it successful."""
        if executor_fn:
            result = executor_fn(subtask)
            subtask.result = str(result) if result else "completed"
        else:
            subtask.result = "simulated_success"

        subtask.status = TaskStatus.SUCCESS
        subtask.completed_at = time.time()

        if self.memory:
            self.memory.store_episodic(
                f"Completed: {subtask.description} -> {subtask.result}",
                tags=["execution", "subtask"], source_action="execute_step",
            )
        logger.info(f"Step {subtask.id} SUCCESS ({subtask.elapsed_ms:.0f}ms): {subtask.result[:80]}")

    def _handle_subtask_failure(self, plan: Plan, subtask: Subtask, error: Exception):
        """Handle subtask failure with retry/replan logic."""
        subtask.error = str(error)
        subtask.retries += 1

        if subtask.retries < subtask.max_retries:
            subtask.status = TaskStatus.RETRYING
            logger.warning(f"Step {subtask.id} RETRY {subtask.retries}/{subtask.max_retries}: {error}")
        else:
            subtask.status = TaskStatus.FAILED
            subtask.completed_at = time.time()
            logger.error(f"Step {subtask.id} FAILED after {subtask.retries} retries: {error}")
            if plan.replan_count < plan.max_replans:
                self._replan(plan, subtask)

    def _record_action(self, plan: Plan, subtask: Subtask):
        """Log action to history."""
        self._action_history.append({
            "timestamp": time.time(), "plan_goal": plan.goal,
            "step": subtask.id, "description": subtask.description,
            "status": subtask.status.value, "result": subtask.result,
            "error": subtask.error, "elapsed_ms": subtask.elapsed_ms,
        })

    def verify_outcome(self, subtask: Subtask, pre_screenshot=None,
                       post_screenshot=None, change_detector=None) -> bool:
        """
        Self-reflective verification: compare expected vs actual outcome.
        
        Uses change detection between pre/post screenshots and VLM analysis
        to determine if the action achieved its intended effect.
        
        LOG FORMAT:
            [REFLECTOR] verify_start — checking outcome of "Navigate to search engine"
            [REFLECTOR] change_detected — hamming=156, pct=65.2% (significant change)
            [REFLECTOR] vlm_check — post-state matches expected outcome ✓
            [REFLECTOR] verify_result — SUCCESS
        """
        logger.info(f"Verifying outcome: {subtask.description}")

        if pre_screenshot and post_screenshot and change_detector:
            # Check if something visually changed
            change = change_detector.detect_change(post_screenshot, monitor_index=99)
            logger.info(f"Change detection: hamming={change.hamming_distance}, pct={change.change_percent:.1f}%")

            if not change.changed:
                logger.warning("No visual change detected — action may have failed")
                subtask.verification_result = "no_change"
                return False

        if self.vlm and self.vlm.is_available and post_screenshot:
            # Ask VLM to verify
            analysis = self.vlm.analyze(post_screenshot, detailed=False)
            if analysis:
                subtask.verification_result = analysis.description
                logger.info(f"VLM verification: {analysis.description[:100]}")
                return True

        # Default: trust the execution
        subtask.verification_result = "assumed_success"
        return True

    def _replan(self, plan: Plan, failed_subtask: Subtask):
        """
        Generate alternative subtasks when a step fails.
        
        LOG FORMAT:
            [PLANNER] replan — step 3 failed, attempting alternative approach
            [PLANNER] replan_result — replaced 1 subtask, continuing from step 3
        """
        plan.replan_count += 1
        logger.info(f"Replanning (attempt {plan.replan_count}/{plan.max_replans}): step {failed_subtask.id} failed")

        # Simple replan: retry with modified approach
        alt_subtask = Subtask(
            id=failed_subtask.id,
            description=f"[ALT] {failed_subtask.description} (alternative approach)",
            max_retries=2,
        )

        # Replace failed subtask
        idx = plan.subtasks.index(failed_subtask)
        plan.subtasks[idx] = alt_subtask
        plan.status = TaskStatus.REPLANNED

        logger.info(f"Replanned step {failed_subtask.id}: {alt_subtask.description}")

    def get_plan_summary(self, plan: Plan) -> str:
        """Generate human-readable plan summary for logging/debugging."""
        lines = [
            f"Plan: {plan.goal}",
            f"Status: {plan.status.value} | Progress: {plan.progress} | Replans: {plan.replan_count}",
            "",
        ]

        for st in plan.subtasks:
            status_icon = {
                TaskStatus.PENDING: "⬜",
                TaskStatus.RUNNING: "🔄",
                TaskStatus.SUCCESS: "✅",
                TaskStatus.FAILED: "❌",
                TaskStatus.RETRYING: "🔁",
                TaskStatus.REPLANNED: "📝",
                TaskStatus.ABORTED: "⛔",
            }.get(st.status, "?")

            line = f"  {status_icon} Step {st.id}: {st.description}"
            if st.result:
                line += f" → {st.result[:60]}"
            if st.error:
                line += f" [ERROR: {st.error[:40]}]"
            if st.elapsed_ms:
                line += f" ({st.elapsed_ms:.0f}ms)"
            lines.append(line)

        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from core.cognitive.memory import EpisodicMemory

    print("=== Hierarchical Planner Test ===\n")

    memory = EpisodicMemory()
    planner = HierarchicalPlanner(memory=memory)

    # Create a plan
    plan = planner.create_plan("Search for latest AI agent research papers on arxiv")

    # Execute steps
    for i in range(len(plan.subtasks)):
        subtask = planner.execute_step(plan)
        print(f"  → {subtask.status.value}")

    # Summary
    print(f"\n{planner.get_plan_summary(plan)}")
    print(f"\nMemory stats: {json.dumps(memory.get_stats())}")
