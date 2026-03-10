"""Full pipeline integration test."""
import sys
import time
import json

sys.path.insert(0, ".")

from core.capture import DXGICapture
from core.change_detector import ChangeDetector
from core.analyzer import ScreenAnalyzer
from core.database import ScreenMemoryDB, ScreenRecord

print("=== FULL PIPELINE TEST ===\n")

# 1. Capture
cap = DXGICapture(use_dxgi=True)
result = cap.capture_monitor(0)
print(f"1. Capture: {result.width}x{result.height} in {result.capture_ms:.0f}ms")

# 2. Change detection
det = ChangeDetector(hash_size=16, threshold=8)
change = det.detect_change(result.image, 0)
print(f"2. Change: detected={change.changed}, hash={change.current_hash[:16]}...")

# 3. Active window
win = cap.get_active_window_info()
title = win.get("title", "unknown")[:60]
proc = win.get("process_name", "unknown")
print(f"3. Window: {proc} — {title}")

# 4. VLM Analysis
analyzer = ScreenAnalyzer(model="moondream", fallback_model="llava:7b")
model_name = analyzer._get_model()
print(f"4. VLM: model={model_name}, available={analyzer.is_available}")

analysis = None
if analyzer.is_available:
    print("   Analyzing screenshot (this may take 10-30s)...")
    analysis = analyzer.analyze(result.image, detailed=True)
    if analysis:
        print(f"   Description: {analysis.description[:200]}")
        print(f"   App: {analysis.active_app}")
        print(f"   Activity: {analysis.activity_type}")
        print(f"   Time: {analysis.analysis_ms:.0f}ms")
        ocr = analysis.ocr_text[:120] if analysis.ocr_text else "(none)"
        print(f"   OCR: {ocr}")
else:
    print("   No vision model — run: ollama pull moondream")

# 5. Database
db = ScreenMemoryDB("data/pipeline_test.db")
record = ScreenRecord(
    timestamp=result.timestamp,
    width=result.width,
    height=result.height,
    dhash=change.current_hash,
    active_window_title=win.get("title", ""),
    active_process=win.get("process_name", ""),
    analysis_text=analysis.description if analysis else "no VLM",
    ocr_text=analysis.ocr_text if analysis else "",
)
rid = db.insert_capture(record)
print(f"5. Database: record #{rid} stored")
stats = db.get_stats()
print(f"   Stats: {json.dumps(stats)}")

# Test text search
results = db.search_text(proc)
print(f"6. Text search for '{proc}': {len(results)} results")

db.close()

print("\n=== PIPELINE TEST COMPLETE ===")
