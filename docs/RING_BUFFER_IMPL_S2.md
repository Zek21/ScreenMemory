# Ring Buffer Hardening — Sprint 2 Implementation

<!-- signed: alpha -->

**Files changed:** `Skynet/bus.go`, `Skynet/server.go`, `Skynet/types.go`
**Implementation date:** 2026-03-17
**Author:** Alpha (Skynet Worker)
**Audit reference:** `docs/RING_BUFFER_AUDIT_S1.md`

---

## Changes Summary

| Priority | Finding | Severity | Fix Applied | Lines Changed |
|----------|---------|----------|-------------|---------------|
| P1 | Seq-ring divergence | **CRITICAL** | Moved `atomic.AddInt64` + `time.Now()` inside `mu.Lock()` | bus.go:93-114 |
| P2 | Hardcoded ring size | **HIGH** | `SKYNET_RING_SIZE` env var, array→slice, `init()` | bus.go:15-23, 46, 59-65 |
| P3 | No overwrite counter | **HIGH** | `overwrites int64` atomic + `Overwrites()` accessor | bus.go:40, 106-108, 217-221 |
| P4 | Clear() memory leak | **HIGH** | Zero ring slots + drain subscriber channels | bus.go:235-276 |
| P5 | False sharing | **MEDIUM** | 128-byte cache-line pad between atomics and mutex | bus.go:42 |

---

## P1: CRITICAL — Sequence-Ring Divergence Fix

### Before

```go
func (b *MessageBus) Post(...) {
    seq := atomic.AddInt64(&b.totalMsg, 1)  // OUTSIDE mutex
    msg := BusMessage{
        ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
        Timestamp: time.Now(),              // OUTSIDE mutex
    }
    b.mu.Lock()
    b.ring[b.head] = msg
    // ...
    b.mu.Unlock()
}
```

**Bug:** Two concurrent goroutines could get seq=5 and seq=6, then acquire the
mutex in reverse order, producing `ring[0]=msg_6, ring[1]=msg_5`. Message IDs
were not monotonically increasing in ring position order. Timestamps could
also invert since `time.Now()` was called before lock acquisition.

### After

```go
func (b *MessageBus) Post(...) {
    b.mu.Lock()
    seq := atomic.AddInt64(&b.totalMsg, 1)  // INSIDE mutex
    msg := BusMessage{
        ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
        Timestamp: time.Now(),              // INSIDE mutex
    }
    b.ring[b.head] = msg
    // ...
    b.mu.Unlock()
}
```

**Guarantee:** Ring position N always has a lower seq number and earlier-or-equal
timestamp than position N+1. `Recent()` returns messages in strict chronological
order.

**Performance:** The mutex was already serializing ring writes. Moving two cheap
operations (~21ns combined) inside it adds negligible overhead.

---

## P2: HIGH — Configurable Ring Size

### Before

```go
const ringSize = 100
type MessageBus struct {
    ring [ringSize]BusMessage  // compile-time array
}
```

### After

```go
var ringSize = 100

func init() {
    if s := os.Getenv("SKYNET_RING_SIZE"); s != "" {
        if n, err := strconv.Atoi(s); err == nil && n >= 100 && n <= 10000 {
            ringSize = n
        }
    }
}

type MessageBus struct {
    ring []BusMessage  // runtime-sized slice
}

func NewMessageBus() *MessageBus {
    return &MessageBus{
        ring: make([]BusMessage, ringSize),
        // ...
    }
}
```

**Constraints:** Min 100 (preserves existing behavior), max 10000 (prevents
accidental multi-MB allocation). Invalid env values silently fall back to 100.

**All internal references** changed from `ringSize` to `len(b.ring)` — the
ring self-describes its size, making it impossible for a mismatch between the
constant and the actual allocation.

---

## P3: HIGH — Overwrite Counter

### New field

```go
type MessageBus struct {
    overwrites int64 // atomic: messages overwritten by ring wrap
    // ...
}
```

### Tracking

```go
// In Post(), inside mutex:
if b.count >= len(b.ring) {
    atomic.AddInt64(&b.overwrites, 1)
}
```

### Exposure

- `bus.Overwrites()` — lock-free accessor (atomic load)
- `bus.Capacity()` — returns `len(b.ring)` for ring size
- SSE `/stream` — `bus_overwrites` and `bus_capacity` fields added
- `GET /metrics` — `bus_overwrites` and `bus_capacity` in MetricsResponse
- Monitor output — `overwrites: N` added to 30s stats line

### Types updated

```go
// types.go MetricsResponse:
BusOverwrites int64 `json:"bus_overwrites"`
BusCapacity   int   `json:"bus_capacity"`
```

---

## P4: HIGH — Clear() Memory Safety

### Before

```go
func (b *MessageBus) Clear() int {
    b.mu.Lock()
    defer b.mu.Unlock()
    cleared := b.count
    b.head = 0
    b.count = 0
    return cleared  // ring slots still hold old BusMessage references
}
```

**Bugs:**
1. Ring slots retained string/map heap references — GC couldn't collect them
2. Subscriber channels still had buffered stale messages

### After

```go
func (b *MessageBus) Clear() int {
    b.mu.Lock()
    cleared := b.count
    b.head = 0
    b.count = 0
    for i := range b.ring {
        b.ring[i] = BusMessage{}  // zero value releases heap refs
    }
    b.mu.Unlock()

    // Drain all subscriber channels
    b.subsMu.RLock()
    for _, topicSubs := range b.subs {
        for _, ch := range topicSubs {
            drainChan(ch)
        }
    }
    b.subsMu.RUnlock()
    // ... same for wildcards
    return cleared
}
```

**Lock ordering:** Ring lock released before subscriber locks to maintain the
same acquisition order as `Post()` — no deadlock risk.

---

## P5: MEDIUM — Cache-Line Padding

### Before (adjacent fields share cache line)

```
[totalMsg int64][dropped int64][mu RWMutex][ring...][head int][count int]
 ← same 64-byte cache line under contention →
```

### After (128-byte separation)

```
[totalMsg int64][dropped int64][overwrites int64]
[_pad 128 bytes ──────────────────────────────────]
[mu RWMutex][ring...][head int][count int]
```

128-byte pad covers both 64-byte (Intel) and 128-byte (Apple M-series) cache
lines. The atomics live in their own cache line, preventing cross-core
invalidation when a writer updates `totalMsg` while a reader holds `mu`.

---

## Test Results

```
All 82 tests PASS (0 failures, 1.08s total)
Build: go build ./... — clean, 0 errors
```

All existing tests pass without modification. The changes are
backward-compatible:
- Default `ringSize` is still 100
- `NewMessageBus()` still works identically
- `Recent()`, `Post()`, `Subscribe()` APIs unchanged
- `Depth()`, `Count()`, `Dropped()` return same values
- `Clear()` returns same count, now also cleans up properly

---

## API Additions

| Method | Returns | Description |
|--------|---------|-------------|
| `Overwrites() int64` | Atomic counter | Ring wrap overwrite count |
| `Capacity() int` | Ring size | Current ring buffer capacity |

## New Config

| Env Var | Default | Range | Description |
|---------|---------|-------|-------------|
| `SKYNET_RING_SIZE` | 100 | 100–10000 | Ring buffer message capacity |

## New Metrics Fields

| Endpoint | Field | Type |
|----------|-------|------|
| `GET /metrics` | `bus_overwrites` | int64 |
| `GET /metrics` | `bus_capacity` | int |
| SSE `/stream` | `bus_overwrites` | int64 |
| SSE `/stream` | `bus_capacity` | int |

---

*End of implementation document. signed: alpha*
