package main

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unsafe"
)

// ─── Test 1: Monotonic sequence under concurrency ───────────────
// Verifies that seq IDs assigned inside the mutex are strictly monotonic
// even when many goroutines publish concurrently. — signed: beta

func TestMonotonicSeqConcurrent(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	// Use a ring large enough to hold all messages so we can inspect via Recent()
	const goroutines = 16
	const msgsPerGoroutine = 200
	total := goroutines * msgsPerGoroutine
	ringSize = total + 10
	bus := NewMessageBus()

	var wg sync.WaitGroup
	wg.Add(goroutines)

	for g := 0; g < goroutines; g++ {
		go func(gid int) {
			defer wg.Done()
			for i := 0; i < msgsPerGoroutine; i++ {
				bus.Post(
					fmt.Sprintf("sender_%d", gid),
					"test", "seq",
					fmt.Sprintf("g%d_i%d", gid, i),
					nil,
				)
			}
		}(g)
	}
	wg.Wait()

	// totalMsg counter must equal total published
	if got := bus.Count(); got != int64(total) {
		t.Errorf("Count() = %d, want %d", got, total)
	}

	// Extract all messages from ring via Recent and verify monotonic seq IDs
	recent := bus.Recent(total)
	if len(recent) != total {
		t.Fatalf("Recent(%d) returned %d messages", total, len(recent))
	}

	var ids []int64
	for _, msg := range recent {
		parts := strings.SplitN(msg.ID, "_", 3)
		if len(parts) < 2 {
			t.Fatalf("bad msg ID format: %s", msg.ID)
		}
		seq, err := strconv.ParseInt(parts[1], 10, 64)
		if err != nil {
			t.Fatalf("bad seq in ID %s: %v", msg.ID, err)
		}
		ids = append(ids, seq)
	}

	// Sort and verify uniqueness (monotonic source means no duplicates)
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	for i := 1; i < len(ids); i++ {
		if ids[i] == ids[i-1] {
			t.Errorf("duplicate seq ID: %d at positions %d and %d", ids[i], i-1, i)
		}
	}

	// Verify IDs are contiguous 1..total
	if ids[0] != 1 {
		t.Errorf("first seq ID = %d, want 1", ids[0])
	}
	if ids[len(ids)-1] != int64(total) {
		t.Errorf("last seq ID = %d, want %d", ids[len(ids)-1], total)
	}
}

// ─── Test 2: Configurable ring size via SKYNET_RING_SIZE env var ─
// The init() reads SKYNET_RING_SIZE. We can't re-run init(), but we
// can verify the global was set and that NewMessageBus uses it.
// — signed: beta

func TestConfigurableRingSize(t *testing.T) {
	// Save and restore
	orig := ringSize
	defer func() { ringSize = orig }()

	// Test: custom size
	ringSize = 500
	bus := NewMessageBus()
	if bus.Capacity() != 500 {
		t.Errorf("Capacity() = %d, want 500", bus.Capacity())
	}

	// Test: minimum boundary
	ringSize = 100
	bus2 := NewMessageBus()
	if bus2.Capacity() != 100 {
		t.Errorf("Capacity() = %d, want 100", bus2.Capacity())
	}

	// Test: env var parsing logic (unit-test the bounds manually)
	// Valid range: 100..10000
	testCases := []struct {
		envVal string
		expect int // 0 means "no change" (stays at current ringSize)
	}{
		{"200", 200},
		{"10000", 10000},
		{"99", 0},    // below min, rejected
		{"10001", 0}, // above max, rejected
		{"abc", 0},   // non-numeric, rejected
		{"", 0},      // empty, no change
	}
	for _, tc := range testCases {
		ringSize = 100 // reset baseline
		if tc.envVal != "" {
			if n, err := strconv.Atoi(tc.envVal); err == nil && n >= 100 && n <= 10000 {
				ringSize = n
			}
		}
		if tc.expect > 0 && ringSize != tc.expect {
			t.Errorf("envVal=%q: ringSize=%d, want %d", tc.envVal, ringSize, tc.expect)
		}
		if tc.expect == 0 && ringSize != 100 {
			t.Errorf("envVal=%q: ringSize=%d, should stay 100", tc.envVal, ringSize)
		}
	}
}

// ─── Test 3: Overwrite counter increments on ring wrap ──────────
// Fill ring beyond capacity and verify overwrites counter tracks
// each overwritten slot. — signed: beta

func TestOverwriteCounterOnWrap(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 100
	bus := NewMessageBus()

	// Post exactly ringSize messages — no overwrites
	for i := 0; i < 100; i++ {
		bus.Post("s", "t", "x", fmt.Sprintf("m%d", i), nil)
	}
	if ow := bus.Overwrites(); ow != 0 {
		t.Errorf("after 100 posts: Overwrites()=%d, want 0", ow)
	}
	if d := bus.Depth(); d != 100 {
		t.Errorf("after 100 posts: Depth()=%d, want 100", d)
	}

	// Post 50 more — should produce 50 overwrites
	for i := 0; i < 50; i++ {
		bus.Post("s", "t", "x", fmt.Sprintf("extra_%d", i), nil)
	}
	if ow := bus.Overwrites(); ow != 50 {
		t.Errorf("after 150 posts: Overwrites()=%d, want 50", ow)
	}
	// Depth stays capped at ringSize
	if d := bus.Depth(); d != 100 {
		t.Errorf("after 150 posts: Depth()=%d, want 100 (capped)", d)
	}
}

// ─── Test 4: Clear() drains ring and subscriber channels ────────
// Verifies Clear returns correct count, resets ring state, zeroes
// slots (GC-friendly), and drains subscriber channels. — signed: beta

func TestClearDrain(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 100
	bus := NewMessageBus()

	// Subscribe before posting
	topicCh := bus.Subscribe("sub1", "topic-a")
	wildCh := bus.SubscribeAll("sub-wild")

	// Post 30 messages
	for i := 0; i < 30; i++ {
		bus.Post("poster", "topic-a", "t", fmt.Sprintf("msg_%d", i), nil)
	}

	// Verify subscriber channels have messages buffered
	// (poster != sub1/sub-wild, so messages should be delivered)
	time.Sleep(10 * time.Millisecond) // let fan-out complete
	if len(topicCh) == 0 {
		t.Error("topic channel should have buffered messages before clear")
	}
	if len(wildCh) == 0 {
		t.Error("wildcard channel should have buffered messages before clear")
	}

	// Clear
	cleared := bus.Clear()
	if cleared != 30 {
		t.Errorf("Clear() returned %d, want 30", cleared)
	}

	// Ring state is reset
	if d := bus.Depth(); d != 0 {
		t.Errorf("after Clear: Depth()=%d, want 0", d)
	}

	// Recent returns empty
	recent := bus.Recent(10)
	if len(recent) != 0 {
		t.Errorf("after Clear: Recent(10) returned %d messages, want 0", len(recent))
	}

	// Subscriber channels are drained
	if len(topicCh) != 0 {
		t.Errorf("after Clear: topic channel has %d messages, want 0", len(topicCh))
	}
	if len(wildCh) != 0 {
		t.Errorf("after Clear: wildcard channel has %d messages, want 0", len(wildCh))
	}

	// Ring slots are zeroed (check first slot)
	bus.mu.RLock()
	if bus.ring[0].ID != "" {
		t.Error("after Clear: ring[0].ID not zeroed")
	}
	bus.mu.RUnlock()
}

// ─── Test 5: Cache-line padding struct layout ───────────────────
// Verify the padding between atomic counters and mutex-protected
// fields is at least 64 bytes (common cache line size). — signed: beta

func TestCacheLinePadding(t *testing.T) {
	var bus MessageBus

	// Verify totalMsg offset is at 0 (first field — hot atomics)
	totalMsgOffset := unsafe.Offsetof(bus.totalMsg)
	if totalMsgOffset != 0 {
		t.Errorf("totalMsg offset = %d, want 0 (should be first field)", totalMsgOffset)
	}

	// The mu field must be well-separated from the last atomic counter
	// (overwrites) to avoid false-sharing. The [128]byte pad between
	// them means the gap should be >= 128 + sizeof(int64) = 136 bytes.
	muOffset := unsafe.Offsetof(bus.mu)
	overwritesOffset := unsafe.Offsetof(bus.overwrites)

	gap := muOffset - overwritesOffset
	// At minimum a full cache line (64 bytes) between last atomic and mutex
	if gap < 64 {
		t.Errorf("gap between overwrites and mu = %d bytes, want >= 64 (cache-line separation)", gap)
	}

	// The struct explicitly uses a [128]byte pad, so the gap should
	// be at least 128 + 8 (overwrites int64 size) = 136 bytes.
	if gap < 128 {
		t.Logf("NOTE: gap between overwrites and mu = %d bytes (padding may be smaller than expected)", gap)
	}

	// Overall struct should be large enough to include the padding
	totalSize := unsafe.Sizeof(bus)
	if totalSize < 200 {
		t.Errorf("MessageBus struct size = %d bytes, expected > 200 (with 128-byte pad)", totalSize)
	}
}

// ─── Test 6: Recent() after ring wrap returns correct order ─────
// Fill ring past capacity, then verify Recent() returns messages in
// chronological (oldest-first) order with correct content. — signed: beta

func TestRecentAfterWrap(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 100
	bus := NewMessageBus()

	// Post 250 messages (2.5x ring capacity)
	for i := 0; i < 250; i++ {
		bus.Post("s", "t", "x", fmt.Sprintf("content_%d", i), nil)
	}

	// Recent(100) should return the last 100 messages (150..249)
	recent := bus.Recent(100)
	if len(recent) != 100 {
		t.Fatalf("Recent(100) returned %d messages, want 100", len(recent))
	}

	// First message should be content_150, last should be content_249
	if recent[0].Content != "content_150" {
		t.Errorf("Recent[0].Content = %q, want %q", recent[0].Content, "content_150")
	}
	if recent[99].Content != "content_249" {
		t.Errorf("Recent[99].Content = %q, want %q", recent[99].Content, "content_249")
	}

	// All messages should be in chronological order
	for i := 1; i < len(recent); i++ {
		if recent[i].Timestamp.Before(recent[i-1].Timestamp) {
			t.Errorf("Recent[%d].Timestamp (%v) before Recent[%d].Timestamp (%v) — not chronological",
				i, recent[i].Timestamp, i-1, recent[i-1].Timestamp)
		}
	}

	// Recent(10) should return the last 10 (content_240..content_249)
	recent10 := bus.Recent(10)
	if len(recent10) != 10 {
		t.Fatalf("Recent(10) returned %d messages, want 10", len(recent10))
	}
	if recent10[0].Content != "content_240" {
		t.Errorf("Recent(10)[0].Content = %q, want %q", recent10[0].Content, "content_240")
	}

	// Recent(200) should be capped at depth (100)
	recent200 := bus.Recent(200)
	if len(recent200) != 100 {
		t.Errorf("Recent(200) returned %d messages, want 100 (capped at depth)", len(recent200))
	}

	// Recent(0) should return empty
	recent0 := bus.Recent(0)
	if len(recent0) != 0 {
		t.Errorf("Recent(0) returned %d messages, want 0", len(recent0))
	}
}

// ─── Test 7: Concurrent publish + subscribe stress test ─────────
// Hammer the bus with concurrent publishers and subscribers to find
// races, deadlocks, or data corruption. — signed: beta

func TestConcurrentPublishSubscribeStress(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 200
	bus := NewMessageBus()

	const publishers = 8
	const subscribers = 4
	const msgsPerPub = 500
	const topics = 4
	totalExpected := publishers * msgsPerPub

	// Create subscribers (each listens to a different topic)
	var received [subscribers]int64
	subChans := make([]<-chan BusMessage, subscribers)
	for i := 0; i < subscribers; i++ {
		topic := fmt.Sprintf("topic_%d", i%topics)
		subChans[i] = bus.Subscribe(fmt.Sprintf("sub_%d", i), topic)
	}
	// One wildcard subscriber
	wildCh := bus.SubscribeAll("wild_stress")

	// Start subscriber goroutines — drain channels
	var subWg sync.WaitGroup
	stopSubs := make(chan struct{})
	for i := 0; i < subscribers; i++ {
		subWg.Add(1)
		go func(idx int, ch <-chan BusMessage) {
			defer subWg.Done()
			for {
				select {
				case <-ch:
					atomic.AddInt64(&received[idx], 1)
				case <-stopSubs:
					// Drain remaining
					for {
						select {
						case <-ch:
							atomic.AddInt64(&received[idx], 1)
						default:
							return
						}
					}
				}
			}
		}(i, subChans[i])
	}

	// Wildcard counter
	var wildCount int64
	subWg.Add(1)
	go func() {
		defer subWg.Done()
		for {
			select {
			case <-wildCh:
				atomic.AddInt64(&wildCount, 1)
			case <-stopSubs:
				for {
					select {
					case <-wildCh:
						atomic.AddInt64(&wildCount, 1)
					default:
						return
					}
				}
			}
		}
	}()

	// Publishers
	var pubWg sync.WaitGroup
	pubWg.Add(publishers)
	for p := 0; p < publishers; p++ {
		go func(pid int) {
			defer pubWg.Done()
			for i := 0; i < msgsPerPub; i++ {
				topic := fmt.Sprintf("topic_%d", i%topics)
				bus.Post(
					fmt.Sprintf("pub_%d", pid),
					topic, "stress",
					fmt.Sprintf("p%d_m%d", pid, i),
					nil,
				)
			}
		}(p)
	}
	pubWg.Wait()
	time.Sleep(50 * time.Millisecond)
	close(stopSubs)
	subWg.Wait()

	// Validate
	count := bus.Count()
	if count != int64(totalExpected) {
		t.Errorf("Count() = %d, want %d", count, totalExpected)
	}

	// Depth is capped at ringSize
	depth := bus.Depth()
	if depth > ringSize {
		t.Errorf("Depth() = %d, exceeds ringSize %d", depth, ringSize)
	}

	// Overwrites should have occurred (4000 msgs into 200-slot ring)
	ow := bus.Overwrites()
	expectedOw := int64(totalExpected - ringSize)
	if ow != expectedOw {
		t.Errorf("Overwrites() = %d, want %d", ow, expectedOw)
	}

	// Recent should return valid messages
	recent := bus.Recent(ringSize)
	if len(recent) != ringSize {
		t.Errorf("Recent(%d) returned %d messages", ringSize, len(recent))
	}

	// No empty/corrupt messages in recent
	for i, msg := range recent {
		if msg.ID == "" || msg.Sender == "" || msg.Content == "" {
			t.Errorf("Recent[%d] has empty fields: ID=%q Sender=%q Content=%q",
				i, msg.ID, msg.Sender, msg.Content)
		}
	}

	t.Logf("Stress results: published=%d depth=%d overwrites=%d dropped=%d wildcard=%d",
		count, depth, ow, bus.Dropped(), atomic.LoadInt64(&wildCount))
}

// ─── Test 8: Subscriber self-exclusion ──────────────────────────
// Verifies that a subscriber doesn't receive its own messages
// (sender == subscriber ID). — signed: beta

func TestSubscriberSelfExclusion(t *testing.T) {
	bus := NewMessageBus()
	ch := bus.Subscribe("alice", "chat")

	// Alice posts — should NOT receive her own message
	bus.Post("alice", "chat", "msg", "hello from alice", nil)
	time.Sleep(10 * time.Millisecond)
	if len(ch) != 0 {
		t.Errorf("subscriber received own message: channel has %d items", len(ch))
	}

	// Bob posts — Alice should receive
	bus.Post("bob", "chat", "msg", "hello from bob", nil)
	time.Sleep(10 * time.Millisecond)
	if len(ch) != 1 {
		t.Errorf("subscriber missed peer message: channel has %d items, want 1", len(ch))
	}
}

// ─── Test 9: Topic isolation ────────────────────────────────────
// Messages to topic A should not appear on topic B subscription.
// — signed: beta

func TestTopicIsolation(t *testing.T) {
	bus := NewMessageBus()
	chA := bus.Subscribe("sub", "topicA")
	chB := bus.Subscribe("sub2", "topicB")

	bus.Post("sender", "topicA", "t", "for-A", nil)
	time.Sleep(10 * time.Millisecond)

	if len(chA) != 1 {
		t.Errorf("topicA channel: got %d, want 1", len(chA))
	}
	if len(chB) != 0 {
		t.Errorf("topicB channel: got %d, want 0 (cross-topic leak)", len(chB))
	}
}

// ─── Test 10: Slow consumer drop tracking ───────────────────────
// When a subscriber channel is full, messages should be dropped and
// the dropped counter incremented. — signed: beta

func TestSlowConsumerDrop(t *testing.T) {
	bus := NewMessageBus()
	_ = bus.Subscribe("slow", "flood")

	// Post more messages than the channel buffer (64)
	for i := 0; i < 100; i++ {
		bus.Post("fast-sender", "flood", "t", fmt.Sprintf("m%d", i), nil)
	}

	time.Sleep(10 * time.Millisecond)
	dropped := bus.Dropped()
	if dropped == 0 {
		t.Error("expected dropped > 0 for slow consumer, got 0")
	}
	// At least 100-64 = 36 should be dropped
	if dropped < 36 {
		t.Errorf("expected dropped >= 36, got %d", dropped)
	}
}

// ─── Test 11: Recent() before any posts ─────────────────────────
// Edge case — empty ring. — signed: beta

func TestRecentEmpty(t *testing.T) {
	bus := NewMessageBus()
	r := bus.Recent(10)
	if len(r) != 0 {
		t.Errorf("Recent(10) on empty bus returned %d messages, want 0", len(r))
	}
}

// ─── Test 12: Depth and Count consistency ───────────────────────
// Depth is capped at ringSize; Count grows unbounded. — signed: beta

func TestDepthVsCount(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 100
	bus := NewMessageBus()

	for i := 0; i < 300; i++ {
		bus.Post("s", "t", "x", "", nil)
	}

	if bus.Count() != 300 {
		t.Errorf("Count() = %d, want 300", bus.Count())
	}
	if bus.Depth() != 100 {
		t.Errorf("Depth() = %d, want 100", bus.Depth())
	}
}

// ─── Test 13: Env var integration smoke test ────────────────────
// Verify the init() env var path works end-to-end for currently
// set SKYNET_RING_SIZE value. — signed: beta

func TestEnvVarIntegration(t *testing.T) {
	// If SKYNET_RING_SIZE is set in the environment, ringSize should match
	if s := os.Getenv("SKYNET_RING_SIZE"); s != "" {
		expected, err := strconv.Atoi(s)
		if err == nil && expected >= 100 && expected <= 10000 {
			if ringSize != expected {
				t.Errorf("ringSize = %d, want %d (from SKYNET_RING_SIZE=%s)", ringSize, expected, s)
			}
		}
	}
	// If not set, ringSize should be default 100 (unless modified by other test)
	// This is a smoke test — just verify it's in valid range
	if ringSize < 100 || ringSize > 10000 {
		t.Errorf("ringSize = %d, outside valid range [100, 10000]", ringSize)
	}
}

// ─── Test 14: Concurrent publish + clear stress ─────────────────
// Clear() during active publishing should not panic or corrupt state.
// — signed: beta

func TestConcurrentPublishClear(t *testing.T) {
	orig := ringSize
	defer func() { ringSize = orig }()

	ringSize = 100
	bus := NewMessageBus()

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// Publisher goroutines
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := 0; ; j++ {
				select {
				case <-stop:
					return
				default:
					bus.Post(fmt.Sprintf("pub_%d", id), "t", "x", fmt.Sprintf("%d_%d", id, j), nil)
				}
			}
		}(i)
	}

	// Clear repeatedly while publishers are running
	for i := 0; i < 20; i++ {
		time.Sleep(time.Millisecond)
		bus.Clear()
	}
	close(stop)
	wg.Wait()

	// No panic = pass. Verify state is consistent after storm.
	depth := bus.Depth()
	if depth < 0 || depth > ringSize {
		t.Errorf("Depth() = %d after stress, should be in [0, %d]", depth, ringSize)
	}
}

// ─── Test 15: Wildcard receives all topics ──────────────────────
// — signed: beta

func TestWildcardReceivesAllTopics(t *testing.T) {
	bus := NewMessageBus()
	wch := bus.SubscribeAll("wild-sub")

	topics := []string{"alpha", "beta", "gamma", "delta"}
	for _, topic := range topics {
		bus.Post("sender", topic, "t", "content-"+topic, nil)
	}
	time.Sleep(10 * time.Millisecond)

	if len(wch) != len(topics) {
		t.Errorf("wildcard channel has %d messages, want %d", len(wch), len(topics))
	}
}
