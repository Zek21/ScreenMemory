#!/usr/bin/env python3
"""Benchmark Win32 IsWindow API call latency.

Measures per-call latency in microseconds over N calls across M window handles.
Reproduces the paper's claim: 0.33 μs/call, 3.03M checks/second (n=4000).

Usage:
    python tools/benchmark_iswindow.py
    python tools/benchmark_iswindow.py --calls 10000
"""
# signed: gamma

import ctypes
import ctypes.wintypes
import time
import json
import statistics
import argparse

user32 = ctypes.windll.user32
IsWindow = user32.IsWindow
IsWindow.argtypes = [ctypes.wintypes.HWND]
IsWindow.restype = ctypes.wintypes.BOOL

EnumWindows = user32.EnumWindows
IsWindowVisible = user32.IsWindowVisible
GetWindowTextW = user32.GetWindowTextW
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def _find_visible_windows(limit=4):
    """Find up to `limit` visible top-level windows with titles."""
    results = []

    def callback(hwnd, _):
        if IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, buf, 256)
            if buf.value.strip():
                results.append(int(hwnd))
        return len(results) < limit * 3  # gather extras, pick first N

    EnumWindows(WNDENUMPROC(callback), 0)
    return results[:limit]


def benchmark_iswindow(hwnds, total_calls=4000):
    """Run IsWindow benchmark and return stats dict."""
    calls_per_hwnd = total_calls // len(hwnds)
    actual_total = calls_per_hwnd * len(hwnds)

    # Warmup — prime the syscall path
    for h in hwnds:
        for _ in range(100):
            IsWindow(h)

    # Timed run
    t0 = time.perf_counter()
    for _ in range(calls_per_hwnd):
        for h in hwnds:
            IsWindow(h)
    elapsed_s = time.perf_counter() - t0

    elapsed_us = elapsed_s * 1_000_000
    per_call_us = elapsed_us / actual_total
    throughput = actual_total / elapsed_s

    # Run 10 batches for CI estimation
    batch_times = []
    batch_size = actual_total // 10
    for _ in range(10):
        bt0 = time.perf_counter()
        for _ in range(batch_size // len(hwnds)):
            for h in hwnds:
                IsWindow(h)
        batch_times.append((time.perf_counter() - bt0) * 1_000_000 / batch_size)

    mean_us = statistics.mean(batch_times)
    stdev_us = statistics.stdev(batch_times) if len(batch_times) > 1 else 0
    # 95% CI with t-distribution approximation (t ≈ 2.262 for df=9)
    ci_margin = 2.262 * stdev_us / (len(batch_times) ** 0.5)

    return {
        "hwnds": len(hwnds),
        "total_calls": actual_total,
        "elapsed_ms": round(elapsed_s * 1000, 3),
        "per_call_us": round(per_call_us, 3),
        "throughput_per_sec": int(throughput),
        "batch_mean_us": round(mean_us, 4),
        "batch_stdev_us": round(stdev_us, 4),
        "ci_95_low_us": round(mean_us - ci_margin, 4),
        "ci_95_high_us": round(mean_us + ci_margin, 4),
        "batches": 10,
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark Win32 IsWindow API")
    parser.add_argument("--calls", type=int, default=4000, help="Total calls (default 4000)")
    parser.add_argument("--windows", type=int, default=4, help="Number of windows (default 4)")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    hwnds = _find_visible_windows(args.windows)
    if not hwnds:
        print("ERROR: No visible windows found")
        return

    result = benchmark_iswindow(hwnds, args.calls)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=" * 60)
        print("IsWindow API Benchmark")
        print("=" * 60)
        print(f"  Windows:     {result['hwnds']}")
        print(f"  Total calls: {result['total_calls']}")
        print(f"  Elapsed:     {result['elapsed_ms']:.3f} ms")
        print(f"  Per-call:    {result['per_call_us']:.3f} μs")
        print(f"  Throughput:  {result['throughput_per_sec']:,} calls/sec")
        print(f"  Mean (10 batches): {result['batch_mean_us']:.4f} μs")
        print(f"  Stdev:       {result['batch_stdev_us']:.4f} μs")
        print(f"  95% CI:      [{result['ci_95_low_us']:.4f}, {result['ci_95_high_us']:.4f}] μs")
        print("=" * 60)

        # Compare with paper claim
        paper_us = 0.33
        print(f"\n  Paper claims: {paper_us} μs/call, 3,030,073 calls/sec")
        ratio = result['per_call_us'] / paper_us
        print(f"  Measured/Paper ratio: {ratio:.2f}x")
        if 0.5 <= ratio <= 2.0:
            print("  VERDICT: CONSISTENT with paper claim")
        elif ratio < 0.5:
            print("  VERDICT: FASTER than paper claim (hardware-dependent)")
        else:
            print("  VERDICT: SLOWER than paper claim (may be hardware-dependent)")

    return result


if __name__ == "__main__":
    main()
