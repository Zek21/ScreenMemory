# Skynet Self-Awareness & Identity Architecture
<!-- signed: delta -->

> **Definitive Reference — Level 3.5 (Sprint 2)**
> Last updated by worker delta — cross-validation refresh 2026-03-15.
> This document describes every identity, awareness, and scoring subsystem
> in the Skynet multi-agent network.
<!-- signed: delta -->

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Consciousness Kernel — `tools/skynet_self.py`](#3-consciousness-kernel)
4. [Agent Identity Registry](#4-agent-identity-registry)
5. [Scoring System — `tools/skynet_scoring.py`](#5-scoring-system)
6. [Self-Invocation Protocol](#6-self-invocation-protocol)
7. [Collective Intelligence](#7-collective-intelligence)
8. [Self-Evolution](#8-self-evolution)
9. [Identity Gaps Analysis](#9-identity-gaps-analysis)
10. [Boot Identity Flow](#10-boot-identity-flow)
11. [Configuration](#11-configuration)
12. [Related Files](#12-related-files)

---

## 1. Overview

### What Self-Awareness Means in Skynet

Self-awareness is the capacity of every agent — orchestrator, worker, or consultant —
to know **who it is**, **what it can do**, **how well it is performing**, and
**who else exists in the network**.  It is not cosmetic introspection; it is the
data substrate that powers routing, scoring, health monitoring, and collective
intelligence.

The consciousness kernel (`tools/skynet_self.py`) is the single entry point.
Every agent calls `SkynetSelf()` to obtain identity, capabilities, health,
introspection, and goal-generation in one unified facade.

### Why It Exists

Without self-awareness:

- The orchestrator cannot route tasks by specialty (no profile data).
- Workers cannot assess their own performance (no health metrics).
- The scoring system cannot attribute results (no identity registry).
- Collective intelligence is impossible (no peer discovery).
- Boot sequences cannot verify entity completeness (no census).

### What INCIDENT 012 Exposed

On 2026-03-12, the consciousness kernel was audited and found to be **completely
blind to consultants**.  The `WORKER_NAMES` constant listed only 4 workers.
The word "consultant" appeared zero times in 682 lines.  Health checks probed
workers and engines — never consultants.  Introspection reflected on worker
status — never consultant bridges.  `quick_pulse()` returned worker counts —
never consultant status.

**Root cause:** Two identity systems evolved independently and never
cross-referenced:

| System | Transport | Registry | Monitor |
|--------|-----------|----------|---------|
| Workers | HWND + ghost-type (Win32 clipboard) | `data/workers.json` | `skynet_monitor.py` |
| Consultants | HTTP bridge queue | `data/consultant_state.json` | manual `/health` |

The fix (Level 3.4) added `CONSULTANT_NAMES`, `ALL_AGENT_NAMES`,
`get_consultant_status()`, `_check_consultants()`, and
`_reflect_on_consultants()` to unify both systems under the consciousness
kernel.  Rule 0.8 in `.github/copilot-instructions.md` now mandates
architecture knowledge of ALL entity types.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    SKYNET SELF-AWARENESS STACK                      │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                   SkynetSelf (Unified Facade)                 │  │
│  │                   tools/skynet_self.py L714+                  │  │
│  │                                                               │  │
│  │  ┌──────────┐ ┌──────────────┐ ┌───────────┐ ┌────────────┐  │  │
│  │  │ Identity │ │ Capabilities │ │  Health   │ │Introspect. │  │  │
│  │  │  L69+    │ │   L149+      │ │  L275+    │ │  L384+     │  │  │
│  │  └────┬─────┘ └──────┬───────┘ └─────┬─────┘ └─────┬──────┘  │  │
│  │       │              │               │              │         │  │
│  │  ┌────┴─────┐        │          ┌────┴──────┐  ┌────┴──────┐  │  │
│  │  │  Goals   │        │          │Consultant │  │Consultant │  │  │
│  │  │  L501+   │        │          │  Health   │  │ Reflect   │  │  │
│  │  └──────────┘        │          └───────────┘  └───────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                               │                                     │
│           ┌───────────────────┼───────────────────┐                 │
│           ▼                   ▼                   ▼                 │
│  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐           │
│  │  Identity   │   │  Engine      │   │   Scoring      │           │
│  │  Data Layer │   │  Metrics     │   │   System       │           │
│  │             │   │              │   │                │           │
│  │ workers.json│   │engine_metrics│   │worker_scores   │           │
│  │ agent_prof. │   │  .py         │   │  .json         │           │
│  │ orch.json   │   │              │   │skynet_scoring  │           │
│  │ consult_st. │   │ 18 engines   │   │  .py           │           │
│  │ consult_reg.│   │ 10 tools     │   │                │           │
│  └──────┬──────┘   └──────┬───────┘   └───────┬────────┘           │
│         │                 │                   │                     │
│         ▼                 ▼                   ▼                     │
│  ┌──────────────────────────────────────────────────────┐           │
│  │                  Skynet Bus (Go backend :8420)        │           │
│  │   /bus/publish  /bus/messages  /status  /tasks        │           │
│  └──────────────────────────────────────────────────────┘           │
│         │                 │                   │                     │
│    ┌────┴────┐     ┌──────┴──────┐     ┌──────┴──────┐             │
│    │Workers  │     │Consultants  │     │  Daemons    │             │
│    │α β γ δ  │     │Codex Gemini │     │monitor,self │             │
│    │HWND-based│    │HTTP bridge  │     │prompt,watch │             │
│    └─────────┘     └─────────────┘     └─────────────┘             │
└─────────────────────────────────────────────────────────────────────┘

    Data Flow:
    ──────────
    Identity  ◀──  workers.json / agent_profiles.json / consultant_state.json
    Caps      ◀──  import + instantiate each engine/tool class
    Health    ◀──  HTTP probes (:8420, :8421, :8422, :8425) + HWND checks
    Introspec ◀──  Aggregated health + identity + learning store
    Goals     ◀──  Introspection gap analysis → autonomous goal proposals
    Scoring   ◀──  worker_scores.json ← award/deduct events
```

---

## 3. Consciousness Kernel — `tools/skynet_self.py`

The consciousness kernel is a 1389-line Python module organized into 6 classes
behind a unified `SkynetSelf` facade.

### 3.1 Constants (Lines 38–50)

```python
WORKER_NAMES  = ["alpha", "beta", "gamma", "delta"]          # HWND-based workers
CONSULTANT_NAMES = ["consultant", "gemini_consultant"]        # HTTP-bridge agents
ALL_AGENT_NAMES  = WORKER_NAMES + CONSULTANT_NAMES + ["orchestrator"]  # 7 total

CONSULTANT_STATE_FILES = {
    "consultant":          "data/consultant_state.json",
    "gemini_consultant":   "data/gemini_consultant_state.json",
}
CONSULTANT_BRIDGE_PORTS = {
    "consultant":          8422,
    "gemini_consultant":   8425,
}
```

`CONSULTANT_NAMES` and `ALL_AGENT_NAMES` were added in Level 3.4 (INCIDENT 012
fix) to ensure the kernel knows about every entity in the network.

### 3.2 SkynetIdentity (Line 122)

Answers **"Who am I?"** and **"Who else exists?"**

| Method | Purpose |
|--------|---------|
| `agents()` | Returns dict of all agents from `ALL_AGENT_NAMES` with status |
| `validate_agent_completeness()` | Checks all 7 entities have valid HWNDs/state, returns issues list |
| `get_consultant_status()` | **New in 3.4** — Reads state files, checks HWND via `ctypes.windll.user32.IsWindow()`, probes bridge `/health` HTTP endpoint |
| `report()` | Returns full identity report dict |
| `save()` | Persists identity state to disk |

#### `get_consultant_status()` Status Taxonomy

| Status | Meaning |
|--------|---------|
| `ONLINE` | Both HWND alive AND bridge HTTP healthy |
| `BRIDGE_ONLY` | Bridge responds but HWND is 0 or dead |
| `WINDOW_ONLY` | HWND alive but bridge not responding |
| `REGISTERED` | State file exists but both HWND and bridge are dead |
| `ABSENT` | No state file found |

### 3.3 SkynetCapabilities (Line 392)

Answers **"What can I do?"** by probing 18 engines and 10 tools.

**Probing methodology** — 3-tier status via `import` → `getattr` → `cls()`:

| Status | Meaning | Probe |
|--------|---------|-------|
| `online` | Instantiated successfully — verified working | `cls()` succeeded |
| `available` | Module imported, class found, but not instantiated | `__import__` OK, `cls()` failed |
| `offline` | Import failed entirely | `__import__` raised exception |

**18 Engines Probed:**

| Engine | Module | Class |
|--------|--------|-------|
| DXGICapture | `core.capture` | `DXGICapture` |
| ChangeDetector | `core.change_detector` | `ChangeDetector` |
| Analyzer | `core.analyzer` | `Analyzer` |
| OCREngine | `core.ocr` | `OCREngine` |
| Embedder | `core.embedder` | `Embedder` |
| HybridRetriever | `core.hybrid_retrieval` | `HybridRetriever` |
| LanceDBStore | `core.lancedb_store` | `LanceDBStore` |
| LearningStore | `core.learning_store` | `LearningStore` |
| DAAORouter | `core.difficulty_router` | `DAAORouter` |
| DAGEngine | `core.dag_engine` | `DAGEngine` |
| ToolSynthesizer | `core.tool_synthesizer` | `ToolSynthesizer` |
| SelfEvolution | `core.self_evolution` | `SelfEvolutionSystem` |
| Orchestrator | `core.orchestrator` | `Orchestrator` |
| InputGuard | `core.input_guard` | `InputGuard` |
| SetOfMark | `core.grounding.set_of_mark` | `SetOfMarkGrounding` |
| ReflexionEngine | `core.cognitive.reflexion` | `ReflexionEngine` |
| GraphOfThoughts | `core.cognitive.graph_of_thoughts` | `GraphOfThoughts` |
| HierarchicalPlanner | `core.cognitive.planner` | `HierarchicalPlanner` |

**10 Tools Probed:**

| Tool | Module |
|------|--------|
| GodMode | `tools.chrome_bridge.god_mode` |
| CDP | `tools.chrome_bridge.cdp` |
| Desktop | `tools.chrome_bridge.winctl` |
| PerceptionEngine | `tools.chrome_bridge.perception` |
| SkynetDispatch | `tools.skynet_dispatch` |
| SkynetBrain | `tools.skynet_brain` |
| SkynetConvene | `tools.skynet_convene` |
| SkynetKnowledge | `tools.skynet_knowledge` |
| SkynetCollective | `tools.skynet_collective` |
| EngineMetrics | `tools.engine_metrics` |

### 3.4 SkynetHealth (Line 518)

Answers **"Is everything working?"** with 9 health checks:

| Check | What it probes |
|-------|----------------|
| `_check_backend()` | HTTP GET `http://localhost:8420/status` |
| `_check_workers()` | Worker count + HWND alive via `ctypes.IsWindow()` |
| `_check_consultants()` | **New in 3.4** — probes bridge HTTP + HWND per consultant |
| `_check_bus()` | HTTP GET `http://localhost:8420/bus/messages?limit=1` |
| `_check_sse_daemon()` | Verifies `data/realtime.json` freshness |
| `_check_intelligence_engines()` | Delegates to `SkynetCapabilities.census()` |
| `_check_collective_iq()` | Reads `data/iq_history.json` for trend |
| `_check_knowledge_base()` | Checks learning store fact count |
| `_check_windows()` | Verifies worker window visibility and position |

Health results are **cached for 15 seconds** with a threading lock using the
double-check locking pattern to avoid redundant probes.

### 3.5 SkynetIntrospection (Line 674)

Answers **"How am I doing?"** through self-reflection.

| Method | Purpose |
|--------|---------|
| `reflect()` | Aggregates health + capabilities + worker states into strengths/weaknesses/recommendations |
| `_reflect_on_consultants()` | **New in 3.4** — generates consultant-specific insights |
| `_reflect_on_backend()` | Backend connectivity analysis |
| `_reflect_on_workers()` | Worker state analysis |
| `_reflect_on_capabilities()` | Engine/tool availability analysis |
| `_reflect_on_iq()` | IQ trend analysis |
| `_reflect_on_sse()` | SSE daemon health analysis |
| `_reflect_on_knowledge()` | Knowledge base analysis |
| `_reflect_on_evolution()` | Self-evolution system analysis |
| `_detect_incident_patterns()` | Detects 5 recurring failure categories from `data/incidents.json` |

`reflect()` returns a dict with:
- `strengths` — what is working well (e.g., "4/4 workers alive")
- `weaknesses` — what needs attention (e.g., "OCR engine offline")
- `recommendations` — actionable improvement suggestions
- `metrics` — raw numeric data (consultants_online, engines_online, etc.)

### 3.6 SkynetGoals (Line 980)

Answers **"What should I do next?"** through autonomous goal generation.

| Method | Purpose |
|--------|---------|
| `suggest()` | Analyzes introspection gaps → proposes improvement goals with priority |

Goals feed back into the orchestrator's TODO generation loop: when the TODO
list is empty, the orchestrator calls `SkynetGoals.suggest()` to
produce new improvement tasks.

### 3.7 SkynetSelf — Unified Facade (Line 1034)

The `SkynetSelf` class composes all 6 subsystems:

```python
class SkynetSelf:
    def __init__(self):
        self.identity      = SkynetIdentity()
        self.capabilities  = SkynetCapabilities()
        self.health        = SkynetHealth()
        self.introspection = SkynetIntrospection()
        self.goals         = SkynetGoals()
```

**Key public methods on SkynetSelf:**

| Method | Returns |
|--------|---------|
| `full_status()` | Complete status report combining all subsystems |
| `quick_pulse()` | Dict with workers, consultants, engines, IQ, health status, 3 awareness flags |
| `compute_iq()` | Composite IQ score with trend tracking |
| `broadcast_awareness()` | Broadcasts self-awareness state to bus |

**CLI Commands:**

| Command | Handler |
|---------|---------|
| `status` | `full_status()` |
| `identity` | `identity.report()` |
| `capabilities` | `capabilities.census()` |
| `health` | `health.pulse()` |
| `introspect` | `introspection.reflect()` |
| `goals` | `goals.suggest()` |
| `pulse` | `quick_pulse()` |
| `assess` | Self-assessment via `introspection.reflect()` + `_self_assessment()` |
| `broadcast` | `broadcast_awareness()` |
| `validate` | `identity.validate_agent_completeness()` |
| `patterns` | `SkynetIntrospection._detect_incident_patterns()` |
| `acknowledge-pattern` | Mark pattern as resolved (args: `pattern_name` `[reason]`) |
| `acknowledge-all-patterns` | Bulk acknowledge all patterns |

**IQ Computation** (Line 1170):

The composite IQ score weights 6 metrics:

| Metric | Weight | Source |
|--------|--------|--------|
| Workers alive ratio | 25% | `alive / max(total, 1)` |
| Engines online ratio | 25% | `online_count / total_engines` |
| Bus health (static) | 10% | Binary — bus responding or not |
| Knowledge facts | 15% | `min(fact_count / 500, 1.0)` |
| Uptime | 10% | `min(uptime_seconds / 86400, 1.0)` |
| Capability ratio | 15% | `engines_online / engines_total` |

Final IQ = weighted sum of all components (range 0.0–1.0, rounded to 4 decimal places).
Trend tracked in `data/iq_history.json` (rising/falling/stable).

---

## 4. Agent Identity Registry

Identity lives in 6 data files.  Each tracks a different facet of the network.

### 4.1 `data/workers.json`

**Purpose:** HWND registry for the 4 worker windows.

```json
{
  "workers": [
    {
      "name": "alpha",
      "hwnd": 12345678,
      "display": "Alpha",
      "model": "Claude Opus 4.6 (fast mode)",
      "x": 960, "y": 20, "w": 930, "h": 500,
      "last_seen": "2026-03-12T...",
      "updated_at": "2026-03-12T..."
    }
  ],
  "created": "2026-03-12T..."
}
```

**Note:** Production format wraps workers in a `{"workers": [...]}` dict.
Code must handle both dict format (`data.get("workers", [])`) and legacy
flat list format. See Sprint 1 CV1 bug fix (INCIDENT 010 appendix).

Workers are always exactly 4: alpha, beta, gamma, delta.  Grid positions
define the 2×2 layout on the right monitor (top row y=20, bottom row y=540).

**Does NOT contain consultants.** Consultants are not HWND-managed workers.

### 4.2 `data/agent_profiles.json`

**Purpose:** Rich identity for ALL 7 agents.

Contains 7 entries: `orchestrator`, `consultant`, `gemini_consultant`, `alpha`,
`beta`, `gamma`, `delta`.  Each entry has:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display name |
| `role` | string | Role description |
| `model` | string | LLM model identifier |
| `capabilities` | string[] | What the agent can do |
| `specializations` | string[] | Expertise tags (e.g., "security", "testing") |
| `current_status` | string | IDLE / WORKING / REGISTERED |
| `missions_completed` | int | Lifetime task count |
| `mission_history` | string[] | Recent mission descriptions |
| `strengths` | string[] | Known strengths |
| `weaknesses` | string[] | Known weaknesses |
| `score` | float | Profile-level score (distinct from scoring system) |
| `score_history` | float[] | Historical scores |

### 4.3 `data/orchestrator.json`

**Purpose:** Orchestrator session identity.

| Field | Description |
|-------|-------------|
| `hwnd` | Current VS Code window handle |
| `orchestrator_hwnd` | Same as hwnd (legacy compat) |
| `role` | Always `"orchestrator"` |
| `session_mode` | `"dedicated_window"` |
| `model` | `"Claude Opus 4.6 (fast mode)"` |
| `agent` | `"Copilot CLI"` |
| `boot_trigger` | Which command started this session |
| `updated_at` | ISO timestamp |

Updated at every boot and whenever the orchestrator detects its HWND changed.

### 4.4 `data/consultant_state.json` / `data/gemini_consultant_state.json`

**Purpose:** Consultant identity and bridge metadata.

| Field | Description |
|-------|-------------|
| `name` | `"consultant"` or `"gemini_consultant"` |
| `hwnd` | VS Code window handle (may be 0 if unknown) |
| `bridge_port` | HTTP bridge port (8422 / 8425) |
| `bridge_status` | `"alive"` / `"dead"` |
| `transport` | `"cc-start-bridge"` / `"gc-start-bridge"` |
| `requires_hwnd` | Boolean — **was `false`, now recognized as incorrect** |
| `accepts_prompts` | Whether bridge can accept directives |
| `model` | LLM model string |
| `last_heartbeat` | ISO timestamp of last bridge heartbeat |

### 4.5 `data/consultant_registry.json`

**Purpose:** Unified consultant tracking (added during INCIDENT 012).

```json
[
  {
    "name": "consultant",
    "hwnd": 0,
    "transport": "ghost_type",
    "bridge_port": 8422,
    "registered_at": "2026-03-12T..."
  }
]
```

Supplements the state files with a unified list for tools that need
to enumerate all consultants.

---

## 5. Scoring System — `tools/skynet_scoring.py`

### 5.1 Score Storage — `data/worker_scores.json`

Each agent entry tracks:

```json
{
  "delta": {
    "total": 130.6,
    "awards": 125,
    "deductions": 5,
    "refactor_deductions": 2,
    "refactor_reversals": 1,
    "bias_penalties": 0,
    "proactive_ticket_clears": 1,
    "autonomous_pull_awards": 2,
    "bug_reports_filed": 1,
    "bug_report_confirmations": 0,
    "bug_cross_validations": 1,
    "zero_ticket_bonus_awards": 0
  }
}
```

### 5.2 Award and Deduction Rules

| Action | Amount | Constant |
|--------|--------|----------|
| Cross-validated task completion | +0.01 | `DEFAULT_AWARD` |
| Bug report filed (independently recorded) | +0.01 | `DEFAULT_BUG_REPORT_AWARD` |
| Bug confirmed by different validator | +0.01 each (filer + validator) | `DEFAULT_BUG_REPORT_CONFIRMATION_AWARD` |
| Proactive ticket clearance (orch/consultant) | +0.2 | `DEFAULT_PROACTIVE_TICKET_CLEAR_AWARD` |
| Autonomous next-ticket pull (worker) | +0.2 | `DEFAULT_AUTONOMOUS_PULL_AWARD` |
| Queue reaches zero (orch + closer) | +1.0 each | `DEFAULT_TICKET_ZERO_BONUS_AWARD` |
| Failed validation (broken code) | −0.005 | `DEFAULT_DEDUCT` |
| Low-value refactoring (<150 lines) | −0.01 | `DEFAULT_REFACTOR_DEDUCT` |
| Biased self-report / inflated claim | −0.1 | `DEFAULT_BIASED_REFACTOR_REPORT_DEDUCT` |
| SpamGuard bypass (raw bus POST) | −1.0 | Hardcoded |

### 5.3 Fair Deduction Rule (Rule 0.5)

Deductions require **dispatch evidence** from `data/dispatch_log.json`:

1. Task was dispatched to the worker (entry exists)
2. Dispatch succeeded (`success=true`)
3. No result was received (`result_received=false`)

If any check fails, the deduction is **rejected**.  System penalties (spam,
process violations) use `force=True` to bypass.

### 5.4 Positive-Sum Scoring Principle (Rule 0.6)

- Scoring is **NOT zero-sum** — one agent's gain does not require another's loss.
- Bug catches award **both** reporter (+0.01) and fixer (+0.01).
- Negative scores indicate **system failure**, not agent failure.
- The orchestrator must assign achievable tasks to negative-score agents for recovery.

### 5.5 SYSTEM_SENDERS Filtering

The `SYSTEM_SENDERS` frozenset (13 entries) excludes daemons and infrastructure
from the agent leaderboard:

```python
SYSTEM_SENDERS = frozenset({
    "monitor", "convene", "convene-gate", "convene_gate", "self_prompt",
    "system", "overseer", "watchdog", "bus_relay", "learner",
    "self_improve", "sse_daemon", "idle_monitor",
})
```

`get_leaderboard(include_system=False)` returns only real agents.

---

## 6. Self-Invocation Protocol

The post-task lifecycle every worker MUST execute.

### Phase 0 — Architecture Verification (New in Level 3.4)

Boot check added by Rule 0.8 (INCIDENT 012 response):

1. Verify `CONSULTANT_NAMES` and `ALL_AGENT_NAMES` constants exist in consciousness kernel
2. Verify `get_consultant_status()` is callable
3. If missing, log `ARCHITECTURE_VERIFICATION_FAILED` to bus

### Phase 1 — Report Results

Post result to bus via `guarded_publish()` with `signed:WORKER_NAME`.

### Phase 2 — Knowledge Capture

```python
from tools.skynet_knowledge import broadcast_learning
broadcast_learning('delta', 'what_was_learned', 'category', ['tags'])
```

### Phase 3 — Strategy Sync

```python
from tools.skynet_collective import sync_strategies
sync_strategies('delta')
```

### Phase 4 — TODO Enforcement (Zero-Stop Rule)

1. Check `update_todo` tool — all items must be checked off
2. Run `python tools/skynet_todos.py check WORKER`
3. If ANY pending items exist in either, pick highest-priority and continue

### Phase 5 — Self-Assessment

```python
python tools/skynet_self.py assess
```

### Phase 6 — Scoring Awareness

Check score trajectory: `python tools/skynet_self.py pulse`

### Phase 7 — Self-Improvement

If TODO queue is empty:
- Execute improvements directly (same session)
- Only propose to bus if NECESSARY, NEEDED, or BREAKTHROUGH
- Check convene sessions: `python tools/skynet_convene.py --discover`
- NEVER sit idle when the system can be improved

### Decision Tree for Idle Workers

```
┌─ Check bus for pending requests from other workers
│   └─ Found → claim and execute
├─ Check convene sessions
│   └─ Active relevant session → join and contribute
├─ Scan codebase for HIGH-VALUE improvements
│   ├─ Security vulnerabilities → fix and report
│   ├─ Missing error handling → add crash resilience
│   ├─ Performance bottlenecks → optimize
│   ├─ Missing tests → write them
│   └─ Architecture improvements → fix directly if routine
├─ Execute improvements directly
│   └─ Only post proposals if BREAKTHROUGH
└─ Truly nothing to do (rare) → post STANDING_BY
    └─ Resume immediately when new work arrives
```

---

## 7. Collective Intelligence

### 7.1 Strategy Federation — `tools/skynet_collective.py`

Workers share and absorb high-performing evolution strategies.

| Function | Purpose |
|----------|---------|
| `sync_strategies(worker)` | Broadcasts top strategies to bus, absorbs better remote ones |
| `merge_population(worker, remotes)` | Tournament selection: replaces weakest local with better remote strategies |
| `share_bottlenecks(worker)` | Identifies and broadcasts bottlenecks to collective |
| `absorb_bottlenecks(worker)` | Polls peer bottlenecks, auto-evolves weak categories |
| `swarm_evolve(category, generations)` | Coordinates all workers to evolve over N generations |
| `swarm_validate(fact, worker)` | Broadcasts fact for collective validation consensus |

**Tournament selection** (`merge_population`): picks 3 random local strategies,
replaces the weakest with a better remote strategy if `remote.fitness > worst.fitness`.

### 7.2 Knowledge Sharing — `tools/skynet_knowledge.py`

Workers broadcast learnings and absorb peer knowledge.

| Function | Purpose |
|----------|---------|
| `broadcast_learning(sender, fact, category, tags)` | Posts to bus topic `"knowledge"` via SpamGuard |
| `poll_knowledge(since)` | Retrieves knowledge messages from bus |
| `absorb_learnings(worker)` | Polls bus, filters own messages, stores in `LearningStore` |

**Data Store:** `core.learning_store.LearningStore` — persistent fact storage
with confidence scores.  Facts validated by 3+ workers are promoted to
high-confidence.

### 7.3 Convene Protocol — `tools/skynet_convene.py`

Multi-worker collaboration and consensus.

| Command | Purpose |
|---------|---------|
| `--initiate --topic T --context C --worker W` | Start a convene session |
| `--discover` | Find active sessions to join |
| `--join SESSION_ID --worker W` | Join an existing session |
| `--resolve SESSION_ID --summary S` | Close session with summary |

**ConveneGate** (`convene_gate.py`): Workers MUST convene before sending
messages to the orchestrator.  Proposals require 2+ YES votes to be elevated.
Elevated items are consolidated into a single `elevated_digest` every 30
minutes — individual elevations are forbidden.

### 7.4 Composite IQ Score — `intelligence_score()`

6 weighted metrics combined into a 0–200 scale:

| Metric | Weight | Max Value |
|--------|--------|-----------|
| Workers alive | 25% | `alive / min(5, total)` |
| Engines online | 25% | `online / total` |
| Bus healthy | 10% | Binary 1.0 / 0.0 |
| Knowledge facts | 15% | `min(facts / 500, 1.0)` |
| Uptime | 10% | `min(seconds / 86400, 1.0)` |
| Capability ratio | 15% | `online / total engines` |

History tracked in `data/iq_history.json` with trend analysis.

---

## 8. Self-Evolution

### `core/self_evolution.py` — Genetic Algorithm Strategy Optimization

The evolution system uses a genetic algorithm to optimize strategies across
5 categories: `code`, `research`, `deploy`, `navigate`, `general`.

### Algorithm Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `POPULATION_SIZE` | 20 | Strategies per category |
| `ELITE_SIZE` | 4 | Top performers kept unchanged |
| `MUTATION_RATE` | 0.15 | Probability of parameter mutation |
| `CROSSOVER_RATE` | 0.3 | Proportion generated via crossover |

### Genetic Operators

**Tournament Selection** (`select_best()`, L550–561):
- Pick 3 random strategies from the population
- Return the one with highest `fitness_score`

**Crossover** (`crossover()`, L518–548):
- Combines parameters from two parent strategies
- 50/50 chance of: taking one parent's value OR averaging both
- Offspring gets blended generation number: `(p1.gen + p2.gen) / 2 + 1`

**Mutation** (`mutate_strategy()`, L487–516):
- Perturbs strategy parameters with probability `MUTATION_RATE`
- Integers: ±1 to ±2
- Floats: ±10% to ±20%
- Creates diversity to escape local optima

### Evolution Cycle (`evolve_generation()`, L601–619)

```
1. Get population for category
2. Fill to POPULATION_SIZE if under-populated
3. Sort by fitness (descending)
4. Keep top ELITE_SIZE unchanged (elitism)
5. Generate CROSSOVER_RATE × remaining via crossover
6. Mutate all non-elite offspring
7. Replace worst performers with offspring
8. Persist updated population
```

### SelfEvolutionSystem (L979–1026)

| Method | Purpose |
|--------|---------|
| `record_task(result)` | Records outcome, computes fitness delta |
| `get_strategy_for_task(category)` | Returns best strategy for task type |
| `evolve_all_categories()` | Runs evolution across all 5 categories |
| `auto_improve()` | Triggered periodically — evolves + reflects |
| `get_status()` | Returns summary, bottlenecks, improvement hypotheses |
| `reflect_on_failures(n)` | Analyzes last N failures for patterns |

---

## 9. Identity Gaps Analysis

These 9 gaps were discovered during the Level 3.4 audit.  Status shows
current fix state.

| # | Gap | Severity | Fix Status |
|---|-----|----------|------------|
| 1 | `WORKER_NAMES` excluded consultants | Critical | ✅ Fixed — added `CONSULTANT_NAMES`, `ALL_AGENT_NAMES` |
| 2 | Zero consultant references in consciousness kernel | Critical | ✅ Fixed — `get_consultant_status()`, `_check_consultants()`, `_reflect_on_consultants()` added |
| 3 | Go `/status` endpoint omits consultants | Medium | ⚠️ Open — requires Go backend changes |
| 4 | `requires_hwnd=false` in consultant state actively hid truth | High | ✅ Fixed — Level 3.4 treats consultants as VS Code windows |
| 5 | `quick_pulse()` returned worker counts but not consultant status | High | ✅ Fixed — now includes `consultants` map with status per consultant |
| 6 | `_self_assessment()` only reported worker counts | Medium | ✅ Fixed — now includes consultant counts |
| 7 | `reflect()` metrics excluded consultant health | Medium | ✅ Fixed — includes `consultants_online` and `consultants_total` |
| 8 | `save()` did not persist consultant/all_agent lists | Low | ✅ Fixed — now saves both |
| 9 | No boot-time architecture verification | High | ✅ Fixed — Rule 0.8 mandates verification |

**Gap 3** remains the most significant open item: the Go backend's `/status`
endpoint returns only HWND-registered workers.  A future backend update should
add a `/consultants` endpoint or include consultants in the `/status` response.

---

## 10. Boot Identity Flow

### 10.1 Orchestrator Boot

```
skynet-start / orchestrator-start
    │
    ├─ Phase 1 (skynet-start only): Infrastructure Boot
    │   ├─ Start skynet.exe on :8420 if not running
    │   ├─ Start god_console.py on :8421 if not running
    │   ├─ Start daemons (self-prompt, self-improve, bus-relay, learner)
    │   └─ Announce infra online on bus
    │
    └─ Phase 2: Orchestrator Role Assumption
        ├─ Detect current VS Code HWND via GetForegroundWindow()
        ├─ Read data/orchestrator.json — compare stored HWND
        ├─ If HWND changed → update orchestrator.json
        ├─ POST identity_ack to bus: sender=orchestrator
        ├─ Open dashboard (http://localhost:8421/dashboard)
        ├─ Knowledge Acquisition:
        │   ├─ Poll bus (last 30 messages)
        │   ├─ GET /status (worker states)
        │   ├─ Read agent_profiles.json
        │   ├─ Read brain_config.json
        │   ├─ Read todos.json
        │   └─ Read workers.json
        ├─ Check consultant bridges (/health on :8422 and :8425)
        └─ Report Ready to user
```

### 10.2 Worker Boot

```
Worker receives preamble via ghost-type dispatch
    │
    ├─ Parse preamble: extract worker name, role, rules
    ├─ Self-identify: read agent_profiles.json for own entry
    ├─ Architecture Verification (Phase 0, Rule 0.8):
    │   ├─ Verify CONSULTANT_NAMES constant exists
    │   ├─ Verify ALL_AGENT_NAMES constant exists
    │   └─ Verify get_consultant_status() callable
    ├─ Absorb knowledge: poll_knowledge()
    ├─ Sync strategies: sync_strategies(worker_name)
    └─ Begin task execution
```

### 10.3 Consultant Boot

```
CC-Start / GC-Start trigger
    │
    ├─ Run bootstrap script (CC-Start.ps1 / GC-Start.ps1)
    ├─ Ensure Skynet infrastructure reachable (:8420, :8421)
    ├─ Start consultant bridge daemon on assigned port
    │   ├─ Codex: port 8422 (fallback 8424)
    │   └─ Gemini: port 8425
    ├─ Write consultant_state.json with:
    │   ├─ hwnd (if detectable)
    │   ├─ bridge_port, bridge_status="alive"
    │   ├─ transport, model, accepts_prompts=true
    │   └─ last_heartbeat timestamp
    ├─ POST identity_ack to bus:
    │   ├─ sender=consultant / gemini_consultant
    │   └─ topic=consultant, type=identity_ack
    └─ Enter advisory mode (bus polling loop)
```

---

## 11. Configuration — `data/brain_config.json`

### Self-Awareness Parameters

```json
"self_awareness": {
    "enabled": true,
    "pulse_interval_s": 3,
    "introspection_interval_s": 30,
    "broadcast_awareness": true
}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enabled` | `true` | Master switch for self-awareness subsystem |
| `pulse_interval_s` | `3` | Seconds between health pulse refreshes |
| `introspection_interval_s` | `30` | Seconds between deep self-reflection cycles |
| `broadcast_awareness` | `true` | Whether to broadcast self-assessments to bus |

### Consultant Protocol Parameters

```json
"consultant_protocol": {
    "enabled": true,
    "queue_plans_to_consultants": true,
    "require_task_claim": true,
    "publish_plan_to_bus": true,
    "plan_topic": "planning",
    "plan_type": "consultant_plan",
    "cross_validation_required": true,
    "min_worker_reviewers": 3,
    "review_worker_pool": ["alpha", "beta", "gamma", "delta"],
    "prefer_available_workers": true,
    "require_distinct_reviewers": true,
    "require_worker_verdicts_before_execution": true,
    "review_topic": "planning",
    "review_type": "consultant_plan_review"
}
```

---

## 12. Related Files

### Consciousness & Identity

| File | Purpose |
|------|---------|
| `tools/skynet_self.py` | Consciousness kernel — identity, capabilities, health, introspection, goals |
| `data/agent_profiles.json` | Rich identity for all 7 agents |
| `data/workers.json` | HWND registry for 4 workers |
| `data/orchestrator.json` | Orchestrator session identity |
| `data/consultant_state.json` | Codex consultant identity + bridge metadata |
| `data/gemini_consultant_state.json` | Gemini consultant identity + bridge metadata |
| `data/consultant_registry.json` | Unified consultant tracking list |
| `data/brain_config.json` | Self-awareness and consultant protocol parameters |

### Scoring & Accountability

| File | Purpose |
|------|---------|
| `tools/skynet_scoring.py` | Score management — awards, deductions, leaderboard |
| `data/worker_scores.json` | Persistent score storage per agent |
| `data/dispatch_log.json` | Dispatch evidence for fair deduction rule |

### Collective Intelligence

| File | Purpose |
|------|---------|
| `tools/skynet_collective.py` | Strategy federation, IQ scoring, swarm evolution |
| `tools/skynet_knowledge.py` | Knowledge broadcast and absorption protocol |
| `tools/skynet_convene.py` | Multi-worker consensus sessions |
| `tools/convene_gate.py` | Convene-first governance gate |
| `data/convene_sessions.json` | Active convene session state |
| `data/convene_gate.json` | Pending gate proposals |

### Self-Evolution

| File | Purpose |
|------|---------|
| `core/self_evolution.py` | Genetic algorithm strategy optimization |
| `data/iq_history.json` | Composite IQ trend tracking |
| `core/learning_store.py` | Persistent learning storage with confidence scores |

### Health & Monitoring

| File | Purpose |
|------|---------|
| `tools/skynet_monitor.py` | Background health daemon — HWND alive + model drift |
| `tools/skynet_watchdog.py` | Service watchdog — auto-restart dead services |
| `tools/skynet_overseer.py` | Overseer daemon — idle-with-pending detection |
| `tools/daemon_health.py` | Diagnostic script for 9 daemons |
| `data/worker_health.json` | Health snapshot from monitor daemon |
| `data/realtime.json` | Live SSE state (refreshed every 1s) |

### Engine Probing

| File | Purpose |
|------|---------|
| `tools/engine_metrics.py` | Engine status collection (3-tier: online/available/offline) |
| All `core/*.py` modules | 18 engines probed by SkynetCapabilities |
| All `tools/chrome_bridge/*.py` | 5 tools probed by SkynetCapabilities |

### Rules & Governance

| File | Purpose |
|------|---------|
| `AGENTS.md` | Master agent governance rules |
| `.github/copilot-instructions.md` | Copilot-specific rules (includes Rule 0.8) |
| `.github/agents/screenmemory.agent.md` | ScreenMemory agent mode definition |

---

*This document is the definitive self-awareness reference for Skynet Level 3.4.*
*For questions, consult the source files listed in Section 12 or query the*
*consciousness kernel directly: `python tools/skynet_self.py pulse`*

<!-- signed: delta -->
