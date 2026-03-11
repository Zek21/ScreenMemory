# Codex Consultant Boot Failure and Truth Remediation Proposal

Timestamp: 2026-03-11 21:02:02 +08:00
Author: `consultant` (Codex Consultant)
Trigger: `CC-Start`
Scope: consultant bootstrap truth, worker boot truth, cross-surface health truth

## Executive Summary

The `CC-Start` path did not fail cleanly. It encountered a real procedural failure during boot, partially recovered, then still declared success. That is a truth problem.

Observed facts:

- `CC-Start.ps1` detected Skynet backend and GOD Console as already running.
- `CC-Start.ps1` decided there were "No live workers -- full boot needed".
- It ran `tools/skynet_start.py --workers 4`.
- Worker session restore/open failed for at least `beta` and `gamma`.
- `tools/new_chat.ps1` reported: `ERROR: Could not find New Chat dropdown (...) button.`
- Boot stopped further worker opening after consecutive failures.
- The bootstrap summary still printed `SKYNET ONLINE` and later `Bootstrap completed successfully`.
- The same bootstrap summary also printed `Workers (0)`.
- Live backend `/status` immediately afterwards reported 5 agents: `alpha`, `beta`, `delta`, `gamma`, `orchestrator`, all `IDLE`.
- Live backend `/health` reported `status=ok`, `workers_alive=5`, `bus_depth=21`.
- Consultant bridge `/health` reported `status=ok`.
- GOD Console `/health` reported `status=ok`.
- GOD Console `/system/health` simultaneously reported `status=degraded` with `issues=["Bus: unreachable"]`.
- The bus contains a truthful `consultant` `identity_ack` at `2026-03-11T21:00:35.4348521+08:00`.

This means multiple system surfaces disagreed about live reality at the same time, yet the user-facing bootstrap path still returned a success conclusion. That violates the repo's stated truth standard.

## Incident Detail

### Boot Timeline

At approximately `2026-03-11 21:00:08 +08:00`:

1. `CC-Start.ps1` confirmed Skynet and GOD Console were already running.
2. It concluded there were no live workers and triggered a full boot.
3. `skynet_start.py` attempted to restore worker sessions.
4. Session restore failed for `Worker BETA` and `Worker GAMMA`.
5. Fresh window creation also failed because `new_chat.ps1` could not find the expected New Chat control.
6. The worker opening phase stopped after consecutive failures.
7. The script continued through registration, engine setup, save state, and daemon checks.
8. It printed a "success" message despite the failed worker window phase.

### Verified Live State After Boot

Independent checks after bootstrap showed:

- `http://localhost:8420/health` returned:
  - `status: ok`
  - `workers_alive: 5`
  - `bus_depth: 21`
- `http://localhost:8420/status` returned agent entries for:
  - `alpha`
  - `beta`
  - `delta`
  - `gamma`
  - `orchestrator`
- `http://localhost:8422/health` returned:
  - `status: ok`
  - `service: consultant-bridge`
- `http://localhost:8421/health` returned:
  - `status: ok`
- `http://localhost:8421/system/health` returned:
  - `status: degraded`
  - `issues: ["Bus: unreachable"]`

The bus also held the consultant identity acknowledgement:

- `sender=consultant`
- `topic=orchestrator`
- `type=identity_ack`
- timestamp `2026-03-11T21:00:35.4348521+08:00`

## Why This Matters

This was not a cosmetic logging issue. It produced operational ambiguity in at least four places:

1. A consultant bootstrap path attempted a full worker boot.
2. Worker boot actually failed for multiple windows.
3. Success was still declared.
4. Different health and status surfaces disagreed about whether workers and the bus were live.

If left unchanged, this leads to bad dispatch decisions, false readiness claims, and dashboard/operator confusion. In a multi-agent system, contradictory control-plane truth is more dangerous than a clean failure.

## Root Cause Analysis

## Root Cause 1: Consultant bootstrap is overreaching

`CC-Start` is a consultant entrypoint. It should ensure shared infrastructure is reachable, announce consultant identity, and stay role-correct. Instead, it escalated into a full worker boot because some worker liveness check judged the system to have "No live workers".

That behavior is risky because consultant startup now depends on worker UIA/window conditions that are outside the consultant's core contract.

Probable issue:

- The liveness decision used by `CC-Start.ps1` is not aligned with backend truth from `/status` or `/health`.
- It may be checking window registry state or UIA-discovered workers only, while backend memory still reports agents alive.

## Root Cause 2: Boot success criteria are wrong

`skynet_start.py` treated worker open failures as non-fatal enough to continue, then emitted a top-level success line. That is misleading.

A boot result should not collapse all of these into one "success":

- backend reachable
- engines reachable
- worker windows successfully opened
- worker registry truthful
- consultant bridge reachable

Those are different dimensions. One cannot substitute for another.

## Root Cause 3: UI selector fragility in `new_chat.ps1`

The immediate operational failure was:

- `ERROR: Could not find New Chat dropdown (...) button.`

That strongly suggests the UI detection logic is brittle against:

- changed VS Code layout
- changed label text
- changed glyph encoding
- changed button hierarchy
- focus/context drift

This is the tactical fault that caused window creation failure.

## Root Cause 4: Surface contracts are underspecified

At the same time, the system presented:

- backend `/health`: ok
- backend `/status`: 5 agents
- bootstrap summary: `Workers (0)`
- GOD `/system/health`: degraded because bus unreachable

Those statements cannot all be treated as equivalent readiness signals. The system needs explicit contracts for what each surface means.

## Truth Violations Observed

These are the specific truth failures exposed by this incident:

### 1. Success without full success

The bootstrap emitted `Bootstrap completed successfully` after a real worker open failure.

### 2. Worker count ambiguity

The script showed `Workers (0)` while `/status` showed 5 live agent records.

### 3. Health ambiguity

One health surface was `ok` while another said `degraded` because the bus was unreachable, even though the backend bus surface was clearly serving data.

### 4. Role drift pressure

A consultant startup path effectively became a worker bootstrap/recovery path.

Even if that is intentionally allowed as a fallback, it must be reported as:

- `consultant live`
- `shared infrastructure recovered`
- `worker bootstrap attempted and partially failed`

not flattened into a generic success message.

## Proposed Systemic Improvements

## Proposal A: Split readiness into named dimensions

Replace single success banners with a structured readiness result:

- `backend_ready`
- `god_console_ready`
- `consultant_bridge_ready`
- `worker_registry_ready`
- `worker_windows_ready`
- `dispatch_ready`
- `truth_consistent`

The displayed overall outcome should be derived from those fields, not hand-written prose.

Suggested aggregate states:

- `ok`
- `degraded`
- `partial`
- `failed`

If worker windows fail to open, overall status must not be `ok`.

## Proposal B: Make consultant boot non-authoritative for worker liveness

`CC-Start.ps1` should not promote a worker bootstrap unless there is a clear, cross-validated signal that shared infrastructure is genuinely down or that worker recovery is explicitly permitted.

Minimum cross-validation before full boot:

1. Check `http://localhost:8420/health`
2. Check `http://localhost:8420/status`
3. Check worker registry/state file
4. Check consultant bridge state
5. Classify mismatch explicitly

If backend says agents are alive but window registry is empty, `CC-Start` should report:

- `consultant ready`
- `worker surface mismatch detected`
- `full worker boot skipped` or `full worker boot attempted due explicit policy`

That preserves truth and role boundaries.

## Proposal C: Treat worker open failures as first-class degraded state

In `skynet_start.py`, the worker opening phase must return structured failures, for example:

```json
{
  "worker_windows_requested": 4,
  "worker_windows_opened": 0,
  "worker_windows_failed": ["beta", "gamma"],
  "fatal_phase_failures": ["worker_open_phase"]
}
```

Then the final status line should derive from those counts.

Example truthful summary:

`BOOT DEGRADED -- backend online, consultant bridge online, worker open failed for beta/gamma, registry inconsistent with /status`

## Proposal D: Harden `new_chat.ps1`

`new_chat.ps1` needs a more resilient control-discovery strategy:

1. Match multiple candidate button names, not one glyph-dependent selector.
2. Prefer automation ids or stable UIA properties over display glyphs.
3. Log every candidate control considered, with name/class/automation id.
4. Distinguish:
   - control not found
   - control found but not invokable
   - wrong VS Code surface active
   - focus stolen
5. Add a dry-run diagnostic mode that prints the resolved candidate set without clicking.

This script should fail loudly but diagnostically, not opaquely.

## Proposal E: Unify worker truth semantics

There are at least three distinct notions of "worker" in the system:

1. backend agent records
2. registered worker windows
3. UIA-confirmed live windows

These should never be collapsed into one unlabeled `Workers (N)` value.

Instead, surfaces should report:

- `backend_agents`
- `registered_windows`
- `uia_live_windows`

If these disagree, the disagreement itself should be shown.

## Proposal F: Define strict health endpoint contracts

Health endpoints should answer different questions explicitly:

- `/health`
  - process/service health
- `/system/health`
  - cross-dependency health
- `/status`
  - current live state snapshot

If `/system/health` says `Bus: unreachable` while `/status` is returning bus data, that is either:

- a stale dependency probe
- a different bus target than expected
- a logic bug in health aggregation

This needs a single source of dependency truth with timestamps and probe targets included in the response.

## Proposal G: Add a boot truth ledger

Every bootstrap should write an atomic structured report to disk, for example:

- requested role
- trigger
- phases run
- phases skipped
- phases failed
- final readiness dimensions
- contradictions detected
- timestamps for each probe

This creates one durable artifact for operators, daemons, dashboard surfaces, and consultants to read without inferring from console text.

## Proposal H: Add regression tests for truth semantics

Tests should cover:

1. consultant boot when backend is already alive
2. consultant boot when worker windows are absent but backend still reports agents
3. worker open failure in `new_chat.ps1`
4. final banner when any worker open fails
5. `/health`, `/system/health`, `/status` disagreement handling
6. consultant identity ack only when bridge `/health` is live

The key invariant:

No bootstrap may print `success` or `online` unless every required readiness dimension for that exact role is actually true.

## Implementation Plan

## Phase 1: Truth-preserving reporting

- Update `CC-Start.ps1` to classify consultant boot separately from worker boot.
- Update `skynet_start.py` final summary to use structured readiness dimensions.
- Change `Workers (N)` displays to label the source of the count.

## Phase 2: UIA robustness

- Refactor `tools/new_chat.ps1` selector logic.
- Add diagnostic output mode.
- Add fallback selector strategy.

## Phase 3: Health consistency

- Audit GOD `/health` and `/system/health` dependency checks.
- Include probe target, probe time, and error source in health payloads.
- Ensure system health cannot claim bus unreachable when the same bus is being served live without explaining the discrepancy.

## Phase 4: Test enforcement

- Add automated tests for consultant boot truth paths.
- Add failure injection tests for worker open failures.
- Gate success banners on test-covered readiness rules.

## Immediate Action Items

1. Fix `tools/new_chat.ps1` New Chat detection logic.
2. Change `CC-Start.ps1` to mark this class of run as `degraded`, not `successful`.
3. Stop showing unlabeled `Workers (0)` when backend `/status` reports live agents.
4. Audit GOD `/system/health` bus dependency logic against live backend reachability.
5. Add a structured boot outcome artifact written on every startup.

## Acceptance Criteria

This incident class is fixed only when all of the following are true:

- `CC-Start` can complete without booting workers when shared infrastructure is already healthy.
- If `CC-Start` does attempt worker recovery and any worker open fails, the final state is `degraded` or `failed`, never `successful`.
- `Workers (N)` style displays are source-labeled and contradiction-safe.
- `new_chat.ps1` no longer depends on one brittle button signature.
- `/health`, `/system/health`, and `/status` expose timestamps and probe provenance for each claim.
- Consultant identity is only announced as live when bridge `/health` is live.
- Operators can inspect one machine-readable boot artifact and see the same truth the console showed.

## Closing

The biggest problem exposed here is not that one UI selector broke. UI selectors break. The more serious problem is that the system allowed a broken phase, contradictory live surfaces, and a success banner to coexist.

That is the exact class of failure the Truth Principle is supposed to prevent.

This proposal recommends moving boot and health reporting from impressionistic console text to explicit, multi-dimensional, testable truth contracts. Once those contracts exist, consultant startup, worker startup, dashboard readiness, and orchestrator decisions can all consume the same verified control-plane truth instead of inferring different realities from different surfaces.
