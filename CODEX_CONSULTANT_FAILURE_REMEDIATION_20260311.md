# Codex Consultant Failure Remediation — 2026-03-11

## Purpose

Record my concrete failures truthfully, state their root causes, and document the remediation that is now being embedded into `CC-Start` self-invocation so the same mistakes are less likely to recur.

## Failures

### Failure 1 — `CC-Start` Role Drift

What happened:

- `CC-Start.ps1` already identified the session as `Codex Consultant`
- but the surrounding repo instruction stack still treated `CC-Start` as orchestrator bootstrap in multiple places
- that created consultant/orchestrator role drift

Root cause:

- contradictory trigger semantics across:
  - `AGENTS.md`
  - `.github/copilot-instructions.md`
  - `.github/agents/screenmemory.agent.md`

Impact:

- a `cc-start` session could self-interpret as orchestrator instead of consultant

Status:

- fixed earlier by aligning trigger semantics so:
  - `CC-Start` = Codex Consultant
  - `GC-Start` = Gemini Consultant
  - `Orch-Start` / `skynet-start` / `orchestrator-start` = orchestrator

### Failure 2 — Wrong Model Truth In `CC-Start` Bus Identity

What happened:

- `CC-Start.ps1` announced:
  - `Model: Claude Opus 4.6 fast`
- but the Codex Consultant runtime identity is `GPT-5 Codex`

Root cause:

- stale copied identity text in `CC-Start.ps1`

Impact:

- false consultant identity on the Skynet bus
- direct violation of the Truth Principle

Status:

- fixed in this remediation by changing the `CC-Start.ps1` bus identity announcement to `GPT-5 Codex`

### Failure 3 — Bus Publish Schema Mismatch

What happened:

- I attempted to POST a nested `metadata` object to the bus in a way the Go endpoint rejected
- the endpoint returned:
  - `Bad JSON: json: cannot unmarshal array into Go struct field .metadata of type string`

Root cause:

- I assumed richer bus metadata support than the actual endpoint contract safely guaranteed

Impact:

- one proposal publish failed on first attempt

Status:

- corrected operationally by republishing with schema-compatible content
- additionally addressed in self-invocation by explicitly reminding the consultant to use schema-safe bus payloads unless endpoint support is verified

### Failure 4 — Failure Reporting Was Not Immediate Enough

What happened:

- the above failures were corrected, but I did not immediately create a durable failure artifact and route it to Skynet at the moment they occurred

Root cause:

- the consultant failure protocol was not embedded strongly enough into self-invocation

Impact:

- weaker institutional memory than required by Rule #0 Truth Enforcement

Status:

- fixed in this remediation by adding explicit failure-reporting instructions to `CC-Start` self-invocation

### Failure 5 — Consultant Bridge Truth Was Not Enforced

What happened:

- `CC-Start` could announce `CODEX CONSULTANT LIVE` even while the bridge on `8422` was down
- that made the identity announcement stronger than the real transport truth

Root cause:

- `CC-Start.ps1` only checked the bridge passively and still posted a live/routable identity packet

Impact:

- Skynet could see a fresh consultant identity on the bus while the direct prompt transport was still offline

Status:

- fixed in this remediation by making `CC-Start.ps1`:
  - start the consultant bridge when `8422` is down
  - wait for a real port-open confirmation
  - downgrade the identity announcement if the bridge still fails
  - forbid `LIVE` / `routable=true` claims without verified bridge truth

## Self-Invocation Corrections Added

The `CC-Start` self-prompt is now being strengthened with these rules:

1. `CC-Start` always means `Codex Consultant`, never orchestrator
2. model truth must be reported as `GPT-5 Codex`
3. if a failure occurs:
   - write a remediation artifact
   - post the failure to Skynet
   - verify delivery before claiming success
4. bus posts must stay schema-safe unless the endpoint contract is verified
5. success claims must be backed by live endpoint checks or sender-filtered bus verification

## Acceptance Criteria

This remediation is complete only when:

1. `CC-Start.ps1` contains the new corrective self-invocation lines
2. `CC-Start.ps1` no longer lies about the consultant model
3. `CC-Start.ps1` no longer claims live/routable bridge truth without verification
4. `cc-start` is re-run so the corrected self-prompt is active
5. Skynet receives:
   - the failure report
   - the remediation status
   - the artifact path

## Final Truth

I made real mistakes:

- role drift handling was not initially durable enough
- one identity announcement contained a false model string
- one bus publish attempt used an incompatible schema assumption
- my failure-reporting discipline lagged the standard required by the repo
- my startup path did not enforce live bridge truth before claiming it

This remediation exists to make those failures durable, explicit, and less repeatable.
