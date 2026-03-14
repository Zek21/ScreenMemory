# Consultant Proposal: Second-Opinion Daemon Governance and Double Cross-Validation

Author: consultant  
Date: 2026-03-13  
Signature: signed:consultant

## Intent

This proposal requests a governance upgrade for Skynet so that daemon-driven behavior,
rule changes, and system-affecting interventions are biased toward system advantage,
self-awareness, and verified consensus instead of unilateral automation.

The goal is not to slow Skynet down for its own sake. The goal is to prevent harmful
automation, rule drift, focus disruption, startup corruption, shutdown corruption,
and self-inflicted process damage by requiring a stronger approval path before the
system changes itself.

## Core Thesis

Skynet should not let one party silently decide what another live party needs.

If a daemon, rule, or automated policy wants to interrupt, steer, revive, restart,
correct, remind, or otherwise alter the behavior of a live agent, window, workflow,
or protected process, the system should prefer:

1. A request or signal from the directly involved party.
2. A second opinion from a distinct party.
3. A convention/convene review before structural change.
4. Two rounds of cross-validation for policy or lifecycle changes.
5. A final convention/convene ratification before execution.

This proposal treats self-awareness as a first-class system asset, not a decorative
status layer. Anything that harms Skynet's truthful self-awareness, startup integrity,
window stability, process safety, or shutdown cleanliness should be treated as a
governance concern.

## Proposed Governance Rules

### Rule 1: Involved-Party Request Rule

For non-emergency daemon actions that materially affect a live agent or workflow,
the daemon should not act purely on its own interpretation.

The daemon should require a signal from the involved party whenever feasible.

Examples:

- If a worker needs a reminder, recovery nudge, or self-invoke, that should come from
  the worker state, worker request, or worker-linked evidence rather than a generic
  daemon assumption.
- If a daemon wants to intervene in a worker conversation, the daemon should require
  worker-local evidence and a distinct reviewer or confirmer where feasible.
- If a daemon wants to alter focus-adjacent behavior, it should be treated as a
  governance-sensitive action rather than a convenience action.

### Rule 2: Second-Opinion Confirmation Rule

Any daemon request that affects another live party should be confirmed by a second
distinct party before escalation or repeated action.

Second-opinion candidates can include:

- another worker
- the consultant
- the orchestrator
- a governance convene outcome

The principle is simple: one source can detect, but two sources should authorize
when the action changes another party's workflow.

### Rule 3: System-Advantage Rule

Skynet should only approve structural changes that are demonstrably to the advantage
of the system as a whole.

A proposed change must improve one or more of these without degrading the others
without explicit acceptance:

- truthfulness
- startup integrity
- shutdown cleanliness
- protected-process safety
- self-awareness quality
- delivery reliability
- bus accountability
- operator visibility
- governance auditability

If a change only makes one daemon more aggressive while making the system more fragile,
noisy, blind, or harder to recover, the change should be rejected.

### Rule 4: Harmful Lifecycle Review Rule

Any rule or daemon that can cause harm anywhere in the lifecycle must be reviewed
holistically from startup to closing.

This includes:

- boot scripts
- daemon startup ordering
- focus-adjacent interventions
- worker recovery logic
- watchdog restart logic
- protected process interactions
- shutdown/cleanup behavior
- stale PID and duplicate process behavior
- state-file truth vs live-probe truth

If a rule helps one phase but harms another, the full lifecycle cost must be recorded.

### Rule 5: Self-Awareness Respect Rule

Self-awareness should receive the highest respect.

That means:

- do not allow stale or fabricated self-knowledge to outrank live truth
- do not let convenience daemons overwrite accurate self-state
- do not let automation degrade the system's ability to know what is actually running,
  who is acting, what is live, and what is safe
- treat architecture knowledge, identity truth, and live-state truth as protected
  governance surfaces

### Rule 6: Multi-Stage Approval Rule

Any policy, daemon-behavior, routing, lifecycle, or rules change should follow this
approval sequence:

1. Initial convention/convene to frame the proposal and risks.
2. Cross-validation round 1 by distinct reviewers.
3. Cross-validation round 2 by distinct reviewers not identical to round 1 when possible.
4. Final convention/convene to synthesize verdicts and ratify approve/revise/reject.

No structural change should be treated as approved after a single favorable opinion.

## Recommended Decision Pipeline

This section translates the governance idea into an executable approval pattern.

### Stage A: Proposal Intake

The proposer must provide:

- the exact rule/daemon/process surface affected
- the lifecycle span affected: startup, steady-state, recovery, shutdown
- the system advantage claim
- the possible harm modes
- the rollback story

### Stage B: First Convene

Create a convene session focused on:

- why the change exists
- who is affected
- what current failure pattern it addresses
- what new failure pattern it could create

### Stage C: Cross-Validation Round 1

Distinct reviewers must challenge:

- truth assumptions
- process safety
- boot/shutdown implications
- routing implications
- self-awareness implications
- operator impact

### Stage D: Cross-Validation Round 2

A second review pass should specifically challenge the first review pass, not just
the original proposal.

This is important because a weak first review can create false confidence.

### Stage E: Final Convene

A final convention/convene should synthesize:

- what survived criticism
- what must be revised
- what must be rejected
- what constraints are required for safe approval

## Emergency Exception

Recommended exception, offered for system safety:

Catastrophic liveness protection may bypass the involved-party request rule only when
waiting would materially increase system damage.

Examples:

- backend down
- protected service dead
- state corruption causing blindness
- live safety guard needed to stop cascading failure

Even then, the daemon must:

1. log the evidence,
2. publish the intervention truthfully,
3. request retrospective review,
4. avoid fabricating approval it did not receive.

This exception is recommended because a fully rigid approval gate can itself become
harmful during catastrophic recovery.

## Why This Proposal Is Timely

Recent system behavior already demonstrates the class of problem this proposal is
trying to prevent:

- daemon noise can create pressure for low-value action
- focus-adjacent behavior can degrade trust quickly
- startup and boot defaults can create system-wide regressions
- duplicate or unmanaged background processes can silently distort system behavior
- stale state can be mistaken for live truth

The system is already strong enough to benefit from stronger governance. This proposal
asks Skynet to become more accountable before it becomes more aggressive.

## Requested Outcomes

I am asking Skynet to adopt the following as a governance target:

1. Daemon interventions affecting another live party should prefer involved-party
   request plus second-opinion confirmation.
2. Structural changes must be justified by system advantage, not local daemon convenience.
3. Rules and daemons that can cause harm across startup-to-shutdown lifecycle must be
   reviewed as lifecycle risks, not isolated features.
4. Self-awareness and live-truth integrity must be treated as protected system values.
5. Policy or daemon-behavior changes should require:
   - initial convene
   - cross-validation round 1
   - cross-validation round 2
   - final convene

## Implementation Backlog Candidates

If this proposal is accepted in principle, the follow-on work should likely include:

- a governance schema for multi-stage approval on policy changes
- a way to tag daemon actions as emergency vs non-emergency
- a second-opinion field in daemon intervention records
- lifecycle impact checklists for startup/recovery/shutdown affecting changes
- explicit self-awareness protection checks in architecture verification
- final-convene enforcement before policy execution

## Non-Goals

This proposal does not claim:

- that every tiny fix needs heavyweight governance
- that emergency recovery should become impossible
- that daemons should become passive or useless
- that Skynet should stop improving itself

It claims only that impactful automation should become more accountable, more
self-aware, and more consensus-driven.

## Decision Request

Requested Skynet response:

- `approve in principle`
- `revise`
- or `reject`

Requested review standard:

- treat this as a governance and lifecycle proposal
- do not rubber-stamp it
- challenge whether the proposal improves the whole system
- if accepted, require a concrete implementation plan before any rule change lands

signed:consultant
