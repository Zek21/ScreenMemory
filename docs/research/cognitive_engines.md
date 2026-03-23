# Cognitive Engines: Self-Correcting Reasoning for Autonomous Agents

> **Version:** 1.0 | **Python:** ≥3.8 | **License:** Proprietary

## Summary

The Cognitive Engines suite provides three complementary reasoning architectures for autonomous AI agents: **ReflexionEngine** implements verbal self-critique that learns from failures through natural language reflections instead of parameter updates; **GraphOfThoughts** replaces linear chain-of-thought reasoning with a graph topology that enables multi-path exploration, branch aggregation, and backtracking; and **HierarchicalPlanner** decomposes high-level goals into executable subtask sequences with built-in self-reflective verification and automatic replanning on failure. Together, these engines enable agents to reason non-linearly, learn from mistakes across sessions, and execute complex multi-step plans with real-time correction. The implementations are grounded in recent academic research — Reflexion (Shinn et al., 2023), Graph of Thoughts (Besta et al., 2024), and hierarchical task decomposition patterns from Agent-E and related work.

---

## Requirements

| Requirement | Value |
|-------------|-------|
| Python | ≥ 3.8 |
| OS | Windows / Linux / macOS (cross-platform) |
| Hardware | CPU-only (GPU optional for VLM-enhanced scoring) |

### Install

```bash
# These modules are part of the ScreenMemory toolkit:
# core/cognitive/reflexion.py
# core/cognitive/graph_of_thoughts.py
# core/cognitive/planner.py

pip install -r requirements.txt
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| Python stdlib | — | `time`, `json`, `logging`, `dataclasses`, `enum`, `typing`, `hashlib` |
| `core.learning_store` (optional) | — | Persistent cross-session learning via SQLite |
| `core.cognitive.memory` (optional) | — | Episodic memory for in-session learning |
| VLM Analyzer (optional) | — | Vision-language model for visual verification |

---

## Quick Start

```python
# Reflexion — learn from failures
from core.cognitive.reflexion import ReflexionEngine, FailureContext

engine = ReflexionEngine()
failure = FailureContext(
    action_type="click", action_target="submit_btn",
    error_type="element_not_found",
    expected_outcome="Form submitted",
    actual_outcome="Button not visible",
)
reflection = engine.on_failure(failure)
print(reflection.lesson)
# → "Re-ground the screenshot before clicking. Elements may have moved."

# Graph of Thoughts — explore multiple reasoning paths
from core.cognitive.graph_of_thoughts import GraphOfThoughts

got = GraphOfThoughts()
root = got.add_thought("Design a caching strategy")
t1 = got.generate(root.id, "Redis for hot data: O(1) lookups")
t2 = got.generate(root.id, "SQLite for cold data: persistent, queryable")
merged = got.aggregate([t1.id, t2.id], "Two-tier cache: Redis hot + SQLite cold")
print(got.resolve())
# → "Goal: Design a caching strategy\nStep 1: ...\nConclusion: Two-tier cache..."

# Hierarchical Planner — decompose and execute goals
from core.cognitive.planner import HierarchicalPlanner

planner = HierarchicalPlanner()
plan = planner.create_plan("Search for AI papers on arxiv")
for i in range(len(plan.subtasks)):
    result = planner.execute_step(plan)
    print(f"Step {result.id}: {result.status.value}")
# → Step 1: success ... Step 6: success
```

---

## Engine 1: ReflexionEngine — Self-Correcting Reasoning

### Overview

ReflexionEngine implements **verbal reinforcement learning**: when an action fails, the agent generates a natural language critique explaining what went wrong, stores it in memory, and retrieves relevant past reflections before attempting similar actions in the future. This replaces traditional gradient-based learning with language-based experience accumulation.

Based on: *"Reflexion: Language Agents with Verbal Reinforcement Learning"* (Shinn et al., 2023).

### `ReflexionEngine`

```python
class ReflexionEngine:
    """
    Verbal reinforcement learning through self-critique.
    Learns from failures via natural language reflections stored in memory.
    """
```

#### Constructor

```python
ReflexionEngine(
    memory=None,
    vlm_analyzer=None,
    max_reflections: int = 100,
    learning_store=None
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `memory` | `EpisodicMemory` | `None` | In-session episodic memory for storing reflections |
| `vlm_analyzer` | `VLMAnalyzer` | `None` | Optional vision-language model for visual analysis |
| `max_reflections` | `int` | `100` | Maximum in-memory reflections before eviction |
| `learning_store` | `LearningStore` | `None` | Persistent SQLite store (auto-initialized if None) |

#### Methods

##### `on_failure(failure: FailureContext) → Reflection`

Core method: process a failure event and generate a verbal reflection. Stores the reflection in both episodic memory and persistent LearningStore. Automatically analyzes side-effects if `failure.achieved_outcomes` is populated.

| Parameter | Type | Description |
|-----------|------|-------------|
| `failure` | `FailureContext` | Complete context of the failed action |

**Returns:** `Reflection` — The generated critique, lesson, and action adjustment.

**Example:**
```python
failure = FailureContext(
    action_type="navigate",
    action_target="url_bar",
    action_value="https://arxiv.org/search",
    error_message="Connection refused",
    error_type="navigation_error",
)
ref = engine.on_failure(failure)
print(ref.critique)
# → "Navigation to 'https://arxiv.org/search' failed. The URL may be incorrect..."
print(ref.lesson)
# → "Verify URL format. Try alternative navigation paths."
```

##### `get_relevant_reflections(action_type: str, target: str = "", context: str = "", limit: int = 3) → List[Reflection]`

Retrieve past reflections relevant to a planned action. Uses scoring based on action type match, target similarity, and context word overlap. Increments `applied_count` on retrieved reflections.

**Returns:** `List[Reflection]` — Ranked list of relevant past reflections.

##### `should_adjust_action(action_type: str, target: str = "") → Optional[str]`

Quick check whether a planned action should be adjusted based on past failures. Returns the adjustment string if a high-confidence reflection exists, or `None`.

##### `get_pre_task_context(task_description: str, action_type: str = "", target: str = "", top_k: int = 3) → str`

Build a warning block from past failures for injection into task preambles. Combines in-memory and persistent reflections. Returns empty string if no relevant warnings exist.

**Returns:** `str` — Formatted warning block or empty string.

##### `get_persistent_reflections(query: str, top_k: int = 3) → List[Dict]`

Query the persistent LearningStore for past reflections matching a task description. Returns structured dicts with `fact_id`, `content`, `confidence`, `reinforcement_count`, and `tags`.

##### `analyze_side_effects(failure: FailureContext) → List[str]`

Analyze a failed task for useful side-effects (Hindsight Experience Replay). When a task fails its primary goal X but achieves Y, stores Y as a successful learning.

**Returns:** `List[str]` — Fact IDs of stored side-effect learnings.

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `stats` | `dict` | Total reflections, failure patterns, useful count, total applications |

---

### `FailureContext`

| Field | Type | Description |
|-------|------|-------------|
| `action_type` | `str` | Type of action that failed (click, navigate, type, etc.) |
| `action_target` | `str` | Target element or resource |
| `action_value` | `str` | Value associated with the action (URL, text, etc.) |
| `expected_outcome` | `str` | What should have happened |
| `actual_outcome` | `str` | What actually happened |
| `error_message` | `str` | Error message from the failure |
| `error_type` | `str` | Error category: `timeout`, `element_not_found`, `wrong_state`, `no_change`, `navigation_error` |
| `pre_state_description` | `str` | Description of state before the action |
| `post_state_description` | `str` | Description of state after the action |
| `achieved_outcomes` | `List[str]` | Side-effects achieved despite primary failure |
| `domain` | `str` | Domain context (e.g., "python", "security") |
| `files_involved` | `List[str]` | Files related to the failure |

### `Reflection`

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Unique reflection ID (e.g., `ref_0001`) |
| `failure` | `FailureContext` | The failure that triggered this reflection |
| `critique` | `str` | Verbal self-critique: "I failed because..." |
| `lesson` | `str` | Extracted lesson: "Next time I should..." |
| `action_adjustment` | `str` | Specific action modification to apply |
| `confidence` | `float` | Confidence score (0.0–1.0) |
| `applied_count` | `int` | Number of times this reflection has been retrieved |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `is_useful` | `bool` | True if applied at least once with confidence > 0.3 |

---

### `DynaActFilter`

Dynamic Action Space Filter that constructs a context-aware action space by boosting relevant actions and penalizing previously failed ones.

```python
class DynaActFilter:
    """Filters candidate actions based on task context and past experience."""
```

#### Constructor

```python
DynaActFilter(reflexion: ReflexionEngine = None, memory=None)
```

#### Methods

##### `filter_actions(candidate_actions: list, task_description: str, current_state_description: str = "") → list`

Filter a list of candidate actions down to the most relevant subset for the current task.

##### `record_success(action_str: str) → None`

Record that an action succeeded (boosts future relevance).

##### `record_failure(action_str: str) → None`

Record that an action failed (reduces future relevance).

---

## Engine 2: GraphOfThoughts — Branching Exploration

### Overview

GraphOfThoughts models reasoning as an **arbitrary directed graph** where vertices represent discrete thought units and edges represent logical dependencies. Unlike linear chain-of-thought, GoT enables parallel exploration of multiple solution paths, merging of complementary findings, iterative refinement, and pruning of dead-end branches. This mirrors human lateral thinking and enables richer reasoning for complex multi-faceted problems.

Based on: *"Graph of Thoughts: Solving Elaborate Problems with Large Language Models"* (Besta et al., 2024).

### `GraphOfThoughts`

```python
class GraphOfThoughts:
    """
    Non-linear reasoning engine that explores multiple solution paths
    simultaneously and merges the best findings.
    """
```

#### Constructor

```python
GraphOfThoughts(
    max_depth: int = 10,
    max_branches: int = 5,
    prune_threshold: float = 0.2,
    scorer: Callable = None
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_depth` | `int` | `10` | Maximum depth of reasoning chains |
| `max_branches` | `int` | `5` | Maximum child branches per thought |
| `prune_threshold` | `float` | `0.2` | Minimum score to survive pruning |
| `scorer` | `Callable` | `None` | Custom scoring function; uses default heuristic if None |

#### Core Operations

##### `add_thought(content: str, score: float = 0.5, metadata: dict = None) → Thought`

Create the root thought (initial problem/goal). The first thought added becomes the graph root.

##### `generate(parent_id: str, content: str, score: float = 0.5, metadata: dict = None) → Thought`

**GENERATE** — Create a new thought branching from a parent. Represents exploring a new reasoning direction.

**Constraints:** Respects `max_depth` and `max_branches` limits.

##### `aggregate(thought_ids: List[str], merged_content: str, score: float = None) → Thought`

**AGGREGATE** — Merge multiple thought vertices into one. Represents synthesizing findings from parallel exploration. The merged thought becomes a child of all source thoughts. Score defaults to the weighted average of sources.

##### `refine(thought_id: str, new_content: str, score_delta: float = 0.1) → Thought`

**REFINE** — Create an improved version of an existing thought with new information. The original is preserved for history. Score increases by `score_delta`.

##### `score_thought(thought_id: str, new_score: float) → None`

Manually set a thought's score (clamped to 0.0–1.0).

##### `score_all() → None`

Score all active thoughts using the configured scorer function.

##### `prune(threshold: float = None) → int`

**PRUNE** — Remove branches with scores below the threshold. Returns the count of pruned thoughts. The root thought is never pruned.

#### Query Operations

##### `get_leaves() → List[Thought]`

Get all active leaf thoughts (endpoints of reasoning with no children).

##### `get_best_thought() → Optional[Thought]`

Get the highest-scoring active thought in the entire graph.

##### `get_best_path() → List[Thought]`

Trace the highest-scoring path from root to best leaf. Returns an ordered list representing the optimal reasoning chain.

##### `get_all_paths() → List[List[Thought]]`

Get all root-to-leaf paths in the graph.

##### `resolve() → str`

Resolve the graph into a final answer by tracing the best path and building a human-readable conclusion string. Marks the final thought as `FINAL`.

##### `to_text() → str`

Render the entire graph as indented text for debugging, including stats and the best path.

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `stats` | `dict` | Total thoughts, status breakdown, max depth, leaf count, best score |

---

### `Thought`

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Unique thought ID (e.g., `t_0001`) |
| `content` | `str` | The thought's textual content |
| `score` | `float` | Quality/relevance score (0.0–1.0) |
| `status` | `ThoughtStatus` | `ACTIVE`, `REFINED`, `MERGED`, `PRUNED`, or `FINAL` |
| `depth` | `int` | Distance from root in the graph |
| `parent_ids` | `List[str]` | IDs of parent thoughts (multiple for aggregated thoughts) |
| `child_ids` | `List[str]` | IDs of child thoughts |
| `refinement_count` | `int` | Number of times this thought has been refined |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `is_leaf` | `bool` | True if the thought has no children |
| `is_root` | `bool` | True if the thought has no parents |

---

### `GoTReasoner`

High-level reasoning interface that wraps GraphOfThoughts with optional VLM scoring and episodic memory integration.

```python
class GoTReasoner:
    """High-level reasoning using Graph of Thoughts."""
```

#### Constructor

```python
GoTReasoner(vlm_analyzer=None, memory=None)
```

#### Methods

##### `reason(problem: str, perspectives: List[str] = None, max_depth: int = 5) → Tuple[str, GraphOfThoughts]`

Solve a problem by creating parallel branches for each perspective, optionally refining via VLM, aggregating, scoring, pruning, and resolving.

**Returns:** Tuple of (resolution string, GraphOfThoughts instance).

---

## Engine 3: HierarchicalPlanner — Multi-Step Execution

### Overview

HierarchicalPlanner implements a **two-tier planning architecture** with self-reflective verification: a Strategic Planner decomposes goals into subtask sequences, a Tactical Executor handles each step, and a Reflector validates outcomes and triggers replanning on failure. This creates a robust execution pipeline that can recover from unexpected failures by retrying, replanning, or aborting gracefully.

### `HierarchicalPlanner`

```python
class HierarchicalPlanner:
    """
    Decomposes goals into subtask sequences and manages execution
    with self-reflective verification at each step.
    """
```

#### Constructor

```python
HierarchicalPlanner(vlm_analyzer=None, memory=None)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `vlm_analyzer` | `VLMAnalyzer` | `None` | Optional vision-language model for intelligent decomposition |
| `memory` | `EpisodicMemory` | `None` | Optional episodic memory for storing execution history |

#### Methods

##### `create_plan(goal: str, context: str = "") → Plan`

Decompose a goal into a sequence of subtasks. Uses VLM for intelligent decomposition if available; otherwise, applies template-based decomposition matching common task patterns:

| Pattern | Keywords | Subtask Count |
|---------|----------|---------------|
| Search/Research | search, find, research, look up | 6 steps |
| Launch/Open | open, launch, start | 3 steps |
| Navigation | navigate, go to, visit | 4 steps |
| Content Creation | write, create, draft | 4 steps |
| Generic | (everything else) | 4 steps |

**Returns:** `Plan` — The complete execution plan.

##### `execute_step(plan: Plan, executor_fn: Callable = None) → Subtask`

Execute the next subtask in the plan. If `executor_fn` is provided, it receives the subtask and should return a result string. Without an executor, steps are simulated as successful.

Failure handling follows a three-tier recovery protocol:
1. **Retry** — If retries remain (default: 3 per subtask), mark as RETRYING
2. **Replan** — If all retries exhausted but replans remain (default: 3), generate an alternative subtask
3. **Fail** — If both retry and replan budgets are exhausted, mark as FAILED

##### `verify_outcome(subtask, pre_screenshot=None, post_screenshot=None, change_detector=None) → bool`

Self-reflective verification: compare expected vs. actual outcome using visual change detection and optional VLM analysis.

| Parameter | Type | Description |
|-----------|------|-------------|
| `subtask` | `Subtask` | The subtask to verify |
| `pre_screenshot` | `bytes` | Screenshot before action |
| `post_screenshot` | `bytes` | Screenshot after action |
| `change_detector` | `ChangeDetector` | Visual change detection engine |

**Returns:** `bool` — True if the outcome matches expectations.

##### `get_plan_summary(plan: Plan) → str`

Generate a human-readable plan summary with status icons for each step.

---

### `Plan`

| Field | Type | Description |
|-------|------|-------------|
| `goal` | `str` | The high-level goal being planned |
| `subtasks` | `List[Subtask]` | Ordered sequence of subtasks |
| `status` | `TaskStatus` | Overall plan status |
| `current_step` | `int` | Index of the next subtask to execute |
| `replan_count` | `int` | Number of replanning attempts used |
| `max_replans` | `int` | Maximum allowed replanning attempts (default: 3) |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `progress` | `str` | Human-readable progress (e.g., `"3/6"`) |
| `is_complete` | `bool` | True when all subtasks have succeeded |

### `Subtask`

| Field | Type | Description |
|-------|------|-------------|
| `id` | `int` | Subtask sequence number |
| `description` | `str` | What this subtask does |
| `actions` | `List[Action]` | Individual executable actions |
| `status` | `TaskStatus` | `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `RETRYING`, `REPLANNED`, `ABORTED` |
| `retries` | `int` | Current retry count |
| `max_retries` | `int` | Maximum retries allowed (default: 3) |
| `result` | `str` | Execution result text |
| `error` | `str` | Error message if failed |
| `verification_result` | `str` | Outcome of self-reflective verification |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `elapsed_ms` | `float` | Execution time in milliseconds |

### `Action`

| Field | Type | Description |
|-------|------|-------------|
| `action_type` | `ActionType` | `CLICK`, `TYPE`, `KEY`, `SCROLL`, `WAIT`, `NAVIGATE`, `SCREENSHOT`, `ANALYZE`, `EXTRACT`, `CUSTOM` |
| `target` | `str` | Mark ID, coordinates, or key sequence |
| `value` | `str` | Text to type, URL to navigate, etc. |
| `expected_outcome` | `str` | What should change after this action |
| `timeout_ms` | `int` | Action timeout in milliseconds (default: 5000) |

---

## Code Examples

### Example 1: Learning From Failures Across Sessions

```python
from core.cognitive.reflexion import ReflexionEngine, FailureContext

# Engine auto-connects to persistent LearningStore
engine = ReflexionEngine()

# Simulate a failure with side-effects
failure = FailureContext(
    action_type="deploy",
    action_target="production",
    expected_outcome="Service deployed successfully",
    actual_outcome="Deploy failed due to missing config",
    error_type="wrong_state",
    domain="devops",
    achieved_outcomes=[
        "Config validation script created",
        "Staging environment verified healthy",
    ],
)

# Process the failure — generates reflection + stores side-effects
reflection = engine.on_failure(failure)
print(f"Critique: {reflection.critique}")
# Output:
# Critique: Action produced unexpected result. Expected: 'Service deployed
# successfully', got: 'Deploy failed due to missing config'. The pre-conditions
# for this action were not met.

print(f"Lesson: {reflection.lesson}")
# Output:
# Lesson: Verify pre-conditions with a screenshot check before acting.

# Before a similar task, inject past failure warnings
context = engine.get_pre_task_context(
    task_description="Deploy the Python service to staging",
    action_type="deploy",
)
print(context)
# Output:
# === PAST FAILURE WARNINGS (avoid repeating these mistakes) ===
# ⚠ Verify pre-conditions with a screenshot check before acting. (confidence: 0.70)
```

### Example 2: Multi-Path Problem Solving with Graph of Thoughts

```python
from core.cognitive.graph_of_thoughts import GraphOfThoughts

got = GraphOfThoughts(max_depth=5, prune_threshold=0.15)

# Define the root problem
root = got.add_thought("Design architecture for real-time data pipeline", score=0.3)

# Explore parallel approaches
kafka = got.generate(root.id, "Apache Kafka: distributed log, 100K msgs/sec", score=0.7)
redis = got.generate(root.id, "Redis Streams: in-memory, sub-ms latency", score=0.6)
rabbit = got.generate(root.id, "RabbitMQ: mature, rich routing", score=0.5)
naive = got.generate(root.id, "Poll database every second", score=0.1)

# Refine promising paths with deeper analysis
kafka_r = got.refine(kafka.id,
    "Kafka with Schema Registry: type safety + backward compat + exactly-once",
    score_delta=0.15)
redis_r = got.refine(redis.id,
    "Redis Streams with consumer groups: multi-consumer, ACK-based delivery",
    score_delta=0.1)

# Prune low-quality branches
pruned = got.prune()
print(f"Pruned {pruned} low-scoring branches")
# Output: Pruned 1 low-scoring branches

# Merge the best findings into a synthesis
merged = got.aggregate(
    [kafka_r.id, redis_r.id],
    "Hybrid: Kafka for durable ingestion + Redis Streams for hot path. "
    "Kafka handles back-pressure and replay; Redis handles sub-ms fan-out.",
    score=0.9,
)

# Resolve to final answer
print(got.resolve())
# Output:
# Goal: Design architecture for real-time data pipeline
# Step 1: Apache Kafka: distributed log, 100K msgs/sec
# Step 2: Kafka with Schema Registry: type safety + backward compat + exactly-once
# Conclusion: Hybrid: Kafka for durable ingestion + Redis Streams for hot path...

print(f"\nGraph stats: {got.stats}")
# Output:
# Graph stats: {'total_thoughts': 8, 'statuses': {'active': 1, 'refined': 2,
# 'merged': 2, 'pruned': 1, 'final': 1}, 'max_depth': 3, 'leaf_count': 1, ...}
```

### Example 3: Goal Decomposition and Fault-Tolerant Execution

```python
from core.cognitive.planner import HierarchicalPlanner, TaskStatus
import json

planner = HierarchicalPlanner()

# Create a plan for a research task
plan = planner.create_plan("Search for latest AI agent research papers on arxiv")
print(f"Created plan with {len(plan.subtasks)} steps:")
# Output: Created plan with 6 steps:

# Execute each step with a custom executor
def my_executor(subtask):
    """Simulate execution — replace with real actions."""
    if "search engine" in subtask.description.lower():
        return "Navigated to Google Scholar"
    if "enter search" in subtask.description.lower():
        return "Searched for 'autonomous AI agent 2024'"
    return f"Completed: {subtask.description}"

for i in range(len(plan.subtasks)):
    result = planner.execute_step(plan, executor_fn=my_executor)
    print(f"  Step {result.id}: {result.status.value} ({result.elapsed_ms:.0f}ms)")
    # Output:
    #   Step 1: success (2ms)
    #   Step 2: success (1ms)
    #   Step 3: success (1ms)
    #   ...

# View the full summary
print(planner.get_plan_summary(plan))
# Output:
# Plan: Search for latest AI agent research papers on arxiv
# Status: success | Progress: 6/6 | Replans: 0
#
#   ✅ Step 1: Open web browser → completed: Open web browser (2ms)
#   ✅ Step 2: Navigate to search engine → Navigated to Google Scholar (1ms)
#   ✅ Step 3: Enter search query → Searched for 'autonomous AI agent 2024' (1ms)
#   ✅ Step 4: Analyze search results → completed: Analyze search results (1ms)
#   ✅ Step 5: Extract relevant information → completed: Extract... (1ms)
#   ✅ Step 6: Store findings in memory → completed: Store findings... (1ms)
```

### Example 4: Combining All Three Engines

```python
from core.cognitive.reflexion import ReflexionEngine, FailureContext
from core.cognitive.graph_of_thoughts import GraphOfThoughts
from core.cognitive.planner import HierarchicalPlanner

# Initialize all engines
reflexion = ReflexionEngine()
planner = HierarchicalPlanner()

# Step 1: Use GoT to explore approaches to a complex problem
got = GraphOfThoughts()
root = got.add_thought("Automate data extraction from competitor websites")
approach_a = got.generate(root.id, "Use headless Chrome + CSS selectors", score=0.6)
approach_b = got.generate(root.id, "Use API reverse engineering", score=0.7)
approach_c = got.generate(root.id, "Use visual grounding on screenshots", score=0.5)
got.score_all()
best = got.get_best_thought()
print(f"Best approach: {best.content}")

# Step 2: Create a plan based on the best approach
plan = planner.create_plan(best.content)

# Step 3: Before executing, check for past failure warnings
context = reflexion.get_pre_task_context(
    task_description=best.content,
    action_type="navigate",
)
if context:
    print(f"Warning: {context}")

# Step 4: Execute the plan, learning from any failures
for i in range(len(plan.subtasks)):
    subtask = planner.execute_step(plan)
    if subtask.status.value == "failed":
        # Feed the failure into Reflexion for learning
        failure = FailureContext(
            action_type=subtask.description.split()[0].lower(),
            action_target=subtask.description,
            error_message=subtask.error,
            error_type="wrong_state",
        )
        ref = reflexion.on_failure(failure)
        print(f"Learned: {ref.lesson}")
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Cognitive Engine Stack                            │
│                                                                      │
│  ┌───────────────────┐    ┌────────────────────┐    ┌─────────────┐  │
│  │  ReflexionEngine   │    │  GraphOfThoughts    │    │Hierarchical │  │
│  │                   │    │                    │    │  Planner    │  │
│  │  on_failure() ────┼───▶│  generate()        │    │             │  │
│  │  get_relevant()   │    │  aggregate()       │───▶│ create_plan │  │
│  │  pre_task_ctx()   │    │  refine() / prune()│    │ execute()   │  │
│  │  side_effects()   │    │  resolve()         │    │ verify()    │  │
│  └────────┬──────────┘    └────────────────────┘    └──────┬──────┘  │
│           │                                                │         │
│           ▼                                                ▼         │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │                     Memory Layer                               │   │
│  │                                                                │   │
│  │  ┌─────────────────┐         ┌──────────────────────────────┐  │   │
│  │  │ EpisodicMemory   │         │ PersistentLearningSystem     │  │   │
│  │  │ (in-session)     │         │ (cross-session, SQLite)      │  │   │
│  │  │ vector-backed    │         │ BM25 search, confidence      │  │   │
│  │  │ importance decay │         │ reinforcement, expertise     │  │   │
│  │  └─────────────────┘         └──────────────────────────────┘  │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │                 Optional Visual Layer                          │   │
│  │                                                                │   │
│  │  ┌──────────────┐   ┌────────────────┐   ┌─────────────────┐  │   │
│  │  │ VLM Analyzer  │   │ Change Detector │   │ Set-of-Mark     │  │   │
│  │  │ visual verify │   │ pre/post diff   │   │ visual ground   │  │   │
│  │  └──────────────┘   └────────────────┘   └─────────────────┘  │   │
│  └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

**Data flow through the engines:**

1. **Problem Analysis (GoT)** — Complex problems enter through GraphOfThoughts, which creates parallel reasoning branches. Each branch explores a different approach or perspective. Low-quality branches are pruned. The best findings are aggregated into a synthesis.

2. **Plan Generation (Planner)** — The synthesis from GoT feeds into HierarchicalPlanner, which decomposes it into an executable sequence of subtasks with specific actions, expected outcomes, and timeouts.

3. **Execution with Learning (Reflexion)** — As the plan executes, failures are captured by ReflexionEngine. Each failure generates a verbal critique, lesson, and action adjustment. These are stored in both in-session episodic memory and persistent LearningStore (SQLite). Side-effects from failed tasks are preserved as positive learnings.

4. **Feedback Loop** — Before subsequent tasks, the pre-task context injection queries both memory stores for relevant past failures, creating warning blocks that prevent repeating the same mistakes. The DynaActFilter uses Reflexion data to boost successful actions and penalize failed ones.

**Key design decisions:**

- **Language over gradients** — Reflexion uses natural language reflections instead of parameter updates. This makes learning interpretable, debuggable, and transferable across sessions.
- **Graph over chain** — GoT's graph topology enables exploring 5 approaches simultaneously and merging the best two, rather than committing to a single chain-of-thought early.
- **Verify-then-continue** — HierarchicalPlanner verifies each step's outcome before proceeding, catching failures early rather than propagating errors through the plan.
- **Persistent memory** — Reflections survive process restarts via SQLite, enabling genuine cross-session learning.

---

## When to Use Each Engine

| Scenario | Engine | Why |
|----------|--------|-----|
| **Action failed, need to learn** | ReflexionEngine | Generates verbal critique, stores lesson for future avoidance |
| **Complex problem with multiple approaches** | GraphOfThoughts | Explores paths in parallel, prunes bad ideas, merges best findings |
| **Multi-step task execution** | HierarchicalPlanner | Decomposes goal, executes steps, auto-recovers from failures |
| **Need past failure warnings before a task** | ReflexionEngine | `get_pre_task_context()` injects relevant warnings |
| **Comparing competing strategies** | GraphOfThoughts | Generate branches per strategy, score, select best |
| **Task with visual verification needs** | HierarchicalPlanner | `verify_outcome()` with pre/post screenshots and change detection |
| **Failed task had useful side-effects** | ReflexionEngine | `analyze_side_effects()` captures partial successes |
| **Filtering actions based on experience** | DynaActFilter + Reflexion | Boosts successful actions, penalizes failed patterns |
| **Full pipeline: analyze → plan → execute → learn** | All three combined | GoT for strategy → Planner for execution → Reflexion for learning |

---

## Performance

### Benchmarks

| Operation | Time | Conditions |
|-----------|------|------------|
| `on_failure()` | ~1ms | In-memory only; +5ms with LearningStore persist |
| `get_relevant_reflections()` | ~0.1ms | 100 reflections, linear scan |
| `get_persistent_reflections()` | ~5ms | SQLite BM25 search |
| `generate()` (GoT) | ~0.01ms | Graph node creation |
| `aggregate()` (GoT) | ~0.02ms | Multi-parent merge |
| `resolve()` (GoT) | ~0.1ms | Path tracing through graph |
| `create_plan()` | ~0.05ms | Template-based decomposition |
| `execute_step()` | ~0.5ms | Without external executor overhead |
| `verify_outcome()` | ~50ms | With change detection; ~200ms with VLM |

### Complexity

| Operation | Time | Space |
|-----------|------|-------|
| `on_failure()` | O(1) | O(n) reflections |
| `get_relevant_reflections()` | O(n) scan | O(k) results |
| `generate()` | O(1) | O(V+E) graph |
| `score_all()` | O(V) vertices | O(1) |
| `prune()` | O(V) vertices | O(1) |
| `get_best_path()` | O(V) trace | O(d) depth |
| `create_plan()` | O(1) template match | O(s) subtasks |

### Optimization Tips

- **Set `max_reflections` appropriately** — Keeping too many reflections slows retrieval. 100 is a good default; increase for long-running agents.
- **Use `prune_threshold` aggressively** — In GoT, prune early to avoid exploring dead-end branches. A threshold of 0.2–0.3 works well.
- **Combine in-memory and persistent recall** — `get_pre_task_context()` queries both stores automatically. Use it before every task for maximum learning.
- **Limit GoT depth for simple problems** — Set `max_depth=3` for straightforward problems; reserve depth 5–10 for genuinely complex multi-faceted reasoning.
- **Batch VLM calls** — If using visual verification, batch pre/post screenshots to minimize VLM round-trips.

---

## Troubleshooting / FAQ

### LearningStore not persisting

**Symptom:** Reflections don't survive process restarts.

**Cause:** `PersistentLearningSystem` from `core.learning_store` failed to initialize (missing SQLite database or import error).

**Fix:**
```python
# Check if LearningStore connected
engine = ReflexionEngine()
print(engine._learning_store is not None)  # Should be True

# If False, initialize manually:
from core.learning_store import PersistentLearningSystem
system = PersistentLearningSystem()
engine = ReflexionEngine(learning_store=system.store)
```

### GoT branches hitting max_depth

**Symptom:** `generate()` returns the parent thought instead of creating a new child.

**Cause:** The reasoning chain has reached `max_depth` (default: 10).

**Fix:**
```python
# Increase depth limit
got = GraphOfThoughts(max_depth=15)

# Or aggregate intermediate results to reset depth
merged = got.aggregate([deep_thought.id], "Summary so far", score=0.7)
# merged.depth = deep_thought.depth + 1, but content is now a fresh starting point
```

### Planner always produces generic steps

**Symptom:** Plan subtasks are generic ("Analyze current state", "Determine required actions") instead of domain-specific.

**Cause:** The goal description didn't match any keyword pattern for template decomposition (search, open, navigate, write), so the generic fallback was used.

**Fix:**
```python
# Include action keywords in the goal
plan = planner.create_plan("Search for and extract data from competitor websites")
# Now matches "search" pattern → 6 specific steps

# Or provide a VLM analyzer for intelligent decomposition
planner = HierarchicalPlanner(vlm_analyzer=my_vlm)
plan = planner.create_plan("Any complex goal here")
```

### FAQ

**Q: Can reflections be shared between multiple agents?**
A: Yes. The `PersistentLearningSystem` uses a shared SQLite database. Multiple agents reading from the same `learning_store.db` will see each other's reflections via `get_persistent_reflections()`.

**Q: Does GoT support cycles in the graph?**
A: The `get_best_path()` and `get_all_paths()` methods use visited-set tracking to prevent infinite loops, so cycles are safe but won't be traversed repeatedly.

**Q: Can I plug in a custom scorer for GoT?**
A: Yes. Pass a callable to the constructor: `GraphOfThoughts(scorer=my_scorer)`. The scorer receives a `Thought` object and should return a float in [0.0, 1.0].

**Q: How does the Planner decide when to replan vs. abort?**
A: Each subtask has `max_retries` (default: 3). After exhausting retries, if `plan.replan_count < plan.max_replans` (default: 3), replanning occurs. After exhausting both retry and replan budgets, the subtask is marked FAILED.

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-23 | Initial research report from source analysis |

---

*Generated from ScreenMemory research toolkit. See [TOOL_INVENTORY.md](TOOL_INVENTORY.md) for the full catalog.*
