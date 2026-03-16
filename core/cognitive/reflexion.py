"""
Reflexion Engine — Verbal Self-Critique for Learning from Failures.

When an action fails, the agent generates a verbal self-critique explaining
WHY it failed, stores it in episodic memory AND the persistent LearningStore,
and uses it to avoid repeating the same mistake. This replaces traditional
parameter updates with natural language learning.

Reference: "Reflexion: Language Agents with Verbal Reinforcement Learning"
(Shinn et al., 2023)

P1.11 Enhancement (Level 4 Upgrade Plan):
    - LearningStore integration: reflections persist across sessions via SQLite
    - Pre-task context injection: query past failures before starting similar tasks
    - Side-effect analysis: when task fails goal X but achieves Y, store Y as success

Workflow:
    1. Action fails (error, unexpected state, timeout)
    2. Capture: error trace + pre/post screenshots + action details
    3. Reflect: Generate verbal critique ("I failed because...")
    4. Store: Save to episodic memory (high importance) AND LearningStore (persistent)
    5. Analyze: Check if failure produced useful side-effects (partial success)
    6. Apply: On next similar action, retrieve relevant reflections from BOTH stores
    7. Adapt: Modify action based on past reflections

LOG FORMAT:
    [REFLEXION] trigger    -- action "click mark 7" failed: element not found
    [REFLEXION] analyze    -- comparing pre/post states, error trace captured
    [REFLEXION] reflect    -- "Clicked wrong element. Mark 7 was a label, not a button."
    [REFLEXION] store      -- saved to episodic memory (importance=0.9)
    [REFLEXION] persist    -- saved to LearningStore (category=reflexion)
    [REFLEXION] side_effect -- task failed goal X but achieved Y; storing Y as success
    [REFLEXION] retrieve   -- found 2 relevant past reflections for current action
    [REFLEXION] adapt      -- adjusting action: target mark 12 instead of mark 7
"""
import time
import json
import logging
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class FailureContext:
    """Complete context of a failed action for reflection."""
    action_type: str
    action_target: str
    action_value: str = ""
    expected_outcome: str = ""
    actual_outcome: str = ""
    error_message: str = ""
    error_type: str = ""  # timeout, element_not_found, wrong_state, etc.
    pre_state_description: str = ""
    post_state_description: str = ""
    timestamp: float = field(default_factory=time.time)
    subtask: str = ""
    attempt_number: int = 1
    # P1.11: additional context for side-effect analysis  # signed: delta
    achieved_outcomes: List[str] = field(default_factory=list)
    domain: str = ""  # e.g. "python", "go", "security", "testing"
    files_involved: List[str] = field(default_factory=list)


@dataclass
class Reflection:
    """A verbal self-critique generated after a failure."""
    id: str
    failure: FailureContext
    critique: str          # "I failed because..."
    lesson: str            # "Next time I should..."
    action_adjustment: str  # Specific adjustment to make
    confidence: float = 0.5  # How confident the reflection is correct
    applied_count: int = 0   # How many times this reflection has been applied
    timestamp: float = field(default_factory=time.time)

    @property
    def is_useful(self) -> bool:
        """A reflection is useful if it's been applied and not caused more failures."""
        return self.applied_count > 0 and self.confidence > 0.3


class ReflexionEngine:
    """
    Verbal reinforcement learning through self-critique.

    The agent learns from failures by generating natural language reflections
    instead of updating model parameters. These reflections are stored in
    memory and retrieved when similar situations arise.

    P1.11 Enhancement: Integrates with PersistentLearningSystem for cross-session
    persistence. Reflections survive process restarts. Pre-task context injection
    queries LearningStore for relevant past failures. Side-effect analysis captures
    partial successes from failed tasks.
    """

    def __init__(self, memory=None, vlm_analyzer=None, max_reflections: int = 100,
                 learning_store=None):
        self.memory = memory
        self.vlm = vlm_analyzer
        self.max_reflections = max_reflections
        self._reflections: List[Reflection] = []
        self._counter = 0
        self._failure_patterns: Dict[str, int] = {}
        # P1.11: persistent learning store integration  # signed: delta
        self._learning_store = learning_store
        self._learning_system = None
        self._init_learning_store()

    def _next_id(self) -> str:
        self._counter += 1
        return f"ref_{self._counter:04d}"

    # ── P1.11: LearningStore integration ──────────────────────  # signed: delta

    def _init_learning_store(self):
        """Lazy-initialize persistent learning system if not provided."""
        if self._learning_store is not None:
            return
        try:
            from core.learning_store import PersistentLearningSystem
            self._learning_system = PersistentLearningSystem()
            self._learning_store = self._learning_system.store
            logger.info("[REFLEXION] LearningStore connected for persistent reflections")
        except Exception as e:
            logger.warning(f"[REFLEXION] LearningStore unavailable, using in-memory only: {e}")
            self._learning_store = None
            self._learning_system = None

    def _persist_reflection(self, reflection: Reflection):
        """Store a reflection in the persistent LearningStore (survives restarts).

        Stores structured data: critique, lesson, adjustment, error type, domain,
        and files involved — all BM25-searchable for future recall.
        """
        if self._learning_store is None:
            return

        try:
            f = reflection.failure
            # Build rich searchable content combining all reflection fields
            content = (
                f"FAILURE [{f.error_type}] in {f.action_type}({f.action_target}): "
                f"{reflection.critique} "
                f"LESSON: {reflection.lesson} "
                f"ADJUSTMENT: {reflection.action_adjustment}"
            )
            if f.files_involved:
                content += f" FILES: {', '.join(f.files_involved)}"

            tags = [
                "reflexion", f.action_type, f.error_type,
                f"attempt_{f.attempt_number}",
            ]
            if f.domain:
                tags.append(f.domain)
            for fpath in f.files_involved:
                tags.append(fpath.replace("\\", "/").split("/")[-1])

            fact_id = self._learning_store.learn(
                content=content,
                category="reflexion",
                source=f"reflexion:{reflection.id}",
                tags=tags,
            )

            # Update expertise profile on failure domain
            if self._learning_system and f.domain:
                self._learning_system.expertise.update(f.domain, success=False)

            logger.info(f"[REFLEXION] persist: stored as fact {fact_id} in LearningStore")
        except Exception as e:
            logger.warning(f"[REFLEXION] persist failed: {e}")

    def get_persistent_reflections(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Query LearningStore for past reflections matching a task description.

        Returns structured dicts with content, confidence, and reinforcement count.
        Use this BEFORE starting a task to inject failure-avoidance context.
        """
        if self._learning_store is None:
            return []

        try:
            # Prefix query with reflexion category keywords for better BM25 matching
            search_query = f"FAILURE LESSON {query}"
            facts = self._learning_store.recall(search_query, top_k=top_k)

            # Filter to reflexion category only
            results = []
            for fact in facts:
                if fact.category == "reflexion":
                    results.append({
                        "fact_id": fact.fact_id,
                        "content": fact.content,
                        "confidence": fact.confidence,
                        "reinforcement_count": fact.reinforcement_count,
                        "tags": fact.tags,
                        "first_learned": fact.first_learned,
                    })
            return results
        except Exception as e:
            logger.warning(f"[REFLEXION] persistent recall failed: {e}")
            return []

    def get_pre_task_context(self, task_description: str, action_type: str = "",
                             target: str = "", top_k: int = 3) -> str:
        """Build context string from past failures for injection into task preamble.

        Combines in-memory reflections with persistent LearningStore reflections
        to produce a compact warning block that prevents repeating past mistakes.
        Returns empty string if no relevant reflections found.
        """
        warnings = []

        # 1. In-memory reflections (current session)
        in_mem = self.get_relevant_reflections(action_type, target, task_description, limit=top_k)
        for ref in in_mem:
            warnings.append(f"⚠ {ref.lesson} (confidence: {ref.confidence:.2f})")

        # 2. Persistent reflections (cross-session)
        persistent = self.get_persistent_reflections(task_description, top_k=top_k)
        # Deduplicate against in-memory results by content overlap
        in_mem_lessons = {r.lesson.lower()[:60] for r in in_mem}
        for pfact in persistent:
            # Extract lesson from stored content
            content = pfact["content"]
            lesson_start = content.find("LESSON: ")
            if lesson_start >= 0:
                lesson_text = content[lesson_start + 8:]
                adj_start = lesson_text.find(" ADJUSTMENT: ")
                if adj_start >= 0:
                    lesson_text = lesson_text[:adj_start]
            else:
                lesson_text = content[:120]

            # Skip if already covered by in-memory reflection
            if lesson_text.lower()[:60] in in_mem_lessons:
                continue

            conf = pfact["confidence"]
            warnings.append(f"⚠ [past session] {lesson_text} (confidence: {conf:.2f})")

        if not warnings:
            return ""

        header = "=== PAST FAILURE WARNINGS (avoid repeating these mistakes) ==="
        return header + "\n" + "\n".join(warnings[:top_k * 2]) + "\n"

    def analyze_side_effects(self, failure: FailureContext) -> List[str]:
        """Analyze a failed task for useful side-effects (Hindsight Experience Replay).

        When a task fails its primary goal X but achieves Y as a side-effect,
        store Y as a successful learning. This extracts value from failures.

        Args:
            failure: The failure context including achieved_outcomes list.

        Returns:
            List of fact_ids for side-effect learnings stored in LearningStore.
        """
        if not failure.achieved_outcomes:
            return []

        fact_ids = []
        for outcome in failure.achieved_outcomes:
            # Store each side-effect as a successful learning
            logger.info(f"[REFLEXION] side_effect: task failed '{failure.expected_outcome}' "
                        f"but achieved '{outcome}'")

            # In-memory: create a positive reflection
            side_ref = Reflection(
                id=self._next_id(),
                failure=failure,
                critique=f"Task failed primary goal but achieved: {outcome}",
                lesson=f"When attempting '{failure.action_type}' on '{failure.action_target}', "
                       f"even if the primary goal fails, '{outcome}' can be achieved as a side-effect.",
                action_adjustment=f"Consider targeting '{outcome}' directly if primary goal fails again",
                confidence=0.6,
            )
            self._reflections.append(side_ref)

            # Persistent: store as a success in LearningStore
            if self._learning_store is not None:
                try:
                    tags = ["side_effect", "hindsight", failure.action_type]
                    if failure.domain:
                        tags.append(failure.domain)

                    fid = self._learning_store.learn(
                        content=(
                            f"SIDE EFFECT SUCCESS: While attempting '{failure.expected_outcome}' "
                            f"via {failure.action_type}({failure.action_target}), "
                            f"achieved '{outcome}' as a side-effect. "
                            f"This outcome has value and can be targeted directly."
                        ),
                        category="pattern",
                        source=f"side_effect:{side_ref.id}",
                        tags=tags,
                    )
                    fact_ids.append(fid)

                    # Update expertise for the domain as partial success
                    if self._learning_system and failure.domain:
                        self._learning_system.expertise.update(failure.domain, success=True)

                except Exception as e:
                    logger.warning(f"[REFLEXION] side_effect persist failed: {e}")

        return fact_ids

    # ── End P1.11 additions ───────────────────────────────────  # signed: delta

    def on_failure(self, failure: FailureContext) -> Reflection:
        """
        Called when an action fails. Generates a verbal reflection.

        This is the core of Reflexion: instead of parameter updates,
        the agent writes a natural language critique that captures
        the operational knowledge of what went wrong and how to fix it.
        """
        logger.info(f"[REFLEXION] trigger: {failure.action_type}({failure.action_target}) "
                     f"failed: {failure.error_type}")

        # Track failure patterns
        pattern_key = f"{failure.action_type}:{failure.error_type}"
        self._failure_patterns[pattern_key] = self._failure_patterns.get(pattern_key, 0) + 1

        # Generate reflection
        critique, lesson, adjustment = self._generate_reflection(failure)

        reflection = Reflection(
            id=self._next_id(),
            failure=failure,
            critique=critique,
            lesson=lesson,
            action_adjustment=adjustment,
            confidence=self._compute_confidence(failure),
        )

        self._reflections.append(reflection)

        # Enforce max capacity
        if len(self._reflections) > self.max_reflections:
            # Remove oldest, least-applied reflections
            self._reflections.sort(key=lambda r: (r.applied_count, r.timestamp))
            self._reflections = self._reflections[-self.max_reflections:]

        # Store in memory
        if self.memory:
            self.memory.store_episodic(
                f"REFLECTION: {critique} LESSON: {lesson}",
                tags=["reflexion", failure.action_type, failure.error_type],
                source_action="reflexion",
                importance=0.9,  # High importance for failures
            )

        # P1.11: Persist to LearningStore for cross-session recall  # signed: delta
        self._persist_reflection(reflection)

        # P1.11: Analyze side-effects — extract value from failures  # signed: delta
        if failure.achieved_outcomes:
            side_ids = self.analyze_side_effects(failure)
            if side_ids:
                logger.info(f"[REFLEXION] side_effects: {len(side_ids)} stored from failure")

        logger.info(f"[REFLEXION] reflect: {critique[:100]}")
        logger.info(f"[REFLEXION] lesson: {lesson[:100]}")

        return reflection

    def get_relevant_reflections(self, action_type: str, target: str = "",
                                  context: str = "", limit: int = 3) -> List[Reflection]:
        """
        Retrieve past reflections relevant to a planned action.
        Used BEFORE executing an action to learn from past mistakes.
        """
        scored = []

        for ref in self._reflections:
            score = 0.0

            # Same action type gets a boost
            if ref.failure.action_type == action_type:
                score += 0.4

            # Similar target
            if target and target in ref.failure.action_target:
                score += 0.3

            # Context similarity (simple word overlap)
            if context:
                ctx_words = set(context.lower().split())
                ref_words = set(ref.critique.lower().split())
                overlap = len(ctx_words & ref_words)
                score += min(0.3, overlap * 0.05)

            if score > 0.1:
                scored.append((score, ref))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = [ref for _, ref in scored[:limit]]
        for ref in results:
            ref.applied_count += 1

        if results:
            logger.info(f"[REFLEXION] retrieve: {len(results)} relevant reflections "
                         f"for {action_type}({target})")

        return results

    def should_adjust_action(self, action_type: str, target: str = "") -> Optional[str]:
        """
        Quick check: should the planned action be adjusted based on past failures?
        Returns adjustment string or None.
        """
        reflections = self.get_relevant_reflections(action_type, target, limit=1)
        if reflections and reflections[0].confidence > 0.5:
            return reflections[0].action_adjustment
        return None

    def _generate_reflection(self, failure: FailureContext) -> Tuple[str, str, str]:
        """
        Generate verbal critique, lesson, and adjustment from failure context.

        Uses VLM if available, otherwise rule-based generation.
        """
        # Rule-based reflection generation
        critique = self._rule_based_critique(failure)
        lesson = self._rule_based_lesson(failure)
        adjustment = self._rule_based_adjustment(failure)

        return critique, lesson, adjustment

    def _rule_based_critique(self, f: FailureContext) -> str:
        """Generate critique based on failure patterns."""
        critiques = {
            "timeout": (
                f"Action '{f.action_type}' on '{f.action_target}' timed out. "
                f"The element may not be visible, the page may still be loading, "
                f"or the target doesn't exist in the current state."
            ),
            "element_not_found": (
                f"Could not find element '{f.action_target}'. "
                f"The page layout may have changed, the element may require scrolling, "
                f"or the marker ID is stale from a previous grounding pass."
            ),
            "wrong_state": (
                f"Action produced unexpected result. Expected: '{f.expected_outcome}', "
                f"got: '{f.actual_outcome}'. The pre-conditions for this action "
                f"were not met."
            ),
            "no_change": (
                f"Action '{f.action_type}' on '{f.action_target}' produced no visual change. "
                f"The click may have missed the target, or the element is non-interactive."
            ),
            "navigation_error": (
                f"Navigation to '{f.action_value}' failed. The URL may be incorrect, "
                f"the site may be blocking automated access, or network issues occurred."
            ),
        }

        base = critiques.get(f.error_type, f"Action failed: {f.error_message}")

        # Add pattern frequency insight
        pattern_key = f"{f.action_type}:{f.error_type}"
        count = self._failure_patterns.get(pattern_key, 1)
        if count > 2:
            base += f" This pattern has failed {count} times - systematic issue."

        return base

    def _rule_based_lesson(self, f: FailureContext) -> str:
        """Generate lesson from failure."""
        lessons = {
            "timeout": "Wait for page load before interacting. Use explicit wait conditions.",
            "element_not_found": "Re-ground the screenshot before clicking. Elements may have moved.",
            "wrong_state": "Verify pre-conditions with a screenshot check before acting.",
            "no_change": "Try a different target element. The current one may be decorative.",
            "navigation_error": "Verify URL format. Try alternative navigation paths.",
        }

        return lessons.get(f.error_type,
                           f"Investigate root cause of '{f.error_type}' before retrying.")

    def _rule_based_adjustment(self, f: FailureContext) -> str:
        """Generate specific action adjustment."""
        adjustments = {
            "timeout": f"Add wait(2000) before {f.action_type}({f.action_target})",
            "element_not_found": f"Re-run visual grounding, then try alternative element",
            "wrong_state": f"Verify current page state matches expected before {f.action_type}",
            "no_change": f"Try clicking adjacent element or scrolling to reveal target",
            "navigation_error": f"Try alternative URL or use search instead of direct navigation",
        }

        return adjustments.get(f.error_type, "Retry with modified parameters")

    def _compute_confidence(self, f: FailureContext) -> float:
        """Compute confidence in the reflection based on available evidence."""
        confidence = 0.5

        # More context = higher confidence
        if f.error_message:
            confidence += 0.1
        if f.pre_state_description:
            confidence += 0.1
        if f.post_state_description:
            confidence += 0.1

        # Repeated failures on same pattern = higher confidence
        pattern_key = f"{f.action_type}:{f.error_type}"
        count = self._failure_patterns.get(pattern_key, 1)
        if count > 1:
            confidence += min(0.2, count * 0.05)

        return min(1.0, confidence)

    @property
    def stats(self) -> dict:
        return {
            "total_reflections": len(self._reflections),
            "failure_patterns": dict(self._failure_patterns),
            "useful_reflections": sum(1 for r in self._reflections if r.is_useful),
            "total_applications": sum(r.applied_count for r in self._reflections),
        }


class DynaActFilter:
    """
    Dynamic Action Space Filter (DynaAct).

    Instead of presenting ALL possible actions to the agent, DynaAct
    constructs a compact, context-aware action space based on:
    - Current task description
    - Visible UI element types
    - Past successful actions in similar states
    - Past failed actions (via Reflexion) to avoid

    This reduces cognitive load on the VLM and prevents hallucinated commands.

    LOG FORMAT:
        [DYNAACT] filter  -- 25 candidate actions -> 6 relevant actions
        [DYNAACT] boost   -- "click search_button" boosted (matches task "search for...")
        [DYNAACT] block   -- "click mark_7" blocked (failed 3 times in similar state)
    """

    def __init__(self, reflexion: Optional[ReflexionEngine] = None,
                 memory=None):
        self.reflexion = reflexion
        self.memory = memory
        self._action_success_history: Dict[str, int] = {}

    def filter_actions(self, candidate_actions: list,
                       task_description: str,
                       current_state_description: str = "") -> list:
        """Filter candidate actions to a context-relevant subset."""
        task_lower = task_description.lower()
        scored = [
            (self._score_action(action, task_lower), action)
            for action in candidate_actions
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        n = max(3, min(8, len(scored) // 2))
        filtered = [action for _, action in scored[:n]]
        logger.info(f"[DYNAACT] filter: {len(candidate_actions)} -> {len(filtered)} actions")
        return filtered

    def _score_action(self, action, task_lower: str) -> float:
        """Compute relevance score for a single action."""
        score = 0.5
        action_str = str(action).lower()

        task_words = set(task_lower.split())
        action_words = set(action_str.split())
        score += len(task_words & action_words) * 0.15

        action_key = action_str[:50]
        if action_key in self._action_success_history:
            score += min(0.3, self._action_success_history[action_key] * 0.1)

        if self.reflexion:
            action_type = getattr(action, 'action_type', str(type(action).__name__))
            target = getattr(action, 'target', '')
            adjustment = self.reflexion.should_adjust_action(str(action_type), str(target))
            if adjustment:
                score -= 0.3
        return score

    def record_success(self, action_str: str):
        """Record a successful action for future boosting."""
        key = action_str[:50].lower()
        self._action_success_history[key] = self._action_success_history.get(key, 0) + 1

    def record_failure(self, action_str: str):
        """Record a failed action (will be captured by Reflexion)."""
        key = action_str[:50].lower()
        self._action_success_history[key] = max(0,
            self._action_success_history.get(key, 0) - 2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== Reflexion + DynaAct Test ===\n")

    # Test Reflexion (with optional LearningStore integration)
    try:
        from core.cognitive.memory import EpisodicMemory
        memory = EpisodicMemory()
    except Exception:
        memory = None
    reflexion = ReflexionEngine(memory=memory)

    # Simulate failures
    f1 = FailureContext(
        action_type="click", action_target="mark_7",
        expected_outcome="Search button clicked",
        actual_outcome="Nothing happened",
        error_type="no_change",
        subtask="Click search button",
    )
    ref1 = reflexion.on_failure(f1)
    print(f"Reflection 1: {ref1.critique[:100]}")

    f2 = FailureContext(
        action_type="navigate", action_target="url_bar",
        action_value="https://arxiv.org/search",
        error_message="Connection refused",
        error_type="navigation_error",
        subtask="Navigate to arxiv",
    )
    ref2 = reflexion.on_failure(f2)
    print(f"Reflection 2: {ref2.critique[:100]}")

    # P1.11: Test side-effect analysis  # signed: delta
    f3 = FailureContext(
        action_type="deploy", action_target="production",
        expected_outcome="Service deployed successfully",
        actual_outcome="Deploy failed due to missing config",
        error_type="wrong_state",
        subtask="Deploy service",
        domain="devops",
        achieved_outcomes=[
            "Config validation script created",
            "Staging environment verified healthy",
        ],
    )
    ref3 = reflexion.on_failure(f3)
    print(f"\nReflection 3 (with side-effects): {ref3.critique[:100]}")

    # P1.11: Test pre-task context injection  # signed: delta
    context = reflexion.get_pre_task_context(
        task_description="Deploy the Python service to production",
        action_type="deploy",
        target="production",
    )
    print(f"\n--- Pre-task context injection ---")
    print(context if context else "(no relevant warnings)")

    # Retrieve relevant reflections
    relevant = reflexion.get_relevant_reflections("click", "mark_", "button")
    print(f"\nRelevant reflections for 'click': {len(relevant)}")
    for r in relevant:
        print(f"  - {r.lesson}")

    # P1.11: Test persistent reflections query  # signed: delta
    persistent = reflexion.get_persistent_reflections("click element not found")
    print(f"\nPersistent reflections from LearningStore: {len(persistent)}")
    for p in persistent:
        print(f"  - [{p['confidence']:.2f}] {p['content'][:100]}")

    # Test DynaAct
    print("\n--- DynaAct Filter ---")
    dynaact = DynaActFilter(reflexion=reflexion, memory=memory)

    candidates = [
        "click search_button",
        "click mark_7",  # Should be penalized (failed before)
        "type search_field query",
        "scroll page down",
        "key Enter",
        "click next_page",
        "navigate url_bar",  # Should be penalized
        "click submit_form",
        "wait 2000",
    ]

    filtered = dynaact.filter_actions(
        candidates,
        task_description="Search for AI papers on arxiv",
    )
    print(f"Filtered: {filtered}")

    print(f"\nReflexion stats: {json.dumps(reflexion.stats)}")
