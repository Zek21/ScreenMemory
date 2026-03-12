# Signature Library And Accountability Plan

Date: 2026-03-12
Author: consultant
Status: Proposal only. Not yet executed.

## Purpose

Skynet already has a partial text-file signature convention and a positive-sum scoring system, but it does not yet have a single enforceable signature library that covers all file classes, including binaries, nor an evidence-driven scoring flow for caught mistakes, verified repairs, consultant findings, and consultant guidance failures.

This plan proposes a unified architecture so every change can be traced, every investigation can be grounded in real file fingerprints, and every score change can be justified by evidence instead of opinion.

## Current Truth-Grounded Baseline

The current repo already contains useful building blocks:

- `AGENTS.md` defines a `Signature Accountability Protocol` for source files and bus results.
- `tools/skynet_scoring.py` already supports awards, deductions, bug filing, bug confirmation, refactor reversals, and positive-sum recovery.
- `tools/skynet_spam_guard.py` already shows that Skynet can maintain durable content fingerprints and enforce protocol centrally.
- `tools/skynet_consultant_protocol.py` already provides a consultant-plan governance lane that can carry architectural policy proposals before execution.

The missing pieces are:

1. No canonical signature library shared by all agents.
2. No single file-fingerprint ledger that survives investigations.
3. No detached signature strategy for binaries and non-commentable files.
4. No automatic enforcement that blocks unsigned or ambiguously attributed edits from earning credit.
5. No first-class scoring workflow for:
   - mistake fixed by another worker -> original deduction cancelled
   - consultant catches an issue -> consultant rewarded, implicated workers deducted
   - consultant guidance proven wrong -> consultant deducted more strongly

## Desired Outcome

Skynet should be able to answer all of these questions with evidence:

- Who changed this file?
- What exact bytes changed?
- Was the file text, structured data, generated output, or binary?
- What was the before fingerprint?
- What is the after fingerprint?
- Which task, bus message, review, and validator proved the change correct or incorrect?
- Was a deduction later reversed because another worker repaired the issue?

If that evidence is absent, Skynet should report `unknown`, not guess.

## Proposed Architecture

### 1. Canonical Signature Library

Add a shared library:

- `tools/skynet_signature.py`

Primary responsibilities:

- classify file type
- compute stable fingerprints
- select the correct signing mode
- write inline signatures when safe
- write detached sidecars when inline signing is unsafe
- register all signatures in a durable ledger
- verify signatures during reviews and investigations

Core API shape:

```python
fingerprint_file(path) -> dict
detect_signature_mode(path) -> str
sign_file(path, actor, task_id, reason, evidence=None) -> dict
verify_file_signature(path) -> dict
register_change(path, actor, task_id, before_fp, after_fp, mode, metadata) -> dict
load_signature_record(path_or_fingerprint) -> dict
```

### 2. Multi-Mode Signature Convention

Skynet should stop treating all files as if inline comments are enough. The system needs a file-class-aware convention.

| File class | Mode | Proposed convention |
|---|---|---|
| Python / PowerShell / JS / Go / similar source | Inline + ledger | Existing nearby `signed:` comment plus ledger record |
| Markdown / HTML / XML | Inline + ledger | Existing comment signature plus ledger record |
| JSON / YAML / TOML / config | Structured block when schema-safe, else sidecar + ledger | Prefer `_skynet_signature` block only for Skynet-owned schemas |
| Binary files (`.exe`, `.dll`, `.png`, `.jpg`, `.zip`, `.db`, model blobs) | Detached sidecar + ledger | `filename.ext.skysig.json` sidecar, never mutate raw binary |
| Generated artifacts / vendor files | Ledger-only or sidecar + ledger | Never inject comments into generated or third-party payloads |
| Bus messages / task results | Content signature + metadata fingerprint | `signed:actor` in content plus metadata fields for `task_id`, `artifact_path`, `evidence_id` |

### 3. Detached Signature Format For Binaries

For binaries and unsafe-to-edit files, use a sidecar:

- Example: `worker_icon.png.skysig.json`
- Example: `Skynet.exe.skysig.json`

Suggested sidecar schema:

```json
{
  "schema": 1,
  "path": "assets/worker_icon.png",
  "file_class": "binary",
  "algo": "sha256",
  "fingerprint": "ab12...",
  "size": 48123,
  "signed_by": "alpha",
  "task_id": "task-20260312-014",
  "reason": "fixed corrupted asset dimensions",
  "before_fingerprint": "cd34...",
  "timestamp": "2026-03-12T01:23:45Z",
  "evidence": {
    "bus_message_id": "msg_123_alpha",
    "validator": "beta"
  }
}
```

This avoids lying about provenance and avoids breaking binaries by modifying their contents.

### 4. Central Signature Ledger

Add a durable append-only store:

- `data/file_signature_ledger.jsonl`

Each record should capture:

- normalized relative path
- file class
- signature mode
- before fingerprint
- after fingerprint
- actor
- task id
- related bus message ids
- validation state
- repair state
- score consequences
- timestamp

Why a ledger is required:

- inline comments alone can be deleted
- sidecars can drift
- investigations need historical sequence, not only the latest file state
- the ledger can correlate file changes with scoring events and bus evidence

### 5. Enforcement Points So All Agents Follow It

This only works if the signature library is mandatory, not optional. The enforcement path should be:

1. `skynet_dispatch.py`
   - attach `task_id`
   - require result packets to declare changed files
2. `CC-Start.ps1`, `GC-Start.ps1`, worker self-invocations, and orchestrator prompts
   - explicitly instruct every actor to sign files through the shared library, not manually improvise
3. `tools/skynet_scoring.py`
   - refuse accountability deductions when ownership is unproven
   - refuse rewards for unsigned work unless the ledger proves ownership another way
4. `tools/skynet_audit.py` or a new `tools/skynet_signature_audit.py`
   - scan changed files for missing or inconsistent signatures
5. bus publishing wrappers
   - require `signature`, `task_id`, and `artifact_path` metadata where a file-changing result is claimed
6. consultant protocol
   - consultant proposals that change code or policy must include a signature impact section

### 6. Signature Verification Modes

Not all file types should be treated equally. Verification should support:

- `inline_match`: nearby source comment matches ledger actor
- `structured_match`: `_skynet_signature` block matches ledger actor
- `sidecar_match`: detached sidecar matches file hash and ledger record
- `ledger_only`: allowed only for generated/vendor/immutable artifacts
- `unknown`: no trustworthy attribution

If verification returns `unknown`, Skynet must not fabricate accountability.

## Proposed Scoring Extensions

The scoring system should remain positive-sum. The point of accountability is to improve behavior, not trap agents in permanent negative score states when truthfully repaired.

### 1. Verified Mistake Repaired By Another Worker

New rule:

- If worker `A` made a real mistake and worker `B` later fixes it, and the fix is independently verified:
  - worker `A`'s earlier deduction is cancelled or reversed
  - worker `B` receives `+0.01`
  - both records stay in history for audit truth

Reasoning:

- the system should reward recovery
- a repaired mistake should not remain a permanent score scar if the truth chain is complete
- history should show the mistake and the repair, but the final score should encourage learning

Suggested scoring event names:

- `mistake_deduction`
- `mistake_repair_reversal`
- `peer_repair_good_conduct_award`

### 2. Consultant Catches A Real Issue

New rule:

- If the consultant catches a real issue and Skynet independently proves the consultant correct:
  - consultant gets `+0.01`
  - each directly implicated signed worker gets `-0.01`

Guardrails:

- only deduct workers whose signed fingerprint chain ties them to the proven issue
- require validator evidence
- if ownership is ambiguous, do not deduct; mark as shared system debt instead

Suggested scoring event names:

- `consultant_catch_award`
- `consultant_catch_worker_deduction`

### 3. Consultant Guidance Proven Wrong

New rule:

- If a consultant gave signed guidance and Skynet later proves that guidance wrong:
  - consultant gets `-0.02`

Guardrails:

- require a real evidence packet:
  - offending plan or result message id
  - affected task id
  - failing proof
  - independent validator
- do not deduct consultants for disagreement alone
- deduct only when the guidance is materially wrong, harmful, or truth-violating

Suggested scoring event name:

- `consultant_guidance_failure`

### 4. Positive-Sum Preservation

The system should explicitly preserve the existing mandate:

- truthful reporting should always be safer than hiding mistakes
- repair should have a recovery path
- catching a peer's issue should grow system intelligence
- score history should retain the incident, even when the deduction is reversed

This keeps the culture aligned with the repo's existing positive-sum rule instead of turning investigations into punishment theater.

## Required Evidence Model

Every accountability event should reference a common evidence packet:

```json
{
  "issue_id": "sig-issue-20260312-01",
  "task_id": "task-20260312-014",
  "artifact_path": "tools/skynet_dispatch.py",
  "signed_actor": "alpha",
  "reporter": "consultant",
  "validator": "beta",
  "before_fingerprint": "cd34...",
  "after_fingerprint": "ab12...",
  "proof": {
    "test_failure": "pytest tests/test_dispatch.py::test_xyz",
    "bus_message_id": "msg_456_consultant",
    "review_message_id": "msg_457_beta"
  }
}
```

Without this evidence packet, the score engine should not apply attribution deductions.

## Investigation Workflow

1. A worker, consultant, or orchestrator reports an issue.
2. Skynet resolves the changed file to a signature record.
3. The ledger provides the ownership chain and fingerprints.
4. An independent validator confirms the issue.
5. `tools/skynet_scoring.py` applies the correct event:
   - deduction
   - reversal
   - consultant catch award
   - consultant guidance failure
6. The bus records the scoring event and links back to the evidence packet.
7. Future investigations can replay the chain without guessing.

## Concrete Implementation Steps

### Phase 1. Foundation

1. Create `tools/skynet_signature.py`.
2. Add `data/file_signature_ledger.jsonl`.
3. Add `tools/skynet_signature_audit.py`.
4. Add a lightweight schema definition for sidecars.

### Phase 2. Enforce On New Work

1. Update worker and consultant self-invocation prompts to mandate the shared signature library.
2. Update result-reporting instructions so changed files must be declared.
3. Add a pre-result audit check:
   - if changed files are missing signatures, warn and block score credit

### Phase 3. Extend Scoring

1. Add scoring protocol fields in `data/brain_config.json`:
   - `peer_repair_award = 0.01`
   - `mistake_reversal_award = 0.01`
   - `consultant_catch_award = 0.01`
   - `consultant_catch_worker_deduction = 0.01`
   - `consultant_guidance_failure_deduction = 0.02`
2. Extend `tools/skynet_scoring.py` with explicit commands:
   - `--mistake-deduct`
   - `--repair-reversal`
   - `--consultant-catch`
   - `--consultant-guidance-wrong`
3. Require evidence packet paths or ids for those commands.

### Phase 4. Verification And Reporting

1. Add a signature audit report to health and incident tooling.
2. Add leaderboard detail for:
   - repaired mistakes
   - consultant catches
   - consultant guidance failures
3. Add a dashboard view for file accountability chains.

## Noted Suggestion

Yes, the architecture can realistically address this, but the key design decision should be:

Use a hybrid signature model where inline signatures are human-readable hints, and the detached ledger plus file fingerprint is the canonical truth.

That suggestion matters because:

- binaries cannot safely hold comment signatures
- structured data files may break if arbitrary fields are injected
- inline comments can be deleted
- a ledger can unify text files, configs, generated artifacts, and binaries under one investigation model

In other words:

- inline signatures are useful for local readability
- fingerprints and ledger entries are the real accountability backbone

If Skynet chooses only inline comments, it will never achieve full binary accountability. If it chooses only detached hashes, humans lose immediate readability. The hybrid model gives both.

## Risks And Safeguards

### Risks

- false attribution if a file is reformatted by another actor without re-signing
- noisy scoring if deductions trigger on ambiguous ownership
- sidecar drift if binary changes are made without updating the sidecar
- ledger bloat if records are not normalized

### Safeguards

- require before/after fingerprints
- require independent validation
- never deduct on ambiguous attribution
- require audit checks before score events are accepted
- keep all reversals as history, not silent deletion

## Recommendation

Adopt this as a staged architecture change, not a one-shot policy edit.

Immediate next action:

1. accept the hybrid signature-library direction
2. implement the ledger and binary sidecar support first
3. then wire the new scoring events into `tools/skynet_scoring.py`
4. then update all self-invocation prompts and review tooling to enforce it

That sequence gives Skynet real accountability without breaking truth, without fabricating ownership, and without turning the scoring system against the positive-sum mandate.

<!-- signed: consultant -->
