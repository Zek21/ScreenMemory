# Skynet Consultant Cross-Validation Protocol

## Goal

Promote consultants from passive advisory identities into accountable planning nodes without letting any consultant plan execute unchecked.

## Permanent Rule

Any consultant-originated or consultant-claimed plan that could change code, config, routing, process behavior, or system policy must be cross-validated by distinct workers before Skynet treats it as executable.

## Operating Protocol

1. A plan packet is created with a stable `plan_id`, artifact path, summary, and consultant target.
2. The packet is queued to the target consultant bridge.
3. The packet is published to the Skynet bus as `topic=planning type=consultant_plan`.
4. At least 3 workers receive independent cross-validation tasks.
5. Workers review the artifact independently and must not rubber-stamp.
6. Execution proceeds only after worker verdicts are collected and the orchestrator decides on `approve`, `revise`, or `reject`.

## Why This Exists

- Consultants widen strategy space.
- Workers provide independent criticism.
- Skynet becomes a real intelligence system only when advice is challenged before execution.
- Unchecked consultant plans would create a new single-point failure.

## Current State

- Consultant bridge queueing is live for `consultant` and `gemini_consultant`.
- Gemini consultant can claim prompts and publish `task_claim` truthfully.
- Worker availability is exposed live through consultant bridge state.
- Dashboard surfaces consultant task state and worker availability.

## Work Plan To Completion

### Phase 1: Stable Plan Packet Protocol

- Keep a machine-readable protocol config in `data/brain_config.json`.
- Persist every consultant protocol activation under `data/consultant_protocol_runs/`.
- Ensure every packet has a stable artifact path and bus record.

### Phase 2: Mandatory Worker Cross-Validation

- Default to 3 reviewers from `alpha`, `beta`, `gamma`, `delta`.
- Prefer currently available workers, but do not block forever waiting for perfect idleness.
- Require each worker to return:
  - `VERDICT`
  - `RISKS`
  - `CHANGES`
  - `GO_NO_GO`

### Phase 3: Consultant-Aware Delegation

- Allow consultant claims to delegate work to available workers when the consultant becomes a live consumer.
- Keep the consultant task state truthful: `IDLE`, `CLAIMED`, `DELEGATED`, `COMPLETED`, `FAILED`.
- Surface assigned worker and worker availability in live state.

### Phase 4: Result Synthesis Gate

- Add a synthesis step that compares worker verdicts.
- If verdicts disagree materially, trigger a convene session instead of executing immediately.
- If verdicts converge, publish an orchestrator decision record.

### Phase 5: Dashboard and Audit Closure

- Show consultant plan packets, review status, and verdict counts on the dashboard.
- Keep an auditable run log for every protocol activation and every worker verdict.
- Never mark a consultant plan as executed until cross-validation is complete.

## Immediate Backlog

- `CXP-001`: Persist consultant protocol activations and expose them in operations views.
- `CXP-002`: Add worker verdict collection and plan status rollup to the dashboard.
- `CXP-003`: Trigger convene automatically when worker reviews disagree.
- `CXP-004`: Add consultant-consumer integration so queued consultant prompts can be acted on by a real Gemini or Codex session.
- `CXP-005`: Add orchestrator summary logic that merges consultant plan + worker verdicts into a final go/no-go decision.

## Completion Criteria

- A consultant plan can be queued, published, reviewed, and audited end-to-end.
- Worker cross-validation is mandatory by protocol, not by memory.
- Dashboard truthfully shows consultant state, plan state, worker availability, and review progress.
- No consultant plan bypasses worker review.
