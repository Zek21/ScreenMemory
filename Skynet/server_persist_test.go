package main

// Cross-validation tests for Gamma's persistence race fixes and token bucket.
// signed: delta

import (
	"bytes"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ═══════════════════════════════════════════════════════════════════
// Token Bucket Tests
// ═══════════════════════════════════════════════════════════════════

func TestTokenBucketAllow_BasicRate(t *testing.T) {
	// A fresh bucket with full capacity should allow exactly tokenBucketCapacity
	// requests, then reject.
	tb := &tokenBucket{}
	tb.tokens.Store(tokenBucketCapacity)
	tb.lastRefill.Store(time.Now().UnixNano())

	allowed := 0
	for i := 0; i < tokenBucketCapacity+5; i++ {
		if tb.allow() {
			allowed++
		}
	}
	if allowed != tokenBucketCapacity {
		t.Errorf("expected %d allowed, got %d", tokenBucketCapacity, allowed)
	}
}

func TestTokenBucketAllow_DeniesWhenEmpty(t *testing.T) {
	tb := &tokenBucket{}
	tb.tokens.Store(0)
	tb.lastRefill.Store(time.Now().UnixNano())

	if tb.allow() {
		t.Error("allow() should return false when tokens == 0")
	}
}

func TestTokenBucketBurst_CannotExceedCapacity(t *testing.T) {
	// Even after a long idle period, tokens should cap at tokenBucketCapacity.
	tb := &tokenBucket{}
	tb.tokens.Store(0)
	// Pretend the last refill was 100 seconds ago → should refill to cap, not 200.
	tb.lastRefill.Store(time.Now().Add(-100 * time.Second).UnixNano())

	// First call triggers refill.
	tb.allow()

	cur := tb.tokens.Load()
	// After refill (200 tokens calculated, capped at 20) minus 1 consumed:
	if cur > tokenBucketCapacity {
		t.Errorf("tokens exceeded capacity: %d > %d", cur, tokenBucketCapacity)
	}
}

func TestTokenBucketRefill_Timing(t *testing.T) {
	tb := &tokenBucket{}
	tb.tokens.Store(0)
	tb.lastRefill.Store(time.Now().UnixNano())

	// With 0 tokens and recent lastRefill, should deny.
	if tb.allow() {
		t.Fatal("expected deny with 0 tokens and recent refill")
	}

	// Wait for refill interval (tokenRefillInterval = 500ms).
	// After 600ms at 2 tokens/sec, we expect 1 token refilled.
	time.Sleep(600 * time.Millisecond)

	if !tb.allow() {
		t.Error("expected allow after 600ms refill (should have ≥1 token)")
	}
}

func TestTokenBucketRefill_ProportionalToElapsed(t *testing.T) {
	tb := &tokenBucket{}
	tb.tokens.Store(0)
	// Set lastRefill to 3 seconds ago → expect 6 tokens refilled (3s × 2/s).
	tb.lastRefill.Store(time.Now().Add(-3 * time.Second).UnixNano())

	// Trigger refill by calling allow once.
	tb.allow()

	cur := tb.tokens.Load()
	// We consumed 1, so expect 5. Allow some timing slack.
	if cur < 4 || cur > 6 {
		t.Errorf("expected ~5 tokens after 3s refill, got %d", cur)
	}
}

func TestTokenBucketConcurrent(t *testing.T) {
	// Hammer a token bucket from many goroutines. The total allowed must never
	// exceed capacity + any refills that occurred during the test.
	tb := &tokenBucket{}
	tb.tokens.Store(tokenBucketCapacity)
	tb.lastRefill.Store(time.Now().UnixNano())

	const goroutines = 50
	const requestsPerG = 10
	var totalAllowed atomic.Int64
	var wg sync.WaitGroup

	start := time.Now()
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < requestsPerG; i++ {
				if tb.allow() {
					totalAllowed.Add(1)
				}
			}
		}()
	}
	wg.Wait()
	elapsed := time.Since(start)

	// Max possible tokens = initial capacity + refills during elapsed time.
	maxRefill := int64(math.Ceil(elapsed.Seconds())) * tokenRefillRate
	maxPossible := int64(tokenBucketCapacity) + maxRefill
	got := totalAllowed.Load()

	if got > maxPossible {
		t.Errorf("allowed %d requests, but max possible is %d (cap=%d + refill=%d in %v)",
			got, maxPossible, tokenBucketCapacity, maxRefill, elapsed)
	}
	if got < 1 {
		t.Error("expected at least 1 allowed request")
	}
}

func TestTokenBucketConcurrent_NoTokenLoss(t *testing.T) {
	// With enough time for full refill and a single goroutine, every allow()
	// should succeed up to capacity. This ensures CAS loops don't lose tokens.
	tb := &tokenBucket{}
	tb.tokens.Store(tokenBucketCapacity)
	tb.lastRefill.Store(time.Now().UnixNano())

	// Consume all tokens.
	for i := 0; i < tokenBucketCapacity; i++ {
		if !tb.allow() {
			t.Fatalf("unexpected deny on token %d of %d", i+1, tokenBucketCapacity)
		}
	}
	// Should now be empty.
	if tb.allow() {
		t.Error("expected deny after consuming all tokens")
	}
}

// ═══════════════════════════════════════════════════════════════════
// File-Based Handler Tests (handleGodFeed, handleBrainPending, handleBrainAck)
// ═══════════════════════════════════════════════════════════════════

// brainTestDir returns the brain data directory path used by the server.
const brainDir = `D:\Prospects\ScreenMemory\data\brain`

// backupAndSetup saves original files and writes test data.
// Returns a cleanup function that restores originals.
func backupAndSetup(t *testing.T, filename string, testData []byte) func() {
	t.Helper()
	path := filepath.Join(brainDir, filename)

	var original []byte
	var hadOriginal bool
	if data, err := os.ReadFile(path); err == nil {
		original = data
		hadOriginal = true
	}

	if err := os.MkdirAll(brainDir, 0755); err != nil {
		t.Fatalf("cannot create brain dir: %v", err)
	}
	if err := os.WriteFile(path, testData, 0644); err != nil {
		t.Fatalf("cannot write test data to %s: %v", path, err)
	}

	return func() {
		if hadOriginal {
			os.WriteFile(path, original, 0644)
		} else {
			os.Remove(path)
		}
	}
}

// ─── handleGodFeed concurrent read safety ────────────────────────

func TestHandleGodFeedConcurrentReads(t *testing.T) {
	// P1 fix: concurrent GET /god_feed must not produce torn reads while
	// appendGodFeed writes concurrently.
	testFeed := []map[string]interface{}{
		{"type": "test", "text": "entry1", "time": "00:00:01", "ts": 1.0},
		{"type": "test", "text": "entry2", "time": "00:00:02", "ts": 2.0},
	}
	testData, _ := json.Marshal(testFeed)
	cleanup := backupAndSetup(t, "god_feed.json", testData)
	defer cleanup()

	srv := newTestServer("alpha")
	handler := srv.Handler()

	const readers = 20
	const writers = 5
	const iterations = 50
	var wg sync.WaitGroup
	var readErrors atomic.Int64

	// Concurrent readers: GET /god_feed
	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				req := httptest.NewRequest(http.MethodGet, "/god_feed", nil)
				req.RemoteAddr = "127.0.0.1:9999"
				rr := httptest.NewRecorder()
				handler.ServeHTTP(rr, req)

				if rr.Code != http.StatusOK {
					readErrors.Add(1)
					continue
				}
				// Verify response is valid JSON (not torn).
				var result interface{}
				if err := json.Unmarshal(rr.Body.Bytes(), &result); err != nil {
					readErrors.Add(1)
				}
			}
		}()
	}

	// Concurrent writers: appendGodFeed
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				srv.appendGodFeed("test", "concurrent_write")
			}
		}(i)
	}

	wg.Wait()

	if readErrors.Load() > 0 {
		t.Errorf("got %d read errors (torn reads or invalid JSON) during concurrent access",
			readErrors.Load())
	}
}

// ─── handleBrainPending concurrent read safety ───────────────────

func TestHandleBrainPendingConcurrentReads(t *testing.T) {
	// P2a fix: concurrent GET /brain/pending must not produce torn reads.
	testInbox := []map[string]interface{}{
		{"request_id": "r1", "directive": "task1", "status": "pending", "timestamp": 1.0},
		{"request_id": "r2", "directive": "task2", "status": "completed", "timestamp": 2.0},
	}
	testData, _ := json.Marshal(testInbox)
	cleanup := backupAndSetup(t, "brain_inbox.json", testData)
	defer cleanup()

	srv := newTestServer("alpha")
	handler := srv.Handler()

	const readers = 20
	const writers = 5
	const iterations = 50
	var wg sync.WaitGroup
	var readErrors atomic.Int64

	// Concurrent readers: GET /brain/pending
	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				req := httptest.NewRequest(http.MethodGet, "/brain/pending", nil)
				req.RemoteAddr = "127.0.0.1:9999"
				rr := httptest.NewRecorder()
				handler.ServeHTTP(rr, req)

				if rr.Code != http.StatusOK {
					readErrors.Add(1)
					continue
				}
				var result interface{}
				if err := json.Unmarshal(rr.Body.Bytes(), &result); err != nil {
					readErrors.Add(1)
				}
			}
		}()
	}

	// Concurrent writers: appendBrainInbox (each with unique goal to bypass dedup)
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				srv.appendBrainInbox(
					"test-"+string(rune('A'+id))+"-"+string(rune('0'+j%10)),
					"goal-"+string(rune('A'+id))+"-"+string(rune('0'+j%10)),
				)
			}
		}(i)
	}

	wg.Wait()

	if readErrors.Load() > 0 {
		t.Errorf("got %d read errors (torn reads or invalid JSON) during concurrent access",
			readErrors.Load())
	}
}

// ─── handleBrainAck RMW atomicity ────────────────────────────────

func TestHandleBrainAckAtomicity(t *testing.T) {
	// P2b fix: concurrent ACK requests must not lose each other's writes.
	// Set up inbox with multiple pending items, ACK them all concurrently,
	// then verify ALL are marked completed.
	const itemCount = 10
	inbox := make([]map[string]interface{}, itemCount)
	for i := 0; i < itemCount; i++ {
		inbox[i] = map[string]interface{}{
			"request_id": "ack-test-" + string(rune('a'+i)),
			"directive":  "task-" + string(rune('a'+i)),
			"status":     "pending",
			"timestamp":  float64(i),
		}
	}
	testData, _ := json.Marshal(inbox)
	cleanup := backupAndSetup(t, "brain_inbox.json", testData)
	defer cleanup()

	srv := newTestServer("alpha")
	handler := srv.Handler()

	// ACK all items concurrently.
	var wg sync.WaitGroup
	var ackErrors atomic.Int64
	for i := 0; i < itemCount; i++ {
		wg.Add(1)
		go func(reqID string) {
			defer wg.Done()
			body, _ := json.Marshal(map[string]string{"request_id": reqID})
			req := httptest.NewRequest(http.MethodPost, "/brain/ack", bytes.NewReader(body))
			req.RemoteAddr = "127.0.0.1:9999"
			req.Header.Set("Content-Type", "application/json")
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, req)
			if rr.Code != http.StatusOK {
				ackErrors.Add(1)
			}
		}(inbox[i]["request_id"].(string))
	}
	wg.Wait()

	if ackErrors.Load() > 0 {
		t.Errorf("got %d ACK failures", ackErrors.Load())
	}

	// Read the file and verify ALL items are now "completed".
	data, err := os.ReadFile(filepath.Join(brainDir, "brain_inbox.json"))
	if err != nil {
		t.Fatalf("cannot read brain_inbox.json: %v", err)
	}
	var result []map[string]interface{}
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("corrupt JSON after concurrent ACKs: %v", err)
	}
	if len(result) != itemCount {
		t.Errorf("expected %d items, got %d (data loss!)", itemCount, len(result))
	}
	for i, item := range result {
		if item["status"] != "completed" {
			t.Errorf("item %d (%s) status=%v, want completed (RMW lost write!)",
				i, item["request_id"], item["status"])
		}
		if _, ok := item["completed_at"]; !ok {
			t.Errorf("item %d missing completed_at timestamp", i)
		}
	}
}

func TestHandleBrainAckConcurrentWithAppend(t *testing.T) {
	// Mixed ACK + append under concurrency: verify no data loss.
	inbox := []map[string]interface{}{
		{"request_id": "mix-1", "directive": "d1", "status": "pending", "timestamp": 1.0},
		{"request_id": "mix-2", "directive": "d2", "status": "pending", "timestamp": 2.0},
	}
	testData, _ := json.Marshal(inbox)
	cleanup := backupAndSetup(t, "brain_inbox.json", testData)
	defer cleanup()

	srv := newTestServer("alpha")
	handler := srv.Handler()

	var wg sync.WaitGroup

	// Concurrent ACKs.
	for _, id := range []string{"mix-1", "mix-2"} {
		wg.Add(1)
		go func(reqID string) {
			defer wg.Done()
			body, _ := json.Marshal(map[string]string{"request_id": reqID})
			req := httptest.NewRequest(http.MethodPost, "/brain/ack", bytes.NewReader(body))
			req.RemoteAddr = "127.0.0.1:9999"
			req.Header.Set("Content-Type", "application/json")
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, req)
		}(id)
	}

	// Concurrent appends (unique goals to bypass dedup).
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			srv.appendBrainInbox("new-"+string(rune('0'+idx)), "new-goal-"+string(rune('0'+idx)))
		}(i)
	}

	wg.Wait()

	// Verify file is valid JSON and contains both original + new items.
	data, err := os.ReadFile(filepath.Join(brainDir, "brain_inbox.json"))
	if err != nil {
		t.Fatalf("cannot read brain_inbox.json: %v", err)
	}
	var result []map[string]interface{}
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("corrupt JSON after mixed concurrent ops: %v", err)
	}
	// Original 2 items + up to 5 appended (some may dedup).
	if len(result) < 2 {
		t.Errorf("expected at least 2 items, got %d (data loss!)", len(result))
	}
}

// ─── Rate limit middleware integration ───────────────────────────

func TestRateLimitMiddleware_LocalhostExempt(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// Localhost should never be rate-limited.
	for i := 0; i < tokenBucketCapacity+10; i++ {
		req := httptest.NewRequest(http.MethodGet, "/status", nil)
		req.RemoteAddr = "127.0.0.1:9999"
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		if rr.Code == http.StatusTooManyRequests {
			t.Fatalf("localhost was rate-limited on request %d", i+1)
		}
	}
}

func TestRateLimitMiddleware_NonLocalhost(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// External IP should be rate-limited after tokenBucketCapacity.
	denied := 0
	for i := 0; i < tokenBucketCapacity+10; i++ {
		req := httptest.NewRequest(http.MethodGet, "/status", nil)
		req.RemoteAddr = "192.168.1.100:9999"
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		if rr.Code == http.StatusTooManyRequests {
			denied++
		}
	}
	if denied == 0 {
		t.Error("expected some requests to be rate-limited for non-localhost IP")
	}
}

// ─── handleGodFeed with concurrent appendGodFeed writer stress ───

func TestHandleGodFeedStress(t *testing.T) {
	// Stress test: many writers + many readers simultaneously.
	// The race detector should catch any unsynchronized access.
	cleanup := backupAndSetup(t, "god_feed.json", []byte("[]"))
	defer cleanup()

	srv := newTestServer("alpha")

	const writers = 10
	const readers = 10
	const ops = 30
	var wg sync.WaitGroup

	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < ops; j++ {
				srv.appendGodFeed("stress", "write")
			}
		}()
	}

	handler := srv.Handler()
	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < ops; j++ {
				req := httptest.NewRequest(http.MethodGet, "/god_feed", nil)
				req.RemoteAddr = "127.0.0.1:9999"
				rr := httptest.NewRecorder()
				handler.ServeHTTP(rr, req)
			}
		}()
	}

	wg.Wait()

	// Verify the final file is valid JSON.
	data, err := os.ReadFile(filepath.Join(brainDir, "god_feed.json"))
	if err != nil {
		t.Fatalf("cannot read god_feed.json: %v", err)
	}
	var result []interface{}
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("god_feed.json is corrupt after stress: %v", err)
	}
	if len(result) == 0 {
		t.Error("expected entries after stress writes")
	}
}

// ─── Token bucket cleanup ────────────────────────────────────────

func TestStartCleanup_EvictsStale(t *testing.T) {
	srv := newTestServer("alpha")

	// Insert a bucket with stale lastRefill (60s ago).
	staleBucket := &tokenBucket{}
	staleBucket.lastRefill.Store(time.Now().Add(-60 * time.Second).UnixNano())
	srv.rateBuckets.Store("10.0.0.1", staleBucket)

	// Insert a fresh bucket.
	freshBucket := &tokenBucket{}
	freshBucket.lastRefill.Store(time.Now().UnixNano())
	srv.rateBuckets.Store("10.0.0.2", freshBucket)

	// Run one cleanup cycle manually (same logic as StartCleanup).
	cutoff := time.Now().Add(-30 * time.Second).UnixNano()
	srv.rateBuckets.Range(func(key, value interface{}) bool {
		if tb, ok := value.(*tokenBucket); ok {
			if tb.lastRefill.Load() < cutoff {
				srv.rateBuckets.Delete(key)
			}
		}
		return true
	})

	// Stale bucket should be evicted, fresh should remain.
	if _, ok := srv.rateBuckets.Load("10.0.0.1"); ok {
		t.Error("stale bucket was not evicted")
	}
	if _, ok := srv.rateBuckets.Load("10.0.0.2"); !ok {
		t.Error("fresh bucket was incorrectly evicted")
	}
}
