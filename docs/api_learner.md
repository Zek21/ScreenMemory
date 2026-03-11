# Learner API Endpoints

The GOD Console (`god_console.py`, default port 8421) exposes two learner-related endpoints for monitoring the learning daemon and its telemetry.

---

## `GET /learner/health`

Check the learner daemon's liveness and staleness.

### Response Schema

| Field               | Type      | Description                                                                                           |
|---------------------|-----------|-------------------------------------------------------------------------------------------------------|
| `status`            | `string`  | One of `"running"`, `"stopped"`, or `"error"`.                                                        |
| `pid`               | `int\|null` | Process ID of the learner daemon. `null` when stopped/error.                                         |
| `episodes_processed`| `int`     | Total episodes the daemon has processed since start. Present only when `status="running"`.            |
| `total_learnings`   | `int`     | Total learnings extracted. Present only when `status="running"`.                                      |
| `last_run`          | `float\|null` | Unix timestamp of the daemon's last processing cycle. `null` if never run.                        |
| `started_at`        | `float\|null` | Unix timestamp when the daemon was started. `null` if unknown.                                    |
| `stale`             | `bool`    | `true` if no episodes processed for >300 seconds, or if daemon is stopped/errored.                    |
| `stale_seconds`     | `int`     | Seconds since last run. `-1` if `last_run` is unknown or daemon is stopped.                           |
| `error`             | `string`  | Error message. Present only when `status="error"`.                                                    |

### Example Responses

**Running and healthy:**
```json
{
  "status": "running",
  "pid": 12345,
  "episodes_processed": 42,
  "total_learnings": 18,
  "last_run": 1741671900.5,
  "started_at": 1741668300.0,
  "stale": false,
  "stale_seconds": 120
}
```

**Running but stale (no episodes for >300s):**
```json
{
  "status": "running",
  "pid": 12345,
  "episodes_processed": 42,
  "total_learnings": 18,
  "last_run": 1741671200.0,
  "started_at": 1741668300.0,
  "stale": true,
  "stale_seconds": 820
}
```

**Stopped:**
```json
{
  "status": "stopped",
  "pid": null,
  "stale": true,
  "stale_seconds": -1
}
```

### Error Codes

| HTTP Status | Meaning                                                 |
|-------------|---------------------------------------------------------|
| 200         | Always returned. Check `status` field for actual state. |

### Cache Behavior

No caching. Each request probes the PID file (`data/learner.pid`) and state file (`data/learner_state.json`) directly.

---

## `GET /learner/metrics`

Detailed learning telemetry with episode counts, outcome breakdowns, fact statistics, and hourly sparkline data.

### Response Schema

| Field                | Type          | Description                                                                                      |
|----------------------|---------------|--------------------------------------------------------------------------------------------------|
| `timestamp`          | `float`       | Unix timestamp when the metrics were collected.                                                  |
| `total_episodes`     | `int`         | Total number of episodes in `data/learning_episodes.json`.                                       |
| `by_outcome`         | `object`      | Outcome breakdown: `{"success": int, "failure": int, "unknown": int}`.                           |
| `last_episode_ts`    | `string\|float\|null` | Timestamp of the most recent episode (ISO string or Unix float). `null` if no episodes.  |
| `last_episode_worker`| `string\|null` | Name of the worker that produced the last episode. `null` if no episodes.                       |
| `sparkline_hourly`   | `int[]`       | Array of episode counts per hour, oldest-first. Up to 24 entries. Empty array if no episodes.    |
| `total_facts`        | `int`         | Total facts stored in the LearningStore (SQLite). `0` if store unavailable.                      |
| `avg_confidence`     | `float`       | Average confidence of stored facts, rounded to 3 decimals. `0.0` if store unavailable.           |
| `by_category`        | `object`      | Fact count by category from LearningStore. `{}` if store unavailable.                            |
| `daemon_status`      | `string`      | One of `"running"`, `"stopped"`, or `"error"`. Same PID check as `/learner/health`.              |

### Example Response

**With data:**
```json
{
  "timestamp": 1741672000.123,
  "total_episodes": 87,
  "by_outcome": {
    "success": 52,
    "failure": 12,
    "unknown": 23
  },
  "last_episode_ts": "2026-03-11T05:30:00+00:00",
  "last_episode_worker": "alpha",
  "sparkline_hourly": [3, 5, 12, 8, 6, 2, 0, 1],
  "total_facts": 134,
  "avg_confidence": 0.742,
  "by_category": {
    "dispatch": 45,
    "code": 32,
    "system": 57
  },
  "daemon_status": "running"
}
```

**Empty state:**
```json
{
  "timestamp": 1741672000.0,
  "total_episodes": 0,
  "by_outcome": {
    "success": 0,
    "failure": 0,
    "unknown": 0
  },
  "last_episode_ts": null,
  "last_episode_worker": null,
  "sparkline_hourly": [],
  "total_facts": 0,
  "avg_confidence": 0.0,
  "by_category": {},
  "daemon_status": "stopped"
}
```

### Error Response

On internal error, returns HTTP 200 with degraded data:
```json
{
  "error": "error message",
  "total_episodes": 0,
  "by_outcome": {"success": 0, "failure": 0, "unknown": 0},
  "sparkline_hourly": [],
  "total_facts": 0,
  "daemon_status": "error",
  "timestamp": 1741672000.0
}
```

### Error Codes

| HTTP Status | Meaning                                                               |
|-------------|-----------------------------------------------------------------------|
| 200         | Always returned (even on error). Check `error` field for issues.      |

### Cache Behavior

No caching. Each request reads `data/learning_episodes.json` from disk and queries the LearningStore SQLite database directly. For high-frequency polling, the dashboard uses an 8-second interval.

---

## Data Sources

| File / Store                    | Used By          | Description                                        |
|---------------------------------|------------------|----------------------------------------------------|
| `data/learner.pid`              | Both endpoints   | PID of the running learner daemon                  |
| `data/learner_state.json`       | `/learner/health`| Daemon state: `total_processed`, `total_learnings`, `last_run`, `started_at` |
| `data/learning_episodes.json`   | `/learner/metrics`| Array of episode records with outcomes and timestamps |
| `core/learning_store.py` (SQLite) | `/learner/metrics`| Fact storage with confidence scores and categories |

## Dashboard Integration

- **Header indicator**: Polls `/learner/health` every 15 seconds. Shows green dot (●) when healthy, red dot (●) when stale (>300s without episodes), grey circle (○) when stopped.
- **Learning card**: Polls `/learner/metrics` every 8 seconds. Shows episode counts, outcome bar, sparkline, fact totals, and daemon status.
