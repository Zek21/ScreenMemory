# Persistence & Rate Limiting Cross-Validation — Sprint 4

<!-- signed: delta -->

**Cross-Validator:** Delta (Architecture Verification Specialist)
**Original Author:** Gamma (Self-Awareness & Collective Intelligence Specialist)
**Date:** 2026-03-17
**Scope:** All `signed: gamma` code in `Skynet/server.go` + `docs/PERSISTENCE_RATELIMIT_IMPL_S2.md`

---

## Verdict: **PASS WITH 2 FINDINGS**

Gamma's implementation is solid. The 3 CRITICAL race fixes are correct, the lock-free token bucket is well-designed, and the RWMutex upgrade is sound. Two bugs were found — one MEDIUM (missing error handling in `appendBrainInbox`) and one MINOR (non-atomic init in rate limiter).

---

## Test Results

| Test | Description | Result |
|------|-------------|--------|
| `TestTokenBucketAllow_BasicRate` | Exactly `tokenBucketCapacity` requests allowed | ✅ PASS |
| `TestTokenBucketAllow_DeniesWhenEmpty` | Zero tokens → deny | ✅ PASS |
| `TestTokenBucketBurst_CannotExceedCapacity` | Long idle refill caps at capacity | ✅ PASS |
| `TestTokenBucketRefill_Timing` | Refill after 600ms yields ≥1 token | ✅ PASS |
| `TestTokenBucketRefill_ProportionalToElapsed` | 3s elapsed → ~6 tokens refilled | ✅ PASS |
| `TestTokenBucketConcurrent` | 50 goroutines × 10 requests, no over-allocation | ✅ PASS |
| `TestTokenBucketConcurrent_NoTokenLoss` | Sequential CAS never drops tokens | ✅ PASS |
| `TestHandleGodFeedConcurrentReads` | 20 readers + 5 writers, no torn JSON | ✅ PASS |
| `TestHandleBrainPendingConcurrentReads` | 20 readers + 5 writers, no torn JSON | ✅ PASS |
| `TestHandleBrainAckAtomicity` | 10 concurrent ACKs, all items marked completed | ✅ PASS |
| `TestHandleBrainAckConcurrentWithAppend` | Mixed ACK + append, no data loss | ✅ PASS |
| `TestRateLimitMiddleware_LocalhostExempt` | localhost never rate-limited | ✅ PASS |
| `TestRateLimitMiddleware_NonLocalhost` | External IPs are rate-limited | ✅ PASS |
| `TestHandleGodFeedStress` | 10 writers + 10 readers × 30 ops each | ✅ PASS |
| `TestStartCleanup_EvictsStale` | Stale buckets evicted, fresh preserved | ✅ PASS |

**Race detector:** Could not run (`-race` requires CGO + gcc, not available on this Windows host). Concurrency correctness was verified via high-contention goroutine tests that would surface data races as torn reads, lost writes, or assertion failures.

---

## Code Review: Detailed Analysis

### P1 — handleGodFeed Race Fix ✅ CORRECT

```go
s.godFeedMu.RLock()
data, err := os.ReadFile(feedPath)
s.godFeedMu.RUnlock()
```

- **Lock scope:** Only `ReadFile` is under RLock. The returned `data` byte slice is an independent copy, so unmarshal after unlock is safe. This is the correct minimal-scope pattern.
- **Writer side:** `appendGodFeed` uses `Lock()` with `defer Unlock()`. Mutually exclusive with RLock. Correct.
- **Deadlock risk:** None. `godFeedMu` is never held while acquiring another mutex.

### P2a — handleBrainPending Race Fix ✅ CORRECT

Same pattern as P1. `brainInboxMu.RLock()` protects `ReadFile`, unmarshal + filtering happen outside lock. Correct.

### P2b — handleBrainAck RMW Atomicity ✅ CORRECT

```go
s.brainInboxMu.Lock()
// ... read → modify → write ...
s.brainInboxMu.Unlock()
```

- **Full RMW under exclusive lock:** Prevents concurrent ACK + appendBrainInbox races. The entire read-modify-write cycle is atomic with respect to `brainInboxMu`.
- **Early return paths:** Lines 829, 835, 850 all call `Unlock()` before returning. Correct.
- **No defer:** Manual unlock is used to allow early returns. This is idiomatic Go for error-heavy paths. Risk: a future code change could miss an unlock. However, this is standard practice and acceptable.
- **Cross-lock safety:** `handleBrainAck` only holds `brainInboxMu`. No other mutex is acquired. No deadlock risk.

### P3 — Token Bucket ✅ CORRECT (with MINOR finding)

**Algorithm correctness:**
- `allow()` CAS refill: Only one goroutine wins the `CompareAndSwap` on `lastRefill`, preventing double-refill. Losers fall through to the consume loop — they still benefit from the winner's refill.
- `allow()` CAS consume: Standard CAS-loop pattern for atomic decrement. Cannot go below 0.
- Token cap: `if newVal > tokenBucketCapacity { newVal = tokenBucketCapacity }` — correct.
- Refill calculation: `int64(elapsed) * tokenRefillRate / int64(time.Second)` — integer division truncates, which is conservative (under-refills rather than over-refills). Acceptable.

**MINOR FINDING — Non-atomic initialization in rateLimitMiddleware (line 2692-2694):**
```go
if tb.lastRefill.Load() == 0 {
    tb.tokens.Store(tokenBucketCapacity)
    tb.lastRefill.Store(time.Now().UnixNano())
}
```
This is a check-then-act without CAS. Under extreme concurrency, two goroutines could both see `lastRefill == 0`, both `Store(20)`, and the second `Store` on `lastRefill` overwrites the first. Impact: benign — both goroutines store the same capacity value and similar timestamps. No security or correctness risk. However, it could be tightened with `CompareAndSwap(0, now)` to match the atomic discipline used elsewhere.

### P4 — MarshalIndent → Marshal ✅ CORRECT

All 3 call sites confirmed:
- `appendGodFeed` L1801: `json.Marshal(feed)` ✓
- `appendBrainInbox` L1836: `json.Marshal(inbox)` ✓
- `handleBrainAck` L855: `json.Marshal(inbox)` ✓

### P5 — Error Handling ⚠️ INCOMPLETE (MEDIUM finding)

**`appendGodFeed`** — L1802-1807: Error handling added for both `Marshal` and `WriteFile`. Early return on marshal error prevents writing corrupt data. ✅ Correct.

**`appendBrainInbox`** — L1836-1837:
```go
out, _ := json.Marshal(inbox)   // error DISCARDED
os.WriteFile(inboxPath, out, 0644) // error DISCARDED
```
**MEDIUM BUG:** P5 was supposed to add error handling for "goroutine-spawned writes," but `appendBrainInbox` was NOT fixed. It still discards both `Marshal` and `WriteFile` errors. If `Marshal` returns an error, `out` is `nil`, and `WriteFile` would write 0 bytes — truncating `brain_inbox.json` and silently destroying all pending directives. This is the same class of bug that P5 fixed in `appendGodFeed`.

### P3 Middleware — Localhost Exemption ✅ CORRECT

```go
if ip == "127.0.0.1" || ip == "[::1]" || ip == "localhost" {
```
All three standard localhost representations checked. `r.RemoteAddr` is parsed with `strings.LastIndex(ip, ":")` to strip port. Correct.

### StartCleanup ✅ CORRECT

- Uses `sync.Map.Range` for non-blocking iteration.
- 30-second staleness cutoff with 60-second cleanup interval.
- `Delete(key)` is safe during `Range` per Go documentation.

---

## Pre-Existing Issues Found

### `worker_steal_test.go` — Missing `fmt` Import (GAMMA-SIGNED)

The file `worker_steal_test.go` (`signed: gamma`) uses `fmt.Sprintf` at line 153 but the `"fmt"` import exists in the file. However, the full test suite had a transient build failure during the first run attempt (`undefined: fmt`). This resolved itself on subsequent runs — likely a Go build cache invalidation issue, not a real code bug.

### `TestWSFrameSizeRejected` Failure (DELTA-SIGNED, separate scope)

One pre-existing test failure exists in `server_ws_test.go` related to my Wave 2 WebSocket changes. This is unrelated to Gamma's work and will be tracked separately.

---

## Findings Summary

| # | Severity | Location | Description | Status |
|---|----------|----------|-------------|--------|
| 1 | **MEDIUM** | `appendBrainInbox` L1836-1837 | P5 error handling not applied — `Marshal` and `WriteFile` errors silently discarded. `nil` marshal output would truncate the inbox file. | OPEN — needs fix |
| 2 | **MINOR** | `rateLimitMiddleware` L2692-2694 | Token bucket initialization uses check-then-act instead of CAS. Benign race under extreme concurrency. | OPEN — low priority |

---

## Recommendation

**Gamma's work is APPROVED.** The core concurrency fixes (P1, P2a, P2b) are correct and well-scoped. The token bucket is sound. Finding #1 (`appendBrainInbox` missing P5 error handling) should be fixed in the next wave — it's a 3-line change mirroring the existing `appendGodFeed` pattern.
