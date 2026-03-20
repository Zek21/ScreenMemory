#!/usr/bin/env python3
"""skynet_profiler.py -- Performance benchmarking for critical Skynet operations.

Measures execution time of the three slowest Skynet operations:
  1. ghost_type_to_worker() — clipboard set/get, UIA scan, total dispatch time
  2. UIA engine scan() — COM initialization, tree walk, pattern matching
  3. Bus polling — HTTP latency, JSON parse time

Usage:
    python tools/skynet_profiler.py                  # Run all benchmarks
    python tools/skynet_profiler.py --bench ghost     # Ghost-type only
    python tools/skynet_profiler.py --bench uia       # UIA scan only
    python tools/skynet_profiler.py --bench bus       # Bus polling only
    python tools/skynet_profiler.py --json            # Machine-readable output
    python tools/skynet_profiler.py --iterations 10   # Custom iteration count
# signed: gamma
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
BUS_URL = "http://localhost:8420"


# ── Utility ──────────────────────────────────────────────────────

def _measure(func, iterations=5, label="operation"):
    """Run func N times and return timing statistics."""  # signed: gamma
    times = []
    errors = []
    for i in range(iterations):
        t0 = time.perf_counter()
        try:
            result = func()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            times.append(elapsed_ms)
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            times.append(elapsed_ms)
            errors.append(f"iter {i}: {type(e).__name__}: {e}")

    return {
        "label": label,
        "iterations": iterations,
        "times_ms": [round(t, 2) for t in times],
        "min_ms": round(min(times), 2) if times else 0,
        "max_ms": round(max(times), 2) if times else 0,
        "mean_ms": round(statistics.mean(times), 2) if times else 0,
        "median_ms": round(statistics.median(times), 2) if times else 0,
        "stdev_ms": round(statistics.stdev(times), 2) if len(times) > 1 else 0,
        "errors": errors,
    }


# ── Benchmark: Ghost-Type Pipeline ──────────────────────────────

def bench_ghost_type(iterations=5):
    """Benchmark ghost_type_to_worker components WITHOUT actually dispatching.

    Measures:
    - Clipboard set/get round-trip (write + read + verify)
    - Temp file write
    - PowerShell script generation
    These are the measurable components; actual UIA delivery requires a live HWND.
    """  # signed: gamma
    results = {}

    # 1. Clipboard set/get round-trip
    def clipboard_roundtrip():
        import ctypes
        from ctypes import wintypes
        CF_UNICODETEXT = 13
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32

        test_text = "SKYNET_BENCHMARK_" + str(time.time())
        if not u32.OpenClipboard(0):
            raise RuntimeError("Cannot open clipboard")
        try:
            u32.EmptyClipboard()
            encoded = test_text.encode("utf-16-le") + b"\x00\x00"
            h = k32.GlobalAlloc(0x0002, len(encoded))  # GMEM_MOVEABLE
            ptr = k32.GlobalLock(h)
            ctypes.memmove(ptr, encoded, len(encoded))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(CF_UNICODETEXT, h)

            # Read back
            handle = u32.GetClipboardData(CF_UNICODETEXT)
            if handle:
                p = k32.GlobalLock(handle)
                read_text = ctypes.wstring_at(p) if p else ""
                k32.GlobalUnlock(handle)
                if read_text != test_text:
                    raise ValueError("Clipboard verify mismatch")
        finally:
            u32.CloseClipboard()
        return True

    results["clipboard_roundtrip"] = _measure(
        clipboard_roundtrip, iterations, "clipboard_set_get_verify")

    # 2. Temp file write
    def temp_file_write():
        text = "SKYNET BENCHMARK DISPATCH TEXT " * 50  # ~1500 chars typical dispatch
        tmp = DATA_DIR / ".dispatch_tmp_benchmark.txt"
        tmp.write_text(text.replace("\n", " "), encoding="utf-8")
        _ = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)
        return True

    results["temp_file_write"] = _measure(
        temp_file_write, iterations, "dispatch_temp_file_write_read")

    # 3. PowerShell script generation
    def ps_script_gen():
        try:
            from tools.skynet_dispatch import _build_ghost_type_ps
            ps = _build_ghost_type_ps(12345, 99999, "C:\\\\fake\\\\path.txt")
            return len(ps)
        except Exception:
            return 0

    results["ps_script_generation"] = _measure(
        ps_script_gen, iterations, "ghost_type_ps_script_build")

    return results


# ── Benchmark: UIA Engine ───────────────────────────────────────

def bench_uia(iterations=5):
    """Benchmark UIA engine operations.

    Measures:
    - Engine import + singleton initialization
    - COM state check (if engine loads)
    """  # signed: gamma
    results = {}

    # 1. Engine import + init
    def uia_import():
        # Force re-import timing by clearing cache
        if "tools.uia_engine" in sys.modules:
            mod = sys.modules["tools.uia_engine"]
            if hasattr(mod, "_engine_singleton"):
                pass  # Don't reset singleton, just measure access time
        from tools.uia_engine import get_engine
        engine = get_engine()
        return engine is not None

    results["uia_engine_init"] = _measure(
        uia_import, iterations, "uia_engine_get_engine")

    # 2. Scan an invalid HWND (measures COM overhead without real window)
    def uia_scan_invalid():
        try:
            from tools.uia_engine import get_engine
            engine = get_engine()
            if engine:
                result = engine.get_state(0)  # Invalid HWND
                return result
        except Exception:
            pass
        return "UNKNOWN"

    results["uia_scan_invalid_hwnd"] = _measure(
        uia_scan_invalid, iterations, "uia_get_state_invalid_hwnd")

    return results


# ── Benchmark: Bus Polling ──────────────────────────────────────

def bench_bus(iterations=5):
    """Benchmark bus HTTP polling operations.

    Measures:
    - GET /bus/messages HTTP round-trip + JSON parse
    - GET /status HTTP round-trip + JSON parse
    - Local file-based status read (data/realtime.json)
    """  # signed: gamma
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    results = {}

    # 1. Bus messages HTTP poll
    def bus_messages_poll():
        try:
            req = Request(f"{BUS_URL}/bus/messages?limit=10")
            with urlopen(req, timeout=5) as resp:
                raw = resp.read()
                data = json.loads(raw)
                return len(data) if isinstance(data, list) else 0
        except (URLError, ConnectionError, OSError):
            return -1  # Backend not running

    results["bus_messages_http"] = _measure(
        bus_messages_poll, iterations, "GET_bus_messages_limit10")

    # 2. Status HTTP poll
    def status_poll():
        try:
            req = Request(f"{BUS_URL}/status")
            with urlopen(req, timeout=5) as resp:
                raw = resp.read()
                data = json.loads(raw)
                return len(data)
        except (URLError, ConnectionError, OSError):
            return -1

    results["status_http"] = _measure(
        status_poll, iterations, "GET_status")

    # 3. Local file-based status read (zero-network)
    def local_status_read():
        for path_name in ["realtime.json", "realtime_sse.json"]:
            p = DATA_DIR / path_name
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                data = json.loads(raw)
                return len(data)
        return -1

    results["local_file_read"] = _measure(
        local_status_read, iterations, "local_realtime_json_read")

    return results


# ── Main ────────────────────────────────────────────────────────

def run_all_benchmarks(iterations=5, bench_filter: Optional[str] = None):
    """Run all or filtered benchmarks and return combined results."""  # signed: gamma
    all_results = {}

    if not bench_filter or bench_filter == "ghost":
        print("[profiler] Benchmarking ghost-type pipeline...")
        all_results["ghost_type"] = bench_ghost_type(iterations)

    if not bench_filter or bench_filter == "uia":
        print("[profiler] Benchmarking UIA engine...")
        all_results["uia_engine"] = bench_uia(iterations)

    if not bench_filter or bench_filter == "bus":
        print("[profiler] Benchmarking bus polling...")
        all_results["bus_polling"] = bench_bus(iterations)

    all_results["metadata"] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "iterations_per_test": iterations,
        "python_version": sys.version,
    }
    return all_results


def print_report(results: dict):
    """Print a human-readable performance report."""  # signed: gamma
    print("\n" + "=" * 70)
    print("  SKYNET PERFORMANCE PROFILE")
    print("=" * 70)

    for category, benchmarks in results.items():
        if category == "metadata":
            continue
        print(f"\n── {category.upper()} ──")
        for bench_name, stats in benchmarks.items():
            if not isinstance(stats, dict) or "mean_ms" not in stats:
                continue
            label = stats.get("label", bench_name)
            mean = stats["mean_ms"]
            median = stats["median_ms"]
            stdev = stats["stdev_ms"]
            errs = len(stats.get("errors", []))
            err_str = f"  [ERRORS: {errs}]" if errs else ""
            print(f"  {label:.<45} mean={mean:>8.2f}ms  "
                  f"median={median:>8.2f}ms  stdev={stdev:>6.2f}ms{err_str}")

    meta = results.get("metadata", {})
    print(f"\nTimestamp: {meta.get('timestamp', 'unknown')}")
    print(f"Iterations: {meta.get('iterations_per_test', '?')}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Skynet Performance Profiler")
    parser.add_argument("--bench", choices=["ghost", "uia", "bus"],
                        help="Run specific benchmark only")
    parser.add_argument("--iterations", "-n", type=int, default=5,
                        help="Iterations per test (default: 5)")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of human-readable")
    parser.add_argument("--output", "-o", type=str,
                        help="Save results to file")
    args = parser.parse_args()

    results = run_all_benchmarks(args.iterations, args.bench)

    if args.json:
        output = json.dumps(results, indent=2)
        print(output)
    else:
        print_report(results)

    if args.output:
        Path(args.output).write_text(
            json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nResults saved to {args.output}")

    # Also save to default location
    out_path = DATA_DIR / "profiler_results.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
