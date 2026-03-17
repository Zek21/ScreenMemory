package main

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// ringSize is the bus ring buffer capacity. Configurable via SKYNET_RING_SIZE
// env var (min 100, max 10000, default 100). — signed: alpha
var ringSize = 100

func init() {
	if s := os.Getenv("SKYNET_RING_SIZE"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n >= 100 && n <= 10000 {
			ringSize = n
		}
	}
}

// MessageBus — topic-based pub/sub with ring buffer.
// Mutex-serialised writes guarantee monotonic ID + timestamp ordering.
// Features:
// - Topic-based routing (not just sender filtering)
// - Configurable ring buffer (fixed memory, no slice growth after init)
// - Atomic counters for lock-free stats reads
// - Overwrite tracking for silent ring-wrap detection
//
// Struct layout: atomic counters are separated from mutex-protected fields
// by a cache-line pad to eliminate false sharing under concurrent access.
// — signed: alpha
type MessageBus struct {
	// --- Hot atomic counters (own cache line) ---
	totalMsg   int64 // atomic: total messages ever posted
	dropped    int64 // atomic: messages dropped due to slow subscribers
	overwrites int64 // atomic: messages overwritten by ring wrap — signed: alpha

	_ [128]byte // cache-line pad — prevents false sharing with mutex fields below — signed: alpha

	// --- Mutex-protected ring state ---
	mu    sync.RWMutex
	ring  []BusMessage // ring buffer — sized once in NewMessageBus
	head  int          // next write position
	count int          // messages in buffer (max ringSize)

	// Topic-based subscriptions: topic → map[subscriberID]channel
	subs   map[string]map[string]chan BusMessage
	subsMu sync.RWMutex

	// Wildcard subscribers (receive ALL topics)
	wildcards   map[string]chan BusMessage
	wildcardsMu sync.RWMutex
}

func NewMessageBus() *MessageBus {
	return &MessageBus{
		ring:      make([]BusMessage, ringSize),
		subs:      make(map[string]map[string]chan BusMessage),
		wildcards: make(map[string]chan BusMessage),
	}
}

// Subscribe to a specific topic. Returns a channel that receives matching messages.
func (b *MessageBus) Subscribe(subscriber, topic string) <-chan BusMessage {
	b.subsMu.Lock()
	defer b.subsMu.Unlock()

	ch := make(chan BusMessage, 64)
	if b.subs[topic] == nil {
		b.subs[topic] = make(map[string]chan BusMessage)
	}
	b.subs[topic][subscriber] = ch
	return ch
}

// SubscribeAll subscribes to ALL topics (wildcard).
func (b *MessageBus) SubscribeAll(subscriber string) <-chan BusMessage {
	b.wildcardsMu.Lock()
	defer b.wildcardsMu.Unlock()

	ch := make(chan BusMessage, 64)
	b.wildcards[subscriber] = ch
	return ch
}

// Post publishes a message to the bus. Fans out to topic subscribers + wildcards.
// Seq assignment and timestamp are inside the mutex so ring order matches ID
// order and timestamps are monotonically non-decreasing. — signed: alpha
func (b *MessageBus) Post(sender, topic, msgType, content string, metadata map[string]string) {
	// Write to ring buffer — seq + timestamp inside lock guarantees ordering
	b.mu.Lock()
	seq := atomic.AddInt64(&b.totalMsg, 1)
	msg := BusMessage{
		ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
		Sender:    sender,
		Topic:     topic,
		Type:      msgType,
		Content:   content,
		Metadata:  metadata,
		Timestamp: time.Now(),
	}
	if b.count >= len(b.ring) {
		atomic.AddInt64(&b.overwrites, 1) // track silent ring overwrites — signed: alpha
	}
	b.ring[b.head] = msg
	b.head = (b.head + 1) % len(b.ring)
	if b.count < len(b.ring) {
		b.count++
	}
	b.mu.Unlock()

	// Fan out to topic subscribers (non-blocking)
	b.subsMu.RLock()
	if topicSubs, ok := b.subs[topic]; ok {
		for id, ch := range topicSubs {
			if id != sender {
				select {
				case ch <- msg:
				default:
					atomic.AddInt64(&b.dropped, 1)
					fmt.Printf("[BUS] Dropped msg for subscriber %s on topic %s (slow consumer)\n", id, topic)
				}
			}
		}
	}
	b.subsMu.RUnlock()

	// Fan out to wildcard subscribers
	b.wildcardsMu.RLock()
	for id, ch := range b.wildcards {
		if id != sender {
			select {
			case ch <- msg:
			default:
				atomic.AddInt64(&b.dropped, 1)
				fmt.Printf("[BUS] Dropped wildcard msg for %s (slow consumer)\n", id)
			}
		}
	}
	b.wildcardsMu.RUnlock()
}

// Recent returns the last N messages from the ring buffer.
func (b *MessageBus) Recent(n int) []BusMessage {
	b.mu.RLock()
	defer b.mu.RUnlock()

	if n > b.count {
		n = b.count
	}
	if n == 0 {
		return []BusMessage{}
	}

	result := make([]BusMessage, n)
	start := (b.head - n + len(b.ring)) % len(b.ring)
	for i := 0; i < n; i++ {
		result[i] = b.ring[(start+i)%len(b.ring)]
	}
	return result
}

// Monitor prints bus stats every 30s to stdout.
func (b *MessageBus) Monitor(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	var lastCount int64
	lastTime := time.Now()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			now := time.Now()
			currentCount := atomic.LoadInt64(&b.totalMsg)
			elapsed := now.Sub(lastTime).Seconds()
			mps := float64(currentCount-lastCount) / elapsed

			b.subsMu.RLock()
			topicCount := len(b.subs)
			subCount := 0
			for _, subs := range b.subs {
				subCount += len(subs)
			}
			b.subsMu.RUnlock()

			b.wildcardsMu.RLock()
			subCount += len(b.wildcards)
			b.wildcardsMu.RUnlock()

			depth := b.Depth()
			overwritten := atomic.LoadInt64(&b.overwrites) // signed: alpha
			fmt.Printf("[bus-monitor] msgs/sec: %.1f | subscribers: %d | topics: %d | queue depth: %d | overwrites: %d | total: %d\n",
				mps, subCount, topicCount, depth, overwritten, currentCount)

			lastCount = currentCount
			lastTime = now
		}
	}
}

// Count returns total messages posted (atomic, no lock).
func (b *MessageBus) Count() int64 {
	return atomic.LoadInt64(&b.totalMsg)
}

// Dropped returns total messages dropped due to slow subscribers.
func (b *MessageBus) Dropped() int64 {
	return atomic.LoadInt64(&b.dropped)
}

// Overwrites returns total messages overwritten by ring buffer wrapping.
// — signed: alpha
func (b *MessageBus) Overwrites() int64 {
	return atomic.LoadInt64(&b.overwrites)
}

// Capacity returns the ring buffer size. — signed: alpha
func (b *MessageBus) Capacity() int {
	return len(b.ring)
}

// Depth returns current messages in the ring buffer.
func (b *MessageBus) Depth() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.count
}

// Clear drains the ring buffer and returns the number of messages cleared.
// Zeroes ring slots to release heap references and drains subscriber channels
// to prevent stale message delivery after a clear. — signed: alpha
func (b *MessageBus) Clear() int {
	b.mu.Lock()
	cleared := b.count
	b.head = 0
	b.count = 0
	for i := range b.ring {
		b.ring[i] = BusMessage{} // release string/map references for GC
	}
	b.mu.Unlock()

	// Drain buffered messages from all subscriber channels
	b.subsMu.RLock()
	for _, topicSubs := range b.subs {
		for _, ch := range topicSubs {
			drainChan(ch)
		}
	}
	b.subsMu.RUnlock()

	b.wildcardsMu.RLock()
	for _, ch := range b.wildcards {
		drainChan(ch)
	}
	b.wildcardsMu.RUnlock()

	return cleared
}

// drainChan removes all pending messages from a buffered channel.
// — signed: alpha
func drainChan(ch chan BusMessage) {
	for {
		select {
		case <-ch:
		default:
			return
		}
	}
}
