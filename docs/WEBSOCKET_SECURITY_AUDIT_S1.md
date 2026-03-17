# WebSocket Security Audit — Sprint 1

**Auditor:** Delta (Architecture Verification Specialist)
**Date:** 2026-03-17
**Scope:** `Skynet/server.go` lines 2731-2858 — WebSocket implementation
**Severity Framework:** CRITICAL / HIGH / MEDIUM / LOW

---

## Executive Summary

The Skynet WebSocket implementation is a hand-rolled RFC 6455 handler using Go's `http.Hijacker` interface with **zero security controls**. No Origin validation, no authentication, no frame size limits, no TLS, and minimal connection lifecycle management. Any website visited by the operator can silently open `ws://localhost:8420/ws` and receive all Skynet bus broadcasts in real-time.

**Overall Security Rating: CRITICAL — 5 vulnerabilities found, 3 are CRITICAL severity.**

---

## Vulnerability Analysis

### 1. CSWSH — Cross-Site WebSocket Hijacking

**Severity: 🔴 CRITICAL**
**CVSS Estimate: 8.1 (High)**

**Finding:** The WebSocket upgrade handler at line 2731 performs **zero Origin header validation**. The only check is for `Sec-WebSocket-Key` (line 2746-2749), which is a protocol requirement — not a security measure.

**Vulnerable Code (lines 2731-2758):**
```go
func (s *SkynetServer) handleWebSocket(w http.ResponseWriter, r *http.Request) {
    hj, ok := w.(http.Hijacker)
    // ... hijack connection ...
    
    // WebSocket handshake
    key := r.Header.Get("Sec-WebSocket-Key")
    if key == "" {
        conn.Close()
        return
    }
    accept := computeWebSocketAccept(key)
    
    bufrw.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
    // ... upgrade completes with NO Origin check ...
}
```

**Attack Vector:**
1. Operator visits any website (e.g., `evil.example.com`)
2. JavaScript on that page executes: `new WebSocket('ws://localhost:8420/ws')`
3. Browser sends the upgrade request with `Origin: https://evil.example.com`
4. Server upgrades without checking Origin — connection established
5. Attacker receives ALL Skynet bus broadcasts: worker states, task results, system topology, agent identities, dispatch content

**What's Exposed:**
- All `broadcastWS()` messages (line 2813-2823)
- Worker task content and results
- Orchestrator dispatch commands
- Agent identities and HWNDs
- System topology (ports, services, health status)
- Score data and internal metrics

**Impact:** Complete information disclosure of the Skynet operational state to any website the operator visits. The attacker gets a real-time feed of all system activity.

**Remediation:**
```go
// Add before handshake (after Hijack):
origin := r.Header.Get("Origin")
if origin != "" && origin != "http://localhost:8421" && origin != "null" {
    http.Error(w, "origin not allowed", http.StatusForbidden)
    return
}
```

**Note:** While the RBAC middleware chain (`rbacMiddleware`) wraps the mux at line 337, the `handleWebSocket` function calls `hj.Hijack()` at line 2739 which **takes over the raw TCP connection**. After hijack, the HTTP middleware pipeline is bypassed — no further middleware checks apply. The RBAC `X-Agent-Role` header check is evaluated pre-hijack, but since the default role for missing headers is `RoleOrchestrator` (line 108), any unauthenticated request gets full orchestrator access.

---

### 2. Frame Security — No Size Limits

**Severity: 🟠 HIGH**
**CVSS Estimate: 6.5 (Medium)**

**Finding:** The reader goroutine (lines 2784-2792) uses a fixed 4096-byte buffer but does **no frame parsing or size validation**:

```go
// Reader goroutine — keep connection alive, handle pings
buf := make([]byte, 4096)
for {
    conn.SetReadDeadline(time.Now().Add(60 * time.Second))
    _, err := conn.Read(buf)
    if err != nil {
        close(ch)
        return
    }
}
```

**Issues:**

**A. No maximum frame size enforcement:**
The `makeWSFrame` function (lines 2825-2850) supports payloads up to 64-bit length (the `else` branch at line 2840 writes 8-byte extended length). While outbound frames are server-controlled, there's no limit on **inbound** frame sizes. A malicious client can send multi-GB frames — the reader only reads 4096 bytes at a time, but Go's TCP stack buffers the full frame.

**B. No frame opcode validation:**
The reader goroutine reads raw bytes but never parses WebSocket frame opcodes. It doesn't:
- Validate frame type (text/binary/close/ping/pong)
- Process ping frames with pong responses (RFC 6455 §5.5.2)
- Handle close frames gracefully (RFC 6455 §5.5.1)
- Reject reserved opcodes

**C. Writer channel has 64-message buffer (line 2761):**
```go
ch := make(chan []byte, 64)
```
The broadcast function (line 2817-2819) drops messages when the channel is full:
```go
select {
case ch <- msg:
default: // drop if channel full
}
```
This is a defense against slow consumers, but a malicious client could intentionally read slowly to force message drops for legitimate consumers.

**D. Slow-Loris vulnerability:**
A client can open a connection, complete the upgrade, then read extremely slowly (one byte per second). The 60-second read deadline (line 2786) provides some protection, but the deadline only fires if `Read()` blocks — if the client sends one byte every 59 seconds, the connection stays alive indefinitely, consuming a goroutine and channel slot.

**Remediation:**
- Add `MaxIncomingFrameSize` check (reject frames > 64KB)
- Parse frame opcodes and respond to ping with pong
- Implement connection count limit per IP
- Add write deadline alongside read deadline

---

### 3. TLS — No Encryption

**Severity: 🟠 HIGH**
**CVSS Estimate: 5.9 (Medium)**

**Finding:** The WebSocket endpoint serves on plain `ws://` — there is no TLS configuration anywhere in `server.go`. The server starts with:

```go
// (from server startup, not shown in WS section)
http.ListenAndServe(":8420", handler)
```

**Impact:**
- All WebSocket traffic is unencrypted plaintext
- Any process on the local machine can sniff traffic via loopback capture
- If the operator ever exposes port 8420 to the network (even temporarily), all traffic including task content, agent identities, and dispatch commands are visible in cleartext
- MITM attacks trivial on any non-localhost network path

**Mitigating factors:**
- Server binds to `localhost:8420` (local only) — network exposure requires explicit misconfiguration
- In the current deployment model (single machine, all agents local), the risk is limited to local process snooping

**Remediation:**
- For production: Add TLS support with self-signed cert (`http.ListenAndServeTLS`)
- For localhost-only: Document that the server MUST NOT be exposed to network interfaces
- Consider adding a startup warning if bound to `0.0.0.0` instead of `127.0.0.1`

---

### 4. Authentication — None on WebSocket

**Severity: 🔴 CRITICAL**
**CVSS Estimate: 8.1 (High)**

**Finding:** The RBAC system (`rbacMiddleware` at line 118) checks `X-Agent-Role` header, but:

1. **Default role is `RoleOrchestrator`** when no header is present (line 108):
   ```go
   if h == "" {
       return RoleOrchestrator // backward-compat: no header = full access
   }
   ```

2. **WebSocket path `/ws` is NOT in the `endpointACL` map** (lines 82-98) — it falls through to default-allow.

3. **After Hijack, middleware is bypassed.** The `Hijack()` call at line 2739 takes over the raw TCP connection. All subsequent communication happens outside the HTTP middleware chain. Even if RBAC checked the initial upgrade request, there's no ongoing auth validation for the WebSocket session.

4. **Browser WebSocket API cannot set custom headers.** The `X-Agent-Role` header cannot be sent by JavaScript's `new WebSocket()` constructor — browsers don't allow custom headers on WebSocket upgrade requests. This means the RBAC system is fundamentally incompatible with browser-based WebSocket clients.

**Combined with CSWSH (Finding 1):** Any webpage can open `ws://localhost:8420/ws`, which arrives with no `X-Agent-Role` header, gets default `RoleOrchestrator` access, passes RBAC (default-allow for `/ws`), and receives all broadcasts. **Zero authentication barriers exist.**

**Remediation:**
- Add `/ws` to `endpointACL` with required role
- Implement token-based auth via query parameter: `ws://localhost:8420/ws?token=SECRET`
- Generate ephemeral tokens on backend startup, distribute to trusted clients
- Reject upgrade requests without valid token

---

### 5. Idle Connection Management — Partial

**Severity: 🟡 MEDIUM**
**CVSS Estimate: 4.3 (Medium)**

**Finding:** Connection lifecycle management is minimal:

**What exists:**
- Read deadline of 60 seconds (line 2786): `conn.SetReadDeadline(time.Now().Add(60 * time.Second))`
- On read error, connection is cleaned up (lines 2788-2791): channel closed, which triggers writer goroutine cleanup

**What's missing:**
- **No ping/pong frames:** The reader goroutine reads raw bytes but never sends WebSocket ping frames. RFC 6455 §5.5.2 recommends ping/pong for keepalive. Without it, dead connections (e.g., client crashes without TCP FIN) won't be detected until the 60-second read deadline fires — and even then, only if the server was expecting incoming data.
- **No write deadline:** The writer goroutine (lines 2767-2781) calls `conn.Write(frame)` with no timeout. If a client stops reading (TCP window fills), `Write()` blocks indefinitely. The goroutine and channel slot leak.
- **No connection count limit:** Any number of WebSocket connections can be opened simultaneously. No per-IP limit, no global limit, no rate limiting on upgrades.
- **No graceful shutdown:** The server has no mechanism to close all WebSocket connections on shutdown. The writer goroutine exits only when the channel is closed or `Write()` errors — there's no signal from the server to drain connections.

**Resource leak scenario:**
1. Attacker opens 10,000 WebSocket connections
2. Each allocates: 1 goroutine (writer) + 1 goroutine (reader) + 1 buffered channel (64 slots) + 4096-byte read buffer
3. Per connection: ~12KB + goroutine stack (8KB default) = ~20KB
4. 10,000 connections ≈ 200MB memory + 20,000 goroutines
5. `broadcastWS()` iterates all clients under `RLock` — O(n) broadcast per message

**Remediation:**
- Add write deadline: `conn.SetWriteDeadline(time.Now().Add(10 * time.Second))` before each `Write()`
- Implement ping/pong: Send ping every 30s, expect pong within 10s
- Add global connection limit (e.g., max 50 WS clients)
- Add graceful shutdown signal to close all connections

---

## Summary of Vulnerabilities

| # | Vulnerability | Severity | Status | Fix Complexity |
|---|---------------|----------|--------|----------------|
| 1 | CSWSH — No Origin validation | 🔴 CRITICAL | OPEN | Low (~5 lines) |
| 2 | No frame size limits / opcode parsing | 🟠 HIGH | OPEN | Medium (~50 lines) |
| 3 | No TLS encryption | 🟠 HIGH | OPEN | Medium (config) |
| 4 | No authentication on WS upgrade | 🔴 CRITICAL | OPEN | Medium (~30 lines) |
| 5 | Incomplete idle management | 🟡 MEDIUM | PARTIAL | Medium (~40 lines) |

---

## Risk Assessment

**Current Deployment Context:**
- Server binds to `localhost:8420` — not network-exposed
- All clients are local Python/PowerShell processes
- Operator is the sole user on the machine

**Despite localhost-only binding, CSWSH is still exploitable** because browsers route `ws://localhost:*` requests from any origin. The attacker doesn't need network access — they only need the operator to visit a webpage.

**Risk Matrix:**

| Threat | Likelihood | Impact | Risk |
|--------|-----------|--------|------|
| CSWSH via malicious webpage | HIGH | HIGH (full info disclosure) | **CRITICAL** |
| Resource exhaustion via WS flooding | MEDIUM | MEDIUM (DoS) | **HIGH** |
| Slow-loris goroutine leak | LOW | LOW (gradual degradation) | **LOW** |
| Network MITM (if port exposed) | LOW | HIGH (full compromise) | **MEDIUM** |

---

## Recommended Fix Priority

### Phase 1 — Immediate (blocks security review sign-off)
1. **Add Origin validation** — whitelist `http://localhost:8421` and `null` (local file)
2. **Add token-based auth** — query param `?token=` checked on upgrade
3. **Add `/ws` to RBAC ACL** — require at minimum `RoleWorker`

### Phase 2 — Short-term (next sprint)
4. **Add connection count limit** — max 50 concurrent WS clients
5. **Add write deadline** — 10s timeout on all `conn.Write()` calls
6. **Implement ping/pong** — 30s ping, 10s pong deadline

### Phase 3 — Medium-term
7. **Add frame parsing** — validate opcodes, enforce max frame size (64KB)
8. **Add TLS option** — `--tls` flag with self-signed cert generation
9. **Add graceful shutdown** — signal all WS connections to close on server stop

---

## Code References

| Item | File | Lines |
|------|------|-------|
| WebSocket handler | `Skynet/server.go` | 2731-2793 |
| WS stats endpoint | `Skynet/server.go` | 2797-2811 |
| Broadcast function | `Skynet/server.go` | 2813-2823 |
| Frame construction | `Skynet/server.go` | 2825-2850 |
| Accept computation | `Skynet/server.go` | 2852-2858 |
| RBAC middleware | `Skynet/server.go` | 66-160 |
| Endpoint registration | `Skynet/server.go` | 325-326 |
| CORS (wildcard) | `Skynet/server.go` | 353 |
| Default role fallback | `Skynet/server.go` | 107-108 |

<!-- signed: delta -->
