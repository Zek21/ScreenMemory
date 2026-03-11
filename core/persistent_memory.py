"""
Persistent Episodic Memory — SQLite-backed persistence for cognitive/memory.py.

Bridges the in-memory EpisodicMemory system to durable SQLite storage so that
knowledge survives session restarts. On session end, flush all memories to disk.
On boot, reload top-K memories by utility score with time-decay applied.

Database: data/episodic_memory.db
Tables: episodes, semantics, sessions, consolidation_log

Usage:
    from core.persistent_memory import PersistentMemoryStore

    store = PersistentMemoryStore()
    store.save_session("session_001", episodic_memory_instance)
    entries = store.load_session("session_001", top_k=50)
    results = store.recall("dispatch failure", top_k=10)
    store.consolidate()
"""

import json
import math
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "episodic_memory.db"

# Decay constants
DECAY_HALF_LIFE_HOURS = 72  # memories lose half their utility after 72 hours
CONSOLIDATION_THRESHOLD = 3  # how many times a pattern must appear to become semantic


class PersistentMemoryStore:
    """SQLite-backed persistence layer for episodic and semantic memories."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:  # signed: alpha
        if self._conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA cache_size=-8192")  # 8MB cache
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, content TEXT NOT NULL,
            context_json TEXT DEFAULT '{}', timestamp REAL NOT NULL,
            last_accessed REAL NOT NULL, access_count INTEGER DEFAULT 1,
            utility_score REAL DEFAULT 1.0, decay_rate REAL DEFAULT 0.05,
            importance REAL DEFAULT 0.5, tags_json TEXT DEFAULT '[]',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantics (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            context_json TEXT DEFAULT '{}', timestamp REAL NOT NULL,
            last_accessed REAL NOT NULL, access_count INTEGER DEFAULT 1,
            utility_score REAL DEFAULT 1.0, decay_rate REAL DEFAULT 0.01,
            importance REAL DEFAULT 0.7, tags_json TEXT DEFAULT '[]',
            source_episode_ids TEXT DEFAULT '[]', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, start_time REAL NOT NULL,
            end_time REAL, episode_count INTEGER DEFAULT 0,
            semantic_count INTEGER DEFAULT 0, metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS consolidation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
            source_episode_ids TEXT NOT NULL, created_semantic_id TEXT NOT NULL,
            pattern_text TEXT NOT NULL, confidence REAL DEFAULT 0.5
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
        CREATE INDEX IF NOT EXISTS idx_episodes_utility ON episodes(utility_score DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_semantics_utility ON semantics(utility_score DESC);
        CREATE INDEX IF NOT EXISTS idx_semantics_timestamp ON semantics(timestamp DESC);
    """

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript(self._SCHEMA_SQL)
        conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Session Management ────────────────────────────────────────────

    def save_session(self, session_id: str, episodic_memory) -> dict:
        """Flush an in-memory EpisodicMemory instance to SQLite.

        Args:
            session_id: Unique session identifier.
            episodic_memory: An EpisodicMemory instance from core/cognitive/memory.py.

        Returns:
            Dict with counts of saved episodes and semantics.
        """
        conn = self._get_conn()
        now = time.time()
        ep_count = 0
        sem_count = 0

        # Save episodic memories (attribute may be _episodic or episodic_memory)
        # Use `is None` check to avoid falsiness of empty lists  # signed: alpha
        episodic_list = getattr(episodic_memory, "_episodic", None)
        if episodic_list is None:
            episodic_list = getattr(episodic_memory, "episodic_memory", [])
        for entry in episodic_list:
            self._upsert_episode(conn, session_id, entry)
            ep_count += 1

        # Save working memory as episodic (it's volatile but worth preserving)
        working_list = getattr(episodic_memory, "_working", None)
        if working_list is None:
            working_list = getattr(episodic_memory, "working_memory", [])
        for entry in working_list:
            self._upsert_episode(conn, session_id, entry)
            ep_count += 1

        # Save semantic memories
        semantic_list = getattr(episodic_memory, "_semantic", None)
        if semantic_list is None:
            semantic_list = getattr(episodic_memory, "semantic_memory", [])
        for entry in semantic_list:
            self._upsert_semantic(conn, entry)
            sem_count += 1

        # Record session
        conn.execute("""
            INSERT OR REPLACE INTO sessions (session_id, start_time, end_time, episode_count, semantic_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, now, now, ep_count, sem_count, "{}"))

        conn.commit()
        return {"session_id": session_id, "episodes_saved": ep_count, "semantics_saved": sem_count}

    def load_session(self, session_id: Optional[str] = None, top_k: int = 50) -> List[dict]:
        """Load memories from a specific session or latest, ranked by decayed utility.

        Args:
            session_id: Session to load from. If None, loads from all sessions.
            top_k: Maximum memories to return.

        Returns:
            List of memory dicts sorted by effective utility (highest first).
        """
        conn = self._get_conn()
        now = time.time()

        if session_id:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE session_id = ? ORDER BY utility_score DESC LIMIT ?",
                (session_id, top_k * 2)  # fetch extra to account for decay filtering
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY utility_score DESC LIMIT ?",
                (top_k * 2,)
            ).fetchall()

        # Also include semantics (cross-session knowledge)
        sem_rows = conn.execute(
            "SELECT * FROM semantics ORDER BY utility_score DESC LIMIT ?",
            (top_k,)
        ).fetchall()

        memories = []
        for row in rows:
            entry = self._row_to_dict(row, "episodic")
            entry["effective_utility"] = self._compute_effective_utility(entry, now)
            memories.append(entry)

        for row in sem_rows:
            entry = self._row_to_dict(row, "semantic")
            entry["effective_utility"] = self._compute_effective_utility(entry, now)
            memories.append(entry)

        # Sort by effective utility and return top_k
        memories.sort(key=lambda m: m["effective_utility"], reverse=True)
        return memories[:top_k]

    def recall(self, query: str, top_k: int = 10) -> List[dict]:
        """Search memories using BM25-style relevance scoring with time decay."""
        conn = self._get_conn()
        now = time.time()
        query_terms = set(query.lower().split())
        if not query_terms:
            return []

        all_rows = conn.execute("SELECT * FROM episodes").fetchall()
        all_rows += conn.execute("SELECT * FROM semantics").fetchall()

        scored = []
        for row in all_rows:
            entry = self._score_memory_row(row, query_terms, now)
            if entry:
                scored.append(entry)

        scored.sort(key=lambda m: m["combined_score"], reverse=True)
        return scored[:top_k]

    def _score_memory_row(self, row, query_terms, now):
        """Score a single memory row against query terms. Returns dict or None."""
        content = row["content"].lower()
        content_terms = content.split()
        if not content_terms:
            return None

        match_count = sum(1 for t in query_terms if t in content)
        if match_count == 0:
            return None

        tf = match_count / len(content_terms)
        k1 = 1.5
        relevance = (tf * (k1 + 1)) / (tf + k1) * (match_count / len(query_terms))

        entry = self._row_to_dict(row, "episodic" if "session_id" in row.keys() else "semantic")
        eff_util = self._compute_effective_utility(entry, now)
        combined = relevance * 0.6 + eff_util * 0.4
        entry["relevance_score"] = round(relevance, 4)
        entry["effective_utility"] = round(eff_util, 4)
        entry["combined_score"] = round(combined, 4)
        return entry

    def consolidate(self) -> dict:
        """Promote repeated episodic patterns to semantic memories."""
        conn = self._get_conn()
        now = time.time()
        episodes = conn.execute(
            "SELECT * FROM episodes ORDER BY timestamp DESC LIMIT 500"
        ).fetchall()

        promoted = 0
        seen_groups = []

        for i, ep in enumerate(episodes):
            words_i = set(ep["content"].lower().split())
            if len(words_i) < 3:
                continue
            group = self._find_overlap_group(ep, words_i, episodes, i)
            if len(group) < CONSOLIDATION_THRESHOLD:
                continue
            group_ids = sorted(set(g["id"] for g in group))
            group_key = ",".join(group_ids)
            if group_key in seen_groups:
                continue
            seen_groups.append(group_key)
            self._promote_to_semantic(conn, group, group_ids, now)
            promoted += 1

        conn.commit()
        return {"consolidated": promoted, "episodes_scanned": len(episodes)}

    @staticmethod
    def _find_overlap_group(ep, words_i, episodes, skip_idx):
        """Find episodes with 60%+ word overlap with the given episode."""
        group = [ep]
        for j, other in enumerate(episodes):
            if j == skip_idx:
                continue
            words_j = set(other["content"].lower().split())
            if len(words_j) < 3:
                continue
            overlap = len(words_i & words_j) / max(len(words_i | words_j), 1)
            if overlap >= 0.6:
                group.append(other)
        return group

    @staticmethod
    def _promote_to_semantic(conn, group, group_ids, now):
        """Create a semantic memory from an overlapping episode group."""
        semantic_id = str(uuid.uuid4())
        combined_content = f"[Consolidated from {len(group)} episodes] {group[0]['content']}"
        conn.execute("""
            INSERT OR IGNORE INTO semantics
            (id, content, context_json, timestamp, last_accessed, access_count,
             utility_score, decay_rate, importance, tags_json, source_episode_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            semantic_id, combined_content, "{}",
            now, now, len(group),
            min(2.0, 0.5 + 0.2 * len(group)), 0.01, 0.8,
            json.dumps(["consolidated"]), json.dumps(group_ids), now,
        ))
        conn.execute("""
            INSERT INTO consolidation_log (timestamp, source_episode_ids, created_semantic_id, pattern_text, confidence)
            VALUES (?, ?, ?, ?, ?)
        """, (now, json.dumps(group_ids), semantic_id, combined_content[:200], min(1.0, 0.3 + 0.15 * len(group))))

    def get_stats(self) -> dict:
        """Get statistics about the persistent memory store."""
        conn = self._get_conn()
        ep_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        sem_count = conn.execute("SELECT COUNT(*) FROM semantics").fetchone()[0]
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        consolidation_count = conn.execute("SELECT COUNT(*) FROM consolidation_log").fetchone()[0]

        db_size_mb = 0
        if self.db_path.exists():
            db_size_mb = round(self.db_path.stat().st_size / (1024 * 1024), 2)

        return {
            "episodes": ep_count,
            "semantics": sem_count,
            "sessions": session_count,
            "consolidations": consolidation_count,
            "db_size_mb": db_size_mb,
            "db_path": str(self.db_path),
        }

    def store_episode(self, session_id: str, content: str,
                      context: Optional[dict] = None, importance: float = 0.5,
                      tags: Optional[List[str]] = None) -> str:
        """Store a single episodic memory directly (without an EpisodicMemory instance).

        Args:
            session_id: Current session identifier.
            content: Memory content text.
            context: Optional context metadata.
            importance: Importance weight (0-1).
            tags: Optional list of tags.

        Returns:
            Memory ID.
        """
        conn = self._get_conn()
        now = time.time()
        mem_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO episodes (id, session_id, content, context_json, timestamp, last_accessed,
                                  access_count, utility_score, decay_rate, importance, tags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 1.0, 0.05, ?, ?, ?)
        """, (mem_id, session_id, content, json.dumps(context or {}),
              now, now, importance, json.dumps(tags or []), now))
        conn.commit()
        return mem_id

    def store_semantic(self, content: str, importance: float = 0.7,
                       tags: Optional[List[str]] = None) -> str:
        """Store a semantic (durable knowledge) memory directly.

        Args:
            content: Knowledge content text.
            importance: Importance weight (0-1).
            tags: Optional list of tags.

        Returns:
            Memory ID.
        """
        conn = self._get_conn()
        now = time.time()
        mem_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO semantics (id, content, context_json, timestamp, last_accessed,
                                   access_count, utility_score, decay_rate, importance,
                                   tags_json, source_episode_ids, created_at)
            VALUES (?, ?, '{}', ?, ?, 1, 1.0, 0.01, ?, ?, '[]', ?)
        """, (mem_id, content, now, now, importance, json.dumps(tags or []), now))
        conn.commit()
        return mem_id

    def prune(self, min_utility: float = 0.05, max_age_days: int = 90) -> int:
        """Remove low-utility and very old episodic memories.

        Args:
            min_utility: Minimum utility_score to keep.
            max_age_days: Maximum age in days before forced removal.

        Returns:
            Number of memories pruned.
        """
        conn = self._get_conn()
        cutoff_time = time.time() - (max_age_days * 86400)

        result = conn.execute("""
            DELETE FROM episodes
            WHERE utility_score < ? OR timestamp < ?
        """, (min_utility, cutoff_time))

        pruned = result.rowcount
        conn.commit()
        return pruned

    # ── Internal Helpers ──────────────────────────────────────────────

    def _upsert_episode(self, conn: sqlite3.Connection, session_id: str, entry) -> None:
        """Insert or update an episodic memory from a MemoryEntry."""
        mem_id = getattr(entry, "id", str(uuid.uuid4()))
        now = time.time()

        conn.execute("""
            INSERT OR REPLACE INTO episodes
            (id, session_id, content, context_json, timestamp, last_accessed,
             access_count, utility_score, decay_rate, importance, tags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mem_id, session_id,
            getattr(entry, "content", str(entry)),
            json.dumps(getattr(entry, "context", {})),
            getattr(entry, "timestamp", now),
            getattr(entry, "last_accessed", now),
            getattr(entry, "access_count", 1),
            getattr(entry, "utility_score", 1.0),
            getattr(entry, "decay_rate", 0.05),
            getattr(entry, "importance", 0.5),
            json.dumps(getattr(entry, "tags", [])),
            now,
        ))

    def _upsert_semantic(self, conn: sqlite3.Connection, entry) -> None:
        """Insert or update a semantic memory from a MemoryEntry."""
        mem_id = getattr(entry, "id", str(uuid.uuid4()))
        now = time.time()

        conn.execute("""
            INSERT OR REPLACE INTO semantics
            (id, content, context_json, timestamp, last_accessed,
             access_count, utility_score, decay_rate, importance,
             tags_json, source_episode_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
        """, (
            mem_id,
            getattr(entry, "content", str(entry)),
            json.dumps(getattr(entry, "context", {})),
            getattr(entry, "timestamp", now),
            getattr(entry, "last_accessed", now),
            getattr(entry, "access_count", 1),
            getattr(entry, "utility_score", 1.0),
            getattr(entry, "decay_rate", 0.01),
            getattr(entry, "importance", 0.7),
            json.dumps(getattr(entry, "tags", [])),
            now,
        ))

    def _row_to_dict(self, row: sqlite3.Row, memory_type: str) -> dict:
        """Convert a SQLite Row to a memory dict."""
        d = dict(row)
        d["memory_type"] = memory_type
        d["context"] = json.loads(d.pop("context_json", "{}"))
        d["tags"] = json.loads(d.pop("tags_json", "[]"))
        if "source_episode_ids" in d:
            d["source_episode_ids"] = json.loads(d.get("source_episode_ids", "[]"))
        return d

    def _compute_effective_utility(self, entry: dict, now: float) -> float:
        """Compute time-decayed effective utility for a memory.

        Models exponential decay: utility * e^(-lambda * hours_since_access)
        Combined with importance boost and access frequency bonus.
        """
        last_accessed = entry.get("last_accessed", entry.get("timestamp", now))
        hours_elapsed = max(0, (now - last_accessed) / 3600)
        decay_rate = entry.get("decay_rate", 0.05)

        # Exponential decay based on half-life
        lam = math.log(2) / DECAY_HALF_LIFE_HOURS
        decay_factor = math.exp(-lam * hours_elapsed * (decay_rate / 0.05))

        base_utility = entry.get("utility_score", 1.0)
        importance = entry.get("importance", 0.5)
        access_count = entry.get("access_count", 1)

        # Frequency bonus: log scale, caps at ~0.3
        freq_bonus = min(0.3, math.log1p(access_count) * 0.1)

        return round(base_utility * decay_factor * (0.7 + importance * 0.3) + freq_bonus, 4)


# ── CLI Interface ─────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Persistent Memory Store CLI")
    parser.add_argument("command", choices=["stats", "recall", "consolidate", "prune", "load"],
                        help="Command to execute")
    parser.add_argument("--query", "-q", type=str, help="Search query for recall")
    parser.add_argument("--top-k", "-k", type=int, default=10, help="Number of results")
    parser.add_argument("--session", "-s", type=str, help="Session ID for load")
    args = parser.parse_args()

    store = PersistentMemoryStore()

    if args.command == "stats":
        stats = store.get_stats()
        for k, v in stats.items():
            print(f"  {k}: {v}")

    elif args.command == "recall":
        if not args.query:
            print("Error: --query required for recall")
            return
        results = store.recall(args.query, top_k=args.top_k)
        for i, r in enumerate(results, 1):
            print(f"\n[{i}] ({r['memory_type']}) score={r.get('combined_score', '?')}")
            print(f"    {r['content'][:200]}")

    elif args.command == "consolidate":
        result = store.consolidate()
        print(f"Consolidated: {result['consolidated']} patterns from {result['episodes_scanned']} episodes")

    elif args.command == "prune":
        pruned = store.prune()
        print(f"Pruned {pruned} low-utility memories")

    elif args.command == "load":
        memories = store.load_session(args.session, top_k=args.top_k)
        for i, m in enumerate(memories, 1):
            print(f"\n[{i}] ({m['memory_type']}) utility={m.get('effective_utility', '?')}")
            print(f"    {m['content'][:200]}")

    store.close()


if __name__ == "__main__":
    main()
