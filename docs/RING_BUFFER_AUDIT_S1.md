# Ring Buffer Architectural Audit — Sprint 1

<!-- signed: alpha -->

**File audited:** `Skynet/bus.go` (204 lines)
**Go version:** 1.24.1 windows/amd64
**Audit date:** 2026-03-17
**Auditor:** Alpha (Skynet Worker — Code Implementation Specialist)

---

## Table of Contents

1. [Current Architecture](#1-current-architecture)
2. [Finding 1: False Sharing on Cache Lines](#2-finding-1-false-sharing-on-cache-lines-medium)
3. [Finding 2: MPMC Race Conditions](#3-finding-2-mpmc-race-conditions-critical)
4. [Finding 3: Ring Capacity Under Burst](#4-finding-3-ring-capacity-under-burst-high)
5. [Finding 4: Memory Ordering Inconsistencies](#5-finding-4-memory-ordering-inconsistencies-high)
6. [Finding 5: Subscriber Fan-Out Drop Impact](#6-finding-5-subscriber-fan-out-drop-impact-medium)
7. [Finding 6: Mixed Locking Disciplines](#7-finding-6-mixed-locking-disciplines-high)
8. [Summary Matrix](#8-summary-matrix)
9. [Recommended Implementation Order](#9-recommended-implementation-order)

---

## 1. Current Architecture

### Struct Layout (MessageBus)

```
┌─────────────────────────────────────────────────────────────────┐
│                      MessageBus struct                          │
├─────────────────────────────────────────────────────────────────┤
│  mu          sync.RWMutex          (~24 bytes on amd64)        │
│  ring        [100]BusMessage       (~100 × ~160 bytes = ~16KB) │
│  head        int                   (8 bytes)                   │
│  count       int                   (8 bytes)                   │
│  totalMsg    int64                 (8 bytes) ← atomic          │
│  dropped     int64                 (8 bytes) ← atomic          │
│  subs        map[...]              (8 bytes — pointer)         │
│  subsMu      sync.RWMutex          (~24 bytes)                 │
│  wildcards   map[...]              (8 bytes — pointer)         │
│  wildcardsMu sync.RWMutex          (~24 bytes)                 │
└─────────────────────────────────────────────────────────────────┘
Total estimated: ~16.2 KB
```

### Data Flow

```
                    POST /bus/publish
                          │
                          ▼
                ┌─────────────────┐
                │   SpamFilter    │ ← fingerprint dedup (60s)
                │   (server.go)   │   + rate limit (10/min/sender)
                │   mutex-locked  │
                └────────┬────────┘
                         │ pass
                         ▼
              ┌──────────────────────┐
              │   MessageBus.Post()  │
              │                      │
              │  1. atomic.Add       │ ← totalMsg (seq number)
              │     (LOCK-FREE)      │
              │                      │
              │  2. mu.Lock()        │ ← ring write (EXCLUSIVE)
              │     ring[head] = msg │
              │     head = (h+1)%100 │
              │     count++          │
              │     mu.Unlock()      │
              │                      │
              │  3. subsMu.RLock()   │ ← topic fan-out (SHARED)
              │     for sub: ch<-msg │
              │     subsMu.RUnlock() │
              │                      │
              │  4. wildcardsMu.     │ ← wildcard fan-out (SHARED)
              │     RLock()          │
              │     for wc: ch<-msg  │
              │     wildcardsMu.     │
              │     RUnlock()        │
              └──────────────────────┘
                         │
                ┌────────┼────────┐
                ▼        ▼        ▼
           ┌────────┐┌────────┐┌────────┐
           │Topic   ││Topic   ││Wildcard│
           │Sub ch  ││Sub ch  ││Sub ch  │
           │(buf=64)││(buf=64)││(buf=64)│
           └────────┘└────────┘└────────┘
                │        │        │
                ▼        ▼        ▼
           SSE daemon  WS push  Bus watcher
           (1Hz poll)  (live)   (auto-route)


              GET /bus/messages
                    │
                    ▼
         ┌────────────────────┐
         │ MessageBus.Recent()│
         │   mu.RLock()       │ ← SHARED read
         │   copy ring[n]     │
         │   mu.RUnlock()     │
         └────────────────────┘
```

### BusMessage Struct Size Estimate

```go
type BusMessage struct {
    ID        string            // 16 bytes (string header: ptr + len)
    Sender    string            // 16 bytes
    Topic     string            // 16 bytes
    Type      string            // 16 bytes
    Content   string            // 16 bytes
    Metadata  map[string]string // 8 bytes  (map pointer)
    Timestamp time.Time         // 24 bytes (wall + ext + loc ptr)
}
// Total: ~112 bytes per BusMessage (header only; string data is heap-allocated)
// Ring array: 100 × 112 = ~11,200 bytes ≈ 11 KB embedded in struct
```

---

## 2. Finding 1: False Sharing on Cache Lines (MEDIUM)

### Analysis

On amd64, a CPU cache line is **64 bytes** (L1/L2/L3). False sharing occurs when
two independently-accessed variables share the same cache line, causing
cross-core cache invalidation thrashing.

The `MessageBus` struct layout (Go compiler order = declaration order for
named fields) places these fields in memory:

```
Offset  Field          Size    Notes
──────  ─────          ────    ─────
0       mu             24 B    sync.RWMutex (state int32 + writerSem + readerSem + readerCount + readerWait)
24      ring           ~11.2KB [100]BusMessage — dominates the struct
~11224  head           8 B     int (mutex-protected write cursor)
~11232  count          8 B     int (mutex-protected counter)
~11240  totalMsg       8 B     int64 (ATOMIC — hot write on every Post)
~11248  dropped        8 B     int64 (ATOMIC — write on subscriber drop)
~11256  subs           8 B     map pointer
~11264  subsMu         24 B    sync.RWMutex
...
```

**Key observation:** `head`, `count`, `totalMsg`, and `dropped` are all within
the same 64-byte cache line region (offsets ~11224 through ~11255 = 32 bytes,
well within one cache line).

However, the practical risk is **MEDIUM, not CRITICAL**, because:

1. **`head` and `count` are mutex-protected.** They are only written under
   `mu.Lock()`, which serializes access. The mutex itself causes cache line
   transfers, so false sharing with `totalMsg` adds only marginal overhead.

2. **`totalMsg` is written atomically BEFORE acquiring the mutex** (line 67).
   This means every `Post()` call does:
   - `atomic.AddInt64(&b.totalMsg, 1)` — cache line write on core A
   - `b.mu.Lock()` — may migrate to core B, pulling the same cache line again
   - `b.ring[b.head] = msg` — writes to a distant offset (the ring array is 11KB away)

   The `totalMsg` → `head` false sharing only matters if the mutex holder and
   the next atomic writer are on different cores, which is the common case
   under concurrent load.

3. **`dropped` is written rarely** (only when channel sends fail). Not a hot
   path under normal operation.

### Severity: MEDIUM

Under the current Skynet workload (~5 msgs/min/sender, max 10/min rate limit),
this causes negligible overhead. Under high-throughput scenarios (100+
concurrent goroutines posting), it would measurably degrade.

### Proposed Fix

```go
type MessageBus struct {
    // --- Hot atomic counters (own cache line) ---
    totalMsg int64
    dropped  int64
    _pad0    [48]byte // pad to 64-byte cache line boundary

    // --- Mutex-protected ring state ---
    mu    sync.RWMutex
    ring  [ringSize]BusMessage
    head  int
    count int

    // --- Subscriber state (separate lock domain) ---
    subs   map[string]map[string]chan BusMessage
    subsMu sync.RWMutex

    wildcards   map[string]chan BusMessage
    wildcardsMu sync.RWMutex
}
```

**Performance impact:** Eliminates ~50ns per concurrent Post under heavy load
from cross-core cache invalidation. Struct grows by 48 bytes (negligible vs
the 11KB ring array). No behavioral change.

---

## 3. Finding 2: MPMC Race Conditions (CRITICAL)

### Analysis

The ring buffer uses a **hybrid concurrency model**: atomic counters for stats +
mutex for ring writes. This creates two distinct vulnerability classes.

#### 2a. Sequence Number vs Ring Position Divergence

```go
func (b *MessageBus) Post(...) {
    seq := atomic.AddInt64(&b.totalMsg, 1)  // ← (1) atomic: seq = N

    msg := BusMessage{
        ID: fmt.Sprintf("msg_%d_%s", seq, sender),  // ← (2) ID uses seq
        ...
    }

    b.mu.Lock()
    b.ring[b.head] = msg    // ← (3) ring position uses head
    b.head = (b.head + 1) % ringSize
    ...
    b.mu.Unlock()
}
```

**The problem:** Steps (1) and (3) are in different synchronization domains.
Consider two concurrent Post() calls:

```
Time  Goroutine A              Goroutine B
────  ─────────────────────    ─────────────────────
t1    seq=atomic.Add → 5
t2                             seq=atomic.Add → 6
t3                             mu.Lock() → acquired
t4                             ring[head=0] = msg{ID:msg_6}
t5                             head=1, mu.Unlock()
t6    mu.Lock() → acquired
t7    ring[head=1] = msg{ID:msg_5}
t8    head=2, mu.Unlock()
```

**Result:** `ring[0]` has `msg_6` and `ring[1]` has `msg_5`. The ring stores
messages out of sequence order. `Recent(2)` returns `[msg_6, msg_5]` — the
messages appear in REVERSED order relative to their sequence numbers.

**Impact:** Medium. Message IDs are not monotonically ordered in the ring.
`Recent()` callers that assume temporal ordering by ID will see inversions.
However, `Timestamp` is set inside the goroutine just before the ring write,
so timestamps may also invert (two `time.Now()` calls on different cores can
return non-monotonic values, though this is rare on amd64).

#### 2b. No ABA Problem (Not Applicable)

The classic ABA problem affects lock-free CAS (Compare-And-Swap) algorithms
where a value transitions A→B→A between a read and CAS, making the CAS
succeed incorrectly.

**This ring does NOT use CAS.** The `head` cursor is protected by a full
mutex, so ABA is not a concern. The `atomic.AddInt64` on `totalMsg` is a
monotonic counter (never wraps in practice) — also immune to ABA.

**Verdict:** No ABA risk. ✅

#### 2c. Fan-Out During Concurrent Posts (Race Window)

```go
// Inside Post(), AFTER mu.Unlock():
b.subsMu.RLock()           // ← (A) acquire read lock
if topicSubs, ok := ...; ok {
    for id, ch := range topicSubs {
        select {
        case ch <- msg:     // ← (B) send to subscriber
        default:
            atomic.AddInt64(&b.dropped, 1)
        }
    }
}
b.subsMu.RUnlock()          // ← (C) release read lock
```

The fan-out holds `subsMu.RLock()` during channel sends. Multiple concurrent
`Post()` calls can all hold the read lock simultaneously (RWMutex allows
concurrent readers). This is **correct** — channel sends to buffered channels
are goroutine-safe. The read lock only guards the map structure, not the
channel sends.

**However:** If `Subscribe()` is called during fan-out (it takes a write lock
on `subsMu`), it blocks until ALL concurrent fan-outs complete. Under burst
conditions with many concurrent Posts all in their fan-out phase, a new
`Subscribe()` call could block for a significant period.

**Impact:** Low. Subscriptions are set up at boot time, not during hot paths.

### Severity: CRITICAL (2a — ordering guarantee violation)

The sequence-ring divergence means `Recent()` does not guarantee chronological
ordering. While practically rare under Skynet's low-throughput workload, this
is a **correctness bug** that could cause:
- Bus pollers receiving out-of-order messages
- Result correlation failures (a result appearing before its dispatch in the ring)
- Debugging confusion when tracing message flow

### Proposed Fix

Move the sequence number assignment inside the mutex to guarantee ring order
matches sequence order:

```go
func (b *MessageBus) Post(sender, topic, msgType, content string, metadata map[string]string) {
    // Build message shell (no seq yet)
    now := time.Now()

    b.mu.Lock()
    seq := atomic.AddInt64(&b.totalMsg, 1) // seq assignment under mutex
    msg := BusMessage{
        ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
        Sender:    sender,
        Topic:     topic,
        Type:      msgType,
        Content:   content,
        Metadata:  metadata,
        Timestamp: now, // captured before lock to reduce hold time
    }
    b.ring[b.head] = msg
    b.head = (b.head + 1) % ringSize
    if b.count < ringSize {
        b.count++
    }
    b.mu.Unlock()

    // Fan-out unchanged...
}
```

**Trade-off:** The `atomic.AddInt64` is now inside the mutex, making the
atomic technically redundant (the mutex already serializes). But keeping it
atomic is harmless and allows `Count()` to remain lock-free. The `time.Now()`
call is moved BEFORE the lock to minimize critical section duration.

**Performance impact:** Near-zero. The mutex was already serializing ring
writes — adding the atomic inside it adds ~1ns to the critical section.

---

## 4. Finding 3: Ring Capacity Under Burst (HIGH)

### Analysis

```go
const ringSize = 100  // hardcoded, compile-time constant
```

The ring is a fixed 100-element FIFO. When `count == ringSize`, new messages
overwrite the oldest without any signal:

```go
b.ring[b.head] = msg           // overwrites whatever was at head
b.head = (b.head + 1) % ringSize
if b.count < ringSize {
    b.count++                  // caps at ringSize, no overflow indicator
}
```

**There is no overwrite counter.** When the ring wraps, the overwritten message
is silently lost. The `dropped` counter only tracks subscriber channel drops,
NOT ring overwrites. These are two different loss mechanisms:

```
┌─────────────────────────────────────────────────────┐
│ Message Loss Paths                                   │
│                                                      │
│  POST ──► Ring Write ──► OVERWRITE (ring full)       │
│                │         ↑ NOT COUNTED ANYWHERE       │
│                │                                      │
│                └──► Channel Send ──► DROP (chan full)  │
│                                     ↑ Counted in      │
│                                       `dropped`       │
└─────────────────────────────────────────────────────┘
```

### Burst Scenario: 150 Simultaneous Sub-Tasks

If 150 sub-tasks are dispatched in rapid succession (e.g., `skynet_brain_dispatch.py`
decomposes a complex goal into 150 subtasks and fires them all):

```
Messages 1-100:   Stored in ring (ring fills)
Messages 101-150: Each overwrites ring[0]..ring[49]
                  50 messages SILENTLY LOST from the ring

Ring state after burst:
  ring[0..49]  = messages 101-150 (newest)
  ring[50..99] = messages 51-100  (survivors)
  Messages 1-50: GONE FOREVER — no record, no counter, no log
```

**Drop rate under burst:** 50/150 = **33.3% message loss** for a 150-message burst.

**Real-world mitigation:** The server-side SpamFilter (10 msgs/min/sender) and
client-side SpamGuard (5 msgs/min/sender) make a 150-message burst physically
impossible through the HTTP API. A single sender can post at most 10 messages
before being rate-limited. Even with 7 distinct senders (4 workers + orchestrator
+ 2 consultants), the maximum burst is ~70 messages/minute — within ring capacity.

**But:** Internal `Post()` calls (from the Go server itself, e.g., task lifecycle
updates, WebSocket broadcasts) bypass the SpamFilter. If internal events generate
bursts, ring overwrites can still occur.

### Severity: HIGH

The ring capacity is adequate for current operational throughput (rate-limited to
~70 msgs/min across all senders), but:
1. **No overwrite counter** means silent data loss is invisible
2. **No configurability** means capacity can't be tuned without recompilation
3. **Internal posts bypass spam filter** and can cause unbounded bursts

### Proposed Fix

```go
// Make ring size configurable via environment variable or config
var ringSize = 100

func init() {
    if envSize := os.Getenv("SKYNET_BUS_RING_SIZE"); envSize != "" {
        if n, err := strconv.Atoi(envSize); err == nil && n >= 100 && n <= 10000 {
            ringSize = n
        }
    }
}

// Use a slice instead of array for dynamic sizing
type MessageBus struct {
    // ...
    ring      []BusMessage
    overflows int64          // atomic: ring overwrite counter (NEW)
    // ...
}

func NewMessageBus() *MessageBus {
    return &MessageBus{
        ring:      make([]BusMessage, ringSize),
        subs:      make(map[string]map[string]chan BusMessage),
        wildcards: make(map[string]chan BusMessage),
    }
}

// In Post():
b.mu.Lock()
if b.count >= len(b.ring) {
    atomic.AddInt64(&b.overflows, 1)  // track overwrites
}
b.ring[b.head] = msg
b.head = (b.head + 1) % len(b.ring)
if b.count < len(b.ring) {
    b.count++
}
b.mu.Unlock()

// New accessor
func (b *MessageBus) Overflows() int64 {
    return atomic.LoadInt64(&b.overflows)
}
```

**Performance impact:** Negligible. Slice access is one indirection vs array
(~1ns). The overflow counter adds one atomic per overwrite, only on the
already-rare overwrite path.

**Recommended default:** Keep 100 for Skynet's workload. Allow up to 1000 for
future scale. Expose via `GET /status` alongside existing `bus_depth` and
`bus_dropped`.

---

## 5. Finding 4: Memory Ordering Inconsistencies (HIGH)

### Analysis

Go's memory model (as of Go 1.19+) specifies that `sync/atomic` operations
participate in the **sequentially consistent** total order. This is stronger
than C++ acquire/release. So the atomic operations themselves are correctly
ordered.

**However, the ISSUE is not with atomic semantics — it's with the interaction
between atomic and mutex-protected variables.**

#### 4a. `totalMsg` Read Without Synchronization

```go
// In Post() — totalMsg written OUTSIDE the mutex:
seq := atomic.AddInt64(&b.totalMsg, 1)  // line 67

// In Monitor() — totalMsg read OUTSIDE the mutex:
currentCount := atomic.LoadInt64(&b.totalMsg)  // line 152
```

This is **correct**. Both operations use atomic, so they participate in the
sequentially consistent order. ✅

#### 4b. `head` and `count` Read in Recent() vs Written in Post()

```go
// Post() writes under mu.Lock():
b.mu.Lock()
b.ring[b.head] = msg
b.head = (b.head + 1) % ringSize
if b.count < ringSize { b.count++ }
b.mu.Unlock()

// Recent() reads under mu.RLock():
b.mu.RLock()
start := (b.head - n + ringSize) % ringSize
result[i] = b.ring[(start+i)%ringSize]
b.mu.RUnlock()
```

This is **correct**. The RWMutex provides the happens-before relationship
between writes and reads of `head`, `count`, and `ring[]`. ✅

#### 4c. Missing Synchronization: Fan-Out Reads Ring Data After Unlock

```go
// In Post():
b.mu.Lock()
b.ring[b.head] = msg    // (1) write msg to ring
// ... update head, count
b.mu.Unlock()            // (2) release mutex

// Fan-out sends `msg` (local variable, not ring reference):
b.subsMu.RLock()
for id, ch := range topicSubs {
    select { case ch <- msg: ... }  // (3) send LOCAL copy
}
```

This is **safe** because the fan-out sends the local `msg` variable, NOT a
reference into the ring. The `msg` struct is captured by value in the closure
at construction time (line 69-77). No dangling reference. ✅

#### 4d. ACTUAL ISSUE: `count` Increment Is Non-Atomic But Read By Depth()

```go
// Post() — under mu.Lock():
if b.count < ringSize {
    b.count++
}

// Depth() — under mu.RLock():
func (b *MessageBus) Depth() int {
    b.mu.RLock()
    defer b.mu.RUnlock()
    return b.count
}
```

This is **correct** as written — both are under the same mutex. ✅

**But `count` is a plain `int`, not atomic.** If anyone ever reads `count`
without holding the mutex (e.g., in a future optimization), it would be a
data race. The SSE handler calls `s.bus.Depth()` (line 2632) which properly
takes the read lock, so this is safe today.

#### 4e. ACTUAL ISSUE: Timestamp Non-Monotonicity

```go
msg := BusMessage{
    ...
    Timestamp: time.Now(),  // line 77
}
```

`time.Now()` is called BEFORE acquiring the mutex. Under concurrent Posts,
two goroutines can call `time.Now()` in arbitrary order vs their mutex
acquisition order:

```
Goroutine A: time.Now() → 10:00:00.001
Goroutine B: time.Now() → 10:00:00.002
Goroutine B: mu.Lock() → acquired first (OS scheduling)
Goroutine B: ring[0] = msg{ts: 10:00:00.002}
Goroutine A: mu.Lock() → acquired second
Goroutine A: ring[1] = msg{ts: 10:00:00.001}  ← EARLIER timestamp at LATER position
```

**Result:** Ring positions do not guarantee timestamp ordering. `Recent()`
can return messages where `result[i].Timestamp > result[i+1].Timestamp`.

### Severity: HIGH

The timestamp inversion (4e) is the real issue. Combined with the sequence
number divergence from Finding 2, the ring provides **no ordering guarantee
whatsoever** — neither by ID, nor by timestamp, nor by insertion order under
concurrent writes. While the mutex ensures each individual write is atomic,
the ordering of writes from different goroutines is non-deterministic.

### Proposed Fix

Capture the timestamp inside the mutex critical section (same fix as Finding 2):

```go
func (b *MessageBus) Post(sender, topic, msgType, content string, metadata map[string]string) {
    b.mu.Lock()
    seq := atomic.AddInt64(&b.totalMsg, 1)
    msg := BusMessage{
        ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
        Sender:    sender,
        Topic:     topic,
        Type:      msgType,
        Content:   content,
        Metadata:  metadata,
        Timestamp: time.Now(), // inside mutex = monotonic ordering guaranteed
    }
    b.ring[b.head] = msg
    b.head = (b.head + 1) % ringSize
    if b.count < ringSize {
        b.count++
    }
    b.mu.Unlock()

    // Fan-out unchanged (uses local `msg` copy)...
}
```

**Trade-off:** `time.Now()` inside the mutex adds ~20ns to the critical
section. Under Skynet's workload (≤10 msgs/min), this is irrelevant. Under
extreme load (>10K msgs/sec), this would become a bottleneck — but at that
scale the entire mutex-based design needs replacing with a proper LMAX
Disruptor pattern.

---

## 6. Finding 5: Subscriber Fan-Out Drop Impact (MEDIUM)

### Analysis

The fan-out uses non-blocking channel sends:

```go
select {
case ch <- msg:
    // delivered
default:
    atomic.AddInt64(&b.dropped, 1)
    fmt.Printf("[BUS] Dropped msg for subscriber %s on topic %s (slow consumer)\n", id, topic)
}
```

**Channel buffer size:** 64 messages (set at Subscribe time, line 47/60).

**Drop behavior:** When a subscriber's channel is full (64 unread messages),
new messages are silently dropped. The subscriber receives no notification
that it missed messages.

### Consumer Impact Analysis

| Consumer | Subscribe Method | Buffer | Drop Impact |
|----------|-----------------|--------|-------------|
| SSE `/stream` | `SubscribeAll()` | 64 | **LOW** — SSE handler uses 1Hz ticker polling `Recent(10)` directly, NOT the subscription channel. The SSE handler in `handleSSEStream()` (server.go:2600) calls `s.bus.Recent(10)` on every tick. It does NOT read from a subscription channel. So SSE is immune to channel drops. |
| WebSocket push | Direct `broadcastWS()` | N/A | **ZERO** — WebSocket broadcast in `handleBusPublish()` (server.go:2448) is called directly from the HTTP handler, not via subscription. No channel involved. |
| Bus monitor | Internal `Monitor()` | N/A | **ZERO** — Uses `atomic.LoadInt64(&b.totalMsg)` directly, not subscriptions. |
| Bus watcher daemon | `skynet_bus_watcher.py` | N/A | **ZERO** — Python daemon polls `GET /bus/messages` (HTTP), not Go channels. |
| Bus persist daemon | `skynet_bus_persist.py` | N/A | **ZERO** — Python daemon subscribes to SSE `/stream`, not Go channels. |
| Realtime daemon | `skynet_realtime.py` | N/A | **ZERO** — Python daemon subscribes to SSE `/stream`. |

**Key finding:** As of the current codebase, **NO external consumer actually
reads from Go subscription channels.** All Python daemons use HTTP polling or
SSE streaming. The Go subscription channels are only used by:

1. **Internal server routing** (e.g., `handleBusMessages` reads from `Recent()`,
   not channels)
2. **The test suite** (`bus_test.go` uses `Subscribe()` to verify fan-out behavior)

The subscription/fan-out mechanism is essentially **dead infrastructure** — it
exists and works correctly, but nothing in production reads from it.

### What Would Break If Channels Were Used

If a future Go-internal consumer subscribes and processes messages slowly:

- **Buffer:** 64 messages per subscriber
- **Fill time at max rate:** 64 / (10 msgs/min) = **6.4 minutes** before first drop
- **Fill time under burst (if spam filter bypassed):** Could fill in <1 second
- **Recovery:** None — dropped messages are gone. The subscriber has no way to
  know it missed messages or request replay. No sequence numbers are exposed
  to subscribers.

### Severity: MEDIUM

No production consumer is affected today, but the mechanism has no replay
capability, no gap detection, and no backpressure signaling. If channels
are adopted for internal routing in the future, this will cause silent
data loss.

### Proposed Fix (For Future-Proofing)

```go
// Option A: Increase buffer (simple, immediate)
ch := make(chan BusMessage, 256) // 4x current buffer

// Option B: Add sequence tracking to enable gap detection
type BusMessage struct {
    Seq       int64             `json:"seq"`       // monotonic sequence number
    // ... existing fields
}

// Subscribers can detect gaps:
// if msg.Seq != lastSeq + 1 { /* gap detected, request replay via Recent() */ }

// Option C: Add backpressure signal (most robust)
type SubscriberStats struct {
    Delivered int64
    Dropped   int64
    LastSeq   int64
}
// Expose per-subscriber stats via GET /bus/subscribers endpoint
```

**Recommended:** Option B (add `Seq` field to BusMessage) is the highest-value
fix. It enables gap detection without changing the non-blocking send pattern.
Cost: 8 bytes per message + one assignment in Post().

---

## 7. Finding 6: Mixed Locking Disciplines (HIGH)

### Analysis

The `MessageBus` struct uses **three independent mutexes**:

| Mutex | Protects | Lock Pattern |
|-------|----------|-------------|
| `mu` | `ring`, `head`, `count` | `Lock()` in Post, `RLock()` in Recent/Depth |
| `subsMu` | `subs` map | `Lock()` in Subscribe, `RLock()` in Post fan-out |
| `wildcardsMu` | `wildcards` map | `Lock()` in SubscribeAll, `RLock()` in Post fan-out |

**Lock acquisition order in Post():**
```
1. mu.Lock()       → ring write
2. mu.Unlock()
3. subsMu.RLock()  → topic fan-out
4. subsMu.RUnlock()
5. wildcardsMu.RLock()  → wildcard fan-out
6. wildcardsMu.RUnlock()
```

**Deadlock risk:** None — locks are always acquired in the same order and
never nested (mu is released before subsMu is acquired). ✅

**However:** The gap between `mu.Unlock()` (step 2) and `subsMu.RLock()`
(step 3) creates a **visibility window** where a message is in the ring but
has not yet been fanned out to subscribers. During this window:

- `Recent()` will return the message (it reads the ring under `mu.RLock()`)
- Subscribers will NOT have received it yet (fan-out hasn't happened)
- A poller calling `GET /bus/messages` could see a message that channel
  subscribers haven't received yet

**Impact:** Low. In practice, the gap is nanoseconds, and all production
consumers use HTTP polling (which reads from the ring via `Recent()`), not
channels.

### ACTUAL ISSUE: Clear() Doesn't Drain Subscriber Channels

```go
func (b *MessageBus) Clear() int {
    b.mu.Lock()
    defer b.mu.Unlock()
    cleared := b.count
    b.head = 0
    b.count = 0
    return cleared
}
```

`Clear()` resets the ring but leaves all subscriber channels with their
existing buffered messages. After `Clear()`:
- `Recent(N)` returns empty (ring is cleared)
- Subscriber channels still have up to 64 stale messages each
- The ring array still contains the old BusMessage structs (just not
  addressable via `head`/`count`) — these reference heap-allocated strings
  that won't be GC'd until the ring slots are overwritten

**Impact:** Memory leak of stale string data after `Clear()`, and stale
messages delivered to channel subscribers after a ring clear. The `Clear()`
function is called via `GET /bus/clear` endpoint — it's an admin operation
used rarely.

### Severity: HIGH (mixed locking) / LOW (Clear leak — admin-only path)

### Proposed Fix

```go
func (b *MessageBus) Clear() int {
    b.mu.Lock()
    cleared := b.count
    b.head = 0
    b.count = 0
    // Zero out ring to allow GC of string data
    for i := range b.ring {
        b.ring[i] = BusMessage{} // zero value — releases string references
    }
    b.mu.Unlock()

    // Drain all subscriber channels (optional — prevents stale delivery)
    b.subsMu.RLock()
    for _, topicSubs := range b.subs {
        for _, ch := range topicSubs {
            drainChannel(ch)
        }
    }
    b.subsMu.RUnlock()

    b.wildcardsMu.RLock()
    for _, ch := range b.wildcards {
        drainChannel(ch)
    }
    b.wildcardsMu.RUnlock()

    return cleared
}

func drainChannel(ch chan BusMessage) {
    for {
        select {
        case <-ch:
        default:
            return
        }
    }
}
```

---

## 8. Summary Matrix

| # | Finding | Severity | Exploitable Today? | Fix Complexity | Performance Impact |
|---|---------|----------|-------------------|----------------|-------------------|
| 1 | False sharing on cache lines (`totalMsg`/`head`/`count` co-located) | **MEDIUM** | No (low throughput) | Low (padding) | ~50ns/op under contention |
| 2 | Sequence-ring divergence (ID ordering not guaranteed) | **CRITICAL** | Yes (concurrent Posts) | Low (move atomic inside mutex) | ~1ns added to critical section |
| 3 | Ring capacity: hardcoded 100, no overwrite counter | **HIGH** | Mitigated by spam filter | Medium (slice + config + counter) | Negligible |
| 4 | Timestamp non-monotonicity under concurrency | **HIGH** | Yes (concurrent Posts) | Low (move time.Now inside mutex) | ~20ns added to critical section |
| 5 | Subscriber fan-out drops: no gap detection, no replay | **MEDIUM** | No (no channel consumers) | Medium (add Seq field) | 8 bytes/msg |
| 6 | Clear() doesn't release ring memory or drain channels | **HIGH** | Yes (after Clear call) | Low (zero ring + drain) | One-time on Clear |

### Risk Heat Map

```
                    LOW IMPACT ◄─────────────► HIGH IMPACT
                    │                                    │
  HIGH LIKELIHOOD   │                                    │
        ▲           │  [5] Fan-out drops                 │
        │           │  (future risk)                     │
        │           │                                    │
        │           │           [1] False sharing        │
        │           │           (perf only)              │
        │           │                                    │
        │           │                    [3] Ring        │
        │           │                    capacity        │
        │           │                                    │
        │           │  [6] Clear() leak    [4] Timestamp │
        │           │                      inversion     │
        │           │                                    │
  LOW LIKELIHOOD    │                    [2] Seq-ring    │
        ▼           │                    divergence      │
                    │                    (correctness)   │
                    │                                    │
```

---

## 9. Recommended Implementation Order

### Phase 1: Correctness (Fix Findings 2 + 4 together)

**Single change** — move both `atomic.AddInt64` and `time.Now()` inside the
mutex. This is a ~5 line diff that fixes both the sequence ordering and
timestamp monotonicity bugs simultaneously.

```go
func (b *MessageBus) Post(sender, topic, msgType, content string, metadata map[string]string) {
    b.mu.Lock()
    seq := atomic.AddInt64(&b.totalMsg, 1)
    msg := BusMessage{
        ID:        fmt.Sprintf("msg_%d_%s", seq, sender),
        Sender:    sender,
        Topic:     topic,
        Type:      msgType,
        Content:   content,
        Metadata:  metadata,
        Timestamp: time.Now(),
    }
    b.ring[b.head] = msg
    b.head = (b.head + 1) % ringSize
    if b.count < ringSize {
        b.count++
    }
    b.mu.Unlock()

    // Fan-out to topic subscribers (non-blocking) — unchanged
    b.subsMu.RLock()
    // ... rest unchanged
}
```

**Risk:** Low. The mutex was already serializing ring writes. Moving two
cheap operations inside it doesn't change behavior or performance
meaningfully.

### Phase 2: Observability (Fix Finding 3 — overflow counter)

Add `overflows int64` atomic counter. Expose via `Overflows()` accessor and
include in `/status` and `/stream` SSE payloads. This is pure instrumentation —
no behavioral change.

### Phase 3: Memory Safety (Fix Finding 6 — Clear())

Zero ring array on Clear() and drain subscriber channels. Small, self-contained
fix. Only affects the admin `Clear()` path.

### Phase 4: Performance (Fix Finding 1 — padding)

Add cache line padding to separate atomic counters from mutex-protected fields.
Pure performance optimization. Only valuable if throughput increases 100x+.

### Phase 5: Future-Proofing (Fix Finding 5 — Seq field)

Add `Seq` to `BusMessage` for gap detection. Requires updating `types.go` and
all message producers. Lower priority because no channel consumers exist today.

---

## Appendix A: Existing Test Coverage

The `bus_test.go` file provides **good coverage** of the ring buffer:

| Test | What It Covers |
|------|---------------|
| `TestNewMessageBus` | Zero-value initialization |
| `TestBusPostAndRecent` | Basic Post + Recent round-trip |
| `TestBusRecentLimit` | Ring read with limit < count |
| `TestBusRecentMoreThanAvailable` | Requesting more than available |
| `TestBusRecentEmpty` | Empty ring behavior |
| `TestBusRingBufferOverflow` | Ring wrapping after >100 messages |
| `TestBusPostWithMetadata` | Metadata preservation |
| `TestBusMessageID` | ID uniqueness |
| `TestBusSubscribe` | Topic-based subscription |
| `TestBusSubscribeSenderExclusion` | Self-message filtering |
| `TestBusSubscribeAll` | Wildcard subscription |
| `TestBusClear` | Clear + depth reset + total preservation |
| `TestBusConcurrentPosts` | 100 goroutines × 10 messages |
| `TestBusConcurrentSubscribeAndPost` | Concurrent Post + channel receive |
| `TestBusMonitorCancellation` | Context cancellation |
| `TestBusMessageTimestamp` | Timestamp bounds |

**Missing tests:**
- No test for message ordering under concurrency (Finding 2)
- No test for ring overwrite detection (Finding 3)
- No test for channel drop behavior (Finding 5)
- No test for Clear() memory release (Finding 6)
- No benchmark for contention measurement (Finding 1)

---

## Appendix B: Comparison With Alternative Designs

| Design | Throughput | Ordering | Memory | Complexity |
|--------|-----------|----------|--------|------------|
| **Current (mutex + ring array)** | ~1M msg/sec | ❌ Not guaranteed | Fixed 11KB | Low |
| **Mutex + ring (seq inside lock)** | ~1M msg/sec | ✅ Guaranteed | Fixed 11KB | Low |
| **Lock-free SPSC ring** | ~10M msg/sec | ✅ By design | Fixed | Medium |
| **LMAX Disruptor (MPMC)** | ~100M msg/sec | ✅ Sequence barrier | Fixed | High |
| **Channel-only (no ring)** | ~5M msg/sec | ✅ FIFO | Dynamic | Lowest |

**Recommendation:** The current mutex-based design is appropriate for Skynet's
throughput (~10 msgs/min). Applying the Phase 1 fix (seq + timestamp inside
mutex) gives correctness guarantees with zero meaningful performance cost.
A lock-free redesign is unnecessary unless throughput requirements increase by
1000x+.

---

*End of audit. signed: alpha*
