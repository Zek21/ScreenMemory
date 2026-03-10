"""
LanceDB multimodal vector store — unified storage for screenshots, metadata,
embeddings, and OCR text in a single table. Replaces sqlite-vec for vector search.
Supports hybrid queries: vector similarity + full-text + SQL filters in one atomic query.
Built on Apache Arrow for zero-copy reads and efficient memory operations.
"""
import os
import io
import json
import time
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MultimodalRecord:
    """A single screen capture record with all modalities."""
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
    ocr_regions_json: str = "[]"  # JSON array of {text, bbox, confidence}
    thumbnail_bytes: Optional[bytes] = None
    embedding: Optional[List[float]] = None
    metadata_json: str = "{}"


class LanceDBStore:
    """
    Unified multimodal vector store using LanceDB.
    Stores raw image thumbnails, metadata, OCR text with spatial data,
    VLM analysis, and vector embeddings in a single Lance table.
    Supports hybrid search: vector + FTS + SQL in one query.
    """

    def __init__(self, db_path: str = "data/lance_memory",
                 embedding_dim: int = 768,
                 table_name: str = "screen_captures"):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.table_name = table_name
        self._db = None
        self._table = None
        self._available = False

        try:
            import lancedb
            import pyarrow as pa
            self._lancedb = lancedb
            self._pa = pa

            os.makedirs(db_path, exist_ok=True)
            self._db = lancedb.connect(db_path)
            self._init_table()
            self._available = True
            logger.info("LanceDB initialized: %s (dim=%d)", db_path, embedding_dim)
        except Exception as e:
            logger.warning("LanceDB not available: %s", e)

    @property
    def is_available(self) -> bool:
        return self._available

    def _init_table(self):
        """Create or open the multimodal captures table."""
        pa = self._pa
        existing = self._db.list_tables() if hasattr(self._db, 'list_tables') else self._db.table_names()

        if self.table_name in existing:
            self._table = self._db.open_table(self.table_name)
            logger.info("Opened existing table '%s' (%d rows)",
                        self.table_name, self._table.count_rows())
        else:
            # Define schema with all modalities
            schema = pa.schema([
                pa.field("id", pa.int64()),
                pa.field("timestamp", pa.float64()),
                pa.field("monitor_index", pa.int32()),
                pa.field("width", pa.int32()),
                pa.field("height", pa.int32()),
                pa.field("dhash", pa.string()),
                pa.field("active_window_title", pa.string()),
                pa.field("active_process", pa.string()),
                pa.field("analysis_text", pa.string()),
                pa.field("ocr_text", pa.string()),
                pa.field("ocr_regions_json", pa.string()),
                pa.field("thumbnail_bytes", pa.binary()),
                pa.field("vector", pa.list_(pa.float32(), self.embedding_dim)),
                pa.field("metadata_json", pa.string()),
            ])

            # Create empty table with schema
            self._table = self._db.create_table(
                self.table_name,
                schema=schema,
            )
            logger.info("Created new table '%s'", self.table_name)

    def _next_id(self) -> int:
        """Get next auto-increment ID."""
        try:
            count = self._table.count_rows()
            if count == 0:
                return 1
            # Get max ID
            result = self._table.search().select(["id"]).limit(1).to_pandas()
            if result.empty:
                return 1
            # Use count as approximation (faster than MAX query)
            return count + 1
        except Exception:
            return int(time.time() * 1000) % 1_000_000

    def insert(self, record: MultimodalRecord) -> int:
        """Insert a single multimodal record."""
        if not self._available:
            raise RuntimeError("LanceDB not available")

        record_id = record.id or self._next_id()

        # Default embedding (zero vector) if none provided
        embedding = record.embedding or [0.0] * self.embedding_dim

        data = [{
            "id": record_id,
            "timestamp": record.timestamp,
            "monitor_index": record.monitor_index,
            "width": record.width,
            "height": record.height,
            "dhash": record.dhash,
            "active_window_title": record.active_window_title,
            "active_process": record.active_process,
            "analysis_text": record.analysis_text,
            "ocr_text": record.ocr_text,
            "ocr_regions_json": record.ocr_regions_json,
            "thumbnail_bytes": record.thumbnail_bytes,
            "vector": embedding,
            "metadata_json": record.metadata_json,
        }]

        self._table.add(data)
        return record_id

    def insert_batch(self, records: List[MultimodalRecord]) -> List[int]:
        """Insert multiple records in one batch (much faster than individual inserts)."""
        if not self._available:
            raise RuntimeError("LanceDB not available")

        ids = []
        data = []
        base_id = self._next_id()

        for i, record in enumerate(records):
            record_id = record.id or (base_id + i)
            ids.append(record_id)

            embedding = record.embedding or [0.0] * self.embedding_dim

            data.append({
                "id": record_id,
                "timestamp": record.timestamp,
                "monitor_index": record.monitor_index,
                "width": record.width,
                "height": record.height,
                "dhash": record.dhash,
                "active_window_title": record.active_window_title,
                "active_process": record.active_process,
                "analysis_text": record.analysis_text,
                "ocr_text": record.ocr_text,
                "ocr_regions_json": record.ocr_regions_json,
                "thumbnail_bytes": record.thumbnail_bytes,
                "vector": embedding,
                "metadata_json": record.metadata_json,
            })

        self._table.add(data)
        return ids

    def update_analysis(self, record_id: int, analysis_text: str, ocr_text: str = "",
                        ocr_regions_json: str = "[]", embedding: Optional[List[float]] = None):
        """Update a record with VLM analysis and OCR results (called by async workers)."""
        if not self._available:
            return

        updates = {
            "analysis_text": analysis_text,
            "ocr_text": ocr_text,
            "ocr_regions_json": ocr_regions_json,
        }

        if embedding:
            updates["vector"] = embedding

        try:
            # LanceDB update via merge
            self._table.update(where=f"id = {record_id}", values=updates)
        except Exception as e:
            logger.error("Failed to update record %d: %s", record_id, e)

    def search_vector(self, query_vector: List[float], limit: int = 20,
                      time_start: Optional[float] = None,
                      time_end: Optional[float] = None,
                      process_filter: Optional[str] = None) -> List[Dict]:
        """
        Vector similarity search with optional SQL filters.
        Returns records ordered by cosine distance.
        """
        if not self._available:
            return []

        query = self._table.search(query_vector).limit(limit)

        # Apply SQL filters
        filters = []
        if time_start:
            filters.append(f"timestamp >= {time_start}")
        if time_end:
            filters.append(f"timestamp <= {time_end}")
        if process_filter:
            filters.append(f"active_process LIKE '%{process_filter}%'")

        if filters:
            query = query.where(" AND ".join(filters))

        try:
            results = query.to_pandas()
            return results.to_dict("records")
        except Exception as e:
            logger.error("Vector search failed: %s", e)
            return []

    def search_text(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Text search across analysis_text, ocr_text, and window titles.
        Uses pandas string matching (LanceDB FTS is available in newer versions).
        """
        if not self._available:
            return []

        try:
            # Get all records and filter (LanceDB FTS is still maturing)
            q = query.lower()
            df = self._table.search().limit(limit * 5).to_pandas()

            mask = (
                df["analysis_text"].str.lower().str.contains(q, na=False) |
                df["ocr_text"].str.lower().str.contains(q, na=False) |
                df["active_window_title"].str.lower().str.contains(q, na=False) |
                df["active_process"].str.lower().str.contains(q, na=False)
            )

            filtered = df[mask].head(limit)
            return filtered.to_dict("records")
        except Exception as e:
            logger.error("Text search failed: %s", e)
            return []

    def search_hybrid(self, query_text: str, query_vector: Optional[List[float]] = None,
                      limit: int = 20, text_weight: float = 0.3,
                      vector_weight: float = 0.7) -> List[Dict]:
        """
        Hybrid search: combines vector similarity with text matching.
        Re-ranks results using weighted fusion.
        """
        text_results = self.search_text(query_text, limit * 2) if query_text else []
        vector_results = (
            self.search_vector(query_vector, limit * 2)
            if query_vector else []
        )

        # Merge with reciprocal rank fusion
        scored = {}
        data_store = {}

        for rank, r in enumerate(text_results):
            rid = r.get("id")
            scored[rid] = scored.get(rid, 0) + text_weight / (rank + 1)
            data_store[rid] = r

        for rank, r in enumerate(vector_results):
            rid = r.get("id")
            scored[rid] = scored.get(rid, 0) + vector_weight / (rank + 1)
            if rid not in data_store:
                data_store[rid] = r

        # Sort by fused score
        sorted_ids = sorted(scored.keys(), key=lambda k: scored[k], reverse=True)
        return [data_store[rid] for rid in sorted_ids[:limit] if rid in data_store]

    def get_recent(self, limit: int = 50) -> List[Dict]:
        """Get most recent captures."""
        if not self._available:
            return []
        try:
            df = self._table.to_pandas().nlargest(limit, "timestamp")
            return df.to_dict("records")
        except Exception as e:
            logger.error("get_recent failed: %s", e)
            return []

    def get_by_timerange(self, start: float, end: float) -> List[Dict]:
        """Get captures within a time range."""
        if not self._available:
            return []
        try:
            df = self._table.search().where(
                f"timestamp >= {start} AND timestamp <= {end}"
            ).limit(10000).to_pandas()
            return df.sort_values("timestamp").to_dict("records")
        except Exception as e:
            logger.error("get_by_timerange failed: %s", e)
            return []

    def get_by_process(self, process_name: str, limit: int = 50) -> List[Dict]:
        """Get captures filtered by process name."""
        if not self._available:
            return []
        try:
            df = self._table.search().where(
                f"active_process LIKE '%{process_name}%'"
            ).limit(limit).to_pandas()
            return df.to_dict("records")
        except Exception as e:
            logger.error("get_by_process failed: %s", e)
            return []

    def get_stats(self) -> Dict:
        """Get database statistics."""
        if not self._available:
            return {"available": False}

        try:
            count = self._table.count_rows()
            # Estimate size from disk
            total_size = 0
            for root, dirs, files in os.walk(self.db_path):
                for f in files:
                    total_size += os.path.getsize(os.path.join(root, f))

            return {
                "total_captures": count,
                "db_size_mb": total_size / (1024 * 1024),
                "table_name": self.table_name,
                "embedding_dim": self.embedding_dim,
                "available": True,
                "backend": "lancedb",
            }
        except Exception as e:
            return {"available": True, "error": str(e)}

    def migrate_from_sqlite(self, sqlite_db) -> int:
        """
        Migrate records from existing ScreenMemoryDB (SQLite) to LanceDB.
        Preserves all data including analysis text and metadata.
        """
        if not self._available:
            return 0

        records = sqlite_db.get_recent(limit=100000)
        if not records:
            return 0

        multimodal_records = []
        for r in records:
            multimodal_records.append(MultimodalRecord(
                id=r.get("id"),
                timestamp=r.get("timestamp", 0),
                monitor_index=r.get("monitor_index", 0),
                width=r.get("width", 0),
                height=r.get("height", 0),
                dhash=r.get("dhash", ""),
                active_window_title=r.get("active_window_title", ""),
                active_process=r.get("active_process", ""),
                analysis_text=r.get("analysis_text", ""),
                ocr_text=r.get("ocr_text", ""),
                metadata_json=r.get("metadata_json", "{}"),
            ))

        if multimodal_records:
            self.insert_batch(multimodal_records)
            logger.info("Migrated %d records from SQLite to LanceDB", len(multimodal_records))

        return len(multimodal_records)

    def close(self):
        """Close LanceDB connection."""
        self._table = None
        self._db = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    store = LanceDBStore("data/test_lance")
    print(f"Available: {store.is_available}")

    if store.is_available:
        # Insert test record
        record = MultimodalRecord(
            timestamp=time.time(),
            monitor_index=0,
            width=1920,
            height=1080,
            dhash="test_hash_123",
            active_window_title="VS Code - ScreenMemory",
            active_process="Code.exe",
            analysis_text="User is editing Python code in VS Code",
            ocr_text="def capture_screen(self):",
            embedding=[0.1] * 768,
        )
        rid = store.insert(record)
        print(f"Inserted record ID: {rid}")

        stats = store.get_stats()
        print(f"Stats: {json.dumps(stats, indent=2)}")

        # Search
        results = store.search_text("VS Code")
        print(f"Text search 'VS Code': {len(results)} results")

        results = store.search_vector([0.1] * 768, limit=5)
        print(f"Vector search: {len(results)} results")

        store.close()
