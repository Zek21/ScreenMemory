# Skynet Level 4 Architecture — Intelligence Maximizer

**Author:** Worker Beta (Protocol Engineer)
**Date:** 2026-03-10
**Status:** Design Document — Ready for Implementation

---

## Executive Summary

Level 3 gave Skynet production-grade resilience (watchdog, crash recovery, real IQ tracking, truth enforcement). Level 4 moves from **reliable infrastructure** to **maximized intelligence** — the system becomes genuinely autonomous, self-healing, and operates with zero human attention overhead.

---

## Capability 1: Fully Focusless Dispatch

### Problem
Current `ghost_type_to_worker()` uses `SetForegroundWindow` + clipboard paste (`WM_PASTE`). This steals focus from the orchestrator for ~200ms per dispatch, disrupting the user if they're typing.

### What Actually Works vs What Doesn't (Tested)

**WORKS:**
- `OpenClipboard(NULL)` + `SetClipboardData` — sets clipboard without needing focus ✓
- `WM_PASTE` via `PostMessage` to Chromium-based VS Code — pastes from clipboard ✓
- `PostMessage(WM_CHAR)` for simple ASCII — sometimes works on Chromium ✓

**DOES NOT WORK (Chromium limitation):**
- `PostMessage(WM_KEYDOWN/WM_KEYUP)` for Enter key — Chromium ignores keyboard messages sent via PostMessage. The Electron/Chromium input pipeline uses a separate IPC channel (Mojo) between the browser process and renderer, so Win32 keyboard messages posted to the HWND never reach the web content.
- `SendInput` without focus — SendInput always targets the foreground window
- `PostMessage(WM_KEYDOWN, VK_RETURN)` — silently dropped by Chromium

**CURRENT WORKAROUND:**
The ghost_type pipeline uses: `SetClipboard` → `PostMessage(WM_PASTE)` → brief `SetForegroundWindow` + `SendKeys({ENTER})` → restore focus. The Enter key is the ONLY operation requiring focus steal (~50ms window).

### Implementation Path

**Phase 1 — Minimize focus steal window (Level 4.0)**
The current approach is already near-optimal. Reduce the focus steal to bare minimum:
1. Set clipboard without focus (already works)
2. `PostMessage(WM_PASTE)` without focus (already works)
3. For Enter: `SetForegroundWindow` → immediate `SendInput(VK_RETURN)` → immediate restore
4. Total focus steal: ~20ms (down from ~200ms)
5. Use `AttachThreadInput` to avoid focus flash

**Files:** `tools/skynet_dispatch.py` → `ghost_type_to_worker()`

**Phase 2 — Named pipe injection (Level 4.1)**
For true zero-UI dispatch:
1. Write a tiny VS Code extension (`skynet-injector`) that listens on a named pipe (`\\.\pipe\skynet-{worker}`)
2. Extension receives task text and programmatically inserts it into the Copilot CLI input + submits
3. No clipboard, no focus, no Win32 messages — pure IPC
4. Extension runs executeCommand('workbench.action.chat.submitPrompt') after inserting text

**Files:** New `extensions/skynet-injector/` directory

**Phase 3 — Direct language server protocol (Level 4.2)**
Bypass UI entirely:
1. Connect to the Copilot CLI's internal API via localhost HTTP or WebSocket
2. Submit prompts programmatically
3. Read responses from the API stream
4. Workers become headless — no VS Code window needed

**Risk:** Copilot CLI internal API is undocumented and may change between versions.

---

## Capability 2: Worker-to-Worker Direct Communication

### Problem
All worker coordination goes through the orchestrator (bus relay). Worker A posts to bus → orchestrator reads → orchestrator dispatches to Worker B. This adds 2-3 seconds latency per hop and burns orchestrator turns.

### Implementation Path

**Phase 1 — Bus-direct addressing (Level 4.0)**
Workers already have bus access. Enable direct messaging:
```python
# Worker Alpha wants Beta to run a subtask:
requests.post('http://localhost:8420/bus/publish', json={
    'sender': 'alpha',
    'topic': 'beta',  # Direct to worker, not 'orchestrator'
    'type': 'sub-task',
    'content': 'Run pytest on core/',
    'reply_to': 'alpha',  # Where to send result
})
```
Workers poll for `topic={their_name}` messages and auto-execute.

**Files:** `tools/skynet_dispatch.py` → new `worker_to_worker()` function, worker preamble updated to poll own topic.

**Phase 2 — Worker mesh network (Level 4.1)**
Each worker runs a lightweight HTTP server on a unique port:
- Alpha: 8430, Beta: 8431, Gamma: 8432, Delta: 8433
- Workers POST tasks directly to each other's endpoints
- Zero bus overhead, sub-100ms latency
- Mesh topology stored in `data/mesh_registry.json`

**Files:** New `tools/skynet_mesh.py` — worker-side HTTP micro-server

**Phase 3 — Shared memory IPC (Level 4.2)**
For maximum speed (sub-1ms):
1. Use Windows named shared memory (`CreateFileMapping`)
2. Each worker has a read slot and write slot
3. Workers poll their read slot at 100ms intervals
4. Zero network overhead

**Files:** `tools/skynet_ipc.py` — ctypes-based shared memory ring buffer

---

## Capability 3: Autonomous Goal Generation

### Problem
Workers sit idle until the orchestrator dispatches tasks. They don't proactively identify improvements or act on them.

### Implementation Path

**Phase 1 — Self-improvement scanner (Level 4.0)**
Each worker runs periodic self-assessment:
```python
# In worker preamble, after task completion:
1. Check TODOs (skynet_todos.py) — pick highest priority pending item
2. Scan data/incidents.json — find unresolved patterns
3. Grep for TODO/FIXME/HACK in codebase — propose fixes
4. Review own past failures (learning_store) — identify recurring issues
5. Post improvement proposal to bus: topic='planning', type='proposal'
```

**Files:** `tools/skynet_self_improve.py` — autonomous goal scanner with proposal generation

**Phase 2 — Goal marketplace (Level 4.1)**
1. Workers post proposed goals to `data/goal_marketplace.json`
2. Other workers vote (upvote/downvote) on proposals
3. Goals reaching consensus threshold auto-dispatch to the proposing worker
4. Orchestrator has veto power but doesn't need to initiate

**Files:** `tools/skynet_goals.py` — marketplace + voting + auto-dispatch

**Phase 3 — Continuous improvement loop (Level 4.2)**
1. Workers maintain personal backlog in `data/worker_{name}_backlog.json`
2. After completing an assigned task, immediately pick next from:
   a. Orchestrator-dispatched queue (highest priority)
   b. Bus sub-task requests from other workers
   c. Own backlog items
   d. System-wide improvement proposals
3. Workers never idle — always improving something

**Files:** `tools/skynet_backlog.py` — personal backlog manager

---

## Capability 4: Distributed Memory / Collective Knowledge Graph

### Problem
Each worker starts fresh each session. Knowledge learned by Alpha is not available to Beta. The LearningStore is file-based and not queryable across workers in real-time.

### Implementation Path

**Phase 1 — Bus-broadcast learnings (Level 4.0)**
Already partially implemented (`skynet_knowledge.py`). Complete the loop:
1. After task completion: `broadcast_learning(fact, confidence, source)`
2. On task start: `absorb_learnings(task_keywords)` — retrieve relevant peer discoveries
3. Facts validated by 3+ workers promoted to `high_confidence` in LearningStore

**Files:** `tools/skynet_knowledge.py` — enhance existing broadcast/absorb

**Phase 2 — Queryable knowledge API (Level 4.1)**
Add Go backend endpoints for knowledge operations:
- `GET /knowledge/search?q=clipboard+dispatch` → returns relevant learnings
- `POST /knowledge/store` → stores validated learning
- `GET /knowledge/graph` → returns relationship graph (fact → related facts)

**Files:** `Skynet/knowledge.go` — new Go module

**Phase 3 — LanceDB vector knowledge store (Level 4.2)**
1. Use existing `core/lancedb_store.py` for vector embeddings
2. Workers embed their learnings as vectors
3. Semantic search across all worker knowledge: "How did we fix the SSE daemon?" → returns relevant incidents, fixes, and learnings from any worker
4. Auto-dedup: if Beta learns same thing Alpha already knows, merge rather than duplicate

**Files:** Extend `core/lancedb_store.py` with knowledge-specific collections, `tools/skynet_knowledge.py` updated to use vector search

---

## Capability 5: Self-Healing Workers

### Problem
Workers get stuck in PROCESSING, STEERING, or crash states. Currently detected by `skynet_monitor.py` (180s threshold) and auto-cancelled. But recovery is slow and lossy.

### Implementation Path

**Phase 1 — Proactive stuck detection (Level 4.0)**
Reduce detection from 180s to 60s with graduated response:
1. **60s PROCESSING** on simple task → auto-cancel, re-dispatch to different worker
2. **120s PROCESSING** on complex task → cancel, split task into subtasks, re-dispatch
3. **STEERING detected** → immediate cancel (already implemented), add rate tracking
4. **3 STEERINGs in 5 minutes** → mark worker as degraded, prefer other workers for dispatch

**Files:** `tools/skynet_monitor.py` — graduated response, `tools/skynet_dispatch.py` — worker health scoring

**Phase 2 — Worker self-diagnostics (Level 4.1)**
Workers detect their own health:
```python
# At start of each task, worker checks:
1. Am I responding within expected timeframes?
2. Is my context window near capacity? (check token count estimate)
3. Am I producing useful output or repeating myself?
4. Post health self-report: {type: 'health', content: 'DEGRADED: context exhaustion'}
```

**Files:** `tools/skynet_self_health.py` — worker-side health monitor

**Phase 3 — Automatic context refresh (Level 4.2)**
When a worker detects context exhaustion:
1. Save current task state to `data/worker_{name}_checkpoint.json`
2. Post `CONTEXT_REFRESH_NEEDED` to bus
3. Orchestrator opens fresh chat window for worker (via `new_chat.ps1`)
4. Restore: inject identity + task checkpoint into fresh window
5. Worker resumes from checkpoint — zero task loss

**Files:** `tools/skynet_checkpoint.py` — task state save/restore, `tools/skynet_start.py` — fresh window for exhausted worker

---

## Implementation Priority (Recommended Order)

| Priority | Capability | Phase | Impact | Effort |
|----------|-----------|-------|--------|--------|
| **P0** | Focusless dispatch | Phase 1 (PostMessage) | High — eliminates user disruption | Low |
| **P0** | Backend counter notification | Phase 1 (notify_backend_dispatch) | High — fixes dashboard | Done ✓ |
| **P1** | Worker-to-worker comms | Phase 1 (bus-direct) | High — reduces orchestrator load | Low |
| **P1** | Self-healing | Phase 1 (proactive detection) | High — reduces stuck incidents | Medium |
| **P1** | Autonomous goals | Phase 1 (self-improvement scanner) | Medium — keeps workers productive | Low |
| **P2** | Distributed memory | Phase 1 (bus-broadcast) | Medium — already partially done | Low |
| **P2** | Batch dispatch | Phase 1 (batch_dispatch) | Medium — reduces dispatch overhead | Done ✓ |
| **P3** | Worker mesh network | Phase 2 | Medium — performance optimization | Medium |
| **P3** | Knowledge API | Phase 2 | Medium — queryable knowledge | Medium |
| **P4** | Named pipe injection | Phase 2 | High — true focusless | High |
| **P4** | Context refresh | Phase 3 | High — eliminates context exhaustion | High |
| **P5** | Headless workers | Phase 3 | Transformative — no UI needed | Very High |
| **P5** | Shared memory IPC | Phase 3 | Marginal — bus is fast enough | High |

---

## Success Metrics for Level 4

| Metric | Level 3 Baseline | Level 4 Target |
|--------|-----------------|----------------|
| Dispatch latency | 800ms (with focus steal) | <100ms (focusless) |
| Worker idle time | 30-60% of session | <5% (autonomous goals) |
| Stuck recovery time | 180s detection + 30s cancel | 60s detection + 10s cancel |
| Cross-worker knowledge reuse | 0% (siloed) | 80% (broadcast + absorb) |
| Orchestrator turns per task | 3-5 (dispatch + poll + synthesize) | 1-2 (dispatch-and-wait) |
| Dashboard accuracy | 0% (counters never increment) | 100% (notify_backend_dispatch) |
| Worker-to-worker latency | 3-5s (bus relay via orchestrator) | <500ms (direct addressing) |

---

## Non-Goals for Level 4

- **Multi-machine distribution** — all workers on same machine for now
- **Custom model per worker** — all workers run Opus 4.6 fast
- **Dynamic worker scaling** — fixed 4-worker grid for now
- **External API exposure** — Skynet stays localhost-only
