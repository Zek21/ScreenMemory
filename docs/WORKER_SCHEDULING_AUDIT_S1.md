# Worker Scheduling Audit — Sprint 1

<!-- signed: beta -->

**Auditor:** Beta (daemon robustness & infrastructure specialist)
**Date:** 2026-03-17
**Target:** `Skynet/worker.go` (414 lines) + scheduling paths in `Skynet/server.go`
**Scope:** Convoy effect, work-stealing feasibility, circuit breaker rigidity, task type asymmetry, idle detection

---

## Current Scheduling Flow

```
                              ┌──────────────────────────────┐
                              │     HTTP Request Arrives      │
                              │  /dispatch  /orchestrate      │
                              │  /pipeline                    │
                              └──────────────┬───────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │ Worker specified │
                                    │   in request?    │
                                    └───┬──────────┬───┘
                                   YES  │          │  NO
                                        │          │
                            ┌───────────▼┐    ┌───▼──────────────────┐
                            │ Use named  │    │ Round-Robin Select   │
                            │ worker     │    │ rrCounter++ % len(w) │
                            └───────────┬┘    └───────┬──────────────┘
                                        │             │
                                        │    ┌────────▼──────────────┐
                                        │    │ Queue-Depth Tiebreak  │
                                        │    │ Scan ALL workers,     │
                                        │    │ pick lowest QueueDepth│
                                        │    └────────┬──────────────┘
                                        │             │
                              ┌─────────▼─────────────▼──┐
                              │     worker.Enqueue(task)  │
                              │  heap.Push(&taskQueue, t) │
                              │  taskNotify <- struct{}{} │
                              └──────────────┬───────────┘
                                             │
                              ┌──────────────▼───────────┐
                              │     Worker.Run() loop    │
                              │  select { <-taskNotify } │
                              │          │               │
                              │  ┌───────▼────────┐      │
                              │  │  drainQueue()  │      │
                              │  │  heap.Pop()    │      │
                              │  │  execute(task) │      │
                              │  └───────┬────────┘      │
                              │          │               │
                              │  ┌───────▼────────┐      │
                              │  │ Circuit Check  │      │
                              │  │ OPEN? skip     │      │
                              │  │ HALF_OPEN? try │      │
                              │  └───────┬────────┘      │
                              │          │               │
                              │  ┌───────▼────────┐      │
                              │  │ runCommand()   │      │
                              │  │ + retry (3x)   │      │
                              │  │ + exp backoff  │      │
                              │  └───────┬────────┘      │
                              │          │               │
                              │  ┌───────▼────────┐      │
                              │  │ Report result  │      │
                              │  │ Set IDLE       │      │
                              │  └────────────────┘      │
                              └──────────────────────────┘
```

---

## 1. CONVOY EFFECT ANALYSIS

**Severity: CRITICAL**

### The Problem

The load balancer at `server.go:811-825` uses a two-phase selection:

```go
// Phase 1: Round-robin starting point
best := s.workers[int(atomic.AddInt64(&s.rrCounter, 1)-1)%len(s.workers)]
// Phase 2: Override if any worker has lower queue depth
bestDepth := best.QueueDepth()
for _, wk := range s.workers {
    if d := wk.QueueDepth(); d < bestDepth {
        bestDepth = d
        best = wk
    }
}
```

This algorithm is **weight-blind**. It treats every task as equal cost. QueueDepth counts *items*, not *expected duration*.

### Worst-Case Scenario: 3 Heavy Tasks

Consider 4 workers (alpha, beta, gamma, delta), all at QueueDepth=0. Three `copilot` tasks (120s timeout each) arrive in rapid succession:

```
T=0ms:   copilot_1 arrives → RR selects alpha (counter=1)
         All depths=0, alpha wins. alpha.QueueDepth=0 (dequeued instantly)
         alpha starts WORKING on copilot_1

T=1ms:   copilot_2 arrives → RR selects beta (counter=2)
         All depths=0, beta wins. beta starts WORKING.

T=2ms:   copilot_3 arrives → RR selects gamma (counter=3)
         gamma starts WORKING.

T=3ms:   shell_1 (50ms) arrives → RR selects delta (counter=4)
         delta finishes at T=53ms.

T=53ms:  delta is IDLE. No new tasks. delta idles.

T=120s:  alpha/beta/gamma finish copilot tasks.
```

**Result:** delta idles for ~119.95s while alpha/beta/gamma each run a 120s task. This is acceptable in this particular case because the scheduler happened to spread tasks.

**BUT** consider the pathological case — the queue-depth tiebreaker actually makes things WORSE:

```
T=0ms:   copilot_1 → RR selects alpha. alpha.QueueDepth=0, no override.
         alpha starts WORKING (QueueDepth still 0 — task was popped).

T=1ms:   copilot_2 → RR selects beta. beta starts WORKING.

T=2ms:   copilot_3 → RR selects gamma. gamma starts WORKING.

T=3ms:   shell_1 → RR selects delta. delta starts WORKING.

T=53ms:  delta finishes shell_1. delta is IDLE.

T=54ms:  copilot_4 → RR would select alpha (counter=5%4=0),
         BUT alpha.QueueDepth=0 because its copilot_1 is actively
         executing (not in queue). All workers show depth=0.
         Alpha wins RR. copilot_4 queues behind copilot_1 on alpha.

T=55ms:  copilot_5 → Same situation. RR selects beta.
         beta.QueueDepth=0 (executing, not queued). Goes to beta.

T=56ms:  copilot_6 → RR selects gamma.

T=57ms:  shell_2 → RR selects delta. Delta finishes at T=107ms.

T=120s:  alpha finishes copilot_1, starts copilot_4 (runs until T=240s).
         beta finishes copilot_2, starts copilot_5 (runs until T=240s).
         gamma finishes copilot_3, starts copilot_6 (runs until T=240s).

T=107ms: delta finishes shell_2. Idles for 119.893s.
```

**Worst-case idle time:** **~119.9 seconds** for delta, while 3 workers are each back-to-back on 120s tasks.

### Root Cause

The QueueDepth tiebreaker only counts **queued** tasks, not the **executing** task. A worker WORKING on a 120s copilot task shows QueueDepth=0 because the task was `heap.Pop()`'d at `worker.go:184`. The scheduler cannot distinguish "idle with empty queue" from "busy with empty queue."

### Key Metrics

| Metric | Current | With Weight-Aware | Improvement |
|--------|---------|-------------------|-------------|
| Worst-case idle time (4 workers, 3 heavy + 1 light) | ~119.9s | ~0s (steal from heavy) | **∞** |
| Max queue imbalance (copilot vs shell mix) | Unbounded | Bounded by weight | **O(n) → O(1)** |
| Scheduling decision cost | O(n) scan | O(n) scan + weight | ~Same |

### Proposed Fix: Weighted Queue Depth

```go
// Add to Worker struct:
type Worker struct {
    // ... existing fields ...
    currentTaskWeight int64 // estimated ms remaining
}

// Weight estimates by task type
var taskWeights = map[string]int64{
    "shell":      50,      // 50ms
    "python":     5000,    // 5s average
    "copilot":    120000,  // 120s timeout
    "powershell": 10000,   // 10s average
    "message":    1,       // instant
}

// WeightedLoad returns estimated total ms of pending + active work
func (w *Worker) WeightedLoad() int64 {
    w.taskMu.Lock()
    defer w.taskMu.Unlock()

    load := atomic.LoadInt64(&w.currentTaskWeight)
    for _, t := range w.taskQueue {
        if weight, ok := taskWeights[t.Type]; ok {
            load += weight
        } else {
            load += 10000 // default 10s
        }
    }
    return load
}
```

Replace the tiebreaker in `server.go:817-824`:

```go
best := s.workers[int(atomic.AddInt64(&s.rrCounter, 1)-1)%len(s.workers)]
bestLoad := best.WeightedLoad()
for _, wk := range s.workers {
    if load := wk.WeightedLoad(); load < bestLoad {
        bestLoad = load
        best = wk
    }
}
```

---

## 2. WORK-STEALING FEASIBILITY (Chase-Lev Deque)

**Severity: HIGH**

### Current Architecture vs. Chase-Lev

| Aspect | Current (Priority Heap) | Chase-Lev Deque |
|--------|------------------------|-----------------|
| Data structure | `container/heap` min-heap | Lock-free circular buffer |
| Local access | O(log n) push/pop | O(1) push/pop (LIFO) |
| Remote steal | Not supported | O(1) steal (FIFO, CAS) |
| Priority support | Native (heap property) | None (FIFO/LIFO only) |
| Contention | Mutex (`taskMu`) per op | Lock-free (atomic CAS) |
| Cache locality | Poor (heap rebalance) | Excellent (LIFO = hot cache) |

### Structural Analysis

The current `workerHeap` (`worker.go:373-391`) is a standard min-heap ordered by `(Priority, DispatchedAt)`. It provides correct priority ordering but has three weaknesses for work-stealing:

1. **No steal interface**: Other workers cannot pull tasks from a neighbor's heap without taking `taskMu`, creating contention.
2. **Heap operations are O(log n)**: Every push/pop rebalances.
3. **No LIFO path**: The heap always returns the globally minimum priority, not the most recently added task (which would be in L1/L2 cache).

### Can the Heap Be Replaced?

**Not directly.** Chase-Lev deques are FIFO/LIFO only — they have no concept of priority ordering. A pure replacement would lose priority semantics.

### Proposed Hybrid: Priority Tiers + Chase-Lev Deques

```
┌─────────────────────────────────────────────┐
│              Worker Task System              │
│                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐     │
│  │ Tier 0  │  │ Tier 1  │  │ Tier 2  │     │
│  │ P=0     │  │ P=1     │  │ P=2+    │     │
│  │ (urgent)│  │ (normal)│  │ (low)   │     │
│  │         │  │         │  │         │     │
│  │ [deque] │  │ [deque] │  │ [deque] │     │
│  └────┬────┘  └────┬────┘  └────┬────┘     │
│       │            │            │            │
│  Local: LIFO  Local: LIFO  Local: LIFO      │
│  Steal: FIFO  Steal: FIFO  Steal: FIFO      │
│                                              │
│  Drain order: Tier 0 → Tier 1 → Tier 2      │
└─────────────────────────────────────────────┘
```

### Go Implementation: Chase-Lev Deque

```go
package main

import (
    "sync/atomic"
    "unsafe"
)

// ChaseLevDeque is a lock-free work-stealing deque.
// Owner pushes/pops from bottom (LIFO). Thieves steal from top (FIFO).
type ChaseLevDeque struct {
    top    int64          // stolen from here (atomic)
    bottom int64          // owner pushes/pops here (atomic)
    array  unsafe.Pointer // *dequeArray (atomic, resizable)
}

type dequeArray struct {
    size int64
    buf  []unsafe.Pointer // stores *Task
}

func newDequeArray(size int64) *dequeArray {
    return &dequeArray{
        size: size,
        buf:  make([]unsafe.Pointer, size),
    }
}

func (a *dequeArray) get(i int64) *Task {
    ptr := atomic.LoadPointer(&a.buf[i%a.size])
    if ptr == nil {
        return nil
    }
    return (*Task)(ptr)
}

func (a *dequeArray) set(i int64, task *Task) {
    atomic.StorePointer(&a.buf[i%a.size], unsafe.Pointer(task))
}

func (a *dequeArray) grow(top, bottom int64) *dequeArray {
    newArr := newDequeArray(a.size * 2)
    for i := top; i < bottom; i++ {
        newArr.set(i, a.get(i))
    }
    return newArr
}

func NewChaseLevDeque() *ChaseLevDeque {
    return &ChaseLevDeque{
        array: unsafe.Pointer(newDequeArray(64)),
    }
}

// Push adds a task to the bottom (owner only, not thread-safe for multiple pushers).
func (d *ChaseLevDeque) Push(task *Task) {
    b := atomic.LoadInt64(&d.bottom)
    t := atomic.LoadInt64(&d.top)
    arr := (*dequeArray)(atomic.LoadPointer(&d.array))

    if b-t >= arr.size-1 {
        arr = arr.grow(t, b)
        atomic.StorePointer(&d.array, unsafe.Pointer(arr))
    }

    arr.set(b, task)
    // StoreStore barrier: task must be visible before bottom advances
    atomic.StoreInt64(&d.bottom, b+1)
}

// Pop removes a task from the bottom (owner only). Returns nil if empty.
func (d *ChaseLevDeque) Pop() *Task {
    b := atomic.LoadInt64(&d.bottom) - 1
    atomic.StoreInt64(&d.bottom, b)
    arr := (*dequeArray)(atomic.LoadPointer(&d.array))
    t := atomic.LoadInt64(&d.top)

    if t <= b {
        // Non-empty: at least one more besides what we're taking
        task := arr.get(b)
        return task
    }

    if t == b {
        // Last element — race with stealers
        task := arr.get(b)
        if !atomic.CompareAndSwapInt64(&d.top, t, t+1) {
            // Stealer won
            task = nil
        }
        atomic.StoreInt64(&d.bottom, t+1)
        return task
    }

    // Empty
    atomic.StoreInt64(&d.bottom, t)
    return nil
}

// Steal takes a task from the top (any thread, lock-free via CAS).
func (d *ChaseLevDeque) Steal() *Task {
    t := atomic.LoadInt64(&d.top)
    arr := (*dequeArray)(atomic.LoadPointer(&d.array))
    b := atomic.LoadInt64(&d.bottom)

    if t >= b {
        return nil // empty
    }

    task := arr.get(t)
    if !atomic.CompareAndSwapInt64(&d.top, t, t+1) {
        return nil // contention, retry externally
    }
    return task
}

// Len returns approximate size (not linearizable).
func (d *ChaseLevDeque) Len() int {
    b := atomic.LoadInt64(&d.bottom)
    t := atomic.LoadInt64(&d.top)
    size := b - t
    if size < 0 {
        return 0
    }
    return int(size)
}
```

### Integration with Worker

```go
type Worker struct {
    // Replace:
    //   taskQueue workerHeap
    //   taskMu    sync.Mutex
    // With:
    deques     [3]*ChaseLevDeque // [urgent, normal, low]
    allWorkers []*Worker         // reference to siblings for stealing
}

func (w *Worker) Enqueue(task *Task) {
    tier := 1 // default normal
    if task.Priority == 0 {
        tier = 0
    } else if task.Priority >= 2 {
        tier = 2
    }
    w.deques[tier].Push(task)

    select {
    case w.taskNotify <- struct{}{}:
    default:
    }
}

func (w *Worker) drainQueue() {
    for {
        task := w.popLocal()
        if task == nil {
            // Local queue empty — try stealing
            task = w.stealFromPeer()
        }
        if task == nil {
            return // truly nothing to do
        }
        w.execute(task)
    }
}

func (w *Worker) popLocal() *Task {
    // Drain highest priority tier first
    for tier := 0; tier < 3; tier++ {
        if task := w.deques[tier].Pop(); task != nil {
            return task
        }
    }
    return nil
}

func (w *Worker) stealFromPeer() *Task {
    // Try stealing from the busiest peer (highest total deque length)
    var bestPeer *Worker
    bestLen := 0
    for _, peer := range w.allWorkers {
        if peer == w {
            continue
        }
        totalLen := 0
        for tier := 0; tier < 3; tier++ {
            totalLen += peer.deques[tier].Len()
        }
        if totalLen > bestLen {
            bestLen = totalLen
            bestPeer = peer
        }
    }
    if bestPeer == nil {
        return nil
    }
    // Steal from lowest priority tier first (FIFO = oldest task)
    for tier := 2; tier >= 0; tier-- {
        if task := bestPeer.deques[tier].Steal(); task != nil {
            return task
        }
    }
    return nil
}
```

### Migration Effort Estimate

| Component | Changes | Risk |
|-----------|---------|------|
| `Worker` struct fields | Replace heap with 3 deques + peer refs | LOW |
| `Worker.Enqueue()` | Priority → tier mapping | LOW |
| `Worker.drainQueue()` | Add steal fallback | MEDIUM |
| `Worker.QueueDepth()` | Sum of 3 deques | LOW |
| `Worker.RemoveTask()` | Deque doesn't support O(1) removal by ID | HIGH — needs separate cancel map |
| `server.go` worker init | Pass `allWorkers` slice after creation | LOW |
| Testing | Lock-free correctness under contention | HIGH |

**Total estimated structural changes:** ~150 lines modified, ~200 lines new (deque implementation).

---

## 3. CIRCUIT BREAKER RIGIDITY

**Severity: HIGH**

### Current Implementation (worker.go:192-269)

```
State Machine:
                 3 consecutive
    CLOSED ──────failures──────▶ CIRCUIT_OPEN
       ▲                              │
       │                              │ 30s cooldown
       │ 1 success                    │
       │ (HALF_OPEN)                  ▼
       └──────────────────────── HALF_OPEN
                                (next task = probe)
```

**Code path analysis:**

1. **Failure tracking** (`worker.go:249-255`): Increments `consecutiveFails` on every error. At `>=3`, opens circuit. **Problem:** Only consecutive failures trigger — a pattern of `fail, success, fail, success, fail` never opens the circuit despite 60% failure rate.

2. **Open state** (`worker.go:198-209`): Tasks are SILENTLY DROPPED with only a log entry. No re-queueing, no bus notification, no retry to another worker. **The task is lost.**

3. **Recovery** (`worker.go:203-208`): After 30s, transitions to `HALF_OPEN`. The next task is a probe — if it succeeds, circuit closes. If it fails, circuit re-opens. **Problem:** The 30s is fixed regardless of failure severity.

4. **Reset** (`worker.go:264-269`): A single success in `HALF_OPEN` closes the circuit. **Problem:** One lucky success after systemic failure could be a transient fluke.

### Failure Scenarios

**Scenario A: Transient Network Partition (3s)**
```
T=0s:    task_1 fails  (consecutiveFails=1)
T=1s:    task_2 fails  (consecutiveFails=2) — retries burn 1+2+4=7s
T=8s:    task_3 fails  (consecutiveFails=3) — CIRCUIT_OPEN
T=8s:    Network recovers. Worker is paralyzed.
T=38s:   30s cooldown expires. HALF_OPEN.
T=38s:   task_4 probe succeeds. Circuit closes.
```
**Impact:** 30 seconds of unnecessary downtime from a 3-second partition. With retry backoff built into each task (1s+2s+4s = 7s per task × 3 tasks), the actual partition could have recovered during the retries.

**Scenario B: Persistent Failure (disk full)**
```
T=0s:    Circuit opens after 3 failures.
T=30s:   HALF_OPEN probe fails (disk still full). Re-opens.
T=60s:   HALF_OPEN probe fails. Re-opens.
...repeats forever...
```
**Problem:** The circuit re-opens after every probe failure, but keeps trying at fixed 30s intervals indefinitely. No escalation, no alert to orchestrator, no exponential backoff on the open duration.

### Proposed Improvement: Sliding Window Error Budget

```go
type SlidingWindowCircuitBreaker struct {
    windowSize    int           // number of requests in window
    errorBudget   float64       // max error rate (e.g., 0.10 = 10%)
    outcomes      []bool        // circular buffer: true=success, false=fail
    head          int           // next write position
    count         int           // total entries in buffer
    state         string        // "CLOSED", "OPEN", "HALF_OPEN"
    openedAt      time.Time
    cooldown      time.Duration // starts at 5s, doubles on each re-open
    baseCooldown  time.Duration
    maxCooldown   time.Duration
    probesNeeded  int           // require N successes in HALF_OPEN
    probeSuccesses int
    mu            sync.Mutex
}

func NewSlidingWindowCB() *SlidingWindowCircuitBreaker {
    return &SlidingWindowCircuitBreaker{
        windowSize:   100,
        errorBudget:  0.10,         // 10% error rate threshold
        outcomes:     make([]bool, 100),
        state:        "CLOSED",
        baseCooldown: 5 * time.Second,
        cooldown:     5 * time.Second,
        maxCooldown:  120 * time.Second,
        probesNeeded: 3,            // require 3 consecutive successes
    }
}

func (cb *SlidingWindowCircuitBreaker) RecordOutcome(success bool) {
    cb.mu.Lock()
    defer cb.mu.Unlock()

    cb.outcomes[cb.head] = success
    cb.head = (cb.head + 1) % cb.windowSize
    if cb.count < cb.windowSize {
        cb.count++
    }

    if cb.state == "HALF_OPEN" {
        if success {
            cb.probeSuccesses++
            if cb.probeSuccesses >= cb.probesNeeded {
                cb.state = "CLOSED"
                cb.cooldown = cb.baseCooldown // reset cooldown
                cb.probeSuccesses = 0
            }
        } else {
            // Re-open with doubled cooldown
            cb.state = "OPEN"
            cb.openedAt = time.Now()
            cb.cooldown = min(cb.cooldown*2, cb.maxCooldown)
            cb.probeSuccesses = 0
        }
        return
    }

    // CLOSED state: check error rate
    if cb.count >= 10 { // minimum sample size
        failures := 0
        for i := 0; i < cb.count; i++ {
            if !cb.outcomes[i] {
                failures++
            }
        }
        errorRate := float64(failures) / float64(cb.count)
        if errorRate > cb.errorBudget {
            cb.state = "OPEN"
            cb.openedAt = time.Now()
        }
    }
}

func (cb *SlidingWindowCircuitBreaker) AllowRequest() bool {
    cb.mu.Lock()
    defer cb.mu.Unlock()

    switch cb.state {
    case "CLOSED":
        return true
    case "OPEN":
        if time.Since(cb.openedAt) >= cb.cooldown {
            cb.state = "HALF_OPEN"
            cb.probeSuccesses = 0
            return true
        }
        return false
    case "HALF_OPEN":
        return true // allow probe requests
    }
    return true
}
```

### Comparison

| Aspect | Current (Fixed 3-fail) | Proposed (Sliding Window) |
|--------|----------------------|---------------------------|
| Trigger | 3 consecutive failures | 10% error rate over 100 requests |
| Transient fault tolerance | Poor (3 rapid fails = 30s lockout) | Good (3/100 = 3%, under threshold) |
| Cooldown | Fixed 30s | Exponential: 5s → 10s → 20s → ... → 120s max |
| Recovery confidence | 1 success = closed | 3 consecutive successes required |
| Dropped task handling | Silent drop, task lost | Should re-queue to another worker |
| Persistent failure escalation | None | Exponential cooldown + bus alert |

### Critical Bug: Silent Task Dropping

At `worker.go:200-201`, when the circuit is open:

```go
if time.Since(cOpened) < 30*time.Second {
    w.log(fmt.Sprintf("⚡ Circuit OPEN — skipping task [%s]", task.ID))
    return // TASK IS SILENTLY LOST
}
```

**This task is never re-queued, never sent to another worker, and never reported as failed.** The caller believes it was dispatched. The result channel never receives anything for this task. This is a **data loss bug**.

**Fix:** Circuit-open tasks must be either:
1. Re-queued to the back of the worker's own queue (for retry after circuit closes), OR
2. Published to the bus with `type=circuit_rejected` so the server can re-route to another worker.

---

## 4. TASK TYPE ASYMMETRY

**Severity: HIGH**

### Execution Profile Analysis

The `runCommand()` function (`worker.go:303-348`) handles 5 task types with vastly different profiles:

| Task Type | Timeout | Typical Duration | Variance | CPU Profile | I/O Profile |
|-----------|---------|-----------------|----------|-------------|-------------|
| `shell` | 30s | 50ms | Low | Minimal | Subprocess |
| `python` | 30s | 100ms–30s | **Very High** | Variable | Subprocess + disk |
| `copilot` | 120s | 5s–120s | **Extreme** | Network-bound | HTTP + LLM inference |
| `powershell` | 30s (default) | 100ms–30s | High | Variable | Subprocess |
| `message` | N/A | 0ms | None | None | None |

### What the Scheduler Doesn't Account For

1. **No task weight in routing:** The round-robin + queue-depth algorithm at `server.go:817` counts tasks, not estimated cost. A worker with 1 copilot task (120s) appears lighter than a worker with 2 shell tasks (100ms total).

2. **No type-aware timeout:** Only `copilot` gets a special timeout (120s). All other types share 30s. A complex Python ML script that legitimately needs 60s will be killed.

3. **No preemption:** Once `drainQueue()` starts executing a task, it runs to completion (or timeout). A P=0 urgent message queued behind a 120s copilot task waits the full 120s.

4. **No concurrency within a worker:** Each worker is single-threaded in its task execution (`drainQueue` is sequential). A worker stuck on a copilot task cannot simultaneously run a shell task.

5. **Retry cost amplification:** The retry mechanism (`worker.go:229-239`) applies uniformly. A copilot task retried 3× with backoff burns `120s + 1s + 120s + 2s + 120s + 4s = 367s` worst case, during which the worker is completely blocked.

### Task Type Distribution Impact

Assume a realistic workload: 70% shell/message (fast), 20% python/powershell (medium), 10% copilot (slow).

```
With 100 tasks over 4 workers:
  - 70 fast tasks × 50ms  = 3.5s total fast work
  - 20 medium tasks × 5s  = 100s total medium work  
  - 10 slow tasks × 60s   = 600s total slow work
  Total work: ~703.5s
  
Optimal (perfect distribution): 703.5s / 4 = ~176s per worker
  
With current weight-blind RR (worst case):
  Worker A: 4 copilot tasks = 480s
  Worker B: 3 copilot tasks = 360s
  Worker C: 3 copilot tasks = 360s
  Worker D: 0 copilot tasks = 26.75s (all fast + medium)
  
  Completion time: 480s (bottleneck on Worker A)
  Utilization: 703.5s / (480s × 4) = 36.6%
  
With weighted routing:
  Each worker gets ~175s of weighted work
  Completion time: ~176s
  Utilization: 703.5s / (176s × 4) = 99.9%
```

**Performance improvement estimate: 2.7× throughput** for mixed workloads.

### Proposed: Task Type Registry

```go
type TaskProfile struct {
    AvgDurationMs  float64
    TimeoutMs      int64
    MaxRetries     int
    RetryBackoffs  []time.Duration
    IsCPUBound     bool
    IsNetworkBound bool
    Preemptible    bool // can be interrupted for urgent tasks
}

var taskProfiles = map[string]TaskProfile{
    "shell": {
        AvgDurationMs:  50,
        TimeoutMs:      30000,
        MaxRetries:     3,
        RetryBackoffs:  []time.Duration{500*time.Millisecond, 1*time.Second, 2*time.Second},
        IsCPUBound:     false,
        Preemptible:    false, // too fast to bother
    },
    "python": {
        AvgDurationMs:  5000,
        TimeoutMs:      60000,
        MaxRetries:     2,
        RetryBackoffs:  []time.Duration{2*time.Second, 5*time.Second},
        IsCPUBound:     true,
        Preemptible:    true,
    },
    "copilot": {
        AvgDurationMs:  60000,
        TimeoutMs:      120000,
        MaxRetries:     1,  // retrying a 120s task is expensive
        RetryBackoffs:  []time.Duration{5*time.Second},
        IsNetworkBound: true,
        Preemptible:    true,
    },
    "powershell": {
        AvgDurationMs:  5000,
        TimeoutMs:      60000,
        MaxRetries:     2,
        RetryBackoffs:  []time.Duration{2*time.Second, 5*time.Second},
        IsCPUBound:     false,
        Preemptible:    true,
    },
    "message": {
        AvgDurationMs:  1,
        TimeoutMs:      1000,
        MaxRetries:     0,
        Preemptible:    false,
    },
}
```

---

## 5. IDLE DETECTION AND QUEUE DRAIN

**Severity: MEDIUM**

### Current Idle Detection

```go
// worker.go:166-175 — Run() loop
for {
    select {
    case <-w.ctx.Done():
        return
    case <-w.taskNotify:
        w.drainQueue()
    }
}
```

When `drainQueue()` returns (queue empty), the worker blocks on `<-w.taskNotify`. The worker is IDLE but **there is no active signal** to the system. The status is set to IDLE at `worker.go:299` after the last task completes, but:

1. **No idle broadcast:** The worker doesn't announce "I'm idle" on the bus. The orchestrator must poll `/status` to discover idle workers.

2. **No work-stealing trigger:** When a worker goes idle, it doesn't attempt to steal from busy peers. It passively waits for `taskNotify`.

3. **No idle timeout:** A worker that's been idle for hours behaves identically to one idle for milliseconds. No escalation, no self-diagnostic.

4. **taskNotify channel buffering:** The channel has capacity 100 (`worker.go:59`). If >100 tasks are enqueued before the worker wakes up, notifications are silently dropped (the `default` case at line 96-97). This is safe because `drainQueue` drains the entire heap — but it means the worker won't wake up until it processes a notification that was already buffered.

### Missing: Idle Worker Signaling

```
Current behavior when queue drains:
  drainQueue() returns → select blocks on taskNotify → silence

Desired behavior:
  drainQueue() returns → broadcast IDLE signal → attempt steal → select blocks
```

### Proposed: Active Idle Protocol

```go
func (w *Worker) Run() {
    w.log(fmt.Sprintf("Worker %s online", strings.ToUpper(w.Name)))
    go w.heartbeatLoop()

    for {
        select {
        case <-w.ctx.Done():
            return
        case <-w.taskNotify:
            w.drainQueue()
            // After draining, try to steal work
            w.trySteal()
            // If still idle, broadcast availability
            if w.QueueDepth() == 0 {
                w.broadcastIdle()
            }
        }
    }
}

func (w *Worker) trySteal() {
    if w.allWorkers == nil {
        return
    }
    for attempts := 0; attempts < 3; attempts++ {
        task := w.stealFromPeer()
        if task == nil {
            return // no work anywhere
        }
        w.execute(task)
    }
}

func (w *Worker) broadcastIdle() {
    w.bus.Post(w.Name, "workers", "idle",
        fmt.Sprintf("%s idle, ready for work", strings.ToUpper(w.Name)), nil)
}
```

---

## Summary of Findings

| # | Issue | Severity | Current Impact | Proposed Fix | Est. Improvement |
|---|-------|----------|----------------|--------------|------------------|
| 1 | **Convoy Effect** — Weight-blind RR ignores task duration | **CRITICAL** | 120s idle time; 36% utilization on mixed loads | Weighted queue depth | **2.7× throughput** |
| 2 | **No Work-Stealing** — Idle workers sit idle while peers are overloaded | **HIGH** | Workers cannot rebalance | Chase-Lev deque with priority tiers | **~40% latency reduction** for bursty workloads |
| 3 | **Circuit Breaker Rigidity** — Fixed 3-fail threshold, silent task drop | **HIGH** | 30s unnecessary downtime on transient faults; **tasks silently lost** | Sliding window + exponential cooldown + re-queue | **Zero task loss**, adaptive recovery |
| 4 | **Task Type Asymmetry** — Scheduler treats 50ms shell = 120s copilot | **HIGH** | Massive queue imbalance, wasted retries | Task profile registry with type-aware retry/timeout | **Type-proportional distribution** |
| 5 | **Passive Idle Detection** — No steal signal, no idle broadcast | **MEDIUM** | Orchestrator must poll to find idle workers | Active idle protocol + steal attempts | **Sub-second rebalancing** |

### Priority Order for Implementation

1. **Weighted Queue Depth** (Issue #1) — Highest impact, lowest risk. ~30 lines changed in server.go + worker.go.
2. **Circuit Breaker Task Re-queue** (Issue #3, the bug) — Data loss fix. ~15 lines.
3. **Task Profile Registry** (Issue #4) — Type-aware timeouts/retries. ~50 lines.
4. **Sliding Window Circuit Breaker** (Issue #3, full) — Replace fixed threshold. ~100 lines.
5. **Chase-Lev Deque + Work-Stealing** (Issue #2 + #5) — Largest change, highest risk. ~350 lines new + ~150 lines modified.

---

## Appendix A: File References

| File | Lines | Relevant Sections |
|------|-------|-------------------|
| `Skynet/worker.go` | 414 | Entire file: Worker struct, Enqueue, drainQueue, execute, runCommand, heartbeatLoop, workerHeap |
| `Skynet/server.go` | L811-825 | `/dispatch` auto load balancing (RR + queue depth) |
| `Skynet/server.go` | L2008-2013 | `/orchestrate` RR dispatch (no queue depth tiebreaker) |
| `Skynet/server.go` | L2220-2225 | `/pipeline` RR dispatch (no queue depth tiebreaker) |
| `Skynet/types.go` | L22-37 | `Task` struct (Priority, Type, no Weight field) |
| `Skynet/types.go` | L39-52 | `TaskResult` struct |
| `Skynet/types.go` | L122-136 | `AgentView` struct (QueueDepth exposed, no WeightedLoad) |

## Appendix B: Inconsistent Routing Across Endpoints

The three dispatch endpoints use **different routing logic**:

| Endpoint | Queue Depth Tiebreaker? | Weighted? | Worker Status Check? |
|----------|------------------------|-----------|---------------------|
| `/dispatch` (L811) | ✅ Yes | ❌ No | ❌ No |
| `/orchestrate` (L2013) | ❌ No — pure RR | ❌ No | ❌ No |
| `/pipeline` (L2225) | ❌ No — pure RR | ❌ No | ❌ No |

**This is a secondary bug:** `/orchestrate` and `/pipeline` don't even attempt queue-depth balancing. They use raw `rrCounter % len(workers)` which is strictly worse. All three endpoints should use the same routing function.

**Fix:** Extract routing into a shared function:

```go
func (s *SkynetServer) selectWorker() *Worker {
    if len(s.workers) == 0 {
        return nil
    }
    // RR starting point
    best := s.workers[int(atomic.AddInt64(&s.rrCounter, 1)-1)%len(s.workers)]
    bestLoad := best.WeightedLoad()
    for _, wk := range s.workers {
        if load := wk.WeightedLoad(); load < bestLoad {
            bestLoad = load
            best = wk
        }
    }
    return best
}
```

<!-- signed: beta -->
