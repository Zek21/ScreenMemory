# Skynet Dashboard Upgrade: Atomic Implementation Plan

**Objective:** Transform the read-only GOD Console (`dashboard.html` / `god_console.py`) into an Interactive Command Center, implementing the principles of `DASHBOARD_UPGRADE_PROPOSAL.md`.
**Constraint:** Strict adherence to the **Truth Principle** (no simulated UIs, actual state reflection only), Zero-Idle rules, and existing UIA/Win32 architecture.

---

## Phase 1: Interactive Worker Controls (Highest ROI)

**Goal:** Allow Orchestrator to unblock workers directly from the UI without terminal commands.

### Atomic Tasks:
- [ ] **T1.1 (UI Component):** Update `dashboard.html` worker card templates. Add a control row containing:
  - `<button class="btn-action cancel" data-worker="${w.name}">Halt (Cancel Gen)</button>`
  - `<button class="btn-action clear-steer" data-worker="${w.name}">Clear Steering</button>`
  - `<button class="btn-action restart" data-worker="${w.name}">Ctrl+N (Soft Reset)</button>`
- [ ] **T1.2 (Backend Endpoint):** In `god_console.py`, add a `do_POST` dispatch path for `/api/worker/action`.
  - Parse JSON body: `{"worker": "alpha", "action": "halt|clear_steer|restart"}`.
- [ ] **T1.3 (UIA Integration):** Wire the POST endpoint to `tools/uia_engine.py` and UIA utilities (running in a background thread to prevent blocking the God Console server):
  - **Halt:** Call `engine.cancel_generation(hwnd)`.
  - **Clear Steering:** Call UIA `Cancel (Alt+Backspace)` button invoke pattern (via `uia_engine`).
  - **Restart:** Send `Ctrl+N` via Win32 or UIA (to clear stuck context), followed by model-guard verification.
- [ ] **T1.4 (Frontend JS):** Add `EventDelegation` on the worker grid. On click, `fetch('/api/worker/action', {method: 'POST'})`. Display true success/failure based on the 200/500 JSON response (Truth Principle). Post actions to Skynet bus (`/bus/publish`) logging the UI intervention.

---

## Phase 2: Kanban TODO Integration

**Goal:** Transform pending text TODOs into a visual, state-mutating Kanban system linked directly to `data/todos.json`.

### Atomic Tasks:
- [ ] **T2.1 (Domain Layer):** Inspect `god_console.py`'s `/todos` GET endpoint. Ensure it returns comprehensive item objects `{id, title, status, assignee, priority}`.
- [ ] **T2.2 (UI Structure):** In `dashboard.html`, replace the `.todos-container` list with a 4-column Flexbox layout: `<div id="kanban-pending">`, `<div id="kanban-active">`, `<div id="kanban-blocked">`, `<div id="kanban-done">`.
- [ ] **T2.3 (HTML5 Drag & Drop):**
  - Add `draggable="true"` to Todo card `<div>`s.
  - Implement `dragstart`, `dragover` (preventDefault), and `drop` event listeners.
- [ ] **T2.4 (Mutation Endpoint):** Add `POST /api/todos/move` in `god_console.py`.
  - Inputs: `{"item_id": 12, "new_status": "active", "assignee": "beta"}`
  - Logic: Acquire lock on `data/todos.json`, read, mutate the matching item's status, write back.
  - Post update to Skynet Bus: `{"topic": "planning", "type": "todo_update", "content": "Dashboard moved Todo 12 to active"}`.
- [ ] **T2.5 (State Binding):** On successful D&D drop, execute the POST. Only permanently move the card visually *after* a 200 HTTP response confirms the file mutation (Truth Principle).

---

## Phase 3: Live DAG Visualization

**Goal:** True visualization of DAGEngine task graphs.

### Atomic Tasks:
- [ ] **T3.1 (Library Import):** Inject a lightweight graph drawing library into `dashboard.html`. Suggestion: `Cytoscape.js` (via CDN or saved to `tools/browser/lib/`) for robust Directed Acyclic Graph (DAG) rendering.
- [ ] **T3.2 (Data Extraction):** In `god_console.py`, add `GET /api/dag/active`.
  - Logic: Parse the active running task list and cross-reference dependency arrays. Return a JSON structure representing `{ "nodes": [...], "edges": [...] }`.
  - Node properties: ID, status (Pending, Active, Done), assigned_worker.
- [ ] **T3.3 (UI Container):** Add a full-width section `<div id="dag-viewport" style="height: 300px; width: 100%;"></div>`.
- [ ] **T3.4 (Render Loop):** In `dashboard.html` JS, function `renderDAG(data)`.
  - Apply styling matching Skynet (Neon green/cyan borders, black fills).
  - Subscribe graph refreshes to the existing `skynet_sse_daemon.py`/WebSocket ping cycle. If graph hash changes, redraw edges.

---

## Phase 4: Consultant Copilot Dock

**Goal:** Direct asynchronous visibility into Gemini and Codex Consultant bus channels.

### Atomic Tasks:
- [ ] **T4.1 (UI Layout):** Enhance `dashboard.html` with a right-column or slide-out `<aside id="consultant-dock">`.
- [ ] **T4.2 (Data Stream Selection):** Modify the existing WebSocket handler/SSE listener in `dashboard.html` to route `bus_messages`.
- [ ] **T4.3 (Message Filtering):** Add UI tabs for `All Bus`, `Codex`, `Gemini`.
  - Logic: Filter bus stream where `msg.sender == 'consultant' || msg.sender == 'gemini_consultant'`.
- [ ] **T4.4 (Direct Prompting UI):** Add an input area at the bottom of the dock: `<input type="text" id="consultant-prompt" /> <select id="consultant-target">`.
- [ ] **T4.5 (Direct Prompt API):** Add JS listener that triggers a `POST` directly to the Skynet backplane at `http://localhost:8420/bus/publish` with:
  `{ "sender": "orchestrator", "topic": "consultant", "type": "prompt", "metadata": {"target": "gemini_consultant"}, "content": "<input_value>" }`.

---

## Phase 5: Telemetry Pulse (Sparklines)

**Goal:** Visualization of real system strain (Tokens/sec, UIA latency) reflecting true HWND query times.

### Atomic Tasks:
- [ ] **T5.1 (Metric Extraction):** Modify the UIA scanner (`tools/uia_engine.py` or the collector daemon) to log `scan_ms` moving averages into `data/metrics.json` or `data/worker_health.json`.
- [ ] **T5.2 (Console Feed):** Ensure `god_console.py`'s existing periodic payload includes `metrics: { alpha: { uia_latency: [120, 110, 150...], ram: ... } }`. Keep array size tightly bounded (e.g., last 20 ticks).
- [ ] **T5.3 (Canvas Drawing):** In frontend JS, implement a tight `requestAnimationFrame` or interval-driven canvas drawing function `drawSparkline(canvasId, dataArray)`.
- [ ] **T5.4 (UI Integration):** Add `<canvas width="100" height="30" class="sparkline"></canvas>` into each worker's card footer.
- [ ] **T5.5 (Alert Automation):** Implement thresholds logic in JS: If `dataArray[-1] > 500` (UIA lag over 500ms), pulse the worker border red, indicating UI bottleneck.

---

## Execution Constraints & Governance

1. **Truth Verification:** The UI **must not** simulate movement or fake active states. If the connection fails, visually report connection failure immediately.
2. **Process Integrity:** `god_console.py` is an unprivileged monitor, except for explicit UI/worker-fix commands. It cannot kill core processes. 
3. **Rollout:** Implement incrementally. Phase 1 (Controls) -> Phase 2 (Kanban) -> Phase 4 (Consultants) -> Phase 3 -> Phase 5.