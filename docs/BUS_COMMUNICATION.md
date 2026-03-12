# Skynet Bus Communication Architecture
<!-- signed: gamma -->

> **Definitive reference** for the Skynet message bus, communication protocols, and all
> related subsystems. This document covers the complete message lifecycle from publisher
> to consumer, including all intermediary layers, filtering, governance, and persistence.
>
> Last updated: 2026-03-12 | Author: gamma (worker)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Go Backend Bus Implementation](#3-go-backend-bus-implementation)
4. [Message Schema](#4-message-schema)
5. [Topic Taxonomy](#5-topic-taxonomy)
6. [Python SpamGuard](#6-python-spamguard)
7. [ConveneGate Governance](#7-convenegate-governance)
8. [Knowledge Broadcasting](#8-knowledge-broadcasting)
9. [Bus Persistence](#9-bus-persistence)
10. [Message Loss Risk Matrix](#10-message-loss-risk-matrix)
11. [Worker State Communication](#11-worker-state-communication)
12. [Configuration Reference](#12-configuration-reference)
13. [Related Files](#13-related-files)

---

## 1. Overview

The Skynet message bus is a centralized, in-memory publish-subscribe system that serves as the
**sole communication backbone** for the entire multi-agent network. It connects the orchestrator,
four workers (alpha, beta, gamma, delta), two consultants (Codex, Gemini), and all daemon
processes into a unified communication plane.

### What It Is

A Go-based HTTP server (`Skynet/server.go`) running on port 8420 that provides:

- **Publish/subscribe messaging** via REST API (`/bus/publish`, `/bus/messages`)
- **Real-time streaming** via Server-Sent Events (`/stream`, 1Hz ticks)
- **WebSocket broadcast** (`/ws` endpoint for dashboard and monitors)
- **Task lifecycle management** (`/bus/tasks`, `/bus/tasks/claim`, `/bus/tasks/complete`)
- **Convene sessions** for multi-worker consensus (`/bus/convene`)
- **Worker state tracking** (`/status`, `/worker/{name}/heartbeat`)

### What Problems It Solves

| Problem | Bus Solution |
|---------|-------------|
| Workers cannot talk directly to each other | Bus provides topic-based pub/sub |
| Orchestrator needs to know all worker states | `/status` endpoint aggregates heartbeats |
| Results must survive worker session resets | Bus ring buffer holds last 100 messages |
| Spam can flood the system during burst activity | Dual spam filtering (Python + Go) |
| Workers need consensus before escalation | ConveneGate governance via bus voting |
| Knowledge must compound across the network | Knowledge broadcasting via bus topics |
| Dashboard needs live state | SSE stream pushes state every second |

### Design Principles

1. **Fire-and-forget publishing** — Publishers POST and move on; no ACK required
2. **Topic-based routing** — Subscribers filter by topic; wildcard subscriptions available
3. **Non-blocking fanout** — Slow consumers get messages dropped, never block publishers
4. **Dual spam protection** — Python client-side pre-filtering + Go server-side enforcement
5. **Bus persistence as optional layer** — SSE subscriber daemon archives to disk separately

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SKYNET MESSAGE BUS ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  PUBLISHERS                    GO BACKEND (port 8420)          CONSUMERS    │
│  ─────────                     ──────────────────────          ─────────    │
│                                                                             │
│  ┌─────────────┐    POST       ┌─────────────────┐                         │
│  │ Orchestrator │──────────────▶│   SpamFilter     │                         │
│  └─────────────┘    /bus/      │  (10/min/sender) │                         │
│  ┌─────────────┐   publish     │  (60s dedup)     │                         │
│  │  Workers     │──────────────▶│                   │                         │
│  │ α β γ δ     │               │  HTTP 429 if spam │                         │
│  └─────────────┘               └────────┬──────────┘                         │
│  ┌─────────────┐                        │ (allowed)                          │
│  │ Consultants  │                        ▼                                   │
│  │ Codex/Gemini│               ┌─────────────────┐         ┌──────────┐    │
│  └─────────────┘               │   Ring Buffer     │ ──────▶│  /bus/    │    │
│  ┌─────────────┐               │   (100 msgs FIFO) │  GET   │ messages │    │
│  │   Daemons    │               │   No persistence  │        └──────────┘    │
│  │ monitor,    │               └────────┬──────────┘                         │
│  │ watchdog,   │                        │                                    │
│  │ overseer    │                        ▼                                    │
│  └─────────────┘               ┌─────────────────┐         ┌──────────┐    │
│                                │  Topic Subscribers │ ──────▶│ Daemons  │    │
│                                │  (64-msg channels) │        │ Monitors │    │
│  PYTHON SPAMGUARD              │  Non-blocking send │        └──────────┘    │
│  ────────────────              └────────┬──────────┘                         │
│  ┌─────────────┐                        │                                    │
│  │ guarded_    │                        ▼                                    │
│  │ publish()   │               ┌─────────────────┐         ┌──────────┐    │
│  │ 5/min sender│               │   SSE Stream      │ ──────▶│ Dashboard│    │
│  │ 900s dedup  │               │   /stream (1Hz)   │  text/  │ Realtime │    │
│  │ SHA-256 fp  │               │   Last 10 msgs    │ event  │ Daemon   │    │
│  │ Category    │               └────────┬──────────┘ stream │ Persist  │    │
│  │ windows     │                        │                    └──────────┘    │
│  └─────────────┘                        ▼                                    │
│        │                       ┌─────────────────┐         ┌──────────┐    │
│  Pre-filters before            │   WebSocket       │ ──────▶│ GOD      │    │
│  HTTP POST to Go               │   /ws (broadcast)  │        │ Console  │    │
│                                │   64-msg channels  │        │ WS Mon   │    │
│                                └─────────────────┘         └──────────┘    │
│                                                                             │
│  GOVERNANCE                    PERSISTENCE                                  │
│  ──────────                    ───────────                                   │
│  ┌─────────────┐               ┌─────────────────┐                         │
│  │ ConveneGate  │               │ bus_persist.py    │                         │
│  │ Propose→Vote │               │ SSE → JSONL       │                         │
│  │ 2-vote gate  │               │ data/bus_archive  │                         │
│  │ 30min digest │               │ .jsonl            │                         │
│  └─────────────┘               │ 50MB rotation     │                         │
│                                └─────────────────┘                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Go Backend Bus Implementation

### 3.1 Ring Buffer

The bus stores messages in a **fixed-size ring buffer** with zero allocation after initialization.

| Parameter | Value | Source |
|-----------|-------|--------|
| Capacity | 100 messages | `bus.go` L33: `const ringSize = 100` |
| Allocation | Fixed array `[ringSize]BusMessage` | Zero allocation after init |
| Eviction | FIFO — oldest message overwritten when full | Head pointer wraps around |
| Persistence | **None** — crash = total message loss | By design; persistence is a separate layer |
| Thread safety | `sync.RWMutex` | Read lock for queries, write lock for posts |
| Counters | `totalMsg` (int64), `dropped` (int64) | Atomic — no lock on hot path |

**Overflow behavior:** When the 101st message arrives, message #1 is silently overwritten.
There is no notification, no archival, no warning. The ring buffer is designed for recency,
not completeness.

### 3.2 Subscriber Channels

The bus supports two subscription modes:

| Mode | Registration | Buffer Size | Use Case |
|------|-------------|-------------|----------|
| Topic-specific | `bus.Subscribe(topic, subscriberID)` | 64 messages | Filtered listening |
| Wildcard | `bus.SubscribeAll(subscriberID)` | 64 messages | SSE stream, monitors |

**Slow subscriber behavior:** Messages are sent via `select` with a `default` case.
If a subscriber's channel is full (64 pending messages), the message is **silently dropped**
and the `dropped` counter is atomically incremented. The publisher is never blocked.

```go
select {
case ch <- msg:
    // delivered
default:
    atomic.AddInt64(&b.dropped, 1)
    // dropped — subscriber too slow
}
```

**Self-exclusion:** Subscribers never receive their own messages (filtered by sender ID).

### 3.3 SSE Stream (`/stream`)

| Parameter | Value |
|-----------|-------|
| Endpoint | `GET /stream` |
| Content-Type | `text/event-stream` |
| Tick interval | 1 second |
| Bus messages included | Last 10 |

**Payload fields per tick:**

| Field | Type | Description |
|-------|------|-------------|
| `uptime_s` | float64 | Server uptime in seconds |
| `bus_depth` | int | Current messages in ring buffer |
| `bus_dropped` | int64 | Total dropped messages (slow subscribers) |
| `agents` | []AgentView | Worker states with heartbeat timestamps |
| `bus` | []BusMessage | Last 10 bus messages |
| `orch_thinking` | []string | Orchestrator thought log |
| `tasks_dispatched` | int64 | Atomic counter — dispatched tasks |
| `tasks_completed` | int64 | Atomic counter — completed tasks |
| `tasks_failed` | int64 | Atomic counter — failed tasks |
| `goroutines` | int | Go runtime goroutine count |
| `timestamp` | int64 | Unix nanosecond timestamp |

### 3.4 WebSocket (`/ws`)

| Parameter | Value |
|-----------|-------|
| Endpoint | `GET /ws` (HTTP upgrade) |
| Channel buffer | 64 messages per client |
| Handshake | Raw HTTP hijack (no external deps) |
| Broadcast trigger | Every `/bus/publish` POST |

WebSocket clients receive a JSON frame for every bus message immediately after it's posted.
Slow WebSocket clients get messages dropped (same `select/default` pattern as bus subscribers).

**Broadcast payload:**

```json
{
  "type": "bus_message",
  "sender": "alpha",
  "topic": "orchestrator",
  "msg_type": "result",
  "content": "task completed",
  "timestamp": "2026-03-12T05:00:00Z"
}
```

### 3.5 Server-Side Spam Filtering

The Go backend enforces its own spam filter **independently** of the Python SpamGuard.

| Parameter | Value | Details |
|-----------|-------|---------|
| Rate limit | 10 msgs/min/sender | Sliding 1-minute window |
| Dedup window | 60 seconds | Fingerprint: `sender\|topic\|type\|content[:200]` |
| HTTP status on block | 429 Too Many Requests | Body: `SPAM_BLOCKED: <reason>` |
| Cleanup interval | 5 minutes | Background goroutine prunes stale entries |
| Content fingerprint | First 200 characters | **No normalization** (unlike Python SpamGuard) |

**Key difference from Python SpamGuard:** The Go filter uses raw content for fingerprinting
(no timestamp/UUID stripping), so messages that are identical except for stripped fields will
pass Go but be caught by Python.

---

## 4. Message Schema

Every bus message conforms to this schema (defined as `BusMessage` in `types.go`):

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | Auto | `msg_{seq}_{sender}` | Unique ID, auto-generated by backend |
| `sender` | string | **Yes** | — | Message originator (e.g., "alpha", "orchestrator") |
| `topic` | string | No | `"general"` | Routing topic for subscriber filtering |
| `type` | string | No | `"message"` | Message type/category within topic |
| `content` | string | **Yes** | — | Main message payload (free-form text) |
| `metadata` | map[string]string | No | `null` | Optional key-value pairs |
| `timestamp` | time.Time | Auto | `time.Now()` | Server-assigned creation timestamp |

### ID Format

Message IDs follow the pattern `msg_{sequence}_{sender}` where `sequence` is an
atomically incrementing int64. Example: `msg_42_alpha`.

### Content Conventions

While `content` is free-form, these conventions are observed:

- **Results:** Include `signed:worker_name` for accountability
- **Alerts:** Prefix with alert type (e.g., `MODEL_DRIFT: ...`, `WORKER_DEAD: ...`)
- **Proposals:** JSON-structured content with `plan`, `reasoning`, `impact` fields
- **Knowledge:** JSON with `fact`, `category`, `tags`, `learned_at` fields

---

## 5. Topic Taxonomy

### Core Topics

| Topic | Description | Primary Publishers | Primary Consumers |
|-------|-------------|-------------------|-------------------|
| `orchestrator` | Worker→orchestrator results, alerts, status | Workers, daemons | Orchestrator |
| `convene` | Multi-worker consensus proposals and votes | Workers via ConveneGate | All workers |
| `knowledge` | Fact sharing and validation | Workers | All workers, LearningStore |
| `planning` | Consultant proposals, architecture plans | Consultants | Orchestrator |
| `scoring` | Score adjustments (awards, deductions) | Workers, orchestrator | Scoring system |
| `workers` | Inter-worker requests, sub-delegation | Workers | Workers |
| `system` | Infrastructure events, boot announcements | System, orchestrator | All |
| `consultant` | Consultant prompts and responses | Orchestrator, consultants | Consultants |
| `tasks` | Task queue events (queued, claimed, completed) | Backend, workers | Orchestrator |
| `general` | Default topic for unclassified messages | Any | Any |

### Type Subtypes by Topic

#### `topic=orchestrator`

| Type | Description | Example |
|------|-------------|---------|
| `result` | Task completion report | Worker posting DONE |
| `alert` | System alert requiring attention | `MODEL_DRIFT`, `WORKER_DEAD` |
| `identity_ack` | Identity announcement on boot | Orchestrator/consultant online |
| `status` | Periodic status update | Worker state summary |
| `urgent` | ConveneGate bypass — critical alert | System-critical emergencies |

#### `topic=convene`

| Type | Description |
|------|-------------|
| `request` | New convene session request |
| `join` | Worker joining existing session |
| `finding` | Substantive finding during convene |
| `resolve` | Session resolution by initiator |
| `gate-proposal` | ConveneGate proposal for voting |
| `gate-vote` | Worker vote on pending gate proposal |
| `gate-elevated-queued` | Finding elevated and queued for digest |

#### `topic=knowledge`

| Type | Description |
|------|-------------|
| `learning` | New fact learned (broadcast_learning) |
| `validation` | Vote on existing fact (validate_fact) |
| `strategy` | Evolution strategy sharing |
| `incident` | Incident report |

#### `topic=scoring`

| Type | Description |
|------|-------------|
| `award` | Points awarded to a worker |
| `deduction` | Points deducted from a worker |

---

## 6. Python SpamGuard

**File:** `tools/skynet_spam_guard.py`

The Python SpamGuard operates as a **client-side pre-filter** before any HTTP POST reaches
the Go backend. It prevents spam at the source, saving bandwidth and reducing noise.

### 6.1 Fingerprinting

| Parameter | Value |
|-----------|-------|
| Algorithm | SHA-256, truncated to 16 hex chars |
| Input | `{sender}\|{topic}\|{type}\|{normalized_content[:200]}` |
| Normalization | Strips timestamps, UUIDs, cycle numbers, gate IDs, numeric suffixes |

The normalization step is critical: it prevents near-duplicate messages (same content with
different timestamps or sequence numbers) from bypassing the dedup filter.

### 6.2 Category-Specific Rules

| Category | Pattern Match | Dedup Window | Notes |
|----------|--------------|--------------|-------|
| General duplicate | Any same fingerprint | **900 seconds** (15 min) | Default for all messages |
| DEAD alerts | `type=alert`, content contains "DEAD" | **120 seconds** (2 min) | Prevents flooding on transient failures |
| `daemon_health` | `type=daemon_health` | **60 seconds** (1 min) | 1 health msg per minute per daemon |
| Knowledge/learning | `topic=knowledge` | **1800 seconds** (30 min) | Don't re-broadcast known facts |
| Gate votes | `type=gate-vote` | **86400 seconds** (permanent) | One vote per voter per gate — ever |
| Results | `type=result` | **300 seconds** (5 min) | Prevent duplicate result reports |

### 6.3 Rate Limiting

| Parameter | Default | Override |
|-----------|---------|----------|
| Per-minute limit | 5 msgs/min | monitor: 10/min, system: 10/min, convene-gate: 8/min |
| Per-hour limit | 30 msgs/hour | — |
| Penalty per violation | -0.1 score | Via `skynet_scoring.py` |

**Override senders** (defined in `SENDER_RATE_OVERRIDES`):

| Sender | Rate Limit |
|--------|-----------|
| `monitor` | 10/min |
| `system` | 10/min |
| `convene-gate` | 8/min |
| `skynet` | 8/min |

### 6.4 `guarded_publish()` API

```python
from tools.skynet_spam_guard import guarded_publish

result = guarded_publish({
    'sender': 'gamma',
    'topic': 'orchestrator',
    'type': 'result',
    'content': 'task completed signed:gamma'
})
```

**Return value:** `dict`

| Key | Type | Description |
|-----|------|-------------|
| `allowed` | bool | True if message passed all spam checks |
| `published` | bool | True if HTTP POST succeeded (only when allowed=True) |
| `fingerprint` | str | Computed fingerprint for reference |
| `reason` | str | Block reason (only when allowed=False) |
| `fallback` | bool | True if SpamGuard failed and direct POST was used |

**Singleton pattern:** Uses module-level `_singleton_guard` — first call initializes,
subsequent calls reuse. If SpamGuard initialization fails, falls back to direct HTTP POST
(never silently drops messages).

### 6.5 `check_would_be_blocked()` Pre-Flight API

```python
from tools.skynet_spam_guard import check_would_be_blocked

result = check_would_be_blocked({
    'sender': 'gamma',
    'topic': 'orchestrator',
    'type': 'result',
    'content': 'task completed'
})

if result['would_block']:
    print(f"Skip: would be blocked by {result['reason']}")
```

**Return value:** `dict`

| Key | Type | Description |
|-----|------|-------------|
| `would_block` | bool | True if message WOULD be blocked |
| `reason` | str | Why it would be blocked (empty if not blocked) |
| `fingerprint` | str | Computed fingerprint for reference |
| `checks` | dict | Results of each check: `pattern`, `dedup`, `rate_limit` |

**Read-only guarantee:** Does NOT record fingerprints, does NOT update sender timestamps,
does NOT publish the message, and does NOT apply score penalties.

### 6.6 Dual Filter Interaction

Messages must pass **both** filters to be published:

```
Publisher → Python SpamGuard → HTTP POST → Go SpamFilter → Ring Buffer
              (5/min, 900s)                   (10/min, 60s)
```

| Scenario | Python Result | Go Result | Outcome |
|----------|---------------|-----------|---------|
| Fresh message, under limits | Pass | Pass | Published |
| Duplicate within 60s | Blocked (900s window) | N/A (never reaches Go) | Blocked at Python |
| Duplicate at 120s | Blocked (900s window) | N/A | Blocked at Python |
| Duplicate at 600s | Blocked (900s window) | N/A | Blocked at Python |
| Duplicate at 1000s | Pass (beyond 900s) | Pass (beyond 60s) | Published |
| 6th msg in 1 minute | Blocked (5/min limit) | N/A | Blocked at Python |
| 11th msg in 1 minute (monitor) | Pass (10/min override) | Blocked (10/min limit) | `allowed=True, published=False` |

---

## 7. ConveneGate Governance

**Files:** `tools/skynet_convene.py` (ConveneGate class), `tools/convene_gate.py` (CLI/monitor)

ConveneGate prevents low-quality or redundant findings from reaching the orchestrator.
Workers cannot post directly to `topic=orchestrator` — all reports must pass through
the gate's proposal → voting → elevation pipeline.

### 7.1 Pipeline Flow

```
Worker finding → propose() → Classification → Duplicate check
                                                    │
                    ┌───────────────────────────────┘
                    │
              ┌─────▼──────┐    ┌─────────────┐    ┌──────────────┐
              │   Pending    │───▶│  Voting      │───▶│  Elevated     │
              │  Proposal    │    │  (2+ YES)    │    │  (queued)     │
              │  gate_id     │    │              │    │               │
              └──────────────┘    └──────┬───────┘    └───────┬───────┘
                                         │                     │
                                    2+ NO votes          30-min digest
                                         │                     │
                                    ┌────▼─────┐         ┌────▼──────┐
                                    │ Rejected  │         │ Delivered  │
                                    └──────────┘         │ to Orch    │
                                                         └───────────┘
```

### 7.2 Proposal Classification

| Classification | Criteria | Action |
|---------------|----------|--------|
| `invalid` | Empty, <24 chars, insufficient detail | Rejected immediately |
| `low_signal` | Generic/non-specific, lacks keywords | Queued for shared cross-validation review |
| `valid` | Specific, has evidence, actionable | Enters voting pipeline |

### 7.3 Voting

| Parameter | Value |
|-----------|-------|
| Majority threshold | **2 YES votes** |
| Rejection threshold | **2 NO votes** |
| Proposer auto-vote | YES (counted as 1 vote) |
| Double-vote penalty | -0.1 score (SpamGuard category: 86400s window) |

### 7.4 Elevation and Digest Delivery

When a proposal reaches 2+ YES votes:

1. Proposal status set to `"elevated"`, moved from `pending` to `elevated` list
2. Added to `delivery_queue` for digest consolidation
3. **Issue-family dedup in queue:** If same fingerprint or issue_key already queued,
   updates existing entry instead of adding new one (increments `repeat_count`)
4. Digest flushed every **30 minutes** (`DELIVERY_INTERVAL_S = 1800`)
5. Before delivery, checks if action was already taken — if yes, suppresses
6. Delivered as consolidated `elevated_digest` type to orchestrator

### 7.5 Issue-Family Dedup

Two dedup mechanisms prevent the same issue from being re-raised:

| Method | Algorithm | Purpose |
|--------|-----------|---------|
| Fingerprint | SHA1 of canonicalized report (first 16 chars) | Exact content match |
| Issue-family key | SHA1 of (code references + concern terms) | Semantic match across phrasings |

**Concern terms:** badge, bridge, cache, daemon, timeout, endpoint, routing, security, etc.

**Resend cooldown:** Same unresolved finding cannot be re-raised within **15 minutes**
(`RESEND_INTERVAL_S = 900`).

### 7.6 Architecture-Backing Rule

Architecture-sensitive reports (containing 2+ architecture signals like "cache", "daemon",
"endpoint", "session") must include:

1. Report length ≥ 80 characters
2. At least one code reference (`.py` file, function call, path)
3. ≥ 2 mechanism hints ("because", "calls", "endpoint", "poll", "queue", "uses", etc.)
4. ≥ 1 realistic fix hint ("add", "fix", "instead", "propose", "timeout", etc.)

Reports failing these checks are routed to `architecture_review` queue instead of
entering the voting pipeline.

### 7.7 Stale Proposal Expiry

Proposals that don't reach consensus within **5 minutes** (`max_age_s=300`) are expired.
If fewer than 2 workers are available, proposals with ≥1 YES vote are auto-elevated
(reason: `expired_insufficient_peers`).

### 7.8 Urgent Bypass

Reports tagged `urgent=True` bypass the entire gate and go directly to the orchestrator
with `type="urgent"`. Reserved for system-critical alerts only (worker death, security
breach, system down).

**State file:** `data/convene_gate.json`

---

## 8. Knowledge Broadcasting

**File:** `tools/skynet_knowledge.py`

The knowledge system enables workers to share learned facts and build collective intelligence
through a validation-based confidence model.

### 8.1 Broadcasting

```python
from tools.skynet_knowledge import broadcast_learning

broadcast_learning(
    sender='gamma',
    fact='Ring buffer overflow drops oldest messages silently',
    category='architecture',
    tags=['bus', 'ring-buffer', 'message-loss']
)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `sender` | str | Worker name |
| `fact` | str | The learned fact |
| `category` | str | One of: `pattern`, `bug`, `optimization`, `architecture`, `security`, `performance` |
| `tags` | list[str] | Optional tags for filtering/search |

**Bus message:** `topic="knowledge"`, `type="learning"`, content is JSON with
`fact`, `category`, `tags`, `learned_at` fields.

### 8.2 Absorption

```python
from tools.skynet_knowledge import absorb_learnings

count = absorb_learnings('gamma')  # Returns number of new facts absorbed
```

- Polls bus for `topic=knowledge` messages
- Filters out own messages (sender ≠ worker_name)
- Only accepts `type="learning"` messages
- Stores each fact in `LearningStore` via `store.learn()` with source `"bus:{sender}"`

### 8.3 Validation and Confidence

```python
from tools.skynet_knowledge import validate_fact

validate_fact(fact_id='...', validator_name='gamma', agrees=True)
```

- Posts validation vote to bus with `type="validation"`
- When **3+ workers agree** on a fact, `store.reinforce(fact_id)` is called
- Reinforced facts receive higher confidence scores in `LearningStore`
- Higher-confidence facts are prioritized in retrieval by `HybridRetriever`

### 8.4 Related Storage

| File | Purpose |
|------|---------|
| `data/knowledge_graph.json` | Cross-linked fact relationships |
| `data/proposals.json` | Pending improvement proposals |
| `data/incidents.json` | Incident reports for institutional memory |
| `core/learning_store.py` | Persistent fact storage with confidence scores |

---

## 9. Bus Persistence

**File:** `tools/skynet_bus_persist.py`

The bus persistence daemon solves the ring buffer's fundamental limitation: no data survives
server crashes, and the 100-message FIFO silently drops old messages during burst traffic.

### 9.1 Architecture

```
Go Backend /stream (SSE) ──▶ skynet_bus_persist.py ──▶ data/bus_archive.jsonl
     (1Hz ticks)                (SSE subscriber)          (append-only JSONL)
```

### 9.2 Implementation Details

| Parameter | Value |
|-----------|-------|
| SSE source | `http://localhost:8420/stream` |
| Archive format | JSONL (one JSON object per line, append-only) |
| Archive file | `data/bus_archive.jsonl` |
| Rotation threshold | 50 MB |
| Dedup | In-memory `seen_ids` set (last 500 IDs) |
| PID file | `data/bus_persist.pid` (singleton enforcement) |
| Reconnect | Exponential backoff: 5s → 10s → 20s → 40s → 60s max |
| Shutdown | Graceful via SIGINT/SIGTERM/SIGBREAK |

### 9.3 Archive Record Format

```json
{
  "id": "msg_42_alpha",
  "sender": "alpha",
  "topic": "orchestrator",
  "type": "result",
  "content": "task completed signed:alpha",
  "timestamp": "2026-03-12T05:00:00Z",
  "archived_at": 1741755600.123
}
```

### 9.4 CLI Commands

```bash
python tools/skynet_bus_persist.py              # Run daemon (foreground)
python tools/skynet_bus_persist.py --stats       # Show archive statistics
python tools/skynet_bus_persist.py --tail 20     # Show last 20 messages
python tools/skynet_bus_persist.py --search "keyword"  # Search archive
```

---

## 10. Message Loss Risk Matrix

Seven identified risks to bus message reliability, from the communication layer analysis:

| # | Risk | Severity | Cause | Mitigation | Monitoring |
|---|------|----------|-------|------------|------------|
| 1 | **Ring buffer overflow** | HIGH | 100-msg FIFO evicts oldest on overflow | `skynet_bus_persist.py` archives ALL messages via SSE | `bus_dropped` counter in SSE stream |
| 2 | **Server crash** | HIGH | In-memory ring buffer, no persistence | `bus_persist.py` JSONL archive + `skynet_watchdog.py` auto-restart | Watchdog daemon monitors PID |
| 3 | **Dual spam filter confusion** | MEDIUM | Independent Python (5/min, 900s) and Go (10/min, 60s) filters can block at either layer | `check_would_be_blocked()` pre-flight + dual filter documentation | `spam_log.json` + Go `[SPAM_BLOCKED]` logs |
| 4 | **Slow subscriber drop** | MEDIUM | 64-msg channel buffer, non-blocking send | Subscribers should drain channels promptly | `bus_dropped` counter |
| 5 | **SSE reconnect gap** | LOW | SSE subscriber (`bus_persist.py`, `realtime.py`) misses messages during reconnect | Exponential backoff reconnect + last-10-messages in SSE payload | Daemon health checks |
| 6 | **Fingerprint collision** | LOW | SHA-256 truncated to 16 hex chars could theoretically collide | 16 hex chars = 2^64 space, collision probability negligible | None needed |
| 7 | **Clock skew in dedup** | LOW | Timestamp-based dedup windows assume consistent clocks | Single-server architecture (no distributed clock issues) | None needed |

---

## 11. Worker State Communication

Worker state is tracked through a **4-tier authority hierarchy** where each tier provides
a different tradeoff between accuracy and speed:

```
Tier 1 (GROUND TRUTH)     Tier 2 (CACHED)          Tier 3 (AGGREGATED)     Tier 4 (FILE-BASED)
─────────────────────      ──────────────           ────────────────────     ──────────────────
UIA Engine COM scan        skynet_monitor.py         /status endpoint         data/realtime.json
  ~30-50ms per scan          polls UIA every            Go backend               skynet_realtime.py
  Direct Win32/UIA           10-60 seconds              aggregates                daemon caches
  Returns: IDLE,             3 consecutive               heartbeats               orch_realtime.py
  PROCESSING,                UNKNOWN = DEAD               from workers              reads file
  STEERING,                  Auto-corrects                                          0.5s poll
  TYPING,                    model drift                                            resolution
  UNKNOWN
```

### How They Interconnect

1. **UIA Engine** (`tools/uia_engine.py`) — COM-based scan of VS Code windows. Returns actual
   UI state (IDLE/PROCESSING/STEERING/TYPING/UNKNOWN). Called by monitor daemon and dispatch.

2. **skynet_monitor.py** — Background daemon that polls UIA every 10-60 seconds. Detects
   model drift (auto-corrects), stuck workers (PROCESSING > 180s → auto-cancel), and dead
   windows (3 consecutive UNKNOWN = DEAD alert). Posts heartbeats to `/worker/{name}/heartbeat`.

3. **`/status` endpoint** — Go backend aggregates worker heartbeats. Returns JSON with all
   worker states, last heartbeat times, and task counts. Used by orchestrator on every turn.

4. **`data/realtime.json`** — `skynet_realtime.py` daemon SSE-subscribes to `/stream` and
   writes atomic state snapshots every second. `orch_realtime.py` CLI reads this file for
   instant (zero-network) status queries and result waiting at 0.5s resolution.

### State Definitions

| State | Meaning | Source |
|-------|---------|--------|
| `IDLE` | Worker chat is empty/waiting for input | UIA: no active generation |
| `PROCESSING` | Worker is generating a response | UIA: generation indicator visible |
| `STEERING` | Worker showing draft choice panel | UIA: multiple draft cards visible |
| `TYPING` | Text is being entered into input | UIA: input box has content |
| `UNKNOWN` | UIA scan failed (window moved, minimized, etc.) | 3 consecutive = treat as DEAD |

---

## 12. Configuration Reference

### Go Backend Configuration

| Parameter | Default | Env Variable | Description |
|-----------|---------|-------------|-------------|
| Port | 8420 | `SKYNET_PORT` | HTTP server port |
| Ring size | 100 | `SKYNET_RING_SIZE` | Bus ring buffer capacity |
| Workers | alpha,beta,gamma,delta,orchestrator | `SKYNET_WORKERS` | Registered worker names |
| Max retries | 3 | `SKYNET_MAX_RETRIES` | Task retry limit |
| Spam rate limit | 10 msgs/min | — | Per-sender, hardcoded |
| Spam dedup window | 60 seconds | — | Fingerprint dedup, hardcoded |
| Spam cleanup | 5 minutes | — | Stale entry pruning interval |
| Sub channel buffer | 64 messages | — | Per subscriber, hardcoded |
| Task queue max | 200 tasks | — | Old completed/failed dropped first |
| Convene session max | 50 sessions | — | Oldest dropped |

### Python SpamGuard Configuration

| Parameter | Value | Constant |
|-----------|-------|----------|
| Default rate limit | 5 msgs/min | `DEFAULT_MAX_PER_MINUTE` |
| Hourly rate limit | 30 msgs/hour | `DEFAULT_MAX_PER_HOUR` |
| General dedup window | 900 seconds | `DEFAULT_DEDUP_WINDOW` |
| Spam penalty | -0.1 score | `SPAM_PENALTY` |
| State file | `data/spam_guard_state.json` | — |
| Spam log | `data/spam_log.json` | — |

### ConveneGate Configuration

| Parameter | Value | Constant |
|-----------|-------|----------|
| Majority threshold | 2 votes | `MAJORITY_THRESHOLD` |
| Resend interval | 900 seconds (15 min) | `RESEND_INTERVAL_S` |
| Digest interval | 1800 seconds (30 min) | `DELIVERY_INTERVAL_S` |
| Stale expiry | 300 seconds (5 min) | `expire_stale(max_age_s=300)` |
| State file | `data/convene_gate.json` | `GATE_FILE` |

### Bus Persistence Configuration

| Parameter | Value | Constant |
|-----------|-------|----------|
| Archive file | `data/bus_archive.jsonl` | `ARCHIVE_FILE` |
| Rotation threshold | 50 MB | `MAX_ARCHIVE_BYTES` |
| Reconnect delay (initial) | 5 seconds | `RECONNECT_DELAY_S` |
| Reconnect delay (max) | 60 seconds | `MAX_RECONNECT_DELAY_S` |
| PID file | `data/bus_persist.pid` | `PID_FILE` |
| SSE source | `http://localhost:8420/stream` | `STREAM_URL` |

---

## 13. Related Files

### Go Backend

| File | Purpose |
|------|---------|
| `Skynet/server.go` | Main HTTP server — all endpoints, spam filter, WebSocket |
| `Skynet/bus.go` | MessageBus implementation — ring buffer, subscribe, publish |
| `Skynet/types.go` | BusMessage struct, AgentView, ConveneSession |
| `Skynet/config.go` | Configuration constants and env var parsing |

### Python — Publishing and Filtering

| File | Purpose |
|------|---------|
| `tools/skynet_spam_guard.py` | Client-side spam guard — `guarded_publish()`, `check_would_be_blocked()` |
| `tools/skynet_bus_persist.py` | SSE subscriber daemon — archives all messages to JSONL |
| `tools/skynet_bus_relay.py` | Bus relay daemon — forwards messages between systems |

### Python — Governance and Knowledge

| File | Purpose |
|------|---------|
| `tools/skynet_convene.py` | ConveneGate class, convene sessions, voting, digest delivery |
| `tools/convene_gate.py` | ConveneGate CLI and monitor daemon |
| `tools/skynet_knowledge.py` | Knowledge broadcasting, absorption, validation, proposals |
| `core/learning_store.py` | Persistent fact storage with confidence scores |

### Python — State and Monitoring

| File | Purpose |
|------|---------|
| `tools/skynet_realtime.py` | SSE subscriber daemon — writes `data/realtime.json` every second |
| `tools/orch_realtime.py` | Orchestrator CLI — reads `realtime.json` for instant state queries |
| `tools/skynet_monitor.py` | Background health monitor — UIA polling, model guard, heartbeats |
| `tools/skynet_watchdog.py` | Service watchdog — auto-restarts crashed backend services |
| `tools/uia_engine.py` | COM-based UI Automation engine — ground truth state scanning |

### Python — Dispatch (uses bus for results)

| File | Purpose |
|------|---------|
| `tools/skynet_dispatch.py` | Ghost-type dispatch — clipboard paste, delivery verification |
| `tools/skynet_brain_dispatch.py` | AI-powered auto pipeline — plan+dispatch+wait+synthesize |
| `tools/skynet_delivery.py` | Central delivery routing — consultant bridge, ghost_type |

### Data Files

| File | Purpose |
|------|---------|
| `data/bus_archive.jsonl` | Persistent message archive (bus_persist.py output) |
| `data/spam_guard_state.json` | SpamGuard fingerprints and sender timestamps |
| `data/spam_log.json` | Log of blocked spam messages |
| `data/convene_gate.json` | ConveneGate state (pending proposals, elevated, stats) |
| `data/realtime.json` | Live state snapshot (updated every second by realtime daemon) |
| `data/worker_scores.json` | Worker scores (affected by spam penalties) |
| `data/workers.json` | Worker registry (HWNDs, names, layout) |

---

> **Document version:** 1.0 | **Generated by:** gamma (worker) | **Date:** 2026-03-12
> <!-- signed: gamma -->
