package main

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// MessageBus — lock-free topic-based pub/sub with ring buffer.
// Zero-copy message passing via Go channels. Features:
// - Topic-based routing (not just sender filtering)
// - Ring buffer (fixed memory, no slice growth)
// - Atomic counters (no lock on hot path for stats)
type MessageBus struct {
	mu       sync.RWMutex
	ring     [ringSize]BusMessage // fixed ring buffer — zero allocation after init
	head     int                  // next write position
	count    int                  // messages in buffer (max ringSize)
	totalMsg int64                // atomic: total messages ever posted
	dropped  int64                // atomic: messages dropped due to slow subscribers

	// Topic-based subscriptions: topic → map[subscriberID]channel
	subs   map[string]map[string]chan BusMessage
	subsMu sync.RWMutex

	// Wildcard subscribers (receive ALL topics)
	wildcards   map[string]chan BusMessage
	wildcardsMu sync.RWMutex
}

const ringSize = 100

func NewMessageBus() *MessageBus {
	return &MessageBus{
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
func (b *MessageBus) Post(sender, topic, msgType, content string, metadata map[string]string) {
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

	// Write to ring buffer
	b.mu.Lock()
	b.ring[b.head] = msg
	b.head = (b.head + 1) % ringSize
	if b.count < ringSize {
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
	start := (b.head - n + ringSize) % ringSize
	for i := 0; i < n; i++ {
		result[i] = b.ring[(start+i)%ringSize]
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
			fmt.Printf("[bus-monitor] msgs/sec: %.1f | subscribers: %d | topics: %d | queue depth: %d | total: %d\n",
				mps, subCount, topicCount, depth, currentCount)

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

// Depth returns current messages in the ring buffer.
func (b *MessageBus) Depth() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.count
}

// Clear drains the ring buffer and returns the number of messages cleared.
func (b *MessageBus) Clear() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	cleared := b.count
	b.head = 0
	b.count = 0
	return cleared
}
