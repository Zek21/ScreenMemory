# Signature Library and Accountability Architecture Plan

Date: 2026-03-12
Author: Codex Consultant
Signature: `signed:consultant`

## Purpose

Skynet already has a signature accountability rule for text files and bus results. The next step is to make accountability durable, machine-readable, and universal across:

1. text files
2. configuration files
3. generated artifacts
4. binaries and non-commentable assets
5. score awards, deductions, reversals, and fix handoffs

The goal is not punishment-first. The goal is positive-sum truth: make every edit attributable, every fix recoverable, every investigation grounded in evidence, and every honest correction rewardable.

## Current State

Current strengths already present in the repo:

1. inline signature convention exists in `AGENTS.md`
2. bus results already require `signed:worker_name`
3. `tools/skynet_scoring.py` already supports awards, deductions, reversals, proactive clears, bug filing, and independent validation
4. fair-deduction rules already require dispatch evidence

Current gaps:

1. signatures are mostly human-readable, not centrally indexed
2. binaries and non-commentable files do not have a first-class signature system
3. investigations still require too much manual blame reconstruction
4. deduction cancellation and fixer reward rules are not explicit for "worker B corrected worker A's signed mistake"
5. consultant-originated findings and consultant-wrong-guidance penalties are not first-class scoring actions

## Architectural Suggestion

Create a **Signature Library + Ledger + Policy Layer** that all Skynet agents use.

### Core Components

1. `tools/skynet_signature.py`
   Purpose: canonical library for signing, fingerprinting, verifying, and attributing artifacts.
2. `data/signature_ledger.jsonl`
   Purpose: append-only audit log of every signed artifact mutation and verification event.
3. `data/signature_manifest.json`
   Purpose: latest known state per tracked artifact for quick lookup.
4. `tools/skynet_signature_guard.py`
   Purpose: validation layer run before score credit, before consultant plan acceptance, and before "DONE" claims are accepted as complete.
5. `tools/skynet_scoring.py` extension
   Purpose: add new score actions for fixer credit, deduction cancellation, consultant issue catches, and consultant wrong-guidance penalties.

## Signature Conventions

### Text Files

Keep the current inline conventions from `AGENTS.md`:

1. Python: `# signed: worker_name`
2. PowerShell: `# signed: worker_name`
3. JavaScript/TypeScript/Go: `// signed: worker_name`
4. HTML/Markdown: `<!-- signed: worker_name -->`
5. Bus results: include `signed:worker_name` in content

Add one rule:

Every signed text change must also be recorded in the signature ledger with:

1. path
2. file type
3. actor
4. task id
5. before hash
6. after hash
7. signature token
8. timestamp
9. parent mutation or previous signer reference

### Binary and Non-Commentable Files

Use **detached sidecar signatures**.

Convention:

1. Binary file remains untouched
2. Create sidecar file next to it or in manifest storage
3. Sidecar name format:
   - `artifact.ext.skysig.json`
   - or central manifest entry if colocated sidecars are undesirable

Minimum sidecar fields:

1. `path`
2. `artifact_type`
3. `mime`
4. `size_bytes`
5. `sha256`
6. `blake3` if available
7. `signed_by`
8. `task_id`
9. `created_at`
10. `previous_sha256`
11. `notes`

This gives every binary a unique fingerprint and attributable signer without corrupting the file format.

## Suggested Library API

### Signing

1. `sign_text_artifact(path, actor, task_id, before_hash, after_hash, anchors)`
2. `sign_binary_artifact(path, actor, task_id, previous_sha256=None, notes="")`
3. `record_result_signature(actor, task_id, changed_files, bus_message_id)`

### Verification

1. `verify_artifact(path)`
2. `verify_task_signatures(task_id)`
3. `find_signers(path)`
4. `find_latest_fingerprint(path)`
5. `trace_mutation_chain(path)`

### Investigation

1. `blame_artifact(path, fingerprint=None)`
2. `find_implicated_signers(path, failing_fingerprint)`
3. `find_fixer_for_failure(path, failure_id)`
4. `build_accountability_report(task_id or path)`

## Enforcement Plan

### 1. Agent-facing enforcement

Update worker and consultant self-invocation text so all agents must:

1. sign every changed text file near the actual edit
2. register binaries through the signature library
3. include changed file list in final result payload
4. run signature verification before claiming done

### 2. Result-gate enforcement

Before a result earns score credit:

1. `skynet_signature_guard.py` checks that all declared changed files have valid signatures
2. if any artifact lacks signature coverage, result is marked incomplete
3. incomplete result earns no positive credit until fixed

### 3. Investigation enforcement

When a bug or regression is reported:

1. fetch artifact signers from manifest/ledger
2. compute implicated signers from the mutation chain
3. create an evidence packet with hashes, signatures, task id, and validator
4. only then apply or reverse scoring actions

## Scoring Semantics Requested

The requested rules fit cleanly into the existing scoring system if implemented as explicit actions.

### A. Fix by another worker

If worker B fixes worker A's signed mistake and the fix is independently validated:

1. worker A's earlier deduction for that specific verified mistake is cancelled
2. worker B receives `+0.01`
3. the audit trail keeps both the original deduction and the later reversal

Implementation suggestion:

1. add scoring action `mistake_deduction_reversal`
2. add scoring action `mistake_fix_credit`
3. require:
   - original failure record id
   - implicated signer
   - fixer
   - independent validator
   - artifact path and fingerprint

### B. Consultant catches the issue

If the consultant accurately catches a signed issue:

1. consultant receives `+0.01`
2. each implicated worker signer on the faulty artifact receives `-0.01`
3. deductions stay tied to the exact failing artifact fingerprint and evidence packet

Implementation suggestion:

1. add scoring action `consultant_issue_catch`
2. add scoring action `signed_artifact_fault`
3. if two workers co-signed the implicated artifact, both can receive `-0.01`
4. if later evidence narrows the fault to only one signer, the unrelated signer's deduction is reversed

### C. Consultant proven wrong

If Skynet proves the consultant guidance wrong:

1. consultant receives `-0.02`
2. the proof packet must include:
   - consultant message id or artifact path
   - contradictory evidence
   - validating actor
   - affected artifact or workflow

Implementation suggestion:

1. add scoring action `consultant_wrong_guidance`
2. amount fixed at `0.02` unless a later rule supersedes it

## Positive-Sum Guardrails

To preserve the mandate that the system should trend positive:

1. deductions related to fixable signed mistakes should be **reversible**
2. fixer credit should be explicit and easy to earn through real correction
3. consultant catches should reward truth, not encourage shallow blame
4. every penalty event should create a paired recovery opportunity

Proposed recovery principles:

1. a signer who accepts correction and produces verified follow-up work should have a clear path back to positive
2. a fixer who improves another agent's signed artifact should be rewarded
3. the ledger should show both error and repair so Skynet learns improvement, not just failure

## Detailed Rollout

### Phase 1. Signature library

Build:

1. `tools/skynet_signature.py`
2. manifest and ledger files
3. text and binary signing helpers
4. verification CLI

### Phase 2. Scoring extensions

Add to `tools/skynet_scoring.py`:

1. `award_mistake_fix_credit()`
2. `reverse_signed_fault_deduction()`
3. `award_consultant_issue_catch()`
4. `deduct_consultant_wrong_guidance()`
5. `deduct_signed_artifact_fault()`

### Phase 3. Result-gate integration

Integrate signature verification into:

1. worker completion path
2. consultant result path
3. cross-validation result path

### Phase 4. Policy propagation

Update:

1. `AGENTS.md`
2. `.github/copilot-instructions.md`
3. worker self-invocation prompt
4. consultant self-invocation prompt

### Phase 5. Investigation tools

Add:

1. `python tools/skynet_signature.py trace <path>`
2. `python tools/skynet_signature.py blame <path>`
3. `python tools/skynet_signature.py verify-task <task_id>`
4. dashboard views for signer, fixer, validator, and current fingerprint

## Binary Handling Details

For binaries, the architecture should not rely on inline comments. Use detached fingerprints plus optional provenance packets:

1. executable builds
2. images
3. PDFs
4. archives
5. compiled frontend assets

If a binary changes again:

1. record old and new hashes
2. record signer and task id
3. link to previous sidecar or previous manifest fingerprint

That gives a real chain-of-custody even where comments are impossible.

## Risks and Mitigations

1. Risk: agents forget to declare changed files
   Mitigation: result gate rejects unsigned completion claims
2. Risk: multiple signers on same file create blame ambiguity
   Mitigation: ledger stores mutation order and per-task fingerprint chain
3. Risk: binaries explode storage size
   Mitigation: store hashes and metadata only, not binary copies
4. Risk: score system becomes punitive
   Mitigation: make reversals and fixer credits first-class, not manual exceptions

## Recommended First Implementation Slice

If Skynet wants the smallest credible first slice:

1. implement `tools/skynet_signature.py`
2. add detached binary sidecar support
3. extend `tools/skynet_scoring.py` with:
   - `mistake_fix_credit +0.01`
   - `mistake_deduction_reversal`
   - `consultant_issue_catch +0.01`
   - `signed_artifact_fault -0.01`
   - `consultant_wrong_guidance -0.02`
4. enforce signature verification before score credit

This would immediately make investigations far more defensible while preserving the positive-sum mandate.

## Closing Mandate

The signature system should not exist to hunt failures in isolation. It should exist so Skynet can do three things truthfully:

1. identify who changed what
2. prove who fixed what
3. reward honest correction faster than it punishes mistakes

That is how the architecture can remain fully accountable while still driving every agent toward positive score and better collective behavior.

<!-- signed: consultant -->
