#!/usr/bin/env python3
"""
skynet_resilience.py -- Stress-tests the Skynet infrastructure.
Hits all endpoints, measures response times, reports slowest,
tests malformed inputs, and validates crash resilience.

Usage:
    python skynet_resilience.py                # full stress test
    python skynet_resilience.py --quick        # 10 iterations
    python skynet_resilience.py --endpoints    # test endpoints only
    python skynet_resilience.py --malformed    # test malformed inputs only
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent.parent
SKYNET = "http://localhost:8420"
GOD_CONSOLE = "http://localhost:8421"

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_GOLD = "\033[93m"
C_CYAN = "\033[96m"
C_DIM = "\033[2m"

GOD_ENDPOINTS = [
    "/", "/version", "/health", "/engines", "/bus",
    "/skynet/status", "/status", "/god_state", "/dashboard",
]

# These are slow (self-awareness probing) -- test separately with fewer iterations
SLOW_ENDPOINTS = [
    "/skynet/self/pulse", "/skynet/self/status",
    "/skynet/self/introspect", "/skynet/self/goals",
    "/skynet/self/assess",
]

SKYNET_ENDPOINTS = [
    "/status", "/metrics", "/bus/messages?limit=5",
    "/bus/convene", "/stream",
]

MALFORMED_PATHS = [
    "/../../../etc/passwd",
    "/version?callback=<script>alert(1)</script>",
    "/" + "A" * 5000,
    "/bus?limit=abc",
    "/bus?limit=-1",
    "/bus?limit=999999",
    "/skynet/self/pulse?%00null",
    "/engines/../../../status",
    "/\x00null",
    "/%2e%2e/%2e%2e/etc/passwd",
]


def _fetch(url, timeout=5):
    """Fetch URL, return (status, body_len, elapsed_ms, error)."""
    t0 = time.time()
    try:
        req = Request(url, headers={"User-Agent": "SkynetResilience/1.0"})
        resp = urlopen(req, timeout=timeout)
        body = resp.read()
        elapsed = (time.time() - t0) * 1000
        return resp.status, len(body), elapsed, None
    except HTTPError as e:
        elapsed = (time.time() - t0) * 1000
        return e.code, 0, elapsed, str(e)
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        return 0, 0, elapsed, str(e)


def _post(url, body, timeout=5):
    """POST JSON, return (status, elapsed_ms, error)."""
    t0 = time.time()
    try:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=timeout)
        resp.read()
        elapsed = (time.time() - t0) * 1000
        return resp.status, elapsed, None
    except HTTPError as e:
        elapsed = (time.time() - t0) * 1000
        return e.code, elapsed, str(e)
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        return 0, elapsed, str(e)


def _bench_endpoint_list(base_url: str, endpoints: list, iterations: int,
                         timeout: int = 10) -> dict:
    """Benchmark a list of endpoints and return {endpoint: stats_dict}."""
    results = {}
    for ep in endpoints:
        url = f"{base_url}{ep}"
        times = []
        errors = 0
        for _ in range(iterations):
            status, _, ms, err = _fetch(url, timeout=timeout)
            if err or status >= 500:
                errors += 1
            times.append(ms)
        sorted_times = sorted(times)
        results[ep] = {
            "avg": sum(times) / len(times),
            "p50": sorted_times[len(times) // 2],
            "p99": sorted_times[int(len(times) * 0.99)],
            "max": max(times),
            "errors": errors,
        }
    return results


def _print_results_table(results: dict):
    """Print a formatted results table sorted by avg descending."""
    ranked = sorted(results.items(), key=lambda x: -x[1]["avg"])
    print(f"{'ENDPOINT':<30} {'AVG':>8} {'P50':>8} {'P99':>8} {'MAX':>8} {'ERR':>5}")
    print(f"{'-'*30} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*5}")
    for ep, r in ranked:
        color = C_RED if r["errors"] > 0 else (C_GOLD if r["avg"] > 100 else C_GREEN)
        print(f"{color}{ep:<30}{C_RESET} {r['avg']:>7.1f}ms {r['p50']:>7.1f}ms "
              f"{r['p99']:>7.1f}ms {r['max']:>7.1f}ms {r['errors']:>5}")


def stress_endpoints(iterations=100):
    """Hit all GOD Console endpoints N times, measure response times."""
    print(f"\n{C_GOLD}{C_BOLD}ENDPOINT STRESS TEST ({iterations} iterations){C_RESET}")

    results = _bench_endpoint_list(GOD_CONSOLE, GOD_ENDPOINTS, iterations)

    slow_iters = max(3, iterations // 10)
    print(f"\n{C_GOLD}Slow endpoints ({slow_iters} iterations):{C_RESET}")
    results.update(_bench_endpoint_list(GOD_CONSOLE, SLOW_ENDPOINTS, slow_iters, timeout=30))

    _print_results_table(results)

    # Skynet backend endpoints
    print(f"\n{C_GOLD}{C_BOLD}SKYNET BACKEND ENDPOINTS{C_RESET}")
    for ep in SKYNET_ENDPOINTS:
        url = f"{SKYNET}{ep}"
        if ep == "/stream":
            status, _, ms, err = _fetch(url, timeout=2)
            label = f"{C_GREEN}OK{C_RESET}" if status == 200 or err and "timeout" in str(err).lower() else f"{C_RED}FAIL{C_RESET}"
            print(f"  {ep:<30} {label} ({ms:.0f}ms)")
            continue
        times = []
        for _ in range(min(iterations, 20)):
            status, _, ms, err = _fetch(url, timeout=5)
            times.append(ms)
        avg = sum(times) / len(times)
        print(f"  {ep:<30} avg={avg:.1f}ms")

    return results


def test_malformed(base_url=None):
    """Test malformed/adversarial requests."""
    base = base_url or GOD_CONSOLE
    print(f"\n{C_GOLD}{C_BOLD}MALFORMED REQUEST TESTS{C_RESET}")
    crashes = 0

    for path in MALFORMED_PATHS:
        url = f"{base}{path}"
        status, _, ms, err = _fetch(url, timeout=5)
        # Server should NOT crash -- any response is OK
        if status == 0 and err and "ConnectionRefused" in str(err):
            crashes += 1
            print(f"  {C_RED}CRASH{C_RESET} {path[:60]} -- server died!")
        elif status >= 500:
            print(f"  {C_GOLD}500{C_RESET}   {path[:60]} -- {err or 'internal error'}")
        else:
            print(f"  {C_GREEN}{status:>3}{C_RESET}   {path[:60]} ({ms:.0f}ms)")

    # Test malformed POST to bus
    print(f"\n{C_GOLD}Malformed bus POSTs:{C_RESET}")
    bad_posts = [
        ({}, "empty body"),
        ({"sender": None}, "null sender"),
        ({"sender": "x" * 10000, "topic": "t", "type": "t", "content": "c"}, "huge sender"),
        ({"sender": "test", "topic": "", "type": "", "content": ""}, "empty fields"),
    ]
    for body, desc in bad_posts:
        status, ms, err = _post(f"{SKYNET}/bus/publish", body)
        label = f"{C_GREEN}{status}{C_RESET}" if status == 200 else f"{C_GOLD}{status}{C_RESET}"
        print(f"  {label} {desc} ({ms:.0f}ms)")

    # Verify server is still alive after all malformed requests
    status, _, ms, err = _fetch(f"{base}/health", timeout=5)
    if status == 200:
        print(f"\n  {C_GREEN}Server survived all malformed requests{C_RESET}")
    else:
        crashes += 1
        print(f"\n  {C_RED}Server may have crashed after malformed requests!{C_RESET}")

    return crashes


def test_concurrent(n_threads=10, n_requests=50):
    """Concurrent request stress test."""
    print(f"\n{C_GOLD}{C_BOLD}CONCURRENT STRESS ({n_threads} threads, {n_requests} requests){C_RESET}")

    errors = 0
    times = []

    def _hit(i):
        ep = GOD_ENDPOINTS[i % len(GOD_ENDPOINTS)]
        return _fetch(f"{GOD_CONSOLE}{ep}", timeout=10)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_hit, i) for i in range(n_requests)]
        for f in as_completed(futures):
            status, _, ms, err = f.result()
            times.append(ms)
            if err or status >= 500:
                errors += 1

    avg = sum(times) / len(times) if times else 0
    mx = max(times) if times else 0
    print(f"  Requests:  {n_requests}")
    print(f"  Errors:    {C_RED if errors else C_GREEN}{errors}{C_RESET}")
    print(f"  Avg time:  {avg:.1f}ms")
    print(f"  Max time:  {mx:.1f}ms")
    print(f"  Throughput: {n_requests / (sum(times)/1000) if sum(times) > 0 else 0:.0f} req/s")
    return errors


def bus_retry_test():
    """Test bus publish with simulated retries."""
    print(f"\n{C_GOLD}{C_BOLD}BUS RELIABILITY TEST{C_RESET}")
    successes = 0
    for i in range(20):
        status, ms, err = _post(f"{SKYNET}/bus/publish", {
            "sender": "resilience_test",
            "topic": "test",
            "type": "ping",
            "content": f"resilience ping {i}",
        })
        if status == 200:
            successes += 1
    print(f"  Bus publish: {C_GREEN}{successes}/20{C_RESET} succeeded")
    return successes


def full_report(endpoint_results, malformed_crashes, concurrent_errors, bus_successes):
    """Generate summary report."""
    print(f"\n{'='*60}")
    print(f"{C_GOLD}{C_BOLD}RESILIENCE REPORT{C_RESET}")
    print(f"{'='*60}")

    slowest = sorted(endpoint_results.items(), key=lambda x: -x[1]["avg"])[:3]
    print(f"\n  {C_BOLD}Slowest endpoints:{C_RESET}")
    for ep, r in slowest:
        print(f"    {ep}: {r['avg']:.1f}ms avg")

    errored = [(ep, r) for ep, r in endpoint_results.items() if r["errors"] > 0]
    if errored:
        print(f"\n  {C_RED}Endpoints with errors:{C_RESET}")
        for ep, r in errored:
            print(f"    {ep}: {r['errors']} errors")
    else:
        print(f"\n  {C_GREEN}No endpoint errors{C_RESET}")

    print(f"\n  Malformed request crashes: {C_RED if malformed_crashes else C_GREEN}{malformed_crashes}{C_RESET}")
    print(f"  Concurrent errors: {C_RED if concurrent_errors else C_GREEN}{concurrent_errors}{C_RESET}")
    print(f"  Bus reliability: {bus_successes}/20")

    total_score = 100
    total_score -= len(errored) * 5
    total_score -= malformed_crashes * 20
    total_score -= concurrent_errors
    total_score -= max(0, 20 - bus_successes) * 2
    total_score = max(0, total_score)

    color = C_GREEN if total_score >= 80 else (C_GOLD if total_score >= 50 else C_RED)
    print(f"\n  {C_BOLD}Resilience Score: {color}{total_score}/100{C_RESET}")

    return {
        "score": total_score,
        "slowest": [(ep, r["avg"]) for ep, r in slowest],
        "errored_endpoints": len(errored),
        "malformed_crashes": malformed_crashes,
        "concurrent_errors": concurrent_errors,
        "bus_reliability": f"{bus_successes}/20",
    }


def main():
    parser = argparse.ArgumentParser(description="Skynet Resilience Stress Tester")
    parser.add_argument("--quick", action="store_true", help="Quick mode (10 iterations)")
    parser.add_argument("--endpoints", action="store_true", help="Endpoints only")
    parser.add_argument("--malformed", action="store_true", help="Malformed inputs only")
    parser.add_argument("--concurrent", action="store_true", help="Concurrent stress only")
    args = parser.parse_args()

    iterations = 10 if args.quick else 100

    if args.endpoints:
        stress_endpoints(iterations)
        return
    if args.malformed:
        test_malformed()
        return
    if args.concurrent:
        test_concurrent()
        return

    # Full test suite
    ep_results = stress_endpoints(iterations)
    mal_crashes = test_malformed()
    conc_errors = test_concurrent()
    bus_ok = bus_retry_test()
    report = full_report(ep_results, mal_crashes, conc_errors, bus_ok)

    # Save report
    report_file = ROOT / "data" / "resilience_report.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  Report saved: {report_file}")


if __name__ == "__main__":
    main()
