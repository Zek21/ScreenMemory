content = r"""
## AREA 2: FAULT TOLERANCE & SELF-HEALING

This section details how to make Skynet resilient to failures. We must assume workers will crash, the network will flake, and the orchestrator will be overwhelmed. We analyze ten fault tolerance patterns and their Go implementation.

### 1. Circuit Breaker Deep-Dive

**Concept:**
A Circuit Breaker prevents an application from repeatedly trying to execute an operation that's likely to fail.
*   **Closed:** Requests pass through.
*   **Open:** Requests fail immediately. Triggered by failure threshold.
*   **Half-Open:** Limited requests allowed to check if the issue is resolved.

**Application to Skynet:**
Skynet currently has a simple "3 failures" rule. It needs adaptive thresholds. If `alpha` is down, we shouldn't send 100 tasks to it.
*   **Upgrade:** Per-worker circuit breakers.
*   **Metrics:** Failure rate (%), latency (p99), timeout count.

**Go Implementation Strategy:**
*   **Library:** Use `gobreaker` or `hystrix-go`.
    ```go
    cb := gobreaker.NewCircuitBreaker(gobreaker.Settings{
        Name:        "WorkerAlpha",
        MaxRequests: 1, // Half-open limit
        Interval:    60 * time.Second,
        Timeout:     30 * time.Second,
        ReadyToTrip: func(counts gobreaker.Counts) bool {
            return counts.ConsecutiveFailures > 3
        },
    })
    
    result, err := cb.Execute(func() (interface{}, error) {
        return workerClient.SendTask(task)
    })
    ```
*   **Adaptive Thresholds:** Dynamically adjust `MaxRequests` based on recent success rate.

**Impact:** CRITICAL. Prevents cascading failures and protects backend resources.
**Priority:** CRITICAL.

### 2. Bulkhead Pattern

**Concept:**
Isolate elements of an application into pools so that if one fails, the others continue to function. Named after ship bulkheads.
*   **Thread Pools:** Dedicated goroutine pools per service.
*   **Connection Pools:** Separate DB connections per tenant.

**Application to Skynet:**
If 4 workers share 1 goroutine pool for processing bus messages, a slow consumer (e.g., logging) can block dispatch.
*   **Upgrade:**
    *   **Worker Pools:** Separate goroutine pool for each worker (`alpha`, `beta`, `gamma`, `delta`).
    *   **System Pool:** For internal tasks (health checks, cron).
    *   **HTTP Pool:** For incoming API requests.

**Go Implementation Strategy:**
*   **Bounded Concurrency:**
    ```go
    // Semaphore pattern
    sem := make(chan struct{}, 10) // Max 10 concurrent requests
    func HandleRequest() {
        sem <- struct{}{}
        defer func() { <-sem }()
        // Process
    }
    ```
*   **Separate Services:** Ideally, move heavy computation to separate microservices. But within Go monolith:
    ```go
    go workerAlpha.RunLoop()
    go workerBeta.RunLoop()
    ```

**Impact:** HIGH. Ensures `alpha` crashing doesn't take down `beta` or the dashboard.
**Priority:** HIGH.

### 3. Retry Strategies

**Concept:**
Retries help transient failures but can cause "Retry Storms" (DDoS-ing yourself).
*   **Exponential Backoff:** `Wait = Base * 2^Attempt`.
*   **Jitter:** Add randomness to prevent synchronized retries. `Wait = (Base * 2^Attempt) + Random()`.
*   **Retry Budget:** Limit total retries system-wide (e.g., 10% of traffic).

**Application to Skynet:**
Skynet retries tasks blindly or not at all.
*   **Upgrade:** Smart retries.
    *   **Network Error:** Retry immediately.
    *   **Rate Limit (429):** Retry after `Retry-After` header.
    *   **Logic Error (500):** Do NOT retry (bug in code).

**Go Implementation Strategy:**
*   **Custom Retry Loop:**
    ```go
    func Retry(op func() error) error {
        for i := 0; i < 3; i++ {
            err := op()
            if err == nil { return nil }
            // Check error type
            if isPermanent(err) { return err }
            
            backoff := time.Duration(math.Pow(2, float64(i))) * time.Second
            jitter := time.Duration(rand.Intn(100)) * time.Millisecond
            time.Sleep(backoff + jitter)
        }
        return fmt.Errorf("max retries exceeded")
    }
    ```

**Impact:** HIGH. Improves reliability for network glitches and temporary unavailability.
**Priority:** HIGH.

### 4. Supervision Trees

**Concept:**
Popularized by Erlang/OTP. A Supervisor process manages child processes. If a child crashes, the Supervisor restarts it according to a policy.
*   **One-For-One:** Restart only the crashed child.
*   **One-For-All:** Restart all children if one crashes (coupled dependencies).
*   **Rest for Intensity:** If > 5 crashes in 1m, give up (escalate to parent supervisor).

**Application to Skynet:**
Skynet has 16 daemons. If `skynet_monitor.py` crashes, it must be restarted. Currently, `skynet_watchdog.py` handles this, but it's external.
*   **Upgrade:** Internal Supervision Tree in Go backend for managing internal goroutines/services.

**Go Implementation Strategy:**
*   **Service Wrapper:**
    ```go
    type Service interface {
        Start() error
        Stop() error
    }
    ```
*   **Supervisor:**
    ```go
    type Supervisor struct {
        Services map[string]Service
    }
    func (s *Supervisor) Monitor(name string, svc Service) {
        defer func() {
            if r := recover(); r != nil {
                log.Printf("Service %s panicked: %v. Restarting...", name, r)
                s.StartService(name)
            }
        }()
        svc.Start()
    }
    ```
*   **Suture Library:** Use `thejerf/suture` for robust supervision trees in Go.

**Impact:** MEDIUM. Improves daemon uptime and reliability.
**Priority:** MEDIUM.

### 5. Health Check Patterns

**Concept:**
Health checks determine if a service can handle traffic.
*   **Liveness:** Is the process running? (If no, restart).
*   **Readiness:** Is it ready to serve? (DB connected, cache warm).
*   **Deep vs Shallow:**
    *   **Shallow:** HTTP 200 OK.
    *   **Deep:** `SELECT 1`, verify Redis ping, check disk space.

**Application to Skynet:**
Skynet checks `GET /status`. This is shallow. A worker might be "Running" but its model is hallucinating or its disk is full.
*   **Upgrade:**
    *   **Deep Check:** Ask worker to solve `1+1`.
    *   **Dependency Check:** Backend checks DB, Redis, Worker connections.
    *   **Health Score:** Aggregate multiple metrics (CPU, RAM, Error Rate) -> 0-100 score.

**Go Implementation Strategy:**
*   **Health Endpoint:**
    ```go
    func HealthCheck(w http.ResponseWriter, r *http.Request) {
        status := CheckDependencies() // DB, Redis, etc.
        if status.Healthy {
            w.WriteHeader(200)
        } else {
            w.WriteHeader(503)
        }
        json.NewEncoder(w).Encode(status)
    }
    ```
*   **Worker Probing:** Periodically send a "Synthetic Transaction" (Test Task) to workers to verify end-to-end functionality.

**Impact:** HIGH. Prevents routing tasks to "Zombie" workers.
**Priority:** HIGH.

---
"""

with open(r"D:\Prospects\ScreenMemory\data\worker_output\reports\gemini_research_stream3_events_faulttolerance.md", "a", encoding="utf-8") as f:
    f.write(content)
