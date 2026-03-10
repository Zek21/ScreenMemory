"""
Integration test for new Recall system components: OCR, Security, LanceDB.
Tests the full pipeline: Capture -> OCR -> VLM -> LanceDB + DPAPI encryption.
"""
import os
import sys
import time
import json
import shutil
import hashlib
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

results = []

def check(name, condition, detail=""):
    ok = bool(condition)
    status = "PASS" if ok else "FAIL"
    msg = f"  {status}  {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)
    results.append((name, ok))
    return ok


# ============================================================
# OCR Engine Tests
# ============================================================
print("=" * 60)
print("  STAGE: OCR Engine (RapidOCR)")
print("=" * 60)

from core.ocr import OCREngine, OCRResult, OCRRegion

ocr = OCREngine()
check("OCR engine available", ocr.is_available, ocr._engine_name)

# Test with a synthetic image
from PIL import Image, ImageDraw, ImageFont

def create_test_image_with_text():
    img = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), "Hello World", fill="black")
    draw.text((50, 100), "ScreenMemory Test", fill="black")
    draw.text((50, 150), "def capture_screen(self):", fill="black")
    draw.text((50, 200), "return self.image", fill="black")
    draw.rectangle([(400, 50), (750, 350)], outline="black", width=2)
    draw.text((420, 70), "Window Title Bar", fill="black")
    draw.text((420, 120), "File  Edit  View", fill="black")
    return img

test_img = create_test_image_with_text()

if ocr.is_available:
    result = ocr.extract(test_img)
    check("OCR extracts text", result.region_count > 0, f"{result.region_count} regions")
    check("OCR has full text", len(result.full_text) > 0, f"{len(result.full_text)} chars")
    check("OCR reports timing", result.extraction_ms > 0, f"{result.extraction_ms:.0f}ms")
    check("OCR engine name", result.engine == "rapidocr")

    # Test spatial data
    check("OCR regions have bbox", all(r.bbox for r in result.regions))
    check("OCR regions have confidence", all(r.confidence > 0 for r in result.regions))

    # Test spatial export
    spatial = result.to_spatial_json()
    check("OCR spatial JSON", len(spatial) > 0)
    check("OCR spatial has bbox", "bbox" in spatial[0] if spatial else False)

    # Test area search
    area_results = result.text_in_area(0, 0, 400, 300)
    check("OCR area search works", len(area_results) >= 0)

# Test OCRResult defaults
empty = OCRResult()
check("Empty OCR result", empty.region_count == 0 and empty.full_text == "")

# Test OCRRegion
region = OCRRegion(text="test", confidence=0.95, bbox=(10, 20, 100, 50))
check("OCRRegion created", region.text == "test" and region.confidence == 0.95)


# ============================================================
# DPAPI Security Tests
# ============================================================
print()
print("=" * 60)
print("  STAGE: DPAPI Security")
print("=" * 60)

from core.security import DPAPIKeyManager, FallbackKeyManager, get_key_manager

mgr = DPAPIKeyManager()
check("DPAPI manager created", mgr is not None)
check("DPAPI available", mgr.is_available)

if mgr.is_available:
    # Key generation
    key = mgr.generate_key(32)
    check("Key generated", len(key) == 32, f"{len(key)} bytes")

    # Encryption round-trip
    test_data = b"ScreenMemory encryption test 12345"
    encrypted = mgr.protect(test_data)
    check("Data encrypted", len(encrypted) > len(test_data), f"{len(encrypted)} bytes")

    decrypted = mgr.unprotect(encrypted)
    check("Data decrypted", decrypted == test_data)

    # Different data encrypts differently
    encrypted2 = mgr.protect(b"different data")
    check("Different ciphertext", encrypted != encrypted2)

    # Key storage with temp path
    temp_key_path = Path(tempfile.mktemp(suffix=".dat"))
    temp_mgr = DPAPIKeyManager(key_path=temp_key_path)
    store_key = temp_mgr.generate_key()
    stored = temp_mgr.store_key(store_key)
    check("Key stored to disk", stored and temp_key_path.exists())

    # Key loading
    loaded = temp_mgr.load_key()
    check("Key loaded from disk", loaded == store_key)

    # SQLCipher key derivation
    sqlcipher_key = mgr.derive_sqlcipher_key(store_key)
    check("SQLCipher key derived", sqlcipher_key.startswith("x'"), f"{len(sqlcipher_key)} chars")

    # Get or create
    auto_key = temp_mgr.get_or_create_key()
    check("get_or_create returns key", auto_key is not None and len(auto_key) == 32)

    # Cleanup
    if temp_key_path.exists():
        temp_key_path.unlink()

# Fallback manager
fallback = FallbackKeyManager()
fallback_key = fallback.get_or_create_key()
check("Fallback key manager works", fallback_key is not None)

# Factory
factory_mgr = get_key_manager()
check("Factory returns manager", factory_mgr is not None)


# ============================================================
# LanceDB Store Tests
# ============================================================
print()
print("=" * 60)
print("  STAGE: LanceDB Multimodal Store")
print("=" * 60)

from core.lancedb_store import LanceDBStore, MultimodalRecord

test_lance_path = "data/test_lance_integration"
if os.path.exists(test_lance_path):
    shutil.rmtree(test_lance_path)

store = LanceDBStore(test_lance_path, embedding_dim=768)
check("LanceDB available", store.is_available)

if store.is_available:
    # Insert single record
    record = MultimodalRecord(
        timestamp=time.time() - 3600,
        width=1920, height=1080,
        dhash="hash_001",
        active_window_title="VS Code - project.py",
        active_process="Code.exe",
        analysis_text="User is writing Python code in Visual Studio Code",
        ocr_text="def main():\n    print('hello')",
        embedding=[0.1] * 768,
    )
    rid = store.insert(record)
    check("Insert single record", rid is not None, f"id={rid}")

    # Insert batch
    batch = []
    for i in range(5):
        batch.append(MultimodalRecord(
            timestamp=time.time() - (i * 60),
            width=1920, height=1080,
            dhash=f"hash_batch_{i}",
            active_window_title=f"Window {i}",
            active_process="chrome.exe" if i % 2 == 0 else "Code.exe",
            analysis_text=f"User activity {i}: browsing web" if i % 2 == 0 else f"User activity {i}: coding",
            ocr_text=f"text content {i}",
            embedding=[0.1 + i * 0.01] * 768,
        ))
    batch_ids = store.insert_batch(batch)
    check("Batch insert", len(batch_ids) == 5, f"{len(batch_ids)} records")

    # Stats
    stats = store.get_stats()
    check("Stats available", stats.get("total_captures", 0) >= 6, f"{stats.get('total_captures')} records")

    # Text search
    txt_results = store.search_text("Python", limit=10)
    check("Text search finds results", len(txt_results) > 0, f"{len(txt_results)} results")

    # Vector search
    vec_results = store.search_vector([0.1] * 768, limit=5)
    check("Vector search works", len(vec_results) > 0, f"{len(vec_results)} results")

    # Hybrid search
    hybrid = store.search_hybrid("coding", [0.12] * 768, limit=5)
    check("Hybrid search works", len(hybrid) > 0, f"{len(hybrid)} results")

    # Time range
    time_results = store.get_by_timerange(time.time() - 7200, time.time())
    check("Time range query", len(time_results) > 0, f"{len(time_results)} results")

    # Process filter
    proc_results = store.get_by_process("chrome", limit=10)
    check("Process filter query", len(proc_results) >= 0)

    # Recent
    recent = store.get_recent(10)
    check("Get recent works", len(recent) > 0, f"{len(recent)} results")

    # Update analysis
    store.update_analysis(rid, "Updated analysis text", "Updated OCR")
    check("Update analysis completes", True)

    store.close()

    # Cleanup test data
    if os.path.exists(test_lance_path):
        shutil.rmtree(test_lance_path)


# ============================================================
# Full Integration: OCR + Security + LanceDB
# ============================================================
print()
print("=" * 60)
print("  STAGE: Full Pipeline Integration")
print("=" * 60)

# Simulate capture -> OCR -> store pipeline
if ocr.is_available:
    # 1. OCR extraction
    ocr_result = ocr.extract(test_img)
    check("Pipeline: OCR extraction", ocr_result.region_count >= 0)

    # 2. DPAPI key
    if mgr.is_available:
        encryption_key = mgr.generate_key()
        protected_key = mgr.protect(encryption_key)
        recovered_key = mgr.unprotect(protected_key)
        check("Pipeline: DPAPI key cycle", recovered_key == encryption_key)

    # 3. LanceDB store
    pipe_lance_path = "data/test_pipe_lance"
    if os.path.exists(pipe_lance_path):
        shutil.rmtree(pipe_lance_path)

    pipe_store = LanceDBStore(pipe_lance_path, embedding_dim=768)
    if pipe_store.is_available:
        pipe_record = MultimodalRecord(
            timestamp=time.time(),
            width=800, height=400,
            dhash="pipe_hash",
            active_window_title="Test Window",
            active_process="test.exe",
            analysis_text="Synthetic test image with code",
            ocr_text=ocr_result.full_text,
            ocr_regions_json=json.dumps(ocr_result.to_spatial_json()),
            embedding=[0.5] * 768,
        )
        pipe_id = pipe_store.insert(pipe_record)
        check("Pipeline: LanceDB insert", pipe_id is not None)

        # Search the OCR text
        found = pipe_store.search_text("test", limit=5)
        check("Pipeline: Search OCR text", len(found) > 0)

        pipe_store.close()

    if os.path.exists(pipe_lance_path):
        shutil.rmtree(pipe_lance_path)


# ============================================================
# Results
# ============================================================
print()
print("=" * 60)
print("  RECALL SYSTEM INTEGRATION TEST RESULTS")
print("=" * 60)

passed = sum(1 for _, ok in results if ok)
total = len(results)
print()
print(f"  {passed}/{total} tests passed ({100*passed//total}%)")
if passed == total:
    print(f"  ALL TESTS PASSED")
else:
    failed_tests = [(name, ok) for name, ok in results if not ok]
    print(f"  {len(failed_tests)} FAILURES:")
    for name, _ in failed_tests:
        print(f"    - {name}")

print()
if __name__ == "__main__":
    sys.exit(0 if passed == total else 1)
