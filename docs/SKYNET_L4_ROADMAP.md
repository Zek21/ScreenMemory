# Skynet Level 4 Roadmap — "Convergence"

> **Status:** DRAFT — compiled from Sprint 1–5 audits, cross-validation findings, and research proposals.
> **Baseline:** Level 3.5 (Sprint 2 complete, all 4 areas hardened, cross-validated, scored 8.25/10 weighted avg).

---

## 1. Executive Summary

Level 4 ("Convergence") targets three strategic goals:

1. **Correctness completion** — close the 5 immediate P0–P4 fixes surfaced by Sprint 5 cross-validation.
2. **Performance unlock** — replace mutex-serialised hot paths with lock-free structures where audit data proves ROI.
3. **Durability** — add write-ahead logging so the bus survives process restarts without message loss.

All items below are sourced from audited, cross-validated findings — no speculative features.

---

## 2. Immediate Fixes (P0–P4, carry-over from Sprint 5)

These were flagged in `FINAL_SCORECARD_S5.md §6` as pre-Level-4 prerequisites.

| ID | Priority | Item | Owner | Effort | Status |
|----|----------|------|-------|--------|--------|
| L4-001 | P0 | ~~Fix `wsAllowedOrigin` subdomain spoof~~ | Alpha | 10 min | ✅ DONE (boundary char check, commit in server.go) |
| L4-002 | P1 | Fix WS connection limit TOCTOU — `atomic.AddInt64` then rollback if over cap | Delta | 5 min | OPEN |
| L4-003 | P2 | Fix `appendBrainInbox` missing error handling — mirror `appendGodFeed` | Gamma | 3 min | OPEN |
| L4-004 | P3 | Add `logSecurityEvent` for unmasked WS frames | Delta | 2 min | OPEN |
| L4-005 | P4 | Fix 4 doc errors in WORKER_SCHEDULING_IMPL_S2.md | Beta | 10 min | OPEN |

---

## 3. Backlog — Prioritised

### 3.1 Ring Buffer & Bus (from RING_BUFFER_AUDIT_S1, RING_BUFFER_CV_S4)

| ID | Priority | Item | Area | Effort | Impact | Source |
|----|----------|------|------|--------|--------|--------|
| L4-010 | P1 | Add `Unsubscribe()` to MessageBus | Ring Buffer | 1 hr | Prevents subscriber leak if dynamic consumers are added | FINAL_SCORECARD §6 |
| L4-011 | P1 | Fix `Clear()` TOCTOU — hold mutex across channel drain | Ring Buffer | 30 min | Eliminates race where Post() can write to a draining ring | FINAL_SCORECARD §6 |
| L4-012 | P2 | Lock-free SPSC ring for SSE fan-out | Ring Buffer | 4 hr | ~10× throughput for the SSE hot path (currently mutex-serialised) | RING_BUFFER_AUDIT Appendix B |
| L4-013 | P3 | LMAX Disruptor-style MPMC ring | Ring Buffer | 20 hr | ~100× throughput — only justified if external traffic or federated buses are added | RING_BUFFER_AUDIT §3, Appendix B |
| L4-014 | P2 | Add ring buffer benchmarks (`go test -bench`) | Ring Buffer | 2 hr | Provides data for all future ring optimisation decisions | RING_BUFFER_AUDIT §10 |

### 3.2 Persistence & Rate Limiting (from PERSISTENCE_RATELIMIT_AUDIT_S1)

| ID | Priority | Item | Area | Effort | Impact | Source |
|----|----------|------|------|--------|--------|--------|
| L4-020 | P1 | **WAL for `god_feed` and `brain_inbox`** — append-only log, periodic compaction, backward-compatible JSON import | Persistence | 4 hr | 16× faster writes, eliminates full-file JSON rewrite on every operation | PERSIST_AUDIT §3 |
| L4-021 | P1 | Replace `rateMu` with atomic token bucket + `sync.Map` | Rate Limit | 2 hr | Lock-free rate limiting; eliminates `rateMu` entirely | PERSIST_AUDIT §5 |
| L4-022 | P2 | Replace `SpamFilter.mu` with `sync.Map` for fingerprints | Spam Filter | 1 hr | Lock-free spam dedup checks | PERSIST_AUDIT §5.5 |
| L4-023 | P2 | Make file paths configurable via env var or config | Persistence | 30 min | Portability across deployments | PERSIST_AUDIT §7 |
| L4-024 | P2 | Switch to `goccy/go-json` for marshal/unmarshal | Performance | 30 min | 2–3× JSON perf across all endpoints | PERSIST_AUDIT §8 |
| L4-025 | P2 | Pre-serialise SSE payload with 500ms TTL cache | SSE | 2 hr | Eliminates per-subscriber JSON marshal (N subscribers × marshal → 1 marshal) | PERSIST_AUDIT §9 |
| L4-026 | P3 | Lock-free ring buffer for `taskResults` | Performance | 3 hr | Eliminates `trMu` entirely | PERSIST_AUDIT §11 |

### 3.3 Worker Scheduling (from WORKER_SCHEDULING_AUDIT_S1)

| ID | Priority | Item | Area | Effort | Impact | Source |
|----|----------|------|------|--------|--------|--------|
| L4-030 | P1 | **Weighted queue depth** — route tasks by estimated duration, not just count | Scheduling | 2 hr | 2.7× throughput for mixed workloads (audit-measured) | SCHED_AUDIT §1 |
| L4-031 | P1 | Circuit breaker task re-queue — failed tasks re-enter queue instead of being silently dropped | Scheduling | 1 hr | Zero task loss (currently tasks are dropped on circuit break) | SCHED_AUDIT §3 |
| L4-032 | P1 | Unify dispatch routing across `/dispatch`, `/orchestrate`, `/pipeline` | Scheduling | 2 hr | All 3 endpoints use consistent queue-depth tiebreaker (currently only `/dispatch` does) | SCHED_AUDIT Appendix B |
| L4-033 | P2 | Task profile registry — type-aware retry counts and timeouts | Scheduling | 3 hr | Type-proportional distribution; 50ms shell tasks don't get 120s copilot retry logic | SCHED_AUDIT §4 |
| L4-034 | P2 | Sliding window circuit breaker — replace fixed 3-fail threshold with exponential cooldown | Scheduling | 3 hr | Adaptive recovery; current fixed threshold causes 30s unnecessary downtime on transient faults | SCHED_AUDIT §3 |
| L4-035 | P3 | **Chase-Lev deque + work-stealing** — idle workers steal from peers | Scheduling | 15 hr | ~40% latency reduction for bursty workloads; largest single change in backlog | SCHED_AUDIT §2 |

### 3.4 WebSocket Security (from WEBSOCKET_CV_S4, WEBSOCKET_SECURITY_AUDIT_S1)

| ID | Priority | Item | Area | Effort | Impact | Source |
|----|----------|------|------|--------|--------|--------|
| L4-040 | P2 | Add `Sec-WebSocket-Key` validation in upgrade handler | WebSocket | 30 min | Defense-in-depth; reject non-browser clients sending garbage keys | WS_CV §CV-S4-MED-001 |
| L4-041 | P2 | Install gcc for `-race` detector on Windows CI | Testing | 1 hr | Enables data race detection across entire test suite | FINAL_SCORECARD §6 |
| L4-042 | P2 | Measure actual work-stealing perf impact vs Beta's "+136%" claim | Validation | 2 hr | Data-driven decision on whether L4-035 is worth the complexity | FINAL_SCORECARD §6 |

### 3.5 Research-Grade (from audit appendices, not yet validated)

These items require further investigation before commitment:

| ID | Priority | Item | Area | Effort | Risk | Source |
|----|----------|------|------|--------|------|--------|
| L4-050 | P3 | **Gossip protocol** for multi-node bus federation | Bus | 40+ hr | HIGH — requires distributed consensus, network partition handling | Research backlog |
| L4-051 | P3 | **Swiss Tables** migration for hot maps | Performance | 8 hr | MEDIUM — Go's built-in map is already Swiss-table-based since Go 1.24 | Research backlog |
| L4-052 | P3 | **sync.Map bifurcation** — split read-heavy vs write-heavy maps | Performance | 4 hr | LOW — marginal gain over current RWMutex patterns for localhost-only traffic | PERSIST_AUDIT §6.3 |
| L4-053 | P3 | Full MPMC ring with sequence barriers | Ring Buffer | 20 hr | MEDIUM — overkill for current ~10 msg/min throughput | RING_BUFFER_AUDIT Appendix B |

---

## 4. Dependencies

```
L4-001 ✅ ─────────────────────────────────────────────────────┐
L4-002 ──┐                                                     │
L4-003 ──┼── Immediate fixes (prerequisite for all P1+ work) ──┤
L4-004 ──┤                                                     │
L4-005 ──┘                                                     │
                                                                ▼
L4-014 (benchmarks) ──► L4-012 (SPSC ring) ──► L4-013 (MPMC)
                    ──► L4-026 (taskResults ring)
                    ──► L4-042 (work-stealing measurement)

L4-020 (WAL) ──────────► standalone, no deps
L4-021 (token bucket) ─► L4-022 (spam sync.Map) — same pattern, do together

L4-030 (weighted queue) ──► L4-032 (unify routing) ──► L4-033 (task profiles)
L4-031 (re-queue) ────────► L4-034 (sliding window CB) ──► L4-035 (work-stealing)

L4-041 (gcc for -race) ──► enables race detection for ALL concurrent changes above
```

---

## 5. Implementation Waves

### Wave L4-A: Correctness & Foundations (est. ~12 hr total)

| Item | Worker | Effort |
|------|--------|--------|
| L4-002 WS TOCTOU fix | Delta | 5 min |
| L4-003 appendBrainInbox error handling | Gamma | 3 min |
| L4-004 Unmasked frame logging | Delta | 2 min |
| L4-005 Doc fixes | Beta | 10 min |
| L4-011 Clear() TOCTOU | Alpha | 30 min |
| L4-010 Unsubscribe() | Alpha | 1 hr |
| L4-014 Ring benchmarks | Alpha | 2 hr |
| L4-031 Circuit breaker re-queue | Beta | 1 hr |
| L4-030 Weighted queue depth | Beta | 2 hr |
| L4-032 Unify dispatch routing | Beta | 2 hr |
| L4-041 Install gcc for race detector | Any | 1 hr |

### Wave L4-B: Performance & Durability (est. ~15 hr total)

| Item | Worker | Effort |
|------|--------|--------|
| L4-020 WAL implementation | Gamma | 4 hr |
| L4-021 + L4-022 Lock-free rate+spam | Gamma | 3 hr |
| L4-024 goccy/go-json | Any | 30 min |
| L4-025 SSE payload cache | Alpha | 2 hr |
| L4-033 Task profile registry | Beta | 3 hr |
| L4-034 Sliding window CB | Beta | 3 hr |

### Wave L4-C: Advanced (est. ~20+ hr, gated on L4-B results)

| Item | Worker | Effort |
|------|--------|--------|
| L4-012 Lock-free SPSC ring | Alpha | 4 hr |
| L4-035 Chase-Lev work-stealing | Beta | 15 hr |
| L4-026 Lock-free taskResults | Alpha | 3 hr |

### Wave L4-R: Research (not scheduled, requires investigation)

L4-050 Gossip, L4-051 Swiss Tables, L4-052 sync.Map bifurcation, L4-053 Full MPMC.

---

## 6. Success Criteria

| Criterion | Metric | Target |
|-----------|--------|--------|
| Correctness | All P0–P4 immediate fixes closed | 5/5 |
| Test coverage | `-race` flag enabled, zero race conditions | 0 races |
| Bus durability | WAL survives process restart; zero message loss on clean shutdown | 100% recovery |
| Scheduling fairness | Weighted queue depth variance across workers | < 20% deviation |
| Task safety | Circuit breaker re-queue: zero dropped tasks | 0 drops |
| Ring throughput | Benchmark baseline established, SPSC path ≥ 5× current | measurable |
| Cross-validation | Every MODERATE+ change cross-validated by different worker | 100% |
| Score trajectory | All workers maintain positive scores through L4 | ≥ 0.0 each |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| WAL adds complexity with minimal throughput gain at current scale (~10 msg/min) | MEDIUM | LOW | WAL is for durability, not speed. Justified by ring buffer's volatile nature (100-msg FIFO, crash = total loss). |
| Work-stealing (L4-035) is largest change in backlog (~500 lines) | HIGH | HIGH | Gate on L4-042 benchmark data. If measured gain < 20%, defer to Level 5. |
| Swiss Tables (L4-051) may be no-op on Go 1.24+ (native Swiss tables) | HIGH | NONE | Research first. If Go runtime already uses Swiss tables, close as WON'T FIX. |
| gcc installation on Windows for `-race` may conflict with MSVC | LOW | MEDIUM | Use MinGW-w64; test in isolated env first. |

---

## 8. Sprint Doc Verification (Level 3.5)

All 15 sprint documents verified non-empty with correct `signed:` tags:

| Document | Size | Signed |
|----------|------|--------|
| RING_BUFFER_AUDIT_S1.md | 38,430 B | ✅ alpha |
| RING_BUFFER_IMPL_S2.md | 7,240 B | ✅ alpha |
| RING_BUFFER_CV_S4.md | 11,241 B | ✅ |
| WEBSOCKET_SECURITY_AUDIT_S1.md | 13,539 B | ✅ |
| WEBSOCKET_SECURITY_IMPL_S2.md | 6,711 B | ✅ |
| WEBSOCKET_CV_S4.md | 11,340 B | ✅ alpha |
| WORKER_SCHEDULING_AUDIT_S1.md | 36,246 B | ✅ |
| WORKER_SCHEDULING_IMPL_S2.md | 7,550 B | ✅ |
| WORKER_SCHEDULING_CV_S4.md | 8,620 B | ✅ |
| PERSISTENCE_RATELIMIT_AUDIT_S1.md | 34,111 B | ✅ |
| PERSISTENCE_RATELIMIT_IMPL_S2.md | 6,745 B | ✅ |
| PERSISTENCE_RATELIMIT_CV_S4.md | 8,289 B | ✅ |
| DOCUMENTATION_AUDIT_S1.md | 14,251 B | ✅ |
| DOCUMENTATION_FINAL_S5.md | 11,447 B | ✅ |
| FINAL_SCORECARD_S5.md | 12,754 B | ✅ delta |

**SKYNET_L4_ROADMAP.md** was 0 bytes — now populated (this document).

---

<!-- signed: alpha -->