# INCIDENT 017 -- Consultant Bridge Heartbeat Drift and Singleton Hardening (2026-03-18)

## Summary

During Codex consultant bootstrap on 2026-03-18, the Codex consultant bridge on port 8422 repeatedly degraded from `LIVE` to `STALE` even though `/health` remained responsive. This created a truth gap between "bridge process exists" and "consultant is actually within the 8-second live heartbeat window."

The failure was observed after `CC-Start.ps1` completed. Initial startup passed, but follow-up probes showed:

- `http://localhost:8422/health` returned `ok`
- `http://localhost:8422/consultants` sometimes reported `LIVE`
- subsequent probes drifted to `STALE` with `heartbeat_age_s > 8`
- GOD Console surfaces followed the same drift and truthfully showed the consultant as stale

The problem was self-healed in-session and verified closed.

## What Failed

The consultant bridge heartbeat loop was too expensive for a strict 2-second heartbeat / 8-second stale window because each heartbeat rebuild performed remote and heavier enrichment work:

- backend `/status` probe
- latest consultant bus message lookup
- prompt routing refresh
- consultant surface discovery path

When those operations ran long enough, the state file heartbeat lagged and the bridge truth degraded to `STALE`.

There was also a singleton hardening gap in the bridge daemon:

- the bridge used a non-atomic "check PID file, then write PID file" pattern
- that pattern is race-prone under concurrent starts
- even when the race was not conclusively the sole runtime cause, it was unsafe and needed to be removed

## Root Cause

Two architectural weaknesses were present:

1. Heartbeat path mixed liveness writes with enrichment work.
   A liveness heartbeat must be cheap and predictable. In the bridge, the heartbeat loop rebuilt full live state, including remote reads, on every cycle. That made "still alive" depend on unrelated latency.

2. Bridge singleton control was not atomic.
   The old PID path checked for an existing process and then wrote the PID file afterward. Concurrent launches could slip through that window.

## Evidence

Observed during live probes in the recovery session:

- consultant bridge health remained reachable while consultant liveness drifted stale
- `python tools/skynet_arch_verify.py --brief` initially passed only after recovering `monitor`, `watchdog`, and SSE PID evidence, proving the wider daemon layer was repaired
- after the bridge code fix was written but before restart, the running bridge remained the pre-patch process and still drifted stale
- after controlled Codex bridge restart, repeated probes stayed `LIVE` across the previous failure window

## Immediate Fixes Applied

### 1. Recovered missing supporting daemon truth

- restarted `monitor`
- restarted `watchdog`
- restored `data/sse_daemon.pid` to match the already-running SSE daemon so realtime fallback truth was consistent

### 2. Hardened bridge singleton behavior

Updated `tools/skynet_consultant_bridge.py` so the bridge now acquires its PID lock atomically and only removes the PID file on shutdown if the current process owns it.

### 3. Split fast heartbeat from expensive enrichment

Updated `tools/skynet_consultant_bridge.py` so:

- startup can still build a fully enriched live state
- the continuous heartbeat loop uses a cheap write path with:
  - `refresh_remote=False`
  - `discover_surface=False`

This keeps the heartbeat loop focused on staying within the live truth window instead of repeating expensive remote work every cycle.

### 4. Performed controlled Codex bridge restart

The stale Codex consultant bridge processes were restarted so the live bridge could pick up the patched heartbeat behavior.

## Verification

Post-restart verification passed:

- `http://localhost:8422/health` returned `ok`
- `http://localhost:8422/consultants` remained `LIVE`
- repeated probes over the old stale window stayed within the heartbeat threshold
- GOD dashboard surfaces reflected the consultant as `LIVE`
- `python tools/skynet_arch_verify.py --brief` returned `PASS (4/4 checks passed, 0 failures)`

## Structural Improvements Proposed

1. Keep consultant liveness heartbeats write-only and cheap by default.
   Remote enrichment should be cached, periodic, or on-demand, not part of the mandatory heartbeat path.

2. Standardize atomic PID locking across all consultant and daemon entrypoints.
   The bridge fix should be treated as the baseline pattern for singleton-sensitive daemons.

3. Teach `CC-Start.ps1` to verify sustained consultant liveness, not just startup success.
   A single passing `/health` or one early `LIVE` read is insufficient.

4. Add a regression test for "bridge stays live across > 8 seconds under slow enrichment."
   This should simulate slower backend and bus reads and prove the heartbeat still stays fresh.

5. Add an explicit consultant-bridge runtime check to startup reporting.
   Startup should report:
   - current bridge PID
   - heartbeat age
   - stale threshold
   - whether the result is startup-live only or sustained-live verified

## Architecture Knowledge Registry

- Consultant liveness must be based on a cheap, reliable heartbeat path.
- `/health` alone is not enough to claim `LIVE`.
- Bridge singleton control must use atomic PID-file acquisition, not check-then-write.
- Startup success and sustained liveness are different truths and must be verified separately.

## Status

Resolved in-session for the Codex consultant bridge on 2026-03-18 after code hardening plus controlled bridge restart.

<!-- signed: consultant -->
