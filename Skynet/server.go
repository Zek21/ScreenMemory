package main

import (
	"bytes"
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// P1.07: sync.Pool for reusable bytes.Buffer — reduces GC pressure on hot JSON paths.
// Used in SSE stream (1Hz), bus publish (WS broadcast), security alerts, and response encoding.
// signed: alpha
var bufPool = sync.Pool{
	New: func() any {
		return new(bytes.Buffer)
	},
}

// getBuffer returns a reset buffer from the pool.
func getBuffer() *bytes.Buffer {
	b := bufPool.Get().(*bytes.Buffer)
	b.Reset()
	return b
}

// putBuffer returns a buffer to the pool. Drops oversized buffers (>64KB) to prevent pool bloat.
func putBuffer(b *bytes.Buffer) {
	if b.Cap() > 65536 {
		return // let GC collect oversized buffers
	}
	bufPool.Put(b)
}

// writeJSON encodes v as JSON into a pooled buffer, writes to w, and returns the buffer to pool.
// Replaces json.NewEncoder(w).Encode(v) on hot paths to reuse allocations.
func writeJSON(w http.ResponseWriter, v any) error {
	buf := getBuffer()
	defer putBuffer(buf)
	if err := json.NewEncoder(buf).Encode(v); err != nil {
		return err
	}
	w.Header().Set("Content-Type", "application/json")
	_, err := w.Write(buf.Bytes())
	return err
}

// marshalPooled encodes v as JSON into a pooled buffer and returns the bytes (caller must putBuffer).
func marshalPooled(v any) (*bytes.Buffer, error) {
	buf := getBuffer()
	if err := json.NewEncoder(buf).Encode(v); err != nil {
		putBuffer(buf)
		return nil, err
	}
	return buf, nil
}

// ─── P1.05: Role-Based Access Control (RBAC) ────────────────────
// Three agent roles with descending privilege: ORCHESTRATOR > WORKER > CONSULTANT.
// Enforced by rbacMiddleware which reads X-Agent-Role header.
// Requests without the header from localhost default to ORCHESTRATOR (backward-compat).
// signed: alpha

type AgentRole string

const (
	RoleOrchestrator AgentRole = "orchestrator"
	RoleWorker       AgentRole = "worker"
	RoleConsultant   AgentRole = "consultant"
)

// endpointACL maps URL path prefixes to the set of roles allowed to access them.
// Endpoints not listed here are open to all authenticated roles (default-allow for reads).
var endpointACL = map[string][]AgentRole{
	// Orchestrator-only: command & control
	"/directive":           {RoleOrchestrator},
	"/dispatch":            {RoleOrchestrator},
	"/cancel":              {RoleOrchestrator},
	"/bus/clear":           {RoleOrchestrator},
	"/orchestrate":         {RoleOrchestrator},
	"/orchestrate/status":  {RoleOrchestrator},
	"/orchestrate/pipeline":{RoleOrchestrator},
	"/brain/ack":           {RoleOrchestrator},

	// Orchestrator + Worker: task lifecycle
	"/bus/tasks/claim":     {RoleOrchestrator, RoleWorker},
	"/bus/tasks/complete":  {RoleOrchestrator, RoleWorker},
	"/task/complete":       {RoleOrchestrator, RoleWorker},

	// WebSocket: orchestrator + worker (consultants use bus HTTP)
	// signed: delta
	"/ws":                  {RoleOrchestrator, RoleWorker},

	// All roles: publish, read bus, stream, status — no entry needed (default-allow)
}

// roleFromHeader parses the X-Agent-Role header. When no header is present,
// defaults to orchestrator for backward compatibility (existing Python tooling
// doesn't send the header yet). When a header IS present but unrecognized,
// returns empty string which rbacMiddleware will reject.
func roleFromHeader(r *http.Request) AgentRole {
	h := strings.ToLower(strings.TrimSpace(r.Header.Get("X-Agent-Role")))
	if h == "" {
		return RoleOrchestrator // backward-compat: no header = full access during rollout
	}
	switch AgentRole(h) {
	case RoleOrchestrator, RoleWorker, RoleConsultant:
		return AgentRole(h)
	}
	return "" // explicit but unrecognized role — deny
}

// rbacMiddleware enforces endpoint-level access control based on agent role.
func rbacMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		role := roleFromHeader(r)

		// Reject unknown roles (non-localhost callers without valid header)
		if role == "" {
			http.Error(w, `{"error":"RBAC: unknown role, set X-Agent-Role header"}`, http.StatusForbidden)
			return
		}

		// Check ACL for the request path — try exact match first, then prefix
		path := r.URL.Path
		var matchedAllowed []AgentRole
		matchedLen := 0
		for prefix, allowed := range endpointACL {
			if strings.HasPrefix(path, prefix) && len(prefix) > matchedLen {
				matchedAllowed = allowed
				matchedLen = len(prefix)
			}
		}
		if matchedLen > 0 {
			permitted := false
			for _, ar := range matchedAllowed {
				if ar == role {
					permitted = true
					break
				}
			}
			if !permitted {
				http.Error(w,
					fmt.Sprintf(`{"error":"RBAC: role '%s' cannot access %s"}`, role, path),
					http.StatusForbidden)
				return
			}
		}

		next.ServeHTTP(w, r)
	})
}

// SkynetServer — HTTP API and dashboard server for Skynet v2.
type SkynetServer struct {
	bus       *MessageBus
	workers   []*Worker
	results   chan *TaskResult
	startTime time.Time

	// Metrics (atomic)
	totalRequests  int64
	totalLatencyUs int64
	tasksDispatched int64
	tasksCompleted int64
	tasksFailed    int64
	rrCounter      int64

	// In-memory worker tasks (not file-based)
	workerTasks []WorkerTask
	wtMu        sync.RWMutex

	// In-memory directives
	directives []Directive
	dirMu      sync.RWMutex

	// Orch thoughts
	thoughts []ThoughtEntry
	thMu     sync.RWMutex

	// Rate limiting: IP → last request time
	rateMu    sync.Mutex
	rateLimit map[string]time.Time

	// Task result history
	taskResults []TaskResult
	trMu        sync.RWMutex

	// Convene sessions
	conveneSessions []ConveneSession
	convMu          sync.RWMutex

	// Security audit log
	securityLog []SecurityEvent
	secMu       sync.RWMutex

	// File I/O mutexes for concurrent goroutine safety
	godFeedMu   sync.Mutex
	brainInboxMu sync.Mutex

	// Task queue for pull-based work distribution
	taskQueue []QueuedTask
	tqMu      sync.RWMutex
	tqSeq     int64 // atomic task ID sequence

	// WebSocket clients for real-time push — P2: hardened with security controls
	// signed: delta
	wsClients    map[chan []byte]bool
	wsMu         sync.RWMutex
	wsBroadcasts int64 // atomic counter
	wsConns      int64 // atomic: current active connection count
	wsRejected   int64 // atomic: total rejected upgrade attempts

	// Spam filter for bus publish deduplication and rate limiting
	spamFilter *SpamFilter // signed: delta

	// Task lifecycle tracker — dispatch-to-result visibility
	// signed: gamma
	taskTrackers []TaskTracker
	ttMu         sync.RWMutex

	// Per-worker circuit breakers — centralised view into each worker's
	// circuit breaker state.  Populated on server init, read by
	// GET /worker/{name}/health.
	// signed: beta
	workerCircuitBreakers map[string]*Worker
}

// ConveneSession represents a multi-worker coordination session.
type ConveneSession struct {
	ID              string       `json:"id"`
	Initiator       string       `json:"initiator"`
	Topic           string       `json:"topic"`
	Context         string       `json:"context"`
	NeedWorkers     int          `json:"need_workers"`
	Participants    []string     `json:"participants"`
	Messages        []BusMessage `json:"messages"`
	CreatedAt       time.Time    `json:"created_at"`
	Status          string       `json:"status"`
	StatusChangedAt *time.Time   `json:"status_changed_at,omitempty"` // when status last changed -- signed: beta
}

// SecurityEvent represents a blocked or flagged security event.
type SecurityEvent struct {
	Timestamp string `json:"timestamp"`
	Source    string `json:"source"`
	Type     string `json:"type"`
	Details  string `json:"details"`
	Blocked  bool   `json:"blocked"`
}

// QueuedTask represents a task in the pull-based work queue.
type QueuedTask struct {
	ID          string     `json:"id"`
	Task        string     `json:"task"`
	Priority    int        `json:"priority"` // 0=normal, 1=high, 2=critical
	Source      string     `json:"source"`   // who posted the task
	ClaimedBy   string     `json:"claimed_by,omitempty"`
	Status      string     `json:"status"` // "pending", "claimed", "completed", "failed"
	Result      string     `json:"result,omitempty"`
	CreatedAt   time.Time  `json:"created_at"`
	ClaimedAt   *time.Time `json:"claimed_at,omitempty"`
	DoneAt      *time.Time `json:"done_at,omitempty"`
	ResultSetAt *time.Time `json:"result_set_at,omitempty"` // when result was written -- signed: beta
	AutoClaimed bool       `json:"auto_claimed,omitempty"`  // true if auto-assigned to idle worker -- signed: alpha
}

func NewSkynetServer(bus *MessageBus, workers []*Worker, results chan *TaskResult) *SkynetServer {
	// Build per-worker circuit breaker lookup map -- signed: beta
	cbMap := make(map[string]*Worker, len(workers))
	for _, w := range workers {
		cbMap[strings.ToLower(w.Name)] = w
	}

	return &SkynetServer{
		bus:         bus,
		workers:     workers,
		results:     results,
		startTime:   time.Now(),
		workerTasks: make([]WorkerTask, 0),
		directives:  make([]Directive, 0),
		thoughts:    make([]ThoughtEntry, 0),
		rateLimit:       make(map[string]time.Time),
		taskResults:     make([]TaskResult, 0),
		conveneSessions: make([]ConveneSession, 0),
		securityLog:     make([]SecurityEvent, 0),
		taskQueue:       make([]QueuedTask, 0),
		wsClients:       make(map[chan []byte]bool),
		spamFilter:      NewSpamFilter(), // signed: delta
		taskTrackers:    make([]TaskTracker, 0), // signed: gamma
		workerCircuitBreakers: cbMap, // signed: beta
	}
}

// Handler returns the mux with middleware applied.
func (s *SkynetServer) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", s.handleStatus)
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/directive", s.handleDirective)
	mux.HandleFunc("/metrics", s.handleMetrics)
	mux.HandleFunc("/god_feed", s.handleGodFeed)
	mux.HandleFunc("/brain/pending", s.handleBrainPending)
	mux.HandleFunc("/brain/ack", s.handleBrainAck)
	mux.HandleFunc("/dispatch", s.handleDispatch)
	mux.HandleFunc("/results", s.handleResults)
	mux.HandleFunc("/cancel", s.handleCancel)
	mux.HandleFunc("/worker/", s.handleWorkerRoute)
	mux.HandleFunc("/dashboard", s.handleDashboard)
	mux.HandleFunc("/stream", s.handleSSEStream)
	mux.HandleFunc("/activity/stream", s.handleActivityStream)
	mux.HandleFunc("/bus/publish", s.handleBusPublish)
	mux.HandleFunc("/bus/messages", s.handleBusMessages)
	mux.HandleFunc("/bus/clear", s.handleBusClear)
	mux.HandleFunc("/bus/convene", s.handleBusConvene)
	mux.HandleFunc("/bus/tasks", s.handleBusTasks)
	mux.HandleFunc("/bus/tasks/claim", s.handleBusTaskClaim)
	mux.HandleFunc("/bus/tasks/complete", s.handleBusTaskComplete)
	mux.HandleFunc("/orchestrate", s.handleOrchestrate)
	mux.HandleFunc("/orchestrate/status", s.handleOrchestrateStatus)
	mux.HandleFunc("/orchestrate/pipeline", s.handleOrchestratePipeline)
	mux.HandleFunc("/security/audit", s.handleSecurityAudit)
	mux.HandleFunc("/security/blocked", s.handleSecurityBlocked)
	mux.HandleFunc("/ws", s.handleWebSocket)
	mux.HandleFunc("/ws/stats", s.handleWSStats)
	mux.HandleFunc("/tasks", s.handleTasks) // Task lifecycle tracker -- signed: gamma
	mux.HandleFunc("/task/complete", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			atomic.AddInt64(&s.tasksCompleted, 1)
			w.WriteHeader(200)
			w.Write([]byte(`{"ok":true}`))
			return
		}
		http.Error(w, "Method not allowed", 405)
	})
	return s.rateLimitMiddleware(rbacMiddleware(s.middleware(mux))) // P1.05: RBAC in chain — signed: alpha
}

// ─── Middleware ───────────────────────────────────────────────────

func (s *SkynetServer) middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		atomic.AddInt64(&s.totalRequests, 1)

		// Limit request body size to 1MB to prevent DoS
		if r.Body != nil {
			r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		}

		// CORS on every response
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-Agent-Role") // P1.05 RBAC header

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}

		next.ServeHTTP(w, r)

		latUs := time.Since(start).Microseconds()
		atomic.AddInt64(&s.totalLatencyUs, latUs)
	})
}

// ─── GET /status ─────────────────────────────────────────────────

func (s *SkynetServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	agents := make(map[string]*AgentView)
	for _, wk := range s.workers {
		agents[wk.Name] = wk.GetState()
	}

	s.thMu.RLock()
	thoughts := make([]ThoughtEntry, len(s.thoughts))
	copy(thoughts, s.thoughts)
	s.thMu.RUnlock()

	payload := DashboardPayload{
		Agents:    agents,
		OrchFeed:  thoughts,
		Bus:       s.bus.Recent(20),
		Uptime:    time.Since(s.startTime).Seconds(),
		Version:   "2.0.0",
		System:    "skynet",
		Timestamp: time.Now().Format(time.RFC3339), // signed: beta
	}

	writeJSON(w, payload) // P1.07: pooled buffer — signed: alpha
}

// ─── GET /health ─────────────────────────────────────────────────

func (s *SkynetServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	alive := 0
	for _, wk := range s.workers {
		wk.mu.RLock()
		// Use external heartbeat (real liveness) if available,
		// fall back to internal heartbeat for goroutine-only workers
		hb := wk.lastHeartbeat
		if wk.extHBReceived {
			hb = wk.lastExtHB
		}
		if time.Since(hb) < 120*time.Second {
			alive++
		}
		wk.mu.RUnlock()
	}
	w.Header().Set("Content-Type", "application/json")
	now := time.Now() // signed: beta
	json.NewEncoder(w).Encode(HealthResponse{
		Status:       "ok",
		Uptime:       time.Since(s.startTime).Seconds(),
		Workers:      alive,
		BusDepth:     s.bus.Depth(),
		Timestamp:    now.UnixNano(),
		TimestampRFC: now.Format(time.RFC3339),
	})
}

// ─── POST /directive ─────────────────────────────────────────────

func (s *SkynetServer) handleDirective(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Goal      string `json:"goal"`
		Directive string `json:"directive"`
		Priority  int    `json:"priority"`
		Route     string `json:"route"`
		TaskType  string `json:"type"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.Goal == "" {
		http.Error(w, "goal required", http.StatusBadRequest)
		return
	}
	if req.Priority < 1 || req.Priority > 10 {
		req.Priority = 3
	}

	now := time.Now()
	d := Directive{
		ID:        fmt.Sprintf("dir_%d", now.UnixNano()),
		Goal:      req.Goal,
		Priority:  req.Priority,
		Status:    "active",
		CreatedAt: now,
		Route:     req.Route,
	}

	s.dirMu.Lock()
	s.directives = append(s.directives, d)
	if len(s.directives) > 500 {
		kept := make([]Directive, 0, 500)
		for _, dd := range s.directives {
			if dd.Status != "completed" {
				kept = append(kept, dd)
			}
		}
		if len(kept) > 500 {
			kept = kept[len(kept)-500:]
		}
		s.directives = kept
	}
	s.dirMu.Unlock()

	s.addThought("directive", fmt.Sprintf("New directive: %s (priority %d)", req.Goal, req.Priority))
	s.bus.Post("skynet", "directives", "new", req.Goal, map[string]string{"priority": fmt.Sprintf("%d", req.Priority)})

	// Write to god_feed.json for GOD Console display
	go s.appendGodFeed("god_prompt", req.Goal)

	// Write to brain_inbox.json for Orchestrator polling
	go s.appendBrainInbox(d.ID, req.Goal)

	// Route to a worker if specified, otherwise auto-complete the directive
	if req.Route != "" {
		for _, wk := range s.workers {
			if strings.EqualFold(wk.Name, req.Route) {
				cmdText := req.Directive
				if cmdText == "" {
					cmdText = req.Goal
				}
				taskType := req.TaskType
				if taskType == "" {
					taskType = "copilot" // Default to Copilot CLI (Claude Opus 4.6 fast)
				}
				maxRetries := 2
				if taskType == "copilot" {
					maxRetries = 1 // Copilot tasks are expensive, fewer retries
				}
				task := &Task{
					ID:           fmt.Sprintf("task_%d", now.UnixNano()),
					Type:         taskType,
					Command:      cmdText,
					Description:  req.Goal,
					Priority:     req.Priority,
					DirectiveID:  d.ID,
					DispatchedAt: now,
					MaxRetries:   maxRetries,
				}
				wk.Enqueue(task)
				atomic.AddInt64(&s.tasksDispatched, 1)

				// Track task lifecycle -- signed: gamma
				s.ttMu.Lock()
				s.taskTrackers = append(s.taskTrackers, TaskTracker{
					TaskID:       task.ID,
					Worker:       wk.Name,
					Goal:         req.Goal,
					DispatchedAt: now,
					Status:       "dispatched",
					DirectiveID:  d.ID,
				})
				// Keep only last 200 entries to bound memory
				if len(s.taskTrackers) > 200 {
					s.taskTrackers = s.taskTrackers[len(s.taskTrackers)-200:]
				}
				s.ttMu.Unlock()

				// Track sub-task for directive completion
				s.dirMu.Lock()
				for i := range s.directives {
					if s.directives[i].ID == d.ID {
						s.directives[i].SubTasks = append(s.directives[i].SubTasks, task.ID)
						break
					}
				}
				s.dirMu.Unlock()

				s.wtMu.Lock()
				s.workerTasks = append(s.workerTasks, WorkerTask{
					TaskID:     task.ID,
					Worker:     wk.Name,
					Directive:  d.ID,
					Status:     "pending",
					AssignedAt: now.Format(time.RFC3339),
				})
				s.wtMu.Unlock()

				break
			}
		}
	} else {
		// No route — this is an informational directive with no tasks.
		// Auto-complete it immediately so it doesn't stay stuck as "active".
		s.dirMu.Lock()
		for i := range s.directives {
			if s.directives[i].ID == d.ID {
				s.directives[i].Status = "completed"
				s.directives[i].CompletedAt = now
				break
			}
		}
		s.dirMu.Unlock()
		s.addThought("directive", fmt.Sprintf("Directive %s auto-completed (no route)", d.ID))
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":       "ok",
		"directive_id": d.ID,
		"goal":         d.Goal,
		"priority":     d.Priority,
	})
}

// ─── GET /metrics ────────────────────────────────────────────────

func (s *SkynetServer) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	uptime := time.Since(s.startTime).Seconds()
	totalReq := atomic.LoadInt64(&s.totalRequests)
	totalLat := atomic.LoadInt64(&s.totalLatencyUs)

	var avgLatUs float64
	if totalReq > 0 {
		avgLatUs = float64(totalLat) / float64(totalReq)
	}

	rps := float64(0)
	if uptime > 0 {
		rps = float64(totalReq) / uptime
	}

	dispatched := atomic.LoadInt64(&s.tasksDispatched)
	completed := atomic.LoadInt64(&s.tasksCompleted)
	failed := atomic.LoadInt64(&s.tasksFailed)

	tpm := float64(0)
	if uptime > 0 {
		tpm = float64(completed+failed) / (uptime / 60.0)
	}

	// Per-worker stats
	now := time.Now().Format(time.RFC3339) // signed: beta
	wStats := make(map[string]WStats)
	for _, wk := range s.workers {
		st := wk.GetState()
		wStats[wk.Name] = WStats{
			TasksCompleted: st.TasksCompleted,
			TotalErrors:    st.TotalErrors,
			AvgTaskMs:      st.AvgTaskMs,
			Status:         st.Status,
			Timestamp:      now, // signed: beta
		}
	}

	// Directive stats
	s.dirMu.RLock()
	dTotal := len(s.directives)
	dActive, dCompleted, dPending := 0, 0, 0
	for _, d := range s.directives {
		switch d.Status {
		case "active":
			dActive++
		case "completed":
			dCompleted++
		case "pending":
			dPending++
		}
	}
	s.dirMu.RUnlock()

	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)

	resp := MetricsResponse{
		Uptime:          uptime,
		TotalRequests:   totalReq,
		RequestsPerSec:  rps,
		AvgLatencyUs:    avgLatUs,
		TasksDispatched: dispatched,
		TasksCompleted:  completed,
		TasksFailed:     failed,
		TaskThroughput:  tpm,
		BusMessages:     s.bus.Count(),
		BusDropped:      s.bus.Dropped(),
		WorkerStats:     wStats,
		Directives:      DirectiveStats{Total: dTotal, Active: dActive, Completed: dCompleted, Pending: dPending, Timestamp: now}, // signed: beta
		GoroutineCount:  runtime.NumGoroutine(),
		MemAllocMB:      float64(memStats.Alloc) / (1024 * 1024),
		Timestamp:       now, // signed: beta
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// ─── GET /god_feed ───────────────────────────────────────────────

func (s *SkynetServer) handleGodFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	feedPath := `D:\Prospects\ScreenMemory\data\brain\god_feed.json`
	data, err := os.ReadFile(feedPath)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode([]interface{}{})
		return
	}
	var feed interface{}
	if json.Unmarshal(data, &feed) == nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(feed)
	} else {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode([]interface{}{})
	}
}

// ─── GET /brain/pending ──────────────────────────────────────────

func (s *SkynetServer) handleBrainPending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	inboxPath := `D:\Prospects\ScreenMemory\data\brain\brain_inbox.json`
	data, err := os.ReadFile(inboxPath)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode([]interface{}{})
		return
	}

	var inbox []map[string]interface{}
	if err := json.Unmarshal(data, &inbox); err != nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode([]interface{}{})
		return
	}

	var pending []map[string]interface{}
	for _, item := range inbox {
		if item["status"] == "pending" {
			pending = append(pending, item)
		}
	}
	if pending == nil {
		pending = []map[string]interface{}{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(pending)
}

// ─── POST /brain/ack ─────────────────────────────────────────────

func (s *SkynetServer) handleBrainAck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		RequestID string `json:"request_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.RequestID == "" {
		http.Error(w, "request_id required", http.StatusBadRequest)
		return
	}

	inboxPath := `D:\Prospects\ScreenMemory\data\brain\brain_inbox.json`
	data, err := os.ReadFile(inboxPath)
	if err != nil {
		http.Error(w, "inbox not found", http.StatusInternalServerError)
		return
	}

	var inbox []map[string]interface{}
	if err := json.Unmarshal(data, &inbox); err != nil {
		http.Error(w, "inbox parse error", http.StatusInternalServerError)
		return
	}

	found := false
	for i, item := range inbox {
		if item["request_id"] == req.RequestID {
			inbox[i]["status"] = "completed"
			inbox[i]["completed_at"] = float64(time.Now().UnixMilli()) / 1000.0
			found = true
		}
	}

	if !found {
		http.Error(w, "request_id not found", http.StatusNotFound)
		return
	}

	out, _ := json.MarshalIndent(inbox, "", "  ")
	os.WriteFile(inboxPath, out, 0644)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":     "ok",
		"request_id": req.RequestID,
		"marked":     "completed",
	})
}

// ─── POST /dispatch ──────────────────────────────────────────────

func (s *SkynetServer) handleDispatch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Worker    string `json:"worker"`
		Directive string `json:"directive"`
		TaskID    string `json:"task_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.Directive == "" || req.TaskID == "" {
		http.Error(w, "directive and task_id required", http.StatusBadRequest)
		return
	}

	// Auto load balancing: round-robin with lowest queue depth tiebreaker
	if req.Worker == "" {
		if len(s.workers) == 0 {
			http.Error(w, "no workers available", http.StatusServiceUnavailable)
			return
		}
		best := s.workers[int(atomic.AddInt64(&s.rrCounter, 1)-1)%len(s.workers)]
		bestDepth := best.QueueDepth()
		for _, wk := range s.workers {
			if d := wk.QueueDepth(); d < bestDepth {
				bestDepth = d
				best = wk
			}
		}
		req.Worker = best.Name
	}

	task := WorkerTask{
		TaskID:     req.TaskID,
		Worker:     strings.ToLower(req.Worker),
		Directive:  req.Directive,
		Status:     "pending",
		AssignedAt: time.Now().Format(time.RFC3339),
	}

	s.wtMu.Lock()
	s.workerTasks = append(s.workerTasks, task)
	if len(s.workerTasks) > 1000 {
		s.workerTasks = s.workerTasks[len(s.workerTasks)-1000:]
	}
	s.wtMu.Unlock()

	atomic.AddInt64(&s.tasksDispatched, 1)
	s.addThought("route", fmt.Sprintf("[dispatch] task %s → %s: %s", req.TaskID, req.Worker, req.Directive))
	s.bus.Post("skynet", "dispatch", "task", req.Directive, map[string]string{"worker": req.Worker, "task_id": req.TaskID})

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"task_id": req.TaskID,
		"worker":  req.Worker,
	})
}

// ─── /worker/{name}/tasks and /worker/{name}/result ──────────────

func (s *SkynetServer) handleWorkerRoute(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) != 3 || parts[0] != "worker" {
		http.Error(w, "use /worker/{name}/tasks or /worker/{name}/result", http.StatusBadRequest)
		return
	}
	workerName := strings.ToLower(parts[1])
	action := parts[2]

	switch action {
	case "tasks":
		s.handleWorkerTasks(w, r, workerName)
	case "result":
		s.handleWorkerResult(w, r, workerName)
	case "heartbeat":
		s.handleWorkerHeartbeat(w, r, workerName)
	case "status":
		s.handleWorkerStatus(w, r, workerName)
	case "health":
		s.handleWorkerHealth(w, r, workerName) // signed: beta
	case "activity":
		if r.Method == http.MethodPost {
			s.handleWorkerActivityPost(w, r, workerName)
		} else {
			s.handleWorkerActivityGet(w, r, workerName)
		}
	default:
		http.Error(w, "unknown action, use tasks, result, heartbeat, status, health, or activity", http.StatusBadRequest)
	}
}

func (s *SkynetServer) handleWorkerTasks(w http.ResponseWriter, r *http.Request, workerName string) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	s.wtMu.RLock()
	var pending []map[string]string
	for _, t := range s.workerTasks {
		if t.Worker == workerName && t.Status == "pending" {
			pending = append(pending, map[string]string{
				"id":          t.TaskID,
				"directive":   t.Directive,
				"status":      t.Status,
				"assigned_at": t.AssignedAt,
			})
		}
	}
	s.wtMu.RUnlock()

	if pending == nil {
		pending = []map[string]string{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(pending)
}

func (s *SkynetServer) handleWorkerResult(w http.ResponseWriter, r *http.Request, workerName string) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		TaskID string `json:"task_id"`
		Result string `json:"result"`
		Status string `json:"status"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.TaskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return
	}

	s.wtMu.Lock()
	found := false
	var directiveID string
	for i, t := range s.workerTasks {
		if t.TaskID == req.TaskID && t.Worker == workerName {
			s.workerTasks[i].Status = "completed"
			s.workerTasks[i].Result = req.Result
			s.workerTasks[i].CompletedAt = time.Now().Format(time.RFC3339)
			directiveID = t.Directive
			found = true
			break
		}
	}
	s.wtMu.Unlock()

	if !found {
		http.Error(w, "task not found for this worker", http.StatusNotFound)
		return
	}

	status := req.Status
	if status == "" || status == "completed" {
		status = "success"
	}
	if status == "success" {
		atomic.AddInt64(&s.tasksCompleted, 1)
	} else {
		atomic.AddInt64(&s.tasksFailed, 1)
	}

	s.addThought("info", fmt.Sprintf("[%s] completed task %s", workerName, req.TaskID))

	// Store in result history
	s.storeTaskResult(&TaskResult{
		TaskID:      req.TaskID,
		Status:      status,
		Output:      req.Result,
		WorkerName:  workerName,
		DirectiveID: directiveID,
		FinishedAt:  time.Now(),
	})

	// Check directive completion
	s.checkDirectiveCompletion(directiveID)

	// Broadcast worker state change to WebSocket clients
	wsMsg, _ := json.Marshal(map[string]interface{}{
		"type": "worker_update", "worker": workerName, "task_id": req.TaskID,
		"status": status, "directive_id": directiveID,
		"timestamp": time.Now().Format(time.RFC3339), // signed: beta
	})
	s.broadcastWS(wsMsg)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"task_id": req.TaskID,
		"worker":  workerName,
	})
}

// ─── POST /worker/{name}/heartbeat ───────────────────────────────
// External monitor (skynet_monitor.py) calls this to report real window health.
// Bumps the worker's lastHeartbeat so /health workers_alive is accurate.

func (s *SkynetServer) handleWorkerHeartbeat(w http.ResponseWriter, r *http.Request, workerName string) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		HWNDAlive bool   `json:"hwnd_alive"`
		Visible   bool   `json:"visible"`
		Model     string `json:"model"`
		GridSlot  string `json:"grid_slot"`
                State     string `json:"state"`
        }
        if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
                http.Error(w, "Bad JSON: "+err.Error(), http.StatusBadRequest)
                return
        }

        // Find the internal worker and bump heartbeat
        var wk *Worker
        for _, w2 := range s.workers {
                if strings.ToLower(w2.Name) == workerName {
                        wk = w2
                        break
                }
        }
        if wk == nil {
                http.Error(w, "worker not found: "+workerName, http.StatusNotFound)
                return
        }

        wk.mu.Lock()
        wk.lastHeartbeat = time.Now()
        wk.lastExtHB = time.Now()
        wk.extHBReceived = true
        if req.Model != "" {
                wk.model = req.Model
        }
        prevStatus := wk.Status
        if req.State != "" {
                wk.Status = req.State
        }
        wk.mu.Unlock()

	// Auto-claim: when worker transitions to IDLE, assign next pending task -- signed: alpha
	if req.State == "IDLE" && prevStatus != "IDLE" {
		if claimed, taskID := s.autoClaimNextTask(workerName); claimed {
			s.bus.Post("skynet", "tasks", "auto_claimed",
				fmt.Sprintf("Auto-claimed %s for %s (heartbeat IDLE transition)", taskID, workerName), nil)
		}
	}

	if !req.HWNDAlive {
		s.bus.Post("monitor", "orchestrator", "alert",
			fmt.Sprintf("WORKER %s DEAD -- hwnd_alive=%v visible=%v", strings.ToUpper(workerName), req.HWNDAlive, req.Visible),
			map[string]string{"worker": workerName, "severity": "critical"})
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":    "ok",
		"worker":    workerName,
		"hwnd_alive": req.HWNDAlive,
	})
}

// ─── GET /worker/{name}/status ───────────────────────────────────

func (s *SkynetServer) handleWorkerStatus(w http.ResponseWriter, r *http.Request, workerName string) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	var wk *Worker
	for _, w2 := range s.workers {
		if strings.ToLower(w2.Name) == workerName {
			wk = w2
			break
		}
	}
	if wk == nil {
		http.Error(w, "worker not found: "+workerName, http.StatusNotFound)
		return
	}

	wk.mu.RLock()
	lastHB := wk.lastHeartbeat
	wk.mu.RUnlock()

	s.wtMu.RLock()
	var pending, running []map[string]string
	for _, t := range s.workerTasks {
		if strings.ToLower(t.Worker) == workerName {
			entry := map[string]string{"id": t.TaskID, "directive": t.Directive[:min(80, len(t.Directive))], "status": t.Status}
			if t.Status == "pending" {
				pending = append(pending, entry)
			} else if t.Status == "running" {
				running = append(running, entry)
			}
		}
	}
	s.wtMu.RUnlock()

	alive := time.Since(lastHB) < 120*time.Second
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"worker":         workerName,
		"alive":          alive,
		"last_heartbeat": lastHB.Format(time.RFC3339),
		"pending_tasks":  len(pending),
		"running_tasks":  len(running),
		"tasks":          append(running, pending...),
	})
}

// ─── GET /worker/{name}/health ───────────────────────────────────
// Returns per-worker circuit breaker state and liveness summary.
// signed: beta

func (s *SkynetServer) handleWorkerHealth(w http.ResponseWriter, r *http.Request, workerName string) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	wk, ok := s.workerCircuitBreakers[workerName]
	if !ok {
		http.Error(w, "worker not found: "+workerName, http.StatusNotFound)
		return
	}

	wk.mu.RLock()
	hbTime := wk.lastHeartbeat
	if wk.extHBReceived {
		hbTime = wk.lastExtHB
	}
	// If no heartbeat received yet, use startTime (worker just booted)
	if hbTime.IsZero() {
		hbTime = wk.startTime
	}
	cbState := wk.circuitState
	if cbState == "" {
		cbState = "CLOSED"
	}
	cb := &CircuitBreaker{
		State:            cbState,
		ConsecutiveFails: wk.consecutiveFails,
		FailThreshold:    3,
		CooldownSec:      30,
		OpenedAt:         wk.circuitOpenedAt,
	}
	completed := int(wk.tasksCompleted)
	errors := int(wk.totalErrors)
	uptime := time.Since(wk.startTime).Seconds()
	qd := wk.QueueDepth()
	wk.mu.RUnlock()

	alive := time.Since(hbTime) < 120*time.Second
	healthy := alive && cbState != "CIRCUIT_OPEN"

	resp := WorkerHealthResponse{
		Worker:         workerName,
		Healthy:        healthy,
		CircuitBreaker: cb,
		Alive:          alive,
		LastHeartbeat:  hbTime.Format(time.RFC3339),
		TasksCompleted: completed,
		TotalErrors:    errors,
		QueueDepth:     qd,
		Uptime:         uptime,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// ─── POST /worker/{name}/activity ────────────────────────────────

func (s *SkynetServer) handleWorkerActivityPost(w http.ResponseWriter, r *http.Request, workerName string) {
	var req struct {
		CurrentTask  string `json:"current_task"`
		ActivityType string `json:"activity_type"`
		Detail       string `json:"detail"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON: "+err.Error(), http.StatusBadRequest)
		return
	}

	var wk *Worker
	for _, w2 := range s.workers {
		if strings.ToLower(w2.Name) == workerName {
			wk = w2
			break
		}
	}
	if wk == nil {
		http.Error(w, "worker not found: "+workerName, http.StatusNotFound)
		return
	}

	wk.mu.Lock()
	if req.CurrentTask != "" {
		wk.CurrentTask = req.CurrentTask
	}
	logEntry := fmt.Sprintf("[%s] %s: %s", time.Now().Format("15:04:05"), req.ActivityType, req.Detail)
	wk.recentLogs = append(wk.recentLogs, logEntry)
	if len(wk.recentLogs) > wk.maxLogs {
		wk.recentLogs = wk.recentLogs[len(wk.recentLogs)-wk.maxLogs:]
	}
	wk.lastHeartbeat = time.Now()
	wk.mu.Unlock()

	// Broadcast activity to WebSocket clients
	wsMsg, _ := json.Marshal(map[string]interface{}{
		"type": "worker_activity", "worker": workerName,
		"current_task": req.CurrentTask, "activity_type": req.ActivityType,
		"detail": req.Detail,
		"timestamp": time.Now().Format(time.RFC3339), // signed: beta
	})
	s.broadcastWS(wsMsg)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok", "worker": workerName})
}

// ─── GET /worker/{name}/activity ─────────────────────────────────

func (s *SkynetServer) handleWorkerActivityGet(w http.ResponseWriter, r *http.Request, workerName string) {
	var wk *Worker
	for _, w2 := range s.workers {
		if strings.ToLower(w2.Name) == workerName {
			wk = w2
			break
		}
	}
	if wk == nil {
		http.Error(w, "worker not found: "+workerName, http.StatusNotFound)
		return
	}

	wk.mu.RLock()
	state := wk.Status
	currentTask := wk.CurrentTask
	completed := int(atomic.LoadInt32(&wk.tasksCompleted))
	avgMs := float64(0)
	if completed > 0 {
		avgMs = wk.totalDurationMs / float64(completed)
	}
	hbTime := wk.lastHeartbeat
	if wk.extHBReceived {
		hbTime = wk.lastExtHB
	}
	// Return last 20 logs
	logCount := len(wk.recentLogs)
	start := 0
	if logCount > 20 {
		start = logCount - 20
	}
	logs := make([]string, logCount-start)
	copy(logs, wk.recentLogs[start:])
	wk.mu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"name":            workerName,
		"state":           state,
		"current_task":    currentTask,
		"recent_logs":     logs,
		"tasks_completed": completed,
		"avg_task_ms":     avgMs,
		"last_heartbeat":  hbTime.Format(time.RFC3339),
	})
}

// ─── GET /activity/stream (SSE) ──────────────────────────────────
// Lightweight SSE that streams only worker activity every 2 seconds.

func (s *SkynetServer) handleActivityStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			workers := make(map[string]interface{})
			for _, wk := range s.workers {
				wk.mu.RLock()
				workers[wk.Name] = map[string]interface{}{
					"state":        wk.Status,
					"current_task": wk.CurrentTask,
				}
				wk.mu.RUnlock()
			}
			data, _ := json.Marshal(map[string]interface{}{
				"workers":   workers,
				"timestamp": time.Now().UnixNano(),
			})
			fmt.Fprintf(w, "data: %s\n\n", data)
			flusher.Flush()
		}
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// ─── POST /bus/clear ─────────────────────────────────────────────

func (s *SkynetServer) handleBusClear(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	cleared := s.bus.Clear()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"cleared": cleared,
	})
}

func (s *SkynetServer) handleDashboard(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache, no-store")
	fmt.Fprint(w, skynetDashboardHTML)
}

// ─── Task Queue: /bus/tasks, /bus/tasks/claim, /bus/tasks/complete ──

func (s *SkynetServer) handleBusTasks(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	if r.Method == http.MethodGet {
		// Return unclaimed (pending) tasks, optionally all with ?all=true
		showAll := r.URL.Query().Get("all") == "true"
		s.tqMu.RLock()
		var result []QueuedTask
		for _, t := range s.taskQueue {
			if showAll || t.Status == "pending" {
				result = append(result, t)
			}
		}
		s.tqMu.RUnlock()
		if result == nil {
			result = []QueuedTask{}
		}
		json.NewEncoder(w).Encode(result)
		return
	}

	if r.Method != http.MethodPost {
		http.Error(w, "GET or POST", http.StatusMethodNotAllowed)
		return
	}

	// POST: add a new task to the queue
	var req struct {
		Task     string `json:"task"`
		Priority int    `json:"priority"`
		Source   string `json:"source"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.Task == "" {
		http.Error(w, "task is required", http.StatusBadRequest)
		return
	}
	if req.Source == "" {
		req.Source = "anonymous"
	}

	id := fmt.Sprintf("tq_%d", atomic.AddInt64(&s.tqSeq, 1))
	task := QueuedTask{
		ID:        id,
		Task:      req.Task,
		Priority:  req.Priority,
		Source:    req.Source,
		Status:    "pending",
		CreatedAt: time.Now(),
	}

	s.tqMu.Lock()
	s.taskQueue = append(s.taskQueue, task)
	// Cap at 200 tasks (drop oldest completed)
	if len(s.taskQueue) > 200 {
		var kept []QueuedTask
		for _, t := range s.taskQueue {
			if t.Status != "completed" && t.Status != "failed" {
				kept = append(kept, t)
			}
		}
		if len(kept) > 200 {
			kept = kept[len(kept)-200:]
		}
		s.taskQueue = kept
	}
	s.tqMu.Unlock()

	s.bus.Post(req.Source, "tasks", "queued", fmt.Sprintf("Task queued: %s [%s]", id, req.Task[:min(60, len(req.Task))]), nil)

	json.NewEncoder(w).Encode(map[string]string{"status": "queued", "task_id": id})
}

func (s *SkynetServer) handleBusTaskClaim(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		TaskID string `json:"task_id"`
		Worker string `json:"worker"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}

	s.tqMu.Lock()
	found := false
	for i := range s.taskQueue {
		if s.taskQueue[i].ID == req.TaskID && s.taskQueue[i].Status == "pending" {
			now := time.Now()
			s.taskQueue[i].Status = "claimed"
			s.taskQueue[i].ClaimedBy = req.Worker
			s.taskQueue[i].ClaimedAt = &now
			found = true
			break
		}
	}
	s.tqMu.Unlock()

	if found {
		s.bus.Post(req.Worker, "tasks", "claimed", fmt.Sprintf("%s claimed by %s", req.TaskID, req.Worker), nil)
		json.NewEncoder(w).Encode(map[string]string{"status": "claimed", "task_id": req.TaskID})
	} else {
		http.Error(w, "task not found or already claimed", http.StatusConflict)
	}
}

func (s *SkynetServer) handleBusTaskComplete(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		TaskID string `json:"task_id"`
		Worker string `json:"worker"`
		Result string `json:"result"`
		Status string `json:"status"` // "completed" or "failed"
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.Status == "" {
		req.Status = "completed"
	}

	s.tqMu.Lock()
	found := false
	for i := range s.taskQueue {
		if s.taskQueue[i].ID == req.TaskID && s.taskQueue[i].ClaimedBy == req.Worker {
			now := time.Now()
			s.taskQueue[i].Status = req.Status
			s.taskQueue[i].Result = req.Result
			s.taskQueue[i].DoneAt = &now
			s.taskQueue[i].ResultSetAt = &now // signed: beta
			found = true
			break
		}
	}
	s.tqMu.Unlock()

	if found {
		s.bus.Post(req.Worker, "tasks", req.Status, fmt.Sprintf("%s %s by %s", req.TaskID, req.Status, req.Worker), nil)
		json.NewEncoder(w).Encode(map[string]string{"status": req.Status, "task_id": req.TaskID})
		// Auto-claim next pending task for this worker -- signed: alpha
		if claimed, taskID := s.autoClaimNextTask(req.Worker); claimed {
			s.bus.Post("skynet", "tasks", "auto_claimed",
				fmt.Sprintf("Auto-claimed %s for %s after completing %s", taskID, req.Worker, req.TaskID), nil)
		}
	} else {
		http.Error(w, "task not found or not claimed by this worker", http.StatusConflict)
	}
}

// autoClaimNextTask finds the highest-priority pending QueuedTask and claims it
// for the given worker. Returns (true, taskID) if a task was claimed, or
// (false, "") if no pending tasks exist. Caller must NOT hold s.tqMu.
// signed: alpha
func (s *SkynetServer) autoClaimNextTask(workerName string) (bool, string) {
	s.tqMu.Lock()
	defer s.tqMu.Unlock()

	bestIdx := -1
	bestPriority := -1
	for i := range s.taskQueue {
		if s.taskQueue[i].Status == "pending" {
			if bestIdx == -1 || s.taskQueue[i].Priority > bestPriority {
				bestIdx = i
				bestPriority = s.taskQueue[i].Priority
			}
		}
	}
	if bestIdx == -1 {
		return false, ""
	}

	now := time.Now()
	s.taskQueue[bestIdx].Status = "claimed"
	s.taskQueue[bestIdx].ClaimedBy = workerName
	s.taskQueue[bestIdx].ClaimedAt = &now
	s.taskQueue[bestIdx].AutoClaimed = true
	return true, s.taskQueue[bestIdx].ID
}

// ─── POST/GET /bus/convene ───────────────────────────────────────

func (s *SkynetServer) handleBusConvene(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	if r.Method == http.MethodGet {
		s.convMu.RLock()
		sessions := make([]ConveneSession, len(s.conveneSessions))
		copy(sessions, s.conveneSessions)
		s.convMu.RUnlock()
		json.NewEncoder(w).Encode(sessions)
		return
	}

	// DELETE /bus/convene?id=SESSION_ID — resolve/close a session
	if r.Method == http.MethodDelete {
		sid := r.URL.Query().Get("id")
		if sid == "" {
			http.Error(w, "missing ?id= parameter", http.StatusBadRequest)
			return
		}
		s.convMu.Lock()
		found := false
		for i := range s.conveneSessions {
			if s.conveneSessions[i].ID == sid {
				s.conveneSessions[i].Status = "resolved"
				resolvedAt := time.Now() // signed: beta
				s.conveneSessions[i].StatusChangedAt = &resolvedAt
				found = true
				break
			}
		}
		s.convMu.Unlock()
		if found {
			json.NewEncoder(w).Encode(map[string]string{"status": "resolved", "session_id": sid})
		} else {
			http.Error(w, "session not found", http.StatusNotFound)
		}
		return
	}

	// PATCH /bus/convene — join a session: {"session_id": "...", "worker": "..."}
	if r.Method == http.MethodPatch {
		var req struct {
			SessionID string `json:"session_id"`
			Worker    string `json:"worker"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		s.convMu.Lock()
		found := false
		for i := range s.conveneSessions {
			if s.conveneSessions[i].ID == req.SessionID {
				// Add participant if not already present
				exists := false
				for _, p := range s.conveneSessions[i].Participants {
					if p == req.Worker {
						exists = true
						break
					}
				}
				if !exists {
					s.conveneSessions[i].Participants = append(s.conveneSessions[i].Participants, req.Worker)
				}
				found = true
				break
			}
		}
		s.convMu.Unlock()
		if found {
			json.NewEncoder(w).Encode(map[string]string{"status": "joined", "session_id": req.SessionID})
		} else {
			http.Error(w, "session not found", http.StatusNotFound)
		}
		return
	}

	if r.Method != http.MethodPost {
		http.Error(w, "GET, POST, PATCH, or DELETE", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Initiator   string `json:"initiator"`
		Topic       string `json:"topic"`
		Context     string `json:"context"`
		NeedWorkers int    `json:"need_workers"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}

	createdNow := time.Now() // signed: beta
	session := ConveneSession{
		ID:              fmt.Sprintf("conv_%d", createdNow.UnixNano()),
		Initiator:       req.Initiator,
		Topic:           req.Topic,
		Context:         req.Context,
		NeedWorkers:     req.NeedWorkers,
		Participants:    []string{req.Initiator},
		Messages:        make([]BusMessage, 0),
		CreatedAt:       createdNow,
		Status:          "active",
		StatusChangedAt: &createdNow,
	}

	s.convMu.Lock()
	s.conveneSessions = append(s.conveneSessions, session)
	if len(s.conveneSessions) > 50 {
		s.conveneSessions = s.conveneSessions[len(s.conveneSessions)-50:]
	}
	s.convMu.Unlock()

	// Also publish to bus so watchers know
	s.bus.Post(req.Initiator, "convene", "request",
		fmt.Sprintf("[%s] %s: %s (need %d workers)", session.ID, req.Topic, req.Context, req.NeedWorkers),
		nil)

	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":     "ok",
		"session_id": session.ID,
	})
}

// ─── Helpers ─────────────────────────────────────────────────────

func (s *SkynetServer) addThought(typ, text string) {
	s.thMu.Lock()
	defer s.thMu.Unlock()
	entry := ThoughtEntry{
		ID:   fmt.Sprintf("th_%d", time.Now().UnixNano()),
		Text: text,
		Type: typ,
		Time: time.Now().Format("15:04:05"),
	}
	s.thoughts = append(s.thoughts, entry)
	if len(s.thoughts) > 200 {
		s.thoughts = s.thoughts[len(s.thoughts)-200:]
	}
}

// appendGodFeed writes a GOD feed entry to god_feed.json for console display.
func (s *SkynetServer) appendGodFeed(feedType, text string) {
	s.godFeedMu.Lock()
	defer s.godFeedMu.Unlock()

	feedPath := `D:\Prospects\ScreenMemory\data\brain\god_feed.json`
	var feed []map[string]interface{}
	if data, err := os.ReadFile(feedPath); err == nil {
		json.Unmarshal(data, &feed)
	}
	feed = append(feed, map[string]interface{}{
		"type": feedType,
		"text": text,
		"time": time.Now().Format("15:04:05"),
		"ts":   float64(time.Now().UnixMilli()) / 1000.0,
	})
	if len(feed) > 200 {
		feed = feed[len(feed)-200:]
	}
	out, _ := json.MarshalIndent(feed, "", "  ")
	os.WriteFile(feedPath, out, 0644)
}

// appendBrainInbox writes a pending directive to brain_inbox.json for Orchestrator polling.
func (s *SkynetServer) appendBrainInbox(directiveID, goal string) {
	s.brainInboxMu.Lock()
	defer s.brainInboxMu.Unlock()

	inboxPath := `D:\Prospects\ScreenMemory\data\brain\brain_inbox.json`
	var inbox []map[string]interface{}
	if data, err := os.ReadFile(inboxPath); err == nil {
		json.Unmarshal(data, &inbox)
	}

	// Dedup: only block if same goal is already pending
	for _, item := range inbox {
		if item["directive"] == goal && item["status"] == "pending" {
			return
		}
	}

	inbox = append(inbox, map[string]interface{}{
		"request_id": directiveID,
		"directive":  goal,
		"status":     "pending",
		"source":     "god_console",
		"timestamp":  float64(time.Now().UnixMilli()) / 1000.0,
	})
	out, _ := json.MarshalIndent(inbox, "", "  ")
	os.WriteFile(inboxPath, out, 0644)
}

// ─── Task Result History ─────────────────────────────────────────

func (s *SkynetServer) storeTaskResult(r *TaskResult) {
	s.trMu.Lock()
	s.taskResults = append(s.taskResults, *r)
	if len(s.taskResults) > 500 {
		s.taskResults = s.taskResults[len(s.taskResults)-500:]
	}
	s.trMu.Unlock()
}

// ─── GET /results ────────────────────────────────────────────────

func (s *SkynetServer) handleResults(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	n := 50
	if v := r.URL.Query().Get("n"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			n = parsed
		}
	}
	s.trMu.RLock()
	total := len(s.taskResults)
	start := total - n
	if start < 0 {
		start = 0
	}
	results := make([]TaskResult, total-start)
	copy(results, s.taskResults[start:])
	s.trMu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(results)
}

// ─── POST /cancel ────────────────────────────────────────────────

func (s *SkynetServer) handleCancel(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		TaskID string `json:"task_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.TaskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return
	}

	s.wtMu.Lock()
	found := false
	for i, t := range s.workerTasks {
		if t.TaskID == req.TaskID && t.Status == "pending" {
			s.workerTasks[i].Status = "cancelled"
			s.workerTasks[i].CompletedAt = time.Now().Format(time.RFC3339)
			found = true
			break
		}
	}
	s.wtMu.Unlock()

	// Also try to remove from worker queues
	for _, wk := range s.workers {
		if wk.RemoveTask(req.TaskID) {
			found = true
			break
		}
	}

	if !found {
		http.Error(w, "task not found or not pending", http.StatusNotFound)
		return
	}

	s.addThought("cancel", fmt.Sprintf("Task %s cancelled", req.TaskID))

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"status":  "ok",
		"task_id": req.TaskID,
		"action":  "cancelled",
	})
}

// ─── Directive Completion ────────────────────────────────────────

func (s *SkynetServer) checkDirectiveCompletion(directiveID string) {
	if directiveID == "" {
		return
	}

	s.wtMu.RLock()
	hasPending := false
	hasAny := false
	for _, t := range s.workerTasks {
		if t.Directive == directiveID {
			hasAny = true
			if t.Status == "pending" {
				hasPending = true
				break
			}
		}
	}
	s.wtMu.RUnlock()

	if hasAny && !hasPending {
		s.dirMu.Lock()
		for i, d := range s.directives {
			if d.ID == directiveID && d.Status == "active" {
				s.directives[i].Status = "completed"
				s.directives[i].CompletedAt = time.Now()
				break
			}
		}
		s.dirMu.Unlock()
		s.addThought("directive", fmt.Sprintf("Directive %s completed — all tasks done", directiveID))
	}
}

// ─── Process Internal Worker Results ─────────────────────────────

func (s *SkynetServer) ProcessResult(r *TaskResult) {
	s.storeTaskResult(r)

	// Update server-level metrics
	if r.Status == "success" {
		atomic.AddInt64(&s.tasksCompleted, 1)
	} else {
		atomic.AddInt64(&s.tasksFailed, 1)
	}

	// Update matching workerTask
	s.wtMu.Lock()
	for i, t := range s.workerTasks {
		if t.TaskID == r.TaskID {
			if r.Status == "success" {
				s.workerTasks[i].Status = "completed"
			} else {
				s.workerTasks[i].Status = r.Status
			}
			s.workerTasks[i].Result = r.Output
			s.workerTasks[i].CompletedAt = r.FinishedAt.Format(time.RFC3339)
			break
		}
	}
	s.wtMu.Unlock()

	if r.DirectiveID != "" {
		s.checkDirectiveCompletion(r.DirectiveID)
	}
}

// ─── Rate Limit Cleanup ─────────────────────────────────────────

func (s *SkynetServer) StartCleanup() {
	go func() {
		ticker := time.NewTicker(60 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			s.rateMu.Lock()
			now := time.Now()
			for ip, last := range s.rateLimit {
				if now.Sub(last) > 10*time.Second {
					delete(s.rateLimit, ip)
				}
			}
			s.rateMu.Unlock()
		}
	}()
}

// ─── POST /orchestrate ──────────────────────────────────────────
// High-level orchestration endpoint. Creates a directive, posts to bus,
// optionally auto-dispatches tasks to workers via round-robin.

func (s *SkynetServer) handleOrchestrate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Prompt       string `json:"prompt"`
		Timeout      int    `json:"timeout"`
		AutoDispatch bool   `json:"auto_dispatch"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON", http.StatusBadRequest)
		return
	}
	if req.Prompt == "" {
		http.Error(w, "prompt required", http.StatusBadRequest)
		return
	}
	if req.Timeout <= 0 {
		req.Timeout = 120
	}

	now := time.Now()
	d := Directive{
		ID:        fmt.Sprintf("dir_%d", now.UnixNano()),
		Goal:      req.Prompt,
		Priority:  1,
		Status:    "active",
		CreatedAt: now,
	}

	s.dirMu.Lock()
	s.directives = append(s.directives, d)
	s.dirMu.Unlock()

	s.addThought("orchestrate", fmt.Sprintf("Orchestrate: %s", truncate(req.Prompt, 80)))
	s.bus.Post("skynet", "orchestrator", "orchestrate", req.Prompt, map[string]string{
		"directive_id": d.ID,
		"timeout":      fmt.Sprintf("%d", req.Timeout),
		"auto_dispatch": fmt.Sprintf("%v", req.AutoDispatch),
	})

	go s.appendGodFeed("orchestrate", req.Prompt)
	go s.appendBrainInbox(d.ID, req.Prompt)

	var assigned []string

	if req.AutoDispatch {
		for _, wk := range s.workers {
			taskID := fmt.Sprintf("task_%d_%s", now.UnixNano(), wk.Name)
			task := &Task{
				ID:           taskID,
				Type:         "copilot",
				Command:      req.Prompt,
				Description:  req.Prompt,
				Priority:     1,
				DirectiveID:  d.ID,
				DispatchedAt: now,
				MaxRetries:   1,
			}
			wk.Enqueue(task)
			atomic.AddInt64(&s.tasksDispatched, 1)

			s.dirMu.Lock()
			for i := range s.directives {
				if s.directives[i].ID == d.ID {
					s.directives[i].SubTasks = append(s.directives[i].SubTasks, taskID)
					break
				}
			}
			s.dirMu.Unlock()

			s.wtMu.Lock()
			s.workerTasks = append(s.workerTasks, WorkerTask{
				TaskID:     taskID,
				Worker:     wk.Name,
				Directive:  d.ID,
				Status:     "pending",
				AssignedAt: now.Format(time.RFC3339),
			})
			s.wtMu.Unlock()

			assigned = append(assigned, wk.Name)
		}
		s.addThought("orchestrate", fmt.Sprintf("Auto-dispatched to %d workers: %v", len(assigned), assigned))
	} else {
		// No auto-dispatch: pick one worker via round-robin
		if len(s.workers) == 0 {
			http.Error(w, "no workers available", http.StatusServiceUnavailable)
			return
		}
		idx := int(atomic.AddInt64(&s.rrCounter, 1)-1) % len(s.workers)
		wk := s.workers[idx]
		taskID := fmt.Sprintf("task_%d_%s", now.UnixNano(), wk.Name)
		task := &Task{
			ID:           taskID,
			Type:         "copilot",
			Command:      req.Prompt,
			Description:  req.Prompt,
			Priority:     1,
			DirectiveID:  d.ID,
			DispatchedAt: now,
			MaxRetries:   1,
		}
		wk.Enqueue(task)
		atomic.AddInt64(&s.tasksDispatched, 1)

		s.dirMu.Lock()
		for i := range s.directives {
			if s.directives[i].ID == d.ID {
				s.directives[i].SubTasks = append(s.directives[i].SubTasks, taskID)
				break
			}
		}
		s.dirMu.Unlock()

		s.wtMu.Lock()
		s.workerTasks = append(s.workerTasks, WorkerTask{
			TaskID:     taskID,
			Worker:     wk.Name,
			Directive:  d.ID,
			Status:     "pending",
			AssignedAt: now.Format(time.RFC3339),
		})
		s.wtMu.Unlock()

		assigned = append(assigned, wk.Name)
		s.addThought("orchestrate", fmt.Sprintf("Dispatched to %s (round-robin)", wk.Name))
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":           "ok",
		"directive_id":     d.ID,
		"prompt":           req.Prompt,
		"workers_assigned": assigned,
	})
}

// ─── GET /orchestrate/status ────────────────────────────────────
// Returns directive status with sub-task completion details.

func (s *SkynetServer) handleOrchestrateStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	dirID := r.URL.Query().Get("directive_id")
	if dirID == "" {
		http.Error(w, "directive_id query param required", http.StatusBadRequest)
		return
	}

	// Find directive
	s.dirMu.RLock()
	var found *Directive
	for i := range s.directives {
		if s.directives[i].ID == dirID {
			d := s.directives[i]
			found = &d
			break
		}
	}
	s.dirMu.RUnlock()

	if found == nil {
		http.Error(w, "directive not found", http.StatusNotFound)
		return
	}

	// Collect sub-task statuses
	s.wtMu.RLock()
	var subtasks []map[string]string
	for _, t := range s.workerTasks {
		if t.Directive == dirID {
			st := map[string]string{
				"task_id":      t.TaskID,
				"worker":       t.Worker,
				"status":       t.Status,
				"assigned_at":  t.AssignedAt,
				"completed_at": t.CompletedAt,
			}
			if t.Result != "" {
				st["result"] = truncate(t.Result, 500)
			}
			subtasks = append(subtasks, st)
		}
	}
	s.wtMu.RUnlock()

	if subtasks == nil {
		subtasks = []map[string]string{}
	}

	pending := 0
	completed := 0
	failed := 0
	for _, st := range subtasks {
		switch st["status"] {
		case "pending":
			pending++
		case "completed":
			completed++
		default:
			if st["status"] != "" && st["status"] != "pending" && st["status"] != "completed" {
				failed++
			}
		}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"directive_id": found.ID,
		"goal":         found.Goal,
		"status":       found.Status,
		"priority":     found.Priority,
		"created_at":   found.CreatedAt.Format(time.RFC3339),
		"completed_at": found.CompletedAt.Format(time.RFC3339),
		"subtasks":     subtasks,
		"summary": map[string]int{
			"total":     len(subtasks),
			"pending":   pending,
			"completed": completed,
			"failed":    failed,
		},
	})
}

// ─── POST /orchestrate/pipeline ─────────────────────────────────
// Accepts a multi-step pipeline of sequential prompts, creates a directive
// per step with dependency chaining, and dispatches step 1 immediately.
// Body: { "steps": [{"name": "step1", "prompt": "do X"}, {"name": "step2", "prompt": "do Y"}] }
// Returns: { "pipeline_id": "pipe_...", "status": "running", "steps": [...] }

func (s *SkynetServer) handleOrchestratePipeline(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Steps []struct {
			Name   string `json:"name"`
			Prompt string `json:"prompt"`
		} `json:"steps"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad JSON: "+err.Error(), http.StatusBadRequest)
		return
	}
	if len(req.Steps) == 0 {
		http.Error(w, "steps array required and must be non-empty", http.StatusBadRequest)
		return
	}

	now := time.Now()
	pipelineID := fmt.Sprintf("pipe_%d", now.UnixNano())

	type stepInfo struct {
		Name        string `json:"name"`
		Prompt      string `json:"prompt"`
		DirectiveID string `json:"directive_id"`
		TaskID      string `json:"task_id"`
		Worker      string `json:"worker"`
		Status      string `json:"status"`
		DependsOn   string `json:"depends_on,omitempty"`
	}
	stepsOut := make([]stepInfo, 0, len(req.Steps))

	var prevDirID string
	for i, step := range req.Steps {
		if step.Prompt == "" {
			http.Error(w, fmt.Sprintf("step %d: prompt required", i), http.StatusBadRequest)
			return
		}
		name := step.Name
		if name == "" {
			name = fmt.Sprintf("step_%d", i+1)
		}

		dirID := fmt.Sprintf("%s_dir_%d", pipelineID, i)
		d := Directive{
			ID:        dirID,
			Goal:      fmt.Sprintf("[%s] %s", name, step.Prompt),
			Priority:  1,
			Status:    "pending",
			CreatedAt: now,
			Route:     pipelineID,
		}
		if prevDirID != "" {
			d.SubTasks = []string{prevDirID} // depends on previous
		}

		s.dirMu.Lock()
		s.directives = append(s.directives, d)
		s.dirMu.Unlock()

		// Pick worker via round-robin
		if len(s.workers) == 0 {
			http.Error(w, "no workers available", http.StatusServiceUnavailable)
			return
		}
		idx := int(atomic.AddInt64(&s.rrCounter, 1)-1) % len(s.workers)
		wk := s.workers[idx]
		taskID := fmt.Sprintf("%s_task_%d_%s", pipelineID, i, wk.Name)

		si := stepInfo{
			Name:        name,
			Prompt:      truncate(step.Prompt, 200),
			DirectiveID: dirID,
			TaskID:      taskID,
			Worker:      wk.Name,
			DependsOn:   prevDirID,
		}

		if i == 0 {
			// Dispatch first step immediately
			task := &Task{
				ID:           taskID,
				Type:         "copilot",
				Command:      step.Prompt,
				Description:  fmt.Sprintf("[pipeline:%s] %s", name, step.Prompt),
				Priority:     1,
				DirectiveID:  dirID,
				DispatchedAt: now,
				MaxRetries:   1,
				Metadata:     map[string]string{"pipeline_id": pipelineID, "step": fmt.Sprintf("%d", i)},
			}
			wk.Enqueue(task)
			atomic.AddInt64(&s.tasksDispatched, 1)

			si.Status = "dispatched"
			d.Status = "active"
			s.dirMu.Lock()
			for j := range s.directives {
				if s.directives[j].ID == dirID {
					s.directives[j].Status = "active"
					s.directives[j].SubTasks = append(s.directives[j].SubTasks, taskID)
					break
				}
			}
			s.dirMu.Unlock()
		} else {
			si.Status = "pending"
		}

		s.wtMu.Lock()
		s.workerTasks = append(s.workerTasks, WorkerTask{
			TaskID:     taskID,
			Worker:     wk.Name,
			Directive:  dirID,
			Status:     si.Status,
			AssignedAt: now.Format(time.RFC3339),
		})
		s.wtMu.Unlock()

		stepsOut = append(stepsOut, si)
		prevDirID = dirID
	}

	s.addThought("pipeline", fmt.Sprintf("Pipeline %s created with %d steps", pipelineID, len(req.Steps)))
	s.bus.Post("skynet", "orchestrator", "pipeline", fmt.Sprintf("Pipeline %s: %d steps", pipelineID, len(req.Steps)), map[string]string{
		"pipeline_id": pipelineID,
		"steps":       fmt.Sprintf("%d", len(req.Steps)),
	})

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"pipeline_id": pipelineID,
		"status":      "running",
		"total_steps": len(req.Steps),
		"steps":       stepsOut,
	})
}

// ─── Spam Filter ─────────────────────────────────────────────────
// Server-side deduplication and rate limiting for bus messages.
// signed: delta

// SpamFilter tracks recent message fingerprints and per-sender rates.
type SpamFilter struct {
	mu           sync.Mutex
	fingerprints map[string]time.Time // fingerprint → last seen time
	senderCounts map[string][]time.Time // sender → list of recent post timestamps
}

// NewSpamFilter creates a SpamFilter and starts a background cleanup goroutine.
func NewSpamFilter() *SpamFilter {
	sf := &SpamFilter{
		fingerprints: make(map[string]time.Time),
		senderCounts: make(map[string][]time.Time),
	}
	go sf.cleanupLoop()
	return sf
}

// cleanupLoop removes stale fingerprints and rate entries every 5 minutes.
func (sf *SpamFilter) cleanupLoop() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		sf.mu.Lock()
		now := time.Now()
		for k, t := range sf.fingerprints {
			if now.Sub(t) > 60*time.Second {
				delete(sf.fingerprints, k)
			}
		}
		for sender, times := range sf.senderCounts {
			var recent []time.Time
			for _, t := range times {
				if now.Sub(t) < time.Minute {
					recent = append(recent, t)
				}
			}
			if len(recent) == 0 {
				delete(sf.senderCounts, sender)
			} else {
				sf.senderCounts[sender] = recent
			}
		}
		sf.mu.Unlock()
	}
}
// signed: delta

// Check returns "" if the message is allowed, or a reason string if blocked.
func (sf *SpamFilter) Check(sender, topic, msgType, content string) string {
	sf.mu.Lock()
	defer sf.mu.Unlock()

	now := time.Now()

	// --- Rate limit: max 10 messages per minute per sender ---
	times := sf.senderCounts[sender]
	var recent []time.Time
	for _, t := range times {
		if now.Sub(t) < time.Minute {
			recent = append(recent, t)
		}
	}
	if len(recent) >= 10 {
		sf.senderCounts[sender] = recent
		return fmt.Sprintf("rate limit exceeded: %d msgs in last 60s from %s", len(recent), sender)
	}
	recent = append(recent, now)
	sf.senderCounts[sender] = recent

	// --- Dedup: same sender+topic+type within 60s with similar content ---
	// Fingerprint uses first 200 chars of content to detect near-duplicates
	contentSnip := content
	if len(contentSnip) > 200 {
		contentSnip = contentSnip[:200]
	}
	fp := fmt.Sprintf("%s|%s|%s|%s", sender, topic, msgType, contentSnip)
	if lastSeen, exists := sf.fingerprints[fp]; exists {
		if now.Sub(lastSeen) < 60*time.Second {
			return fmt.Sprintf("duplicate message from %s (topic=%s type=%s) within 60s", sender, topic, msgType)
		}
	}
	sf.fingerprints[fp] = now

	return ""
}
// signed: delta

// ─── POST /bus/publish ───────────────────────────────────────────
// Workers and orchestrator publish messages to the bus.
// Body: { "sender": "alpha", "topic": "results", "type": "report", "content": "...", "metadata": {...} }

func (s *SkynetServer) handleBusPublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}

	var msg struct {
		Sender   string            `json:"sender"`
		Topic    string            `json:"topic"`
		Type     string            `json:"type"`
		Content  string            `json:"content"`
		Metadata map[string]string `json:"metadata,omitempty"`
	}
	if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
		http.Error(w, "Bad JSON: "+err.Error(), http.StatusBadRequest)
		return
	}
	if msg.Sender == "" || msg.Content == "" {
		http.Error(w, "sender and content required", http.StatusBadRequest)
		return
	}
	if msg.Topic == "" {
		msg.Topic = "general"
	}
	if msg.Type == "" {
		msg.Type = "message"
	}

	// Spam filter: dedup + rate limit -- signed: delta
	if reason := s.spamFilter.Check(msg.Sender, msg.Topic, msg.Type, msg.Content); reason != "" {
		fmt.Printf("[SPAM_BLOCKED] sender=%s topic=%s type=%s reason=%s\n",
			msg.Sender, msg.Topic, msg.Type, reason)
		http.Error(w, "SPAM_BLOCKED: "+reason, http.StatusTooManyRequests)
		return
	}

	s.bus.Post(msg.Sender, msg.Topic, msg.Type, msg.Content, msg.Metadata)

	// Task lifecycle: when a worker posts type=result, complete matching tracker -- signed: gamma
	if msg.Type == "result" && msg.Sender != "" {
		now := time.Now()
		s.ttMu.Lock()
		for i := len(s.taskTrackers) - 1; i >= 0; i-- {
			tt := &s.taskTrackers[i]
			if strings.EqualFold(tt.Worker, msg.Sender) && tt.Status == "dispatched" {
				tt.Status = "completed"
				tt.CompletedAt = &now
				tt.DurationMs = float64(now.Sub(tt.DispatchedAt).Milliseconds())
				break // match most recent dispatched task for this worker
			}
		}
		s.ttMu.Unlock()
	}

	// Broadcast to WebSocket clients — P1.07: pooled buffer — signed: alpha
	wsBuf, err := marshalPooled(map[string]interface{}{
		"type": "bus_message", "sender": msg.Sender, "topic": msg.Topic,
		"msg_type": msg.Type, "content": msg.Content,
		"timestamp": time.Now().Format(time.RFC3339), // signed: beta
	})
	if err == nil {
		s.broadcastWS(wsBuf.Bytes())
		putBuffer(wsBuf)
	}

	writeJSON(w, map[string]interface{}{ // P1.07: pooled buffer — signed: alpha
		"status":    "published",
		"sender":    msg.Sender,
		"topic":     msg.Topic,
		"bus_depth": s.bus.Depth(),
	})
}

// ─── GET /bus/messages ───────────────────────────────────────────
// Returns recent bus messages. Optional: ?limit=N (default 20), ?sender=X, ?topic=X

func (s *SkynetServer) handleBusMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	limit := 20
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := fmt.Sscanf(l, "%d", &limit); n == 1 && err == nil {
			if limit > 100 {
				limit = 100
			}
		}
	}

	msgs := s.bus.Recent(limit)

	// Optional filters
	sender := r.URL.Query().Get("sender")
	topic := r.URL.Query().Get("topic")

	if sender != "" || topic != "" {
		filtered := make([]BusMessage, 0, len(msgs))
		for _, m := range msgs {
			if sender != "" && m.Sender != sender {
				continue
			}
			if topic != "" && m.Topic != topic {
				continue
			}
			filtered = append(filtered, m)
		}
		msgs = filtered
	}

	writeJSON(w, msgs) // P1.07: pooled buffer — signed: alpha
}

// ─── Embedded Dashboard ──────────────────────────────────────────

// ─── GET /tasks — Task Lifecycle Tracker ─────────────────────────
// Returns tracked task lifecycle entries. Optional: ?worker=NAME to filter.
// signed: gamma

func (s *SkynetServer) handleTasks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	workerFilter := r.URL.Query().Get("worker")
	limitStr := r.URL.Query().Get("limit")
	limit := 100
	if limitStr != "" {
		if n, err := strconv.Atoi(limitStr); err == nil && n > 0 {
			if n > 500 {
				n = 500
			}
			limit = n
		}
	}

	s.ttMu.RLock()
	src := s.taskTrackers
	s.ttMu.RUnlock()

	// Filter by worker if requested
	var filtered []TaskTracker
	if workerFilter != "" {
		for _, tt := range src {
			if strings.EqualFold(tt.Worker, workerFilter) {
				filtered = append(filtered, tt)
			}
		}
	} else {
		filtered = src
	}

	// Return last N entries (most recent)
	if len(filtered) > limit {
		filtered = filtered[len(filtered)-limit:]
	}

	// Build summary stats
	stats := map[string]int{
		"dispatched": 0, "completed": 0, "failed": 0, "timeout": 0, "processing": 0,
	}
	for _, tt := range filtered {
		stats[tt.Status]++
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"tasks": filtered,
		"total": len(filtered),
		"stats": stats,
	})
}

// ─── Rate Limiting Middleware ─────────────────────────────────────

func (s *SkynetServer) rateLimitMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ip := r.RemoteAddr
		if idx := strings.LastIndex(ip, ":"); idx != -1 {
			ip = ip[:idx]
		}

		// Exempt localhost from rate limiting
		if ip == "127.0.0.1" || ip == "[::1]" || ip == "localhost" {
			next.ServeHTTP(w, r)
			return
		}

		s.rateMu.Lock()
		last, exists := s.rateLimit[ip]
		now := time.Now()
		if exists && now.Sub(last) < 500*time.Microsecond {
			s.rateMu.Unlock()
			http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
			return
		}
		s.rateLimit[ip] = now
		s.rateMu.Unlock()

		next.ServeHTTP(w, r)
	})
}

// ─── GET /stream (SSE) ───────────────────────────────────────────

func (s *SkynetServer) handleSSEStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			agents := make(map[string]*AgentView)
			for _, wk := range s.workers {
				agents[wk.Name] = wk.GetState()
			}

			s.thMu.RLock()
			thoughts := make([]ThoughtEntry, len(s.thoughts))
			copy(thoughts, s.thoughts)
			s.thMu.RUnlock()

			payload := map[string]interface{}{
				"uptime_s":         time.Since(s.startTime).Seconds(),
				"bus_depth":        s.bus.Depth(),
				"bus_dropped":      s.bus.Dropped(),
				"agents":           agents,
				"bus":              s.bus.Recent(10),
				"orch_thinking":    thoughts,
				"tasks_dispatched": atomic.LoadInt64(&s.tasksDispatched),
				"tasks_completed":  atomic.LoadInt64(&s.tasksCompleted),
				"tasks_failed":     atomic.LoadInt64(&s.tasksFailed),
				"goroutines":       runtime.NumGoroutine(),
				"timestamp":        time.Now().UnixNano(),
			}
			// P1.07: pooled buffer for 1Hz SSE — highest-frequency alloc path — signed: alpha
			buf := getBuffer()
			if err := json.NewEncoder(buf).Encode(payload); err == nil {
				fmt.Fprintf(w, "data: %s\n", buf.Bytes())
			}
			putBuffer(buf)
			flusher.Flush()
		}
	}
}

// ─── Security Audit ──────────────────────────────────────────────

func (s *SkynetServer) logSecurityEvent(source, eventType, details string, blocked bool) {
	event := SecurityEvent{
		Timestamp: time.Now().Format(time.RFC3339),
		Source:    source,
		Type:     eventType,
		Details:   details,
		Blocked:  blocked,
	}
	s.secMu.Lock()
	s.securityLog = append(s.securityLog, event)
	if len(s.securityLog) > 500 {
		s.securityLog = s.securityLog[len(s.securityLog)-500:]
	}
	s.secMu.Unlock()

	// Broadcast to WebSocket clients — P1.07: pooled buffer — signed: alpha
	if blocked {
		secBuf, err := marshalPooled(map[string]interface{}{
			"type": "security_alert", "event": event,
			"timestamp": time.Now().Format(time.RFC3339), // signed: beta
		})
		if err == nil {
			s.broadcastWS(secBuf.Bytes())
			putBuffer(secBuf)
		}
	}
}

func (s *SkynetServer) handleSecurityAudit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	s.secMu.RLock()
	events := make([]SecurityEvent, len(s.securityLog))
	copy(events, s.securityLog)
	s.secMu.RUnlock()

	blocked := 0
	for _, e := range events {
		if e.Blocked {
			blocked++
		}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"total_events":  len(events),
		"blocked_count": blocked,
		"events":        events,
		"uptime_s":      time.Since(s.startTime).Seconds(),
	})
}

func (s *SkynetServer) handleSecurityBlocked(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Source  string `json:"source"`
		Reason string `json:"reason"`
		Text   string `json:"text"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	s.logSecurityEvent(req.Source, "identity_injection", req.Reason+": "+req.Text[:min(200, len(req.Text))], true)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "logged"})
}

// ─── WebSocket (lightweight, no external deps) ── P2: Security Hardened ──
// CSWSH protection, RBAC auth, frame limits, connection caps, ping/pong.
// signed: delta

const (
	wsMaxConnections = 50              // hard cap on concurrent WS clients
	wsMaxFrameSize   = 1 << 20        // 1 MB max inbound frame
	wsPingInterval   = 30 * time.Second
	wsIdleTimeout    = 5 * time.Minute // close connections idle longer than this
	wsWriteTimeout   = 10 * time.Second
	wsReadTimeout    = 60 * time.Second
)

// wsAllowedOrigin returns true if the Origin header is acceptable.
// Empty origin (non-browser clients), localhost variants, and null (local file) are allowed.
func wsAllowedOrigin(origin string) bool {
	if origin == "" || origin == "null" {
		return true
	}
	lower := strings.ToLower(origin)
	for _, prefix := range []string{
		"http://localhost", "https://localhost",
		"http://127.0.0.1", "https://127.0.0.1",
		"http://[::1]", "https://[::1]",
	} {
		if strings.HasPrefix(lower, prefix) {
			return true
		}
	}
	return false
}

func (s *SkynetServer) handleWebSocket(w http.ResponseWriter, r *http.Request) {
	// ── P2.1: Origin validation (CSWSH protection) ──────────────────
	origin := r.Header.Get("Origin")
	if !wsAllowedOrigin(origin) {
		atomic.AddInt64(&s.wsRejected, 1)
		s.logSecurityEvent(origin, "ws_cswsh_blocked",
			fmt.Sprintf("WebSocket upgrade rejected: disallowed Origin %q from %s", origin, r.RemoteAddr), true)
		http.Error(w, `{"error":"origin not allowed"}`, http.StatusForbidden)
		return
	}

	// ── P2.2: RBAC authentication ───────────────────────────────────
	// Note: roleFromHeader defaults to RoleOrchestrator when no header is present
	// (backward-compat for existing Python tooling). The ACL entry for /ws
	// restricts access to orchestrator + worker roles.
	role := roleFromHeader(r)
	if role == "" {
		atomic.AddInt64(&s.wsRejected, 1)
		s.logSecurityEvent(r.RemoteAddr, "ws_auth_rejected",
			"WebSocket upgrade rejected: unknown role", true)
		http.Error(w, `{"error":"RBAC: set X-Agent-Role header"}`, http.StatusForbidden)
		return
	}

	// ── P2.3: Connection count limit ────────────────────────────────
	current := atomic.LoadInt64(&s.wsConns)
	if current >= wsMaxConnections {
		atomic.AddInt64(&s.wsRejected, 1)
		s.logSecurityEvent(r.RemoteAddr, "ws_limit_reached",
			fmt.Sprintf("WebSocket upgrade rejected: %d/%d connections", current, wsMaxConnections), true)
		http.Error(w, `{"error":"too many websocket connections"}`, http.StatusServiceUnavailable)
		return
	}

	// ── Upgrade HTTP to WebSocket using raw hijack ──────────────────
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "websocket not supported", http.StatusInternalServerError)
		return
	}

	conn, bufrw, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	// WebSocket handshake
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" {
		conn.Close()
		return
	}
	accept := computeWebSocketAccept(key)

	bufrw.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
	bufrw.WriteString("Upgrade: websocket\r\n")
	bufrw.WriteString("Connection: Upgrade\r\n")
	bufrw.WriteString("Sec-WebSocket-Accept: " + accept + "\r\n")
	bufrw.WriteString("\r\n")
	bufrw.Flush()

	// Track connection
	atomic.AddInt64(&s.wsConns, 1)

	// Register client channel
	ch := make(chan []byte, 64)
	s.wsMu.Lock()
	s.wsClients[ch] = true
	s.wsMu.Unlock()

	// cleanup removes the client from the map and decrements the counter
	cleanup := sync.OnceFunc(func() {
		s.wsMu.Lock()
		delete(s.wsClients, ch)
		s.wsMu.Unlock()
		atomic.AddInt64(&s.wsConns, -1)
		conn.Close()
	})

	// ── Writer goroutine with write deadline + ping keepalive ───────
	go func() {
		defer cleanup()
		pingTicker := time.NewTicker(wsPingInterval)
		defer pingTicker.Stop()

		for {
			select {
			case msg, ok := <-ch:
				if !ok {
					return // channel closed
				}
				conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
				frame := makeWSFrame(msg)
				if _, err := conn.Write(frame); err != nil {
					return
				}
			case <-pingTicker.C:
				// Send WebSocket ping frame (opcode 0x89)
				conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
				ping := []byte{0x89, 0x00} // FIN + ping opcode, zero payload
				if _, err := conn.Write(ping); err != nil {
					return
				}
			}
		}
	}()

	// ── Reader goroutine with frame validation + idle timeout ───────
	buf := make([]byte, 4096)
	lastActivity := time.Now()
	for {
		deadline := wsReadTimeout
		idleRemaining := time.Until(lastActivity.Add(wsIdleTimeout))
		if idleRemaining < deadline {
			deadline = idleRemaining
		}
		if deadline <= 0 {
			// Idle timeout exceeded
			close(ch)
			return
		}
		conn.SetReadDeadline(time.Now().Add(deadline))
		n, err := conn.Read(buf)
		if err != nil {
			close(ch)
			return
		}
		if n < 2 {
			continue
		}
		lastActivity = time.Now()

		// ── P2.3: Frame validation ──────────────────────────────────
		opcode := buf[0] & 0x0F
		masked := (buf[1] & 0x80) != 0
		payloadLen := int(buf[1] & 0x7F)

		// Clients MUST mask frames per RFC 6455 §5.1
		if !masked {
			close(ch)
			return
		}

		// Calculate actual payload length for size check
		actualLen := payloadLen
		if payloadLen == 126 && n >= 4 {
			actualLen = int(buf[2])<<8 | int(buf[3])
		} else if payloadLen == 127 && n >= 10 {
			actualLen = 0
			for i := 0; i < 8; i++ {
				actualLen = actualLen<<8 | int(buf[2+i])
			}
		}

		// Reject oversized frames
		if actualLen > wsMaxFrameSize {
			s.logSecurityEvent(r.RemoteAddr, "ws_frame_too_large",
				fmt.Sprintf("WebSocket frame rejected: %d bytes > %d max", actualLen, wsMaxFrameSize), true)
			close(ch)
			return
		}

		// Handle control frames
		switch opcode {
		case 0x08: // Close frame
			close(ch)
			return
		case 0x09: // Ping — respond with pong
			conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
			pong := []byte{0x8A, 0x00} // FIN + pong opcode
			conn.Write(pong)
		case 0x0A: // Pong — acknowledged, update activity
			// lastActivity already updated above
		}
	}
}

// ─── GET /ws/stats ────────────────────────────────────────────────

func (s *SkynetServer) handleWSStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	s.wsMu.RLock()
	clients := len(s.wsClients)
	s.wsMu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"connected_clients":  clients,
		"max_connections":    wsMaxConnections,
		"total_broadcasts":   atomic.LoadInt64(&s.wsBroadcasts),
		"total_rejected":     atomic.LoadInt64(&s.wsRejected),
		"active_connections": atomic.LoadInt64(&s.wsConns),
	})
}

func (s *SkynetServer) broadcastWS(msg []byte) {
	s.wsMu.RLock()
	defer s.wsMu.RUnlock()
	for ch := range s.wsClients {
		select {
		case ch <- msg:
		default: // drop if channel full
		}
	}
	atomic.AddInt64(&s.wsBroadcasts, 1)
}

func makeWSFrame(payload []byte) []byte {
	n := len(payload)
	var frame []byte
	if n < 126 {
		frame = make([]byte, 2+n)
		frame[0] = 0x81 // FIN + text opcode
		frame[1] = byte(n)
		copy(frame[2:], payload)
	} else if n < 65536 {
		frame = make([]byte, 4+n)
		frame[0] = 0x81
		frame[1] = 126
		frame[2] = byte(n >> 8)
		frame[3] = byte(n)
		copy(frame[4:], payload)
	} else {
		frame = make([]byte, 10+n)
		frame[0] = 0x81
		frame[1] = 127
		for i := 0; i < 8; i++ {
			frame[9-i] = byte(n >> (8 * i))
		}
		copy(frame[10:], payload)
	}
	return frame
}

func computeWebSocketAccept(key string) string {
	// WebSocket magic string per RFC 6455
	magic := "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	h := sha1.New()
	h.Write([]byte(key + magic))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

var skynetDashboardHTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Skynet v2 Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#00ff41;font-family:'Courier New',monospace;font-size:14px;padding:20px}
h1{text-align:center;font-size:22px;margin-bottom:16px;color:#00ff41;text-shadow:0 0 10px #00ff41}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-bottom:16px}
.card{background:#111;border:1px solid #00ff4133;border-radius:6px;padding:12px}
.card h2{font-size:14px;color:#0af;margin-bottom:8px}
.status{font-size:12px;color:#888;line-height:1.6}
.ok{color:#0f0}.err{color:#f44}.warn{color:#fa0}
pre{background:#000;padding:8px;border-radius:4px;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap}
#uptime{text-align:center;color:#666;margin-bottom:12px}
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:16px}
.metric{background:#111;border:1px solid #00ff4122;border-radius:4px;padding:10px;text-align:center}
.metric .val{font-size:20px;color:#0f0;font-weight:bold}
.metric .lbl{font-size:10px;color:#666;margin-top:4px}
.circuit-open{color:#f44;font-weight:bold}
.circuit-half{color:#fa0;font-weight:bold}
.circuit-closed{color:#0f0}
</style>
</head>
<body>
<h1>&#x26A1; SKYNET v2</h1>
<div id="uptime">connecting...</div>
<div class="metrics-grid" id="metrics"></div>
<div class="grid" id="agents"></div>
<div class="card"><h2>Bus Messages</h2><pre id="bus">loading...</pre></div>
<div class="card"><h2>Orchestrator Thoughts</h2><pre id="thoughts">loading...</pre></div>
<script>
const es=new EventSource('/stream');
es.onmessage=function(e){
 try{
  const d=JSON.parse(e.data);
  document.getElementById('uptime').textContent='Uptime: '+d.uptime_s.toFixed(1)+'s | SSE Connected';
  let m='';
  m+='<div class="metric"><div class="val">'+(d.tasks_dispatched||0)+'</div><div class="lbl">Dispatched</div></div>';
  m+='<div class="metric"><div class="val">'+(d.tasks_completed||0)+'</div><div class="lbl">Completed</div></div>';
  m+='<div class="metric"><div class="val">'+(d.tasks_failed||0)+'</div><div class="lbl">Failed</div></div>';
  m+='<div class="metric"><div class="val">'+(d.goroutines||0)+'</div><div class="lbl">Goroutines</div></div>';
  m+='<div class="metric"><div class="val">'+(d.bus_depth||0)+'</div><div class="lbl">Bus Depth</div></div>';
  document.getElementById('metrics').innerHTML=m;
  let h='';
  for(const[name,a]of Object.entries(d.agents||{})){
   let cc='circuit-closed',ct='CLOSED';
   if(a.circuit_state==='CIRCUIT_OPEN'){cc='circuit-open';ct='OPEN';}
   else if(a.circuit_state==='HALF_OPEN'){cc='circuit-half';ct='HALF_OPEN';}
   h+='<div class="card"><h2>'+name.toUpperCase()+'</h2><div class="status">';
   h+='Status: <span class="'+(a.status==="IDLE"?"ok":"err")+'">'+a.status+'</span><br>';
   h+='Circuit: <span class="'+cc+'">'+ct+'</span>';
   if(a.consecutive_fails>0)h+=' (fails: '+a.consecutive_fails+')';
   h+='<br>';
   h+='Tasks: '+a.tasks_completed+' | Errors: '+a.total_errors+'<br>';
   h+='Queue: '+a.queue_depth+' | Avg: '+a.avg_task_ms.toFixed(1)+'ms';
   h+='</div></div>';
  }
  document.getElementById('agents').innerHTML=h;
  const bus=(d.bus||[]).slice(-10).reverse().map(m=>m.sender+' ['+m.topic+'] '+m.content).join('\n');
  document.getElementById('bus').textContent=bus||'(empty)';
  const th=(d.orch_thinking||[]).slice(-10).reverse().map(t=>'['+t.time+'] '+t.text).join('\n');
  document.getElementById('thoughts').textContent=th||'(none)';
 }catch(err){console.error('SSE parse error:',err)}
};
es.onerror=function(){document.getElementById('uptime').textContent='SSE disconnected - reconnecting...'};
</script>
</body>
</html>`
