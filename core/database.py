"""
Database layer: sqlite-vec for vector search + optional SQLCipher encryption.
Stores screen captures, VLM analysis text, embeddings, and metadata.
"""
import os
import json
import time
import sqlite3
import struct
import logging
import threading
from typing import Optional, List, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ScreenRecord:
    id: Optional[int] = None
    timestamp: float = 0.0
    monitor_index: int = 0
    width: int = 0
    height: int = 0
    dhash: str = ""
    active_window_title: str = ""
    active_process: str = ""
    analysis_text: str = ""
    ocr_text: str = ""
    embedding: Optional[bytes] = None
    thumbnail_path: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class ScreenMemoryDB:
    """
    SQLite database with sqlite-vec extension for vector similarity search
    and FTS5 for full-text keyword search.
    """

    def __init__(self, db_path: str = "data/screen_memory.db",
                 encryption_key: Optional[str] = None,
                 embedding_dim: int = 768):
        self.db_path = db_path
        # Validate embedding_dim to prevent SQL injection in CREATE VIRTUAL TABLE
        if not isinstance(embedding_dim, int) or embedding_dim < 1 or embedding_dim > 10000:  # signed: alpha
            raise ValueError(f"embedding_dim must be an integer between 1 and 10000, got {embedding_dim!r}")
        self.embedding_dim = embedding_dim
        self._encryption_key = encryption_key
        self._local = threading.local()  # thread-local connection pool  # signed: delta

        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

        self.conn = self._connect(db_path, encryption_key)
        self._init_schema()
        self._init_vec_extension()
        self._init_fts()

        logger.info(f"Database initialized: {db_path} (encrypted={encryption_key is not None})")

    def _connect(self, db_path: str, encryption_key: Optional[str] = None) -> sqlite3.Connection:
        """Connect to database, optionally with SQLCipher encryption."""
        if encryption_key:
            try:
                import pysqlcipher3.dbapi2 as sqlcipher
                conn = sqlcipher.connect(db_path)
                # Validate key format: only allow alphanumeric + safe punctuation to
                # prevent SQL injection in PRAGMA (which doesn't support parameters)  # signed: delta
                import re as _re
                if not _re.match(r'^[A-Za-z0-9!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]{1,256}$', encryption_key):
                    raise ValueError("Encryption key contains disallowed characters or exceeds 256 chars")
                safe_key = encryption_key.replace("'", "''")
                conn.execute(f"PRAGMA key = '{safe_key}'")
                conn.execute("PRAGMA cipher_memory_security = ON")
                logger.info("SQLCipher encryption enabled")
                return conn
            except ImportError:
                logger.warning("pysqlcipher3 not available — using unencrypted SQLite")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """Return a thread-local connection. Reuses per-thread connections
        instead of creating a new one per operation."""  # signed: delta
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._connect(self.db_path, self._encryption_key)
            self._local.conn = conn
        return conn

    def _init_schema(self):
        """Create core tables."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                monitor_index INTEGER DEFAULT 0,
                width INTEGER,
                height INTEGER,
                dhash TEXT,
                active_window_title TEXT,
                active_process TEXT,
                analysis_text TEXT,
                ocr_text TEXT,
                thumbnail_path TEXT,
                metadata_json TEXT,
                created_at REAL DEFAULT (unixepoch('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_captures_timestamp ON captures(timestamp);
            CREATE INDEX IF NOT EXISTS idx_captures_process ON captures(active_process);
            CREATE INDEX IF NOT EXISTS idx_captures_dhash ON captures(dhash);

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time REAL NOT NULL,
                end_time REAL,
                total_captures INTEGER DEFAULT 0,
                metadata_json TEXT
            );
        """)
        self.conn.commit()

    def _init_vec_extension(self):
        """Load sqlite-vec extension for vector similarity search."""
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)

            # Create virtual table for vector search
            self.conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS capture_embeddings
                USING vec0(
                    capture_id INTEGER PRIMARY KEY,
                    embedding float[{self.embedding_dim}]
                )
            """)
            self.conn.commit()
            self._vec_available = True
            logger.info(f"sqlite-vec loaded (dim={self.embedding_dim})")
        except (ImportError, Exception) as e:
            logger.warning(f"sqlite-vec not available: {e}. Vector search disabled.")
            self._vec_available = False

    def _init_fts(self):
        """Create FTS5 virtual table for full-text search."""
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts
                USING fts5(
                    analysis_text,
                    ocr_text,
                    active_window_title,
                    active_process,
                    content=captures,
                    content_rowid=id
                )
            """)
            # Triggers to keep FTS in sync
            self.conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
                    INSERT INTO captures_fts(rowid, analysis_text, ocr_text, active_window_title, active_process)
                    VALUES (new.id, new.analysis_text, new.ocr_text, new.active_window_title, new.active_process);
                END;

                CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
                    INSERT INTO captures_fts(captures_fts, rowid, analysis_text, ocr_text, active_window_title, active_process)
                    VALUES ('delete', old.id, old.analysis_text, old.ocr_text, old.active_window_title, old.active_process);
                END;

                CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
                    INSERT INTO captures_fts(captures_fts, rowid, analysis_text, ocr_text, active_window_title, active_process)
                    VALUES ('delete', old.id, old.analysis_text, old.ocr_text, old.active_window_title, old.active_process);
                    INSERT INTO captures_fts(rowid, analysis_text, ocr_text, active_window_title, active_process)
                    VALUES (new.id, new.analysis_text, new.ocr_text, new.active_window_title, new.active_process);
                END;
            """)
            self.conn.commit()
            self._fts_available = True
            logger.info("FTS5 full-text search initialized")
        except Exception as e:
            logger.warning(f"FTS5 init failed: {e}")
            self._fts_available = False

    def insert_capture(self, record: ScreenRecord) -> int:
        """Insert a screen capture record and its embedding."""
        cursor = self.conn.execute("""
            INSERT INTO captures
            (timestamp, monitor_index, width, height, dhash,
             active_window_title, active_process, analysis_text,
             ocr_text, thumbnail_path, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.timestamp, record.monitor_index, record.width, record.height,
            record.dhash, record.active_window_title, record.active_process,
            record.analysis_text, record.ocr_text, record.thumbnail_path,
            json.dumps(record.metadata) if record.metadata else None,
        ))

        capture_id = cursor.lastrowid

        # Insert embedding if available
        if record.embedding is not None and self._vec_available:
            self.conn.execute(
                "INSERT INTO capture_embeddings(capture_id, embedding) VALUES (?, ?)",
                (capture_id, record.embedding),
            )

        self.conn.commit()
        record.id = capture_id
        return capture_id

    def _serialize_embedding(self, embedding: list[float]) -> bytes:
        """Serialize float list to bytes for sqlite-vec."""
        return struct.pack(f"{len(embedding)}f", *embedding)

    def search_semantic(self, query_embedding: bytes, limit: int = 20,
                        time_start: Optional[float] = None,
                        time_end: Optional[float] = None) -> List[dict]:
        """
        Search captures by vector similarity (cosine distance).
        Returns captures ordered by relevance.
        """
        if not self._vec_available:
            logger.warning("Vector search unavailable")
            return []

        # Build time filter
        time_filter = ""
        params = [query_embedding, limit]
        if time_start or time_end:
            conditions = []
            if time_start:
                conditions.append("c.timestamp >= ?")
                params.append(time_start)
            if time_end:
                conditions.append("c.timestamp <= ?")
                params.append(time_end)
            time_filter = "AND " + " AND ".join(conditions)

        results = self.conn.execute(f"""
            SELECT c.*, e.distance
            FROM capture_embeddings e
            JOIN captures c ON c.id = e.capture_id
            WHERE e.embedding MATCH ?
            AND k = ?
            {time_filter}
            ORDER BY e.distance ASC
        """, params).fetchall()

        return [self._row_to_dict(r) for r in results]

    def search_text(self, query: str, limit: int = 20) -> List[dict]:
        """Full-text search across analysis text, OCR text, window titles."""
        if not self._fts_available:
            # Fallback to LIKE
            return self.conn.execute("""
                SELECT * FROM captures
                WHERE analysis_text LIKE ? OR ocr_text LIKE ?
                   OR active_window_title LIKE ?
                ORDER BY timestamp DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()

        # Escape FTS5 special characters
        safe_query = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in query).strip()
        if not safe_query:
            return []

        # Quote each term for exact matching
        terms = safe_query.split()
        fts_query = " OR ".join(f'"{t}"' for t in terms if t)

        results = self.conn.execute("""
            SELECT c.*, rank
            FROM captures_fts fts
            JOIN captures c ON c.id = fts.rowid
            WHERE captures_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_query, limit)).fetchall()

        return [self._row_to_dict(r) for r in results]

    def search_hybrid(self, query: str, query_embedding: Optional[bytes] = None,
                      limit: int = 20, text_weight: float = 0.3,
                      semantic_weight: float = 0.7) -> List[dict]:
        """
        Hybrid search combining FTS5 text search and vector similarity.
        Merges and re-ranks results from both sources.
        """
        text_results = self.search_text(query, limit * 2) if query else []
        semantic_results = (
            self.search_semantic(query_embedding, limit * 2)
            if query_embedding and self._vec_available
            else []
        )

        # Merge results by capture ID with weighted scores
        scored = {}
        for i, r in enumerate(text_results):
            rid = r.get("id")
            text_score = 1.0 - (i / max(len(text_results), 1))
            scored[rid] = scored.get(rid, 0) + text_score * text_weight
            scored[f"_data_{rid}"] = r

        for i, r in enumerate(semantic_results):
            rid = r.get("id")
            sem_score = 1.0 - (i / max(len(semantic_results), 1))
            scored[rid] = scored.get(rid, 0) + sem_score * semantic_weight
            if f"_data_{rid}" not in scored:
                scored[f"_data_{rid}"] = r

        # Sort by combined score
        result_ids = [k for k in scored if not str(k).startswith("_data_")]
        result_ids.sort(key=lambda k: scored[k], reverse=True)

        return [scored[f"_data_{rid}"] for rid in result_ids[:limit]]

    def get_recent(self, limit: int = 50) -> List[dict]:
        """Get most recent captures."""
        results = self.conn.execute(
            "SELECT * FROM captures ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(r) for r in results]

    def get_by_timerange(self, start: float, end: float) -> List[dict]:
        """Get captures within a time range."""
        results = self.conn.execute(
            "SELECT * FROM captures WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (start, end),
        ).fetchall()
        return [self._row_to_dict(r) for r in results]

    def get_by_process(self, process_name: str, limit: int = 50) -> List[dict]:
        """Get captures filtered by process name."""
        results = self.conn.execute(
            "SELECT * FROM captures WHERE active_process LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{process_name}%", limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in results]

    def get_stats(self) -> dict:
        """Get database statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        oldest = self.conn.execute("SELECT MIN(timestamp) FROM captures").fetchone()[0]
        newest = self.conn.execute("SELECT MAX(timestamp) FROM captures").fetchone()[0]
        size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "total_captures": total,
            "oldest_timestamp": oldest,
            "newest_timestamp": newest,
            "db_size_mb": size_bytes / (1024 * 1024),
            "vec_available": self._vec_available,
            "fts_available": self._fts_available,
        }

    def cleanup_old(self, retention_days: int = 90):
        """Remove captures older than retention period."""
        cutoff = time.time() - (retention_days * 86400)
        deleted = self.conn.execute(
            "DELETE FROM captures WHERE timestamp < ?", (cutoff,)
        ).rowcount

        if self._vec_available:
            self.conn.execute(
                "DELETE FROM capture_embeddings WHERE capture_id NOT IN (SELECT id FROM captures)"
            )

        self.conn.execute("PRAGMA incremental_vacuum")
        self.conn.commit()
        logger.info(f"Cleaned up {deleted} captures older than {retention_days} days")
        return deleted

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite row to dict."""
        if row is None:
            return {}
        cols = [desc[0] for desc in self.conn.execute("SELECT * FROM captures LIMIT 0").description]
        result = {}
        for i, col in enumerate(cols):
            if i < len(row):
                result[col] = row[i]
        # Handle extra columns (distance, rank) from JOINs
        if len(row) > len(cols):
            result["score"] = row[len(cols)]
        return result

    def close(self):
        """Close database connection and thread-local connections."""  # signed: delta
        self.conn.close()
        tl_conn = getattr(self._local, 'conn', None)
        if tl_conn is not None:
            try:
                tl_conn.close()
            except Exception:
                pass
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    db = ScreenMemoryDB("data/test_memory.db")
    stats = db.get_stats()
    print(f"DB Stats: {json.dumps(stats, indent=2)}")

    # Insert a test record
    record = ScreenRecord(
        timestamp=time.time(),
        monitor_index=0,
        width=1920,
        height=1080,
        dhash="abcdef1234567890",
        active_window_title="VS Code - ScreenMemory",
        active_process="Code.exe",
        analysis_text="User is editing Python code in VS Code, working on a screen memory project.",
        ocr_text="def capture_monitor(self):",
    )
    rid = db.insert_capture(record)
    print(f"Inserted record ID: {rid}")

    # Search
    results = db.search_text("VS Code")
    print(f"Text search 'VS Code': {len(results)} results")

    db.close()
