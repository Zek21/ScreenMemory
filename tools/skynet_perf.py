#!/usr/bin/env python3
"""
skynet_perf.py -- Skynet Performance Profiler.

Benchmarks key operations and identifies bottlenecks.

Usage:
    python tools/skynet_perf.py              # Run all benchmarks
    python tools/skynet_perf.py --json       # JSON output
    python tools/skynet_perf.py --save       # Save to data/perf_report.json
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))

SKYNET = "http://localhost:8420"
GOD = "http://localhost:8421"


def _time_fn(fn, label, iterations=1):
    """Time a function and return result dict."""
    times = []
    result = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            result = fn()
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return {"label": label, "status": "error", "error": str(e)[:100], "ms": round(elapsed, 2)}
    avg = sum(times) / len(times)
    return {"label": label, "status": "ok", "ms": round(avg, 2), "min_ms": round(min(times), 2),
            "max_ms": round(max(times), 2), "iterations": iterations}


def _http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _http_post(url, data, timeout=5):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def bench_bus_roundtrip():
    """Post a message and read it back."""
    tag = f"perf_{time.time_ns()}"
    _http_post(f"{SKYNET}/bus/publish", {"sender": "perf", "topic": tag, "type": "bench", "content": "ping"})
    msgs = _http_get(f"{SKYNET}/bus/messages?topic={tag}&limit=1")
    return len(msgs) if isinstance(msgs, list) else 0


def bench_bus_throughput():
    """Post 20 messages rapidly, measure throughput."""
    t0 = time.perf_counter()
    for i in range(20):
        _http_post(f"{SKYNET}/bus/publish", {"sender": "perf", "topic": "perf_tp", "type": "bench", "content": f"m{i}"})
    elapsed = time.perf_counter() - t0
    return round(20 / elapsed, 1)


def _bench_http_endpoints(base: str, endpoints: list[tuple[str, str]],
                          iterations: int = 3) -> list[dict]:
    """Benchmark a list of (path, label) HTTP endpoints."""
    return [
        _time_fn(lambda p=path: _http_get(f"{base}{p}"), label, iterations=iterations)
        for path, label in endpoints
    ]


def _bench_engine_probes() -> list[dict]:
    """Benchmark engine metric probes (cached and uncached)."""
    results = [_time_fn(
        lambda: __import__("engine_metrics").collect_engine_metrics(),
        "Engine probe (18 engines)", iterations=1
    )]
    try:
        import engine_metrics
        engine_metrics._cache = {}
        engine_metrics._cache_time = 0
        results.append(_time_fn(
            lambda: engine_metrics.collect_engine_metrics(),
            "Engine probe (uncached)", iterations=1
        ))
    except Exception:
        pass
    return results


def _bench_serialization() -> list[dict]:
    """Benchmark JSON serialization (stdlib vs orjson)."""
    import json as stdlib_json
    test_data = {"key": list(range(1000)), "nested": {"a": "b" * 500}}
    results = [_time_fn(lambda: stdlib_json.dumps(test_data), "json.dumps (stdlib)", iterations=100)]
    try:
        import orjson
        results.append(_time_fn(lambda: orjson.dumps(test_data), "orjson.dumps", iterations=100))
    except ImportError:
        results.append({"label": "orjson.dumps", "status": "not_installed", "ms": 0})
    return results


def run_benchmarks():
    """Run all benchmarks and return results."""
    skynet_endpoints = [
        ("/health", "Skynet /health"), ("/status", "Skynet /status"),
        ("/metrics", "Skynet /metrics"), ("/bus/messages?limit=5", "Skynet /bus/messages"),
        ("/bus/tasks", "Skynet /bus/tasks"), ("/bus/convene", "Skynet /bus/convene"),
    ]
    god_endpoints = [
        ("/health", "GOD /health"), ("/engines", "GOD /engines"),
        ("/skynet/self/pulse", "GOD /pulse"), ("/windows", "GOD /windows"),
        ("/skynet/status", "GOD /skynet/status"),
    ]

    results = _bench_http_endpoints(SKYNET, skynet_endpoints, iterations=3)
    results += _bench_http_endpoints(GOD, god_endpoints, iterations=3)
    results.append(_time_fn(bench_bus_roundtrip, "Bus round-trip (post+read)", iterations=5))

    tp_result = _time_fn(bench_bus_throughput, "Bus throughput (20 msgs)")
    tp_result["throughput_msg_s"] = bench_bus_throughput()
    results.append(tp_result)

    results += _bench_engine_probes()
    results.append(_time_fn(
        lambda: __import__("skynet_self").SkynetSelf().compute_iq(),
        "IQ computation", iterations=3))
    results.append(_time_fn(
        lambda: __import__("skynet_windows").scan_windows(),
        "Window scan", iterations=2))
    results += _bench_serialization()
    results.append(_time_fn(
        lambda: __import__("skynet_health_report").collect_report(),
        "Health report (full)", iterations=1))

    results.sort(key=lambda r: r.get("ms", 0), reverse=True)
    slowest = [r for r in results if r.get("status") == "ok"][:3]

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "benchmarks": results,
        "slowest_3": [{"label": r["label"], "ms": r["ms"]} for r in slowest],
        "total_benchmarks": len(results),
    }


def format_report(report: dict) -> str:
    lines = []
    lines.append("=" * 65)
    lines.append("  SKYNET PERFORMANCE REPORT")
    lines.append(f"  Generated: {report['timestamp']}")
    lines.append("=" * 65)

    lines.append(f"\n{'Label':<35s} {'Status':>8s} {'Avg ms':>10s} {'Min':>8s} {'Max':>8s}")
    lines.append("-" * 75)
    for b in report["benchmarks"]:
        status = b.get("status", "?")
        avg = f"{b.get('ms', 0):.1f}"
        mn = f"{b.get('min_ms', 0):.1f}" if "min_ms" in b else "-"
        mx = f"{b.get('max_ms', 0):.1f}" if "max_ms" in b else "-"
        flag = " <<<" if b in report["benchmarks"][:3] and status == "ok" else ""
        lines.append(f"  {b['label']:<33s} {status:>8s} {avg:>10s} {mn:>8s} {mx:>8s}{flag}")

    lines.append(f"\n-- TOP 3 SLOWEST --")
    for s in report["slowest_3"]:
        lines.append(f"  {s['label']}: {s['ms']:.1f}ms")

    lines.append(f"\n{'=' * 65}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Skynet Performance Profiler")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    report = run_benchmarks()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))

    if args.save:
        DATA.mkdir(exist_ok=True)
        (DATA / "perf_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved to data/perf_report.json")


if __name__ == "__main__":
    main()
