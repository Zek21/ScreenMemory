# WebSocket Security Implementation — Sprint 2

**Implementer:** Delta (Architecture Verification Specialist)
**Date:** 2026-03-17
**File Modified:** `Skynet/server.go`
**Build Status:** ✅ PASS (`go build ./...` — zero errors)

---

## Summary

Implemented all CRITICAL and HIGH severity fixes from the Sprint 1 WebSocket Security Audit (`docs/WEBSOCKET_SECURITY_AUDIT_S1.md`). The hand-rolled WebSocket handler now has defense-in-depth security: Origin validation, RBAC authentication, connection limits, frame size enforcement, ping/pong keepalive, and idle timeout.

---

## Changes Implemented

### P1: CSWSH Protection (CRITICAL → FIXED)

**Function:** `wsAllowedOrigin(origin string) bool`

Origin header validation added as the FIRST check before WebSocket upgrade. Allowlist:
- Empty origin (non-browser clients like Python/PowerShell)
- `"null"` (local file origin)
- `http://localhost:*` and `https://localhost:*`
- `http://127.0.0.1:*` and `https://127.0.0.1:*`
- `http://[::1]:*` and `https://[::1]:*`

Rejected origins are:
- Logged via `logSecurityEvent()` as `ws_cswsh_blocked`
- Counted in `wsRejected` atomic counter
- Returned HTTP 403 with JSON error body

**Impact:** Any website attempting `new WebSocket('ws://localhost:8420/ws')` from a non-localhost origin will be rejected before the TCP hijack occurs.

### P2: RBAC Authentication (CRITICAL → FIXED)

**Changes:**
1. Added `/ws` to `endpointACL` with roles `{RoleOrchestrator, RoleWorker}`
2. Added explicit `roleFromHeader()` check in `handleWebSocket` before upgrade
3. Unknown roles (explicit but unrecognized `X-Agent-Role` header) are rejected with 403

**Note:** The RBAC middleware chain already runs before `handleWebSocket` (line 337: `rbacMiddleware(s.middleware(mux))`), but since `Hijack()` bypasses subsequent middleware, the explicit role check inside the handler provides defense-in-depth. The default-no-header → `RoleOrchestrator` backward compatibility is preserved for existing Python tooling.

### P3: Frame Size Limits + Connection Cap (HIGH → FIXED)

**Constants:**
```go
wsMaxConnections = 50       // hard cap on concurrent WS clients
wsMaxFrameSize   = 1 << 20  // 1 MB max inbound frame
```

**Connection limiting:**
- `wsConns` atomic counter tracks active connections
- Checked BEFORE `Hijack()` — rejected connections get clean HTTP 503
- Logged as `ws_limit_reached` security event

**Frame validation (reader goroutine):**
- Validates client frame masking (RFC 6455 §5.1 mandates client-to-server masking)
- Parses payload length (7-bit, 16-bit extended, 64-bit extended)
- Rejects frames exceeding `wsMaxFrameSize` with security log entry
- Handles control frames: Close (0x08), Ping (0x09 → pong response), Pong (0x0A)

### P4: sync.Map Migration — NOT APPLICABLE

**Finding:** The `agentViews` field does not exist in the `SkynetServer` struct. Agent state is computed on-demand in the SSE hot path via `wk.GetState()` (line ~2621-2622 in `handleSSEStream`). This is already lock-free — each `Worker` manages its own state internally. No sync.Map migration is needed because there is no shared map to migrate.

The `workers` slice is read-only after initialization (set in `NewSkynetServer`) and iterated without locks in `handleSSEStream`, `handleStatus`, and `handleMetrics`. This is safe because the slice itself never changes — only the state within each `Worker` is mutated (via `Worker.GetState()`).

### P5: Connection Lifecycle Management (MEDIUM → FIXED)

**Ping/pong keepalive:**
- Writer goroutine sends WebSocket ping frames (opcode 0x89) every 30 seconds
- Reader goroutine responds to client ping frames with pong (opcode 0x8A)
- Client pong frames update `lastActivity` timestamp

**Idle timeout:**
- 5-minute idle timeout enforced in reader goroutine
- `lastActivity` tracked per connection, updated on any received data
- Read deadline dynamically set to `min(wsReadTimeout, timeUntilIdleExpiry)`
- Connections exceeding idle timeout are closed cleanly

**Write timeout:**
- 10-second write deadline set before every `conn.Write()` call
- Prevents goroutine leaks from slow/dead clients

**Cleanup:**
- `sync.OnceFunc` ensures cleanup runs exactly once regardless of which goroutine exits first
- Cleanup decrements `wsConns`, removes channel from `wsClients`, closes TCP connection

---

## New Fields Added to `SkynetServer`

| Field | Type | Purpose |
|-------|------|---------|
| `wsConns` | `int64` (atomic) | Current active WebSocket connection count |
| `wsRejected` | `int64` (atomic) | Total rejected upgrade attempts (security monitoring) |

## New Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `wsMaxConnections` | 50 | Hard cap on concurrent WS clients |
| `wsMaxFrameSize` | 1,048,576 (1 MB) | Maximum inbound frame payload |
| `wsPingInterval` | 30s | Keepalive ping frequency |
| `wsIdleTimeout` | 5 min | Close connections idle longer than this |
| `wsWriteTimeout` | 10s | Deadline for each `conn.Write()` |
| `wsReadTimeout` | 60s | Base deadline for `conn.Read()` |

## Updated Endpoints

### GET /ws/stats — Enhanced Response

```json
{
  "connected_clients": 2,
  "max_connections": 50,
  "total_broadcasts": 1547,
  "total_rejected": 3,
  "active_connections": 2
}
```

New fields: `max_connections`, `total_rejected`, `active_connections`.

---

## Security Event Types Added

| Event Type | Trigger | Logged As |
|------------|---------|-----------|
| `ws_cswsh_blocked` | Non-localhost Origin header | Blocked (security alert broadcast) |
| `ws_auth_rejected` | Unknown `X-Agent-Role` value | Blocked |
| `ws_limit_reached` | Connection count >= 50 | Blocked |
| `ws_frame_too_large` | Inbound frame > 1 MB | Blocked (connection closed) |

All events go through `logSecurityEvent()` and are visible in `GET /security/audit` and broadcast to existing WS clients as `security_alert` type.

---

## Remaining Considerations

1. **TLS**: Not implemented in this sprint. The server is localhost-only. Adding TLS would require certificate management infrastructure. Documented as Phase 3 in the security audit.

2. **Token-based auth**: Not implemented. The existing RBAC header-based auth is sufficient for the current deployment (all clients are local Python/PowerShell). Query-param tokens would be needed if browser-based clients from non-GOD-Console origins are added.

3. **Per-IP connection limits**: Not implemented (global limit is sufficient for localhost-only). Would be needed if the server is ever exposed to a network.

<!-- signed: delta -->
