# WORLD-CLASS SKYNET COMMAND CENTER: ATOMIC DESIGN & ARCHITECTURE PROPOSAL

## Executive Summary
The current iteration of the GOD Console (`dashboard.html` & `god_console.py`) provides an excellent, cyberpunk-themed visual foundation but remains a **passive monitoring surface**. A "world-class dashboard" for an autonomous multi-agent hierarchy cannot just be a read-only telemetry screen; it must be an **Interactive Control Plane**. 

This proposal maps out the exact design issues, the structural grid overhaul, and the atomic component specifications required to turn the dashboard into a truth-driven, functional command center.

---

## 1. Current Design Issues & Truth Violations
Before building the new, we must address the architectural and UX debt in the existing UI:

### 1.1 Non-Interactive UI
- **Issue:** The Orchestrator/Operator can see an agent is locked in "STEERING" or "PROCESSING" but cannot intervene from the UI. Terminal scripts must be manually executed to unblock them.
- **Fix:** Add absolute unblock controls directly onto the agent DOM cards (Halt, Reset, Clear Steering).

### 1.2 Space Utilization & Grid Monopolization
- **Issue:** The `iq-panel` spans the entire left column (rows 1-3), while the `orchFeed` is crammed into the center. There is no dedicated space for the two most critical operational views: the **Kanban Board** and the **Active Task DAG**.
- **Fix:** Restructure the global CSS Grid. Introduce dynamic pane-switching or a collapsible "Drawer" architecture to multiplex the viewable area.

### 1.3 Truth Principle Violations in Animation
- **Issue:** Several CSS elements (e.g., `scanline`, `dotPulse`, `radar`) are infinite CSS animations. They pulse regardless of whether data is actually flowing over the SSE channel.
- **Fix:** Tie **all** animations to the `skynet_sse_daemon.py` ping rate. If the ping is delayed, the radar stops. If the scan is slow, the pulse turns red. Silence is truth.

### 1.4 Abstract Consultant Logging
- **Issue:** The Gemini and Codex Consultants post rich proposals to the bus, which are currently dumped into the global event stream, getting lost under rapid worker logs.
- **Fix:** Introduce a dedicated Consultant Dock that isolates advisory intelligence and allows direct conversational prompting from the UI.

---

## 2. The New Global Grid Architecture
To accommodate new capabilities without causing sensory overload, the CSS layout must be fundamentally restructured.

### 2.1 The 4-Sector Layout
```css
.main-v3 {
  display: grid;
  height: calc(100vh - 48px);
  padding: 8px;
  gap: 8px;
  grid-template-columns: 280px 1fr 350px; /* Left Telemetry, Center Canvas, Right Dock */
  grid-template-rows: minmax(0, 1fr) 220px; /* Top Canvas, Bottom Worker Array */
}
```

- **Left Column (Telemetry & Identity):**
  Combines the existing IQ Panel, overall system latency, DAG nodes summary, and Skynet brain config states.
- **Center Canvas (Multiplexed Viewport):**
  A tabbed or toggleable central pane switching between:
  1. **Feed View:** The raw Skynet Bus stream (current behavior).
  2. **Kanban View:** Visual drag-and-drop representation of `data/todos.json`.
  3. **DAG View:** `Cytoscape.js` network graph mapping `core/dag_engine.py` task dependencies.
- **Right Column (Consultant Dock & Planning):**
  Dedicated panel tracking `topic: consultant` bus messages, with an input API field to request Gemini/Codex review.
- **Bottom Row (Worker Array):**
  The 4 agent cards spanning the bottom horizontally, modified to include direct action controls.

---

## 3. Atomic Implementation Blueprint

### 3.1 Interactive Worker Array (The "Unblocker")
**Goal:** Empower the dashboard to directly send standard Win32/UIA unblock signals.
* **UI Elements:**
  Below the `<span id="task-alpha">`, append:
  ```html
  <div class="worker-actions">
    <button class="btn action-halt" onclick="issueCommand('alpha', 'halt')">â– </button>
    <button class="btn action-steer" onclick="issueCommand('alpha', 'clear_steer')">CLEAR</button>
    <button class="btn action-reset" onclick="issueCommand('alpha', 'reset')">â†º</button>
  </div>
  ```
* **Backend (`god_console.py`):**
  Create `POST /api/control/worker`. Import `tools/uia_engine.py` (via a safe queue so it doesn't block the server loop) to execute:
  1. `halt` -> `engine.cancel_generation()`
  2. `clear_steer` -> Invoke UIA Backspace pattern.
  3. `reset` -> Send `Ctrl+N` via Win32.

### 3.2 Visual Kanban (The "Zero-Idle Tracker")
**Goal:** Render `todos.json` into a drag-and-drop interface, giving the Orchestrator immediate visibility on mission progress.
* **Frontend Flow:**
  - Create 4 flex columns: `[ Pending | Active | Blocked | Done ]`.
  - Fetch `/api/todos`. Map items to Draggable `<li>` cards.
  - Apply colors based on `assignee` (`var(--alpha)`, `var(--beta)`, etc.).
* **State Mutation:**
  - On `drop()`, JS calls `POST /api/todos/move` with `{"id": "79689c69", "status": "active"}`.
  - **Truth Rule:** The DOM card must revert its position if the POST receives anything other than `200 OK`. 

### 3.3 Genuine Telemetry Sparklines
**Goal:** Replace CSS guessing with accurate system load representation.
* **Metrics:** 
  Modify `skynet_watchdog.py` or the SSE daemon to package `[UIA Latency, Bus Depth, Tokens/Sec]` arrays.
* **Canvas Drawing:**
  - Inject `<canvas class="sparkline"></canvas>` into the left telemetry column.
  - Update on every SSE Event. Draw real data: 
    - Green = latency under 150ms.
    - Amber = latency 150-400ms.
    - Red (with UI glow warning) = latency > 400ms (system overloaded).

### 3.4 Consultant Dock (The "Advisory Peer")
**Goal:** Full visibility of Codex and Gemini intelligence flows.
* **UI Element:** 
  The Right column becomes `div#consultant-dock`. It subscribes to the SSE stream but ONLY renders messages where `sender == "consultant" || sender == "gemini_consultant"`.
* **Interaction:**
  Add a command input mapped to `POST /api/bus/publish`. 
  Payload: `{"topic": "consultant", "type": "prompt", "content": "Review the load balancer config"}`. This triggers the consultant dynamically.

---

## 4. Phased Execution Directives for Workers

To execute this massive upgrade without system destabilization, the Orchestrator should delegate utilizing the following phases via `skynet_dispatch.py`:

**Wave 1: Data & Backend Prep (Worker: Gamma / Delta)**
- Expose `POST /api/control/worker`, `POST /api/todos/move`, and `POST /api/bus/publish` in `god_console.py`. 
- Ensure `skynet_sse_daemon.py` correctly pipes the telemetry arrays.

**Wave 2: Grid Restructure (Worker: Alpha / Beta)**
- Modify `dashboard.html` to the 3-column, 2-row grid. 
- Implement the "Center Canvas" tab switcher.

**Wave 3: Component Injection (Worker: All)**
- **Alpha:** Implement Kanban DND logic and API hooks.
- **Beta:** Create the Sparkline graphical render loops.
- **Gamma:** Build the interactive worker buttons & tie them to `/api/control/worker`.
- **Delta:** Implement the Consultant Dock filtering and input mechanism.

## Conclusion
This architecture aligns the dashboard with the **Truth Principle** completely. It drops simulated animations in favor of reactive telemetry, decentralizes the view into functional tabs, and replaces basic terminal oversight with a genuine, point-and-click Command Center.