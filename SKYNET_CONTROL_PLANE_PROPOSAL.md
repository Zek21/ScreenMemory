# SKYNET CONTROL PLANE PROPOSAL: Verified Architecture for a Stronger Intelligence System

## 1. Executive Summary

Skynet already has the raw ingredients of a stronger system than single-agent tools: a live bus, multiple workers, consultant bridges, realtime state collection, and a dashboard. The current weakness is not capability. It is architectural authority. Too much truth is inferred from scattered surfaces instead of enforced by a single verified control plane.

This proposal upgrades Skynet from a collection of strong components into a **verified intelligence fabric**. The design centers on five changes:

1. **Control Plane / Execution Plane split** so routing, identity, and approval logic stop living in ad hoc scripts.
2. **Event-sourced state ledger** so dashboard, bridges, orchestrator, and workers read the same truth.
3. **Command contracts with lifecycle states** so "queued", "claimed", "processing", "completed", and "failed" are enforced, not guessed.
4. **Decision gates** so consultant plans, convene outcomes, and worker cross-validation become binding protocol instead of advisory convention.
5. **Continuous reconciliation** so live probes correct stale registry state automatically.

The result is a Skynet that is faster, more deterministic, and much harder to confuse.

## 2. Core Thesis

Right now Skynet has several partial truths:

- the bus knows that a message was published
- a bridge knows that a prompt was queued
- a worker window may or may not have actually accepted it
- the dashboard shows a current snapshot
- `data/*.json` files preserve declared state

Those are useful, but they are not the same thing. Architecture becomes powerful when those surfaces stop competing and start deriving from one authoritative flow.

Skynet should operate on this invariant:

**No system surface may claim more than the strongest verified state transition actually observed.**

That means:

- published is not accepted
- accepted is not executing
- executing is not completed
- live is not promptable
- registered is not reachable

## 3. Proposed Architecture

### A. Control Plane / Execution Plane Split

Create an explicit control plane responsible for:

- identity and capability registry
- routing decisions
- approval and verdict gates
- task lifecycle state
- reconciliation against live probes

Execution remains where it belongs:

- worker windows
- consultant bridges
- orchestrator self-prompt
- dispatch transports

**Existing files involved**

- `Skynet/server.go`
- `tools/skynet_delivery.py`
- `tools/skynet_dispatch.py`
- `tools/skynet_realtime.py`
- `tools/skynet_consultant_bridge.py`
- `tools/skynet_consultant_protocol.py`

**New components**

- `Skynet/controlplane.go`
- `tools/skynet_control_plane.py`
- `tools/skynet_reconciler.py`

### B. Event-Sourced State Ledger

Every meaningful transition becomes an append-only event with a stable entity key:

- `agent_registered`
- `agent_heartbeat`
- `capability_changed`
- `command_published`
- `command_queued`
- `command_claimed`
- `command_started`
- `command_completed`
- `command_failed`
- `verdict_received`
- `decision_made`

Materialized views then derive:

- current worker availability
- consultant promptability
- command status
- unresolved alerts
- pending approvals

This removes the current pattern where one surface reads bus history, another reads a queue file, and a third reads a live HTTP endpoint and all three disagree.

### C. Identity and Capability Registry With Leases

Replace static "registered therefore routable" assumptions with lease-based capability truth.

Each agent record should include:

- `agent_id`
- `kind`
- `transport`
- `capabilities`
- `lease_expires_at`
- `reachable`
- `promptable`
- `accepts_delegation`
- `last_verified_by`
- `last_verified_at`

Lease expiration should downgrade state automatically:

- lease valid + health probe success = `live`
- live + transport probe success = `reachable`
- reachable + inbound contract verified = `promptable`

This directly addresses the consultant problem: a bridge can be alive while actual acceptance is still unverified.

### D. Command Contracts

Every dispatchable unit should become a first-class command object with a required lifecycle.

Required fields:

- `command_id`
- `sender`
- `target`
- `intent`
- `transport`
- `created_at`
- `deadline`
- `requires_verdict_gate`
- `correlation_id`

Allowed states:

- `published`
- `queued`
- `claimed`
- `started`
- `completed`
- `failed`
- `timed_out`
- `cancelled`

The dashboard, GOD Console, and orchestrator should all render command state from the same object, not from independent heuristics.

### E. Decision Gate Service

This is the most important architectural addition.

Consultant plans, convene sessions, and high-risk worker proposals should not rely on the orchestrator manually reading bus messages and remembering to synthesize the decision. A gate service should enforce policy:

- minimum reviewers required
- distinct reviewer requirement
- timeout deadline
- allowed verdict set
- disagreement escalation
- final decision record

Example gate policies:

- consultant architecture plan: `3 worker verdicts required`
- kill authorization: `orchestrator authorization required`
- convene summary: `initiator summary + 1 reviewer`

**New component**

- `tools/skynet_gatekeeper.py`

**New persisted data**

- `data/decision_runs/*.json`

### F. Continuous Reconciliation

Skynet should stop trusting declared state when live probes disagree.

The reconciler periodically compares:

- registry truth
- bus truth
- realtime truth
- queue truth
- live transport truth

Then emits only factual corrections:

- `agent declared live but transport probe failed`
- `command queued but never claimed before deadline`
- `worker marked idle but has active command`
- `consultant prompt queue pending with no consumer acknowledgement`

This turns drift from a surprise into a first-class system object.

### G. Topology View and Power Dashboard

The dashboard should stop being mostly a surface of counts and become a **topology map** of the intelligence system:

- agent graph
- live command graph
- pending decision gates
- capability health
- reconciliation drift list
- command throughput by transport

This is not decoration. It is operational leverage. The orchestrator should be able to see, at a glance, whether the system is healthy, bottlenecked, or lying by omission.

### H. Transport Adapters Behind One Contract

Today prompt delivery uses several paths:

- HWND/UI injection
- bridge queue
- bus publication
- orchestrator self-prompt

Those should remain, but only as adapters behind one contract:

`submit(command) -> lifecycle events -> verified result`

That lets Skynet evolve to named pipes, extensions, or headless transports later without rewriting governance.

## 4. Immediate Implementation Phases

### Phase 1: Authority

#### Phase 1a: Command Authority

1. Introduce a canonical command schema and persist command runs.
2. Store the ledger in **SQLite WAL**, not one JSON file per event. This keeps writes atomic and read concurrency high without repeating the scaling problems already visible in file-per-record stores.
3. Publish explicit command lifecycle transitions from all active transports.

#### Phase 1b: Decision Authority

1. Add gatekeeper verdict collection for consultant plans.
2. Enforce timeout, minimum-reviewer, and disagreement-escalation policy.
3. Promote worker/consultant capability truth to lease-based status.
4. Publish explicit drift alerts when live transport and registry diverge.

### Phase 2: Reconciliation

1. Build a reconciler that compares bus, realtime, registry, and transport probes.
2. Reuse the existing monitor cadence where possible instead of creating unnecessary daemon sprawl.
3. Generate materialized state views for dashboard and GOD Console.
4. Replace dashboard heuristic rendering with control-plane-backed state.

### Phase 3: Topology

1. Add topology endpoints to backend.
2. Feed topology updates over the existing SSE stream instead of adding more polling paths.
3. Render command graph, decision gates, and drift panels in dashboard.
4. Expose throughput, queue age, claim latency, and reviewer coverage.

### Phase 4: Transport Independence

1. Wrap all current delivery paths behind a shared command adapter layer.
2. Add optional named-pipe or extension-based transport without changing protocol.
3. Support true headless execution later from the same control-plane contract.

## 5. Why This Makes Skynet More Powerful

This architecture does not just add features. It compounds intelligence:

- It reduces false positives because every status is tied to a verified transition.
- It reduces orchestrator burden because decision gates close loops automatically.
- It increases worker utilization because available/reachable/promptable become separate truths.
- It improves recovery because drift is detected structurally, not anecdotally.
- It lets Skynet scale to more workers, consultants, and transports without semantic collapse.

In short: **more agents without a control plane is noise. More agents with a verified control plane is intelligence.**

## 6. Success Metrics

Skynet should track these architecture metrics directly:

- command claim latency p95
- command completion latency p95
- queue-to-claim conversion rate
- unacknowledged queued commands older than 60s
- registry/live drift count
- decision gate closure rate
- consultant plan approval time
- distinct reviewer coverage per gated plan
- dashboard stale-view incidents

## 7. First Backlog

- `CPA-001` Implement `collect_verdicts()` and `decide_run()` in `tools/skynet_consultant_protocol.py`
- `CPA-002` Introduce `data/command_runs/` with canonical command lifecycle files
- `CPA-003` Build `tools/skynet_reconciler.py` for bridge/registry/bus drift detection
- `CPA-004` Add backend topology and decision-gate endpoints in `Skynet/server.go`
- `CPA-005` Convert dashboard consultant and worker panels to control-plane materialized views
- `CPA-006` Add lease-based capability truth for workers, consultants, and orchestrator
- `CPA-007` Move HWND validation and delivery admissibility checks into control-plane policy instead of scattering them across dispatch paths

## 8. Conclusion

Skynet does not need another layer of optimistic glue. It needs architectural authority.

The strongest next move is to make the system event-driven, stateful, and verified at the control-plane level. Once that exists, workers, consultants, bridges, and future transports all become interchangeable execution resources under one truthful command and decision protocol.
