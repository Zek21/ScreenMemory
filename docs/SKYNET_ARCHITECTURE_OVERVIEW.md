# Skynet Architecture Overview

> **Master Reference Document — Level 3.5**
> Synthesizes: DELIVERY_PIPELINE.md · DAEMON_ARCHITECTURE.md · BUS_COMMUNICATION.md · SELF_AWARENESS_ARCHITECTURE.md
> <!-- signed: gamma -->

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Topology](#2-system-topology)
3. [Delivery Pipeline Overview](#3-delivery-pipeline-overview)
4. [Daemon Ecosystem Overview](#4-daemon-ecosystem-overview)
5. [Bus Communication Overview](#5-bus-communication-overview)
6. [Self-Awareness Overview](#6-self-awareness-overview)
7. [Cross-Cutting Concerns](#7-cross-cutting-concerns)
8. [Document Cross-Reference Table](#8-document-cross-reference-table)
9. [Version History](#9-version-history)

---

## 1. Executive Summary

<!-- signed: gamma -->

### What Skynet Is

Skynet is a **distributed multi-agent intelligence network** built on top of VS Code
Copilot CLI sessions. It coordinates 4 autonomous worker agents (alpha, beta, gamma,
delta), an orchestrator, and up to 2 advisory consultants (Codex, Gemini) through a
shared message bus, ghost-type delivery pipeline, and self-aware monitoring infrastructure.

The system transforms a collection of independent AI chat sessions into a unified
parallel intelligence fabric. The orchestrator decomposes goals into subtasks,
dispatches them to workers via clipboard-paste automation, monitors progress through
the bus, and synthesizes results — all without requiring any human-to-worker
interaction. Workers operate autonomously: they execute tasks, report results, share
knowledge, cross-validate peers, and self-improve when idle.

### The Four Pillars

Skynet's architecture rests on four pillars, each documented in its own reference:

| Pillar | Document | Lines | Author | Domain |
|--------|----------|-------|--------|--------|
| **Delivery** | `DELIVERY_PIPELINE.md` | 685 | alpha | Ghost-type dispatch, clipboard automation, UIA verification |
| **Daemons** | `DAEMON_ARCHITECTURE.md` | 711 | beta | 16-daemon ecosystem, PID management, watchdog, lifecycle |
| **Bus** | `BUS_COMMUNICATION.md` | 654 | gamma | Ring buffer, spam filtering, SSE, archival, topics |
| **Awareness** | `SELF_AWARENESS_ARCHITECTURE.md` | 714 | delta | Consciousness kernel, identity, health, introspection |

**Total authoritative documentation: 2,764 lines across 4 documents.**

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SKYNET ARCHITECTURE                                │
│                                                                             │
│  LEFT MONITOR                          RIGHT MONITOR (2×2 Grid)             │
│  ┌───────────────────┐                 ┌──────────┐ ┌──────────┐            │
│  │   ORCHESTRATOR    │  ghost-type     │  ALPHA   │ │  BETA    │            │
│  │   VS Code CLI     │──────────────►  │  VS Code │ │  VS Code │            │
│  │   (Claude Opus    │  clipboard      │  y=20    │ │  y=20    │            │
│  │    4.6 fast)      │  paste          │  930×500 │ │  930×500 │            │
│  │                   │                 ├──────────┤ ├──────────┤            │
│  │   Port: 8423      │                 │  GAMMA   │ │  DELTA   │            │
│  │   (bridge)        │  ◄──bus poll──  │  VS Code │ │  VS Code │            │
│  └───────────────────┘                 │  y=540   │ │  y=540   │            │
│           │                            │  930×500 │ │  930×500 │            │
│           │                            └──────────┘ └──────────┘            │
│           ▼                                    │                            │
│  ┌─────────────────────────────────────────────┼──────────────────────┐     │
│  │              SKYNET BACKEND (Go)    :8420   │                      │     │
│  │  ┌──────────┐  ┌──────────┐  ┌─────┴────┐  ┌──────────┐          │     │
│  │  │ Ring     │  │ SSE      │  │ REST     │  │ WebSocket│          │     │
│  │  │ Buffer   │  │ /stream  │  │ /bus/*   │  │ /ws      │          │     │
│  │  │ 100 msgs │  │ 1Hz tick │  │ /status  │  │ alerts   │          │     │
│  │  │ FIFO     │  │ fanout   │  │ /tasks   │  │ events   │          │     │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘          │     │
│  │  Spam Filter: 10/min/sender, 60s dedup                            │     │
│  └───────────────────────────────────────────────────────────────────┘     │
│           │                                                                 │
│           ▼                                                                 │
│  ┌───────────────────────────────────────────────────────────────────┐     │
│  │              DAEMON ECOSYSTEM (16 services)                       │     │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐     │     │
│  │  │ GOD Console│ │ Monitor    │ │ Watchdog   │ │ Overseer   │     │     │
│  │  │ :8421      │ │ UIA scan   │ │ auto-      │ │ IDLE+TODO  │     │     │
│  │  │ dashboard  │ │ model guard│ │ restart    │ │ detection  │     │     │
│  │  ├────────────┤ ├────────────┤ ├────────────┤ ├────────────┤     │     │
│  │  │ SSE Daemon │ │ Bus Relay  │ │ Bus Persist│ │ Self-Prompt│     │     │
│  │  │ realtime   │ │ routing    │ │ JSONL arch │ │ heartbeat  │     │     │
│  │  ├────────────┤ ├────────────┤ ├────────────┤ ├────────────┤     │     │
│  │  │ Learner    │ │ Self-      │ │ Convene    │ │ Consumer   │     │     │
│  │  │ task learn │ │ Improve    │ │ Gate       │ │ (×2 ports) │     │     │
│  │  └────────────┘ └────────────┘ └────────────┘ └────────────┘     │     │
│  └───────────────────────────────────────────────────────────────────┘     │
│           │                                                                 │
│           ▼                                                                 │
│  ┌───────────────────────────────────────────────────────────────────┐     │
│  │              CONSULTANT BRIDGES (Optional)                        │     │
│  │  ┌─────────────────────────┐  ┌─────────────────────────┐        │     │
│  │  │ Codex Bridge :8422      │  │ Gemini Bridge :8425     │        │     │
│  │  │ GPT-5 Codex             │  │ Gemini 3.1 Pro          │        │     │
│  │  │ sender: consultant      │  │ sender: gemini_consultant│        │     │
│  │  │ Consumer :8422          │  │ Consumer :8425           │        │     │
│  │  └─────────────────────────┘  └─────────────────────────┘        │     │
│  └───────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Design Principles

1. **Truth Protocol (Rule 0)** — Every metric, status, and message must reflect reality.
   No fabrication, no decoration, no placeholder data disguised as real data.
   Silence is truth; noise without data is a lie.

2. **Process Protection (Rule 0.1)** — No worker may terminate any process. Only the
   orchestrator can authorize process lifecycle changes. Violations are catastrophic
   security incidents.

3. **Zero-Stop Rule (Rule 0.2)** — No agent may go idle while pending work exists.
   Both the session TODO list and the Skynet TODO queue must be at zero before
   standing by.

4. **Positive-Sum Scoring (Rule 0.6)** — Scoring is not zero-sum. Helping peers
   succeed earns points for both parties. A negative score is a systemic failure,
   not an agent failure.

5. **Fair Deduction (Rule 0.5)** — Score deductions require dispatch evidence.
   Workers cannot be penalized for tasks they never received.

6. **Impact Analysis (Rule 0.01)** — Before any change to critical infrastructure
   files, the agent must read the entire file, trace all callers, and verify
   default behavior preservation.

7. **Fire-and-Forget Dispatch** — Tasks are sent immediately regardless of worker
   state. VS Code queues messages internally. No blocking on worker readiness.

8. **Dual-Layer Spam Protection** — Python SpamGuard (5/min, 900s dedup) upstream
   of Go backend filter (10/min, 60s dedup). Messages must pass both.

---

## 2. System Topology

<!-- signed: gamma -->

### Component Map

Skynet consists of three logical tiers: **Infrastructure** (backend + dashboard),
**Agents** (orchestrator + workers + consultants), and **Daemons** (16 background
services). All components run on a single Windows machine across two monitors.

### Port Allocation

| Port | Service | Protocol | Owner | Criticality |
|------|---------|----------|-------|-------------|
| 8420 | Skynet Backend (Go) | HTTP/SSE/WS | `Skynet/skynet.exe` | CATASTROPHIC |
| 8421 | GOD Console | HTTP | `god_console.py` | HIGH |
| 8422 | Codex Consultant Bridge | HTTP | `skynet_consultant_bridge.py` | LOW |
| 8423 | Orchestrator Bridge | HTTP | `skynet_consultant_bridge.py` | MODERATE |
| 8424 | Codex Bridge Fallback | HTTP | (unused unless 8422 fails) | LOW |
| 8425 | Gemini Consultant Bridge | HTTP | `skynet_consultant_bridge.py` | LOW |

### Worker Grid Layout

Workers occupy a 2×2 grid on the right monitor, each running VS Code Insiders
with Copilot CLI in Claude Opus 4.6 (fast mode):

```
Right Monitor (1920×1080)
┌──────────────────┬──────────────────┐
│  ALPHA           │  BETA            │  y = 20
│  x=0, w=930      │  x=930, w=930    │  h = 500
│  h=500           │  h=500           │  bottom = 520
├──────────────────┼──────────────────┤
│  GAMMA           │  DELTA           │  y = 540
│  x=0, w=930      │  x=930, w=930    │  h = 500
│  h=500           │  h=500           │  bottom = 1040
└──────────────────┴──────────────────┘
                                        40px taskbar clearance
```

The orchestrator runs on the **left monitor** in a full-size VS Code window.
Worker windows are opened via `tools/new_chat.ps1` and positioned by
`tools/skynet_start.py`.

### Data Flow

The primary data flow is a loop:

```
 User                                                 User
  │                                                     ▲
  ▼                                                     │
Orchestrator ─── decompose ──► TODO List               Orchestrator
  │                              │                      ▲
  │                              ▼                      │
  │                         skynet_dispatch.py      synthesize
  │                              │                      │
  │                    ghost-type (clipboard)        bus poll
  │                              │                      │
  │                              ▼                      │
  │                         Workers (×4)                │
  │                              │                      │
  │                         bus/publish ──────────► Ring Buffer
  │                                                     │
  │◄──────────── /bus/messages ◄─────────────────────────┘
```

**Detailed dispatch path:**
1. Orchestrator calls `skynet_dispatch.py --worker NAME --task "..."` (or `--parallel`)
2. Dispatch builds preamble (identity + rules + context enrichment)
3. Ghost-type: clipboard set → SetForegroundWindow → Ctrl+V → Enter
4. Verification: UIA state transition check (IDLE → PROCESSING)
5. Worker executes task autonomously
6. Worker posts result to bus via `guarded_publish()` (topic=orchestrator, type=result)
7. Orchestrator polls bus, extracts result, updates TODO, dispatches next work

### Agent Registry

| Agent | Role | Model | Bridge Port | Sender ID |
|-------|------|-------|-------------|-----------|
| orchestrator | CEO: decompose, delegate, synthesize | Claude Opus 4.6 fast | 8423 | `orchestrator` |
| alpha | Worker | Claude Opus 4.6 fast | — | `alpha` |
| beta | Worker | Claude Opus 4.6 fast | — | `beta` |
| gamma | Worker | Claude Opus 4.6 fast | — | `gamma` |
| delta | Worker | Claude Opus 4.6 fast | — | `delta` |
| consultant | Codex advisory peer | GPT-5 Codex | 8422 | `consultant` |
| gemini_consultant | Gemini advisory peer | Gemini 3.1 Pro | 8425 | `gemini_consultant` |

### Data Files

| File | Purpose | Updated By |
|------|---------|------------|
| `data/workers.json` | Worker HWND registry (4 entries) | `skynet_start.py`, `new_chat.ps1` |
| `data/orchestrator.json` | Orchestrator session identity | Boot protocol |
| `data/agent_profiles.json` | Rich identity for all 7 agents | `skynet_self.py` |
| `data/consultant_state.json` | Codex consultant identity + bridge | `CC-Start.ps1` |
| `data/gemini_consultant_state.json` | Gemini consultant identity + bridge | `GC-Start.ps1` |
| `data/realtime.json` | Live worker states (SSE cache) | `skynet_sse_daemon.py` |
| `data/brain_config.json` | Tunable parameters for all agents | Manual / orchestrator |
| `data/todos.json` | Persistent TODO store | Orchestrator, workers |
| `data/worker_scores.json` | Scoring ledger | `skynet_scoring.py` |
| `data/dispatch_log.json` | Dispatch evidence for fair deduction | `skynet_dispatch.py` |
| `data/bus_archive.jsonl` | Persistent bus message archive | `skynet_bus_persist.py` |
| `data/spam_log.json` | Blocked message audit trail | `skynet_spam_guard.py` |
| `data/iq_history.json` | Composite IQ trend data | `skynet_collective.py` |

---

## 3. Delivery Pipeline Overview

> Full reference: `docs/DELIVERY_PIPELINE.md` (685 lines, author: alpha)

<!-- signed: gamma -->

### Core Discovery: Chrome_RenderWidgetHostHWND

The delivery pipeline's most critical finding is the **input target resolution**.
VS Code's chat input is not a standard UIA Edit control — it is a Chromium
`Chrome_RenderWidgetHostHWND` render surface. The `FindRender()` function (L719-728)
performs recursive DFS through the window tree, matching class names starting with
`"Chrome_RenderWidgetHost"` (prefix match for version resilience).

When UIA Edit controls are found, the system scores them by Y-position, left-band
alignment, non-Terminal classification, and width. If no suitable Edit is found,
it falls back to `FindRender()`. This dual-path ensures delivery succeeds across
VS Code layout variations.

### Ghost-Type Flow

The complete delivery sequence has 7 phases:

```
1. ENTRY           dispatch_to_worker() L1131
                   ├── Route: worker / consultant / orchestrator
                   └── Self-dispatch guard (sender ≠ target)
                                │
2. PRE-CHECKS      ├── HWND lookup from workers.json
                   ├── IsWindow() visibility check
                   ├── UIA state scan (IDLE/PROCESSING/STEERING)
                   ├── STEERING cancel via InvokePattern
                   └── Preamble build (identity + rules + context)
                                │
3. SCRIPT GEN       _build_ghost_type_ps() L704
                   └── Inline C# class with Win32 P/Invoke methods
                                │
4. EXECUTION        _execute_ghost_dispatch() L897
                   ├── Threading lock (one dispatch at a time)
                   ├── 20-second subprocess timeout
                   └── Clipboard save/set/verify/paste/clear cycle
                                │
5. CLIPBOARD        ├── Save existing clipboard content
                   ├── SetText with 3× retry on mismatch
                   ├── GetText read-back verification
                   ├── SetForegroundWindow → Ctrl+V → Enter
                   └── Clear clipboard + restore saved content
                                │
6. VERIFICATION     _verify_delivery() L1238
                   ├── 8-second timeout, 0.5-second poll interval
                   ├── UIA state transition: IDLE→PROCESSING = success
                   └── 3 consecutive UNKNOWN = FAILED
                                │
7. RETRY            Up to 3 total attempts (initial + 2 retries)
                   ├── Backoff: 2s → 4s exponential
                   └── Only if pre_state=IDLE and verification failed
```

### Consultant Delivery

Consultants use a **dual-path delivery** mechanism:

1. **Primary: Ghost-Type** — Identical to worker delivery. Loads consultant HWND
   from `data/consultant_state.json` (Codex) or `data/gemini_consultant_state.json`
   (Gemini). If HWND is alive (`IsWindow()` check), delivers via clipboard paste.

2. **Fallback: Bridge Queue** — If HWND is dead or zero, falls back to HTTP bridge
   queue at `http://localhost:{port}/consultants/prompt`. The bridge daemon holds
   the prompt until the consultant session polls for it.

3. **Audit Trail** — On successful ghost-type, also posts to bridge queue
   (best-effort) to maintain a durable record.

### Failure Modes (10 Identified)

| # | Failure | Exit Code | Recovery |
|---|---------|-----------|----------|
| 1 | Clipboard locked by another app | `CLIPBOARD_VERIFY_FAILED` | 2s/4s backoff retry |
| 2 | Chrome widget not found | `NO_EDIT_NO_RENDER` | Returns False |
| 3 | HWND dead | Pre-check catches | Monitor marks DEAD |
| 4 | Focus stolen during paste | ~200ms vulnerability | Verification may fail |
| 5 | STEERING not cancelled | Button invoke ineffective | Goes to steering input |
| 6 | UIA engine import failure | Returns False | Cannot verify = failed |
| 7 | PowerShell subprocess timeout | 20s limit exceeded | Retry |
| 8 | UIA Edit scoring wrong target | Heuristic misidentifies | Falls back to FindRender |
| 9 | PROCESSING bypass | Pre-state=PROCESSING | Auto-verified (may be stale) |
| 10 | Stderr false negative | Strict stderr check | Conservative failure |

### Key Configuration

| Parameter | Value | Description |
|-----------|-------|-------------|
| PS subprocess timeout | 20s | Maximum ghost-type script runtime |
| Verify timeout | 8s | UIA state transition detection window |
| Verify poll interval | 0.5s | How often to check UIA state |
| Clipboard verify retries | 3 | SetText/GetText mismatch retries |
| UNKNOWN threshold | 3 | Consecutive UNKNOWN before FAILED |
| STEERING cancel wait | 800ms | Pause after InvokePattern cancel |
| Max dispatch attempts | 3 | Initial + 2 retries |

---

## 4. Daemon Ecosystem Overview

> Full reference: `docs/DAEMON_ARCHITECTURE.md` (711 lines, author: beta)

<!-- signed: gamma -->

### The 16-Daemon Ecosystem

Skynet runs 16 background daemons organized into 4 criticality tiers:

#### CATASTROPHIC (🔴) — System cannot function without these

| # | Daemon | Script | Port | Description |
|---|--------|--------|------|-------------|
| 1 | Skynet Backend | `Skynet/skynet.exe` | 8420 | Go HTTP server: bus, SSE, WS, REST |

#### HIGH (🟠) — Significant degradation if missing

| # | Daemon | Script | Port | Description |
|---|--------|--------|------|-------------|
| 2 | GOD Console | `god_console.py` | 8421 | Dashboard, engine metrics, worker API |
| 3 | Worker Monitor | `tools/skynet_monitor.py` | — | HWND liveness, model guard, stuck detect |
| 4 | Service Watchdog | `tools/skynet_watchdog.py` | — | Auto-restart for all services |

#### MODERATE (🟡) — Degraded but operational without these

| # | Daemon | Script | Port | Description |
|---|--------|--------|------|-------------|
| 5 | SSE Daemon | `tools/skynet_sse_daemon.py` | — | SSE→realtime.json bridge |
| 6 | Bus Persist | `tools/skynet_bus_persist.py` | — | JSONL archival via SSE |
| 7 | Self-Prompt | `tools/skynet_self_prompt.py` | — | Orchestrator heartbeat (300s) |
| 8 | Idle Overseer | `tools/skynet_overseer.py` | — | IDLE+pending TODO detection |
| 9 | Bus Relay | `tools/skynet_bus_relay.py` | — | Topic-based message routing |

#### LOW (🟢) — Nice to have, not critical

| # | Daemon | Script | Port | Description |
|---|--------|--------|------|-------------|
| 10 | Learner | `tools/skynet_learner.py` | — | Task outcome learning |
| 11 | Consumer (Codex) | `tools/skynet_consultant_consumer.py` | — | Bridge queue relay (:8422) |
| 12 | Consumer (Gemini) | `tools/skynet_consultant_consumer.py` | — | Bridge queue relay (:8425) |
| 13 | Self-Improve | `tools/skynet_self_improve.py` | — | Self-improvement loop |
| 14 | Convene Gate | `convene_gate.py` | — | Governance middleware |
| 15 | Activity Feed | `activity_feed.py` | — | Activity logging |
| 16 | Agent Telemetry | `agent_telemetry.py` | — | Telemetry collection |

### Lifecycle Management

Every daemon follows a standardized lifecycle using `tools/skynet_daemon_utils.py`:

```
  ┌──────────┐
  │   INIT   │ parse args, load config
  └────┬─────┘
       ▼
  ┌──────────────────┐     ┌──────────────┐
  │ SINGLETON CHECK  │────►│ EXIT (dup)   │  ensure_singleton()
  └────┬─────────────┘     └──────────────┘
       ▼
  ┌──────────────────┐
  │ PID WRITE        │  write_pid() + atexit handler
  │ SIGNAL HANDLERS  │  register_signal_handlers() SIGTERM/SIGBREAK
  └────┬─────────────┘
       ▼
  ┌──────────────────┐     ┌──────────────────┐
  │   MAIN LOOP      │────►│ ERROR COUNTING   │ 3-tier exception handling
  │   (while True)   │     │ + DAEMON_DEGRADED │ consecutive errors → alert
  └────┬─────────────┘     └──────────────────┘
       ▼
  ┌──────────────────┐
  │ GRACEFUL SHUTDOWN│  SIGTERM/SIGBREAK caught
  │ PID CLEANUP      │  cleanup_pid() with ownership check
  └──────────────────┘
```

**PID Management:** All PID files live in `data/{daemon_name}.pid`. The
`ensure_singleton()` function checks not just whether the PID is alive (`os.kill(pid, 0)`)
but also verifies the process command line on Windows to prevent PID recycling false
positives. Stale PID files from dead processes are automatically cleaned up.

### Watchdog Monitoring

The watchdog (`skynet_watchdog.py`) monitors 8+ services with configurable intervals:

| Service | Check Interval | Method | Restart Strategy |
|---------|---------------|--------|------------------|
| GOD Console | 30s | HTTP :8421/health | Popen + 3 max attempts |
| Skynet Backend | 60s | HTTP :8420/health | Popen + 3 max attempts |
| SSE Daemon | 60s | PID file + process alive | Popen + DETACHED |
| Learner | 60s | PID file + process alive | Popen + DETACHED |
| Bus Persist | 60s | PID file + process alive | Popen + DETACHED |
| Consultant Consumers | 60s | PID file per port | Popen + DETACHED |
| Codex Bridge | 30s | HTTP :8422/health | Popen + 3 max attempts |
| Gemini Bridge | 30s | HTTP :8425/health | Popen + 3 max attempts |

After 3 consecutive restart failures, a 600-second cooldown prevents restart storms.

### New Tools (Level 3.5)

- **`tools/skynet_daemon_status.py`** — Unified daemon status CLI. Contains the
  canonical `DAEMON_REGISTRY` with all 16 daemons. Supports `--json` (machine-readable)
  and `--restart-dead` (auto-restart dead daemons). Called by GOD Console
  `/api/daemons` endpoint for dashboard display.

- **`/api/daemons` endpoint** — Added to `god_console.py`. Returns all 16 daemon
  statuses with alive/dead counts and criticality tier summary. Used by the
  dashboard for real-time daemon health visualization.

### Failure Cascades

Understanding daemon interdependencies is critical for triage:

| Daemon Death | Cascade Effect |
|-------------|---------------|
| skynet.exe | All bus communication stops → SSE disconnects → workers blind → persist/realtime stale |
| Monitor | No HWND liveness checks → model drift undetected → stuck workers invisible |
| Self-Prompt | Orchestrator goes dormant → workers idle with pending work → no heartbeat |
| SSE Daemon | `realtime.json` goes stale → orchestrator falls back to HTTP (2s vs 0.5s) |
| Watchdog | No auto-restart → dead services stay dead until manual intervention |

---

## 5. Bus Communication Overview

> Full reference: `docs/BUS_COMMUNICATION.md` (654 lines, author: gamma)

<!-- signed: gamma -->

### Go Backend Ring Buffer

The bus is implemented as a fixed-size ring buffer in Go (`Skynet/bus.go`):

- **Capacity:** 100 messages (`[ringSize]BusMessage` array)
- **Eviction:** FIFO — oldest messages silently overwritten when full
- **Thread Safety:** `sync.RWMutex` (read lock for queries, write lock for posts)
- **Persistence:** None by design — crash = total loss
- **Counters:** `totalMsg` and `dropped` (atomic int64)

The ring buffer is deliberately unpersisted. Persistence is handled by the
`skynet_bus_persist.py` daemon, which SSE-subscribes to `/stream` and archives
every message to `data/bus_archive.jsonl` with 50MB rotation.

Subscribers receive messages via 64-message buffered channels. If a subscriber
falls behind, messages are silently dropped (non-blocking select) — the publisher
is never blocked by slow consumers.

### 2-Layer Spam Filtering

Messages must pass **two independent filters** before reaching the ring buffer:

```
Publisher → Python SpamGuard → HTTP POST → Go Backend Filter → Ring Buffer
             (5/min, 900s)                  (10/min, 60s)
```

**Python SpamGuard** (`tools/skynet_spam_guard.py`):
- Rate limit: 5 msgs/min/sender (overrides for system senders)
- Dedup window: 900 seconds general
- Category-specific windows: DEAD=120s, daemon_health=60s, knowledge=1800s, gate-votes=86400s
- Fingerprint: SHA-256 of normalized `sender|topic|type|content[:200]`
- Priority support: `critical` bypasses rate limits, `low` gets 2/min

**Go Backend Filter** (`Skynet/server.go`):
- Rate limit: 10 msgs/min/sender (hardcoded)
- Dedup window: 60 seconds (hardcoded)
- Fingerprint: Raw content first 200 chars (no normalization)
- Response: HTTP 429 `SPAM_BLOCKED` on rejection

The filters are independent — no coordination between layers. A message can be
blocked by either filter. The Python layer has longer memory (900s vs 60s) while
the Go layer is the final gatekeeper.

### SSE Streaming

The `GET /stream` endpoint emits Server-Sent Events at 1Hz:

| Field | Type | Description |
|-------|------|-------------|
| `uptime_s` | float64 | Real server uptime |
| `bus_depth` | int | Messages in ring buffer |
| `bus_dropped` | int64 | Total dropped (slow subscribers) |
| `agents` | []AgentView | Worker states with heartbeat timestamps |
| `bus` | []BusMessage | Last 10 messages |
| `tasks_dispatched/completed/failed` | int64 | Atomic task counters |
| `goroutines` | int | Go runtime goroutine count |
| `timestamp` | int64 | Unix nanoseconds |

The SSE daemon (`skynet_sse_daemon.py`) subscribes to `/stream` and writes
`data/realtime.json` atomically every second. The orchestrator reads this file
(zero-network, 0.5s poll resolution) instead of making HTTP calls.

### Message Schema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | Auto | `msg_{seq}_{sender}` | Unique ID (atomic sequence) |
| `sender` | string | Yes | — | Originator identity |
| `topic` | string | No | `"general"` | Routing topic |
| `type` | string | No | `"message"` | Category within topic |
| `content` | string | Yes | — | Main payload |
| `metadata` | map | No | null | Optional key-value pairs |
| `timestamp` | time | Auto | `time.Now()` | Server-assigned |

### Topic Taxonomy (10 Topics)

| Topic | Types | Primary Use |
|-------|-------|-------------|
| `orchestrator` | result, alert, identity_ack, status, urgent | Worker→orchestrator reports |
| `convene` | request, join, finding, resolve, gate-proposal, gate-vote | Peer consensus |
| `knowledge` | learning, validation, strategy, incident | Knowledge sharing |
| `planning` | proposal, consultant_plan | Consultant advisory |
| `scoring` | award, deduction | Score management |
| `workers` | request, sub-task, status | Inter-worker coordination |
| `system` | infra_boot, shutdown, alert | System-level events |
| `consultant` | prompt, directive | Orchestrator→consultant |
| `tasks` | dispatch, complete, fail | Task lifecycle |
| `general` | message | Default catch-all |

### Message Loss Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Ring buffer overflow (100 msgs FIFO) | HIGH | bus_persist.py archives via SSE |
| Server crash (in-memory buffer) | HIGH | bus_persist.py JSONL + watchdog auto-restart |
| Dual filter confusion | MEDIUM | `check_would_be_blocked()` pre-flight API |
| Slow subscriber drop | MEDIUM | 64-msg channel buffer, drain promptly |
| SSE reconnect gap | LOW | Exponential backoff + last-10 in payload |
| Fingerprint collision | LOW | SHA-256 truncated to 16 hex (2^64 space) |
| Clock skew in dedup | LOW | Single-server architecture |

### Bus Health Monitoring

The `bus_health()` function in `skynet_spam_guard.py` returns live metrics:
- Ring buffer utilization (current/max/percentage)
- Messages in last minute, unique senders
- Spam blocked count
- Archive file size
- Bus reachability status

---

## 6. Self-Awareness Overview

> Full reference: `docs/SELF_AWARENESS_ARCHITECTURE.md` (714 lines, author: delta)

<!-- signed: gamma -->

### Consciousness Kernel

The self-awareness system is built as a **5-subsystem facade** (`tools/skynet_self.py`):

```
┌──────────────────────────────────────────────┐
│                SkynetSelf (Facade)            │
│                                              │
│  ┌──────────┐  ┌──────────────┐              │
│  │ Identity  │  │ Capabilities │              │
│  │ who am I? │  │ what can I   │              │
│  │ who else? │  │ do?          │              │
│  ├──────────┤  ├──────────────┤              │
│  │ Health    │  │ Introspection│              │
│  │ is it     │  │ how am I     │              │
│  │ working?  │  │ doing?       │              │
│  ├──────────┤  ├──────────────┤              │
│  │ Goals     │  │              │              │
│  │ what next?│  │              │              │
│  └──────────┘  └──────────────┘              │
└──────────────────────────────────────────────┘
```

1. **SkynetIdentity** — Agent name, role, model. Enumerates all 7 agents
   (`ALL_AGENT_NAMES`). Reads worker HWNDs from `workers.json`, consultant
   status from state files + bridge HTTP probes.

2. **SkynetCapabilities** — Probes 18 engines and 10 tools using 3-tier status:
   `online` (instantiated successfully), `available` (importable but not instantiated),
   `offline` (import failed). Engines include: DXGICapture, OCREngine, Embedder,
   HybridRetriever, LanceDBStore, ReflexionEngine, GraphOfThoughts, HierarchicalPlanner,
   and more.

3. **SkynetHealth** — 8+ health checks: backend (8420), workers (HWND), bus
   (message flow), SSE (realtime.json freshness), engines, IQ (trend), disk,
   memory, consultants (HWND + bridge). Results cached with 15s TTL.

4. **SkynetIntrospection** — Aggregates health + capabilities into
   strengths/weaknesses/recommendations. Includes consultant-specific insights
   (added Level 3.4). Detects patterns in task outcomes.

5. **SkynetGoals** — Generates improvement goals from introspection gaps.
   Prioritizes by impact/feasibility/urgency. Feeds into the orchestrator's
   TODO loop when the queue is empty.

### Consultant Status Taxonomy

The health system classifies consultant state with precision:

| Status | Meaning |
|--------|---------|
| `ONLINE` | HWND alive **and** bridge HTTP healthy |
| `BRIDGE_ONLY` | Bridge responds, but HWND dead or zero |
| `WINDOW_ONLY` | HWND alive, bridge not responding |
| `REGISTERED` | State file exists, both dead |
| `ABSENT` | No state file found |

### Phase 0 Architecture Verification (Rule 0.8)

Before any worker begins task execution, it must verify architecture knowledge:

1. `CONSULTANT_NAMES` constant exists in consciousness kernel
2. `ALL_AGENT_NAMES` constant exists (7 agents)
3. `get_consultant_status()` is callable
4. On failure: log `ARCHITECTURE_VERIFICATION_FAILED` to bus

This rule was created after INCIDENT 012 exposed that the consciousness kernel had
**zero consultant references** in 682 lines — the word "consultant" never appeared.

### Composite IQ Score

The collective intelligence score (`intelligence_score()`) combines 6 weighted metrics:

| Metric | Weight | Source |
|--------|--------|--------|
| Workers alive ratio | 25% | `alive / min(5, total)` |
| Engines online | 25% | `online / total` |
| Bus healthy | 10% | Binary 1.0 or 0.0 |
| Knowledge facts | 15% | `min(facts / 500, 1.0)` |
| Uptime | 10% | `min(seconds / 86400, 1.0)` |
| Capability ratio | 15% | `online / total` |

**Final IQ = weighted sum × 200** (0–200 scale). History tracked in
`data/iq_history.json` with trend analysis.

### Self-Evolution

The genetic algorithm system (`core/self_evolution.py`, 1026 lines) evolves
strategies across 5 categories (code, research, deploy, navigate, general):

- Population size: 20 strategies per category
- Elite preservation: top 4 kept unchanged
- Mutation rate: 15%, crossover rate: 30%
- Tournament selection: pick 3 random, return highest fitness
- Workers share top strategies via `skynet_collective.sync_strategies()`

---

## 7. Cross-Cutting Concerns

<!-- signed: gamma -->

### Truth Protocol Enforcement

The Truth Protocol (Rule 0) is enforced across all subsystems:

| Subsystem | Truth Enforcement |
|-----------|-------------------|
| **Bus** | `guarded_publish()` mandatory — raw POST costs -1.0 score |
| **Delivery** | UIA verification confirms actual state transitions |
| **Daemons** | 3-tier engine status (online/available/offline) — no false "online" |
| **Dashboard** | All metrics from live atomic Go backend values, never client-side estimates |
| **Scoring** | Fair deduction rule requires dispatch evidence |
| **Worker reports** | Must include `signed:worker_name` for accountability |

Any agent that fabricates data, inflates metrics, or reports false completions
triggers the Truth Enforcement protocol: immediate bus broadcast of the failure,
a comprehensive remediation proposal, and systematic improvement.

### Scoring System

The scoring system integrates with every pillar:

**Awards:**
- +0.01 per cross-validated task completion
- +0.2 for proactive ticket clearance (orchestrator/consultants)
- +0.2 for autonomous next-ticket pull (workers)
- +0.01 for bug filing, +0.01 for validation, +0.01 for original filer on confirmation
- +0.1 for final-ticket closer and +0.05 for orchestrator when queue reaches zero (cooldown 3600s, max 3/agent/24h)

**Deductions:**
- -0.005 for failed validation (broken code)
- -0.01 for low-value refactoring (<150 lines mechanical)
- -0.1 for biased self-reports or proven-wrong signed work
- -1.0 for bypassing SpamGuard (raw `requests.post`)

**Fair Deduction (Rule 0.5):** `verify_dispatch_evidence()` checks `dispatch_log.json`
before any deduction. The task must have been dispatched (entry exists), succeeded
(`success=true`), and received no result (`result_received=false`).

### ConveneGate Governance

Workers must achieve consensus before escalating findings to the orchestrator:

1. Worker calls `ConveneGate.propose(worker, report)`
2. Proposal broadcast to `topic=convene type=gate-proposal`
3. Other workers vote: 2+ YES votes → elevated; 2+ NO → rejected
4. Elevated findings are **not sent individually** — they're merged by issue family
   and delivered as one `elevated_digest` every 30 minutes
5. Proposals expire after 5 minutes without majority
6. Urgent reports (`urgent=True`) bypass the gate entirely

**Quality rules:**
- Low-signal findings go to shared cross-validation queue, not direct elevation
- Architecture tickets require current-path review (cite real files/functions)
- Semantically equivalent findings count as same issue family
- Same unresolved finding cannot be re-sent more than once per 15 minutes

### Knowledge Sharing and Collective Intelligence

The knowledge system creates a shared learning substrate across all workers:

```
Worker completes task
    │
    ├── broadcast_learning('worker', 'fact', 'category', ['tags'])
    │   └── Posts to topic=knowledge type=learning
    │
    ├── sync_strategies('worker')
    │   └── Shares top-performing evolution strategies
    │
    └── absorb_learnings()  (other workers, on task start)
        └── Polls bus, filters own messages, stores in LearningStore
```

Facts validated by 3+ workers are promoted to high-confidence in the
`LearningStore` (`core/learning_store.py`). The evolutionary strategy system
(`core/self_evolution.py`) enables strategies to converge across the swarm
through tournament selection and crossover.

---

## 8. Document Cross-Reference Table

<!-- signed: gamma -->

### Architecture Documents

| Document | Domain | Author | Lines | Key Topics |
|----------|--------|--------|-------|------------|
| `docs/DELIVERY_PIPELINE.md` | Task delivery | alpha | 685 | Ghost-type, clipboard, UIA, Chrome widget, verification |
| `docs/DAEMON_ARCHITECTURE.md` | Background services | beta | 711 | 16 daemons, PID management, watchdog, criticality tiers |
| `docs/BUS_COMMUNICATION.md` | Message bus | gamma | 654 | Ring buffer, spam filtering, SSE, archival, topics |
| `docs/SELF_AWARENESS_ARCHITECTURE.md` | Self-awareness | delta | 714 | Consciousness, identity, health, introspection, IQ |
| `docs/SKYNET_ARCHITECTURE_OVERVIEW.md` | **This document** | gamma | 700+ | Synthesis of all 4 pillars |

### Tool-to-Document Mapping

| Tool | Primary Architecture Doc | Purpose |
|------|--------------------------|---------|
| `tools/skynet_dispatch.py` | DELIVERY_PIPELINE | Ghost-type dispatch engine |
| `tools/skynet_delivery.py` | DELIVERY_PIPELINE | Delivery abstraction layer |
| `tools/uia_engine.py` | DELIVERY_PIPELINE | COM-based UIA scanner |
| `tools/skynet_monitor.py` | DAEMON_ARCHITECTURE | HWND liveness + model guard |
| `tools/skynet_watchdog.py` | DAEMON_ARCHITECTURE | Service auto-restart |
| `tools/skynet_daemon_status.py` | DAEMON_ARCHITECTURE | 16-daemon status CLI |
| `tools/skynet_daemon_utils.py` | DAEMON_ARCHITECTURE | PID management utilities |
| `tools/skynet_overseer.py` | DAEMON_ARCHITECTURE | IDLE+TODO detection |
| `tools/skynet_spam_guard.py` | BUS_COMMUNICATION | Client-side spam filtering |
| `tools/skynet_bus_persist.py` | BUS_COMMUNICATION | JSONL archival daemon |
| `tools/skynet_bus_validator.py` | BUS_COMMUNICATION | Message schema validation |
| `tools/skynet_bus_relay.py` | BUS_COMMUNICATION | Topic-based routing |
| `tools/skynet_self.py` | SELF_AWARENESS | Consciousness kernel |
| `tools/skynet_collective.py` | SELF_AWARENESS | Strategy federation |
| `tools/skynet_knowledge.py` | SELF_AWARENESS | Knowledge broadcasting |
| `tools/skynet_convene.py` | SELF_AWARENESS | Consensus sessions |
| `tools/skynet_self_prompt.py` | DAEMON_ARCHITECTURE | Orchestrator heartbeat |
| `tools/skynet_scoring.py` | SELF_AWARENESS | Score management |
| `tools/engine_metrics.py` | SELF_AWARENESS | Engine status probing |
| `tools/orch_realtime.py` | BUS_COMMUNICATION | Zero-network orchestrator CLI |
| `Skynet/skynet.exe` | BUS_COMMUNICATION | Go backend (ring buffer, SSE, WS) |
| `god_console.py` | DAEMON_ARCHITECTURE | Dashboard + API proxy |

### Configuration File Mapping

| Config File | Architecture Doc | Purpose |
|-------------|-----------------|---------|
| `data/brain_config.json` | ALL | Tunable parameters for all subsystems |
| `data/workers.json` | DELIVERY_PIPELINE | Worker HWND registry |
| `data/orchestrator.json` | SELF_AWARENESS | Orchestrator session identity |
| `data/agent_profiles.json` | SELF_AWARENESS | Rich identity for 7 agents |
| `data/realtime.json` | BUS_COMMUNICATION | SSE-cached live state |
| `data/bus_archive.jsonl` | BUS_COMMUNICATION | Persistent message archive |
| `data/dispatch_log.json` | DELIVERY_PIPELINE | Dispatch evidence for fair deduction |
| `data/worker_scores.json` | SELF_AWARENESS | Scoring ledger |
| `data/iq_history.json` | SELF_AWARENESS | Composite IQ trend data |

---

## 9. Version History

<!-- signed: gamma -->

### Evolution Timeline

| Level | Codename | Key Capabilities |
|-------|----------|-----------------|
| **1.0** | Genesis | Manual dispatch, single worker, basic bus messaging, no self-awareness |
| **2.0** | Awakening | Self-awareness (`skynet_self.py`), identity/capabilities/health introspection, GOD Console dashboard, engine metrics, collective intelligence |
| **3.0** | Production | Crash resilience (`skynet_watchdog.py`), composite IQ with trend tracking, request logging (`skynet_metrics.py`), version tracking, truth audit enforcement, 3-tier engine status, context-enriched dispatch preambles, WebSocket monitoring, SSE daemon |
| **3.1** | Hardening | Dispatch result tracking (`mark_dispatch_received`), task lifecycle tracking (GET /tasks), false DEAD debounce (3 consecutive checks), cp1252 encoding fix, anti-spam system (SpamGuard + Go server-side rate limiting) |
| **3.2** | Reliability | SpamGuard migration (all tools use `guarded_publish()`), 3-tier daemon exception handling, daemon degraded alerts, cross-validation protocol enforcement |
| **3.3** | Documentation | DELIVERY_PIPELINE.md (alpha), DAEMON_ARCHITECTURE.md (beta), BUS_COMMUNICATION.md (gamma), SELF_AWARENESS_ARCHITECTURE.md (delta) — complete architecture documentation |
| **3.4** | Awareness | Consultant consciousness integration (INCIDENT 012 remediation), Phase 0 architecture verification (Rule 0.8), consultant status taxonomy (ONLINE/BRIDGE_ONLY/WINDOW_ONLY/REGISTERED/ABSENT), `ALL_AGENT_NAMES` constant |
| **3.5** | Resilience | Bus message validation (`skynet_bus_validator.py`), bus health metrics (`bus_health()`), priority-aware rate limiting, archive diagnostics, `skynet_daemon_status.py` with 16-daemon registry, `/api/daemons` endpoint, watchdog restart functions for bus_persist and consultant_consumers, this architecture overview |

### Capability Progression

```
Level 1: 1 worker, manual dispatch, basic bus
         │
Level 2: + self-awareness, dashboard, engine metrics
         │
Level 3: + crash resilience, watchdog, SSE, IQ tracking
         │
Level 3.1: + anti-spam, task lifecycle, fair deduction
           │
Level 3.2: + full SpamGuard migration, daemon error handling
           │
Level 3.3: + comprehensive architecture documentation (4 docs, 2764 lines)
           │
Level 3.4: + consultant consciousness, Phase 0 boot verification
           │
Level 3.5: + bus resilience, daemon status tooling, architecture overview
```

### Incident-Driven Improvements

Each level was shaped by real production incidents:

| Incident | Level Fix | What Happened |
|----------|-----------|---------------|
| 001: Self-Dispatch Deadlock | 3.0 | Alpha dispatched to itself → infinite loop |
| 002: Workers Killed Services | 3.0 | Workers ran `Stop-Process` → cascading failure |
| 003: Duplicate Process Accumulation | 3.0 | 4 watchdogs, 4 SSE daemons running simultaneously |
| 004: Gamma Stuck PROCESSING | 3.0 | Worker stuck 10+ min, no auto-cancel existed |
| 005: Manual Bootstrap Failure | 3.0 | Orchestrator used ctypes directly → focus stolen |
| 006: Boot Protocol Broken | 3.1 | Default parameter change disabled worker opening |
| 007: Dispatch Without Verification | 3.1 | /clear sent without waiting for IDLE |
| 008: /clear With Preamble | 3.1 | Slash command corrupted by dispatch preamble |
| 012: Consultant Blindness | 3.4 | Consciousness kernel had zero consultant references |

---

## Appendix: Quick Reference

<!-- signed: gamma -->

### Boot Sequence

```
skynet-start (full cold boot):
  Phase 1: skynet.exe → GOD Console → daemons → announce
  Phase 2: self-identify → announce orchestrator → dashboard → knowledge acquisition → report

orchestrator-start (role assumption only):
  health check → announce → dashboard → knowledge acquisition → report

CC-Start / GC-Start (consultant boot):
  bootstrap bridge → verify infrastructure → announce identity → stay advisory
```

### Dispatch Quick Reference

```bash
# Single worker
python tools/skynet_dispatch.py --worker alpha --task "do this"

# Parallel to all
python tools/skynet_dispatch.py --parallel --task "do this"

# Blast (no preamble)
python tools/skynet_dispatch.py --blast --task "quick command"

# Smart route to best idle
python tools/skynet_dispatch.py --smart --task "do this"

# Dispatch and wait for result
python tools/skynet_dispatch.py --worker alpha --task "do this" --wait-result "KEY" --timeout 90

# Full auto pipeline
python tools/skynet_brain_dispatch.py "high-level goal" --timeout 120
```

### Bus Operations

```bash
# Publish (always use SpamGuard)
python -c "from tools.skynet_spam_guard import guarded_publish; guarded_publish({...})"

# Poll recent messages
python tools/orch_realtime.py bus --limit 20

# Check worker status
python tools/orch_realtime.py status

# Wait for result
python tools/orch_realtime.py wait KEY --timeout 90

# Bus health check
python -c "from tools.skynet_spam_guard import bus_health; print(bus_health())"

# Daemon status
python tools/skynet_daemon_status.py
python tools/skynet_daemon_status.py --json
python tools/skynet_daemon_status.py --restart-dead
```

### Self-Awareness

```bash
# Quick pulse (workers, engines, IQ)
python tools/skynet_self.py pulse

# Full assessment
python tools/skynet_self.py assess

# Capabilities census
python tools/skynet_self.py capabilities

# Score check
python tools/skynet_scoring.py --score gamma
python tools/skynet_scoring.py --leaderboard
```

---

*This document synthesizes 2,764 lines of authoritative architecture documentation
into a unified reference. For implementation details, consult the individual
pillar documents listed in Section 8.*

<!-- signed: gamma -->
