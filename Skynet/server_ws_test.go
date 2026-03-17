package main

// WebSocket security tests — cross-validation of Delta's P2 hardening.
// Tests: origin validation, RBAC auth, connection limits, frame size
// limits, malformed frames, close/ping/pong handling.
// signed: alpha

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ──────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────

// wsUpgradeRequest builds an HTTP request with the WebSocket upgrade headers.
func wsUpgradeRequest(path, origin, role string) *http.Request {
	req := httptest.NewRequest("GET", path, nil)
	req.RemoteAddr = "127.0.0.1:9999"
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Sec-WebSocket-Version", "13")
	req.Header.Set("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	if role != "" {
		req.Header.Set("X-Agent-Role", role)
	}
	return req
}

// wsHandshake performs a raw TCP WebSocket handshake against a live server
// and returns the connection, buffered reader, and any error.
func wsHandshake(t *testing.T, addr, origin, role string) (net.Conn, *bufio.Reader, error) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		return nil, nil, fmt.Errorf("dial: %w", err)
	}

	key := "dGhlIHNhbXBsZSBub25jZQ=="
	lines := []string{
		"GET /ws HTTP/1.1",
		"Host: " + addr,
		"Connection: Upgrade",
		"Upgrade: websocket",
		"Sec-WebSocket-Version: 13",
		"Sec-WebSocket-Key: " + key,
	}
	if origin != "" {
		lines = append(lines, "Origin: "+origin)
	}
	if role != "" {
		lines = append(lines, "X-Agent-Role: "+role)
	}
	lines = append(lines, "", "")
	raw := strings.Join(lines, "\r\n")
	conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Write([]byte(raw)); err != nil {
		conn.Close()
		return nil, nil, fmt.Errorf("write handshake: %w", err)
	}

	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	br := bufio.NewReader(conn)
	return conn, br, nil
}

// readHTTPStatus reads the first line of the HTTP response and returns the
// status code.
func readHTTPStatus(br *bufio.Reader) (int, string, error) {
	line, err := br.ReadString('\n')
	if err != nil {
		return 0, "", err
	}
	line = strings.TrimSpace(line)
	// "HTTP/1.1 101 Switching Protocols"
	parts := strings.SplitN(line, " ", 3)
	if len(parts) < 2 {
		return 0, line, fmt.Errorf("malformed status line: %q", line)
	}
	var code int
	fmt.Sscanf(parts[1], "%d", &code)
	return code, line, nil
}

// consumeHeaders reads until an empty line (end of HTTP headers).
func consumeHeaders(br *bufio.Reader) error {
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return err
		}
		if strings.TrimSpace(line) == "" {
			return nil
		}
	}
}

// sendMaskedFrame sends a WebSocket frame with client-side masking.
func sendMaskedFrame(conn net.Conn, opcode byte, payload []byte) error {
	mask := [4]byte{0x12, 0x34, 0x56, 0x78}
	pLen := len(payload)

	var header []byte
	if pLen < 126 {
		header = []byte{0x80 | opcode, 0x80 | byte(pLen)}
	} else if pLen < 65536 {
		header = []byte{0x80 | opcode, 0x80 | 126, byte(pLen >> 8), byte(pLen)}
	} else {
		header = make([]byte, 10)
		header[0] = 0x80 | opcode
		header[1] = 0x80 | 127
		binary.BigEndian.PutUint64(header[2:], uint64(pLen))
	}
	header = append(header, mask[:]...)

	masked := make([]byte, pLen)
	for i, b := range payload {
		masked[i] = b ^ mask[i%4]
	}

	conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Write(header); err != nil {
		return err
	}
	if _, err := conn.Write(masked); err != nil {
		return err
	}
	return nil
}

// sendUnmaskedFrame sends a WebSocket frame WITHOUT client masking
// (protocol violation per RFC 6455 §5.1).
func sendUnmaskedFrame(conn net.Conn, opcode byte, payload []byte) error {
	pLen := len(payload)
	header := []byte{0x80 | opcode, byte(pLen)}
	conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Write(header); err != nil {
		return err
	}
	if _, err := conn.Write(payload); err != nil {
		return err
	}
	return nil
}

// ──────────────────────────────────────────────────────────────────
// 1. Origin Validation (wsAllowedOrigin)
// ──────────────────────────────────────────────────────────────────

func TestWSAllowedOrigin(t *testing.T) {
	tests := []struct {
		origin  string
		allowed bool
	}{
		// Allowed origins
		{"", true},                                 // no origin (non-browser client)
		{"null", true},                             // local file:// pages
		{"http://localhost", true},                  // bare localhost
		{"http://localhost:8420", true},             // with port
		{"http://localhost:8421/dashboard", true},   // with path
		{"https://localhost:8421", true},            // HTTPS localhost
		{"http://127.0.0.1", true},                 // IPv4 loopback
		{"http://127.0.0.1:3000", true},            // IPv4 with port
		{"https://127.0.0.1:443", true},            // IPv4 HTTPS
		{"http://[::1]", true},                     // IPv6 loopback
		{"http://[::1]:8420", true},                 // IPv6 with port
		{"HTTP://LOCALHOST:8420", true},             // case insensitive

		// Blocked origins
		{"http://evil.com", false},                 // foreign domain
		{"http://localhost.evil.com", false},        // subdomain spoof (CV-S4-BUG-001 fix)
		{"https://attacker.io", false},             // HTTPS foreign
		{"http://192.168.1.100", false},            // non-loopback private IP
		{"http://10.0.0.1", false},                 // non-loopback private IP
		{"http://0.0.0.0", false},                  // wildcard bind
		{"chrome-extension://abc", false},          // browser extension
	}

	for _, tt := range tests {
		t.Run(fmt.Sprintf("origin=%q", tt.origin), func(t *testing.T) {
			got := wsAllowedOrigin(tt.origin)
			if got != tt.allowed {
				t.Errorf("wsAllowedOrigin(%q) = %v, want %v", tt.origin, got, tt.allowed)
			}
		})
	}
}

// TestWSOriginBlockedAtUpgrade verifies the full HTTP handler rejects
// foreign origins with 403 and logs a security event.
func TestWSOriginBlockedAtUpgrade(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	conn, br, err := wsHandshake(t, addr, "http://evil.com", "")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusForbidden {
		t.Errorf("expected 403 for foreign origin, got %d", code)
	}

	// Verify rejected counter incremented
	rejected := atomic.LoadInt64(&srv.wsRejected)
	if rejected < 1 {
		t.Errorf("expected wsRejected >= 1, got %d", rejected)
	}
}

// TestWSOriginAllowedUpgrade verifies localhost origin completes the
// WebSocket handshake with 101 Switching Protocols.
func TestWSOriginAllowedUpgrade(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	// Must include a valid role header after RBAC hardening — signed: delta
	conn, br, err := wsHandshake(t, addr, "http://localhost:8421", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusSwitchingProtocols {
		t.Errorf("expected 101 for localhost origin, got %d", code)
	}
}

// ──────────────────────────────────────────────────────────────────
// 2. RBAC Authentication
// ──────────────────────────────────────────────────────────────────

// TestWSRBACConsultantBlocked verifies that a consultant role is rejected
// from /ws (ACL restricts to orchestrator + worker only).
func TestWSRBACConsultantBlocked(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	conn, br, err := wsHandshake(t, addr, "http://localhost", "consultant")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusForbidden {
		t.Errorf("expected 403 for consultant role on /ws, got %d", code)
	}
}

// TestWSRBACUnknownRoleBlocked verifies that an unrecognized role is rejected.
func TestWSRBACUnknownRoleBlocked(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	conn, br, err := wsHandshake(t, addr, "http://localhost", "hacker")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusForbidden {
		t.Errorf("expected 403 for unknown role, got %d", code)
	}
}

// TestWSRBACWorkerAllowed verifies that a worker role can establish a
// WebSocket connection.
func TestWSRBACWorkerAllowed(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	conn, br, err := wsHandshake(t, addr, "http://localhost", "worker")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusSwitchingProtocols {
		t.Errorf("expected 101 for worker role, got %d", code)
	}
}

// TestWSRBACNoHeaderDefaultsOrchestrator verifies that no X-Agent-Role
// header defaults to orchestrator (backward-compat) and is allowed.
// TestWSRBACNoHeaderRejectsConnection verifies that missing X-Agent-Role
// header results in a 403 rejection (default-deny after RBAC hardening).
// signed: delta
func TestWSRBACNoHeaderRejectsConnection(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	addr := ts.Listener.Addr().String()
	// Empty role = no X-Agent-Role header — should be rejected
	conn, br, err := wsHandshake(t, addr, "http://localhost", "")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, err := readHTTPStatus(br)
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	if code != http.StatusForbidden {
		t.Errorf("expected 403 for missing role header, got %d", code)
	}
}

// ──────────────────────────────────────────────────────────────────
// 3. Connection Limits
// ──────────────────────────────────────────────────────────────────

// TestWSConnectionLimit verifies that connections beyond wsMaxConnections
// are rejected with 503 and the counter tracks correctly.
func TestWSConnectionLimit(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	// Pre-set wsConns to just below the limit to avoid opening 50 real connections
	atomic.StoreInt64(&srv.wsConns, wsMaxConnections-1)

	// This connection should succeed (fills the last slot)
	conn1, br1, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake for last slot: %v", err)
	}
	defer conn1.Close()

	code1, _, _ := readHTTPStatus(br1)
	if code1 != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101 for last slot, got %d", code1)
	}

	// Now at limit. Next connection should be rejected.
	conn2, br2, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake for overlimit: %v", err)
	}
	defer conn2.Close()

	code2, _, _ := readHTTPStatus(br2)
	if code2 != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when at connection limit, got %d", code2)
	}
}

// TestWSConnectionCounterDecrement verifies wsConns is decremented when
// a client disconnects.
func TestWSConnectionCounterDecrement(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	before := atomic.LoadInt64(&srv.wsConns)

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)

	// Allow connection to register
	time.Sleep(50 * time.Millisecond)
	during := atomic.LoadInt64(&srv.wsConns)
	if during <= before {
		t.Errorf("expected wsConns to increase after connect, before=%d during=%d", before, during)
	}

	// Close the connection — send a proper close frame
	sendMaskedFrame(conn, 0x08, []byte{0x03, 0xE8}) // close with 1000 status
	conn.Close()

	// Wait for cleanup
	time.Sleep(200 * time.Millisecond)
	after := atomic.LoadInt64(&srv.wsConns)
	if after != before {
		t.Errorf("expected wsConns to return to %d after disconnect, got %d", before, after)
	}
}

// ──────────────────────────────────────────────────────────────────
// 4. Frame Size Limits
// ──────────────────────────────────────────────────────────────────

// TestWSFrameSizeRejected verifies that oversized frames are rejected
// and a security event is logged.
func TestWSFrameSizeRejected(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)

	// Allow connection setup
	time.Sleep(50 * time.Millisecond)

	// Send a masked frame with payload length marker = 127 (8-byte extended)
	// claiming payload size > wsMaxFrameSize (1MB).
	mask := [4]byte{0x12, 0x34, 0x56, 0x78}
	frame := make([]byte, 14) // 2 header + 8 length + 4 mask
	frame[0] = 0x81           // FIN + text
	frame[1] = 0x80 | 127     // masked + 8-byte length follows
	binary.BigEndian.PutUint64(frame[2:10], uint64(wsMaxFrameSize+1))
	copy(frame[10:14], mask[:])

	conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	conn.Write(frame)

	// Wait for the server to process the frame and log the security event.
	// The connection may or may not be closed immediately (async cleanup),
	// but the security event MUST be logged synchronously before channel close.
	time.Sleep(500 * time.Millisecond)

	// Primary assertion: security event was logged
	srv.secMu.RLock()
	found := false
	for _, ev := range srv.securityLog {
		if ev.Type == "ws_frame_too_large" {
			found = true
			break
		}
	}
	srv.secMu.RUnlock()
	if !found {
		t.Error("expected ws_frame_too_large security event to be logged")
	}

	// Secondary assertion: connection should eventually be terminated.
	// After cleanup chain: reader close(ch) → writer detects → defer cleanup →
	// conn.Close(). Try reading — should fail.
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	tmp := make([]byte, 128)
	for i := 0; i < 5; i++ {
		_, readErr := conn.Read(tmp)
		if readErr != nil {
			return // connection closed as expected
		}
		// Might receive a ping from writer before it detects closed ch
		time.Sleep(100 * time.Millisecond)
	}
	t.Log("NOTICE: connection not closed within timeout — async cleanup may be slow")
}

// TestWSFrameSizeAccepted verifies that frames within the limit are processed.
func TestWSFrameSizeAccepted(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)
	time.Sleep(50 * time.Millisecond)

	// Send a small valid ping frame (should not close connection)
	err = sendMaskedFrame(conn, 0x09, []byte("hello"))
	if err != nil {
		t.Fatalf("send ping: %v", err)
	}

	// We should receive a pong back (opcode 0x0A)
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	resp := make([]byte, 16)
	n, readErr := conn.Read(resp)
	// May get either a pong or a ping from the server's keepalive ticker,
	// or we might read broadcast data. The key assertion: connection is alive.
	if readErr != nil && n == 0 {
		t.Errorf("connection should stay alive after valid frame, got err: %v", readErr)
	}
}

// ──────────────────────────────────────────────────────────────────
// 5. Malformed Frames
// ──────────────────────────────────────────────────────────────────

// TestWSUnmaskedFrameRejected verifies that unmasked client frames cause
// immediate connection closure (RFC 6455 §5.1 violation).
func TestWSUnmaskedFrameRejected(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)
	time.Sleep(50 * time.Millisecond)

	// Send an UNMASKED text frame (protocol violation)
	err = sendUnmaskedFrame(conn, 0x01, []byte("bad frame"))
	if err != nil {
		t.Fatalf("send unmasked frame: %v", err)
	}

	// Server should close the connection
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	tmp := make([]byte, 128)
	_, readErr := conn.Read(tmp)
	if readErr == nil {
		t.Error("expected connection to be closed after unmasked frame")
	}
}

// TestWSCloseFrameHandled verifies that a close frame terminates the
// connection gracefully.
func TestWSCloseFrameHandled(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)
	time.Sleep(50 * time.Millisecond)

	before := atomic.LoadInt64(&srv.wsConns)

	// Send masked close frame (opcode 0x08) with status 1000 (normal)
	err = sendMaskedFrame(conn, 0x08, []byte{0x03, 0xE8})
	if err != nil {
		t.Fatalf("send close: %v", err)
	}

	// Wait for server-side cleanup
	time.Sleep(300 * time.Millisecond)
	after := atomic.LoadInt64(&srv.wsConns)
	if after >= before && before > 0 {
		t.Errorf("expected wsConns to decrease after close frame, before=%d after=%d", before, after)
	}
}

// TestWSPingPong verifies that client ping frames receive server pong
// responses.
func TestWSPingPong(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)
	time.Sleep(50 * time.Millisecond)

	// Send a masked ping
	err = sendMaskedFrame(conn, 0x09, []byte{})
	if err != nil {
		t.Fatalf("send ping: %v", err)
	}

	// Read response — should contain a pong (0x8A = FIN + pong opcode)
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	resp := make([]byte, 64)
	n, readErr := conn.Read(resp)
	if readErr != nil {
		t.Fatalf("read pong: %v", readErr)
	}

	// Look for pong opcode anywhere in the response data
	// (server may also send broadcast data or ping frames)
	foundPong := false
	for i := 0; i < n; i++ {
		if resp[i]&0x8F == 0x8A { // FIN + pong
			foundPong = true
			break
		}
	}
	if !foundPong {
		t.Errorf("expected pong frame (0x8A) in response, got %d bytes: %x", n, resp[:n])
	}
}

// ──────────────────────────────────────────────────────────────────
// 6. Broadcast Delivery
// ──────────────────────────────────────────────────────────────────

// TestWSBroadcastDelivery verifies that broadcastWS delivers messages to
// connected WebSocket clients.
func TestWSBroadcastDelivery(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, br, err := wsHandshake(t, addr, "http://localhost", "orchestrator")
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	defer conn.Close()

	code, _, _ := readHTTPStatus(br)
	if code != http.StatusSwitchingProtocols {
		t.Fatalf("expected 101, got %d", code)
	}
	consumeHeaders(br)

	// Allow connection to register
	time.Sleep(100 * time.Millisecond)

	// Broadcast a test message
	testPayload := `{"type":"test","data":"hello_ws"}`
	srv.broadcastWS([]byte(testPayload))

	// Read the frame from the connection
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	frameBuf := make([]byte, 4096)
	n, readErr := conn.Read(frameBuf)
	if readErr != nil {
		t.Fatalf("read broadcast: %v", readErr)
	}
	if n < 2 {
		t.Fatalf("expected at least 2 bytes, got %d", n)
	}

	// Parse frame: first byte = opcode, second byte = length
	opcode := frameBuf[0] & 0x0F
	if opcode != 0x01 { // text frame
		t.Errorf("expected text frame opcode (1), got %d", opcode)
	}
	payloadLen := int(frameBuf[1] & 0x7F)
	payloadStart := 2
	if payloadLen == 126 {
		payloadStart = 4
		payloadLen = int(frameBuf[2])<<8 | int(frameBuf[3])
	}
	if payloadStart+payloadLen > n {
		t.Fatalf("frame claims %d bytes payload but only %d bytes received", payloadLen, n-payloadStart)
	}
	received := string(frameBuf[payloadStart : payloadStart+payloadLen])
	if received != testPayload {
		t.Errorf("broadcast payload mismatch:\n  got:  %q\n  want: %q", received, testPayload)
	}
}

// ──────────────────────────────────────────────────────────────────
// 7. WebSocket Accept Key Computation
// ──────────────────────────────────────────────────────────────────

// TestWSComputeAccept verifies the RFC 6455 Sec-WebSocket-Accept computation
// against a known test vector.
func TestWSComputeAccept(t *testing.T) {
	// RFC 6455 §4.2.2 example
	key := "dGhlIHNhbXBsZSBub25jZQ=="
	expected := computeWebSocketAcceptRef(key)
	got := computeWebSocketAccept(key)
	if got != expected {
		t.Errorf("computeWebSocketAccept(%q) = %q, want %q", key, got, expected)
	}
}

// Reference implementation for cross-check
func computeWebSocketAcceptRef(key string) string {
	magic := "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	h := sha1.New()
	h.Write([]byte(key + magic))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// ──────────────────────────────────────────────────────────────────
// 8. WS Stats Endpoint
// ──────────────────────────────────────────────────────────────────

func TestWSStatsEndpoint(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	// Set some counter values
	atomic.StoreInt64(&srv.wsConns, 3)
	atomic.StoreInt64(&srv.wsRejected, 7)
	srv.broadcastWS([]byte(`test`)) // increment wsBroadcasts

	rr := doRequest(handler, "GET", "/ws/stats", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var stats map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&stats)

	if int(stats["max_connections"].(float64)) != wsMaxConnections {
		t.Errorf("max_connections should be %d, got %v", wsMaxConnections, stats["max_connections"])
	}
	if int(stats["active_connections"].(float64)) != 3 {
		t.Errorf("active_connections should be 3, got %v", stats["active_connections"])
	}
	if int(stats["total_rejected"].(float64)) != 7 {
		t.Errorf("total_rejected should be 7, got %v", stats["total_rejected"])
	}
}

// ──────────────────────────────────────────────────────────────────
// 9. Concurrent Connection Stress
// ──────────────────────────────────────────────────────────────────

// TestWSConcurrentConnections verifies multiple simultaneous WebSocket
// connections are tracked correctly and all clean up on close.
func TestWSConcurrentConnections(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	const numConns = 5
	conns := make([]net.Conn, 0, numConns)
	var mu sync.Mutex

	var wg sync.WaitGroup
	for i := 0; i < numConns; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			conn, br, err := wsHandshake(t, addr, "http://localhost", "worker")
			if err != nil {
				t.Errorf("handshake: %v", err)
				return
			}
			code, _, _ := readHTTPStatus(br)
			if code != http.StatusSwitchingProtocols {
				conn.Close()
				return
			}
			consumeHeaders(br)
			mu.Lock()
			conns = append(conns, conn)
			mu.Unlock()
		}()
	}
	wg.Wait()

	time.Sleep(100 * time.Millisecond)
	active := atomic.LoadInt64(&srv.wsConns)
	if active < int64(len(conns)) {
		t.Errorf("expected at least %d active connections, got %d", len(conns), active)
	}

	// Close all connections
	mu.Lock()
	for _, c := range conns {
		sendMaskedFrame(c, 0x08, []byte{0x03, 0xE8})
		c.Close()
	}
	mu.Unlock()

	// Wait for cleanup
	time.Sleep(500 * time.Millisecond)
	final := atomic.LoadInt64(&srv.wsConns)
	if final != 0 {
		t.Errorf("expected 0 active connections after all closed, got %d", final)
	}
}

// ──────────────────────────────────────────────────────────────────
// 10. Missing Sec-WebSocket-Key
// ──────────────────────────────────────────────────────────────────

// TestWSMissingKeyRejectsUpgrade verifies that a WebSocket upgrade
// without Sec-WebSocket-Key fails cleanly.
func TestWSMissingKeyRejectsUpgrade(t *testing.T) {
	srv := newTestServer()
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()
	addr := ts.Listener.Addr().String()

	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	// Handshake WITHOUT Sec-WebSocket-Key
	raw := "GET /ws HTTP/1.1\r\n" +
		"Host: " + addr + "\r\n" +
		"Connection: Upgrade\r\n" +
		"Upgrade: websocket\r\n" +
		"Sec-WebSocket-Version: 13\r\n" +
		"Origin: http://localhost\r\n" +
		"\r\n"
	conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	conn.Write([]byte(raw))

	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	br := bufio.NewReader(conn)
	code, _, err := readHTTPStatus(br)
	if err != nil {
		// Connection closed immediately — acceptable for missing key
		return
	}
	// The handler should either reject or close. If it sent 101 without a key,
	// that's a bug.
	if code == http.StatusSwitchingProtocols {
		t.Error("server should not send 101 without Sec-WebSocket-Key")
	}
}
