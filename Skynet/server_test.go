package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// newTestServer creates a SkynetServer with test workers for httptest.
func newTestServer(workerNames ...string) *SkynetServer {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 256)

	if len(workerNames) == 0 {
		workerNames = []string{"alpha", "beta"}
	}

	workers := make([]*Worker, len(workerNames))
	for i, name := range workerNames {
		workers[i] = NewWorker(name, bus, results)
	}

	srv := NewSkynetServer(bus, workers, results)
	return srv
}

func doRequest(handler http.Handler, method, path string, body interface{}) *httptest.ResponseRecorder {
	var reqBody io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		reqBody = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, reqBody)
	req.RemoteAddr = "127.0.0.1:9999" // Bypass rate limiting in tests
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)
	return rr
}

// ─── /status ─────────────────────────────────────────────────────

func TestHandleStatus(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/status", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp DashboardPayload
	if err := json.NewDecoder(rr.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	if resp.Version != "2.0.0" {
		t.Errorf("expected version '2.0.0', got '%s'", resp.Version)
	}
	if resp.System != "skynet" {
		t.Errorf("expected system 'skynet', got '%s'", resp.System)
	}
	if len(resp.Agents) != 2 {
		t.Errorf("expected 2 agents, got %d", len(resp.Agents))
	}
	if _, ok := resp.Agents["alpha"]; !ok {
		t.Error("expected agent 'alpha' in response")
	}
	if _, ok := resp.Agents["beta"]; !ok {
		t.Error("expected agent 'beta' in response")
	}
}

func TestHandleStatusMethodNotAllowed(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	rr := doRequest(handler, "POST", "/status", nil)
	if rr.Code != http.StatusMethodNotAllowed {
		t.Errorf("expected 405, got %d", rr.Code)
	}
}

// ─── /health ─────────────────────────────────────────────────────

func TestHandleHealth(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/health", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp HealthResponse
	if err := json.NewDecoder(rr.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode: %v", err)
	}

	if resp.Status != "ok" {
		t.Errorf("expected status 'ok', got '%s'", resp.Status)
	}
	if resp.Workers < 0 {
		t.Errorf("expected non-negative alive workers, got %d", resp.Workers)
	}
}

// ─── /metrics ────────────────────────────────────────────────────

func TestHandleMetrics(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/metrics", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp MetricsResponse
	if err := json.NewDecoder(rr.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode: %v", err)
	}

	if resp.Uptime < 0 {
		t.Error("expected non-negative uptime")
	}
	if resp.GoroutineCount <= 0 {
		t.Error("expected positive goroutine count")
	}
	if len(resp.WorkerStats) != 2 {
		t.Errorf("expected 2 worker stats, got %d", len(resp.WorkerStats))
	}
}

// ─── /directive ──────────────────────────────────────────────────

func TestHandleDirective(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	body := map[string]interface{}{
		"goal":     "test directive",
		"priority": 5,
	}
	rr := doRequest(handler, "POST", "/directive", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["status"] != "ok" {
		t.Errorf("expected status 'ok', got '%v'", resp["status"])
	}
	if resp["directive_id"] == nil || resp["directive_id"] == "" {
		t.Error("expected non-empty directive_id")
	}
}

func TestHandleDirectiveMissingGoal(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	body := map[string]interface{}{"priority": 5}
	rr := doRequest(handler, "POST", "/directive", body)
	if rr.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for missing goal, got %d", rr.Code)
	}
}

func TestHandleDirectiveBadJSON(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	req := httptest.NewRequest("POST", "/directive", strings.NewReader("not json"))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for bad JSON, got %d", rr.Code)
	}
}

func TestHandleDirectiveWithRoute(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	body := map[string]interface{}{
		"goal":  "routed task",
		"route": "alpha",
	}
	rr := doRequest(handler, "POST", "/directive", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	// Verify task was enqueued to alpha
	if srv.workers[0].QueueDepth() == 0 {
		t.Error("expected task to be enqueued to alpha worker")
	}
}

func TestHandleDirectivePriorityNormalization(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// Priority out of range should be normalized to 3
	body := map[string]interface{}{
		"goal":     "test",
		"priority": 99,
	}
	rr := doRequest(handler, "POST", "/directive", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["priority"].(float64) != 3 {
		t.Errorf("expected normalized priority 3, got %v", resp["priority"])
	}
}

// ─── /dispatch ───────────────────────────────────────────────────

func TestHandleDispatch(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	body := map[string]interface{}{
		"worker":    "alpha",
		"directive": "test task",
		"task_id":   "task_001",
	}
	rr := doRequest(handler, "POST", "/dispatch", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	var resp map[string]string
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["worker"] != "alpha" {
		t.Errorf("expected worker 'alpha', got '%s'", resp["worker"])
	}
	if resp["task_id"] != "task_001" {
		t.Errorf("expected task_id 'task_001', got '%s'", resp["task_id"])
	}
}

func TestHandleDispatchAutoBalance(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	body := map[string]interface{}{
		"directive": "auto-balanced task",
		"task_id":   "task_auto",
	}
	rr := doRequest(handler, "POST", "/dispatch", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	var resp map[string]string
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["worker"] == "" {
		t.Error("expected auto-balanced worker assignment")
	}
}

func TestHandleDispatchMissingFields(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	body := map[string]interface{}{"worker": "alpha"}
	rr := doRequest(handler, "POST", "/dispatch", body)
	if rr.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for missing fields, got %d", rr.Code)
	}
}

// ─── /dispatch with no workers (division-by-zero fix) ────────────

func TestHandleDispatchNoWorkers(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 256)
	srv := NewSkynetServer(bus, []*Worker{}, results)
	handler := srv.Handler()

	body := map[string]interface{}{
		"directive": "task for nobody",
		"task_id":   "task_empty",
	}
	rr := doRequest(handler, "POST", "/dispatch", body)

	// Should return 503 instead of panicking with division-by-zero
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when no workers available, got %d: %s", rr.Code, rr.Body.String())
	}
}

// ─── /bus/publish ────────────────────────────────────────────────

func TestHandleBusPublish(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	body := map[string]interface{}{
		"sender":  "alpha",
		"topic":   "results",
		"type":    "report",
		"content": "task completed",
	}
	rr := doRequest(handler, "POST", "/bus/publish", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["status"] != "published" {
		t.Errorf("expected status 'published', got '%v'", resp["status"])
	}
}

func TestHandleBusPublishMissingSender(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	body := map[string]interface{}{"content": "no sender"}
	rr := doRequest(handler, "POST", "/bus/publish", body)
	if rr.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for missing sender, got %d", rr.Code)
	}
}

func TestHandleBusPublishMissingContent(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	body := map[string]interface{}{"sender": "alpha"}
	rr := doRequest(handler, "POST", "/bus/publish", body)
	if rr.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for missing content, got %d", rr.Code)
	}
}

func TestHandleBusPublishDefaultTopicAndType(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	// Omit topic and type — should default to "general" and "message"
	body := map[string]interface{}{
		"sender":  "alpha",
		"content": "test msg",
	}
	rr := doRequest(handler, "POST", "/bus/publish", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	msgs := srv.bus.Recent(1)
	if len(msgs) != 1 {
		t.Fatal("expected 1 bus message")
	}
	if msgs[0].Topic != "general" {
		t.Errorf("expected default topic 'general', got '%s'", msgs[0].Topic)
	}
	if msgs[0].Type != "message" {
		t.Errorf("expected default type 'message', got '%s'", msgs[0].Type)
	}
}

// ─── /bus/messages ───────────────────────────────────────────────

func TestHandleBusMessages(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	// Publish some messages first
	srv.bus.Post("alpha", "results", "report", "msg1", nil)
	srv.bus.Post("beta", "system", "alert", "msg2", nil)

	rr := doRequest(handler, "GET", "/bus/messages?limit=10", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var msgs []BusMessage
	json.NewDecoder(rr.Body).Decode(&msgs)

	if len(msgs) != 2 {
		t.Errorf("expected 2 messages, got %d", len(msgs))
	}
}

func TestHandleBusMessagesWithFilters(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	srv.bus.Post("alpha", "results", "report", "alpha msg", nil)
	srv.bus.Post("beta", "system", "alert", "beta msg", nil)

	// Filter by sender
	rr := doRequest(handler, "GET", "/bus/messages?sender=alpha", nil)
	var msgs []BusMessage
	json.NewDecoder(rr.Body).Decode(&msgs)

	for _, m := range msgs {
		if m.Sender != "alpha" {
			t.Errorf("expected only alpha messages, got sender '%s'", m.Sender)
		}
	}

	// Filter by topic
	rr = doRequest(handler, "GET", "/bus/messages?topic=system", nil)
	msgs = nil
	json.NewDecoder(rr.Body).Decode(&msgs)

	for _, m := range msgs {
		if m.Topic != "system" {
			t.Errorf("expected only system topic, got '%s'", m.Topic)
		}
	}
}

// ─── /bus/clear ──────────────────────────────────────────────────

func TestHandleBusClear(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	srv.bus.Post("s", "t", "r", "c", nil)
	srv.bus.Post("s", "t", "r", "c", nil)

	rr := doRequest(handler, "POST", "/bus/clear", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	cleared := int(resp["cleared"].(float64))
	if cleared != 2 {
		t.Errorf("expected 2 cleared, got %d", cleared)
	}
	if srv.bus.Depth() != 0 {
		t.Errorf("expected depth 0 after clear, got %d", srv.bus.Depth())
	}
}

// ─── /cancel ─────────────────────────────────────────────────────

func TestHandleCancel(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// First dispatch a task
	dispatchBody := map[string]interface{}{
		"worker":    "alpha",
		"directive": "cancellable task",
		"task_id":   "task_cancel_1",
	}
	doRequest(handler, "POST", "/dispatch", dispatchBody)

	// Now cancel it
	cancelBody := map[string]interface{}{
		"task_id": "task_cancel_1",
	}
	rr := doRequest(handler, "POST", "/cancel", cancelBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestHandleCancelNotFound(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	body := map[string]interface{}{"task_id": "nonexistent"}
	rr := doRequest(handler, "POST", "/cancel", body)
	if rr.Code != http.StatusNotFound {
		t.Errorf("expected 404, got %d", rr.Code)
	}
}

// ─── /results ────────────────────────────────────────────────────

func TestHandleResults(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	// Store some results
	srv.storeTaskResult(&TaskResult{
		TaskID:     "r1",
		Status:     "success",
		Output:     "output1",
		WorkerName: "alpha",
		FinishedAt: time.Now(),
	})

	rr := doRequest(handler, "GET", "/results?n=10", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var results []TaskResult
	json.NewDecoder(rr.Body).Decode(&results)

	if len(results) != 1 {
		t.Errorf("expected 1 result, got %d", len(results))
	}
}

// ─── /worker/{name}/heartbeat ────────────────────────────────────

func TestHandleWorkerHeartbeat(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	body := map[string]interface{}{
		"hwnd_alive": true,
		"visible":    true,
		"model":      "claude-opus-4.6-fast",
		"state":      "IDLE",
	}
	rr := doRequest(handler, "POST", "/worker/alpha/heartbeat", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	// Verify model was updated
	state := srv.workers[0].GetState()
	if state.Model != "claude-opus-4.6-fast" {
		t.Errorf("expected model 'claude-opus-4.6-fast', got '%s'", state.Model)
	}
}

func TestHandleWorkerHeartbeatDeadWorker(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	body := map[string]interface{}{
		"hwnd_alive": false,
		"visible":    false,
	}
	rr := doRequest(handler, "POST", "/worker/alpha/heartbeat", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	// Should have posted an alert to the bus
	msgs := srv.bus.Recent(5)
	foundAlert := false
	for _, m := range msgs {
		if m.Topic == "orchestrator" && m.Type == "alert" && strings.Contains(m.Content, "DEAD") {
			foundAlert = true
			break
		}
	}
	if !foundAlert {
		t.Error("expected DEAD worker alert on bus")
	}
}

func TestHandleWorkerHeartbeatNotFound(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	body := map[string]interface{}{"hwnd_alive": true}
	rr := doRequest(handler, "POST", "/worker/nonexistent/heartbeat", body)
	if rr.Code != http.StatusNotFound {
		t.Errorf("expected 404, got %d", rr.Code)
	}
}

// ─── /worker/{name}/tasks ────────────────────────────────────────

func TestHandleWorkerTasks(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// Dispatch a task
	dispatchBody := map[string]interface{}{
		"worker":    "alpha",
		"directive": "some work",
		"task_id":   "wt_001",
	}
	doRequest(handler, "POST", "/dispatch", dispatchBody)

	// Get worker tasks
	rr := doRequest(handler, "GET", "/worker/alpha/tasks", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var tasks []map[string]string
	json.NewDecoder(rr.Body).Decode(&tasks)

	if len(tasks) != 1 {
		t.Errorf("expected 1 pending task, got %d", len(tasks))
	}
}

// ─── /worker/{name}/result ───────────────────────────────────────

func TestHandleWorkerResult(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// First dispatch a task
	dispatchBody := map[string]interface{}{
		"worker":    "alpha",
		"directive": "test work",
		"task_id":   "wr_001",
	}
	doRequest(handler, "POST", "/dispatch", dispatchBody)

	// Report result
	resultBody := map[string]interface{}{
		"task_id": "wr_001",
		"result":  "work completed",
		"status":  "completed",
	}
	rr := doRequest(handler, "POST", "/worker/alpha/result", resultBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
}

// ─── /orchestrate with no workers (division-by-zero fix) ─────────

func TestHandleOrchestrateNoWorkers(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 256)
	srv := NewSkynetServer(bus, []*Worker{}, results)
	handler := srv.Handler()

	body := map[string]interface{}{
		"prompt": "test orchestration",
	}
	rr := doRequest(handler, "POST", "/orchestrate", body)

	// Should return 503 instead of panicking
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when no workers, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestHandleOrchestrateWithWorkers(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	body := map[string]interface{}{
		"prompt": "test orchestration task",
	}
	rr := doRequest(handler, "POST", "/orchestrate", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	if resp["status"] != "ok" {
		t.Errorf("expected status 'ok', got '%v'", resp["status"])
	}
	if resp["directive_id"] == nil {
		t.Error("expected directive_id")
	}
}

func TestHandleOrchestrateAutoDispatch(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	body := map[string]interface{}{
		"prompt":        "broadcast task",
		"auto_dispatch": true,
	}
	rr := doRequest(handler, "POST", "/orchestrate", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	assigned := resp["workers_assigned"].([]interface{})
	if len(assigned) != 2 {
		t.Errorf("expected 2 workers assigned for auto_dispatch, got %d", len(assigned))
	}
}

// ─── /orchestrate/pipeline with no workers ───────────────────────

func TestHandleOrchestratePipelineNoWorkers(t *testing.T) {
	bus := NewMessageBus()
	results := make(chan *TaskResult, 256)
	srv := NewSkynetServer(bus, []*Worker{}, results)
	handler := srv.Handler()

	body := map[string]interface{}{
		"steps": []map[string]string{
			{"name": "step1", "prompt": "do something"},
		},
	}
	rr := doRequest(handler, "POST", "/orchestrate/pipeline", body)

	// Should return 503 instead of panicking
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when no workers, got %d: %s", rr.Code, rr.Body.String())
	}
}

// ─── /bus/tasks ──────────────────────────────────────────────────

func TestHandleBusTasksLifecycle(t *testing.T) {
	srv := newTestServer("alpha")
	handler := srv.Handler()

	// 1. Post a new task
	postBody := map[string]interface{}{
		"task":     "investigate bug",
		"priority": 1,
		"source":   "orchestrator",
	}
	rr := doRequest(handler, "POST", "/bus/tasks", postBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	var postResp map[string]string
	json.NewDecoder(rr.Body).Decode(&postResp)
	taskID := postResp["task_id"]
	if taskID == "" {
		t.Fatal("expected non-empty task_id")
	}

	// 2. List pending tasks
	rr = doRequest(handler, "GET", "/bus/tasks", nil)
	var tasks []QueuedTask
	json.NewDecoder(rr.Body).Decode(&tasks)
	if len(tasks) != 1 {
		t.Errorf("expected 1 pending task, got %d", len(tasks))
	}

	// 3. Claim the task
	claimBody := map[string]interface{}{
		"task_id": taskID,
		"worker":  "alpha",
	}
	rr = doRequest(handler, "POST", "/bus/tasks/claim", claimBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 for claim, got %d: %s", rr.Code, rr.Body.String())
	}

	// 4. Complete the task
	completeBody := map[string]interface{}{
		"task_id": taskID,
		"worker":  "alpha",
		"result":  "bug fixed",
	}
	rr = doRequest(handler, "POST", "/bus/tasks/complete", completeBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 for complete, got %d: %s", rr.Code, rr.Body.String())
	}

	// 5. Verify no more pending tasks
	rr = doRequest(handler, "GET", "/bus/tasks", nil)
	tasks = nil
	json.NewDecoder(rr.Body).Decode(&tasks)
	if len(tasks) != 0 {
		t.Errorf("expected 0 pending tasks after completion, got %d", len(tasks))
	}
}

// ─── /bus/convene ────────────────────────────────────────────────

func TestHandleBusConveneLifecycle(t *testing.T) {
	srv := newTestServer("alpha", "beta")
	handler := srv.Handler()

	// 1. Create session
	createBody := map[string]interface{}{
		"initiator":    "alpha",
		"topic":        "code review",
		"context":      "review security.py",
		"need_workers": 2,
	}
	rr := doRequest(handler, "POST", "/bus/convene", createBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	var createResp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&createResp)
	sessionID := createResp["session_id"].(string)

	// 2. Join session
	joinBody := map[string]interface{}{
		"session_id": sessionID,
		"worker":     "beta",
	}
	rr = doRequest(handler, "PATCH", "/bus/convene", joinBody)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 for join, got %d", rr.Code)
	}

	// 3. List sessions
	rr = doRequest(handler, "GET", "/bus/convene", nil)
	var sessions []ConveneSession
	json.NewDecoder(rr.Body).Decode(&sessions)
	if len(sessions) != 1 {
		t.Errorf("expected 1 session, got %d", len(sessions))
	}
	if len(sessions[0].Participants) != 2 {
		t.Errorf("expected 2 participants, got %d", len(sessions[0].Participants))
	}

	// 4. Resolve session
	rr = doRequest(handler, "DELETE", "/bus/convene?id="+sessionID, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 for resolve, got %d", rr.Code)
	}
}

// ─── /security/audit ─────────────────────────────────────────────

func TestHandleSecurityAudit(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	srv.logSecurityEvent("test", "probe", "test probe", false)
	srv.logSecurityEvent("test", "injection", "blocked injection", true)

	rr := doRequest(handler, "GET", "/security/audit", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	totalEvents := int(resp["total_events"].(float64))
	blockedCount := int(resp["blocked_count"].(float64))

	if totalEvents != 2 {
		t.Errorf("expected 2 total events, got %d", totalEvents)
	}
	if blockedCount != 1 {
		t.Errorf("expected 1 blocked event, got %d", blockedCount)
	}
}

// ─── /ws/stats ───────────────────────────────────────────────────

func TestHandleWSStats(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/ws/stats", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	var resp map[string]interface{}
	json.NewDecoder(rr.Body).Decode(&resp)

	clients := int(resp["connected_clients"].(float64))
	if clients != 0 {
		t.Errorf("expected 0 connected clients, got %d", clients)
	}
}

// ─── /dashboard ──────────────────────────────────────────────────

func TestHandleDashboard(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/dashboard", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}

	contentType := rr.Header().Get("Content-Type")
	if !strings.Contains(contentType, "text/html") {
		t.Errorf("expected Content-Type text/html, got '%s'", contentType)
	}

	body := rr.Body.String()
	if !strings.Contains(body, "SKYNET") {
		t.Error("dashboard HTML should contain 'SKYNET'")
	}
}

// ─── Middleware ───────────────────────────────────────────────────

func TestCORSHeaders(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	rr := doRequest(handler, "GET", "/health", nil)

	if rr.Header().Get("Access-Control-Allow-Origin") != "*" {
		t.Error("expected CORS header Access-Control-Allow-Origin: *")
	}
}

func TestOptionsPreflightReturns200(t *testing.T) {
	srv := newTestServer()
	handler := srv.Handler()

	rr := doRequest(handler, "OPTIONS", "/status", nil)
	if rr.Code != http.StatusOK {
		t.Errorf("expected 200 for OPTIONS preflight, got %d", rr.Code)
	}
}

// ─── Internal helpers ────────────────────────────────────────────

func TestAddThought(t *testing.T) {
	srv := newTestServer()

	srv.addThought("test", "hello world")
	srv.addThought("info", "second thought")

	srv.thMu.RLock()
	count := len(srv.thoughts)
	srv.thMu.RUnlock()

	if count != 2 {
		t.Errorf("expected 2 thoughts, got %d", count)
	}
}

func TestAddThoughtMaxSize(t *testing.T) {
	srv := newTestServer()

	for i := 0; i < 250; i++ {
		srv.addThought("test", "thought")
	}

	srv.thMu.RLock()
	count := len(srv.thoughts)
	srv.thMu.RUnlock()

	if count > 200 {
		t.Errorf("expected thoughts capped at 200, got %d", count)
	}
}

func TestStoreTaskResult(t *testing.T) {
	srv := newTestServer()

	srv.storeTaskResult(&TaskResult{
		TaskID: "r1", Status: "success", Output: "done",
	})

	srv.trMu.RLock()
	count := len(srv.taskResults)
	srv.trMu.RUnlock()

	if count != 1 {
		t.Errorf("expected 1 result stored, got %d", count)
	}
}

func TestStoreTaskResultMaxSize(t *testing.T) {
	srv := newTestServer()

	for i := 0; i < 600; i++ {
		srv.storeTaskResult(&TaskResult{TaskID: "r", Status: "success"})
	}

	srv.trMu.RLock()
	count := len(srv.taskResults)
	srv.trMu.RUnlock()

	if count > 500 {
		t.Errorf("expected results capped at 500, got %d", count)
	}
}

func TestBroadcastWSNoClients(t *testing.T) {
	srv := newTestServer()

	// Should not panic when no WS clients connected
	srv.broadcastWS([]byte(`{"test": true}`))

	// Verify broadcast counter incremented
	if srv.wsBroadcasts != 1 {
		t.Errorf("expected 1 broadcast, got %d", srv.wsBroadcasts)
	}
}

func TestMakeWSFrame(t *testing.T) {
	tests := []struct {
		name        string
		payloadSize int
	}{
		{"small", 10},
		{"medium", 200},
		{"large", 70000},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			payload := make([]byte, tt.payloadSize)
			frame := makeWSFrame(payload)

			if frame[0] != 0x81 {
				t.Errorf("expected opcode 0x81, got 0x%02x", frame[0])
			}

			if tt.payloadSize < 126 {
				if int(frame[1]) != tt.payloadSize {
					t.Errorf("expected length byte %d, got %d", tt.payloadSize, frame[1])
				}
			} else if tt.payloadSize < 65536 {
				if frame[1] != 126 {
					t.Errorf("expected extended length marker 126, got %d", frame[1])
				}
			} else {
				if frame[1] != 127 {
					t.Errorf("expected extended length marker 127, got %d", frame[1])
				}
			}
		})
	}
}

func TestComputeWebSocketAccept(t *testing.T) {
	// Known test vector from RFC 6455 section 4.2.2
	key := "dGhlIHNhbXBsZSBub25jZQ=="
	expected := "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

	result := computeWebSocketAccept(key)
	if result != expected {
		t.Errorf("expected '%s', got '%s'", expected, result)
	}
}

// ─── Config ──────────────────────────────────────────────────────

func TestLoadConfigDefaults(t *testing.T) {
	cfg := LoadConfig()

	if cfg.Port != 8420 {
		t.Errorf("expected default port 8420, got %d", cfg.Port)
	}
	if cfg.RingSize != 100 {
		t.Errorf("expected default ring size 100, got %d", cfg.RingSize)
	}
	if cfg.MaxRetries != 3 {
		t.Errorf("expected default max retries 3, got %d", cfg.MaxRetries)
	}
	if len(cfg.Workers) != 5 {
		t.Errorf("expected 5 default workers, got %d", len(cfg.Workers))
	}
}
