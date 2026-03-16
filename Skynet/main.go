package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	_ "net/http/pprof" // P1.06: profiling endpoints on :6060 — signed: alpha
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	log.SetFlags(log.Ltime | log.Lmicroseconds)
	log.Println("╔═══════════════════════════════════════╗")
	log.Println("║        SKYNET v2 — BOOTING            ║")
	log.Println("╚═══════════════════════════════════════╝")

	// Config
	cfg := LoadConfig()

	// Message bus
	bus := NewMessageBus()
	log.Println("[bus] Ring buffer online (capacity:", ringSize, ")")

	// Result channel shared by all workers
	results := make(chan *TaskResult, 256)

	// Worker pool
	workers := make([]*Worker, len(cfg.Workers))
	for i, name := range cfg.Workers {
		workers[i] = NewWorker(name, bus, results)
		go workers[i].Run()
		log.Printf("[pool] Worker %s spawned", name)
	}

	// Bus monitor
	busCtx, busCancel := context.WithCancel(context.Background())
	defer busCancel()
	go bus.Monitor(busCtx)

	// HTTP server
	srv := NewSkynetServer(bus, workers, results)
	srv.StartCleanup()

	// Drain results in background
	go func() {
		for r := range results {
			log.Printf("[result] %s from %s (%.2fms)", r.TaskID, r.WorkerName, r.DurationMs)
			srv.ProcessResult(r)
		}
	}()

	// Startup thoughts
	srv.addThought("system", "SKYNET v2 ONLINE — all workers spawned")
	srv.addThought("system", fmt.Sprintf("Bus capacity: %d | Workers: %d | Port: %d", ringSize, len(workers), cfg.Port))
	// P1.06: pprof profiling on localhost-only debug port — signed: alpha
	go func() {
		log.Println("[pprof] Debug endpoints on http://127.0.0.1:6060/debug/pprof/")
		if err := http.ListenAndServe("127.0.0.1:6060", nil); err != nil {
			log.Printf("[pprof] Failed to start: %v", err)
		}
	}()

	httpServer := &http.Server{
		// P1.08: Bind to localhost only — blocks external network access — signed: alpha
		Addr:         fmt.Sprintf("127.0.0.1:%d", cfg.Port),
		Handler:      srv.Handler(),
		ReadTimeout: 5 * time.Second,
		// WriteTimeout must be 0 for SSE/stream endpoints (long-lived connections).
		// Per-request timeouts are handled by context cancellation instead.
		WriteTimeout: 0,
	}

	// Start server
	go func() {
		log.Printf("[http] Listening on 127.0.0.1:%d", cfg.Port)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[http] Fatal: %v", err)
		}
	}()

	fmt.Println()
	log.Println("Skynet v2 online — all systems nominal")
	log.Printf("Dashboard: http://localhost:%d/dashboard", cfg.Port)
	log.Printf("Status:    http://localhost:%d/status", cfg.Port)
	fmt.Println()

	// Graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("[shutdown] Signal received — draining...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Stop HTTP server
	if err := httpServer.Shutdown(ctx); err != nil {
		log.Printf("[shutdown] HTTP drain error: %v", err)
	}

	// Stop all workers
	for _, w := range workers {
		w.Stop()
	}

	log.Println("[shutdown] Skynet v2 offline. Goodbye.")
}
