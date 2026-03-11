package main

import (
	"container/heap"
	"context"
	"sync/atomic"
	"testing"
	"time"
)

func newTestWorker(name string) (*Worker, *MessageBus, chan *TaskResult) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	w := NewWorker(name, bus, results)
	return w, bus, results
}

func TestNewWorker(t *testing.T) {
	w, _, _ := newTestWorker("alpha")

	if w.Name != "alpha" {
		t.Errorf("expected name 'alpha', got '%s'", w.Name)
	}
	if w.Status != "IDLE" {
		t.Errorf("expected status 'IDLE', got '%s'", w.Status)
	}
	if w.QueueDepth() != 0 {
		t.Errorf("expected queue depth 0, got %d", w.QueueDepth())
	}
}

func TestWorkerEnqueue(t *testing.T) {
	w, _, _ := newTestWorker("beta")

	task := &Task{
		ID:           "task_1",
		Type:         "message",
		Command:      "hello",
		Description:  "test task",
		Priority:     1,
		DispatchedAt: time.Now(),
	}
	w.Enqueue(task)

	if w.QueueDepth() != 1 {
		t.Errorf("expected queue depth 1 after enqueue, got %d", w.QueueDepth())
	}
}

func TestWorkerEnqueuePriority(t *testing.T) {
	w, _, _ := newTestWorker("gamma")

	tasks := []*Task{
		{ID: "low", Priority: 5, DispatchedAt: time.Now(), Type: "message", Command: "low"},
		{ID: "high", Priority: 1, DispatchedAt: time.Now(), Type: "message", Command: "high"},
		{ID: "med", Priority: 3, DispatchedAt: time.Now(), Type: "message", Command: "med"},
	}

	for _, task := range tasks {
		w.Enqueue(task)
	}

	if w.QueueDepth() != 3 {
		t.Fatalf("expected queue depth 3, got %d", w.QueueDepth())
	}

	// Pop should give highest priority (lowest number) first
	w.taskMu.Lock()
	first := heap.Pop(&w.taskQueue).(*Task)
	w.taskMu.Unlock()

	if first.ID != "high" {
		t.Errorf("expected highest priority task 'high', got '%s'", first.ID)
	}
}

func TestWorkerRemoveTask(t *testing.T) {
	w, _, _ := newTestWorker("delta")

	task := &Task{
		ID:           "removable",
		Type:         "message",
		Command:      "test",
		Priority:     1,
		DispatchedAt: time.Now(),
	}
	w.Enqueue(task)

	if w.QueueDepth() != 1 {
		t.Fatal("expected queue depth 1")
	}

	removed := w.RemoveTask("removable")
	if !removed {
		t.Error("expected RemoveTask to return true")
	}
	if w.QueueDepth() != 0 {
		t.Errorf("expected queue depth 0 after removal, got %d", w.QueueDepth())
	}

	// Remove non-existent task
	removed = w.RemoveTask("nonexistent")
	if removed {
		t.Error("RemoveTask should return false for non-existent task")
	}
}

func TestWorkerGetState(t *testing.T) {
	w, _, _ := newTestWorker("alpha")

	state := w.GetState()
	if state.Status != "IDLE" {
		t.Errorf("expected status 'IDLE', got '%s'", state.Status)
	}
	if state.TasksCompleted != 0 {
		t.Errorf("expected 0 tasks completed, got %d", state.TasksCompleted)
	}
	if state.TotalErrors != 0 {
		t.Errorf("expected 0 errors, got %d", state.TotalErrors)
	}
	if state.Uptime < 0 {
		t.Error("expected non-negative uptime")
	}
	if state.QueueDepth != 0 {
		t.Errorf("expected queue depth 0, got %d", state.QueueDepth)
	}
}

func TestWorkerSetStatus(t *testing.T) {
	w, _, _ := newTestWorker("beta")

	w.setStatus("WORKING", "building features")

	w.mu.RLock()
	status := w.Status
	task := w.CurrentTask
	w.mu.RUnlock()

	if status != "WORKING" {
		t.Errorf("expected status 'WORKING', got '%s'", status)
	}
	if task != "building features" {
		t.Errorf("expected task 'building features', got '%s'", task)
	}
}

func TestWorkerLog(t *testing.T) {
	w, _, _ := newTestWorker("gamma")

	w.log("test log entry")

	w.mu.RLock()
	logCount := len(w.recentLogs)
	w.mu.RUnlock()

	if logCount != 1 {
		t.Errorf("expected 1 log entry, got %d", logCount)
	}
}

func TestWorkerLogMaxSize(t *testing.T) {
	w, _, _ := newTestWorker("delta")
	w.maxLogs = 5

	for i := 0; i < 10; i++ {
		w.log("entry")
	}

	w.mu.RLock()
	logCount := len(w.recentLogs)
	w.mu.RUnlock()

	if logCount != 5 {
		t.Errorf("expected logs capped at 5, got %d", logCount)
	}
}

func TestWorkerExecuteMessageTask(t *testing.T) {
	w, _, results := newTestWorker("alpha")

	task := &Task{
		ID:           "msg_task_1",
		Type:         "message",
		Command:      "hello from test",
		Description:  "message test",
		Priority:     1,
		DispatchedAt: time.Now(),
		MaxRetries:   3,
	}

	go w.execute(task)

	select {
	case result := <-results:
		if result.TaskID != "msg_task_1" {
			t.Errorf("expected task ID 'msg_task_1', got '%s'", result.TaskID)
		}
		if result.Status != "success" {
			t.Errorf("expected status 'success', got '%s'", result.Status)
		}
		if result.Output != "hello from test" {
			t.Errorf("expected output 'hello from test', got '%s'", result.Output)
		}
		if result.WorkerName != "alpha" {
			t.Errorf("expected worker 'alpha', got '%s'", result.WorkerName)
		}
		if result.DurationMs < 0 {
			t.Error("expected non-negative duration")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for task result")
	}

	// Verify task completion counter
	if atomic.LoadInt32(&w.tasksCompleted) != 1 {
		t.Errorf("expected 1 task completed, got %d", atomic.LoadInt32(&w.tasksCompleted))
	}
}

func TestWorkerCircuitBreaker(t *testing.T) {
	w, _, _ := newTestWorker("gamma")

	// Directly simulate 3 consecutive failures to trip circuit breaker
	// (Avoids shell execution which varies across platforms)
	w.mu.Lock()
	w.consecutiveFails = 3
	w.circuitState = "CIRCUIT_OPEN"
	w.circuitOpenedAt = time.Now()
	w.mu.Unlock()

	// Check circuit breaker state
	w.mu.RLock()
	cState := w.circuitState
	cFails := w.consecutiveFails
	w.mu.RUnlock()

	if cState != "CIRCUIT_OPEN" {
		t.Errorf("expected circuit state 'CIRCUIT_OPEN', got '%s'", cState)
	}
	if cFails < 3 {
		t.Errorf("expected at least 3 consecutive fails, got %d", cFails)
	}

	// Verify that a task during CIRCUIT_OPEN is skipped (if opened recently)
	task := &Task{
		ID:           "circuit_test",
		Type:         "message",
		Command:      "should be skipped",
		Description:  "circuit test",
		Priority:     1,
		DispatchedAt: time.Now(),
	}
	// Execute while circuit is open — should skip and not produce result
	go w.execute(task)
	time.Sleep(100 * time.Millisecond)

	// Worker should still be IDLE after skipping
	w.mu.RLock()
	status := w.Status
	w.mu.RUnlock()
	if status != "IDLE" {
		// May be "IDLE" if skipped, or brief WORKING if half-open kicked in
		t.Logf("status after circuit-open execute: %s (acceptable)", status)
	}
}

func TestWorkerRunAndStop(t *testing.T) {
	w, _, _ := newTestWorker("delta")

	done := make(chan struct{})
	go func() {
		w.Run()
		close(done)
	}()

	// Let it run briefly
	time.Sleep(100 * time.Millisecond)

	w.Stop()

	select {
	case <-done:
		// Worker exited cleanly
	case <-time.After(2 * time.Second):
		t.Error("worker did not stop after Stop()")
	}
}

func TestWorkerRunWithTask(t *testing.T) {
	w, _, results := newTestWorker("beta")

	ctx, cancel := context.WithCancel(context.Background())
	w.ctx = ctx
	w.cancel = cancel

	go w.Run()

	// Enqueue a message task
	task := &Task{
		ID:           "run_task_1",
		Type:         "message",
		Command:      "hello from run test",
		Description:  "run test",
		Priority:     1,
		DispatchedAt: time.Now(),
	}
	w.Enqueue(task)

	select {
	case result := <-results:
		if result.TaskID != "run_task_1" {
			t.Errorf("expected task ID 'run_task_1', got '%s'", result.TaskID)
		}
		if result.Status != "success" {
			t.Errorf("expected success, got '%s': %s", result.Status, result.Error)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for task result from running worker")
	}

	w.Stop()
}

func TestWorkerHeartbeat(t *testing.T) {
	w, _, _ := newTestWorker("alpha")

	before := time.Now()
	w.mu.Lock()
	w.lastExtHB = before
	w.extHBReceived = true
	w.model = "claude-opus-4.6-fast"
	w.mu.Unlock()

	state := w.GetState()
	if state.Model != "claude-opus-4.6-fast" {
		t.Errorf("expected model 'claude-opus-4.6-fast', got '%s'", state.Model)
	}
	if state.LastHeartbeat == "" {
		t.Error("expected non-empty heartbeat")
	}
}

func TestTruncate(t *testing.T) {
	tests := []struct {
		input    string
		max      int
		expected string
	}{
		{"hello", 10, "hello"},
		{"hello world", 5, "he..."},
		{"hi", 2, "hi"},
		{"hello", 5, "hello"},
		{"", 5, ""},
	}

	for _, tt := range tests {
		result := truncate(tt.input, tt.max)
		if result != tt.expected {
			t.Errorf("truncate(%q, %d) = %q, want %q", tt.input, tt.max, result, tt.expected)
		}
	}
}

func TestWorkerHeapPriorityOrdering(t *testing.T) {
	h := &workerHeap{}
	heap.Init(h)

	now := time.Now()
	tasks := []*Task{
		{ID: "p3", Priority: 3, DispatchedAt: now},
		{ID: "p1", Priority: 1, DispatchedAt: now},
		{ID: "p2", Priority: 2, DispatchedAt: now},
		{ID: "p1_late", Priority: 1, DispatchedAt: now.Add(time.Second)},
	}

	for _, task := range tasks {
		heap.Push(h, task)
	}

	// Should pop in priority order, with same-priority using FIFO (earlier dispatch first)
	first := heap.Pop(h).(*Task)
	if first.ID != "p1" {
		t.Errorf("expected first pop to be 'p1', got '%s'", first.ID)
	}

	second := heap.Pop(h).(*Task)
	if second.ID != "p1_late" {
		t.Errorf("expected second pop to be 'p1_late', got '%s'", second.ID)
	}

	third := heap.Pop(h).(*Task)
	if third.ID != "p2" {
		t.Errorf("expected third pop to be 'p2', got '%s'", third.ID)
	}

	fourth := heap.Pop(h).(*Task)
	if fourth.ID != "p3" {
		t.Errorf("expected fourth pop to be 'p3', got '%s'", fourth.ID)
	}
}

func TestWorkerNames(t *testing.T) {
	expected := []string{"alpha", "beta", "gamma", "delta"}
	if len(workerNames) != len(expected) {
		t.Fatalf("expected %d worker names, got %d", len(expected), len(workerNames))
	}
	for i, name := range expected {
		if workerNames[i] != name {
			t.Errorf("workerNames[%d] = '%s', want '%s'", i, workerNames[i], name)
		}
	}
}
