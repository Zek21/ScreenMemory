# Consultant Startup Truth Mismatch - 2026-03-14 08:49 Asia/Manila

signed:consultant

## Summary

`CC-Start.ps1` completed successfully for the Codex Consultant and the consultant bridge on port `8422` is live. During the same startup window, worker truth diverged across system surfaces:

- `CC-Start.ps1` reported all four worker HWNDs as dead / not visible.
- Backend `GET /status` still reported `alpha`, `beta`, `gamma`, and `delta` as `IDLE`.
- `data/workers.json` was rewritten to an empty worker registry at `2026-03-14 08:49:07 +08:00`.
- `data/realtime.json` remained stale with `last_update` at `2026-03-14T00:42:50.139645+00:00`.

This means the system is currently overstating worker availability on some surfaces. The consultant can truthfully certify the consultant bridge, but cannot truthfully certify worker liveness.

## Evidence

### Verified live

- `http://127.0.0.1:8422/health` returned `{"status":"ok","service":"consultant-bridge",...}` after `CC-Start`.
- `http://127.0.0.1:8422/consultants` reported:
  - `id=consultant`
  - `status=LIVE`
  - `accepts_prompts=true`
  - `prompt_transport=bridge_queue`
  - `heartbeat_age_s ~= 1.2`
- Bus confirmation showed:
  - consultant `identity_ack`
  - consultant consumer daemon start

### Verified degraded

- `CC-Start.ps1` output at `08:48:32` reported:
  - `Worker ALPHA: HWND=9701650 -- DEAD`
  - `Worker BETA: HWND=198310 -- DEAD`
  - `Worker GAMMA: HWND=198274 -- DEAD`
  - `Worker DELTA: HWND=1050290 -- DEAD`
- `GET http://localhost:8420/status` shortly after still reported all four workers as `IDLE` with stale heartbeats (`08:47:26`).
- `data/workers.json` contents after startup were:
  - `"workers": []`
  - `"created": "2026-03-14T08:49:07.431165"`
- `data/realtime.json` still reflected older worker heartbeats (`08:42:39`) and `last_update` `2026-03-14T00:42:50.139645+00:00`.
- `data/chat_open_failures.json` recorded `Dropdown open failed: NO_ROOT` at `2026-03-14T08:49:53.1924715+08:00`.

## Likely Failure Shape

The strongest current hypothesis is not "workers are healthy." It is:

1. Previous worker HWNDs were present long enough for `CC-Start.ps1` to read and classify them as dead.
2. A later startup or recovery path rewrote `data/workers.json` with an empty worker list.
3. Backend and/or realtime state continued advertising prior worker availability from stale memory rather than current window truth.

That combination produces a direct Truth Protocol violation on worker-facing status surfaces even though the consultant bridge itself is healthy.

## Safe Next Actions

1. Trace the process that rewrote `data/workers.json` at `08:49:07 +08:00`.
2. Prevent empty worker-registry writes unless the caller is explicitly performing a fresh boot with no workers discovered.
3. Gate worker availability in `/status` and related live surfaces on current worker registry plus live window truth, not stale in-memory state alone.
4. Run orchestrator-owned worker recovery via `Orch-Start.ps1` or the designated worker bootstrap flow once ownership is confirmed.
5. Add a regression test covering this exact state split:
   - dead or missing worker HWNDs
   - empty `workers.json`
   - backend must not still claim all workers are available

## Consultant Position

The consultant has completed `CC-Start`, verified the consultant bridge truthfully, and surfaced the worker truth mismatch. Worker-window recovery remains an orchestrator / worker bootstrap concern, not a consultant delivery-route claim.
