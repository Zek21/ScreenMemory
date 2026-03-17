#!/usr/bin/env python3
"""
skynet_knowledge_distill_daemon.py -- Knowledge Distillation Background Daemon.

Continuously converts episodic memories into durable semantic knowledge.
Every cycle (default 60s), scans the LearningStore for recent episodic-style
entries, consolidates related facts into semantic patterns, and prunes stale
entries that have already been distilled.

This daemon bridges two subsystems:
  - core/cognitive/knowledge_distill.py  (KnowledgeDistiller engine)
  - core/learning_store.py              (LearningStore persistence)

Workflow per cycle:
  1. Scan LearningStore for low-confidence or high-contradiction entries
  2. Group related entries by category/tag overlap
  3. Consolidate clusters into higher-confidence pattern facts
  4. Prune stale episodic entries older than 24h that have been distilled
  5. Publish distillation stats to bus (topic=knowledge, type=distill_stats)
  6. Broadcast new patterns via skynet_knowledge for peer absorption

Singleton enforcement via PID file. Graceful shutdown on SIGTERM/SIGBREAK.

Usage:
    python tools/skynet_knowledge_distill_daemon.py start     # Run as daemon
    python tools/skynet_knowledge_distill_daemon.py --status  # Show stats
    python tools/skynet_knowledge_distill_daemon.py --run-once # Single pass
    python tools/skynet_knowledge_distill_daemon.py --prune   # Manual cleanup

# signed: gamma
"""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "knowledge_distill.pid"
STATS_FILE = DATA_DIR / "knowledge_distill_stats.json"

# Cycle configuration
DEFAULT_CYCLE_INTERVAL = 60   # seconds between distillation cycles
STALE_AGE_HOURS = 24          # entries older than this eligible for pruning
MIN_CLUSTER_SIZE = 3          # minimum related entries to form a pattern
CONFIDENCE_THRESHOLD = 0.4    # entries below this confidence are candidates
SIMILARITY_THRESHOLD = 0.5    # word overlap ratio to consider entries related
MAX_PRUNE_PER_CYCLE = 50      # cap on entries pruned per cycle

# Ensure UTF-8 output on Windows  # signed: gamma
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    """Timestamped daemon log output."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [DISTILL-DAEMON] [{level}] {msg}", flush=True)


# ── Bus Integration ──────────────────────────────────────────────────────────

def _bus_publish(message: dict) -> bool:
    """Publish to Skynet bus via SpamGuard. Returns True on success."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(message)
        return bool(result and result.get("allowed", False))
    except ImportError:
        try:
            import urllib.request
            payload = json.dumps(message).encode()
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish", payload,
                {"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception:
            return False
    except Exception:
        return False
    # signed: gamma


def _broadcast_pattern(pattern_content: str, category: str, tags: List[str]):
    """Broadcast a newly distilled pattern via skynet_knowledge."""
    try:
        from tools.skynet_knowledge import broadcast_learning
        broadcast_learning("distill_daemon", pattern_content, category, tags)
    except Exception as e:
        log(f"Failed to broadcast pattern: {e}", "WARN")
    # signed: gamma


# ── PID / Singleton ─────────────────────────────────────────────────────────

def _write_pid():
    """Write current PID to file for singleton enforcement."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"PID {os.getpid()} written to {PID_FILE}")
    # signed: gamma


def _read_pid() -> Optional[int]:
    """Read PID from file, return None if missing or invalid."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
    # signed: gamma


def _check_singleton() -> bool:
    """Return True if no other instance is running. Clean stale PID if needed."""
    existing = _read_pid()
    if existing is None:
        return True
    if existing == os.getpid():
        return True
    if _pid_alive(existing):
        return False
    log(f"Stale PID {existing} found, cleaning up", "WARN")
    PID_FILE.unlink(missing_ok=True)
    return True
    # signed: gamma


def _cleanup_pid():
    """Remove PID file on shutdown."""
    try:
        if PID_FILE.exists():
            stored = _read_pid()
            if stored == os.getpid():
                PID_FILE.unlink(missing_ok=True)
                log("PID file cleaned up")
    except Exception:
        pass
    # signed: gamma


# ── Stats Persistence ────────────────────────────────────────────────────────

class DistillStats:
    """Tracks and persists distillation statistics."""

    def __init__(self):
        self.cycles_completed: int = 0
        self.episodes_processed: int = 0
        self.patterns_created: int = 0
        self.pruned_count: int = 0
        self.last_cycle_time: Optional[str] = None
        self.last_cycle_duration_ms: float = 0.0
        self.total_consolidations: int = 0
        self.start_time: str = datetime.now().isoformat()
        self._load()

    def _load(self):
        """Load stats from disk if available."""
        if STATS_FILE.exists():
            try:
                data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                self.cycles_completed = data.get("cycles_completed", 0)
                self.episodes_processed = data.get("episodes_processed", 0)
                self.patterns_created = data.get("patterns_created", 0)
                self.pruned_count = data.get("pruned_count", 0)
                self.last_cycle_time = data.get("last_cycle_time")
                self.last_cycle_duration_ms = data.get("last_cycle_duration_ms", 0.0)
                self.total_consolidations = data.get("total_consolidations", 0)
                self.start_time = data.get("start_time", self.start_time)
            except (json.JSONDecodeError, OSError):
                pass
        # signed: gamma

    def save(self):
        """Persist stats to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "cycles_completed": self.cycles_completed,
            "episodes_processed": self.episodes_processed,
            "patterns_created": self.patterns_created,
            "pruned_count": self.pruned_count,
            "last_cycle_time": self.last_cycle_time,
            "last_cycle_duration_ms": self.last_cycle_duration_ms,
            "total_consolidations": self.total_consolidations,
            "start_time": self.start_time,
            "pid": os.getpid(),
        }
        STATS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # signed: gamma

    def to_dict(self) -> dict:
        """Return stats as a dictionary for bus publishing."""
        return {
            "cycles_completed": self.cycles_completed,
            "episodes_processed": self.episodes_processed,
            "patterns_created": self.patterns_created,
            "pruned_count": self.pruned_count,
            "last_cycle_time": self.last_cycle_time,
            "last_cycle_duration_ms": round(self.last_cycle_duration_ms, 1),
            "total_consolidations": self.total_consolidations,
            "uptime_s": round(time.time() - time.mktime(
                datetime.fromisoformat(self.start_time).timetuple()), 0)
            if self.start_time else 0,
        }
        # signed: gamma


# ── Distillation Engine ──────────────────────────────────────────────────────

def _get_learning_store():
    """Lazy-load LearningStore with graceful fallback."""
    try:
        from core.learning_store import LearningStore
        return LearningStore()
    except Exception as e:
        log(f"LearningStore unavailable: {e}", "ERROR")
        return None
    # signed: gamma


def _tokenize(text: str) -> set:
    """Simple whitespace tokenizer with stopword removal."""
    stopwords = {
        "the", "a", "an", "is", "was", "are", "were", "to", "for",
        "in", "on", "at", "of", "and", "or", "but", "with", "from",
        "by", "as", "it", "this", "that", "i", "my", "be", "has",
        "had", "have", "not", "no", "do", "does", "did"
    }
    words = set()
    for w in text.lower().split():
        cleaned = w.strip(".,!?;:'\"()[]{}#-_")
        if cleaned and len(cleaned) > 2 and cleaned not in stopwords:
            words.add(cleaned)
    return words
    # signed: gamma


def _word_overlap(text_a: str, text_b: str) -> float:
    """Calculate Jaccard-like word overlap between two texts."""
    words_a = _tokenize(text_a)
    words_b = _tokenize(text_b)
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0
    # signed: gamma


def _scan_episodic_candidates(store) -> List[dict]:
    """
    Scan LearningStore for entries suitable for distillation.

    Candidates are low-confidence entries or those with high contradiction
    counts that indicate episodic/noisy data worth consolidating.
    """
    candidates = []
    try:
        db_path = store.db_path
        with sqlite3.connect(db_path, check_same_thread=False) as conn:
            cursor = conn.execute("""
                SELECT fact_id, content, category, confidence,
                       reinforcement_count, contradiction_count,
                       first_learned, last_accessed, tags
                FROM learned_facts
                WHERE confidence < ? OR contradiction_count > reinforcement_count
                ORDER BY confidence ASC
                LIMIT 200
            """, (CONFIDENCE_THRESHOLD,))
            for row in cursor.fetchall():
                try:
                    tags = json.loads(row[8]) if row[8] else []
                except (json.JSONDecodeError, TypeError):
                    tags = []
                candidates.append({
                    "fact_id": row[0],
                    "content": row[1],
                    "category": row[2],
                    "confidence": row[3],
                    "reinforcement_count": row[4],
                    "contradiction_count": row[5],
                    "first_learned": row[6],
                    "last_accessed": row[7],
                    "tags": tags,
                })
    except Exception as e:
        log(f"Error scanning candidates: {e}", "ERROR")
    return candidates
    # signed: gamma


def _cluster_candidates(candidates: List[dict]) -> Dict[str, List[dict]]:
    """
    Group related candidates by category + word overlap.

    Uses a greedy union approach: for each candidate, try to add it to an
    existing cluster where it has sufficient word overlap with any member.
    Falls back to category-based grouping.
    """
    clusters: Dict[str, List[dict]] = defaultdict(list)

    for candidate in candidates:
        placed = False
        # Try to find an existing cluster with word overlap
        for cluster_key, members in clusters.items():
            for member in members[:5]:  # only check first 5 for perf
                if _word_overlap(candidate["content"], member["content"]) >= SIMILARITY_THRESHOLD:
                    clusters[cluster_key].append(candidate)
                    placed = True
                    break
            if placed:
                break

        if not placed:
            # Create new cluster keyed by category + first tag
            tags = candidate.get("tags", [])
            key = candidate["category"]
            if tags:
                key = f"{key}:{tags[0]}"
            clusters[key].append(candidate)

    return dict(clusters)
    # signed: gamma


def _synthesize_pattern(cluster_key: str, entries: List[dict]) -> Tuple[str, str, List[str]]:
    """
    Synthesize a pattern summary from a cluster of related entries.

    Returns (pattern_content, category, tags).
    """
    # Collect all unique words for keyword extraction
    all_words: Dict[str, int] = defaultdict(int)
    all_tags: set = set()
    categories: Dict[str, int] = defaultdict(int)

    for entry in entries:
        for word in _tokenize(entry["content"]):
            all_words[word] += 1
        all_tags.update(entry.get("tags", []))
        categories[entry["category"]] += 1

    # Top keywords by frequency
    top_keywords = sorted(all_words.items(), key=lambda x: x[1], reverse=True)[:8]
    keywords_str = ", ".join(k for k, _ in top_keywords)

    # Dominant category
    category = max(categories, key=categories.get) if categories else "pattern"

    # Build pattern description
    pattern_content = (
        f"[Distilled pattern: {cluster_key}] "
        f"Consolidated from {len(entries)} related entries. "
        f"Key concepts: {keywords_str}. "
        f"Category: {category}."
    )

    # Try LLM-based summarization if KnowledgeDistiller is available
    try:
        from core.cognitive.knowledge_distill import KnowledgeDistiller
        distiller = KnowledgeDistiller(decay_threshold=CONFIDENCE_THRESHOLD)
        content_list = [e["content"][:200] for e in entries[:10]]
        combined = "\n".join(f"- {c}" for c in content_list)
        rule_summary = distiller._rule_summarize(cluster_key, _FakeEntryList(entries))
        if rule_summary and len(rule_summary) > len(pattern_content):
            pattern_content = rule_summary
    except Exception:
        pass  # LLM or engine not available, use rule-based

    tags = list(all_tags)[:10]
    if "distilled" not in tags:
        tags.append("distilled")

    return pattern_content, category, tags
    # signed: gamma


class _FakeEntry:
    """Minimal entry-like object for KnowledgeDistiller._rule_summarize compatibility."""
    def __init__(self, d: dict):
        self.content = d.get("content", "")
        self.tags = d.get("tags", [])
        self.source_action = d.get("category", "")


class _FakeEntryList(list):
    """Wrap dicts as fake entry objects for _rule_summarize."""
    def __init__(self, entries: List[dict]):
        super().__init__(_FakeEntry(e) for e in entries)


def _prune_stale_entries(store, already_distilled_ids: set) -> int:
    """
    Remove stale entries from LearningStore that are old and low-value.

    Targets entries that:
      - Are older than STALE_AGE_HOURS
      - Have low confidence (< CONFIDENCE_THRESHOLD)
      - Have already been distilled (in already_distilled_ids set)
    """
    pruned = 0
    cutoff = (datetime.now() - timedelta(hours=STALE_AGE_HOURS)).isoformat()
    try:
        db_path = store.db_path
        with sqlite3.connect(db_path, check_same_thread=False) as conn:
            # Only prune entries that are old AND low confidence AND distilled
            if already_distilled_ids:
                placeholders = ",".join("?" for _ in already_distilled_ids)
                cursor = conn.execute(f"""
                    DELETE FROM learned_facts
                    WHERE fact_id IN ({placeholders})
                      AND confidence < ?
                      AND first_learned < ?
                """, list(already_distilled_ids) + [CONFIDENCE_THRESHOLD, cutoff])
                pruned = cursor.rowcount
                conn.commit()
    except Exception as e:
        log(f"Error pruning stale entries: {e}", "ERROR")
    return min(pruned, MAX_PRUNE_PER_CYCLE)
    # signed: gamma


def run_distillation_cycle(store, stats: DistillStats) -> dict:
    """
    Execute a single distillation cycle.

    Returns a summary dict of what happened.
    """
    cycle_start = time.perf_counter()
    result = {
        "candidates_found": 0,
        "clusters_formed": 0,
        "patterns_created": 0,
        "entries_consolidated": 0,
        "entries_pruned": 0,
        "consolidations_run": 0,
    }

    # Step 1: Scan for candidates
    candidates = _scan_episodic_candidates(store)
    result["candidates_found"] = len(candidates)
    if not candidates:
        log("No distillation candidates found")
        return result

    log(f"Found {len(candidates)} distillation candidates")

    # Step 2: Cluster related entries
    clusters = _cluster_candidates(candidates)
    result["clusters_formed"] = len(clusters)
    log(f"Formed {len(clusters)} clusters")

    # Step 3: Synthesize patterns from clusters meeting minimum size
    distilled_ids: set = set()
    patterns_created = 0

    for cluster_key, entries in clusters.items():
        if len(entries) < MIN_CLUSTER_SIZE:
            continue

        pattern_content, category, tags = _synthesize_pattern(cluster_key, entries)

        # Store the distilled pattern as a new high-confidence fact
        try:
            fact_id = store.learn(
                content=pattern_content,
                category="pattern",
                source="distill_daemon",
                tags=tags,
            )
            # Boost confidence of the new pattern fact
            store.reinforce(fact_id)
            store.reinforce(fact_id)  # double reinforce for distilled patterns

            patterns_created += 1
            distilled_ids.update(e["fact_id"] for e in entries)

            # Broadcast the new pattern
            _broadcast_pattern(pattern_content, category, tags)

            log(f"Created pattern '{cluster_key}' from {len(entries)} entries")
        except Exception as e:
            log(f"Failed to store pattern '{cluster_key}': {e}", "ERROR")

    result["patterns_created"] = patterns_created
    result["entries_consolidated"] = len(distilled_ids)

    # Step 4: Run LearningStore's built-in consolidation (merges similar facts)
    try:
        merged = store.consolidate()
        result["consolidations_run"] = merged
        if merged > 0:
            log(f"Consolidated {merged} similar facts via LearningStore.consolidate()")
    except Exception as e:
        log(f"Consolidation error: {e}", "WARN")

    # Step 5: Prune stale entries that were distilled
    pruned = _prune_stale_entries(store, distilled_ids)
    result["entries_pruned"] = pruned
    if pruned > 0:
        log(f"Pruned {pruned} stale entries")

    # Update stats
    elapsed_ms = (time.perf_counter() - cycle_start) * 1000
    stats.cycles_completed += 1
    stats.episodes_processed += len(distilled_ids)
    stats.patterns_created += patterns_created
    stats.pruned_count += pruned
    stats.total_consolidations += result["consolidations_run"]
    stats.last_cycle_time = datetime.now().isoformat()
    stats.last_cycle_duration_ms = elapsed_ms
    stats.save()

    result["elapsed_ms"] = round(elapsed_ms, 1)
    log(f"Cycle complete: {patterns_created} patterns, "
        f"{len(distilled_ids)} entries processed, "
        f"{pruned} pruned ({elapsed_ms:.0f}ms)")

    return result
    # signed: gamma


# ── Daemon Loop ──────────────────────────────────────────────────────────────

class KnowledgeDistillDaemon:
    """Background daemon that runs distillation cycles periodically."""

    def __init__(self, interval: int = DEFAULT_CYCLE_INTERVAL):
        self.interval = interval
        self.running = False
        self.stats = DistillStats()
        self._setup_signals()

    def _setup_signals(self):
        """Register signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        if sys.platform == "win32":
            try:
                signal.signal(signal.SIGBREAK, self._handle_signal)
            except (AttributeError, OSError):
                pass
        # signed: gamma

    def _handle_signal(self, signum, frame):
        """Graceful shutdown on signal."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        log(f"Received {sig_name}, shutting down gracefully")
        self.running = False
        # signed: gamma

    def start(self):
        """Main daemon loop."""
        if not _check_singleton():
            existing = _read_pid()
            log(f"Another instance running (PID {existing}), exiting", "ERROR")
            sys.exit(1)

        _write_pid()
        self.running = True
        log(f"Knowledge Distillation Daemon started (PID {os.getpid()}, "
            f"interval={self.interval}s)")

        # Announce on bus
        _bus_publish({
            "sender": "distill_daemon",
            "topic": "system",
            "type": "daemon_start",
            "content": json.dumps({
                "daemon": "knowledge_distill",
                "pid": os.getpid(),
                "interval_s": self.interval,
            }),
        })

        try:
            while self.running:
                self._run_cycle()
                # Sleep in small increments for responsive shutdown
                for _ in range(self.interval * 2):
                    if not self.running:
                        break
                    time.sleep(0.5)
        except KeyboardInterrupt:
            log("KeyboardInterrupt received")
        finally:
            self._shutdown()

    def _run_cycle(self):
        """Execute one distillation cycle with error recovery."""
        store = _get_learning_store()
        if store is None:
            log("LearningStore unavailable, skipping cycle", "WARN")
            return

        try:
            result = run_distillation_cycle(store, self.stats)

            # Publish stats to bus (only if something happened)
            if result.get("patterns_created", 0) > 0 or result.get("entries_pruned", 0) > 0:
                _bus_publish({
                    "sender": "distill_daemon",
                    "topic": "knowledge",
                    "type": "distill_stats",
                    "content": json.dumps(self.stats.to_dict()),
                })
        except Exception as e:
            log(f"Cycle error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
        # signed: gamma

    def _shutdown(self):
        """Clean shutdown: save stats, remove PID, announce."""
        self.stats.save()
        _cleanup_pid()
        _bus_publish({
            "sender": "distill_daemon",
            "topic": "system",
            "type": "daemon_stop",
            "content": json.dumps({
                "daemon": "knowledge_distill",
                "cycles_completed": self.stats.cycles_completed,
            }),
        })
        log("Daemon stopped")
        # signed: gamma


# ── CLI Commands ─────────────────────────────────────────────────────────────

def cmd_status():
    """Show daemon status and distillation stats."""
    pid = _read_pid()
    alive = _pid_alive(pid) if pid else False

    print("=== Knowledge Distillation Daemon Status ===")
    print(f"  PID:     {pid or 'N/A'}")
    print(f"  Status:  {'RUNNING' if alive else 'STOPPED'}")

    if STATS_FILE.exists():
        try:
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            print(f"  Cycles:  {data.get('cycles_completed', 0)}")
            print(f"  Episodes processed: {data.get('episodes_processed', 0)}")
            print(f"  Patterns created:   {data.get('patterns_created', 0)}")
            print(f"  Entries pruned:      {data.get('pruned_count', 0)}")
            print(f"  Consolidations:      {data.get('total_consolidations', 0)}")
            print(f"  Last cycle:          {data.get('last_cycle_time', 'never')}")
            print(f"  Last duration:       {data.get('last_cycle_duration_ms', 0):.1f}ms")
        except (json.JSONDecodeError, OSError):
            print("  Stats file corrupted")
    else:
        print("  No stats recorded yet")

    # Show LearningStore stats if available
    store = _get_learning_store()
    if store:
        try:
            ls_stats = store.stats()
            print(f"\n  LearningStore:")
            print(f"    Total facts:      {ls_stats.get('total_facts', 0)}")
            print(f"    Avg confidence:   {ls_stats.get('average_confidence', 0):.3f}")
            cats = ls_stats.get("by_category", {})
            if cats:
                print(f"    Categories:       {', '.join(f'{k}={v}' for k, v in cats.items())}")
        except Exception:
            pass
    # signed: gamma


def cmd_run_once():
    """Run a single distillation pass without entering daemon mode."""
    log("Running single distillation pass")
    store = _get_learning_store()
    if store is None:
        log("LearningStore unavailable", "ERROR")
        sys.exit(1)

    stats = DistillStats()
    result = run_distillation_cycle(store, stats)
    print(json.dumps(result, indent=2))
    # signed: gamma


def cmd_prune():
    """Manually prune stale entries from LearningStore."""
    log("Running manual prune")
    store = _get_learning_store()
    if store is None:
        log("LearningStore unavailable", "ERROR")
        sys.exit(1)

    # Prune entries below confidence threshold that are old
    cutoff = (datetime.now() - timedelta(hours=STALE_AGE_HOURS)).isoformat()
    try:
        with sqlite3.connect(store.db_path, check_same_thread=False) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM learned_facts
                WHERE confidence < ? AND first_learned < ?
            """, (CONFIDENCE_THRESHOLD, cutoff))
            count = cursor.fetchone()[0]
            print(f"Found {count} stale entries (confidence < {CONFIDENCE_THRESHOLD}, "
                  f"older than {STALE_AGE_HOURS}h)")

            if count > 0:
                conn.execute("""
                    DELETE FROM learned_facts
                    WHERE confidence < ? AND first_learned < ?
                """, (CONFIDENCE_THRESHOLD, cutoff))
                conn.commit()
                print(f"Pruned {count} entries")

            # Also run consolidation
            merged = store.consolidate()
            if merged > 0:
                print(f"Consolidated {merged} similar facts")

        # Run forget for low-confidence contradicted facts
        forgotten = store.forget(min_confidence=0.15)
        if forgotten > 0:
            print(f"Forgot {forgotten} low-confidence contradicted facts")

    except Exception as e:
        log(f"Prune error: {e}", "ERROR")
        sys.exit(1)
    # signed: gamma


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Knowledge Distillation Daemon -- converts episodic memories "
                    "into durable semantic knowledge"
    )
    parser.add_argument("command", nargs="?", default=None,
                        choices=["start"],
                        help="Daemon command (start)")
    parser.add_argument("--status", action="store_true",
                        help="Show daemon status and stats")
    parser.add_argument("--run-once", action="store_true",
                        help="Run a single distillation pass")
    parser.add_argument("--prune", action="store_true",
                        help="Manually prune stale entries")
    parser.add_argument("--interval", type=int, default=DEFAULT_CYCLE_INTERVAL,
                        help=f"Cycle interval in seconds (default: {DEFAULT_CYCLE_INTERVAL})")

    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.run_once:
        cmd_run_once()
    elif args.prune:
        cmd_prune()
    elif args.command == "start":
        daemon = KnowledgeDistillDaemon(interval=args.interval)
        daemon.start()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
# signed: gamma
