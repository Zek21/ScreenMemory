# SKYNET Intelligence Manual
## Version 3.0 — Level 3 Production

> *"A thinking system is not its code. It is the pattern of decisions that emerges when code, context, and collaboration converge."*

This is the definitive manual for SKYNET — a distributed AI intelligence network that decomposes goals into parallel worker tasks, monitors its own health, learns from outcomes, and self-corrects when things break.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Intelligence Flow](#2-intelligence-flow)
3. [Self-Awareness](#3-self-awareness)
4. [Worker Protocol](#4-worker-protocol)
5. [IQ System](#5-iq-system)
6. [Dispatch Intelligence](#6-dispatch-intelligence)
7. [Failure Recovery](#7-failure-recovery)
8. [Task Queue System](#8-task-queue-system)
9. [Convene Protocol](#9-convene-protocol)
10. [Health & Diagnostics](#10-health--diagnostics)
11. [API Endpoint Reference](#11-api-endpoint-reference)
12. [Tool Reference](#12-tool-reference)

---

## 1. Architecture Overview

SKYNET is a **5-layer distributed intelligence system** built on a Go backend with Python tooling and VS Code Copilot CLI workers.

```
┌─────────────────────────────────────────────────────────┐
│                    LAYER 5: CONSCIOUSNESS               │
│  skynet_self.py — Identity, IQ, Introspection, Goals    │
├─────────────────────────────────────────────────────────┤
│                    LAYER 4: COGNITION                   │
│  skynet_brain.py — Task Decomposition & Context Inject  │
│  skynet_orchestrate.py — Pipeline Coordination          │
│  skynet_collective.py — Cross-Worker Evolution          │
├─────────────────────────────────────────────────────────┤
│                    LAYER 3: COMMUNICATION               │
│  Message Bus (Go ring buffer, 100 msgs)                 │
│  SSE Stream (1 Hz real-time state)                      │
│  skynet_convene.py — Multi-Worker Sessions              │
│  skynet_knowledge.py — Learning Federation              │
├─────────────────────────────────────────────────────────┤
│                    LAYER 2: EXECUTION                   │
│  skynet_dispatch.py — Ghost Automation (Win32 API)      │
│  skynet_pipeline.py — Chain/Parallel/Retry Composition  │
│  Workers: Alpha, Beta, Gamma, Delta (VS Code CLI)       │
├─────────────────────────────────────────────────────────┤
│                    LAYER 1: INFRASTRUCTURE              │
│  Skynet Go Backend (port 8420) — 22 HTTP endpoints      │
│  GOD Console (port 8421) — Dashboard + Engine Metrics   │
│  skynet_watchdog.py — Service Resurrection              │
│  skynet_monitor.py — Model Drift Detection              │
└─────────────────────────────────────────────────────────┘
```

### Why This Architecture?

**Problem**: A single AI agent hits context limits, can't parallelize, and has no memory across sessions.

**Solution**: One orchestrator decomposes goals into subtasks, dispatches them to 4 parallel workers via Win32 ghost automation, collects results via message bus, and synthesizes a coherent response. Workers share knowledge via a federated learning protocol and evolve strategies using genetic algorithms.

### Component Map

| Component | Location | Role |
|-----------|----------|------|
| **Go Backend** | `Skynet/` (port 8420) | Message bus, worker registry, task queue, SSE stream |
| **GOD Console** | `god_console.py` (port 8421) | Web dashboard, engine metrics, self-awareness endpoints |
| **Orchestrator** | VS Code window (left monitor) | Decomposes tasks, dispatches to workers, synthesizes results |
| **Workers** | 4 VS Code windows (right monitor, 2×2 grid) | Execute tasks autonomously, post results to bus |
| **Core Engines** | `core/` (18 modules) | Vision, OCR, embeddings, retrieval, security, evolution |
| **Chrome Bridge** | `tools/chrome_bridge/` | Browser automation via GodMode/CDP/Win32 |

### The Go Backend

The Go server at port 8420 is the nervous system. It provides:

- **In-memory ring buffer bus** (100 messages, topic-based pub/sub, non-blocking fan-out)
- **Worker pool** with priority min-heap task queues and circuit breakers (3 failures → 30s backoff)
- **SSE stream** at `/stream` pushing full state at 1 Hz
- **26+ HTTP endpoints** for dispatch, status, orchestration, security, task queue, and brain integration

Workers are statically registered at startup (`alpha`, `beta`, `gamma`, `delta`) and tracked via heartbeats (5s internal tick + external monitor heartbeat).

---

## 2. Intelligence Flow

How a task travels from user request to completed result:

```
User types command in Orchestrator
            │
            ▼
┌──────────────────────┐
│   Identity Guard     │──── Block? ──→ REJECT (worker preamble injection)
│ (skynet_identity_    │
│  guard.py)           │
└──────────┬───────────┘
           │ PASS
           ▼
┌──────────────────────┐
│   Brain Decompose    │    Assess difficulty (DAAORouter)
│ (skynet_brain.py)    │──→ Recall learnings (LearningStore)
│                      │──→ Search context (HybridRetriever)
│                      │──→ Split into subtasks by difficulty:
│                      │      TRIVIAL → 1 worker, direct
│                      │      MODERATE → 2 workers, plan+implement
│                      │      COMPLEX → 3-4 workers, DAG chain
│                      │      ADVERSARIAL → debate+synthesize
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Smart Routing      │    Parallel UIA state scan (all workers)
│ (skynet_dispatch.py) │──→ Score: IDLE(0) < TYPING(1) < PROCESSING(2)
│                      │──→ Tiebreak by pending_tasks count
│                      │──→ Context injection (learnings + past solutions)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Ghost Dispatch     │    Per worker (parallel via ThreadPoolExecutor):
│ (ghost_type_to_      │    1. UIA locate bottommost Edit control
│  worker)             │    2. PostMessage click (no cursor movement)
│                      │    3. Clipboard paste (thread-locked)
│                      │    4. PostMessage Enter to submit
└──────────┬───────────┘
           │
     ┌─────┼─────┬─────┐
     ▼     ▼     ▼     ▼
   ALPHA  BETA  GAMMA  DELTA   ← Workers execute independently
     │     │     │     │
     └─────┼─────┴─────┘
           │
           ▼
┌──────────────────────┐
│   Bus Collection     │    Poll /bus/messages for type="result"
│ (skynet_realtime.py) │──→ Deduplicate by message ID
│                      │──→ Match by key substring in content/sender
│                      │──→ Timeout: 90-120s default
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Synthesis          │    Merge results into coherent report
│ (skynet_orchestrate) │──→ Learn from outcomes (knowledge bus)
│                      │──→ Calibrate router via feedback
└──────────────────────┘
```

### Dispatch Modes (Fastest to Most Controlled)

| Mode | Preamble | Target | Speed | Use Case |
|------|----------|--------|-------|----------|
| `--blast` | None | IDLE only | ~100ms/worker | Quick commands, broadcasts |
| `--parallel` | Full | All workers | ~600ms/worker | Wave dispatches |
| `--smart` | Full | Best N idle | ~600ms/worker | Auto-routed tasks |
| `--worker NAME` | Full | Specific | ~600ms | Specialized work |
| `--idle` | Full | First idle | ~600ms | Sub-delegation |
| `--all` | Full | Sequential | ~2.5s/worker | Ordered broadcasts |

### Ghost Type Mechanism

The dispatch system types into worker windows without moving the mouse or stealing focus:

1. **UIA COM Engine** scans the worker window for Edit controls
2. Selects the **bottommost Edit** (the chat input box, not steering cards)
3. **PostMessage** sends `WM_LBUTTONDOWN`/`WM_LBUTTONUP` at computed client coordinates
4. Text is placed on **clipboard** (thread-locked to prevent races)
5. **PostMessage** sends `Ctrl+V` to paste, then `Enter` to submit
6. Orchestrator focus is immediately restored via `SetForegroundWindow`

This is a zero-cursor, zero-focus-steal mechanism — the user never sees anything move.

---

## 3. Self-Awareness

SKYNET knows itself through `skynet_self.py` — a consciousness kernel with 5 subsystems:

```
┌─────────────────────────────────────────────┐
│              SkynetSelf                     │
│  ┌───────────┐  ┌──────────────┐            │
│  │ Identity   │  │ Capabilities │            │
│  │ name, ver  │  │ engine census│            │
│  │ level, mdl │  │ tool probing │            │
│  └───────────┘  └──────────────┘            │
│  ┌───────────┐  ┌──────────────┐            │
│  │ Health     │  │ Introspection│            │
│  │ pulse chks │  │ reflect()    │            │
│  │ bus/worker │  │ strengths    │            │
│  │ engines    │  │ weaknesses   │            │
│  └───────────┘  └──────────────┘            │
│  ┌───────────┐  ┌──────────────┐            │
│  │ Goals      │  │ IQ System    │            │
│  │ suggest()  │  │ compute_iq() │            │
│  │ priorities │  │ trend track  │            │
│  └───────────┘  └──────────────┘            │
└─────────────────────────────────────────────┘
```

### Identity (`SkynetIdentity`)

Persistent in `data/skynet_identity.json`:
- **Name**: SKYNET
- **Version**: 3.0
- **Level**: 3
- **Model**: Claude Opus 4.6 (fast mode)
- **Born**: ISO timestamp of first creation
- **Workers**: [alpha, beta, gamma, delta]

### Health Pulse (`SkynetHealth.pulse()`)

Checks 6 subsystems in sequence:

| Check | Method | Healthy When |
|-------|--------|-------------|
| Backend | HTTP GET `/status` | 200 response |
| Workers | Agent status from backend | All 4 alive |
| Bus | HTTP GET `/bus/messages?limit=1` | Non-null response |
| SSE Daemon | Read `data/realtime.json` age | < 5 seconds old |
| Intelligence | `engine_metrics.collect_engine_metrics()` | Engines online > 0 |
| Knowledge | `LearningStore.stats()` | Facts > 0 |

Overall: **HEALTHY** if backend + workers both up, **DEGRADED** otherwise.

### Introspection (`SkynetIntrospection.reflect()`)

Generates a self-reflection by aggregating:
- Worker alive count and total
- Engine online ratio (from `engine_metrics`)
- Collective IQ score
- Capability ratio (online/total)
- Identifies **strengths** (metrics above 0.7) and **weaknesses** (below 0.3)

### Self-Assessment (Natural Language)

```
"I am SKYNET Level 3 -- Orchestrator of the distributed intelligence network.
 Status: HEALTHY. I command 4/4 workers. Intelligence engines: 12/18 online.
 Collective intelligence: 0.754. Capability coverage: 67%.
 Strengths: worker availability; bus uptime. Weaknesses: engine coverage."
```

---

## 4. Worker Protocol

### Communication Channels

Workers communicate through the Go backend message bus:

```
┌──────────┐    POST /bus/publish     ┌──────────┐
│  Worker   │ ──────────────────────→ │   Bus    │
│  (sender) │                         │ (ring    │
│           │ ← GET /bus/messages ──  │  buffer) │
│           │                         │  100 max │
└──────────┘                          └──────────┘
                                           │
                    ┌──────────────────────┤
                    ▼                      ▼
              ┌──────────┐          ┌──────────┐
              │  Worker   │          │  Orch    │
              │  (reader) │          │ (reader) │
              └──────────┘          └──────────┘
```

### Message Types

| Topic | Type | Purpose |
|-------|------|---------|
| `orchestrator` | `result` | Worker reports task completion |
| `orchestrator` | `alert` | Worker reports problem (model drift, etc.) |
| `workers` | `request` | Worker asks for help from peers |
| `workers` | `sub-task` | Worker delegates to another worker |
| `convene` | `invite` | Initiate multi-worker collaboration |
| `convene` | `join` | Accept collaboration invitation |
| `convene` | `update` | Contribute to active session |
| `convene` | `resolve` | Close session with synthesis |
| `knowledge` | `learning` | Share discovered fact |
| `knowledge` | `strategy` | Share high-fitness strategy |
| `knowledge` | `validation` | Vote on fact accuracy |
| `collective` | `strategy` | Cross-worker strategy federation (fitness > 0.7) |
| `collective` | `bottleneck` | Share performance bottlenecks |
| `awareness` | `pulse` | Self-awareness broadcast |

### Convene Protocol

Workers can form ad-hoc collaboration sessions:

```
Alpha initiates: POST /bus/convene
  {initiator: "alpha", topic: "code review", need_workers: 2}
                    │
  ┌─────────────────┼─────────────────┐
  ▼                 ▼                 ▼
Beta discovers   Gamma discovers   Delta discovers
via poll_and_join()
  │                 │
  ▼                 ▼
Beta joins       Gamma joins
  │                 │
  ▼                 ▼
Beta posts       Gamma posts
update           update
  │                 │
  └────────┬────────┘
           ▼
Alpha collects updates
           │
           ▼
Alpha resolves session
(synthesis posted to bus)
```

Sessions persist in `data/convene_sessions.json` and remain active until explicitly resolved.

### Knowledge Federation

Workers share learnings via the bus:

1. **Learn**: Worker discovers insight → calls `LearningStore.learn()` locally
2. **Share**: Posts to bus with `topic=knowledge, type=learning`
3. **Absorb**: Other workers poll bus, filter by sender ≠ self, store locally
4. **Validate**: Workers can vote on facts (3+ agreeing votes → reinforce)
5. **Evolve**: High-fitness strategies (> 0.7) broadcast via `topic=collective`

### Worker Identity Preamble

Every dispatched task is prefixed with a **preamble** that tells the worker:
- Its name and role in the system
- How to post results to the bus
- How to request help from peers
- How to coordinate via convene sessions
- **NO STEERING** — execute directly, no drafts, no questions

The preamble includes an anti-injection fingerprint: if this text appears in the orchestrator window, the identity guard blocks it.

---

## 5. IQ System

### Composite IQ Score

`SkynetSelf.compute_iq()` calculates a real-time intelligence score from 6 weighted components:

```
IQ = Σ (component × weight)

┌─────────────────────┬────────┬────────────────────────────┐
│ Component           │ Weight │ Normalization               │
├─────────────────────┼────────┼────────────────────────────┤
│ workers_alive       │  25%   │ alive / total_expected      │
│ engines_online      │  25%   │ online / total_engines      │
│ bus_healthy         │  10%   │ UP = 1.0, DOWN = 0.0        │
│ knowledge_facts     │  15%   │ min(facts / 500, 1.0)       │
│ uptime_hours        │  10%   │ min(uptime_s / 86400, 1.0)  │
│ capability_ratio    │  15%   │ importable / total engines   │
└─────────────────────┴────────┴────────────────────────────┘

Score range: 0.0 (brain-dead) to 1.0 (peak intelligence)
```

### Trend Tracking

IQ readings are stored in `data/iq_history.json` (last 100 readings). The trend compares the current reading against the average of the previous 5:

| Delta | Trend |
|-------|-------|
| > +0.02 | `rising` — system is getting smarter |
| < -0.02 | `falling` — system is degrading |
| ±0.02 | `stable` — steady state |

### Collective Intelligence Score

`skynet_collective.py` provides a separate `intelligence_score()` with a different formula:

```
Score = fitness(40%) + knowledge(20%) + diversity(20%) + collaboration(20%)
```

- **fitness**: Average strategy fitness across evolution populations
- **knowledge**: Normalized fact count from LearningStore
- **diversity**: Unique strategies across all workers
- **collaboration**: Convene sessions resolved / initiated

### Improving the IQ

| Action | Impact | Component |
|--------|--------|-----------|
| Bring workers online | +6.25% per worker | workers_alive |
| Fix offline engines | +1.4% per engine | engines_online |
| Keep bus running | +10% | bus_healthy |
| Learn more facts | up to +15% | knowledge_facts |
| Keep system running | up to +10% | uptime_hours |
| Fix broken imports | up to +15% | capability_ratio |

---

## 6. Dispatch Intelligence

### Brain Decomposition Pipeline

`skynet_brain.py` uses a 4-stage pipeline:

```
ASSESS ──→ RECALL ──→ SEARCH ──→ DECOMPOSE
  │           │          │           │
  │           │          │           ▼
  │           │          │     Subtask DAG
  │           │          │     (depends on difficulty)
  │           │          │
  │           │          └─ HybridRetriever.search(goal)
  │           │             → Past solutions (top 5)
  │           │
  │           └─ LearningStore.recall(goal)
  │              → Past learnings (top 5)
  │
  └─ DAAORouter.classify(goal)
     → {difficulty, confidence, operator, domain_tags}
     → Override: text signals can bump difficulty up
```

### Difficulty-Based Decomposition

| Difficulty | Workers | Pattern | Example |
|-----------|---------|---------|---------|
| TRIVIAL | 1 | Direct execution | "List files in core/" |
| SIMPLE | 1 | Direct with context | "Fix the typo in config.json" |
| MODERATE | 2 | Plan → Implement | "Add a new API endpoint" |
| COMPLEX | 3-4 | Research → Design → Implement → Validate | "Refactor the auth system" |
| ADVERSARIAL | 4 | Propose A → Propose B → Critique → Synthesize | "Design the best caching strategy" |

### Text Signal Override

The brain overrides router heuristics when text signals indicate higher complexity:

```python
signals = 0
if action_verb_count >= 3:  signals += 2   # Many actions = complex
if enumeration_found:       signals += 1   # "1) X 2) Y" = structured
if and_count >= 2:          signals += 2   # "X and Y and Z" = multi-part
if scope_word_found:        signals += 1   # "all", "every", "entire"
if len(goal) > 200:         signals += 1   # Long = complex

if signals >= 4: bump difficulty up 2 levels
```

### Context Injection

Each subtask is enriched with context before dispatch:

```
RELEVANT PAST LEARNINGS (use these to avoid past mistakes):
  1. UIA InvokePattern is the only reliable STEERING fix
  2. PostMessage works without focus, SendKeys does not
  3. Clipboard is process-wide — needs thread lock

RELEVANT PAST SOLUTIONS:
  1. Previous auth refactor used token rotation pattern
  2. Cache invalidation solved with TTL + version hash

TASK COMPLEXITY: COMPLEX
ROUTING REASON: Alpha has lowest pending tasks (0)

TASK: Refactor the authentication module to use JWT tokens
```

### Smart Decomposition (Keyword-Based)

`skynet_smart_decompose.py` provides fast, non-LLM decomposition:

1. **Explicit routing**: `"alpha: task1, beta: task2"` → direct assignment
2. **Numbered lists**: `"1) scan 2) fix 3) test"` → 3 subtasks
3. **Conjunction splitting**: `"audit code; deploy; run tests"` → 3 subtasks
4. **Verb-preceding "and"**: `"build the API and deploy it"` → 2 subtasks

Each subtask is classified by type (code/audit/test/research/infra) using keyword scoring, assigned a complexity (1-10), and load-balanced across idle workers.

---

## 7. Failure Recovery

SKYNET has 4 layers of failure recovery:

```
┌─────────────────────────────────────────────┐
│  LAYER 4: WATCHDOG (skynet_watchdog.py)      │
│  Checks every 30-60s, auto-restarts services│
├─────────────────────────────────────────────┤
│  LAYER 3: MONITOR (skynet_monitor.py)        │
│  Checks every 10-60s, fixes model drift      │
├─────────────────────────────────────────────┤
│  LAYER 2: CIRCUIT BREAKER (Go backend)       │
│  3 failures → 30s backoff → half-open test   │
├─────────────────────────────────────────────┤
│  LAYER 1: DISPATCH RETRY (skynet_dispatch)   │
│  STEERING cancel → steer-bypass → respawn    │
└─────────────────────────────────────────────┘
```

### Layer 1: Dispatch Retry

When dispatch to a worker fails:

```
1. Pre-check: Is worker STEERING?
   YES → Cancel via UIA InvokePattern ("Cancel (Alt+Backspace)" button)
       → Wait 1s settle
       → Re-dispatch

2. ghost_type_to_worker() fails?
   → Try clear_steering_and_send() (cancel + retry)
   → Still fails? → Log failure, record metric

3. Worker PROCESSING for > 30s?
   → wait_for_idle_uia(timeout=30)
   → Still busy? → Dispatch anyway (queue)
```

### Layer 2: Circuit Breaker (Go Backend)

Each worker has a circuit breaker in the Go backend:

```
CLOSED (normal) ──[3 consecutive failures]──→ OPEN (reject all)
                                                │
                                          [30s backoff]
                                                │
                                                ▼
                                          HALF_OPEN (test 1 task)
                                                │
                                     ┌──────────┼──────────┐
                                     │ success  │          │ failure
                                     ▼          │          ▼
                                   CLOSED       │        OPEN
                                                │     (reset timer)
```

### Layer 3: Monitor Daemon

`skynet_monitor.py` runs as a background daemon:

| Check | Interval | Action on Failure |
|-------|----------|-------------------|
| Window alive | 10s | Post alert to bus, mark heartbeat dead |
| Worker model | 60s | Auto-correct via UIA (type "fast" → Down+Enter) |
| Orchestrator model | 30s | Auto-correct + CRITICAL alert |

**Model drift correction**:
1. Find "Pick Model" button via UIA
2. PostMessage click to open picker
3. Type "fast" to filter
4. Down+Enter to select "Claude Opus 4.6 (fast mode)"
5. Re-verify model string via UIA

### Layer 4: Watchdog

`skynet_watchdog.py` monitors services:

| Service | Interval | Recovery |
|---------|----------|----------|
| GOD Console (8421) | 30s | Auto-restart as detached subprocess |
| Skynet Backend (8420) | 60s | Alert only (manual restart required) |

Status written to `data/watchdog_status.json` with timestamps.

### Worker Recovery (skynet_realtime.py)

When a worker window dies:

```
1. Detect: IsWindow() + IsWindowVisible() return False
2. Snapshot: Record existing VS Code HWNDs
3. Spawn: Execute new_chat.ps1 (opens new VS Code chat window)
4. Detect new HWND: Diff window list (after - before)
5. Update: Write new HWND to workers.json
6. Re-inject: Dispatch worker identity preamble
7. Re-dispatch: Send original task
```

### Identity Guard (Security)

`skynet_identity_guard.py` prevents prompt injection:

- **Worker preamble detection**: Regex matches for worker-addressed commands appearing in orchestrator
- **Command injection patterns**: Blocks `rm`, `del`, `shutdown`, `eval()`, external `curl`/`wget`
- **HMAC-SHA256 dispatch signing**: Hourly key rotation, 1-minute clock skew tolerance
- **Blocked events**: Logged to `identity_guard.log` and POST to `/security/blocked`

---

## 8. Task Queue System

The **pull-based task queue** allows any worker to grab unclaimed tasks without orchestrator intervention. This decouples task creation from task execution.

### Architecture

```
Producer (any agent)                 Consumer (any worker)
       │                                    │
       ▼                                    ▼
  POST /bus/tasks ──→ ┌─────────┐ ←── GET /bus/tasks
  {task, priority,    │  QUEUE  │     (list pending)
   source}            │ (cap:200│
                      │ Go mem) │ ←── POST /bus/tasks/claim
                      └─────────┘     {task_id, worker}
                           │
                           ▼
                    POST /bus/tasks/complete
                    {task_id, worker, result, status}
```

### Task Lifecycle

| State | Meaning |
|-------|---------|
| `pending` | Created, waiting for a worker to claim |
| `claimed` | Worker has claimed it (atomic — only one wins) |
| `completed` | Worker finished successfully |
| `failed` | Worker reported failure |

### Priority Levels

| Priority | Use For |
|----------|---------|
| `0` | Low — background tasks, cleanup |
| `1` | Normal — standard work items |
| `2` | High — urgent fixes, user-facing |

### Python API (`tools/skynet_task_queue.py`)

```python
from tools.skynet_task_queue import post_task, list_tasks, claim_task, complete_task, grab_next

# Add a task
post_task("Fix bug in parser", priority=2, source="orchestrator")

# List pending tasks
tasks = list_tasks(status="pending")

# Claim a specific task
claim_task("tq_42", "delta")

# Complete it
complete_task("tq_42", "delta", result="Fixed", status="completed")

# Or auto-grab highest priority pending task
task = grab_next("delta")  # Claims and returns task, or None
```

### CLI

```bash
python tools/skynet_task_queue.py list              # Show all tasks
python tools/skynet_task_queue.py add "Fix X"        # Add priority-1 task
python tools/skynet_task_queue.py claim tq_5 delta   # Claim task
python tools/skynet_task_queue.py done tq_5 delta    # Mark complete
python tools/skynet_task_queue.py grab delta          # Auto-grab highest priority
```

---

## 9. Convene Protocol

The **convene protocol** enables multi-worker collaboration sessions. When a task requires coordination between workers, any agent can initiate a convene session.

### Session Lifecycle

```
Initiator                    Go Backend                  Participants
    │                            │                            │
    │  POST /bus/convene         │                            │
    │  {initiator, topic,        │                            │
    │   context, need_workers}   │                            │
    │───────────────────────────→│                            │
    │                            │  Bus broadcast:            │
    │                            │  topic="convene"           │
    │                            │  type="request"            │
    │                            │───────────────────────────→│
    │                            │                            │
    │                            │  PATCH /bus/convene        │
    │                            │  {session_id, worker}      │
    │                            │←───────────────────────────│
    │                            │                            │
    │  Bus: post results         │                            │
    │───────────────────────────→│                            │
    │                            │                            │
    │  DELETE /bus/convene?id=X  │                            │
    │  (resolve session)         │                            │
    │───────────────────────────→│                            │
```

### Python API (`tools/skynet_convene.py`)

```python
from tools.skynet_convene import initiate_session, join_session, resolve_session, discover_sessions

# Initiate
session = initiate_session(initiator="alpha", topic="code review", context="Fix parser", need_workers=2)

# Other workers discover and join
sessions = discover_sessions()
join_session(session["session_id"], "beta")

# When done, resolve
resolve_session(session["session_id"])
```

### CLI

```bash
python tools/skynet_convene.py --initiate --topic "debug" --context "Fix X" --need 2
python tools/skynet_convene.py --discover
python tools/skynet_convene.py --join SESSION_ID --worker delta
```

---

## 10. Health & Diagnostics

SKYNET provides multiple layers of health monitoring and diagnostics.

### Health Report (`tools/skynet_health_report.py`)

The single command to verify the entire system:

```bash
python tools/skynet_health_report.py           # Human-readable report
python tools/skynet_health_report.py --json     # JSON output
python tools/skynet_health_report.py --save     # Save to data/health_report.txt
```

**Report sections:**

| Section | Data Source | Checks |
|---------|------------|--------|
| Backend | `/health`, `/metrics`, `/status` | Uptime, goroutines, memory, request count |
| Workers | `/status` → agents map | Status, model, tasks completed, errors, heartbeat |
| GOD Console | `localhost:8421/engines` | Engine count by tier (online/available/offline) |
| IQ | `skynet_self.compute_iq()` | Score (0-1), trend (rising/stable/declining) |
| Collective | `skynet_collective.intelligence_score()` | Fitness, knowledge, diversity, collaboration |
| Version | `skynet_version.current_version()` | Current version, level, timestamp |
| Watchdog | `data/watchdog_status.json` | GOD Console status, Skynet status, last check |
| Task Queue | `/bus/tasks` | Pending/claimed/completed counts |
| Bus Stress | 10-message probe | Throughput (msg/s), loss detection |
| E2E Tests | `data/e2e_results.json` | Pass/fail counts, last run status |

### E2E Test Suite (`tools/skynet_e2e_test.py`)

21 integration tests across 9 sections:

```bash
python tools/skynet_e2e_test.py           # Run all tests
python tools/skynet_e2e_test.py --save     # Save results to data/e2e_results.json
```

**Test sections**: Backend health, Bus pub/sub, GOD Console, Engine metrics, Self-awareness, Collective intelligence, Version tracking, Watchdog, Task queue.

### Watchdog Daemon (`tools/skynet_watchdog.py`)

Lightweight service monitor with auto-restart:

```bash
python tools/skynet_watchdog.py start      # Run daemon (background)
python tools/skynet_watchdog.py status     # Show last check results
```

| Service | Check Interval | Auto-Restart |
|---------|---------------|--------------|
| GOD Console (8421) | 30 seconds | ✅ Spawns `god_console.py --no-open` |
| Skynet Backend (8420) | 60 seconds | ❌ Alert only |

PID lock at `data/watchdog.pid`. Status at `data/watchdog_status.json`.

### Bus Stress Test Results

Verified throughput (2026-03-10):

| Test | Messages | Time | Throughput | Loss |
|------|----------|------|------------|------|
| Sequential | 100 | 0.285s | 350 msg/s | 0% |
| Concurrent (4 threads) | 100 | 0.145s | 690 msg/s | 0% |
| Health probe (urllib) | 10 | 0.012s | 846 msg/s | 0% |

Bus ring buffer capacity: 100 messages. Oldest messages silently dropped when full.

---

## 11. API Endpoint Reference

### Skynet Backend (port 8420)

| Endpoint | Method | Purpose | Returns |
|----------|--------|---------|---------|
| `/status` | GET | Full system state | `{agents: {name: {status, model, tasks_completed, ...}}}` |
| `/health` | GET | Quick health check | `{status, uptime_s, workers_alive, bus_depth}` |
| `/metrics` | GET | Performance metrics | `{total_requests, goroutine_count, mem_alloc_mb}` |
| `/stream` | GET | SSE event stream (1 Hz) | Server-sent events with full state |
| `/god_feed` | GET | Event feed for GOD Console | Array of recent events |
| `/results` | GET | Completed task results | Array of result objects |
| `/directive` | POST | Submit a task directive | `{status, directive_id}` — requires `goal` field |
| `/dispatch` | POST | Dispatch to specific worker | Requires `directive` + `task_id` fields |
| `/bus/messages` | GET | Read bus messages | Array; filter with `?topic=X&limit=N` |
| `/bus/publish` | POST | Publish to bus | `{status: "published", bus_depth: N}` |
| `/bus/tasks` | GET | List task queue | Array of QueuedTask objects |
| `/bus/tasks` | POST | Add task to queue | `{status: "queued", task_id: "tq_N"}` |
| `/bus/tasks/claim` | POST | Claim a pending task | `{status: "claimed", task_id}` |
| `/bus/tasks/complete` | POST | Mark task done | `{status: "completed", task_id}` |
| `/bus/convene` | GET | List convene sessions | Array of session objects |
| `/bus/convene` | POST | Create convene session | `{session_id, status: "ok"}` |
| `/bus/convene` | PATCH | Join a session | `{status: "joined"}` |
| `/bus/convene?id=X` | DELETE | Resolve/close session | `{session_id, status: "resolved"}` |
| `/brain/pending` | GET | Pending brain tasks | Array of pending tasks |
| `/orchestrate/status` | GET | Orchestration pipeline state | Requires query params |
| `/security/audit` | GET | Security audit log | Array of security events |
| `/security/blocked` | GET | Blocked command log | Array of blocked attempts |
| `/ws/stats` | GET | WebSocket stats | Connection count, message stats |
| `/worker/{name}/heartbeat` | POST | Worker heartbeat | Accepts `model` field |
| `/worker/{name}/tasks` | GET | Worker task history | Array of tasks for worker |

### GOD Console (port 8421)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Console health + uptime |
| `/engines` | GET | Engine metrics (18 engines, 3-tier status) |
| `/bus/messages` | GET | Proxied bus messages |
| `/skynet/status` | GET | Proxied Skynet status |
| `/skynet/self/assess` | GET | Self-awareness assessment |
| `/skynet/collective/score` | GET | Collective intelligence score |

---

## 12. Tool Reference

All tools live in `tools/` with the `skynet_*.py` naming convention.

### Core Operations

| Tool | CLI | Purpose |
|------|-----|---------|
| `skynet_start.py` | `--reconnect`, `--status`, `--dispatch "task"` | Bootstrap orchestrator, open workers, connect engines |
| `skynet_dispatch.py` | `--worker`, `--parallel`, `--smart`, `--blast`, `--all` | Ghost-type tasks into worker windows |
| `skynet_cli.py` | Unified interface | Single entry point for all Skynet operations |

### Intelligence

| Tool | CLI | Purpose |
|------|-----|---------|
| `skynet_brain.py` | Internal API | AI-powered task decomposition with difficulty assessment |
| `skynet_brain_dispatch.py` | Internal API | Bridge between brain planning and dispatch execution |
| `skynet_smart_decompose.py` | Internal API | Keyword-driven prompt splitting and worker routing |
| `skynet_orchestrate.py` | Internal API | Master pipeline: decompose → dispatch → collect → synthesize |
| `skynet_pipeline.py` | Internal API | Composable chain/parallel/retry task execution |

### Awareness & Health

| Tool | CLI | Purpose |
|------|-----|---------|
| `skynet_self.py` | `status`, `pulse`, `identity`, `health`, `introspect`, `goals` | Consciousness kernel with IQ scoring |
| `skynet_health.py` | `--check` | Comprehensive 4-point health check |
| `skynet_health_report.py` | `--json`, `--save` | One-page system health report (all sections) |
| `skynet_e2e_test.py` | `--save` | 21 integration tests across 9 sections |
| `skynet_monitor.py` | Background daemon | Real-time model drift detection and auto-correction |
| `skynet_watchdog.py` | `start`, `status` | Service resurrection (GOD Console auto-restart) |
| `skynet_metrics.py` | Internal API | UIA times, dispatch latency, performance benchmarks |
| `skynet_audit.py` | `--run`, `--fix` | Diagnostic integrity checks with optional auto-fix |
| `skynet_version.py` | `--current`, `--history` | Version tracking and upgrade history |

### Communication & Learning

| Tool | CLI | Purpose |
|------|-----|---------|
| `skynet_collective.py` | `--sync`, `--health`, `--score`, `--swarm-evolve` | Cross-worker strategy federation and swarm evolution |
| `skynet_convene.py` | `--initiate`, `--discover`, `--join` | Multi-worker collaboration sessions |
| `skynet_task_queue.py` | `list`, `add`, `claim`, `done`, `grab` | Pull-based task queue for decoupled work |
| `skynet_knowledge.py` | `--share`, `--absorb` | Bus-based learning broadcast and absorption |
| `skynet_bus_watcher.py` | Background daemon | Auto-routes bus requests to idle workers |
| `skynet_realtime.py` | Internal API | UIA-based result extraction, worker recovery |
| `skynet_sse_daemon.py` | Background daemon | SSE event loop streaming state to `data/realtime.json` |
| `skynet_ws_monitor.py` | Background daemon | WebSocket listener for security alerts |

### Security

| Tool | Purpose |
|------|---------|
| `skynet_identity_guard.py` | Prevents worker preamble injection into orchestrator; HMAC dispatch signing |

---

## Appendix: Engine Status Levels

The engine metrics system (`tools/engine_metrics.py`) uses a **3-tier honest status model**:

| Status | Meaning | Verified By |
|--------|---------|-------------|
| **online** | Class instantiated successfully — engine is verified working | `cls()` succeeded |
| **available** | Module imported, class found, but may fail at runtime | `__import__` + `getattr` succeeded |
| **offline** | Import failed entirely | `__import__` raised exception |

**"Online" never means "importable."** This distinction prevents false confidence in the dashboard.

---

*SKYNET v3.0 Level 3 — Production Grade Intelligence Network*
*Manual written by Workers GAMMA & DELTA | 2026-03-10*
