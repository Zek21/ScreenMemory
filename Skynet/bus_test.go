package main

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestNewMessageBus(t *testing.T) {
	bus := NewMessageBus()
	if bus == nil {
		t.Fatal("NewMessageBus returned nil")
	}
	if bus.Count() != 0 {
		t.Errorf("expected 0 messages, got %d", bus.Count())
	}
	if bus.Dropped() != 0 {
		t.Errorf("expected 0 dropped, got %d", bus.Dropped())
	}
	if bus.Depth() != 0 {
		t.Errorf("expected depth 0, got %d", bus.Depth())
	}
}

func TestBusPostAndRecent(t *testing.T) {
	bus := NewMessageBus()

	bus.Post("alpha", "test", "report", "hello world", nil)

	if bus.Count() != 1 {
		t.Errorf("expected 1 message, got %d", bus.Count())
	}
	if bus.Depth() != 1 {
		t.Errorf("expected depth 1, got %d", bus.Depth())
	}

	msgs := bus.Recent(10)
	if len(msgs) != 1 {
		t.Fatalf("expected 1 recent message, got %d", len(msgs))
	}
	if msgs[0].Sender != "alpha" {
		t.Errorf("expected sender 'alpha', got '%s'", msgs[0].Sender)
	}
	if msgs[0].Topic != "test" {
		t.Errorf("expected topic 'test', got '%s'", msgs[0].Topic)
	}
	if msgs[0].Type != "report" {
		t.Errorf("expected type 'report', got '%s'", msgs[0].Type)
	}
	if msgs[0].Content != "hello world" {
		t.Errorf("expected content 'hello world', got '%s'", msgs[0].Content)
	}
}

func TestBusRecentLimit(t *testing.T) {
	bus := NewMessageBus()

	for i := 0; i < 10; i++ {
		bus.Post("sender", "topic", "type", fmt.Sprintf("msg_%d", i), nil)
	}

	msgs := bus.Recent(5)
	if len(msgs) != 5 {
		t.Fatalf("expected 5 messages, got %d", len(msgs))
	}
	// Should get the last 5 messages (msg_5 through msg_9)
	if msgs[0].Content != "msg_5" {
		t.Errorf("expected first message 'msg_5', got '%s'", msgs[0].Content)
	}
	if msgs[4].Content != "msg_9" {
		t.Errorf("expected last message 'msg_9', got '%s'", msgs[4].Content)
	}
}

func TestBusRecentMoreThanAvailable(t *testing.T) {
	bus := NewMessageBus()
	bus.Post("a", "t", "r", "only one", nil)

	msgs := bus.Recent(100)
	if len(msgs) != 1 {
		t.Errorf("expected 1 message when requesting more than available, got %d", len(msgs))
	}
}

func TestBusRecentEmpty(t *testing.T) {
	bus := NewMessageBus()
	msgs := bus.Recent(10)
	if len(msgs) != 0 {
		t.Errorf("expected 0 messages on empty bus, got %d", len(msgs))
	}
}

func TestBusRingBufferOverflow(t *testing.T) {
	bus := NewMessageBus()

	// Post more than ringSize messages
	for i := 0; i < ringSize+50; i++ {
		bus.Post("sender", "topic", "type", fmt.Sprintf("msg_%d", i), nil)
	}

	// Count should be capped at ringSize
	if bus.Depth() != ringSize {
		t.Errorf("expected depth %d after overflow, got %d", ringSize, bus.Depth())
	}

	// Total count should reflect all messages posted
	if bus.Count() != int64(ringSize+50) {
		t.Errorf("expected total count %d, got %d", ringSize+50, bus.Count())
	}

	// Recent should return messages from the end
	msgs := bus.Recent(5)
	if len(msgs) != 5 {
		t.Fatalf("expected 5 messages, got %d", len(msgs))
	}
	expectedLast := fmt.Sprintf("msg_%d", ringSize+49)
	if msgs[4].Content != expectedLast {
		t.Errorf("expected last message '%s', got '%s'", expectedLast, msgs[4].Content)
	}
}

func TestBusPostWithMetadata(t *testing.T) {
	bus := NewMessageBus()

	meta := map[string]string{"key": "value", "worker": "alpha"}
	bus.Post("sender", "topic", "report", "content", meta)

	msgs := bus.Recent(1)
	if len(msgs) != 1 {
		t.Fatal("expected 1 message")
	}
	if msgs[0].Metadata["key"] != "value" {
		t.Errorf("expected metadata key='value', got '%s'", msgs[0].Metadata["key"])
	}
	if msgs[0].Metadata["worker"] != "alpha" {
		t.Errorf("expected metadata worker='alpha', got '%s'", msgs[0].Metadata["worker"])
	}
}

func TestBusMessageID(t *testing.T) {
	bus := NewMessageBus()
	bus.Post("alpha", "topic", "report", "test", nil)
	bus.Post("beta", "topic", "report", "test2", nil)

	msgs := bus.Recent(2)
	if msgs[0].ID == msgs[1].ID {
		t.Error("message IDs should be unique")
	}
	if msgs[0].ID == "" || msgs[1].ID == "" {
		t.Error("message IDs should not be empty")
	}
}

func TestBusSubscribe(t *testing.T) {
	bus := NewMessageBus()

	ch := bus.Subscribe("test-sub", "results")

	// Post to the subscribed topic
	bus.Post("alpha", "results", "report", "result data", nil)

	// Post to a different topic (should NOT appear)
	bus.Post("beta", "other", "info", "other data", nil)

	select {
	case msg := <-ch:
		if msg.Content != "result data" {
			t.Errorf("expected 'result data', got '%s'", msg.Content)
		}
	case <-time.After(100 * time.Millisecond):
		t.Error("expected to receive subscribed message")
	}

	// Ensure the other-topic message was not delivered
	select {
	case msg := <-ch:
		t.Errorf("unexpected message on topic subscription: %s", msg.Content)
	case <-time.After(50 * time.Millisecond):
		// Good, no extra message
	}
}

func TestBusSubscribeSenderExclusion(t *testing.T) {
	bus := NewMessageBus()

	ch := bus.Subscribe("alpha", "results")

	// Post from the same sender — should NOT be delivered to self
	bus.Post("alpha", "results", "report", "self msg", nil)

	select {
	case msg := <-ch:
		t.Errorf("subscriber should not receive own messages, got: %s", msg.Content)
	case <-time.After(50 * time.Millisecond):
		// Good
	}

	// Post from a different sender — SHOULD be delivered
	bus.Post("beta", "results", "report", "beta msg", nil)

	select {
	case msg := <-ch:
		if msg.Content != "beta msg" {
			t.Errorf("expected 'beta msg', got '%s'", msg.Content)
		}
	case <-time.After(100 * time.Millisecond):
		t.Error("expected to receive message from different sender")
	}
}

func TestBusSubscribeAll(t *testing.T) {
	bus := NewMessageBus()

	ch := bus.SubscribeAll("monitor")

	bus.Post("alpha", "results", "report", "msg1", nil)
	bus.Post("beta", "system", "alert", "msg2", nil)

	received := 0
	for i := 0; i < 2; i++ {
		select {
		case <-ch:
			received++
		case <-time.After(100 * time.Millisecond):
			break
		}
	}

	if received != 2 {
		t.Errorf("wildcard subscriber should receive all topics, got %d/2", received)
	}
}

func TestBusClear(t *testing.T) {
	bus := NewMessageBus()

	for i := 0; i < 10; i++ {
		bus.Post("s", "t", "r", "c", nil)
	}
	if bus.Depth() != 10 {
		t.Errorf("expected depth 10, got %d", bus.Depth())
	}

	cleared := bus.Clear()
	if cleared != 10 {
		t.Errorf("expected 10 cleared, got %d", cleared)
	}
	if bus.Depth() != 0 {
		t.Errorf("expected depth 0 after clear, got %d", bus.Depth())
	}

	// Total count should still reflect historical messages
	if bus.Count() != 10 {
		t.Errorf("total count should still be 10 after clear, got %d", bus.Count())
	}
}

func TestBusConcurrentPosts(t *testing.T) {
	bus := NewMessageBus()

	var wg sync.WaitGroup
	numGoroutines := 100
	msgsPerGoroutine := 10

	for g := 0; g < numGoroutines; g++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for i := 0; i < msgsPerGoroutine; i++ {
				bus.Post(fmt.Sprintf("sender_%d", id), "topic", "msg",
					fmt.Sprintf("msg_%d_%d", id, i), nil)
			}
		}(g)
	}

	wg.Wait()

	expectedTotal := int64(numGoroutines * msgsPerGoroutine)
	if bus.Count() != expectedTotal {
		t.Errorf("expected total count %d, got %d", expectedTotal, bus.Count())
	}
}

func TestBusConcurrentSubscribeAndPost(t *testing.T) {
	bus := NewMessageBus()

	ch := bus.Subscribe("listener", "events")

	var wg sync.WaitGroup
	numMessages := 50

	// Post messages concurrently
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < numMessages; i++ {
			bus.Post("producer", "events", "data", fmt.Sprintf("event_%d", i), nil)
		}
	}()

	// Receive messages
	received := 0
	done := make(chan struct{})
	go func() {
		for range ch {
			received++
			if received >= numMessages {
				break
			}
		}
		close(done)
	}()

	wg.Wait()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		// OK if not all received (channel buffer might be full)
	}

	if received == 0 {
		t.Error("subscriber received 0 messages during concurrent test")
	}
}

func TestBusMonitorCancellation(t *testing.T) {
	bus := NewMessageBus()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})

	go func() {
		bus.Monitor(ctx)
		close(done)
	}()

	cancel()

	select {
	case <-done:
		// Monitor exited cleanly
	case <-time.After(2 * time.Second):
		t.Error("Monitor did not exit after context cancellation")
	}
}

func TestBusMessageTimestamp(t *testing.T) {
	bus := NewMessageBus()

	before := time.Now()
	bus.Post("s", "t", "r", "c", nil)
	after := time.Now()

	msgs := bus.Recent(1)
	if msgs[0].Timestamp.Before(before) || msgs[0].Timestamp.After(after) {
		t.Errorf("message timestamp %v should be between %v and %v",
			msgs[0].Timestamp, before, after)
	}
}
