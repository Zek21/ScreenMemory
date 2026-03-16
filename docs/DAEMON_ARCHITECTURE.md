# Skynet Daemon Architecture

> **Definitive reference for the Skynet daemon ecosystem.**
> Every daemon, its purpose, ports, interconnections, PID management, failure behavior, and configuration.

<!-- signed: beta -->

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Infrastructure Daemons (CRITICAL)](#3-infrastructure-daemons-critical)
4. [Monitoring Daemons](#4-monitoring-daemons)
5. [Communication Daemons](#5-communication-daemons)
6. [Intelligence Daemons](#6-intelligence-daemons)
7. [PID Management](#7-pid-management)
8. [Criticality Matrix](#8-criticality-matrix)
9. [Health Check Mechanisms](#9-health-check-mechanisms)
10. [Startup Sequence](#10-startup-sequence)
11. [Failure Cascades](#11-failure-cascades)
12. [Configuration](#12-configuration)
13. [Additional Daemons (Level 3.5)](#13-additional-daemons-added-in-level-35)
14. [Updated Criticality Matrix (Level 3.5)](#14-updated-criticality-matrix-level-35)
15. [Related Files](#15-related-files)

---

## 1. Overview

### What Are Daemons?

Skynet daemons are long-running background processes that provide the infrastructure, monitoring,
communication, and intelligence layers of the multi-agent system. They operate independently of the
orchestrator and workers, ensuring continuous system health, message delivery, knowledge capture,
and autonomous self-improvement.

### Why They Exist

The Skynet multi-agent system consists of an orchestrator (CEO), 4 worker agents (alpha, beta, gamma,
delta), and optional consultant peers (Codex, Gemini). Without daemons, the system would have:

- **No health monitoring** — dead workers would go undetected
- **No message bus** — agents could not communicate
- **No persistence** — bus messages would be lost on restart
- **No learning** — task outcomes would not feed back into strategy evolution
- **No self-correction** — model drift, stuck workers, and stale processes would accumulate
- **No orchestrator heartbeat** — the orchestrator would go idle when no user input arrives

### Daemon Lifecycle Model

Every daemon follows a standard lifecycle:

```
INIT ──► SINGLETON CHECK ──► PID WRITE ──► MAIN LOOP ──► GRACEFUL SHUTDOWN ──► PID CLEANUP
  │            │                                │               │
  │            ▼                                ▼               ▼
  │       (exit if another          (atexit + signal         (remove PID file,
  │        instance running)         handlers registered)     log shutdown)
  │
  ▼
 FATAL ERROR ──► ERROR COUNTING ──► DEGRADED ALERT ──► (continue or exit)
```

Key lifecycle invariants:

1. **Singleton enforcement** — only one instance of each daemon may run (PID file locking)
2. **PID file written on start** — enables external liveness checks
3. **Graceful shutdown via atexit/signal** — PID file cleaned up on exit
4. **Error counting with degraded alerts** — consecutive errors trigger bus alerts before exit
5. **Stale PID detection** — if a PID file exists but the process is dead, the daemon reclaims it

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SKYNET DAEMON ECOSYSTEM                               │
│                                                                                 │
│  ┌─────────────────────────── INFRASTRUCTURE ───────────────────────────┐        │
│  │                                                                      │        │
│  │   ┌──────────────────┐          ┌──────────────────┐                 │        │
│  │   │   skynet.exe     │◄────────►│  god_console.py  │                 │        │
│  │   │   (Go backend)   │  HTTP    │  (Python/Flask)  │                 │        │
│  │   │   Port 8420      │  proxy   │  Port 8421       │                 │        │
│  │   │                  │          │                  │                 │        │
│  │   │  ┌────────────┐  │          │  ┌────────────┐  │                 │        │
│  │   │  │ Ring Buffer │  │          │  │ Dashboard  │  │                 │        │
│  │   │  │ (100 msgs)  │  │          │  │ SSE Stream │  │                 │        │
│  │   │  ├────────────┤  │          │  ├────────────┤  │                 │        │
│  │   │  │ SSE /stream │──┼──────┐  │  │ /engines   │  │                 │        │
│  │   │  │ WebSocket   │  │      │  │  │ /api/*     │  │                 │        │
│  │   │  │ Task Tracker│  │      │  │  │ /stream/*  │  │                 │        │
│  │   │  │ Spam Filter │  │      │  │  └────────────┘  │                 │        │
│  │   │  └────────────┘  │      │  └──────────────────┘                 │        │
│  │   └──────────────────┘      │                                        │        │
│  └──────────────────────────────┼────────────────────────────────────────┘        │
│                                 │                                                │
│  ┌──── MONITORING ─────┐       │  ┌──── COMMUNICATION ────┐                     │
│  │                      │       │  │                        │                     │
│  │  skynet_monitor.py   │       │  │  skynet_bus_relay.py   │                     │
│  │  ├─ HWND alive (30s) │       │  │  ├─ 3s poll cycle      │                     │
│  │  ├─ Model guard (60s)│       │  │  ├─ Topic routing      │                     │
│  │  ├─ Stuck detect     │       │  │  └─ 1hr digest hold    │                     │
│  │  └─ DEAD debounce    │       │  │                        │                     │
│  │                      │       │  │  skynet_bus_persist.py  │                     │
│  │  skynet_watchdog.py  │       │  │  ├─ SSE subscriber ────┼──── /stream         │
│  │  ├─ GOD check (30s)  │       │  │  ├─ JSONL archival     │                     │
│  │  ├─ Backend (60s)    │       │  │  └─ 50MB rotation      │                     │
│  │  ├─ Auto-restart     │       │  │                        │                     │
│  │  └─ 3-attempt limit  │       │  │  skynet_sse_daemon.py  │                     │
│  │                      │       │  │  ├─ SSE subscriber ────┼──── /stream         │
│  │  skynet_overseer.py  │       │  │  ├─ realtime.json      │                     │
│  │  ├─ IDLE+TODO (30s)  │       │  │  └─ Exp. backoff       │                     │
│  │  ├─ Service check    │       │  │                        │                     │
│  │  └─ Stall detect     │       │  └────────────────────────┘                     │
│  │                      │       │                                                │
│  └──────────────────────┘       │  ┌──── INTELLIGENCE ─────┐                     │
│                                 │  │                        │                     │
│                                 │  │  skynet_learner.py     │                     │
│                                 │  │  ├─ 30s bus poll       │                     │
│                                 │  │  ├─ Episode processing │                     │
│                                 │  │  └─ Learning store     │                     │
│                                 │  │                        │                     │
│                                 │  │  skynet_self_prompt.py │                     │
│                                 │  │  ├─ 300s cycle         │                     │
│                                 │  │  ├─ 60s idle gate      │                     │
│                                 │  │  └─ UIA prompt fire    │                     │
│                                 │  │                        │                     │
│                                 │  │  consultant_consumer   │                     │
│                                 │  │  ├─ 2s poll            │                     │
│                                 │  │  ├─ Bridge queue read  │                     │
│                                 │  │  └─ Bus relay          │                     │
│                                 │  │                        │                     │
│                                 │  └────────────────────────┘                     │
│                                                                                 │
│  ┌──── CONSULTANT BRIDGES ──────────────────────────────────────┐               │
│  │                                                               │               │
│  │  Codex Bridge (port 8422)        Gemini Bridge (port 8425)    │               │
│  │  ├─ /health                      ├─ /health                   │               │
│  │  ├─ /consultants/prompts/*       ├─ /consultants/prompts/*    │               │
│  │  └─ 2s heartbeat                 └─ 2s heartbeat              │               │
│  │                                                               │               │
│  └───────────────────────────────────────────────────────────────┘               │
│                                                                                 │
│  ┌──── DATA FLOWS ──────────────────────────────────────────────┐               │
│  │                                                               │               │
│  │  Bus Messages ──► Ring Buffer (100) ──► SSE /stream           │               │
│  │       │                                      │                │               │
│  │       ▼                                      ├──► sse_daemon ──► realtime.json│
│  │  bus_relay ──► Topic routing                  │                │               │
│  │       │                                      └──► bus_persist ──► archive.jsonl│
│  │       ▼                                                       │               │
│  │  Worker/Convene delivery                                      │               │
│  │                                                               │               │
│  └───────────────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Infrastructure Daemons (CRITICAL)

### 3.1 skynet.exe (Go Backend)

The Go backend is the **single point of failure** for the entire Skynet system. Every agent, daemon,
and tool communicates through it.

| Attribute | Value |
|-----------|-------|
| **Binary** | `Skynet/skynet.exe` |
| **Port** | 8420 |
| **Language** | Go |
| **Ring Buffer** | 100 messages (fixed-size, zero-copy after init) |
| **SSE Interval** | 2 seconds |
| **Read Timeout** | 5 seconds |
| **Write Timeout** | 0 (unlimited — required for SSE/stream endpoints) |

#### HTTP Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/status` | GET | Server status and worker states |
| `/health` | GET | Health check with endpoint listing |
| `/stream` | GET (SSE) | Main event stream — aggregated state every 2s |
| `/activity/stream` | GET (SSE) | Worker activity stream — 2s interval |
| `/bus/publish` | POST | Publish message to bus ring buffer |
| `/bus/messages` | GET | Retrieve bus messages (supports `?limit=N`) |
| `/bus/clear` | POST | Clear bus buffer |
| `/bus/convene` | GET | Convene session state |
| `/bus/tasks` | GET/POST | Task queue — list and create tasks |
| `/bus/tasks/claim` | POST | Worker claims a task |
| `/bus/tasks/complete` | POST | Worker marks task complete |
| `/directive` | GET | Retrieve pending directives |
| `/dispatch` | POST | Dispatch task to a worker |
| `/results` | GET | Task result history |
| `/cancel` | POST | Cancel pending task |
| `/worker/` | GET/POST | Dynamic worker route handler |
| `/tasks` | GET | Task lifecycle tracker (supports `?worker=` and `?limit=`) |
| `/task/complete` | POST | Mark task complete inline |
| `/orchestrate` | POST | Create directive |
| `/orchestrate/status` | GET | Directive status |
| `/orchestrate/pipeline` | POST | Multi-step pipeline directive |
| `/ws` | Upgrade | WebSocket endpoint (raw HTTP hijack, 64-msg buffer per client) |
| `/ws/stats` | GET | WebSocket connection statistics |
| `/metrics` | GET | Performance metrics |
| `/god_feed` | GET | GOD Console event feed |
| `/brain/pending` | GET | Pending brain directives |
| `/brain/ack` | POST | Brain directive acknowledgment |
| `/dashboard` | GET | Static HTML dashboard |
| `/security/audit` | GET | Security audit log |
| `/security/blocked` | GET | Blocked security events |

#### Spam Filtering (Server-Side)

The Go backend enforces server-side spam filtering on `/bus/publish`:

- **Fingerprint dedup**: SHA-256 hash of `sender+topic+type+content`, 60-second window
- **Rate limiting**: 10 messages per minute per sender
- **Response**: HTTP 429 with body `SPAM_BLOCKED` when filtered
- **Logging**: Blocked messages logged with `[SPAM_BLOCKED]` prefix

### 3.2 god_console.py (GOD Console)

The Python-based GOD Console provides the web dashboard and acts as a caching proxy to the Go backend.

| Attribute | Value |
|-----------|-------|
| **Script** | `god_console.py` (repo root) and `core/god_console.py` |
| **Port** | 8421 (configurable, constant `DEFAULT_PORT = 8421`) |
| **Server** | `ThreadedHTTPServer` with `ConsoleHandler` |
| **PID File** | `data/god_console.pid` |
| **Cache TTL** | 3 seconds (`_DASHBOARD_TTL = 3`) |

#### Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/dashboard` | GET | Main HTML dashboard (`dashboard.html`) |
| `/health` | GET | Health check with endpoint listing |
| `/version` | GET | Version, level, timestamp |
| `/bus`, `/bus/messages` | GET | Cached bus messages (proxied from 8420) |
| `/engines` | GET | Engine status with 3-second cache |
| `/stream/dashboard` | GET (SSE) | Dashboard SSE — pushed every 2 seconds |
| `/status`, `/skynet/status` | GET | Backend status (proxied from 8420) |
| `/todos` | GET | TODO items from `data/todos.json` |
| `/consultants` | GET | Consultant bridge status (ports 8422, 8424, 8425) |
| `/workers/health` | GET | Worker health snapshot |
| `/api/worker/action` | POST | Worker action buttons (HALT/CLR/RST) |
| `/api/scores` | GET | Scoring leaderboard |
| `/api/workers/performance` | GET | Worker performance data |
| `/leadership` | GET | Consolidated orchestrator + consultant status |
| `/performance/leaderboard` | GET | Worker ranking |
| `/learner/health` | GET | Learner daemon health |
| `/learner/metrics` | GET | Learner episode metrics |
| `/overseer` | GET | Overseer status |
| `/system/health` | GET | System resource usage |
| `/metrics/throughput` | GET | Task throughput metrics |
| `/processes` | GET | Running system processes |

---

## 4. Monitoring Daemons

### 4.1 skynet_monitor.py

The monitor daemon is the **eyes of the system** — it watches every worker window for liveness,
model correctness, and stuck states. It is the only daemon that performs UIA (UI Automation)
operations.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_monitor.py` |
| **PID File** | `data/monitor.pid` |
| **HWND Check Interval** | 30 seconds (`HWND_CHECK_INTERVAL = 30`) |
| **Model Check Interval** | 60 seconds (`MODEL_CHECK_INTERVAL = 60`) |
| **Orchestrator Model Check** | 30 seconds (`ORCH_MODEL_CHECK_INTERVAL = 30`) |
| **Idle Optimization** | After 3 consecutive IDLE scans, switches to 60s HWND interval |
| **Daemon Health Check** | 120 seconds (`DAEMON_CHECK_INTERVAL = 120`) |

#### Capabilities

1. **HWND Liveness** — checks `IsWindowVisible` for each worker window every 30s
2. **Model Guard** — verifies Claude Opus 4.6 (fast mode) via UIA; auto-corrects drift using `fix_model()` from `skynet_model_guard`
3. **Stuck Detection** — flags workers in `PROCESSING` state for >600s (`STUCK_PROCESSING_THRESHOLD = 600`); auto-cancels via `uia_engine.cancel_generation(hwnd)`
4. **DEAD Alert Debounce** — requires 3 consecutive HWND failures before posting `DEAD` alert; 300-second dedup window prevents repeat alerts
5. **Heartbeat Posting** — POSTs to `/worker/{name}/heartbeat` on every health check cycle
6. **Adaptive Intervals** — when all workers idle for 3+ scans (`IDLE_STREAK_THRESHOLD = 3`), slows to `HWND_IDLE_INTERVAL = 60` to save CPU

### 4.2 skynet_watchdog.py

The watchdog monitors critical services and auto-restarts them on failure.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_watchdog.py` |
| **PID File** | `data/watchdog.pid` |
| **GOD Check Interval** | 30 seconds (`GOD_CHECK_INTERVAL = 30`) |
| **Backend Check Interval** | 60 seconds (`SKYNET_CHECK_INTERVAL = 60`) |
| **Max Restart Attempts** | 3 (`MAX_RESTART_ATTEMPTS = 3`) |
| **Restart Cooldown** | 600 seconds / 10 minutes (`RESTART_COOLDOWN_S = 600`) |
| **Atexit Cleanup** | Yes — `_cleanup_pid()` via `atexit.register()` with `missing_ok=True` |

#### Monitored Services

| Service | Port | Check Method |
|---------|------|-------------|
| GOD Console | 8421 | HTTP GET `/health` |
| Skynet Backend | 8420 | HTTP GET `/health` |
| Codex Consultant Bridge | 8422 | HTTP GET `/health` |
| Gemini Consultant Bridge | 8425 | HTTP GET `/health` |
| SSE Daemon | — | PID file check |
| Learner Daemon | — | PID file check |

#### Restart Strategy

1. Detect service down (failed health check or dead PID)
2. Increment restart counter for that service
3. If counter < 3: attempt restart via `Start-Process` / `subprocess.Popen`
4. If counter >= 3: enter 600-second cooldown before further attempts
5. Reset counter on successful health check

### 4.3 skynet_overseer.py

The overseer detects idle workers sitting on pending work — the "anti-laziness" daemon.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_overseer.py` |
| **PID File** | `data/overseer.pid` |
| **Worker Scan Interval** | 30 seconds (`WORKER_SCAN_INTERVAL = 30`) |
| **Idle Stall Threshold** | 180 seconds (`IDLE_STALL_S = 180`) |
| **Service Dedup Window** | 300 seconds (`SERVICE_DEDUP_WINDOW = 300`) |
| **PID Cleanup** | `try/finally` in `run()` + atexit |

#### What It Monitors

1. **IDLE + Pending TODOs** — scans `data/todos.json` and worker states from `/status`; posts `WORKER_IDLE_WITH_PENDING_TODOS` alert when a worker is IDLE but has assignable work
2. **Stalled Workers** — workers IDLE for >180s with pending bus results
3. **Service Liveness** — checks Backend (8420), GOD Console (8421), and Watchdog daemon

---

## 5. Communication Daemons

### 5.1 skynet_bus_relay.py

Routes bus messages to the correct recipients based on topic.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_bus_relay.py` |
| **PID File** | `data/bus_relay.pid` |
| **Poll Interval** | 3.0 seconds (`POLL_INTERVAL = 3.0`, min 2.0s) |
| **Relay Topics** | `{"workers", "convene"} | WORKER_NAMES` (alpha, beta, gamma, delta) |
| **Digest Hold Time** | 3600 seconds / 1 hour (`HOLD_INTERVAL_S = 3600`) |
| **Max Dedup Entries** | 500 (`MAX_DELIVERED_IDS = 500`) |

#### Behavior

1. Polls `GET /bus/messages?limit=20` every 3 seconds
2. Filters messages by `RELAY_TOPICS` — only forwards worker-targeted and convene messages
3. Tracks delivered message IDs (500-entry cache) to prevent re-delivery
4. Holds digest messages for 1 hour before forwarding (consolidation)

### 5.2 skynet_bus_persist.py

Archives all bus messages to persistent JSONL storage via SSE subscription.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_bus_persist.py` |
| **PID File** | `data/bus_persist.pid` |
| **SSE Endpoint** | `http://localhost:8420/stream` |
| **Output File** | `data/bus_archive.jsonl` |
| **Rotation Size** | 50 MB (`50 * 1024 * 1024`) |
| **Rotation Function** | `_rotate_if_needed()` |

#### Behavior

1. Subscribes to SSE `/stream` endpoint on the Go backend
2. Extracts bus messages from SSE events
3. Appends each message as a JSON line to `data/bus_archive.jsonl`
4. When file exceeds 50 MB, rotates to `bus_archive.jsonl.1`, `.2`, etc.
5. Provides persistent audit trail independent of the ring buffer's 100-message limit

### 5.3 skynet_sse_daemon.py

Bridges SSE events from the Go backend into a local JSON state file for zero-network orchestrator reads.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_sse_daemon.py` |
| **PID File** | `data/sse_daemon.pid` |
| **SSE Endpoint** | `http://127.0.0.1:8420/stream` |
| **Output File** | `data/realtime.json` |
| **Reconnect Backoff** | Exponential: 2s → 4s → 8s → … → 30s max |
| **Degraded Threshold** | 10 consecutive errors (`_DEGRADED_THRESHOLD = 10`) |

#### Behavior

1. Subscribes to Go backend SSE `/stream` endpoint
2. Parses SSE events and extracts worker states, bus messages, task stats
3. Writes state atomically to `data/realtime.json` via `_atomic_write()` (write-to-temp + rename)
4. The orchestrator reads this file instead of making HTTP calls — zero-network, 0.5s resolution
5. On connection failure, uses exponential backoff (2s initial, 30s max)
6. After 10 consecutive errors, posts `DAEMON_DEGRADED` alert to bus

#### Output Schema (`data/realtime.json`)

```json
{
  "workers": {"alpha": {"state": "IDLE", ...}, ...},
  "bus_recent": [...],
  "pending_results": [...],
  "pending_alerts": [...],
  "task_stats": {"dispatched": 0, "completed": 0, "failed": 0},
  "uptime_s": 3600,
  "latency_ms": 12,
  "timestamp": "2026-03-12T10:00:00Z"
}
```

---

## 6. Intelligence Daemons

### 6.1 skynet_learner.py

Processes task results from the bus and feeds them into the persistent learning system.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_learner.py` |
| **PID File** | `data/learner.pid` |
| **Loop Interval** | 30 seconds (`DEFAULT_LOOP_INTERVAL = 30`) |
| **Health Report Interval** | 300 seconds |
| **Max Insights Per Task** | 6 |

#### Behavior

1. Polls bus for messages with `type=result` every 30 seconds
2. Categorizes results by domain: infrastructure, browser, dashboard, dispatch, security, perception, email, prospecting, code
3. Extracts learnings from task outcomes via `PersistentLearningSystem.learn_from_task()`
4. Stores insights in `core.learning_store` with confidence scores
5. Broadcasts top insight via bus when `broadcast_top_insight` is enabled in config
6. Provides episode metrics via the GOD Console `/learner/metrics` endpoint

### 6.2 skynet_self_prompt.py

The orchestrator heartbeat daemon — keeps the orchestrator alive and responsive by injecting
status prompts when all workers are idle.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_self_prompt.py` |
| **PID File** | `data/self_prompt.pid` |
| **Main Loop Interval** | 300 seconds / 5 minutes (`LOOP_INTERVAL = 300`) |
| **All-Idle Quiet Window** | 60 seconds (`ALL_IDLE_INTERVAL = 60`) |
| **Min Prompt Gap** | 45 seconds (`MIN_PROMPT_GAP = 45`) |
| **Idle Worker Threshold** | 90 seconds (`IDLE_WORKER_THRESHOLD = 90`) |
| **Error Counting** | `_consecutive_loop_errors`, posts `DAEMON_DEGRADED` on threshold breach |

#### Idle Gate (Critical Safety Mechanism)

The idle gate prevents the self-prompt daemon from typing into the orchestrator while workers
are active (which would steal context and disrupt ongoing operations).

**Fire Authorization Sequence:**

1. **Snapshot worker states** — reads `/status` or UIA scans all 4 workers
2. **All must be IDLE** — `_snapshot_all_workers_idle()` checks `alpha`, `beta`, `gamma`, `delta` are all `"IDLE"`
3. **Start quiet window** — records `all_idle_since` timestamp
4. **Wait 60 seconds** — all workers must remain continuously IDLE for the full window
5. **Pre-fire re-check** — immediately before typing, `_refresh_all_idle_window()` re-probes all workers
6. **Fire** — `_send_self_prompt(orch_hwnd, prompt_text)` types into orchestrator input via UIA

**If any worker transitions out of IDLE during the quiet window, the timer resets.**

#### Required Workers

```python
REQUIRED_WORKERS = ("alpha", "beta", "gamma", "delta")
```

All four must be IDLE. The daemon does NOT fire if any worker is missing from status or in PROCESSING/STEERING/UNKNOWN state.

### 6.3 skynet_consultant_consumer.py

Polls consultant bridge queues and relays prompts/results to the bus.

| Attribute | Value |
|-----------|-------|
| **Script** | `tools/skynet_consultant_consumer.py` |
| **PID Files** | `data/consultant_consumer_8422.pid`, `data/consultant_consumer_8425.pid` |
| **Poll Interval** | 2.0 seconds (`POLL_INTERVAL = 2.0`) |
| **Bridge Ports** | 8422 (Codex), 8425 (Gemini) |

#### Bridge Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/consultants/prompts/next` | GET | Fetch next queued prompt |
| `/consultants/prompts/ack` | POST | Acknowledge prompt receipt |
| `/consultants/prompts/complete` | POST | Mark prompt processing complete |

#### Behavior

1. Polls bridge `/consultants/prompts/next` every 2 seconds
2. When a prompt is found, ACKs it via POST `/consultants/prompts/ack`
3. Relays the prompt content to the Skynet bus via `guarded_publish()`
4. After processing, marks complete via POST `/consultants/prompts/complete`
5. Uses `guarded_publish()` with raw `requests.post` fallback for bus relay

---

## 7. PID Management

### PID File Paths

| Daemon | PID File | Directory |
|--------|----------|-----------|
| GOD Console | `data/god_console.pid` | `data/` |
| Watchdog | `data/watchdog.pid` | `data/` |
| Monitor | `data/monitor.pid` | `data/` |
| Overseer | `data/overseer.pid` | `data/` |
| Self-Prompt | `data/self_prompt.pid` | `data/` |
| Self-Improve | `data/self_improve.pid` | `data/` |
| Bus Relay | `data/bus_relay.pid` | `data/` |
| Bus Persist | `data/bus_persist.pid` | `data/` |
| SSE Daemon | `data/sse_daemon.pid` | `data/` |
| Learner | `data/learner.pid` | `data/` |
| Consultant Consumer (Codex) | `data/consultant_consumer_8422.pid` | `data/` |
| Consultant Consumer (Gemini) | `data/consultant_consumer_8425.pid` | `data/` |
| Convene Gate | `data/convene_gate.pid` | `data/` |
| Activity Feed | `data/activity_feed.pid` | `data/` |
| Agent Telemetry | `data/agent_telemetry.pid` | `data/` |

### Singleton Enforcement

Each daemon checks for an existing instance before starting:

1. Read PID file if it exists
2. Check if PID is alive via `os.kill(pid, 0)` (signal 0 = existence check)
3. On Windows: verify process command line via `Get-CimInstance Win32_Process` to prevent PID recycling false positives
4. If alive and matches: exit (another instance running)
5. If dead or mismatched: delete stale PID file and proceed

### skynet_daemon_utils.py API

The shared PID utility module (`tools/skynet_daemon_utils.py`) provides standardized PID management:

| Function | Signature | Purpose |
|----------|-----------|---------|
| `write_pid` | `write_pid(daemon_name: str) -> Path` | Write current PID to `data/{name}.pid`, register atexit cleanup |
| `check_pid` | `check_pid(daemon_name: str) -> bool` | Return `True` if daemon running (PID file + process alive) |
| `cleanup_pid` | `cleanup_pid(daemon_name: str) -> None` | Remove PID file if owned by current process |
| `ensure_singleton` | `ensure_singleton(daemon_name: str) -> bool` | Return `True` if safe to start (no running instance) |
| `register_signal_handlers` | `register_signal_handlers(shutdown_flag_setter=None)` | Register SIGTERM/SIGBREAK handlers; accepts optional callback |

---

## 8. Criticality Matrix

Impact classification when each daemon dies:

| Daemon | Criticality | Impact of Death |
|--------|-------------|-----------------|
| **skynet.exe** | 🔴 **CATASTROPHIC** | **Total system failure.** All bus communication stops. Workers cannot report. Orchestrator is blind. Dashboard shows nothing. Every other daemon loses its data source. |
| **god_console.py** | 🟠 **HIGH** | Dashboard goes offline. No web UI. Engine metrics unavailable. Worker action buttons stop working. SSE dashboard stream dies. System still functions via CLI. |
| **skynet_monitor.py** | 🟠 **HIGH** | No DEAD detection — dead workers go unnoticed indefinitely. No model guard — model drift accumulates silently. No stuck detection — workers hang forever. No heartbeat posting. |
| **skynet_watchdog.py** | 🟠 **HIGH** | No auto-restart of dead services. GOD Console or backend failures require manual intervention. Consultant bridges not monitored. |
| **skynet_sse_daemon.py** | 🟡 **MODERATE** | `data/realtime.json` goes stale. Orchestrator falls back to HTTP polling (slower, 2s vs 0.5s). `orch_realtime.py` wait commands degrade. |
| **skynet_bus_persist.py** | 🟡 **MODERATE** | Bus messages not archived. Audit trail breaks. Ring buffer limit (100 msgs) becomes the only storage. Older messages lost permanently. |
| **skynet_self_prompt.py** | 🟡 **MODERATE** | Orchestrator goes dormant when no user input. Workers may idle with pending work because no one prompts the orchestrator. System becomes reactive instead of proactive. |
| **skynet_overseer.py** | 🟡 **MODERATE** | IDLE-with-pending-TODOs alerts stop. Workers can sit idle while work is available with no alarm. |
| **skynet_bus_relay.py** | 🟡 **MODERATE** | Worker-to-worker and convene topic routing stops. Direct bus polling still works but targeted delivery fails. |
| **skynet_learner.py** | 🟢 **LOW** | Task learnings not captured. Strategy evolution stalls. System still functions but stops getting smarter. |
| **consultant_consumer** | 🟢 **LOW** | Consultant bridge prompts not relayed to bus. Consultants can still post directly. Only impacts bridge-queue transport. |

---

## 9. Health Check Mechanisms

### Per-Daemon Health Reporting

| Daemon | Health Mechanism | Endpoint / Channel |
|--------|------------------|--------------------|
| **skynet.exe** | HTTP `GET /health` returns endpoint list | Port 8420 |
| **god_console.py** | HTTP `GET /health` returns endpoint list | Port 8421 |
| **skynet_monitor.py** | Posts heartbeats to `/worker/{name}/heartbeat` | Bus (8420) |
| **skynet_watchdog.py** | PID file alive check; bus alert on restart events | PID + Bus |
| **skynet_overseer.py** | PID file + bus alerts (`WORKER_IDLE_WITH_PENDING_TODOS`) | PID + Bus |
| **skynet_self_prompt.py** | PID file + bus health messages; `DAEMON_DEGRADED` on error threshold | PID + Bus |
| **skynet_sse_daemon.py** | PID file + `DAEMON_DEGRADED` alert after 10 consecutive errors | PID + Bus |
| **skynet_bus_relay.py** | PID file alive check | PID only |
| **skynet_bus_persist.py** | PID file alive check | PID only |
| **skynet_learner.py** | PID file + GOD Console `/learner/health` endpoint | PID + HTTP |
| **consultant_consumer** | PID file; bridge `/health` endpoint | PID + HTTP |

### External Monitoring

- **Watchdog** monitors: GOD Console, Skynet Backend, Consultant Bridges, SSE Daemon, Learner
- **Overseer** monitors: Backend, GOD Console, Watchdog
- **Monitor** monitors: Worker windows (HWND), worker models (UIA), worker stuck state
- **Orch-Start.ps1** `Start-VerifiedDaemon` verifies: PID file written + process alive after start

### Health Data Files

| File | Purpose | Updated By |
|------|---------|-----------|
| `data/realtime.json` | Live system state (workers, bus, tasks) | SSE Daemon |
| `data/worker_health.json` | Worker health snapshots | Monitor |
| `data/bus_archive.jsonl` | Persistent bus message archive | Bus Persist |
| `data/spam_log.json` | Blocked message audit trail | SpamGuard |

---

## 10. Startup Sequence

The boot sequence is defined in `Orch-Start.ps1` and follows strict ordering to ensure
dependencies are satisfied before dependents start.

### Phase 1: Infrastructure (skynet-start trigger)

```
Step 1 ──► Skynet Backend (skynet.exe)
           Port 8420 — wait up to 15 seconds for port ready
           MUST succeed before anything else

Step 2 ──► GOD Console (god_console.py)
           Port 8421 — wait up to 10 seconds for port ready
           Requires: Skynet Backend (proxies requests to 8420)

Step 3 ──► Worker Windows (optional, via skynet_start.py)
           UIA-heavy — sequential window opening via new_chat.ps1
           Requires: Skynet Backend (for status), GOD Console (for dashboard)
           Skipped if -SkipInfra flag or workers already exist
```

### Phase 2: Daemons (started by Orch-Start.ps1)

```
Step 4a ──► Self-Prompt (skynet_self_prompt.py start)
            PID: data/self_prompt.pid
            Requires: Skynet Backend (reads /status)

Step 4b ──► Self-Improve (skynet_self_improve.py start)
            PID: data/self_improve.pid
            Requires: Skynet Backend

Step 4c ──► Bus Relay (skynet_bus_relay.py)
            PID: data/bus_relay.pid
            Requires: Skynet Backend (polls /bus/messages)

Step 4d ──► Learner (skynet_learner.py --daemon)
            PID: data/learner.pid
            Requires: Skynet Backend (polls bus for results)
```

### Phase 3: Post-Boot

```
Step 5 ──► Open Dashboard (http://localhost:8421/dashboard)
           Requires: GOD Console

Step 6 ──► Orchestrator Role Assumption
           Identity announcement, knowledge acquisition, bus polling
           Requires: All infrastructure
```

### Daemons NOT Started by Orch-Start.ps1

These daemons are started separately or on-demand:

| Daemon | How Started | Notes |
|--------|-------------|-------|
| Monitor (`skynet_monitor.py`) | Manual or `cmd /c python tools/skynet_monitor.py` | Often started by orchestrator post-boot |
| Watchdog (`skynet_watchdog.py`) | Manual start | Should auto-start but not in daemon specs |
| Overseer (`skynet_overseer.py`) | Manual start | Autonomous monitoring |
| Bus Persist (`skynet_bus_persist.py`) | Manual start | SSE subscriber |
| SSE Daemon (`skynet_sse_daemon.py`) | Manual start | SSE-to-file bridge |
| Consultant Consumer | Started by `CC-Start.ps1` / `GC-Start.ps1` | Per-consultant instance |
| Convene Gate | Manual start | Consensus governance |

---

## 11. Failure Cascades

What breaks when each daemon dies — first-order and cascading effects.

### skynet.exe Death (CATASTROPHIC)

```
skynet.exe dies
  ├──► Bus communication stops (ALL agents blind)
  ├──► SSE /stream disconnects
  │    ├──► sse_daemon loses connection → realtime.json goes stale
  │    └──► bus_persist loses connection → archive stops
  ├──► /status returns nothing → monitor cannot check workers
  ├──► /bus/publish fails → SpamGuard publishes fail
  │    └──► Workers cannot report results → orchestrator thinks they're stuck
  ├──► /directive fails → dispatch cannot route tasks
  ├──► Watchdog detects death → attempts restart (up to 3 times)
  └──► All daemons enter degraded mode (no bus connectivity)
```

### god_console.py Death (HIGH)

```
god_console.py dies
  ├──► Dashboard goes offline → no web UI
  ├──► /engines endpoint unavailable → engine metrics lost
  ├──► /api/worker/action stops → worker buttons non-functional
  ├──► /stream/dashboard SSE dies → dashboard clients disconnect
  ├──► Watchdog detects death → attempts restart
  └──► System continues functioning via CLI and direct backend access
```

### skynet_monitor.py Death (HIGH)

```
skynet_monitor.py dies
  ├──► No HWND liveness checks → dead workers invisible
  ├──► No model guard → model drift accumulates
  │    └──► Workers may switch to Sonnet/Auto → intelligence degrades
  ├──► No stuck detection → workers hang indefinitely in PROCESSING
  │    └──► Orchestrator keeps dispatching to stuck workers → task loss
  ├──► No heartbeat posting → /worker/{name}/heartbeat stale
  └──► Worker health data in data/worker_health.json goes stale
```

### skynet_self_prompt.py Death (MODERATE)

```
skynet_self_prompt.py dies
  ├──► Orchestrator goes dormant (no heartbeat prompts)
  ├──► Workers finish tasks → go IDLE → no one prompts orchestrator
  │    └──► Pending TODOs accumulate with no dispatch
  └──► System becomes fully reactive (only responds to user input)
```

### skynet_sse_daemon.py Death (MODERATE)

```
skynet_sse_daemon.py dies
  ├──► data/realtime.json goes stale
  ├──► orch_realtime.py reads stale data → false state info
  │    └──► Orchestrator falls back to HTTP polling (slower)
  └──► wait commands lose 0.5s resolution → degrade to 2s HTTP polling
```

---

## 12. Configuration

### brain_config.json Daemon Parameters

All daemon-tunable parameters in `data/brain_config.json`:

#### Self-Prompt Section

```json
{
  "self_prompt": {
    "loop_interval": 30,
    "health_report_interval": 300
  }
}
```

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `loop_interval` | 30 | Hot-reloadable main loop interval (seconds) |
| `health_report_interval` | 300 | Interval between health report bus messages |

#### Self-Awareness Section

```json
{
  "self_awareness": {
    "pulse_interval_s": 3,
    "introspection_interval_s": 30
  }
}
```

#### Learner Section

```json
{
  "learner": {
    "loop_interval_s": 30,
    "health_report_interval_s": 300,
    "max_insights_per_task": 6,
    "broadcast_top_insight": true
  }
}
```

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `loop_interval_s` | 30 | Bus poll interval |
| `health_report_interval_s` | 300 | Health report frequency |
| `max_insights_per_task` | 6 | Cap on insights extracted per task result |
| `broadcast_top_insight` | true | Whether to broadcast top learning to bus |

#### Watchdog Section

```json
{
  "watchdog": {
    "watchdog_interval": 30,
    "god_check_interval": 30,
    "skynet_check_interval": 60
  }
}
```

#### Stuck Detector Section

```json
{
  "stuck_detector": {
    "processing_info_s": 600,
    "processing_long_s": 900,
    "alert_dedup_window_s": 300
  }
}
```

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `processing_info_s` | 600 | Seconds before PROCESSING worker flagged stuck |
| `processing_long_s` | 900 | Seconds before critical stuck alert |
| `alert_dedup_window_s` | 300 | Minimum gap between duplicate stuck alerts |

## 13. Additional Daemons (Added in Level 3.5)
<!-- signed: delta -->

The following 11 daemons were added post-Sprint 2 and documented during the
Level 3.5 cross-validation refresh.

### 13.1 skynet_bus_watcher.py — Bus Auto-Router

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_bus_watcher.py` |
| **PID File** | `data/bus_watcher.pid` |
| **Port** | — (polls 8420) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ (KeyboardInterrupt only) |

Background daemon for orchestrator total bus awareness. Polls the message bus,
tracks worker activity, auto-routes pending requests to idle workers, and
maintains a rolling activity log.

### 13.2 skynet_idle_monitor.py — Extended Idle Detection

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_idle_monitor.py` |
| **PID File** | `data/idle_monitor.pid` |
| **Port** | — (UIA engine) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ |

Monitors worker states every 30s via UIA engine. Detects workers idle for
extended periods (12+ hours) and auto-redispatches them with self-invoke
tasks including full capability preamble.

### 13.3 skynet_ws_monitor.py — WebSocket Event Listener

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_ws_monitor.py` |
| **PID File** | `data/ws_monitor.pid` |
| **Port** | — (connects to ws://localhost:8420/ws) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ |

Real-time WebSocket event listener. Connects to the Go backend's `/ws`
endpoint and receives security alerts, bus events, and system notifications.
Logs events to `data/ws_events.log`.

### 13.4 skynet_worker_autonomy.py — Improvement Generator

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_worker_autonomy.py` |
| **PID File** | `data/worker_autonomy.pid` |
| **Port** | — (bus interaction) |
| **Criticality** | 🟢 LOW |
| **Signal Handlers** | ❌ |

Auto-generates improvement tasks for idle workers. When workers have no
pending work and the bus has no tasks, generates improvement proposals
and dispatches them as directives. Tracks activity in `data/autonomy_log.json`.
Supports `--daemon` flag for continuous 60s loop.

### 13.5 skynet_orch_poller.py — Orchestrator Queue

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_orch_poller.py` |
| **PID File** | `data/orch_poller.pid` |
| **Port** | — (bus interaction) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ (KeyboardInterrupt cleanup) |

Polls the bus for messages addressed to the orchestrator (`topic=orchestrator`,
`type=directive` or `type=task`) and queues them in `data/orch_queue.json`.
The self-prompt daemon reads this queue and types pending directives into the
orchestrator window.

### 13.6 skynet_bus_worker.py — Per-Worker Bus Delivery

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_bus_worker.py` |
| **PID File** | `data/bus_worker_{name}.pid` (runtime) |
| **Port** | — (polls 8420) |
| **Criticality** | 🟢 LOW |
| **Signal Handlers** | ❌ |

Bus-based task delivery alternative to cross-window keyboard injection.
Each worker instance polls the bus every 3s for `topic=worker_{name} type=task`
messages and types received tasks into the worker's VS Code chat window.

### 13.7 skynet_activity_feed.py — Worker Activity Extraction

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_activity_feed.py` |
| **PID File** | `data/activity_feed.pid` |
| **Port** | — (posts to 8420) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ✅ SIGTERM + atexit cleanup |

Real-time worker activity extraction daemon. Scans all 4 worker windows every
3s via UIA, extracts conversation content, diffs against previous snapshot,
and posts NEW lines to the Skynet bus. Uses proper singleton enforcement with
`_acquire_singleton()` and `atexit.register(_release_singleton)`.

### 13.8 skynet_agent_telemetry.py — Telemetry HTTP Server

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_agent_telemetry.py` |
| **PID File** | `data/agent_telemetry.pid` |
| **Port** | **8426** |
| **Criticality** | 🟢 LOW |
| **Signal Handlers** | ❌ (atexit cleanup) |

Truthful live visibility for agent state: `doing` (inferred from task/transport
state), `typing_visible` (only visible text in UIA fields), `thinking_summary`
(explicit self-report only). Runs HTTP API server on port 8426.

### 13.9 skynet_stuck_detector.py — Stuck Worker Detection

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_stuck_detector.py` |
| **PID File** | — |
| **Port** | — (bus interaction) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ |

Detects and intervenes when workers get stuck. In `--monitor` mode, polls
worker states every 15s via UIA, tracks state history, and alerts the
orchestrator. Philosophy: workers in PROCESSING are thinking (don't interrupt);
only the orchestrator decides intervention. Tracks in `data/worker_stuck_history.json`.

### 13.10 skynet_consultant_bridge.py — Consultant HTTP Bridge

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_consultant_bridge.py` |
| **PID File** | `data/consultant_bridge.pid` |
| **Port** | **8422** (Codex) / **8425** (Gemini) |
| **Criticality** | 🟡 MEDIUM |
| **Signal Handlers** | ❌ (atexit cleanup) |

Live presence bridge HTTP server for consultants. Supports multiple consultant
identities via CLI args (`--id`, `--display-name`, `--model`, `--source`,
`--state-file`). Accepts prompts into queue, heartbeats live status every 2s.
Queue prompts can be consumed by `skynet_consultant_consumer.py`.

### 13.11 skynet_worker_loop.py — Worker Autonomy Loop

| Property | Value |
|----------|-------|
| **Script** | `tools/skynet_worker_loop.py` |
| **PID File** | — |
| **Port** | — (bus polling) |
| **Criticality** | 🟢 LOW |
| **Signal Handlers** | ❌ |

Worker autonomy daemon. Each worker runs this loop to stay productive without
orchestrator babysitting. Polls bus for tasks, checks TODOs, picks up planning
proposals when idle. Tracks state in `data/task_queue.json`.

---

## 14. Updated Criticality Matrix (Level 3.5)
<!-- signed: delta -->

Updated to align with AGENTS.md (which has been updated more frequently):

| Daemon | Criticality | Purpose |
|--------|-------------|---------|
| **skynet.exe** | 🔴 CATASTROPHIC | Total system data source |
| **skynet_monitor.py** | 🔴 CRITICAL | Worker HWND + model guard |
| **skynet_watchdog.py** | 🔴 CRITICAL | Service auto-restart |
| **skynet_realtime.py** | 🔴 CRITICAL | SSE→realtime.json bridge |
| **god_console.py** | 🟠 HIGH | Dashboard + engine proxy |
| **skynet_self_prompt.py** | 🟠 HIGH | Orchestrator heartbeat |
| **skynet_bus_relay.py** | 🟠 HIGH | Topic-based routing |
| **skynet_overseer.py** | 🟠 HIGH | IDLE+TODO detection |
| **skynet_learner.py** | 🟠 HIGH | Learning engine |
| **skynet_sse_daemon.py** | 🟡 MEDIUM | Dashboard live updates |
| **skynet_bus_persist.py** | 🟡 MEDIUM | JSONL archival |
| **skynet_bus_watcher.py** | 🟡 MEDIUM | Bus auto-routing |
| **skynet_ws_monitor.py** | 🟡 MEDIUM | WebSocket events |
| **skynet_idle_monitor.py** | 🟡 MEDIUM | Extended idle detection |
| **skynet_orch_poller.py** | 🟡 MEDIUM | Orchestrator queue |
| **skynet_activity_feed.py** | 🟡 MEDIUM | Worker activity extraction |
| **skynet_stuck_detector.py** | 🟡 MEDIUM | Stuck worker detection |
| **skynet_consultant_bridge.py** | 🟡 MEDIUM | Consultant HTTP bridge |
| **skynet_consultant_consumer.py** | 🟡 MEDIUM | Bridge queue relay |
| **skynet_self_improve.py** | 🟠 HIGH | Self-improvement engine |
| **convene_gate.py** | 🟢 LOW | Consensus governance |
| **skynet_worker_autonomy.py** | 🟢 LOW | Improvement generator |
| **skynet_agent_telemetry.py** | 🟢 LOW | Telemetry API (port 8426) |
| **skynet_bus_worker.py** | 🟢 LOW | Per-worker bus delivery |
| **skynet_worker_loop.py** | 🟢 LOW | Worker autonomy loop |
| **skynet_health_report.py** | 🟢 LOW | Periodic health reports |

---

## 15. Related Files
<!-- signed: delta -->

### All Daemon Source Files

| File | Path | Port | PID File | Purpose |
|------|------|------|----------|---------|
| skynet.exe | `Skynet/skynet.exe` | 8420 | — | Go backend: bus, SSE, WebSocket, tasks |
| god_console.py | `god_console.py` | 8421 | `data/god_console.pid` | Dashboard, engine proxy, SSE, worker actions |
| skynet_monitor.py | `tools/skynet_monitor.py` | — | `data/monitor.pid` | Worker HWND, model guard, stuck detection |
| skynet_watchdog.py | `tools/skynet_watchdog.py` | — | `data/watchdog.pid` | Service liveness, auto-restart |
| skynet_overseer.py | `tools/skynet_overseer.py` | — | `data/overseer.pid` | IDLE+TODO detection, service checks |
| skynet_bus_relay.py | `tools/skynet_bus_relay.py` | — | `data/bus_relay.pid` | Topic-based message routing |
| skynet_bus_persist.py | `tools/skynet_bus_persist.py` | — | `data/bus_persist.pid` | SSE→JSONL archival, 50MB rotation |
| skynet_sse_daemon.py | `tools/skynet_sse_daemon.py` | — | `data/sse_daemon.pid` | SSE→realtime.json bridge |
| skynet_learner.py | `tools/skynet_learner.py` | — | `data/learner.pid` | Episode processing, learning store |
| skynet_self_prompt.py | `tools/skynet_self_prompt.py` | — | `data/self_prompt.pid` | Orchestrator heartbeat, idle gate |
| skynet_self_improve.py | `tools/skynet_self_improve.py` | — | `data/self_improve.pid` | Self-improvement loop |
| skynet_consultant_consumer.py | `tools/skynet_consultant_consumer.py` | — | `data/consultant_consumer_{port}.pid` | Bridge queue→bus relay |
| convene_gate.py | `convene_gate.py` | — | `data/convene_gate.pid` | Consensus governance middleware |
| skynet_bus_watcher.py | `tools/skynet_bus_watcher.py` | — | `data/bus_watcher.pid` | Bus auto-routing |
| skynet_idle_monitor.py | `tools/skynet_idle_monitor.py` | — | `data/idle_monitor.pid` | Extended idle detection (12h) |
| skynet_ws_monitor.py | `tools/skynet_ws_monitor.py` | — | `data/ws_monitor.pid` | WebSocket event listener |
| skynet_worker_autonomy.py | `tools/skynet_worker_autonomy.py` | — | `data/worker_autonomy.pid` | Improvement task generation |
| skynet_orch_poller.py | `tools/skynet_orch_poller.py` | — | `data/orch_poller.pid` | Orchestrator bus poller |
| skynet_bus_worker.py | `tools/skynet_bus_worker.py` | — | `data/bus_worker_{name}.pid` | Per-worker bus delivery |
| skynet_activity_feed.py | `tools/skynet_activity_feed.py` | — | `data/activity_feed.pid` | Worker activity extraction |
| skynet_agent_telemetry.py | `tools/skynet_agent_telemetry.py` | 8426 | `data/agent_telemetry.pid` | Telemetry HTTP server |
| skynet_stuck_detector.py | `tools/skynet_stuck_detector.py` | — | — | Stuck worker detection |
| skynet_consultant_bridge.py | `tools/skynet_consultant_bridge.py` | 8422/8425 | `data/consultant_bridge.pid` | Consultant HTTP bridge |
| skynet_worker_loop.py | `tools/skynet_worker_loop.py` | — | — | Worker autonomy loop |

### Supporting Files

| File | Path | Purpose |
|------|------|---------|
| skynet_daemon_utils.py | `tools/skynet_daemon_utils.py` | Shared PID utility functions |
| skynet_spam_guard.py | `tools/skynet_spam_guard.py` | Bus publish rate limiting + dedup |
| skynet_model_guard.py | `tools/skynet_model_guard.py` | UIA model correction (`fix_model()`) |
| uia_engine.py | `tools/uia_engine.py` | COM-based UI Automation scanner |
| Orch-Start.ps1 | `Orch-Start.ps1` | Boot script with `Start-VerifiedDaemon` |
| brain_config.json | `data/brain_config.json` | Daemon tunable parameters |
| realtime.json | `data/realtime.json` | SSE daemon output (live state) |
| bus_archive.jsonl | `data/bus_archive.jsonl` | Bus persist output (message archive) |
| worker_health.json | `data/worker_health.json` | Monitor output (health snapshots) |
| spam_log.json | `data/spam_log.json` | SpamGuard blocked message audit |

### Consultant Bridge Files

| File | Path | Port | Purpose |
|------|------|------|---------|
| skynet_consultant_bridge.py | `tools/skynet_consultant_bridge.py` | 8422 / 8425 | HTTP bridge server for consultant queues |
| CC-Start.ps1 | `CC-Start.ps1` | — | Codex Consultant bootstrap (port 8422) |
| GC-Start.ps1 | `GC-Start.ps1` | — | Gemini Consultant bootstrap (port 8425) |
| consultant_state.json | `data/consultant_state.json` | — | Codex state persistence |
| gemini_consultant_state.json | `data/gemini_consultant_state.json` | — | Gemini state persistence |

---

*Document generated by worker beta as part of Agile Sprint 1.*
*Level 3.5 update: 11 additional daemons documented, criticality matrix refreshed — delta.*
*All values sourced directly from daemon source files — no fabrication.*

<!-- signed: beta -->
<!-- signed: delta -->
