# Skynet Backend API Reference

> **Version:** 2.0.0 | **Port:** 8420 | **Backend:** Go  
> **Author:** Gamma (Wiring & Documentation Specialist) | signed: gamma  
> **Source:** `Skynet/server.go` (3213 lines)

Complete reference for all Skynet backend HTTP endpoints. Every request/response format is documented from the actual Go source code — no assumptions.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Authentication & RBAC](#authentication--rbac)
- [Rate Limiting](#rate-limiting)
- [System Endpoints](#system-endpoints)
  - [GET /status](#get-status)
  - [GET /health](#get-health)
  - [GET /metrics](#get-metrics)
  - [GET /stream (SSE)](#get-stream-sse)
  - [GET /activity/stream (SSE)](#get-activitystream-sse)
  - [GET /dashboard](#get-dashboard)
- [Directive & Dispatch](#directive--dispatch)
  - [POST /directive](#post-directive)
  - [POST /dispatch](#post-dispatch)
  - [GET /results](#get-results)
  - [POST /cancel](#post-cancel)
- [Worker Endpoints](#worker-endpoints)
  - [GET /worker/{name}/tasks](#get-workernametasks)
  - [POST /worker/{name}/result](#post-workernameresult)
  - [POST /worker/{name}/heartbeat](#post-workernameheartbeat)
  - [GET /worker/{name}/status](#get-workernamestatus)
  - [GET /worker/{name}/health](#get-workernamehealth)
  - [POST /worker/{name}/activity](#post-workernameactivity)
  - [GET /worker/{name}/activity](#get-workernameactivity)
- [Bus Communication](#bus-communication)
  - [POST /bus/publish](#post-buspublish)
  - [GET /bus/messages](#get-busmessages)
  - [POST /bus/clear](#post-busclear)
- [Task Queue](#task-queue)
  - [GET/POST /bus/tasks](#getpost-bustasks)
  - [POST /bus/tasks/claim](#post-bustasksclaim)
  - [POST /bus/tasks/complete](#post-bustaskscomplete)
- [Task Lifecycle Tracker](#task-lifecycle-tracker)
  - [GET /tasks](#get-tasks)
  - [POST /task/complete](#post-taskcomplete)
- [Orchestration](#orchestration)
  - [POST /orchestrate](#post-orchestrate)
  - [GET /orchestrate/status](#get-orchestratestatus)
  - [POST /orchestrate/pipeline](#post-orchestratepipeline)
- [Brain Interface](#brain-interface)
  - [GET /brain/pending](#get-brainpending)
  - [POST /brain/ack](#post-brainack)
  - [GET /god_feed](#get-god_feed)
- [Convene (Multi-Worker Consensus)](#convene-multi-worker-consensus)
  - [POST /bus/convene](#post-busconvene)
  - [GET /bus/convene](#get-busconvene)
  - [PATCH /bus/convene](#patch-busconvene)
  - [DELETE /bus/convene](#delete-busconvene)
- [Security](#security)
  - [GET /security/audit](#get-securityaudit)
  - [POST /security/blocked](#post-securityblocked)
- [WebSocket](#websocket)
  - [GET /ws (Upgrade)](#get-ws-upgrade)
  - [GET /ws/stats](#get-wsstats)
- [Spam Filtering](#spam-filtering)
- [Error Codes](#error-codes)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                Skynet Go Backend (port 8420)              │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  Ring Buffer  │  │  SSE Stream  │  │  Spam Filter  │  │
│  │ 100 messages  │  │  /stream 1Hz │  │ dedup + rate  │  │
│  │ FIFO eviction │  │  live state  │  │  10/min/sender│  │
│  └──────────────┘  └──────────────┘  └───────────────┘  │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Task Tracker  │  │   Workers    │  │   WebSocket   │  │
│  │  GET /tasks   │  │ alpha..delta │  │  50 max conns │  │
│  │  lifecycle    │  │  weighted LB │  │  RBAC + CSWSH │  │
│  └──────────────┘  └──────────────┘  └───────────────┘  │
│                                                          │
│  Middleware chain: rateLimitMiddleware → RBAC → logging   │
└──────────────────────────────────────────────────────────┘
```

**Key facts:**
- Bus ring buffer holds **100 messages** — FIFO eviction, no persistence
- SSE stream ticks at **1 Hz** with full system state
- Worker selection uses **weighted load** with round-robin tiebreaker
- All responses are `Content-Type: application/json` unless noted
- Localhost requests are exempt from IP-based rate limiting

---

## Authentication & RBAC

Three agent roles with descending privilege:

| Role | Header Value | Privilege Level |
|------|-------------|-----------------|
| `orchestrator` | `X-Agent-Role: orchestrator` | Full access (all endpoints) |
| `worker` | `X-Agent-Role: worker` | Task lifecycle + bus + stream |
| `consultant` | `X-Agent-Role: consultant` | Bus read/write + stream only |

**Header:** `X-Agent-Role`  
**Default (no header):** `orchestrator` (backward compatibility — Python tooling doesn't send the header yet)  
**Unrecognized header value:** HTTP 403 `RBAC: unknown role`

### Orchestrator-Only Endpoints
```
/directive, /dispatch, /cancel, /bus/clear,
/orchestrate, /orchestrate/status, /orchestrate/pipeline, /brain/ack
```

### Orchestrator + Worker Endpoints
```
/bus/tasks/claim, /bus/tasks/complete, /task/complete, /ws
```

### Open to All Roles (no ACL entry needed)
```
/status, /health, /metrics, /stream, /activity/stream,
/bus/publish, /bus/messages, /bus/tasks, /bus/convene,
/god_feed, /brain/pending, /results, /tasks,
/worker/{name}/*, /security/audit, /ws/stats, /dashboard
```

---

## Rate Limiting

**IP-based token bucket** applied to all non-localhost requests.

| Parameter | Value |
|-----------|-------|
| Bucket capacity | Configured in code |
| Refill rate | Per-second |
| Localhost exempt | Yes |
| Exceeded response | HTTP 429 `rate limit exceeded` |

**Bus-specific spam filter** (separate from IP rate limiting):

| Filter | Limit | Window |
|--------|-------|--------|
| Per-sender rate | 10 messages/minute | 60 seconds |
| Fingerprint dedup | Same `sender|topic|type|content[:200]` | 60 seconds |
| Cleanup interval | Stale entries removed | Every 5 minutes |

---

## System Endpoints

### GET /status

Returns full system state — workers, bus messages, orchestrator thoughts.

**Response:**
```json
{
  "agents": {
    "alpha": {
      "name": "alpha",
      "status": "IDLE",
      "tasks_completed": 42,
      "total_errors": 0,
      "avg_task_ms": 1500.5,
      "queue_depth": 0,
      "current_task": "",
      "last_heartbeat": "2026-03-20T10:00:00Z"
    }
  },
  "orch_feed": [
    {"type": "directive", "text": "New directive: ...", "timestamp": "..."}
  ],
  "bus": [ /* last 20 bus messages */ ],
  "uptime": 3600.5,
  "version": "2.0.0",
  "system": "skynet",
  "timestamp": "2026-03-20T10:00:00Z"
}
```

### GET /health

Lightweight liveness check. A worker is "alive" if its last heartbeat was within 120 seconds.

**Response:**
```json
{
  "status": "ok",
  "uptime": 3600.5,
  "workers": 4,
  "bus_depth": 37,
  "timestamp": 1710928800000000000,
  "timestamp_rfc": "2026-03-20T10:00:00Z"
}
```

### GET /metrics

Comprehensive system metrics including per-worker stats, directive stats, memory, and goroutines.

**Response:**
```json
{
  "uptime": 3600.5,
  "total_requests": 15000,
  "requests_per_sec": 4.2,
  "avg_latency_us": 250.0,
  "tasks_dispatched": 100,
  "tasks_completed": 95,
  "tasks_failed": 2,
  "task_throughput": 1.6,
  "bus_messages": 37,
  "bus_dropped": 0,
  "bus_overwrites": 12,
  "bus_capacity": 100,
  "worker_stats": {
    "alpha": {
      "tasks_completed": 30,
      "total_errors": 1,
      "avg_task_ms": 1200.0,
      "status": "IDLE",
      "timestamp": "2026-03-20T10:00:00Z"
    }
  },
  "directives": {
    "total": 50,
    "active": 2,
    "completed": 45,
    "pending": 3,
    "timestamp": "2026-03-20T10:00:00Z"
  },
  "goroutine_count": 24,
  "mem_alloc_mb": 15.3,
  "timestamp": "2026-03-20T10:00:00Z"
}
```

### GET /stream (SSE)

Server-Sent Events stream. Emits full system state every **1 second**. Used by `skynet_realtime.py` daemon.

**Headers:**
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
Access-Control-Allow-Origin: *
```

**Event format (every 1s):**
```
data: {"uptime_s":3600.5,"bus_depth":37,"bus_dropped":0,"bus_overwrites":12,"bus_capacity":100,"agents":{...},"bus":[...],"orch_thinking":[...],"tasks_dispatched":100,"tasks_completed":95,"tasks_failed":2,"goroutines":24,"timestamp":1710928800000000000}
```

### GET /activity/stream (SSE)

Lightweight SSE that streams only worker activity every **2 seconds**. Lower overhead than `/stream`.

**Event format (every 2s):**
```
data: {"workers":{"alpha":{"state":"IDLE","current_task":""},"beta":{"state":"PROCESSING","current_task":"Fix dashboard"}},"timestamp":1710928800000000000}
```

### GET /dashboard

Returns the embedded HTML dashboard page. Content-Type: `text/html; charset=utf-8`.

---

## Directive & Dispatch

### POST /directive

Create a directive (high-level goal) and optionally route it to a worker.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "goal": "Audit security module",
  "directive": "Check all imports in core/security.py",
  "priority": 3,
  "route": "alpha",
  "type": "copilot"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `goal` | string | **Yes** | — | High-level objective |
| `directive` | string | No | `goal` | Specific instruction (used as task command if routed) |
| `priority` | int | No | 3 | 1-10 (clamped to 3 if out of range) |
| `route` | string | No | — | Worker name to route to. If empty, auto-completes |
| `type` | string | No | `copilot` | Task type. `copilot` = 1 retry max |

**Response:**
```json
{
  "status": "ok",
  "directive_id": "dir_1710928800000000000",
  "goal": "Audit security module",
  "priority": 3
}
```

**Side effects:**
- Writes to `data/brain/god_feed.json`
- Writes to `data/brain/brain_inbox.json`
- Posts bus message: `{sender:"skynet", topic:"directives", type:"new"}`
- If `route` specified: enqueues task on worker, tracks in task tracker
- If no `route`: directive auto-completed immediately

### POST /dispatch

Dispatch a task to a specific worker or auto-select via weighted load balancing.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "worker": "beta",
  "directive": "Run pytest on core/ modules",
  "task_id": "task_abc123"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `worker` | string | No | Target worker. If empty, auto-selected via weighted load |
| `directive` | string | **Yes** | Task instruction text |
| `task_id` | string | **Yes** | Unique task identifier |

**Response:**
```json
{
  "status": "ok",
  "task_id": "task_abc123",
  "worker": "beta"
}
```

**Auto-selection:** If `worker` is empty, `selectWorker()` picks the worker with lowest weighted load, using a round-robin counter as tiebreaker.

### GET /results

Returns task result history (last N entries).

**Query params:**
- `n` — Number of results to return (default: 50)

**Response:**
```json
[
  {
    "task_id": "task_abc123",
    "status": "success",
    "output": "All tests passed",
    "worker_name": "beta",
    "directive_id": "dir_...",
    "finished_at": "2026-03-20T10:05:00Z"
  }
]
```

### POST /cancel

Cancel a pending task.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "task_id": "task_abc123"
}
```

**Response:**
```json
{
  "status": "ok",
  "task_id": "task_abc123",
  "action": "cancelled"
}
```

**Error:** HTTP 404 if task not found or not in `pending` status.

---

## Worker Endpoints

All worker endpoints use the pattern `/worker/{name}/action` where `{name}` is the lowercase worker name (e.g., `alpha`, `beta`, `gamma`, `delta`).

### GET /worker/{name}/tasks

Returns pending tasks assigned to this worker.

**Response:**
```json
[
  {
    "id": "task_abc123",
    "directive": "Run pytest on core/ modules",
    "status": "pending",
    "assigned_at": "2026-03-20T10:00:00Z"
  }
]
```

### POST /worker/{name}/result

Submit a completed task result.

**Request:**
```json
{
  "task_id": "task_abc123",
  "result": "All 15 tests passed successfully",
  "status": "success"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `task_id` | string | **Yes** | — | Task to complete |
| `result` | string | No | — | Result text |
| `status` | string | No | `success` | `success` or `failed` |

**Response:**
```json
{
  "status": "ok",
  "task_id": "task_abc123",
  "worker": "beta"
}
```

**Side effects:**
- Increments `tasks_completed` or `tasks_failed` atomic counter
- Stores in result history (capped at 500)
- Checks directive completion (auto-completes directive if all subtasks done)
- Broadcasts worker update to WebSocket clients

**Error:** HTTP 404 if task not found for this worker.

### POST /worker/{name}/heartbeat

External health monitor reports worker window liveness. Called by `skynet_monitor.py`.

**Request:**
```json
{
  "hwnd_alive": true,
  "visible": true,
  "model": "Claude Opus 4.6 fast",
  "grid_slot": "top-left",
  "state": "IDLE"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hwnd_alive` | bool | No | Window handle still valid |
| `visible` | bool | No | Window is visible |
| `model` | string | No | Current AI model |
| `grid_slot` | string | No | Position in grid layout |
| `state` | string | No | Worker state: `IDLE`, `PROCESSING`, `STEERING` |

**Response:**
```json
{
  "status": "ok",
  "worker": "alpha",
  "hwnd_alive": true
}
```

**Side effects:**
- Bumps worker's `lastHeartbeat` and `lastExtHB` timestamps
- Updates worker `Status` and `model` if provided
- **Auto-claim:** When state transitions to `IDLE` from non-IDLE, auto-claims next highest-priority pending task from the task queue
- **DEAD alert:** If `hwnd_alive=false`, posts CRITICAL alert to bus

### GET /worker/{name}/status

Returns worker status with pending and running task details.

**Response:**
```json
{
  "worker": "alpha",
  "alive": true,
  "last_heartbeat": "2026-03-20T10:00:00Z",
  "pending_tasks": 2,
  "running_tasks": 1,
  "tasks": [
    {"id": "task_1", "directive": "Fix bug in...", "status": "running"},
    {"id": "task_2", "directive": "Add tests...", "status": "pending"}
  ]
}
```

### GET /worker/{name}/health

Returns circuit breaker state and detailed health for a worker.

**Response:**
```json
{
  "worker": "alpha",
  "healthy": true,
  "circuit_breaker": {
    "state": "CLOSED",
    "consecutive_fails": 0,
    "fail_threshold": 3,
    "cooldown_sec": 30,
    "opened_at": "0001-01-01T00:00:00Z"
  },
  "alive": true,
  "last_heartbeat": "2026-03-20T10:00:00Z",
  "tasks_completed": 42,
  "total_errors": 1,
  "queue_depth": 0,
  "uptime": 3600.5
}
```

**Circuit breaker states:** `CLOSED` (normal), `CIRCUIT_OPEN` (failing, tasks rerouted), `HALF_OPEN` (testing recovery)

### POST /worker/{name}/activity

Log worker activity for real-time monitoring.

**Request:**
```json
{
  "current_task": "Fixing dashboard CSS",
  "activity_type": "code_edit",
  "detail": "Modified god_console.html line 450"
}
```

**Response:**
```json
{
  "status": "ok",
  "worker": "alpha"
}
```

**Side effects:** Broadcasts to WebSocket clients, bumps heartbeat.

### GET /worker/{name}/activity

Returns recent activity log and performance stats.

**Response:**
```json
{
  "name": "alpha",
  "state": "PROCESSING",
  "current_task": "Fixing dashboard CSS",
  "recent_logs": [
    "[10:01:05] code_edit: Modified line 450",
    "[10:01:12] test_run: pytest core/test_x.py"
  ],
  "tasks_completed": 42,
  "avg_task_ms": 1500.5,
  "last_heartbeat": "2026-03-20T10:01:12Z"
}
```

---

## Bus Communication

### POST /bus/publish

Publish a message to the bus ring buffer. **All agents must use `guarded_publish()` from `tools/skynet_spam_guard.py`** — raw `requests.post` is forbidden (-1.0 score penalty).

**Request:**
```json
{
  "sender": "alpha",
  "topic": "orchestrator",
  "type": "result",
  "content": "RESULT: All tests passed signed:alpha",
  "metadata": {
    "task_id": "task_123",
    "duration_ms": "1500"
  }
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `sender` | string | **Yes** | — | Agent name |
| `topic` | string | No | `general` | Message topic for filtering |
| `type` | string | No | `message` | Message type |
| `content` | string | **Yes** | — | Message body |
| `metadata` | object | No | — | Key-value string pairs |

**Response:**
```json
{
  "status": "published",
  "sender": "alpha",
  "topic": "orchestrator",
  "bus_depth": 38
}
```

**Spam filter:** Messages are checked against the server-side spam filter:
- **Rate limit:** 10 messages/minute per sender → HTTP 429 `SPAM_BLOCKED`
- **Dedup:** Same `sender|topic|type|content[:200]` within 60s → HTTP 429 `SPAM_BLOCKED`

**Side effects:**
- Broadcasts to all WebSocket clients
- If `type=result`: auto-completes matching task tracker entry for the sender

### GET /bus/messages

Read recent bus messages with optional filtering.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max messages to return (capped at 100) |
| `sender` | string | — | Filter by sender name |
| `topic` | string | — | Filter by topic |

**Example:** `GET /bus/messages?limit=30&topic=orchestrator`

**Response:**
```json
[
  {
    "sender": "alpha",
    "topic": "orchestrator",
    "type": "result",
    "content": "RESULT: Task completed signed:alpha",
    "metadata": {"task_id": "task_123"},
    "timestamp": "2026-03-20T10:00:00Z"
  }
]
```

### POST /bus/clear

Clear all messages from the bus ring buffer.

**RBAC:** Orchestrator only

**Response:**
```json
{
  "status": "ok",
  "cleared": 37
}
```

---

## Task Queue

Server-managed task queue with claim/complete lifecycle. Separate from the directive/dispatch system — this is for worker-initiated task pulling.

### GET/POST /bus/tasks

**GET** — Returns tasks from the queue.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `all` | string | `false` | If `true`, returns all tasks including completed/failed |

**Response (GET):**
```json
[
  {
    "id": "tq_1",
    "task": "Review auth module",
    "priority": 5,
    "source": "orchestrator",
    "status": "pending",
    "created_at": "2026-03-20T10:00:00Z",
    "claimed_by": "",
    "claimed_at": null,
    "result": "",
    "done_at": null
  }
]
```

**POST** — Add a new task to the queue.

**Request:**
```json
{
  "task": "Review auth module for security issues",
  "priority": 5,
  "source": "orchestrator"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `task` | string | **Yes** | — | Task description |
| `priority` | int | No | 0 | Higher = more important |
| `source` | string | No | `anonymous` | Who created the task |

**Response (POST):**
```json
{
  "status": "queued",
  "task_id": "tq_1"
}
```

**Queue cap:** 200 tasks. When exceeded, completed/failed tasks are evicted first.

### POST /bus/tasks/claim

Claim a pending task for execution.

**RBAC:** Orchestrator + Worker

**Request:**
```json
{
  "task_id": "tq_1",
  "worker": "alpha"
}
```

**Response:**
```json
{
  "status": "claimed",
  "task_id": "tq_1"
}
```

**Error:** HTTP 409 if task not found or already claimed.

### POST /bus/tasks/complete

Mark a claimed task as completed or failed.

**RBAC:** Orchestrator + Worker

**Request:**
```json
{
  "task_id": "tq_1",
  "worker": "alpha",
  "result": "Found 3 auth issues, all fixed",
  "status": "completed"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `task_id` | string | **Yes** | — | Task to complete |
| `worker` | string | **Yes** | — | Must match claiming worker |
| `result` | string | No | — | Result text |
| `status` | string | No | `completed` | `completed` or `failed` |

**Response:**
```json
{
  "status": "completed",
  "task_id": "tq_1"
}
```

**Side effects:**
- Posts completion to bus
- **Auto-claims** next highest-priority pending task for this worker

**Error:** HTTP 409 if task not found or not claimed by this worker.

---

## Task Lifecycle Tracker

Separate from the task queue — this tracks tasks dispatched via `/directive` and correlates completions from bus `type=result` messages.

### GET /tasks

Returns tracked task lifecycle entries.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `worker` | string | — | Filter by worker name |
| `limit` | int | 100 | Max entries (capped at 500) |

**Response:**
```json
{
  "tasks": [
    {
      "task_id": "task_123",
      "worker": "alpha",
      "goal": "Audit security module",
      "dispatched_at": "2026-03-20T10:00:00Z",
      "status": "completed",
      "directive_id": "dir_123",
      "completed_at": "2026-03-20T10:02:00Z",
      "duration_ms": 120000
    }
  ],
  "total": 42,
  "stats": {
    "dispatched": 5,
    "completed": 35,
    "failed": 1,
    "timeout": 1,
    "processing": 0
  }
}
```

### POST /task/complete

Simple task completion counter. Increments the global `tasks_completed` atomic counter.

**RBAC:** Orchestrator + Worker

**Request:** Any POST body (ignored).

**Response:**
```json
{"ok": true}
```

---

## Orchestration

### POST /orchestrate

High-level orchestration endpoint. Creates a directive and optionally auto-dispatches to all workers.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "prompt": "Audit all security modules and fix any issues",
  "timeout": 120,
  "auto_dispatch": true
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `prompt` | string | **Yes** | — | High-level goal |
| `timeout` | int | No | 120 | Timeout in seconds |
| `auto_dispatch` | bool | No | `false` | If `true`, dispatches to ALL workers simultaneously |

**Response:**
```json
{
  "status": "ok",
  "directive_id": "dir_...",
  "prompt": "Audit all security modules...",
  "workers_assigned": ["alpha", "beta", "gamma", "delta"]
}
```

**Behavior:**
- `auto_dispatch=true`: Creates a task for EVERY worker with the same prompt
- `auto_dispatch=false`: Selects ONE worker via weighted load balancing

### GET /orchestrate/status

Check directive progress with sub-task completion details.

**RBAC:** Orchestrator only

**Query params:**
- `directive_id` — **Required.** Directive ID to check.

**Response:**
```json
{
  "directive_id": "dir_...",
  "goal": "Audit all security modules",
  "status": "active",
  "priority": 1,
  "created_at": "2026-03-20T10:00:00Z",
  "completed_at": "0001-01-01T00:00:00Z",
  "subtasks": [
    {
      "task_id": "task_1_alpha",
      "worker": "alpha",
      "status": "completed",
      "assigned_at": "2026-03-20T10:00:00Z",
      "completed_at": "2026-03-20T10:02:00Z",
      "result": "Found 3 issues in core/security.py..."
    }
  ],
  "summary": {
    "total": 4,
    "pending": 1,
    "completed": 2,
    "failed": 1
  }
}
```

### POST /orchestrate/pipeline

Create a multi-step sequential pipeline. Step 1 dispatches immediately; subsequent steps have dependency chaining.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "steps": [
    {"name": "analyze", "prompt": "Scan core/ for security issues"},
    {"name": "fix", "prompt": "Fix all issues found in step 1"},
    {"name": "test", "prompt": "Run full test suite to verify fixes"}
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `steps` | array | **Yes** | Non-empty array of pipeline steps |
| `steps[].name` | string | No | Step label (auto-generated as `step_N` if empty) |
| `steps[].prompt` | string | **Yes** | Instruction for this step |

**Response:**
```json
{
  "pipeline_id": "pipe_...",
  "status": "running",
  "total_steps": 3,
  "steps": [
    {
      "name": "analyze",
      "prompt": "Scan core/ for security issues",
      "directive_id": "pipe_..._dir_0",
      "task_id": "pipe_..._task_0_alpha",
      "worker": "alpha",
      "status": "dispatched",
      "depends_on": ""
    },
    {
      "name": "fix",
      "prompt": "Fix all issues found...",
      "directive_id": "pipe_..._dir_1",
      "task_id": "pipe_..._task_1_beta",
      "worker": "beta",
      "status": "pending",
      "depends_on": "pipe_..._dir_0"
    }
  ]
}
```

---

## Brain Interface

### GET /brain/pending

Returns pending (unacknowledged) items from `data/brain/brain_inbox.json`.

**Response:**
```json
[
  {
    "request_id": "dir_123",
    "goal": "Audit security module",
    "status": "pending",
    "created_at": 1710928800.0
  }
]
```

### POST /brain/ack

Acknowledge a brain inbox item, marking it as completed.

**RBAC:** Orchestrator only

**Request:**
```json
{
  "request_id": "dir_123"
}
```

**Response:**
```json
{
  "status": "ok",
  "request_id": "dir_123",
  "marked": "completed"
}
```

**Error:** HTTP 404 if `request_id` not found.

### GET /god_feed

Returns the GOD Console feed from `data/brain/god_feed.json`.

**Response:** JSON array of feed entries (format depends on what's been written).

---

## Convene (Multi-Worker Consensus)

Multi-worker consensus sessions for collaborative decision-making.

### POST /bus/convene

Create a new convene session.

**Request:**
```json
{
  "initiator": "alpha",
  "topic": "Security architecture review",
  "context": "Need to decide on auth strategy for external workers",
  "need_workers": 3
}
```

**Response:**
```json
{
  "status": "ok",
  "session_id": "conv_..."
}
```

**Side effect:** Posts convene request to bus so watchers can discover and join.

**Cap:** 50 sessions maximum (oldest evicted).

### GET /bus/convene

List all convene sessions.

**Response:**
```json
[
  {
    "id": "conv_...",
    "initiator": "alpha",
    "topic": "Security architecture review",
    "context": "Need to decide on...",
    "need_workers": 3,
    "participants": ["alpha", "beta"],
    "messages": [],
    "created_at": "2026-03-20T10:00:00Z",
    "status": "active",
    "status_changed_at": "2026-03-20T10:00:00Z"
  }
]
```

### PATCH /bus/convene

Join an existing session.

**Request:**
```json
{
  "session_id": "conv_...",
  "worker": "gamma"
}
```

**Response:**
```json
{
  "status": "joined",
  "session_id": "conv_..."
}
```

### DELETE /bus/convene

Resolve/close a session.

**Query params:**
- `id` — **Required.** Session ID.

**Example:** `DELETE /bus/convene?id=conv_123`

**Response:**
```json
{
  "status": "resolved",
  "session_id": "conv_123"
}
```

---

## Security

### GET /security/audit

Returns the security event log.

**Response:**
```json
{
  "total_events": 15,
  "blocked_count": 3,
  "events": [
    {
      "timestamp": "2026-03-20T10:00:00Z",
      "source": "unknown-origin.com",
      "type": "ws_cswsh_blocked",
      "details": "WebSocket upgrade rejected: disallowed Origin",
      "blocked": true
    }
  ],
  "uptime_s": 3600.5
}
```

### POST /security/blocked

Log a security event (identity injection attempt, etc.).

**Request:**
```json
{
  "source": "external_worker",
  "reason": "Attempted to modify core config",
  "text": "Remove-Item data/workers.json..."
}
```

**Response:**
```json
{
  "status": "logged"
}
```

---

## WebSocket

### GET /ws (Upgrade)

WebSocket upgrade endpoint. Security-hardened with CSWSH protection, RBAC, frame limits, and connection caps.

**Requirements:**
- `X-Agent-Role` header **required** (unlike HTTP endpoints — no backward-compat default)
- Only `orchestrator` and `worker` roles allowed
- Origin must be localhost variant or empty

**Limits:**

| Parameter | Value |
|-----------|-------|
| Max connections | 50 |
| Max frame size | 1 MB |
| Ping interval | 30 seconds |
| Idle timeout | 5 minutes |
| Write timeout | 10 seconds |
| Read timeout | 60 seconds |

**Message types received via WebSocket:**
```json
{"type": "bus_message", "sender": "...", "topic": "...", "msg_type": "...", "content": "...", "timestamp": "..."}
{"type": "worker_update", "worker": "...", "task_id": "...", "status": "...", "directive_id": "...", "timestamp": "..."}
{"type": "worker_activity", "worker": "...", "current_task": "...", "activity_type": "...", "detail": "...", "timestamp": "..."}
{"type": "security_alert", "event": {...}, "timestamp": "..."}
```

### GET /ws/stats

WebSocket connection statistics.

**Response:**
```json
{
  "connected_clients": 2,
  "max_connections": 50,
  "total_broadcasts": 1500,
  "total_rejected": 3,
  "active_connections": 2
}
```

---

## Spam Filtering

The bus has **two layers** of spam protection:

### Layer 1: Client-Side — `tools/skynet_spam_guard.py`

| Filter | Window | Penalty |
|--------|--------|---------|
| Content fingerprint (SHA-256) | 900s | -0.1 score |
| Per-sender rate | 5/min, 30/hour | -0.1 per excess |
| Duplicate bus messages | 900s | -0.1 |
| DEAD alerts (same worker) | 120s | -0.1 |
| daemon_health | 1/60s per daemon | -0.1 |
| Knowledge/learning (same fact) | 1800s | -0.1 |
| **SpamGuard bypass** | — | **-1.0 score** |

### Layer 2: Server-Side — `Skynet/server.go` SpamFilter

| Filter | Limit | Response |
|--------|-------|----------|
| Per-sender rate | 10 msgs/min | HTTP 429 `SPAM_BLOCKED` |
| Fingerprint dedup | `sender|topic|type|content[:200]` within 60s | HTTP 429 `SPAM_BLOCKED` |
| Cleanup | Stale entries removed every 5 min | — |

**Rule:** Always use `guarded_publish()`. Raw `requests.post` to `/bus/publish` is forbidden.

---

## Error Codes

| HTTP Code | Meaning | Common Causes |
|-----------|---------|---------------|
| 400 | Bad Request | Missing required field, malformed JSON |
| 403 | Forbidden | RBAC violation, unknown agent role, WebSocket origin blocked |
| 404 | Not Found | Worker/task/directive/session not found |
| 405 | Method Not Allowed | Wrong HTTP method for endpoint |
| 409 | Conflict | Task already claimed, task not owned by worker |
| 429 | Too Many Requests | IP rate limit or spam filter blocked |
| 503 | Service Unavailable | No workers available, WebSocket connection limit reached |

---

## Usage Examples

### Python — Post to bus (correct way)
```python
from tools.skynet_spam_guard import guarded_publish

guarded_publish({
    "sender": "gamma",
    "topic": "orchestrator",
    "type": "result",
    "content": "RESULT: API docs created signed:gamma"
})
```

### PowerShell — Read bus messages
```powershell
Invoke-RestMethod http://localhost:8420/bus/messages?limit=30
```

### PowerShell — Check worker status
```powershell
Invoke-RestMethod http://localhost:8420/worker/alpha/status
```

### PowerShell — Dispatch directive to worker
```powershell
Invoke-RestMethod -Uri http://localhost:8420/directive -Method POST `
  -ContentType 'application/json' `
  -Body (ConvertTo-Json @{
    goal="Fix dashboard CSS"
    route="alpha"
    priority=5
  })
```

### Python — Full orchestration pipeline
```python
import requests

# Create pipeline
resp = requests.post("http://localhost:8420/orchestrate/pipeline", json={
    "steps": [
        {"name": "scan", "prompt": "Find all security issues"},
        {"name": "fix", "prompt": "Fix all issues found"},
        {"name": "test", "prompt": "Run test suite"}
    ]
})
pipeline = resp.json()

# Check progress
status = requests.get(
    f"http://localhost:8420/orchestrate/status?directive_id={pipeline['steps'][0]['directive_id']}"
).json()
```

---

*This document was generated from `Skynet/server.go` source code analysis. All request/response formats are verified from actual Go struct definitions and handler implementations. signed: gamma*
