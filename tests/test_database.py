"""Tests for core/database.py - ScreenMemoryDB with SQLite, FTS5, and optional sqlite-vec.
# signed: beta
"""
import os
import json
import time
import struct
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core.database import ScreenMemoryDB, ScreenRecord


class TestScreenRecord(unittest.TestCase):
    """Test the ScreenRecord dataclass."""

    def test_default_fields(self):
        r = ScreenRecord()
        self.assertIsNone(r.id)
        self.assertEqual(r.timestamp, 0.0)
        self.assertEqual(r.monitor_index, 0)
        self.assertEqual(r.width, 0)
        self.assertEqual(r.height, 0)
        self.assertEqual(r.dhash, "")
        self.assertEqual(r.active_window_title, "")
        self.assertEqual(r.active_process, "")
        self.assertEqual(r.analysis_text, "")
        self.assertEqual(r.ocr_text, "")
        self.assertIsNone(r.embedding)
        self.assertIsNone(r.thumbnail_path)
        self.assertIsInstance(r.metadata, dict)
        # signed: beta

    def test_custom_fields(self):
        r = ScreenRecord(
            timestamp=1000.0, monitor_index=1, width=1920, height=1080,
            dhash="abc123", active_window_title="VS Code",
            active_process="code.exe", analysis_text="test analysis",
            ocr_text="test ocr", thumbnail_path="/tmp/thumb.png",
            metadata={"key": "value"}
        )
        self.assertEqual(r.timestamp, 1000.0)
        self.assertEqual(r.width, 1920)
        self.assertEqual(r.active_process, "code.exe")
        self.assertEqual(r.metadata["key"], "value")
        # signed: beta


class TestScreenMemoryDBInit(unittest.TestCase):
    """Test database initialization."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_db_file(self):
        db = ScreenMemoryDB(db_path=self.db_path)
        self.assertTrue(os.path.exists(self.db_path))
        db.close()
        # signed: beta

    def test_creates_parent_dirs(self):
        nested_path = os.path.join(self.tmpdir, "sub", "dir", "test.db")
        db = ScreenMemoryDB(db_path=nested_path)
        self.assertTrue(os.path.exists(nested_path))
        db.close()
        # signed: beta

    def test_invalid_embedding_dim_raises(self):
        with self.assertRaises(ValueError):
            ScreenMemoryDB(db_path=self.db_path, embedding_dim=-1)
        # signed: beta

    def test_invalid_embedding_dim_string_raises(self):
        with self.assertRaises(ValueError):
            ScreenMemoryDB(db_path=self.db_path, embedding_dim="512")
        # signed: beta

    def test_embedding_dim_too_large_raises(self):
        with self.assertRaises(ValueError):
            ScreenMemoryDB(db_path=self.db_path, embedding_dim=20000)
        # signed: beta

    def test_default_embedding_dim(self):
        db = ScreenMemoryDB(db_path=self.db_path)
        self.assertEqual(db.embedding_dim, 768)
        db.close()
        # signed: beta

    def test_custom_embedding_dim(self):
        db = ScreenMemoryDB(db_path=self.db_path, embedding_dim=512)
        self.assertEqual(db.embedding_dim, 512)
        db.close()
        # signed: beta


class TestInsertAndRetrieve(unittest.TestCase):
    """Test insert_capture and retrieval methods."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_insert_returns_id(self):
        record = ScreenRecord(timestamp=time.time(), width=1920, height=1080,
                              active_process="test.exe")
        capture_id = self.db.insert_capture(record)
        self.assertIsInstance(capture_id, int)
        self.assertGreater(capture_id, 0)
        # signed: beta

    def test_insert_sets_record_id(self):
        record = ScreenRecord(timestamp=time.time())
        self.db.insert_capture(record)
        self.assertIsNotNone(record.id)
        # signed: beta

    def test_get_recent_returns_inserted(self):
        ts = time.time()
        self.db.insert_capture(ScreenRecord(timestamp=ts, active_process="chrome.exe",
                                            analysis_text="browsing web"))
        results = self.db.get_recent(limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["active_process"], "chrome.exe")
        # signed: beta

    def test_get_recent_ordered_by_timestamp_desc(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0, active_process="first"))
        self.db.insert_capture(ScreenRecord(timestamp=200.0, active_process="second"))
        self.db.insert_capture(ScreenRecord(timestamp=150.0, active_process="middle"))
        results = self.db.get_recent(limit=10)
        self.assertEqual(results[0]["active_process"], "second")
        self.assertEqual(results[1]["active_process"], "middle")
        self.assertEqual(results[2]["active_process"], "first")
        # signed: beta

    def test_get_by_timerange(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0, active_process="a"))
        self.db.insert_capture(ScreenRecord(timestamp=200.0, active_process="b"))
        self.db.insert_capture(ScreenRecord(timestamp=300.0, active_process="c"))
        results = self.db.get_by_timerange(150.0, 250.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["active_process"], "b")
        # signed: beta

    def test_get_by_process(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0, active_process="chrome.exe"))
        self.db.insert_capture(ScreenRecord(timestamp=200.0, active_process="code.exe"))
        self.db.insert_capture(ScreenRecord(timestamp=300.0, active_process="chrome.exe"))
        results = self.db.get_by_process("chrome")
        self.assertEqual(len(results), 2)
        # signed: beta

    def test_metadata_serialized(self):
        record = ScreenRecord(timestamp=100.0, metadata={"key": "value", "n": 42})
        self.db.insert_capture(record)
        results = self.db.get_recent(limit=1)
        meta_json = results[0].get("metadata_json")
        self.assertIsNotNone(meta_json)
        meta = json.loads(meta_json)
        self.assertEqual(meta["key"], "value")
        self.assertEqual(meta["n"], 42)
        # signed: beta


class TestSearchText(unittest.TestCase):
    """Test full-text search."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_search_text_finds_match(self):
        self.db.insert_capture(ScreenRecord(
            timestamp=100.0, analysis_text="Python debugging session",
            active_process="code.exe"))
        self.db.insert_capture(ScreenRecord(
            timestamp=200.0, analysis_text="Browsing documentation",
            active_process="chrome.exe"))
        results = self.db.search_text("Python")
        self.assertGreaterEqual(len(results), 1)
        # signed: beta

    def test_search_text_empty_query(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0, analysis_text="test"))
        results = self.db.search_text("")
        self.assertEqual(len(results), 0)
        # signed: beta

    def test_search_text_no_match(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0, analysis_text="hello world"))
        results = self.db.search_text("zzzznonexistent")
        self.assertEqual(len(results), 0)
        # signed: beta


class TestGetStats(unittest.TestCase):
    """Test database statistics."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_db_stats(self):
        stats = self.db.get_stats()
        self.assertEqual(stats["total_captures"], 0)
        self.assertIsNone(stats["oldest_timestamp"])
        self.assertIsNone(stats["newest_timestamp"])
        self.assertIn("db_size_mb", stats)
        self.assertIn("vec_available", stats)
        self.assertIn("fts_available", stats)
        # signed: beta

    def test_stats_after_inserts(self):
        self.db.insert_capture(ScreenRecord(timestamp=100.0))
        self.db.insert_capture(ScreenRecord(timestamp=200.0))
        stats = self.db.get_stats()
        self.assertEqual(stats["total_captures"], 2)
        self.assertAlmostEqual(stats["oldest_timestamp"], 100.0)
        self.assertAlmostEqual(stats["newest_timestamp"], 200.0)
        # signed: beta


class TestCleanup(unittest.TestCase):
    """Test cleanup_old method."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_removes_old(self):
        old_ts = time.time() - (100 * 86400)  # 100 days ago
        new_ts = time.time()
        self.db.insert_capture(ScreenRecord(timestamp=old_ts, active_process="old"))
        self.db.insert_capture(ScreenRecord(timestamp=new_ts, active_process="new"))
        deleted = self.db.cleanup_old(retention_days=90)
        self.assertEqual(deleted, 1)
        remaining = self.db.get_recent(limit=10)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["active_process"], "new")
        # signed: beta

    def test_cleanup_nothing_to_delete(self):
        self.db.insert_capture(ScreenRecord(timestamp=time.time()))
        deleted = self.db.cleanup_old(retention_days=90)
        self.assertEqual(deleted, 0)
        # signed: beta


class TestSerializeEmbedding(unittest.TestCase):
    """Test embedding serialization."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_serialize_roundtrip(self):
        embedding = [0.1, 0.2, 0.3, 0.4, 0.5]
        serialized = self.db._serialize_embedding(embedding)
        self.assertIsInstance(serialized, bytes)
        deserialized = struct.unpack(f"{len(embedding)}f", serialized)
        for orig, deser in zip(embedding, deserialized):
            self.assertAlmostEqual(orig, deser, places=5)
        # signed: beta

    def test_serialize_empty(self):
        serialized = self.db._serialize_embedding([])
        self.assertEqual(serialized, b"")
        # signed: beta


class TestContextManager(unittest.TestCase):
    """Test __enter__/__exit__ context manager."""

    def test_context_manager(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        try:
            with ScreenMemoryDB(db_path=db_path) as db:
                db.insert_capture(ScreenRecord(timestamp=100.0))
                results = db.get_recent(limit=1)
                self.assertEqual(len(results), 1)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        # signed: beta


class TestThreadLocalConnection(unittest.TestCase):
    """Test thread-local connection pooling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.db = ScreenMemoryDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_connection_returns_connection(self):
        conn = self.db.get_connection()
        self.assertIsNotNone(conn)
        # signed: beta

    def test_get_connection_reuses_same_thread(self):
        conn1 = self.db.get_connection()
        conn2 = self.db.get_connection()
        self.assertIs(conn1, conn2)
        # signed: beta


if __name__ == "__main__":
    unittest.main()
