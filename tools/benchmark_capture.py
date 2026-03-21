#!/usr/bin/env python3
"""Benchmark mss screen capture at different resolutions.

Measures capture latency for various region sizes and compares with paper claims.
Paper reports: 4.99ms (200x200), 7.54ms (400x300), 22.15ms (1920x1080).

Usage:
    python tools/benchmark_capture.py
    python tools/benchmark_capture.py --iterations 50
"""
# signed: gamma

import time
import json
import statistics
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def benchmark_mss_capture(iterations=20):
    """Benchmark mss capture at multiple region sizes."""
    import mss

    regions = [
        {"name": "100x100", "w": 100, "h": 100},
        {"name": "200x200", "w": 200, "h": 200},
        {"name": "400x300", "w": 400, "h": 300},
        {"name": "800x600", "w": 800, "h": 600},
        {"name": "1920x1080", "w": 1920, "h": 1080},
    ]

    paper_claims_ms = {
        "200x200": 4.99,
        "400x300": 7.54,
        "1920x1080": 22.15,
    }

    results = []
    sct = mss.mss()
    monitors = sct.monitors

    # Use primary monitor (index 1 in mss)
    primary = monitors[1] if len(monitors) > 1 else monitors[0]

    for region in regions:
        mon = {
            "left": primary["left"],
            "top": primary["top"],
            "width": min(region["w"], primary["width"]),
            "height": min(region["h"], primary["height"]),
        }

        # Warmup
        for _ in range(3):
            sct.grab(mon)

        # Timed iterations
        times_ms = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            sct.grab(mon)
            elapsed = (time.perf_counter() - t0) * 1000
            times_ms.append(elapsed)

        mean_ms = statistics.mean(times_ms)
        stdev_ms = statistics.stdev(times_ms) if len(times_ms) > 1 else 0
        min_ms = min(times_ms)
        max_ms = max(times_ms)
        p50 = statistics.median(times_ms)
        sorted_t = sorted(times_ms)
        p95 = sorted_t[int(len(sorted_t) * 0.95)] if len(sorted_t) >= 20 else max_ms

        paper_ms = paper_claims_ms.get(region["name"])

        entry = {
            "region": region["name"],
            "pixels": region["w"] * region["h"],
            "iterations": iterations,
            "mean_ms": round(mean_ms, 2),
            "stdev_ms": round(stdev_ms, 2),
            "min_ms": round(min_ms, 2),
            "max_ms": round(max_ms, 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "paper_claim_ms": paper_ms,
        }
        if paper_ms:
            entry["ratio_vs_paper"] = round(mean_ms / paper_ms, 2)

        results.append(entry)

    sct.close()
    return results


def benchmark_dxgi_capture(iterations=20):
    """Benchmark using the DXGICapture class from core/capture.py."""
    try:
        from core.capture import DXGICapture
        cap = DXGICapture(use_dxgi=True)
    except Exception as e:
        return {"error": str(e)}

    # Full monitor capture
    times_ms = []
    for _ in range(3):  # warmup
        cap.capture_monitor(0)

    for _ in range(iterations):
        result = cap.capture_monitor(0)
        if result:
            times_ms.append(result.capture_ms)

    if not times_ms:
        return {"error": "No successful captures"}

    return {
        "method": "DXGICapture (mss backend)",
        "iterations": len(times_ms),
        "mean_ms": round(statistics.mean(times_ms), 2),
        "stdev_ms": round(statistics.stdev(times_ms), 2) if len(times_ms) > 1 else 0,
        "min_ms": round(min(times_ms), 2),
        "max_ms": round(max(times_ms), 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark screen capture")
    parser.add_argument("--iterations", type=int, default=20, help="Iterations per region (default 20)")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    mss_results = benchmark_mss_capture(args.iterations)
    dxgi_result = benchmark_dxgi_capture(args.iterations)

    output = {
        "mss_regions": mss_results,
        "dxgi_capture": dxgi_result,
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("=" * 70)
        print("Screen Capture Benchmark (mss / GDI BitBlt)")
        print("=" * 70)
        print(f"{'Region':<12} {'Mean ms':>8} {'σ ms':>7} {'Min':>7} {'P50':>7} {'P95':>7} {'Paper':>7} {'Ratio':>6}")
        print("-" * 70)
        for r in mss_results:
            paper = f"{r['paper_claim_ms']:.2f}" if r.get('paper_claim_ms') else "  —"
            ratio = f"{r['ratio_vs_paper']:.2f}" if r.get('ratio_vs_paper') else " —"
            print(f"{r['region']:<12} {r['mean_ms']:>8.2f} {r['stdev_ms']:>7.2f} "
                  f"{r['min_ms']:>7.2f} {r['p50_ms']:>7.2f} {r['p95_ms']:>7.2f} "
                  f"{paper:>7} {ratio:>6}")
        print("-" * 70)
        print(f"  Iterations per region: {args.iterations}")

        if isinstance(dxgi_result, dict) and "mean_ms" in dxgi_result:
            print(f"\n  DXGICapture class: {dxgi_result['mean_ms']:.2f} ms mean "
                  f"(σ={dxgi_result['stdev_ms']:.2f}, n={dxgi_result['iterations']})")

        print("\n  Paper claims: 4.99ms (200×200), 7.54ms (400×300), 22.15ms (1920×1080)")
        print("=" * 70)

    return output


if __name__ == "__main__":
    main()
