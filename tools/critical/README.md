# Critical Tool Calls

This folder is the consolidated entrypoint surface for the operational tools that are critical to live Skynet control, worker safety, and prompt-path diagnosis.

The real implementations remain in `tools/`. These wrappers exist so operators can use one stable folder without breaking existing imports, daemons, or scripts that already reference the canonical files.

## What Is In Here

| Wrapper | Canonical script | Purpose |
| --- | --- | --- |
| `tools/critical/monitor.py` | `tools/skynet_monitor.py` | Worker window health, model drift, heartbeats, stuck `PROCESSING` recovery |
| `tools/critical/watchdog.py` | `tools/skynet_watchdog.py` | Service watchdog and daemon/service restart supervision |
| `tools/critical/realtime.py` | `tools/skynet_realtime.py` | Real-time worker state collection and result tracking |
| `tools/critical/bus_relay.py` | `tools/skynet_bus_relay.py` | Relay hold queue and hourly orchestrator digest |
| `tools/critical/worker_check.py` | `tools/skynet_worker_check.py` | Fast worker state and idle/busy inspection |
| `tools/critical/stuck_detector.py` | `tools/skynet_stuck_detector.py` | Deep stuck-worker diagnosis |
| `tools/critical/draft_inspector.py` | `tools/skynet_draft_inspector.py` | Find workers left in `TYPING` with unsent drafts and dissect likely source |
| `tools/critical/convene_gate.py` | `tools/convene_gate.py` | Convene-gate monitoring and consolidated `elevated_digest` delivery |

## Why This Folder Exists

- One place for the critical operational calls
- Safer operator workflow during incidents
- No risky file moves that would break existing callers
- Stable entrypoints that can be documented and extended without changing the canonical scripts

## Operator Usage

### Worker health and drift

```powershell
python tools/critical/monitor.py --status
python tools/critical/monitor.py --once
python tools/critical/monitor.py
```

Use this when you need the real worker health snapshot, model/agent drift checks, or stuck `PROCESSING` auto-recovery.

### Hanging draft inspection

```powershell
python tools/critical/draft_inspector.py scan
python tools/critical/draft_inspector.py scan --worker beta --json
python tools/critical/draft_inspector.py watch --interval 5
```

This is the tool for the exact failure mode where a worker is left in `TYPING` with an unsent prompt sitting in the input box. It shows:

- live UIA state
- visible draft preview
- likely source classification such as `bus_relay_convene_gate_proposal`
- bus correlation when a gate id, sender/topic, or report preview matches recent bus traffic
- whether the worker monitor is actually alive or stale

### Fast worker state checks

```powershell
python tools/critical/worker_check.py scan
python tools/critical/worker_check.py busy
python tools/critical/worker_check.py idle
```

Use this for a quick operational read when you do not need full monitor output.

### Deep stuck diagnosis

```powershell
python tools/critical/stuck_detector.py
```

Use this when a worker is repeatedly hanging or oscillating and the simple monitor view is not enough.

### Bus relay

```powershell
python tools/critical/bus_relay.py --status
python tools/critical/bus_relay.py --dry-run
python tools/critical/bus_relay.py
```

Use this to inspect or run the relay hold path. Relayable worker/convene traffic is no longer typed directly into worker windows; it is held in the relay queue and sent to the orchestrator as an hourly digest for explicit action.

### Realtime collection

```powershell
python tools/critical/realtime.py --status
python tools/critical/realtime.py --monitor
```

Use this to maintain or inspect the realtime state file used by orchestration and result waiting.

### Watchdog

```powershell
python tools/critical/watchdog.py status
python tools/critical/watchdog.py start
```

Use this when you need the service watchdog state or need to start the watchdog explicitly.

### Convene gate

```powershell
python tools/critical/convene_gate.py --stats
python tools/critical/convene_gate.py --monitor
```

Use this to inspect or run the convene consensus gate and the consolidated `elevated_digest` delivery path.

## Draft Inspector Details

`tools/skynet_draft_inspector.py` is intended to answer:

- Which workers are currently in `TYPING`?
- What text is visible in the unsent draft?
- Does the draft look like a bus relay, convene proposal, or reply template?
- Can it be correlated to a recent bus message?
- Is the worker monitor alive, stale, or missing?

This is intentionally diagnosis-only. It does not auto-submit, auto-clear, or mutate worker input.

## Design Rule

When a tool here needs deeper changes, update the canonical implementation in `tools/` first, then keep the wrapper stable. Do not fork business logic into this folder.
