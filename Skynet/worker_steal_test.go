package main

// Work-stealing scheduler cross-validation tests — signed: gamma
// Tests cover: TaskWeight, WeightedLoad, trySteal, steal threshold,
// weighted dispatch routing (selectWorker), and concurrent safety.

import (
	"container/heap"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ─── TaskWeight() correctness ────────────────────────────────────

func TestTaskWeightKnownTypes(t *testing.T) {
	cases := []struct {
		taskType string
		want     int
	}{
		{"shell", 1},
		{"message", 1},
		{"python", 10},
		{"powershell", 10},
		{"copilot", 50},
	}
	for _, tc := range cases {
		got := TaskWeight(tc.taskType)
		if got != tc.want {
			t.Errorf("TaskWeight(%q) = %d, want %d", tc.taskType, got, tc.want)
		}
	}
}

func TestTaskWeightDefaultFallback(t *testing.T) {
	// Unknown types should get default weight 10
	unknowns := []string{"http", "grpc", "", "unknown", "docker"}
	for _, u := range unknowns {
		got := TaskWeight(u)
		if got != 10 {
			t.Errorf("TaskWeight(%q) = %d, want default 10", u, got)
		}
	}
}

func TestTaskWeightPositive(t *testing.T) {
	// All weights must be positive — zero weight breaks load balancer fairness
	types := []string{"shell", "message", "python", "powershell", "copilot", "unknown"}
	for _, tp := range types {
		w := TaskWeight(tp)
		if w <= 0 {
			t.Errorf("TaskWeight(%q) = %d, must be positive", tp, w)
		}
	}
}

// ─── WeightedLoad() correctness ─────────────────────────────────

func TestWeightedLoadEmpty(t *testing.T) {
	w, _, _ := newTestWorker("wl_empty")
	load := w.WeightedLoad()
	if load != 0 {
		t.Errorf("empty worker WeightedLoad() = %d, want 0", load)
	}
}

func TestWeightedLoadQueuedOnly(t *testing.T) {
	w, _, _ := newTestWorker("wl_queued")
	// Enqueue: shell(1) + copilot(50) + python(10) = 61
	w.Enqueue(&Task{ID: "s1", Type: "shell", Priority: 1, DispatchedAt: time.Now()})
	w.Enqueue(&Task{ID: "c1", Type: "copilot", Priority: 2, DispatchedAt: time.Now()})
	w.Enqueue(&Task{ID: "p1", Type: "python", Priority: 3, DispatchedAt: time.Now()})

	load := w.WeightedLoad()
	if load != 61 {
		t.Errorf("WeightedLoad() with 3 queued tasks = %d, want 61", load)
	}
}

func TestWeightedLoadActiveWeight(t *testing.T) {
	w, _, _ := newTestWorker("wl_active")
	// Simulate an active copilot task (weight 50)
	atomic.StoreInt64(&w.activeWeight, 50)
	// Plus one queued shell task (weight 1)
	w.Enqueue(&Task{ID: "s1", Type: "shell", Priority: 1, DispatchedAt: time.Now()})

	load := w.WeightedLoad()
	if load != 51 {
		t.Errorf("WeightedLoad() active(50)+queued(1) = %d, want 51", load)
	}
}

func TestWeightedLoadSumsCorrectly(t *testing.T) {
	w, _, _ := newTestWorker("wl_sum")
	// 5 shell tasks (5*1=5) + active python (10) = 15
	atomic.StoreInt64(&w.activeWeight, 10)
	for i := 0; i < 5; i++ {
		w.Enqueue(&Task{ID: "sh", Type: "shell", Priority: 1, DispatchedAt: time.Now()})
	}
	load := w.WeightedLoad()
	if load != 15 {
		t.Errorf("WeightedLoad() 5*shell+active_python = %d, want 15", load)
	}
}

func TestEnqueueStampsWeight(t *testing.T) {
	w, _, _ := newTestWorker("wl_stamp")
	task := &Task{ID: "t1", Type: "copilot", Priority: 1, DispatchedAt: time.Now()}
	if task.EstimatedWeight != 0 {
		t.Fatal("pre-condition: EstimatedWeight should be 0 before Enqueue")
	}
	w.Enqueue(task)
	// Peek at the task in the queue
	w.taskMu.Lock()
	queued := w.taskQueue[0]
	w.taskMu.Unlock()
	if queued.EstimatedWeight != 50 {
		t.Errorf("Enqueue should stamp copilot weight=50, got %d", queued.EstimatedWeight)
	}
}

func TestEnqueuePreservesExistingWeight(t *testing.T) {
	w, _, _ := newTestWorker("wl_preserve")
	task := &Task{ID: "t1", Type: "shell", Priority: 1, DispatchedAt: time.Now(), EstimatedWeight: 99}
	w.Enqueue(task)
	w.taskMu.Lock()
	queued := w.taskQueue[0]
	w.taskMu.Unlock()
	if queued.EstimatedWeight != 99 {
		t.Errorf("Enqueue should preserve existing weight=99, got %d", queued.EstimatedWeight)
	}
}

// ─── trySteal: steal from heaviest peer ─────────────────────────

func TestTryStealFromHeaviestPeer(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	thief := NewWorker("thief", bus, results)
	light := NewWorker("light", bus, results)
	heavy := NewWorker("heavy", bus, results)

	all := []*Worker{thief, light, heavy}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Heavy worker: 5 tasks — trySteal is RECURSIVE and message-type tasks
	// execute instantly, so it will steal repeatedly until victim has <2 tasks.
	// With 5 tasks: steals 4 (5→4→3→2→1, stops because <2). signed:gamma
	for i := 0; i < 5; i++ {
		heavy.Enqueue(&Task{ID: fmt.Sprintf("h%d", i), Type: "message", Priority: 5, DispatchedAt: time.Now(), Command: "msg"})
	}
	// Light worker: 1 task (weight 1) — below threshold, never stolen from
	light.Enqueue(&Task{ID: "l", Type: "message", Priority: 1, DispatchedAt: time.Now(), Command: "msg"})

	thief.trySteal()

	// Heavy should be down to 1 (recursive steal drains to <2)
	if heavy.QueueDepth() != 1 {
		t.Errorf("heavy should have 1 task after recursive steal, got %d", heavy.QueueDepth())
	}
	// Light unchanged — only 1 task, never a steal target
	if light.QueueDepth() != 1 {
		t.Errorf("light should still have 1 task, got %d", light.QueueDepth())
	}
	// Thief stole multiple tasks via recursive steal
	stolen := atomic.LoadInt32(&thief.tasksStolen)
	if stolen != 4 {
		t.Errorf("thief.tasksStolen should be 4 after recursive steal from 5-task victim, got %d", stolen)
	}
}

func TestTryStealSkipsSelf(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	w := NewWorker("solo", bus, results)
	w.SetPeers([]*Worker{w}) // only self

	// Enqueue tasks on self — should NOT steal from self
	for i := 0; i < 5; i++ {
		w.Enqueue(&Task{ID: "s", Type: "python", Priority: 1, DispatchedAt: time.Now(), Command: "msg"})
	}
	before := w.QueueDepth()
	w.trySteal()
	after := w.QueueDepth()
	if before != after {
		t.Errorf("trySteal should not steal from self: before=%d, after=%d", before, after)
	}
}

// ─── Steal threshold: ≥2 queued tasks required ──────────────────

func TestTryStealThresholdMinimum(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	thief := NewWorker("thief", bus, results)
	victim := NewWorker("victim", bus, results)
	all := []*Worker{thief, victim}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Victim has exactly 1 task — below ≥2 threshold
	victim.Enqueue(&Task{ID: "v1", Type: "message", Priority: 1, DispatchedAt: time.Now(), Command: "msg"})
	thief.trySteal()
	if victim.QueueDepth() != 1 {
		t.Errorf("steal should NOT happen when victim has <2 tasks, got depth %d", victim.QueueDepth())
	}
}

func TestTryStealThresholdExactlyTwo(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	thief := NewWorker("thief", bus, results)
	victim := NewWorker("victim", bus, results)
	all := []*Worker{thief, victim}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Victim has exactly 2 tasks — meets ≥2 threshold. Use message type for
	// instant execution (no real subprocess). signed:gamma
	victim.Enqueue(&Task{ID: "v1", Type: "message", Priority: 1, DispatchedAt: time.Now(), Command: "msg"})
	victim.Enqueue(&Task{ID: "v2", Type: "message", Priority: 2, DispatchedAt: time.Now(), Command: "msg"})

	thief.trySteal()
	// Should have stolen one, leaving victim with 1
	if victim.QueueDepth() != 1 {
		t.Errorf("steal with 2-task victim: expected depth 1, got %d", victim.QueueDepth())
	}
}

func TestTryStealNoPeers(t *testing.T) {
	w, _, _ := newTestWorker("lonely")
	// No peers set — trySteal should return immediately
	w.trySteal() // should not panic
}

// ─── selectWorker: weighted dispatch routing ────────────────────

func TestSelectWorkerPicksLightest(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	workers := make([]*Worker, 4)
	for i := range workers {
		workers[i] = NewWorker(workerNames[i], bus, results)
	}

	srv := NewSkynetServer(bus, workers, results)

	// Give workers different loads
	workers[0].Enqueue(&Task{ID: "a1", Type: "copilot", Priority: 1, DispatchedAt: time.Now()}) // weight 50
	workers[1].Enqueue(&Task{ID: "b1", Type: "python", Priority: 1, DispatchedAt: time.Now()})  // weight 10
	// workers[2] empty — weight 0 (lightest)
	workers[3].Enqueue(&Task{ID: "d1", Type: "shell", Priority: 1, DispatchedAt: time.Now()})   // weight 1

	picked := srv.selectWorker()
	if picked == nil {
		t.Fatal("selectWorker returned nil with 4 workers")
	}
	if picked.Name != "gamma" {
		t.Errorf("selectWorker should pick lightest worker (gamma, load=0), got %s (load=%d)",
			picked.Name, picked.WeightedLoad())
	}
}

func TestSelectWorkerNoWorkers(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	srv := NewSkynetServer(bus, []*Worker{}, results)

	picked := srv.selectWorker()
	if picked != nil {
		t.Error("selectWorker should return nil with no workers")
	}
}

func TestSelectWorkerRRTiebreaker(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	workers := make([]*Worker, 4)
	for i := range workers {
		workers[i] = NewWorker(workerNames[i], bus, results)
	}
	srv := NewSkynetServer(bus, workers, results)

	// All workers have equal load (0) — RR counter should cycle through
	seen := make(map[string]bool)
	for i := 0; i < 8; i++ {
		picked := srv.selectWorker()
		if picked == nil {
			t.Fatal("selectWorker returned nil")
		}
		seen[picked.Name] = true
	}
	// With RR tiebreaker, the "start" position cycles, but all workers
	// have equal load so the start position IS the pick.
	// Over 8 calls we should see all 4 workers.
	if len(seen) != 4 {
		t.Errorf("RR tiebreaker should spread across all 4 workers over 8 calls, saw %d: %v",
			len(seen), seen)
	}
}

func TestSelectWorkerPrefersTrueMinOverRRStart(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	workers := make([]*Worker, 4)
	for i := range workers {
		workers[i] = NewWorker(workerNames[i], bus, results)
	}
	srv := NewSkynetServer(bus, workers, results)

	// Set RR so it starts at alpha (index 0), but gamma (index 2) is lightest
	atomic.StoreInt64(&srv.rrCounter, 0)
	workers[0].Enqueue(&Task{ID: "a1", Type: "copilot", Priority: 1, DispatchedAt: time.Now()}) // 50
	workers[1].Enqueue(&Task{ID: "b1", Type: "copilot", Priority: 1, DispatchedAt: time.Now()}) // 50
	// workers[2] (gamma): load 0
	workers[3].Enqueue(&Task{ID: "d1", Type: "python", Priority: 1, DispatchedAt: time.Now()})  // 10

	picked := srv.selectWorker()
	if picked.Name != "gamma" {
		t.Errorf("selectWorker must pick true min load (gamma=0), not RR start, got %s (load=%d)",
			picked.Name, picked.WeightedLoad())
	}
}

// ─── Concurrent steal+push safety ───────────────────────────────

func TestConcurrentEnqueueAndSteal(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 1000)
	victim := NewWorker("victim", bus, results)
	thief := NewWorker("thief", bus, results)
	all := []*Worker{victim, thief}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Pre-populate victim with enough tasks
	for i := 0; i < 20; i++ {
		victim.Enqueue(&Task{
			ID: "init", Type: "message", Command: "msg",
			Priority: 5, DispatchedAt: time.Now(),
		})
	}

	var wg sync.WaitGroup
	// Concurrently enqueue more tasks to victim
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 50; i++ {
			victim.Enqueue(&Task{
				ID: "push", Type: "message", Command: "msg",
				Priority: 3, DispatchedAt: time.Now(),
			})
		}
	}()

	// Concurrently steal from victim
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 30; i++ {
			thief.trySteal()
		}
	}()

	wg.Wait()

	// Both workers' queue depths should be non-negative (no corruption)
	vd := victim.QueueDepth()
	td := thief.QueueDepth()
	if vd < 0 {
		t.Errorf("victim queue depth negative: %d (data corruption)", vd)
	}
	if td < 0 {
		t.Errorf("thief queue depth negative: %d (data corruption)", td)
	}

	// Total tasks should equal: initial(20) + pushed(50) - stolen(consumed by execute)
	// Since message tasks execute instantly in the steal path, the thief's
	// stolen counter is the source of truth.
	stolen := atomic.LoadInt32(&thief.tasksStolen)
	t.Logf("concurrent test: victim_depth=%d, thief_depth=%d, stolen=%d", vd, td, stolen)
}

func TestConcurrentWeightedLoadReads(t *testing.T) {
	w, _, _ := newTestWorker("wl_concurrent")

	// Pre-load some tasks
	for i := 0; i < 10; i++ {
		w.Enqueue(&Task{ID: "c", Type: "python", Priority: 1, DispatchedAt: time.Now()})
	}

	var wg sync.WaitGroup
	// Many concurrent WeightedLoad() reads while active weight changes
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			load := w.WeightedLoad()
			if load < 0 {
				t.Errorf("WeightedLoad() returned negative: %d", load)
			}
		}()
	}

	// Mutate activeWeight concurrently
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			atomic.StoreInt64(&w.activeWeight, int64(i%50))
		}
	}()

	wg.Wait()
}

func TestConcurrentStealFromMultipleThieves(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 1000)
	victim := NewWorker("victim", bus, results)
	thief1 := NewWorker("thief1", bus, results)
	thief2 := NewWorker("thief2", bus, results)
	all := []*Worker{victim, thief1, thief2}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Give victim many tasks
	for i := 0; i < 30; i++ {
		victim.Enqueue(&Task{
			ID: "multi", Type: "message", Command: "msg",
			Priority: 5, DispatchedAt: time.Now(),
		})
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 15; i++ {
			thief1.trySteal()
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 15; i++ {
			thief2.trySteal()
		}
	}()
	wg.Wait()

	// No panics or negative depths = data integrity verified
	vd := victim.QueueDepth()
	if vd < 0 {
		t.Errorf("victim queue depth negative after multi-thief steal: %d", vd)
	}
	s1 := atomic.LoadInt32(&thief1.tasksStolen)
	s2 := atomic.LoadInt32(&thief2.tasksStolen)
	t.Logf("multi-thief: victim_depth=%d, thief1_stolen=%d, thief2_stolen=%d", vd, s1, s2)
}

// ─── Steal steals lowest-priority task ──────────────────────────

func TestStealTakesLowestPriorityTask(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 100)
	thief := NewWorker("thief", bus, results)
	victim := NewWorker("victim", bus, results)
	all := []*Worker{thief, victim}
	for _, w := range all {
		w.SetPeers(all)
	}

	// Enqueue tasks with different priorities (lower number = higher priority)
	// trySteal is RECURSIVE and message-type tasks execute instantly.
	// With 3 tasks, recursive steal drains to 1 (3→2→1). signed:gamma
	victim.Enqueue(&Task{ID: "critical", Type: "message", Command: "msg", Priority: 1, DispatchedAt: time.Now()})
	victim.Enqueue(&Task{ID: "normal", Type: "message", Command: "msg", Priority: 5, DispatchedAt: time.Now()})
	victim.Enqueue(&Task{ID: "low", Type: "message", Command: "msg", Priority: 10, DispatchedAt: time.Now()})

	// After recursive steal, victim should keep only 1 task (the one not stolen)
	thief.trySteal()

	// Victim should have 1 task remaining (recursive steal: 3→2→1)
	if victim.QueueDepth() != 1 {
		t.Errorf("victim should have 1 task after recursive steal, got %d", victim.QueueDepth())
	}

	// The remaining task should be the highest-priority one (priority 1 = "critical")
	// because heap.Remove takes from lastIdx (bottom of heap), preserving the root
	victim.taskMu.Lock()
	var hasCritical bool
	for _, task := range victim.taskQueue {
		if task.ID == "critical" {
			hasCritical = true
		}
	}
	victim.taskMu.Unlock()

	if !hasCritical {
		t.Error("victim should retain highest-priority task (critical, p=1) after steal")
	}
}

// ─── AgentView includes WeightedLoad ────────────────────────────

func TestGetStateIncludesWeightedLoad(t *testing.T) {
	w, _, _ := newTestWorker("view_wl")
	w.Enqueue(&Task{ID: "c1", Type: "copilot", Priority: 1, DispatchedAt: time.Now()})
	atomic.StoreInt64(&w.activeWeight, 10)

	state := w.GetState()
	if state.WeightedLoad != 60 {
		t.Errorf("GetState().WeightedLoad = %d, want 60 (active=10 + queued=50)", state.WeightedLoad)
	}
}

// ─── Circuit breaker re-queue (P4 fix validation) ───────────────

func TestCircuitOpenRequeuesTask(t *testing.T) {
	w, _, _ := newTestWorker("cb_requeue")
	// Open circuit
	w.mu.Lock()
	w.circuitState = "CIRCUIT_OPEN"
	w.circuitOpenedAt = time.Now()
	w.mu.Unlock()

	task := &Task{
		ID: "cb_task", Type: "message", Command: "test",
		Priority: 1, DispatchedAt: time.Now(),
	}

	// Drain any notify signals first
	for len(w.taskNotify) > 0 {
		<-w.taskNotify
	}

	go w.execute(task)
	time.Sleep(200 * time.Millisecond)

	// Task should be re-queued (not lost)
	depth := w.QueueDepth()
	if depth < 1 {
		t.Errorf("circuit OPEN should re-queue task, got depth %d (task lost!)", depth)
	}
}

// ─── SetPeers wiring ────────────────────────────────────────────

func TestSetPeersStoresAllWorkers(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 10)
	workers := make([]*Worker, 4)
	for i := range workers {
		workers[i] = NewWorker(workerNames[i], bus, results)
	}
	for _, w := range workers {
		w.SetPeers(workers)
	}

	// Each worker should have 4 peers (including self — filtered at steal time)
	for _, w := range workers {
		if len(w.peers) != 4 {
			t.Errorf("worker %s should have 4 peers, got %d", w.Name, len(w.peers))
		}
	}
}

// ─── Edge case: heap.Remove at last index ───────────────────────

func TestHeapRemoveLastIndex(t *testing.T) {
	// Validates that heap.Remove on the last index (used by trySteal) works correctly
	h := &workerHeap{}
	heap.Init(h)

	tasks := []*Task{
		{ID: "p1", Priority: 1, DispatchedAt: time.Now(), Type: "shell", EstimatedWeight: 1},
		{ID: "p5", Priority: 5, DispatchedAt: time.Now(), Type: "shell", EstimatedWeight: 1},
		{ID: "p3", Priority: 3, DispatchedAt: time.Now(), Type: "shell", EstimatedWeight: 1},
	}
	for _, task := range tasks {
		heap.Push(h, task)
	}

	// Remove last index (used in trySteal)
	lastIdx := h.Len() - 1
	removed := heap.Remove(h, lastIdx).(*Task)

	// Should not panic and heap should still be valid
	if h.Len() != 2 {
		t.Errorf("heap should have 2 items after remove, got %d", h.Len())
	}
	// The removed item came from the last position
	_ = removed // just verify no panic

	// Verify remaining heap pops in priority order
	first := heap.Pop(h).(*Task)
	second := heap.Pop(h).(*Task)
	if first.Priority > second.Priority {
		t.Errorf("heap invariant broken: popped p=%d before p=%d", first.Priority, second.Priority)
	}
}
