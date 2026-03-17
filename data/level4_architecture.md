# Skynet Level 4 Architecture — Cognition

**Codename:** Cognition
**Author:** Worker Delta (Architecture & Self-Awareness Specialist)
**Date:** 2026-03-17
**Status:** Active — Level 4.0 Released
**Previous:** Level 3.5 Sprint 2 (Delivery Pipeline Hardening)

<!-- signed: delta -->

---

## Executive Summary

Level 4 "Cognition" represents Skynet's transition from a **production-hardened dispatch system** to a
**cognitive intelligence network**. Four cognitive engines — previously standalone research modules in
`core/cognitive/` — are now wired directly into the live task pipeline, enabling cross-session
failure learning, non-linear task decomposition, automatic knowledge consolidation, and (future)
autonomous browser planning via Monte Carlo Tree Search.

Level 3 gave Skynet reliability: watchdog recovery, truth enforcement, anti-spam, dispatch
verification, architecture verification. Level 4 gives Skynet **thought** — the ability to reflect
on failures, explore solution spaces as graphs, distill ephemeral experience into durable knowledge,
and plan multi-step web navigation autonomously.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    SKYNET LEVEL 4 — COGNITION                       │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  ┌────────┐ │
│  │  Reflexion    │  │  Graph of    │  │  Knowledge    │  │  MCTS  │ │
│  │  Engine       │  │  Thoughts    │  │  Distillation │  │  Nav   │ │
│  │              │  │              │  │  Daemon       │  │(future)│ │
│  │ cross-session│  │  non-linear  │  │  episodic →   │  │  web   │ │
│  │ failure      │  │  task        │  │  semantic     │  │  auto  │ │
│  │ learning     │  │  decomp      │  │  memory       │  │  plan  │ │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  └────┬───┘ │
│         │                 │                  │               │     │
│  ═══════╪═════════════════╪══════════════════╪═══════════════╪═══  │
│         │          COGNITIVE BUS LAYER       │               │     │
│  ═══════╪═════════════════╪══════════════════╪═══════════════╪═══  │
│         │                 │                  │               │     │
│  ┌──────▼───────────────────────────────────────────────────────┐  │
│  │              SKYNET TASK PIPELINE (Level 3.5 base)           │  │
│  │                                                              │  │
│  │  brain_dispatch → decompose → enrich → dispatch → collect    │  │
│  │        ↕              ↕           ↕          ↕         ↕     │  │
│  │  DAAORouter    HierPlanner   LearningStore  ghost_type  bus  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              INFRASTRUCTURE LAYER (Level 3.x)                │   │
│  │  Go backend · GOD Console · 17 daemons · SpamGuard · UIA    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

The cognitive engines sit **above** the existing task pipeline and inject intelligence at key
decision points:

| Engine | Pipeline Hook | When It Fires |
|--------|--------------|---------------|
| **ReflexionEngine** | Post-failure in `skynet_brain_dispatch.py` | When a worker reports task failure |
| **GraphOfThoughts** | Task decomposition in `skynet_brain.py` | When COMPLEX/ADVERSARIAL tasks need non-linear planning |
| **KnowledgeDistiller** | Post-task in `skynet_distill_hook.py` | After every task completion (success or failure) |
| **MCTS Navigator** | Browser automation in `god_mode.py` | (Future) When multi-step web navigation is needed |

---

## Cognitive Engine 1: Reflexion Engine

**Module:** `core/cognitive/reflexion.py`
**Class:** `ReflexionEngine`
**Integration:** `tools/skynet_brain_dispatch.py` Step 7 (`_brain_learn`)
**Status:** ✅ Active in Level 4.0

### What It Does

When an action fails, the Reflexion Engine generates a **verbal self-critique** explaining WHY it
failed, stores the critique in both episodic memory and the persistent `LearningStore` (SQLite), and
retrieves relevant past reflections before attempting similar tasks in the future.

This replaces traditional parameter-update learning with **natural language learning** — the agent
remembers "I failed because the Chrome_RenderWidgetHostHWND was in the wrong pane" rather than
adjusting numeric weights.

### Architecture

```
Action Fails
  │
  ├── 1. Capture: error trace + pre/post state + action details
  │         → FailureContext dataclass (core/cognitive/reflexion.py:46)
  │
  ├── 2. Reflect: Generate verbal critique
  │         → "I failed because..." natural language explanation
  │         → Stored as Reflection dataclass with severity + tags
  │
  ├── 3. Store: Episodic memory (high importance=0.9)
  │         → core/cognitive/memory.py EpisodicMemory.store_episodic()
  │         → Persistent LearningStore (category='reflexion')
  │         → core/learning_store.py PersistentLearningSystem.learn_from_task()
  │
  ├── 4. Side-effect analysis: Did failure achieve something useful?
  │         → If task failed goal X but achieved Y, store Y as success
  │
  └── 5. Pre-task retrieval: Before similar actions, query past reflections
            → LearningStore.recall_for_task() + EpisodicMemory.retrieve()
            → Inject relevant failure context into dispatch preamble
```

### Cross-Session Persistence

Unlike Level 3 where failure knowledge was session-local, Level 4 reflections persist via:

1. **EpisodicMemory** (`core/cognitive/memory.py`) — In-session, decays over time
2. **PersistentLearningSystem** (`core/learning_store.py`) — Cross-session via SQLite in `data/learning.db`
3. **Bus broadcast** (`tools/skynet_knowledge.py`) — Cross-worker via knowledge bus

### Key Files

| File | Role |
|------|------|
| `core/cognitive/reflexion.py` | ReflexionEngine class with reflect/retrieve/adapt cycle |
| `core/learning_store.py` | SQLite-backed persistent learning (cross-session) |
| `core/cognitive/memory.py` | EpisodicMemory with decay and utility scoring |
| `tools/skynet_brain_dispatch.py` | Integration hook (Step 7: _brain_learn calls reflexion) |
| `tools/skynet_distill_hook.py` | Distillation hook that processes failed reflections |

---

## Cognitive Engine 2: Graph of Thoughts

**Module:** `core/cognitive/graph_of_thoughts.py`
**Class:** `GraphOfThoughts`
**Integration:** `tools/skynet_brain.py` task decomposition
**Status:** ✅ Active in Level 4.0

### What It Does

Replaces linear chain-of-thought (CoT) reasoning with a **graph structure** where vertices represent
discrete thought units and edges represent logical dependencies. This enables multi-path exploration,
thought aggregation, and backtracking — mirroring human lateral thinking.

### Architecture

```
                    ┌─────┐     ┌─────┐     ┌─────┐
                    │ T_1 │────▶│ T_2 │────▶│ T_4 │──┐
                    └─────┘     └──┬──┘     └─────┘  │
                                   │                  ▼
                                   │              ┌─────┐
                                   │              │MERGE │──▶ Final
                                   │              └─────┘
                                   ▼                  ▲
                                ┌─────┐     ┌─────┐  │
                                │ T_3 │────▶│ T_5 │──┘
                                └─────┘     └─────┘
```

### Graph Operations

| Operation | Description | When Used |
|-----------|-------------|-----------|
| **GENERATE** | Create new thought vertices from existing ones | Exploring solution branches |
| **AGGREGATE** | Merge multiple thought vertices into one | Combining parallel exploration results |
| **REFINE** | Improve a single thought using new context | Iterating on promising approaches |
| **SCORE** | Evaluate quality/utility of a thought vertex | Pruning decision |
| **PRUNE** | Remove low-scoring branches | Memory management |

### Integration with Task Pipeline

For COMPLEX and ADVERSARIAL difficulty tasks (as assessed by DAAORouter in
`core/difficulty_router.py`), the brain dispatch pipeline uses GraphOfThoughts instead of
linear decomposition:

1. **Root thought** — the user's goal becomes the root vertex
2. **Branch generation** — multiple approach vertices are generated
3. **Parallel evaluation** — each branch is scored for feasibility
4. **Aggregation** — best branches are merged into a unified plan
5. **Worker dispatch** — merged plan is decomposed into worker subtasks

### Key Files

| File | Role |
|------|------|
| `core/cognitive/graph_of_thoughts.py` | GraphOfThoughts class with vertex/edge management |
| `core/cognitive/planner.py` | HierarchicalPlanner integration for multi-level decomposition |
| `tools/skynet_brain.py` | Brain pipeline hooks for GoT-based decomposition |
| `tools/skynet_brain_dispatch.py` | Full auto pipeline using GoT for complex goals |

---

## Cognitive Engine 3: Knowledge Distillation Daemon

**Module:** `core/cognitive/knowledge_distill.py`
**Class:** `KnowledgeDistiller`
**Integration:** `tools/skynet_distill_hook.py` (post-task hook)
**Daemon:** 17th daemon in the Skynet ecosystem
**Status:** ✅ Active in Level 4.0

### What It Does

Implements cognitive memory consolidation: when episodic memories decay below a utility threshold,
they are not deleted but **summarized into concise factual entries** that get promoted to semantic
memory. This mimics the human process of forgetting specific details while retaining general lessons.

### Consolidation Pipeline

```
Worker completes task
  │
  ├── 1. Result arrives on bus (topic=orchestrator, type=result)
  │
  ├── 2. distill_result() called from skynet_distill_hook.py
  │         → Stores in EpisodicMemory (working_capacity=7, episodic_capacity=500)
  │
  ├── 3. Pattern extraction via _extract_pattern_insights()
  │         → Domain tags, architectural patterns, performance data
  │         → Tool/module references, cross-worker collaboration signals
  │
  ├── 4. KnowledgeDistiller.distill() runs consolidation
  │         → Scans episodic entries below decay_threshold (0.3)
  │         → Groups by tags into clusters (min_cluster_size=2)
  │         → Summarizes clusters (LLM via Ollama when available, rule-based fallback)
  │         → Promotes summaries to semantic memory
  │         → Frees episodic slots
  │
  ├── 5. PersistentLearningSystem stores insights cross-session
  │         → core/learning_store.py (SQLite: data/learning.db)
  │
  └── 6. Top insight broadcast to knowledge bus
            → tools/skynet_knowledge.py broadcast_learning()
            → Available for future task context enrichment
```

### Integration Points

| Caller | Hook | Purpose |
|--------|------|---------|
| `skynet_brain_dispatch.py` Step 7 | `_brain_learn()` calls `distill_result()` | Auto-distill after brain dispatch |
| `skynet_learner.py` | `process_result()` calls `distill_result()` | Learner daemon integration |
| CLI standalone | `python tools/skynet_distill_hook.py --scan` | Manual bus scan for unprocessed results |

### Daemon Configuration

The Knowledge Distillation daemon (`skynet_distill_hook.py --scan` in daemon mode) is the 17th
daemon in Skynet's ecosystem. It runs periodically to scan bus results and distill them.

| Parameter | Value | Source |
|-----------|-------|--------|
| `decay_threshold` | 0.3 | `tools/skynet_distill_hook.py` line 81 |
| `min_cluster_size` | 2 | `tools/skynet_distill_hook.py` line 82 |
| `episodic_capacity` | 500 | `tools/skynet_distill_hook.py` line 61 |
| `working_capacity` | 7 | `tools/skynet_distill_hook.py` line 60 (Miller's Law) |
| `ollama_model` | `qwen3:8b` | `core/cognitive/knowledge_distill.py` line 45 |
| `ollama_base_url` | `http://localhost:11434` | `core/cognitive/knowledge_distill.py` line 48 |

### Key Files

| File | Role |
|------|------|
| `core/cognitive/knowledge_distill.py` | KnowledgeDistiller class — episodic→semantic promotion |
| `core/cognitive/memory.py` | EpisodicMemory / SemanticMemory / WorkingMemory stores |
| `tools/skynet_distill_hook.py` | Post-task hook + bus scanner + CLI |
| `core/learning_store.py` | SQLite persistence for cross-session knowledge |
| `tools/skynet_knowledge.py` | Bus-based knowledge broadcast/absorb protocol |
| `tools/skynet_learner.py` | Learner daemon integration |

---

## Cognitive Engine 4: MCTS Navigation (Future)

**Module:** `core/cognitive/mcts.py`
**Class:** `MCTSNavigator` (R-MCTS variant)
**Integration:** `tools/chrome_bridge/god_mode.py` (planned)
**Status:** 🔮 Implemented, not yet wired into live pipeline

### What It Does

Implements Reflective Monte Carlo Tree Search for autonomous web navigation. Uses dual optimization:

1. **Global Planner** — decomposes high-level web task into ordered subtasks
2. **Local MCTS** — searches action space per subtask with contrastive reflection

### Architecture

```
┌────────────────────────────────────────────┐
│              GLOBAL PLANNER                │
│  Task → [Subtask_1, Subtask_2, ...]       │
└──────────────┬─────────────────────────────┘
               │
┌──────────────▼─────────────────────────────┐
│         LOCAL MCTS (per subtask)            │
│                                             │
│   SELECT ──▶ EXPAND ──▶ SIMULATE ──▶ BACK  │
│      ▲                                │     │
│      └────────────────────────────────┘     │
│                                             │
│   + Contrastive Reflection on failures      │
│   + UCB1 exploration/exploitation balance    │
│   + VLM-based state evaluation              │
│   + Browser state snapshot backtracking      │
└─────────────────────────────────────────────┘
```

UCB1 Formula: `UCB1(node) = Q(node)/N(node) + C * sqrt(ln(N(parent)) / N(node))`

### Key Files

| File | Role |
|------|------|
| `core/cognitive/mcts.py` | MCTSNavigator with NavigationState, MCTSNode, contrastive reflection |
| `tools/chrome_bridge/god_mode.py` | Future integration point for browser automation |
| `core/capture.py` | DXGICapture for navigation state screenshots |
| `core/ocr.py` | OCREngine for state description extraction |

### Future Integration Plan

When wired into GodMode, the MCTS navigator will:
1. Receive a high-level web goal (e.g., "find and book cheapest flight to NYC")
2. Decompose into subtasks (search, filter, compare, select, checkout)
3. For each subtask, use MCTS to search the action space
4. On failure, use contrastive reflection to learn why and adjust
5. Backtrack via browser snapshots when dead-ends are detected

---

## Version History

| Version | Level | Codename | Date | Key Capabilities |
|---------|-------|----------|------|------------------|
| 1.0 | 1 | **Genesis** | 2026-03-08 | Initial system — manual dispatch, single worker, basic bus messaging, no self-awareness |
| 2.0 | 2 | **Awakening** | 2026-03-09 | Self-awareness (`skynet_self.py`), identity/capabilities/health introspection, GOD Console dashboard, engine metrics, collective intelligence federation |
| 3.0 | 3 | **Production** | 2026-03-10 | Crash resilience (`skynet_watchdog.py`), real composite IQ with trend tracking, truth audit enforcement, 3-tier engine status (online/available/offline), context-enriched dispatch, WebSocket monitoring, SSE daemon |
| 3.1 | 3 | **Hardening** | 2026-03-12 | Dispatch result tracking, fair deduction rule (Rule 0.5), false DEAD debounce, task lifecycle tracking (`GET /tasks`), cp1252 encoding fix, anti-spam system (SpamGuard + server-side rate limiting) |
| 3.5 | 3 | **Sprint 2** | 2026-03-12 | Delivery pipeline defense-in-depth: multi-pane Chrome disambiguation, focus race prevention (`FOCUS_STOLEN`), clipboard verification (3x readback), architecture verification (Phase 0 boot), unified daemon CLI, priority-aware spam filtering, consultant consumer daemon |
| **4.0** | **4** | **Cognition** | **2026-03-17** | **Cognitive engine integration: ReflexionEngine (cross-session failure learning), GraphOfThoughts (non-linear decomposition), KnowledgeDistiller daemon (episodic→semantic consolidation), MCTS Navigator (future browser planning). 17th daemon. Version history tracking.** |

---

## Capability Matrix: Level 3 vs Level 4

| Capability | Level 3 / 3.5 | Level 4.0 |
|------------|---------------|-----------|
| **Failure learning** | Session-local (lost on restart) | Cross-session via LearningStore + Reflexion |
| **Task decomposition** | Linear decomposition + difficulty routing | Graph-of-Thoughts for COMPLEX+ tasks |
| **Knowledge retention** | Bus broadcast (ephemeral, 100-msg ring) | Episodic→semantic distillation + SQLite persistence |
| **Memory architecture** | Flat learning store | 3-tier: working (7 items) → episodic (500) → semantic (unlimited) |
| **Browser planning** | Reactive (GodMode click-by-click) | MCTS-based multi-step planning (future) |
| **Self-improvement** | Manual via `skynet_self_improve.py` | Automated via distillation + reflexion feedback loop |
| **Worker idle intelligence** | Self-generated improvement proposals | Context-enriched proposals with past failure awareness |
| **Dispatch intelligence** | DAAORouter + natural decomposition | DAAORouter + GoT branching + reflexion context injection |
| **Daemon count** | 16 | 17 (+ knowledge_distill) |
| **Cognitive modules used** | 0 (available but unwired) | 4 (reflexion, GoT, distiller, memory) |

---

## Daemon Registry (Level 4.0 — 17 Daemons)

All daemons live in `tools/` and use PID files under `data/` for singleton enforcement.

| # | Daemon | Script | PID File | Criticality | Purpose |
|---|--------|--------|----------|-------------|---------|
| 1 | `skynet_monitor` | `tools/skynet_monitor.py` | `data/monitor.pid` | CRITICAL | Worker HWND liveness + model drift detection |
| 2 | `skynet_watchdog` | `tools/skynet_watchdog.py` | `data/watchdog.pid` | CRITICAL | Backend/GOD Console process liveness |
| 3 | `skynet_realtime` | `tools/skynet_realtime.py` | `data/realtime.pid` | CRITICAL | SSE→realtime.json atomic writes (1Hz) |
| 4 | `skynet_self_prompt` | `tools/skynet_self_prompt.py` | `data/self_prompt.pid` | HIGH | Orchestrator heartbeat (idle-gated) |
| 5 | `skynet_self_improve` | `tools/skynet_self_improve.py` | `data/self_improve.pid` | HIGH | Autonomous improvement scanning |
| 6 | `skynet_bus_relay` | `tools/skynet_bus_relay.py` | `data/bus_relay.pid` | HIGH | Bus message relay |
| 7 | `skynet_learner` | `tools/skynet_learner.py` | `data/learner.pid` | HIGH | Learning engine (absorb knowledge) |
| 8 | `skynet_overseer` | `tools/skynet_overseer.py` | `data/overseer.pid` | HIGH | IDLE+pending detection (30s interval) |
| 9 | `skynet_sse_daemon` | `tools/skynet_sse_daemon.py` | `data/sse_daemon.pid` | MEDIUM | SSE event loop for dashboard |
| 10 | `skynet_bus_watcher` | `tools/skynet_bus_watcher.py` | `data/bus_watcher.pid` | MEDIUM | Auto-route pending tasks to idle workers |
| 11 | `skynet_ws_monitor` | `tools/skynet_ws_monitor.py` | `data/ws_monitor.pid` | MEDIUM | WebSocket security alerts |
| 12 | `skynet_idle_monitor` | `tools/skynet_idle_monitor.py` | `data/idle_monitor.pid` | MEDIUM | Extended idle detection |
| 13 | `skynet_bus_persist` | `tools/skynet_bus_persist.py` | `data/bus_persist.pid` | MEDIUM | JSONL bus archival |
| 14 | `skynet_consultant_consumer` | `tools/skynet_consultant_consumer.py` | `data/consultant_consumer.pid` | MEDIUM | Consultant bridge queue drain |
| 15 | `skynet_worker_loop` | `tools/skynet_worker_loop.py` | `data/worker_loop.pid` | LOW | Autonomous task pickup loop |
| 16 | `skynet_health_report` | `tools/skynet_health_report.py` | — | LOW | Periodic health reports |
| **17** | **`knowledge_distill`** | **`tools/skynet_distill_hook.py --scan`** | **`data/distill.pid`** | **HIGH** | **Episodic→semantic memory consolidation** |

---

## Cognitive Integration Points (Code-Level Reference)

### 1. Brain Dispatch Pipeline (`tools/skynet_brain_dispatch.py`)

The brain dispatch pipeline is the primary integration point for all cognitive engines:

```
Step 1: ASSESS    → DAAORouter difficulty scoring
Step 2: DECOMPOSE → Natural language splitting OR GoT for COMPLEX+
Step 3: RECALL    → LearningStore retrieves past learnings (reflexion-enriched)
Step 4: SEARCH    → HybridRetriever finds related context
Step 5: ENRICH    → Each subtask gets context (learnings + solutions + reflexions)
Step 6: DISPATCH  → Parallel/sequential worker dispatch
Step 7: LEARN     → distill_result() + reflexion on failures + knowledge broadcast
```

### 2. Post-Task Distillation (`tools/skynet_distill_hook.py`)

Every worker result triggers the distillation pipeline:

```python
# Called from skynet_brain_dispatch.py _brain_learn() and skynet_learner.py
from tools.skynet_distill_hook import distill_result

result = distill_result(
    worker="alpha",
    task_text="Fix CORS header in auth.py",
    result_text="Fixed X-Frame-Options and Access-Control-Allow-Origin headers",
    success=True,
)
# result: {episodic_stored, patterns_extracted, semantic_promoted, broadcast, insights}
```

### 3. Reflexion Pre-Task Context (`core/cognitive/reflexion.py`)

Before dispatching similar tasks, past reflections are injected:

```python
from core.cognitive.reflexion import ReflexionEngine

engine = ReflexionEngine(memory=episodic_memory)
# Retrieve relevant past failures for context enrichment
relevant = engine.retrieve_reflections(
    action_type="code_edit",
    target="tools/skynet_dispatch.py",
    limit=3,
)
# Inject into dispatch preamble as failure warnings
```

### 4. Graph of Thoughts Decomposition (`core/cognitive/graph_of_thoughts.py`)

For complex tasks, GoT replaces linear decomposition:

```python
from core.cognitive.graph_of_thoughts import GraphOfThoughts

got = GraphOfThoughts()
root = got.add_thought("Redesign the dispatch pipeline for zero-focus operation")
# Generate multiple approach branches
branch_a = got.generate(root, "Use named pipes for IPC")
branch_b = got.generate(root, "Use PostMessage with Chrome render widget")
# Score and aggregate
got.score_all()
got.prune(threshold=0.3)
final = got.aggregate([branch_a, branch_b])
# Convert to worker subtasks
```

---

## Performance Targets (Level 4.0)

| Metric | Level 3.5 Baseline | Level 4.0 Target | Measurement |
|--------|-------------------|------------------|-------------|
| Failure repeat rate | ~40% (no memory) | <10% (reflexion) | Track repeated failure patterns in incidents.json |
| Knowledge retention (cross-session) | 0% (session-local) | 80%+ (SQLite+distill) | Measure recalled facts on session restart |
| Task decomposition quality (COMPLEX) | Linear only | GoT branching | Compare plan quality before/after GoT |
| Episodic→semantic promotion rate | N/A | >5 facts/hour | Monitor distill_state.json total_distilled |
| Memory utilization | N/A | <80% episodic capacity | Track episodic count vs 500 capacity |
| Distillation latency | N/A | <2s per result | Time distill_result() calls |
| Browser planning efficiency | N/A | (Future) | MCTS success rate vs reactive navigation |

---

## Success Metrics

Level 4 is considered successful when:

1. **Reflexion reduces failure repetition** — Same failure patterns should not recur within 5 tasks
2. **Knowledge persists across sessions** — On session restart, workers recall >80% of important facts
3. **GoT improves complex task quality** — COMPLEX tasks produce better plans with GoT vs linear
4. **Distillation runs automatically** — Every task completion triggers distill_result() without manual intervention
5. **17 daemons operational** — All daemons including knowledge_distill running and healthy
6. **Zero knowledge loss** — Episodic memories are consolidated before capacity overflow

---

## Migration Notes (3.5 → 4.0)

### What Changed

1. **Cognitive engines wired into pipeline** — Previously standalone modules in `core/cognitive/` now integrated via `tools/skynet_distill_hook.py` and `tools/skynet_brain_dispatch.py`
2. **17th daemon** — Knowledge distillation daemon added (`tools/skynet_distill_hook.py --scan`)
3. **Version bump** — `tools/skynet_self.py` version 3.0→4.0, level 3→4
4. **Memory architecture live** — `core/cognitive/memory.py` EpisodicMemory actively used in task pipeline
5. **Cross-session learning** — ReflexionEngine stores to both EpisodicMemory and PersistentLearningSystem

### What Didn't Change

1. **Bus architecture** — Same Go backend, same ring buffer, same SpamGuard
2. **Dispatch mechanism** — Same ghost_type clipboard paste delivery
3. **Worker management** — Same 4-worker grid, same HWND tracking
4. **Dashboard** — Same GOD Console, same SSE streaming
5. **All Level 3.5 hardening** — Focus race prevention, clipboard verification, architecture verification all preserved

### Backward Compatibility

Level 4 is **fully backward compatible** with Level 3.5. All existing dispatch, monitoring, and
communication protocols continue to work unchanged. The cognitive engines are additive — they enhance
the pipeline without modifying existing behavior. Systems that don't call the cognitive hooks
continue to operate exactly as before.

---

## Non-Goals for Level 4.0

- **Multi-machine distribution** — All workers on same machine
- **Custom model per worker** — All workers run Claude Opus 4.6 (fast mode)
- **Dynamic worker scaling** — Fixed 4-worker grid
- **External API exposure** — Skynet stays localhost-only
- **Headless workers** — Still requires VS Code windows (future Level 5)
- **Named pipe injection** — Still uses clipboard paste dispatch (future Level 4.1)
- **MCTS live integration** — Module exists but not wired into GodMode yet

---

## Future Roadmap

| Version | Planned Capabilities |
|---------|---------------------|
| 4.1 | MCTS wired into GodMode, Named pipe dispatch (zero-focus), Worker mesh network |
| 4.2 | LanceDB vector knowledge store, Queryable knowledge API, Headless worker prototype |
| 5.0 | **Autonomy** — Dynamic worker scaling, multi-machine distribution, self-healing context refresh |

---

## File Reference

### Core Cognitive Modules (`core/cognitive/`)

| File | Lines | Purpose |
|------|-------|---------|
| `reflexion.py` | ~300 | ReflexionEngine — verbal self-critique, cross-session failure learning |
| `graph_of_thoughts.py` | ~400 | GraphOfThoughts — non-linear reasoning graph with operations |
| `knowledge_distill.py` | ~250 | KnowledgeDistiller — episodic→semantic memory consolidation |
| `mcts.py` | ~350 | MCTSNavigator — reflective MCTS for web navigation |
| `memory.py` | ~300 | EpisodicMemory/SemanticMemory/WorkingMemory stores |
| `planner.py` | ~250 | HierarchicalPlanner — multi-level task decomposition |
| `code_gen.py` | ~200 | Code generation utilities |
| `__init__.py` | ~10 | Package init |

### Integration Hooks (`tools/`)

| File | Purpose |
|------|---------|
| `skynet_distill_hook.py` | Post-task distillation hook + bus scanner + CLI |
| `skynet_brain_dispatch.py` | Brain dispatch pipeline (Steps 1-7 including cognitive hooks) |
| `skynet_brain.py` | Brain task intelligence (GoT integration for COMPLEX+ tasks) |
| `skynet_knowledge.py` | Knowledge broadcast/absorb protocol |
| `skynet_learner.py` | Learner daemon (calls distill_result) |
| `skynet_version.py` | Version tracking and upgrade history |
| `skynet_self.py` | Self-awareness kernel (version constants) |

### Data Files (`data/`)

| File | Purpose |
|------|---------|
| `version_history.json` | Full version progression from Level 1 to Level 4 |
| `distill_state.json` | Distillation dedup state (seen_ids, total_distilled) |
| `learning.db` | SQLite persistent learning store (cross-session) |
| `brain_config.json` | Brain dispatch configuration + scoring protocol |
| `agent_profiles.json` | Agent identity + capabilities + self-assessment |
| `incidents.json` | Institutional incident memory |
| `level4_architecture.md` | This document |
