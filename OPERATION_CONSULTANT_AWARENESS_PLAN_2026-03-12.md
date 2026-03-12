# Operation Consultant Awareness

Date: 2026-03-12
Author: consultant
Status: Proposal only, pending worker cross-validation before execution

## Mission

Build a truthful, always-queryable awareness layer so Skynet can reliably find:

- all four workers
- the orchestrator
- Codex Consultant
- Gemini Consultant

The operation name is **Consultant Awareness**, but the scope is intentionally broader than consultants alone. The real requirement is actor awareness across the whole Skynet topology, with consultants treated as first-class discoverable actors instead of optional side surfaces.

## Current Real Topology

### Workers

Current worker registry from `data/workers.json`:

| Worker | HWND | Grid | Status |
|---|---:|---|---|
| Alpha | 268790 | top-left | IDLE |
| Beta | 530704 | top-right | IDLE |
| Gamma | 399770 | bottom-left | IDLE |
| Delta | 334246 | bottom-right | IDLE |

Current live backend state from `/status`:

- Alpha -> `IDLE`
- Beta -> `IDLE`
- Gamma -> `IDLE`
- Delta -> `IDLE`

### Orchestrator

Current orchestrator identity from `data/orchestrator.json`:

- HWND: `8132014`
- role: `orchestrator`
- session mode: `dedicated_window`
- model: `Claude Opus 4.6 (fast mode)`
- agent: `Copilot CLI`
- boot trigger: `orchestrator-start`

### Consultants

Current live consultant bridge state:

- Codex Consultant
  - id: `consultant`
  - port: `8422`
  - status: `LIVE`
  - routable: `true`
  - prompt transport: `bridge_queue`
- Gemini Consultant
  - id: `gemini_consultant`
  - port: `8425`
  - status: `LIVE`
  - routable: `true`
  - prompt transport: `bridge_queue`

## Problem Statement

Skynet already has pieces of awareness, but they are split across different files and endpoints:

- workers are tracked in `data/workers.json`
- orchestrator identity is tracked in `data/orchestrator.json`
- consultants are discovered via bridge probes and cached dashboard views
- dashboard rendering still has to merge separate data paths

That fragmentation creates predictable failures:

1. one surface knows workers but not consultants
2. another surface knows consultants but not window geometry
3. orchestrator identity exists, but is not presented as part of the same actor lookup model
4. stale cache can disagree with live probes
5. users can ask "where is Codex" or "where is Gemini" and the answer depends on which surface got queried

This is an awareness architecture problem, not just a dashboard bug.

## Core Goal

Skynet must be able to answer these questions truthfully at any time:

- where is Alpha/Beta/Gamma/Delta?
- what HWND and grid slot does each worker own?
- where is the orchestrator?
- which VS Code window or role identity is the orchestrator using?
- is Codex Consultant live and promptable right now?
- is Gemini Consultant live and promptable right now?
- what source proved each answer?

If the answer is unknown, report `unknown`. Do not guess.

## Architectural Principle

The system should stop treating workers, orchestrator, and consultants as separate discovery categories.

Instead, Skynet should expose a single **actor awareness model** with role-specific locators:

- worker -> `hwnd`, `grid`, `window health`, `backend status`
- orchestrator -> `hwnd`, `session mode`, `agent`, `model`, `boot trigger`
- consultant -> `bridge port`, `/health`, `/consultants`, `routable`, `prompt_transport`

Same registry, different locator type.

## Proposed Design

### 1. Canonical Actor Registry

Add a new declaration file:

- `data/actor_registry.json`

This should become the canonical list of discoverable Skynet actors:

- `alpha`
- `beta`
- `gamma`
- `delta`
- `orchestrator`
- `consultant`
- `gemini_consultant`

Each entry should define:

- stable `id`
- display name
- actor kind: `worker`, `orchestrator`, or `consultant`
- locator kind: `hwnd`, `bridge`, or mixed
- expected live source
- aliases
- whether the actor is command-routable

Example structure:

```json
{
  "id": "consultant",
  "display_name": "Codex Consultant",
  "kind": "consultant",
  "locator_kind": "bridge",
  "aliases": ["codex", "cc", "cc-start"],
  "bridge": {
    "port": 8422,
    "health_url": "http://localhost:8422/health",
    "view_url": "http://localhost:8422/consultants"
  },
  "routable_when": "bridge_live_and_accepts_prompts"
}
```

### 2. Unified Awareness Snapshot

Add an aggregator:

- `tools/skynet_awareness.py`

Output:

- `data/awareness_snapshot.json`

Responsibilities:

- load actor registry
- probe each actor using the correct truth path
- merge worker registry, orchestrator identity, and consultant bridge truth
- record freshness timestamps
- keep source attribution per field

The snapshot should include:

- `actors`
- `leadership`
- `workers`
- `find_index`
- `updated_at`

### 3. Awareness Endpoint

Expose one consolidated dashboard/API surface:

- `GET /awareness`
- `GET /awareness/actor/<id>`
- `GET /awareness/find?q=gemini`

This should live in `god_console.py` first because it already aggregates multiple truth sources. The dashboard should read the awareness endpoint instead of having separate ad hoc lookup logic for:

- workers
- consultants
- leadership

### 4. Lookup Rules

Every actor should be discoverable by:

- canonical id
- display name
- known aliases

Examples:

- `alpha` -> worker Alpha
- `orchestrator` -> orchestrator actor
- `codex`, `consultant`, `cc-start` -> Codex Consultant
- `gemini`, `gemini_consultant`, `gc-start` -> Gemini Consultant

This matters because users do not always use the internal routing id.

### 5. Truth Priority By Actor Type

#### Workers

Priority:

1. live worker window/UIA truth
2. `data/worker_health.json`
3. backend `/status`
4. `data/workers.json`

#### Orchestrator

Priority:

1. live dedicated HWND truth if available
2. `data/orchestrator.json`
3. backend `/leadership` style probe

#### Consultants

Priority:

1. live bridge `/health`
2. live bridge `/consultants`
3. heartbeat age + PID liveness
4. state file
5. previous bus identity

Cached state must never outrank a failed live probe.

## Required Data Model

Each actor in the awareness snapshot should include:

- `id`
- `display_name`
- `kind`
- `status`
- `live`
- `routable`
- `locator`
- `truth_sources`
- `last_probe_at`
- `staleness`
- `notes`

Examples:

### Worker actor

```json
{
  "id": "alpha",
  "kind": "worker",
  "status": "IDLE",
  "live": true,
  "locator": {
    "hwnd": 268790,
    "grid": "top-left"
  },
  "truth_sources": ["workers.json", "worker_health.json", "backend:/status"]
}
```

### Orchestrator actor

```json
{
  "id": "orchestrator",
  "kind": "orchestrator",
  "status": "IDLE",
  "live": true,
  "locator": {
    "hwnd": 8132014,
    "session_mode": "dedicated_window"
  },
  "truth_sources": ["orchestrator.json", "leadership probe"]
}
```

### Consultant actor

```json
{
  "id": "consultant",
  "kind": "consultant",
  "status": "LIVE",
  "live": true,
  "routable": true,
  "locator": {
    "port": 8422,
    "health_url": "http://localhost:8422/health",
    "view_url": "http://localhost:8422/consultants"
  },
  "truth_sources": ["bridge:/health", "bridge:/consultants"]
}
```

## Dashboard Changes

The dashboard should stop maintaining separate discovery logic for workers versus consultants where possible.

Recommended dashboard changes:

1. add a dedicated **Awareness** panel
2. show all 7 actors in one list:
   - Alpha
   - Beta
   - Gamma
   - Delta
   - Orchestrator
   - Codex Consultant
   - Gemini Consultant
3. show role-specific locator data:
   - workers -> HWND + grid
   - orchestrator -> HWND + session mode
   - consultants -> bridge port + promptability
4. show the source of truth for each row
5. show `unknown` instead of silently omitting a missing actor

This solves the "can we always find Gemini and Codex?" problem at the UI layer.

## Self-Invocation And Protocol Changes

Awareness has to be enforced behaviorally, not just rendered.

### Consultant starts

`CC-Start.ps1` and `GC-Start.ps1` should explicitly require:

- verify that the actor is discoverable in the awareness snapshot
- if bridge is live but awareness says missing/offline, report awareness drift
- if awareness says live but live probes fail, self-heal immediately

### Orchestrator

The orchestrator already has stronger rules, but awareness should add:

- verify the orchestrator actor entry on startup
- verify the worker table matches `data/workers.json` plus live status
- verify both consultants are present in the actor index, even when not live

### Workers

Workers do not need consultant bridge semantics, but they should remain discoverable in the same actor index so the system can answer "where is Delta?" and "what grid slot owns Gamma?" from one place.

## Alerts And Auto-Recovery

Add awareness-specific alerts:

- `AWARENESS_MISSING_ACTOR`
- `AWARENESS_STALE_LOCATOR`
- `AWARENESS_TRUTH_DRIFT`
- `CONSULTANT_NOT_DISCOVERABLE`
- `ORCHESTRATOR_NOT_DISCOVERABLE`

Examples:

- Codex bridge is live, but `/awareness` does not include `consultant`
- `workers.json` says Alpha HWND is `268790`, but UIA says it is gone
- `orchestrator.json` exists, but the HWND is dead

These should post to the Skynet bus with evidence, not vague claims.

## Why This Is Better

This operation fixes a deeper structural issue:

- today, "find the actor" depends on which file or endpoint happened to be checked
- after this operation, "find the actor" becomes a single truthful operation

That gives Skynet:

- better dashboard truth
- better incident investigations
- better self-heal triggers
- less duplication across startup scripts
- reliable discovery of both consultants even when one is down

## Rollout Plan

### Phase 1. Declaration

1. create `data/actor_registry.json`
2. define stable ids, aliases, and locator kinds
3. preserve current worker HWND/grid data exactly

### Phase 2. Aggregation

1. build `tools/skynet_awareness.py`
2. generate `data/awareness_snapshot.json`
3. validate against current worker/orchestrator/consultant truth

### Phase 3. API

1. add `/awareness`
2. add `/awareness/actor/<id>`
3. add `/awareness/find?q=...`

### Phase 4. Dashboard

1. add Awareness panel
2. consume `/awareness`
3. show workers, orchestrator, and both consultants in one place

### Phase 5. Startup Enforcement

1. update `CC-Start.ps1`
2. update `GC-Start.ps1`
3. update orchestrator startup/reporting path
4. add awareness drift alerts

### Phase 6. Validation

1. tests for actor lookup by id and alias
2. tests for stale cache versus live probe precedence
3. tests for missing consultant handling
4. tests for worker HWND/grid integrity

## Acceptance Criteria

This operation is complete only if all of these are true:

1. `consultant` can always be found by id, name, or alias
2. `gemini_consultant` can always be found by id, name, or alias
3. the orchestrator can always be found with truthful HWND identity
4. workers always expose HWND + grid + live status in one awareness surface
5. dashboard shows all 7 actors or shows `unknown` truthfully when not available
6. no surface claims consultant `LIVE` from stale cache alone
7. no actor disappears from awareness silently

## Recommendation

Proceed with **Operation Consultant Awareness** as a system-awareness architecture change, not as a consultant-only patch.

The right end state is:

- one actor registry
- one awareness snapshot
- one query path for findability
- truthful role-specific locators
- both consultants permanently discoverable as identities, and truthfully live only when their bridges are actually live

That will make Codex Consultant and Gemini Consultant reliably findable without distorting worker or orchestrator truth.

<!-- signed: consultant -->
