# SKYNET GOD Console (Dashboard) Upgrade Proposal

## Overview
The current GOD Console (`dashboard.html` / `god_console.html`) provides a strong visual foundation with its cyberpunk grid aesthetics and basic read-only monitoring. However, as Skynet scales, the dashboard must evolve from a passive monitoring surface into an **Interactive Command Center**. 

This proposal outlines the architectural and UI/UX changes required to make the dashboard significantly more powerful and user-friendly for the Orchestrator and human operators alike.

## 1. Live DAG Visualization
**Problem:** Currently, DAGEngine workflows (task decomposition and routing) are opaque in the UI. We see workers as "PROCESSING" but lack visibility into the exact graph of tasks being executed.
**Solution:**
- Implement an interactive, real-time node-graph using Canvas or a lightweight library (e.g., D3, Cytoscape, or a custom visualizer).
- Show the Directed Acyclic Graph (DAG) for active missions. 
- Color-code nodes by status: Pending (Gray), In-Progress (Yellow), Complete (Green), Failed (Red).
- Allow hovering over nodes to see the assigned worker and localized outputs.

## 2. Interactive Worker Controls
**Problem:** Intervening on stuck or steering workers currently requires the Orchestrator to run terminal scripts like `skynet_dispatch.py` or `orch_realtime.py`. 
**Solution:**
- Upgrade the worker cards (Alpha, Beta, Gamma, Delta) in the UI with direct action controls.
- **Halt Action:** Send an emergency cancel/kill signal directly to the underlying `uia_engine.cancel_generation(hwnd)`.
- **Clear Steering:** A one-click button to execute the `clear_steering_and_send()` UIA pattern to dismiss stuck inline prompts.
- **Re-Assign:** Drag-and-drop a stalled task to a different idling worker.
- Tie these UI buttons directly to the REST endpoints exposed by Skynet's Go backend.

## 3. Kanban TODO Integration
**Problem:** The 6 pending TODOs are tracked in `data/todos.json` and dumped to the bus, but they lack an intuitive management interface.
**Solution:**
- Transform the static TODO textual lists into a visual Kanban board.
- Columns: Backlog, Next-Up, In-Progress, Blocked, Done.
- Two-way sync: Moving a card on the dashboard instantly updates `data/todos.json` and publishes a `topic: planning` message to the bus.
- Automatically assign worker icons (Avatars/Badges) to cards when a worker picks up the ticket.

## 4. Consultant Copilot Dock
**Problem:** Consultants (like Gemini Consultant and Codex Consultant) broadcast their status to the bus, but establishing a focused 1:1 interaction with them clutters the main Orchestrator log.
**Solution:**
- Add an overlay or side-panel dock dedicated to Consultant interactions.
- Provide a localized chat-like interface or specialized data-feed specifically for the Gemini/Codex components. 
- Ensure this panel operates off the primary task-routing flow, allowing true "Advisory" parallel tracks without distracting the Orhcestrator's main loop.

## 5. Telemetry Pulse (Sparklines)
**Problem:** Operational metrics like scan latency, bus depth, and token limits require terminal checks.
**Solution:**
- Integrate high-performance `<canvas>` based sparklines above or directly on the worker cards.
- **Metrics to Track:**
  - Token throughput (Tokens / Sec).
  - UI Automation latency (ms per scan).
  - Bus depth / Queue length for `skynet_watchdog` and Message Bus updates.
- **Visuals:** Add dynamic warning pulses if metrics cross threshold limits (e.g., UIA latency > 400ms triggers a red pulse).

## Execution Path
1. **Frontend:** Update `dashboard.html` with the new DOM elements, integrating interactive CSS layers to match the existing theme (`var(--cyan-glow)`, etc.).
2. **Backend:** Ensure `dashboard_server.py` or the Go Skynet backend exposes POST routes (e.g., `/api/worker/{id}/halt`) for the interactive elements to hook into.
3. **Data Sync:** Implement a WebSocket or SSE push from `skynet_sse_daemon.py` to animate the DAG and Kanban changes in sub-50ms real-time without page refreshes.