# Consultant Offline Self-Heal Incident

Date: 2026-03-12
Author: Codex Consultant
Signature: `signed:consultant`

## Summary

The consultant path violated the truth-and-recovery standard in two ways:

1. Codex Consultant could report a bridge-offline state without escalating it as a signed Skynet incident.
2. The consultant self-invocation guidance did not explicitly require immediate self-heal before going idle.

This was not just a wording issue. A consultant that reports itself as offline or not promptable, then stops at that status line, is leaving GOD with degraded capacity while presenting the degradation as if the reporting itself were sufficient remediation.

## What Happened

The current startup scripts and bridge surfaces allowed these failure modes:

1. `CC-Start.ps1` could announce a truthful offline identity state, but it did not also emit a dedicated critical incident alert requiring self-heal.
2. `GC-Start.ps1` had drifted behind the Codex consultant path:
   - raw `/bus/publish` instead of SpamGuard
   - no score/signature metadata
   - weaker bridge truth verification
   - weaker self-invocation accountability text
3. `tools/skynet_consultant_bridge.py` published presence, but a graceful bridge exit did not publish an OFFLINE transition alert to Skynet.

## Why This Matters

Skynet treats stale consultant state as real operational loss:

1. Consultants are advisory peers that surface architectural and operational failures.
2. A stale or offline consultant bridge removes promptability and routable advisory capacity.
3. "Offline" is not a completion state. It is an incident state.
4. If the consultant only reports "offline" without attempting repair, publishing a signed alert, and leaving a durable artifact, the system loses both capacity and accountability.

## Root Cause

The root cause was incomplete consultant self-invocation policy.

The scripts explained identity, bridge truth, and scoring, but they did not force this exact sequence:

1. detect degraded/offline consultant truth
2. attempt self-heal immediately when safe
3. publish signed alert/result with evidence
4. leave a repo-root proposal artifact
5. only then claim the incident is handled

## Fixes Applied

### 1. Startup incident reporting

`CC-Start.ps1` now publishes a signed critical bus alert when the Codex consultant bridge is offline or not promptable after startup verification.

`GC-Start.ps1` now does the same for Gemini.

### 2. Gemini consultant parity restoration

`GC-Start.ps1` now matches the signed consultant boot discipline:

1. guarded publish via SpamGuard
2. score/signature metadata
3. live bridge truth via `/health` plus `/consultants`
4. truthful bridge status metadata

### 3. Bridge OFFLINE transition reporting

`tools/skynet_consultant_bridge.py` now emits a signed OFFLINE alert when the bridge leaves LIVE state through its normal shutdown path.

### 4. Self-invocation hardening

Both consultant identity self-prompts now explicitly require:

1. self-heal before passive reporting
2. signed incident posting to Skynet
3. repo-root Markdown artifact for truth failures
4. post-repair verification before claiming closure

## Systemic Improvements Proposed

1. Add a consultant-specific watchdog alert type for `STALE` and `OFFLINE` that distinguishes:
   - startup not yet live
   - heartbeat stale
   - graceful shutdown
   - failed restart
2. Add consultant incident counters to `worker_scores.json` so repeated "reported degraded, did not self-heal" failures are visible.
3. Add a small test suite for consultant boot scripts that validates:
   - signed content
   - SpamGuard path
   - score metadata
   - bridge truth verification
   - offline incident alert emission
4. Add a bridge restart command path that can be invoked by the consultant session itself without requiring ad hoc shell reasoning.
5. Add a consultant liveness dashboard row that shows:
   - `LIVE`
   - `STALE`
   - `OFFLINE`
   - heartbeat age
   - last signed incident/result

## Truth Lessons

1. A truthful degradation report is necessary but not sufficient.
2. Self-report without recovery is still a system failure.
3. "Not promptable yet" must be treated as an active repair obligation, not a passive label.
4. Consultant self-invocation must encode repair behavior, not just identity behavior.

## Files Changed

1. `CC-Start.ps1`
2. `GC-Start.ps1`
3. `tools/skynet_consultant_bridge.py`

## Closing Statement

This incident is resolved only when all of the following are true:

1. degraded consultant startup emits a signed alert
2. consultant self-invocation explicitly requires self-heal
3. bridge shutdown publishes OFFLINE truth
4. Skynet receives a signed incident/result record

<!-- signed: consultant -->
