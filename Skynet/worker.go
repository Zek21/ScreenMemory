package main

import (
	"bytes"
	"container/heap"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Worker — goroutine-based agent. Picks tasks from priority queue, executes, reports.
// Features: per-worker task queue, health monitoring, circuit breaker, retry with backoff.
type Worker struct {
	Name           string
	Status         string
	CurrentTask    string
	tasksCompleted int32  // success only (not errors)
	totalErrors    int32
	totalDurationMs float64
	startTime      time.Time
	lastHeartbeat  time.Time  // internal goroutine heartbeat (always fresh)
	lastExtHB      time.Time  // external heartbeat from skynet_monitor (real liveness)
	extHBReceived  bool       // true after first external heartbeat
	model          string     // actual model, updated via external heartbeat
	recentLogs     []string
	maxLogs        int

	// Circuit breaker
	consecutiveFails int
	circuitState     string // "", "CIRCUIT_OPEN", "HALF_OPEN"
	circuitOpenedAt  time.Time

	// Per-worker task queue (in-memory priority heap)
	taskQueue workerHeap
	taskMu    sync.Mutex
	taskNotify chan struct{}

	bus     *MessageBus
	results chan *TaskResult
	mu      sync.RWMutex
	ctx     context.Context
	cancel  context.CancelFunc
}

var workerNames = []string{"alpha", "beta", "gamma", "delta"}

func NewWorker(name string, bus *MessageBus, results chan *TaskResult) *Worker {
	ctx, cancel := context.WithCancel(context.Background())
	w := &Worker{
		Name:       name,
		Status:     "IDLE",
		startTime:  time.Now(),
		model:      "unknown (no heartbeat yet)",
		maxLogs:    50,
		taskNotify: make(chan struct{}, 100),
		bus:        bus,
		results:    results,
		ctx:        ctx,
		cancel:     cancel,
	}
	heap.Init(&w.taskQueue)
	return w
}

func (w *Worker) log(msg string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	entry := fmt.Sprintf("[%s] %s", time.Now().Format("15:04:05"), msg)
	w.recentLogs = append(w.recentLogs, entry)
	if len(w.recentLogs) > w.maxLogs {
		w.recentLogs = w.recentLogs[len(w.recentLogs)-w.maxLogs:]
	}
	w.lastHeartbeat = time.Now()
}

func (w *Worker) setStatus(status, task string) {
	w.mu.Lock()
	w.Status = status
	w.CurrentTask = task
	w.lastHeartbeat = time.Now()
	w.mu.Unlock()
}

// Enqueue adds a task to this worker's personal queue.
func (w *Worker) Enqueue(task *Task) {
	w.taskMu.Lock()
	heap.Push(&w.taskQueue, task)
	w.taskMu.Unlock()

	select {
	case w.taskNotify <- struct{}{}:
	default:
	}
}

// QueueDepth returns pending tasks for this worker.
func (w *Worker) QueueDepth() int {
	w.taskMu.Lock()
	defer w.taskMu.Unlock()
	return w.taskQueue.Len()
}

// GetState returns a thread-safe snapshot for the dashboard.
func (w *Worker) GetState() *AgentView {
	w.mu.RLock()
	defer w.mu.RUnlock()

	logs := make([]string, len(w.recentLogs))
	copy(logs, w.recentLogs)

	completed := int(atomic.LoadInt32(&w.tasksCompleted))
	errors := int(atomic.LoadInt32(&w.totalErrors))

	progress := 0
	if w.Status == "WORKING" {
		progress = 50
	}

	avgMs := float64(0)
	if completed > 0 {
		avgMs = w.totalDurationMs / float64(completed)
	}

	circuitState := w.circuitState
	consecutiveFails := w.consecutiveFails

	// Model: use externally reported model, not a hardcoded lie
	model := w.model

	// Heartbeat: prefer external heartbeat (real liveness) over internal
	hbTime := w.lastHeartbeat
	if w.extHBReceived {
		hbTime = w.lastExtHB
	}

	return &AgentView{
		Status:           w.Status,
		TasksCompleted:   completed,
		TotalErrors:      errors,
		CurrentTask:      w.CurrentTask,
		RecentLogs:       logs,
		Progress:         progress,
		Model:            model,
		Uptime:           time.Since(w.startTime).Seconds(),
		AvgTaskMs:        avgMs,
		LastHeartbeat:    hbTime.Format("15:04:05"),
		QueueDepth:       w.QueueDepth(),
		CircuitState:     circuitState,
		ConsecutiveFails: consecutiveFails,
	}
}

// Run starts the worker loop — blocks until context cancelled.
func (w *Worker) Run() {
	w.log(fmt.Sprintf("Worker %s online — ready for tasks", strings.ToUpper(w.Name)))
	w.bus.Post(w.Name, "system", "report",
		fmt.Sprintf("%s online, awaiting dispatch", strings.ToUpper(w.Name)), nil)

	// Health heartbeat goroutine
	go w.heartbeatLoop()

	for {
		select {
		case <-w.ctx.Done():
			w.log("Shutting down")
			return
		case <-w.taskNotify:
			w.drainQueue()
		}
	}
}

func (w *Worker) drainQueue() {
	for {
		w.taskMu.Lock()
		if w.taskQueue.Len() == 0 {
			w.taskMu.Unlock()
			return
		}
		task := heap.Pop(&w.taskQueue).(*Task)
		w.taskMu.Unlock()

		w.execute(task)
	}
}

func (w *Worker) execute(task *Task) {
	// Circuit breaker check
	w.mu.RLock()
	cState := w.circuitState
	cOpened := w.circuitOpenedAt
	w.mu.RUnlock()

	if cState == "CIRCUIT_OPEN" {
		if time.Since(cOpened) < 30*time.Second {
			w.log(fmt.Sprintf("⚡ Circuit OPEN — skipping task [%s]", task.ID))
			return
		}
		// Transition to HALF_OPEN
		w.mu.Lock()
		w.circuitState = "HALF_OPEN"
		w.mu.Unlock()
		w.setStatus("HALF_OPEN", task.Description)
		w.log("Circuit → HALF_OPEN, retrying...")
	}

	w.setStatus("WORKING", task.Description)
	w.log(fmt.Sprintf("▶ Task [%s]: %s", task.ID, truncate(task.Description, 60)))

	started := time.Now()
	result := &TaskResult{
		TaskID:      task.ID,
		Description: task.Description,
		WorkerName:  w.Name,
		DirectiveID: task.DirectiveID,
		StartedAt:   started,
	}

	// Real execution with retry + exponential backoff
	maxRetries := 3
	backoffs := []time.Duration{1 * time.Second, 2 * time.Second, 4 * time.Second}
	var lastErr error
	var output string

	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			w.log(fmt.Sprintf("  ↻ Retry %d/%d (backoff %v)", attempt, maxRetries, backoffs[attempt-1]))
			time.Sleep(backoffs[attempt-1])
		}

		output, lastErr = w.runCommand(task)
		if lastErr == nil {
			break
		}
	}

	if lastErr != nil {
		result.Status = "error"
		result.Error = lastErr.Error()
		result.Output = output
		result.ReturnCode = 1
		w.log(fmt.Sprintf("  ✗ Failed after %d retries: %s", maxRetries, truncate(lastErr.Error(), 80)))

		// Circuit breaker: track consecutive failures
		w.mu.Lock()
		w.consecutiveFails++
		if w.consecutiveFails >= 3 {
			w.circuitState = "CIRCUIT_OPEN"
			w.circuitOpenedAt = time.Now()
			w.log("⚡ Circuit breaker OPEN — 3 consecutive failures")
		}
		w.mu.Unlock()
	} else {
		result.Status = "success"
		result.Output = output
		result.ReturnCode = 0
		w.log(fmt.Sprintf("  ✓ %s task done", task.Type))

		// Reset circuit breaker on success
		w.mu.Lock()
		w.consecutiveFails = 0
		if w.circuitState == "HALF_OPEN" {
			w.circuitState = ""
			w.log("Circuit breaker CLOSED — recovered")
		}
		w.mu.Unlock()
	}

	finished := time.Now()
	durationMs := float64(finished.Sub(started).Microseconds()) / 1000.0
	result.DurationMs = durationMs
	result.FinishedAt = finished
	result.OutputLines = len(strings.Split(result.Output, "\n"))

	// Only count successful completions (not errors) to avoid misleading metrics
	if result.Status == "success" {
		atomic.AddInt32(&w.tasksCompleted, 1)
	}
	w.mu.Lock()
	w.totalDurationMs += durationMs
	w.mu.Unlock()

	if result.Status != "success" {
		atomic.AddInt32(&w.totalErrors, 1)
	}

	// Report to bus
	w.bus.Post(w.Name, "results", "report",
		fmt.Sprintf("✓ %s (%.2fms)", truncate(task.Description, 40), durationMs),
		map[string]string{"task_id": task.ID, "duration_ms": fmt.Sprintf("%.2f", durationMs)})

	// Send result to orchestrator
	w.results <- result

	w.setStatus("IDLE", "")
}

// runCommand executes the task command for real using os/exec.
func (w *Worker) runCommand(task *Task) (string, error) {
	timeout := 30 * time.Second
	if task.Type == "copilot" {
		timeout = 120 * time.Second // Copilot CLI needs more time
	}
	ctx, cancel := context.WithTimeout(w.ctx, timeout)
	defer cancel()

	var cmd *exec.Cmd
	switch task.Type {
	case "shell":
		cmd = exec.CommandContext(ctx, "cmd", "/c", task.Command)
	case "python":
		cmd = exec.CommandContext(ctx, "python", "-c", task.Command)
	case "copilot":
		cmd = exec.CommandContext(ctx, "copilot", "-p", task.Command,
			"--model", "claude-opus-4.6-fast",
			"--yolo", "--no-ask-user", "-s")
		cmd.Dir = `D:\Prospects\ScreenMemory`
	case "message":
		// Messages don't need execution
		return task.Command, nil
	default:
		cmd = exec.CommandContext(ctx, "powershell", "-NoProfile", "-Command", task.Command)
	}

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	combined := stdout.String()
	if stderr.Len() > 0 {
		if combined != "" {
			combined += "\n"
		}
		combined += stderr.String()
	}

	if err != nil {
		if combined == "" {
			combined = err.Error()
		}
		return combined, err
	}
	return combined, nil
}

func (w *Worker) heartbeatLoop() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-w.ctx.Done():
			return
		case <-ticker.C:
			w.mu.Lock()
			w.lastHeartbeat = time.Now()
			w.mu.Unlock()
		}
	}
}

// Stop gracefully shuts down the worker.
func (w *Worker) Stop() {
	w.cancel()
}

// ─── Per-worker priority heap ────────────────────────────────────

type workerHeap []*Task

func (h workerHeap) Len() int      { return len(h) }
func (h workerHeap) Less(i, j int) bool {
	if h[i].Priority != h[j].Priority {
		return h[i].Priority < h[j].Priority
	}
	return h[i].DispatchedAt.Before(h[j].DispatchedAt)
}
func (h workerHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *workerHeap) Push(x interface{}) { *h = append(*h, x.(*Task)) }
func (h *workerHeap) Pop() interface{} {
	old := *h
	n := len(old)
	item := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return item
}

// RemoveTask removes a task from this worker's queue by ID. Returns true if found.
func (w *Worker) RemoveTask(taskID string) bool {
	w.taskMu.Lock()
	defer w.taskMu.Unlock()
	for i, t := range w.taskQueue {
		if t.ID == taskID {
			heap.Remove(&w.taskQueue, i)
			return true
		}
	}
	return false
}

// ─── Helpers ─────────────────────────────────────────────────────

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max-3] + "..."
}
