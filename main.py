"""
ScreenMemory — Main capture daemon.
Runs continuously, capturing screens, detecting changes,
analyzing with VLM, generating embeddings, and storing in encrypted DB.
"""
import os
import sys
import json
import time
import signal
import logging
import threading
import argparse
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.capture import DXGICapture, CaptureResult
from core.change_detector import ChangeDetector
from core.analyzer import ScreenAnalyzer
from core.embedder import EmbeddingEngine
from core.database import ScreenMemoryDB, ScreenRecord
from core.ocr import OCREngine
from core.lancedb_store import LanceDBStore, MultimodalRecord
from guardian import ProcessGuardian

logger = logging.getLogger("screenmemory")


class ScreenMemoryDaemon:
    """
    Main daemon that orchestrates the capture -> analyze -> store pipeline.
    VLM analysis runs in a background thread so captures aren't blocked.
    """

    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.running = False
        self._stats = {
            "captures_total": 0,
            "captures_analyzed": 0,
            "captures_skipped": 0,
            "captures_queued": 0,
            "errors": 0,
            "start_time": None,
        }

        # Process guardian — register self, enforce safety
        self.guardian = ProcessGuardian()
        self.guardian.register_self("screenmemory-daemon", max_lifetime=86400)

        # VLM analysis queue (capture continues while VLM processes)
        from queue import Queue
        self._analysis_queue = Queue(maxsize=10)
        self._analysis_thread = None
        self._ocr_thread = None

        # Initialize components
        logger.info("Initializing ScreenMemory components...")

        self.capture = DXGICapture(
            use_dxgi=self.config["capture"]["method"] == "dxgi"
        )

        self.detector = ChangeDetector(
            hash_size=self.config["change_detection"]["hash_size"],
            threshold=self.config["change_detection"]["threshold"],
            min_change_pct=self.config["change_detection"]["min_change_percent"],
        )

        self.analyzer = ScreenAnalyzer(
            model=self.config["analysis"]["model"],
            fallback_model=self.config["analysis"]["fallback_model"],
            max_tokens=self.config["analysis"]["max_tokens"],
        )

        self.embedder = EmbeddingEngine(prefer_gpu=True)

        # OCR engine (PaddleOCR for precise text extraction)
        self.ocr = OCREngine(lang="en", use_gpu=False)

        # LanceDB multimodal store (primary)
        lance_path = self.config.get("database", {}).get("lance_path", "data/lance_memory")
        self.lance_store = LanceDBStore(
            db_path=lance_path,
            embedding_dim=self.embedder.embedding_dim,
        )

        db_path = self.config["database"]["path"]
        encryption_key = os.environ.get("SCREENMEMORY_KEY") if self.config["database"]["encryption"] else None
        self.db = ScreenMemoryDB(
            db_path=db_path,
            encryption_key=encryption_key,
            embedding_dim=self.embedder.embedding_dim,
        )

        logger.info("All components initialized")
        logger.info(f"  Capture: {self.config['capture']['method']}")
        logger.info(f"  Monitors: {len(self.capture.monitors)}")
        logger.info(f"  VLM: {self.analyzer.model} (available={self.analyzer.is_available})")
        logger.info(f"  OCR: {self.ocr._engine_name} (available={self.ocr.is_available})")
        logger.info(f"  LanceDB: {lance_path} (available={self.lance_store.is_available})")
        logger.info(f"  Embeddings: {self.embedder._backend if self.embedder.is_available else 'none'}")
        logger.info(f"  Database: {db_path}")

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from JSON file."""
        if os.path.exists(config_path):
            with open(config_path) as f:
                return json.load(f)

        # Default config
        return {
            "capture": {"interval_seconds": 3, "method": "dxgi", "capture_all_monitors": True},
            "change_detection": {"hash_size": 16, "threshold": 8, "min_change_percent": 5},
            "analysis": {"model": "minicpm-v", "fallback_model": "llava:7b", "max_tokens": 512},
            "database": {"path": "data/screen_memory.db", "encryption": False},
            "privacy": {"excluded_apps": [], "excluded_window_titles": [], "pause_on_lock": True},
        }

    def _should_exclude(self, window_info: dict) -> bool:
        """Check if current window should be excluded (privacy filter)."""
        privacy = self.config.get("privacy", {})

        title = window_info.get("title", "").lower()
        process = window_info.get("process_name", "").lower()

        # Check excluded apps
        for app in privacy.get("excluded_apps", []):
            if app.lower() in process:
                return True

        # Check excluded window titles
        for pattern in privacy.get("excluded_window_titles", []):
            if pattern.lower() in title:
                return True

        return False

    def _process_capture(self, capture_result: CaptureResult, window_info: dict):
        """Process a single capture through the pipeline.
        OCR runs synchronously (fast ~200ms), VLM offloaded to background thread."""
        try:
            # 1. Change detection (fast: ~7ms)
            change = self.detector.detect_change(
                capture_result.image, capture_result.monitor_index
            )

            if not change.changed:
                self._stats["captures_skipped"] += 1
                return

            self._stats["captures_total"] += 1

            # 2. Run OCR synchronously (fast: ~200-500ms with PaddleOCR)
            ocr_text = ""
            ocr_regions_json = "[]"
            if self.ocr.is_available:
                ocr_result = self.ocr.extract(capture_result.image)
                ocr_text = ocr_result.full_text
                ocr_regions_json = json.dumps(ocr_result.to_spatial_json())
                logger.debug(f"OCR: {ocr_result.region_count} regions in {ocr_result.extraction_ms:.0f}ms")

            # 3. Store immediately with window metadata + OCR text
            record = ScreenRecord(
                timestamp=capture_result.timestamp,
                monitor_index=capture_result.monitor_index,
                width=capture_result.width,
                height=capture_result.height,
                dhash=change.current_hash,
                active_window_title=window_info.get("title", ""),
                active_process=window_info.get("process_name", ""),
                analysis_text="",  # VLM fills this async
                ocr_text=ocr_text,
                metadata={
                    "capture_ms": capture_result.capture_ms,
                    "change_percent": change.change_percent,
                    "ocr_regions": ocr_regions_json,
                },
            )

            capture_id = self.db.insert_capture(record)

            # 3b. Also store in LanceDB if available
            if self.lance_store.is_available:
                lance_record = MultimodalRecord(
                    id=capture_id,
                    timestamp=capture_result.timestamp,
                    monitor_index=capture_result.monitor_index,
                    width=capture_result.width,
                    height=capture_result.height,
                    dhash=change.current_hash,
                    active_window_title=window_info.get("title", ""),
                    active_process=window_info.get("process_name", ""),
                    ocr_text=ocr_text,
                    ocr_regions_json=ocr_regions_json,
                    metadata_json=json.dumps({
                        "capture_ms": capture_result.capture_ms,
                        "change_percent": change.change_percent,
                    }),
                )
                try:
                    self.lance_store.insert(lance_record)
                except Exception as e:
                    logger.debug(f"LanceDB insert failed: {e}")

            logger.info(
                f"Captured #{capture_id}: {window_info.get('process_name', '?')} "
                f"(capture={capture_result.capture_ms:.0f}ms, "
                f"change={change.change_percent:.0f}%, "
                f"ocr={len(ocr_text)} chars)"
            )

            # 4. Queue for async VLM analysis
            if self.analyzer.is_available and not self._analysis_queue.full():
                self._analysis_queue.put((capture_id, capture_result.image))
                self._stats["captures_queued"] += 1

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Processing error: {e}", exc_info=True)

    def _vlm_worker(self):
        """Background thread: processes VLM analysis queue.
        Uses its own SQLite connection (SQLite objects are thread-bound).
        Also updates LanceDB records with analysis text."""
        import sqlite3
        logger.info("VLM analysis worker started")
        db_path = self.config["database"]["path"]
        worker_conn = sqlite3.connect(db_path)
        worker_conn.execute("PRAGMA journal_mode=WAL")

        while self.running or not self._analysis_queue.empty():
            try:
                capture_id, image = self._analysis_queue.get(timeout=2)
            except Exception:
                continue

            try:
                analysis = self.analyzer.analyze(image, detailed=False)
                if analysis:
                    worker_conn.execute(
                        "UPDATE captures SET analysis_text=?, ocr_text=CASE WHEN ocr_text='' THEN ? ELSE ocr_text END WHERE id=?",
                        (analysis.description, analysis.ocr_text, capture_id),
                    )
                    worker_conn.commit()

                    # Update FTS index
                    try:
                        worker_conn.execute(
                            "INSERT OR REPLACE INTO captures_fts(rowid, analysis_text, ocr_text, active_window_title) "
                            "SELECT id, analysis_text, ocr_text, active_window_title FROM captures WHERE id=?",
                            (capture_id,),
                        )
                        worker_conn.commit()
                    except Exception:
                        pass

                    # Update LanceDB record with VLM analysis
                    if self.lance_store.is_available:
                        try:
                            self.lance_store.update_analysis(
                                capture_id, analysis.description, analysis.ocr_text
                            )
                        except Exception:
                            pass

                    self._stats["captures_analyzed"] += 1
                    logger.info(
                        f"Analyzed #{capture_id}: {analysis.description[:60]} "
                        f"({analysis.analysis_ms:.0f}ms)"
                    )
            except Exception as e:
                logger.error(f"VLM analysis error for #{capture_id}: {e}")
                self._stats["errors"] += 1

        worker_conn.close()
        logger.info("VLM analysis worker stopped")

    def run(self):
        """Main capture loop. VLM analysis runs in a separate thread."""
        self.running = True
        self._stats["start_time"] = time.time()
        interval = self.config["capture"]["interval_seconds"]

        # Start VLM analysis worker thread
        if self.analyzer.is_available:
            self._analysis_thread = threading.Thread(
                target=self._vlm_worker, daemon=True, name="vlm-worker"
            )
            self._analysis_thread.start()
            logger.info("VLM analysis worker thread started")

        logger.info(f"ScreenMemory daemon started (interval={interval}s)")

        # Handle graceful shutdown
        def shutdown(signum, frame):
            logger.info("Shutdown signal received")
            self.running = False

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        try:
            heartbeat_counter = 0
            while self.running:
                loop_start = time.perf_counter()

                # Guardian heartbeat every 10 loops
                heartbeat_counter += 1
                if heartbeat_counter % 10 == 0:
                    self.guardian.heartbeat()
                    self.guardian.enforce()

                # Get active window info
                window_info = self.capture.get_active_window_info()

                # Privacy check
                if self._should_exclude(window_info):
                    logger.debug(f"Excluded: {window_info.get('process_name', '')}")
                    time.sleep(interval)
                    continue

                # Capture
                if self.config["capture"].get("capture_all_monitors", False):
                    captures = self.capture.capture_all()
                else:
                    result = self.capture.capture_monitor(0)
                    captures = [result] if result else []

                # Process each capture
                for cap in captures:
                    self._process_capture(cap, window_info)

                # Sleep remaining interval
                elapsed = time.perf_counter() - loop_start
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            # Wait for VLM worker to finish queued items
            if self._analysis_thread and self._analysis_thread.is_alive():
                logger.info(f"Waiting for VLM worker to finish "
                            f"({self._analysis_queue.qsize()} items remaining)...")
                self._analysis_thread.join(timeout=60)
            self._print_stats()
            self.db.close()
            self.guardian.unregister(os.getpid())
            logger.info("ScreenMemory daemon stopped")

    def _print_stats(self):
        """Print session statistics."""
        runtime = time.time() - (self._stats["start_time"] or time.time())
        print(f"\n--- ScreenMemory Session Stats ---")
        print(f"Runtime: {runtime/60:.1f} minutes")
        print(f"Captures processed: {self._stats['captures_total']}")
        print(f"Captures analyzed (VLM): {self._stats['captures_analyzed']}")
        print(f"Captures queued for VLM: {self._stats['captures_queued']}")
        print(f"Captures skipped (unchanged): {self._stats['captures_skipped']}")
        print(f"Errors: {self._stats['errors']}")

        db_stats = self.db.get_stats()
        print(f"Total records in DB: {db_stats['total_captures']}")
        print(f"Database size: {db_stats['db_size_mb']:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="ScreenMemory — Local screen history daemon")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--interval", type=float, help="Override capture interval (seconds)")
    parser.add_argument("--no-analysis", action="store_true", help="Skip VLM analysis (capture only)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--stats", action="store_true", help="Show database stats and exit")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.stats:
        config = json.load(open(args.config)) if os.path.exists(args.config) else {}
        db_path = config.get("database", {}).get("path", "data/screen_memory.db")
        if os.path.exists(db_path):
            db = ScreenMemoryDB(db_path)
            stats = db.get_stats()
            print(json.dumps(stats, indent=2))
            db.close()
        else:
            print(f"No database found at {db_path}")
        return

    daemon = ScreenMemoryDaemon(args.config)

    if args.interval:
        daemon.config["capture"]["interval_seconds"] = args.interval

    daemon.run()


if __name__ == "__main__":
    main()
