# Dashboard Leadership Panel Truth Plan

## Objective

Upgrade the dashboard so three fixed leadership identities are monitored truthfully and continuously:

1. `Codex Consultant`
2. `Skynet Orchestrator`
3. `Gemini Consultant`

This is not a cosmetic panel split. It is a control-surface architecture change: each identity must have a dedicated monitored panel with a canonical backend contract, explicit source attribution, live/stale/offline semantics, and dashboard rendering that never fabricates presence or activity.

## Why This Is Needed

Current dashboard behavior is partially correct but structurally incomplete:

- `dashboard.html` already renders an `Orchestrator` strip from telemetry.
- `dashboard.html` renders consultants through one aggregated consultant panel, not fixed identity panels.
- `god_console.py` aggregates consultant bridges independently from orchestrator truth.
- `tools/skynet_agent_telemetry.py` already knows how to collect worker, orchestrator, and consultant telemetry, but the dashboard consumes them through different paths.
- There is no single canonical `leadership panel` contract combining identity, liveness, telemetry, bridge truth, bus truth, and cache age.

That means the dashboard can show all three identities, but it does not yet treat them as first-class monitored command surfaces.

## Current Truth In Code

### Dashboard

- `dashboard.html`
  - `renderOrchTelemetry()` renders orchestrator telemetry only.
  - `renderConsultants()` renders consultant entries as a shared list.
  - `fetchConsultantsData()` merges `/consultants` and direct bridge fallbacks.
  - `pollTelemetry()` and `masterPoll()` pull separate data streams.

### Backend

- `god_console.py`
  - `_cached_consultants()` probes consultant bridges.
  - `_build_dashboard_data()` returns aggregated dashboard data.
  - `/consultants` exposes consultant state.
  - `/stream/dashboard` pushes dashboard snapshots over SSE.

### Telemetry

- `tools/skynet_agent_telemetry.py`
  - `_collect_window_telemetry()` covers workers and orchestrator.
  - `_collect_consultant_telemetry()` covers consultants from bridge/state truth.
  - output already contains `doing`, `typing_visible`, and `thinking_summary`.

### Consultant Runtime

- `tools/skynet_consultant_bridge.py`
  - `get_consultant_view()` exposes bridge truth.
  - worker availability and task state are already attached to consultant views.

## Target Architecture

Create a canonical `leadership panel` layer with three fixed identities and one shared schema.

### Canonical Identities

- `orchestrator`
- `consultant`
- `gemini_consultant`

### Canonical Schema

Each leadership panel entry should expose:

- `id`
- `display_name`
- `role`
- `kind`
- `declared`
- `live`
- `status`
- `promptable`
- `routable`
- `transport`
- `prompt_transport`
- `backend_connected`
- `heartbeat_age_s`
- `stale_after_s`
- `current_task`
- `doing`
- `typing_visible`
- `thinking_summary`
- `last_bus_message`
- `source_map`
- `source_age_s`
- `truth_state`
- `panel_health`

### Truth Rules

- If a field is unknown, return `unknown`.
- If a source is stale, mark it stale instead of inheriting older truth as current truth.
- Orchestrator, Codex, and Gemini must always render as fixed slots even when offline.
- A missing bridge must render `OFFLINE`, not disappear.
- A missing telemetry feed must render `Telemetry unavailable`, not a synthetic fallback.

## Proposed Backend Design

### New Aggregator

Add a backend leadership aggregator in `god_console.py`:

- new helper: `_cached_leadership_panels()`
- inputs:
  - orchestrator identity and heartbeat truth
  - consultant bridge truth from `_cached_consultants()`
  - telemetry truth from `agent_telemetry.json` or `8426/telemetry`
  - latest relevant bus identity/result messages
- output:
  - fixed dict keyed by `orchestrator`, `consultant`, `gemini_consultant`

### New Endpoint

Add:

- `GET /leadership`

This endpoint becomes the canonical dashboard source for the three monitored leadership identities.

### Dashboard Data Integration

Extend:

- `/dashboard/data`
- `/stream/dashboard`

to include:

- `leadership`

This reduces frontend merge logic and prevents panel truth drift between polling modes.

## Proposed Frontend Design

Replace the single shared consultant panel with a fixed three-panel command strip:

1. `Codex Consultant`
2. `Orchestrator`
3. `Gemini Consultant`

Each panel must show:

- live status badge
- bridge/transport truth
- heartbeat age
- backend link truth
- promptability/routability
- current task
- `DO`
- `TYPE`
- `THINK`
- last bus message
- data age / stale marker

### Frontend Rules

- Never sort these identities dynamically.
- Never hide one because another is live.
- Never collapse three identities into one count-only abstraction.
- Never overwrite a fixed identity slot with an empty aggregate result.

## Atomic Code Plan

### Wave 0: Contract Definition

1. Define leadership schema contract in code comments or a helper docstring in `god_console.py`.
2. Define fixed leadership ids: `["consultant", "orchestrator", "gemini_consultant"]`.
3. Define status semantics:
   - `LIVE`
   - `STALE`
   - `OFFLINE`
   - `DECLARED`
   - `UNKNOWN`

### Wave 1: Backend Leadership Aggregation

1. In `god_console.py`, add `_latest_bus_message_for(sender_ids, types=None)`.
2. Add `_orchestrator_panel_view()`:
   - derive identity from `data/orchestrator.json`
   - merge backend `/status` orchestrator agent
   - merge telemetry `agents.orchestrator`
   - merge latest orchestrator `identity_ack`
3. Add `_consultant_panel_view(consultant_id, consultant_view, telemetry_entry)`.
4. Add `_cached_leadership_panels()`.
5. Add `GET /leadership`.
6. Add `leadership` to `_build_dashboard_data()`.
7. Add `leadership` to `/stream/dashboard`.

### Wave 2: Telemetry Alignment

1. In `tools/skynet_agent_telemetry.py`, add a stable panel-oriented export helper:
   - `leadership_projection(snapshot)`
2. Ensure orchestrator telemetry always carries:
   - `status`
   - `doing`
   - `typing_visible`
   - `thinking_summary`
   - `source`
3. Ensure consultant telemetry always carries:
   - `live`
   - `task_state`
   - `doing`
   - `thinking_summary`
   - `prompt_queue`
4. Add source-age fields for every leadership telemetry entry.

### Wave 3: Dashboard Layout Refactor

1. In `dashboard.html`, replace the consultant aggregate block with three fixed cards.
2. Add dedicated containers:
   - `leadership-codex`
   - `leadership-orchestrator`
   - `leadership-gemini`
3. Replace `renderConsultants()` with:
   - `renderLeadershipPanels()`
   - `renderLeadershipPanel(id, entry)`
4. Keep consultant counts in the top bar, but derive them from leadership entries rather than a separate consultant list.
5. Preserve the current worker telemetry section without conflating it with leadership panels.

### Wave 4: Truth and Staleness UX

1. Add explicit stale badges.
2. Add `cache age` / `source age` lines.
3. Show source provenance:
   - `bridge`
   - `telemetry`
   - `backend:/status`
   - `bus identity`
   - `state file`
4. If two sources disagree, show the stricter truth:
   - live only if bridge/heartbeat truth confirms it
   - unknown if telemetry is absent

### Wave 5: Regression Coverage

Add or extend tests for:

- `god_console.py`
  - `/leadership` returns three fixed entries
  - offline consultant still renders as fixed identity
  - stale heartbeat does not become `LIVE`
  - orchestrator panel survives missing consultant bridges
- `tools/skynet_agent_telemetry.py`
  - consultant/orchestrator telemetry source-age handling
  - missing `typing_visible` remains empty, not fabricated
- dashboard rendering tests or JS smoke assertions
  - three panel placeholders exist
  - `renderLeadershipPanels()` preserves slot identity
  - no consultant overwrite when one fetch path returns empty

## Worker Execution Plan

This should be executed as a high-complexity but feasible Skynet wave:

- `alpha`
  - dashboard DOM/CSS/JS refactor
  - fixed three-panel rendering
- `beta`
  - `god_console.py` leadership aggregation and `/leadership` endpoint
- `gamma`
  - telemetry schema alignment and orchestrator truth fusion
- `delta`
  - regression coverage and stale/offline truth tests

## Cross-Validation Plan

This task should run at the hardest feasible level:

1. Parallel implementation wave across `alpha`, `beta`, `gamma`, `delta`
2. Orchestrator synthesis
3. Independent validation wave:
   - frontend validated by non-author frontend reviewer
   - backend aggregation validated by non-author backend reviewer
   - telemetry truth validated against live files/endpoints
4. Final dashboard truth audit with screenshots and endpoint samples

This is intentionally high-complexity, but it is feasible because the primitives already exist:

- consultant bridge truth exists
- orchestrator telemetry exists
- dashboard SSE exists
- telemetry daemon exists
- consultant runtime state already includes task and worker snapshot data

## Acceptance Criteria

The task is complete only when all are true:

1. Dashboard always renders exactly three leadership panels.
2. Codex Consultant, Orchestrator, and Gemini Consultant never disappear from the dashboard because of fetch-path drift.
3. `LIVE` means confirmed by real liveness data, not by declaration alone.
4. `TYPE` and `THINK` show only real visible or explicitly reported data.
5. Offline or stale identities remain visible with truthful labels.
6. `/leadership`, `/dashboard/data`, and `/stream/dashboard` agree on leadership truth.
7. Tests cover live, stale, offline, and unknown states.

## Recommended Priority

- `priority`: critical
- `difficulty`: adversarial-feasible
- `execution_mode`: parallel multi-wave with cross-validation
- `owner`: orchestrator plus workers
- `consultant_role`: advisory architecture and truth audit only
