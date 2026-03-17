package main

import "time"

// ─── Risk Classification ─────────────────────────────────────────

type RiskLevel int

const (
	RiskLow RiskLevel = iota
	RiskMedium
	RiskHigh
	RiskCritical
)

func (r RiskLevel) String() string {
	return [...]string{"LOW", "MEDIUM", "HIGH", "CRITICAL"}[r]
}

// ─── Task System ─────────────────────────────────────────────────

type Task struct {
	ID              string            `json:"task_id"`
	Type            string            `json:"type"`
	Command         string            `json:"command"`
	Description     string            `json:"description"`
	Priority        int               `json:"priority"`
	Phase           string            `json:"phase"`
	DependsOn       []string          `json:"depends_on,omitempty"`
	DirectiveID     string            `json:"directive_id,omitempty"`
	OriginAgent     string            `json:"origin_agent,omitempty"`
	DispatchedAt    time.Time         `json:"dispatched_at"`
	RetryCount      int               `json:"retry_count"`
	MaxRetries      int               `json:"max_retries"`
	RetryAt         *time.Time        `json:"retry_at,omitempty"`      // timestamp of last retry event -- signed: beta
	EstimatedWeight int               `json:"estimated_weight"`        // scheduling weight (higher = heavier task) -- signed: beta
	Metadata        map[string]string `json:"metadata,omitempty"`
}

// TaskWeight returns the scheduling weight for a task based on its type.
// Weight approximates relative execution cost: shell=1, message=1, python=10,
// powershell=10, copilot=50. Used by weighted load balancer to prevent convoy
// effects where heavy tasks (copilot 120s) are treated identically to fast
// tasks (shell 50ms). -- signed: beta
func TaskWeight(taskType string) int {
	switch taskType {
	case "shell":
		return 1
	case "message":
		return 1
	case "python":
		return 10
	case "powershell":
		return 10
	case "copilot":
		return 50
	default:
		return 10
	}
}

type TaskResult struct {
	TaskID      string    `json:"task_id"`
	Status      string    `json:"status"`
	Output      string    `json:"output"`
	Error       string    `json:"error,omitempty"`
	Description string    `json:"description"`
	DurationMs  float64   `json:"duration_ms"`
	ReturnCode  int       `json:"returncode"`
	WorkerName  string    `json:"worker_name"`
	DirectiveID string    `json:"directive_id,omitempty"`
	StartedAt   time.Time `json:"started_at"`
	FinishedAt  time.Time `json:"finished_at"`
	OutputLines int       `json:"output_lines"`
}

// ─── Message Bus ─────────────────────────────────────────────────

type BusMessage struct {
	ID        string            `json:"id"`
	Sender    string            `json:"sender"`
	Topic     string            `json:"topic"`
	Type      string            `json:"type"`
	Content   string            `json:"content"`
	Metadata  map[string]string `json:"metadata,omitempty"`
	Timestamp time.Time         `json:"timestamp"`
}

// ─── Directives ──────────────────────────────────────────────────

type Directive struct {
	ID          string    `json:"id"`
	Goal        string    `json:"goal"`
	Priority    int       `json:"priority"`
	Status      string    `json:"status"`
	CreatedAt   time.Time `json:"created_at"`
	CompletedAt time.Time `json:"completed_at,omitempty"`
	SubTasks    []string  `json:"sub_tasks,omitempty"`
	Route       string    `json:"route,omitempty"`
}

// ─── Orchestrator Thoughts ───────────────────────────────────────

type ThoughtEntry struct {
	ID   string `json:"id"`
	Text string `json:"text"`
	Type string `json:"type"`
	Time string `json:"time"`
}

// ─── Circuit Breaker ─────────────────────────────────────────────

// CircuitBreaker tracks per-worker failure state.  When a worker accumulates
// FailThreshold consecutive failures its circuit opens, blocking new tasks for
// CooldownSec seconds before transitioning to HALF_OPEN.  A single success in
// HALF_OPEN closes the circuit.
// signed: beta
type CircuitBreaker struct {
	State            string    `json:"state"`             // "CLOSED", "CIRCUIT_OPEN", "HALF_OPEN"
	ConsecutiveFails int       `json:"consecutive_fails"`
	FailThreshold    int       `json:"fail_threshold"`
	CooldownSec      int       `json:"cooldown_sec"`
	LastFailure      time.Time `json:"last_failure,omitempty"`
	OpenedAt         time.Time `json:"opened_at,omitempty"`
	LastSuccess      time.Time `json:"last_success,omitempty"`
	TotalTrips       int       `json:"total_trips"` // lifetime open count
}

// WorkerHealthResponse is returned by GET /worker/{name}/health.
// signed: beta
type WorkerHealthResponse struct {
	Worker         string          `json:"worker"`
	Healthy        bool            `json:"healthy"`
	CircuitBreaker *CircuitBreaker `json:"circuit_breaker"`
	Alive          bool            `json:"alive"`
	LastHeartbeat  string          `json:"last_heartbeat"`
	TasksCompleted int             `json:"tasks_completed"`
	TotalErrors    int             `json:"total_errors"`
	QueueDepth     int             `json:"queue_depth"`
	Uptime         float64         `json:"uptime_s"`
}

// ─── Worker Views ────────────────────────────────────────────────

type AgentView struct {
	Status         string   `json:"status"`
	TasksCompleted int      `json:"tasks_completed"`
	TotalErrors    int      `json:"total_errors"`
	CurrentTask    string   `json:"current_task"`
	RecentLogs     []string `json:"recent_logs"`
	Progress       int      `json:"progress"`
	Model          string   `json:"model"`
	Uptime         float64  `json:"uptime_s"`
	AvgTaskMs      float64  `json:"avg_task_ms"`
	LastHeartbeat  string   `json:"last_heartbeat"`
	QueueDepth       int      `json:"queue_depth"`
	WeightedLoad     int64    `json:"weighted_load"`      // sum of task weights (queued + active) -- signed: beta
	CircuitState     string   `json:"circuit_state"`
	ConsecutiveFails int      `json:"consecutive_fails"`
}

// ─── API Payloads ────────────────────────────────────────────────

type DashboardPayload struct {
	Agents    map[string]*AgentView `json:"agents"`
	OrchFeed  []ThoughtEntry        `json:"orch_thinking"`
	Bus       []BusMessage          `json:"bus"`
	Uptime    float64               `json:"uptime_s"`
	Version   string                `json:"version"`
	System    string                `json:"system"`
	Timestamp string                `json:"timestamp"` // RFC3339 server timestamp -- signed: beta
}

type HealthResponse struct {
	Status       string  `json:"status"`
	Uptime       float64 `json:"uptime_s"`
	Workers      int     `json:"workers_alive"`
	BusDepth     int     `json:"bus_depth"`
	Timestamp    int64   `json:"timestamp_ns"`
	TimestampRFC string  `json:"timestamp"` // RFC3339 standardized -- signed: beta
}

type MetricsResponse struct {
	Uptime          float64            `json:"uptime_s"`
	TotalRequests   int64              `json:"total_requests"`
	RequestsPerSec  float64            `json:"requests_per_sec"`
	AvgLatencyUs    float64            `json:"avg_latency_us"`
	TasksDispatched int64              `json:"tasks_dispatched"`
	TasksCompleted  int64              `json:"tasks_completed"`
	TasksFailed     int64              `json:"tasks_failed"`
	TaskThroughput  float64            `json:"tasks_per_min"`
	BusMessages     int64              `json:"bus_messages_total"`
	BusDropped      int64              `json:"bus_dropped"`
	BusOverwrites   int64              `json:"bus_overwrites"`   // signed: alpha
	BusCapacity     int                `json:"bus_capacity"`     // signed: alpha
	WorkerStats     map[string]WStats  `json:"worker_stats"`
	Directives      DirectiveStats     `json:"directives"`
	GoroutineCount  int                `json:"goroutine_count"`
	MemAllocMB      float64            `json:"mem_alloc_mb"`
	Timestamp       string             `json:"timestamp"` // RFC3339 server timestamp -- signed: beta
}

type WStats struct {
	TasksCompleted int     `json:"tasks_completed"`
	TotalErrors    int     `json:"total_errors"`
	AvgTaskMs      float64 `json:"avg_task_ms"`
	Status         string  `json:"status"`
	Timestamp      string  `json:"timestamp"` // RFC3339 snapshot time -- signed: beta
}

type DirectiveStats struct {
	Total     int    `json:"total"`
	Active    int    `json:"active"`
	Completed int    `json:"completed"`
	Pending   int    `json:"pending"`
	Timestamp string `json:"timestamp"` // RFC3339 snapshot time -- signed: beta
}

// ─── GOD Feed ────────────────────────────────────────────────────

type GodFeedEntry struct {
	Type string  `json:"type"`
	Text string  `json:"text"`
	Time string  `json:"time"`
	Ts   float64 `json:"ts"`
}

// ─── Worker Task (API-level, in-memory) ──────────────────────────

type WorkerTask struct {
	TaskID      string `json:"task_id"`
	Worker      string `json:"worker"`
	Directive   string `json:"directive"`
	Status      string `json:"status"`
	Result      string `json:"result,omitempty"`
	AssignedAt  string `json:"assigned_at"`
	CompletedAt string `json:"completed_at,omitempty"`
}

// ─── Task Lifecycle Tracker ──────────────────────────────────────
// Tracks the full dispatch-to-result lifecycle for real task visibility.
// signed: gamma

type TaskTracker struct {
	TaskID       string     `json:"task_id"`
	Worker       string     `json:"worker"`
	Goal         string     `json:"goal"`
	DispatchedAt time.Time  `json:"dispatched_at"`
	Status       string     `json:"status"` // dispatched, processing, completed, failed, timeout
	CompletedAt  *time.Time `json:"completed_at,omitempty"`
	DurationMs   float64    `json:"duration_ms,omitempty"`
	DirectiveID  string     `json:"directive_id,omitempty"`
}
