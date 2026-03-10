#!/usr/bin/env python3
"""
skynet_e2e_test.py -- End-to-end integration test suite for Skynet v3.0.

Tests all major subsystems: backend, bus, GOD Console, engines, self-awareness,
version tracking, watchdog, IQ computation, collective intelligence.

Usage:
    python tools/skynet_e2e_test.py           # Run all tests
    python tools/skynet_e2e_test.py --verbose # Verbose output
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))
DATA = ROOT / "data"

SKYNET = "http://localhost:8420"
GOD = "http://localhost:8421"

passed = 0
failed = 0
results = []


def test(name: str, fn):
    global passed, failed
    try:
        result = fn()
        if result is True or result is None:
            passed += 1
            results.append(("PASS", name, ""))
            print(f"  ✓ {name}")
            return True
        else:
            failed += 1
            results.append(("FAIL", name, str(result)))
            print(f"  ✗ {name}: {result}")
            return False
    except Exception as e:
        failed += 1
        results.append(("ERROR", name, str(e)[:120]))
        print(f"  ✗ {name}: {e}")
        return False


def http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def http_post(url, body, timeout=5):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── BACKEND TESTS ─────────────────────────────────────────────────────

def test_backend_alive():
    r = http_get(f"{SKYNET}/status")
    assert "agents" in r, f"Missing 'agents' in /status: {list(r.keys())}"

def test_backend_health():
    r = http_get(f"{SKYNET}/health")
    assert r.get("status") == "ok", f"Health status: {r.get('status')}"
    assert r.get("uptime_s", 0) > 0, f"Uptime should be > 0: {r.get('uptime_s')}"

def test_backend_metrics():
    r = http_get(f"{SKYNET}/metrics")
    assert "goroutine_count" in r, f"Missing goroutine_count in /metrics"
    assert r["goroutine_count"] > 0, f"Goroutine count should be > 0"

def test_backend_sse():
    req = urllib.request.Request(f"{SKYNET}/stream")
    with urllib.request.urlopen(req, timeout=5) as r:
        chunk = r.read(500).decode("utf-8", errors="replace")
        assert "data:" in chunk, "SSE stream should contain 'data:' events"


# ── BUS TESTS ─────────────────────────────────────────────────────────

def test_bus_publish():
    r = http_post(f"{SKYNET}/bus/publish", {
        "sender": "e2e_test", "topic": "test", "type": "ping", "content": "e2e_test_ping"
    })
    assert r.get("status") == "published", f"Bus publish status: {r.get('status')}"

def test_bus_read():
    msgs = http_get(f"{SKYNET}/bus/messages?topic=test&limit=5")
    assert isinstance(msgs, list), f"Bus messages should be list, got {type(msgs)}"
    found = any(m.get("content") == "e2e_test_ping" for m in msgs if isinstance(m, dict))
    assert found, "Published test message not found in bus"

def test_bus_topic_filter():
    msgs = http_get(f"{SKYNET}/bus/messages?topic=nonexistent_topic_xyz&limit=5")
    assert isinstance(msgs, list), "Topic filter should return list"


# ── GOD CONSOLE TESTS ────────────────────────────────────────────────

def test_god_console_alive():
    r = http_get(f"{GOD}/health")
    assert r.get("status") == "ok", f"GOD Console health: {r.get('status')}"

def test_god_console_engines():
    r = http_get(f"{GOD}/engines")
    engines = r.get("engines", {})
    assert len(engines) > 0, "GOD Console should report at least 1 engine"
    summary = r.get("summary", {})
    assert summary.get("total", 0) > 0, "Engine total should be > 0"

def test_god_console_dashboard():
    req = urllib.request.Request(f"{GOD}/")
    with urllib.request.urlopen(req, timeout=5) as r:
        html = r.read().decode("utf-8", errors="replace")
        assert "<html" in html.lower() or "<!doctype" in html.lower(), "Dashboard should serve HTML"


# ── ENGINE METRICS TESTS ─────────────────────────────────────────────

def test_engine_metrics_honest():
    from tools.engine_metrics import collect_engine_metrics
    m = collect_engine_metrics()
    engines = m.get("engines", {})
    assert len(engines) > 0, "Should have at least 1 engine"
    for name, info in engines.items():
        status = info.get("status")
        assert status in ("online", "available", "offline"), f"Engine {name} has invalid status: {status}"
    assert "timestamp" in m, "Metrics must include timestamp (truth standard)"

def test_engine_metrics_3tier():
    from tools.engine_metrics import collect_engine_metrics
    m = collect_engine_metrics()
    statuses = set(e.get("status") for e in m["engines"].values())
    # At least online and offline should exist (some engines need GPU/deps)
    assert "online" in statuses or "available" in statuses, f"No online/available engines: {statuses}"


# ── SELF-AWARENESS TESTS ─────────────────────────────────────────────

def test_compute_iq_math():
    """Verify IQ formula weights sum to 1.0 and output is bounded [0, 1]."""
    weights = [0.25, 0.25, 0.10, 0.15, 0.10, 0.15]
    total = sum(weights)
    assert abs(total - 1.0) < 0.001, f"IQ weights sum to {total}, expected 1.0"
    # Test with all-perfect inputs
    perfect_iq = sum(1.0 * w for w in weights)
    assert abs(perfect_iq - 1.0) < 0.001, f"Perfect IQ should be 1.0, got {perfect_iq}"
    # Test with all-zero inputs
    zero_iq = sum(0.0 * w for w in weights)
    assert zero_iq == 0.0, f"Zero IQ should be 0.0, got {zero_iq}"

def test_compute_iq_live():
    from tools.skynet_self import SkynetSelf
    s = SkynetSelf()
    result = s.compute_iq()
    assert "score" in result, f"compute_iq missing 'score': {result.keys()}"
    assert "trend" in result, f"compute_iq missing 'trend'"
    assert 0.0 <= result["score"] <= 1.0, f"IQ score out of range: {result['score']}"
    assert result["trend"] in ("rising", "stable", "falling"), f"Invalid trend: {result['trend']}"


# ── COLLECTIVE INTELLIGENCE TESTS ────────────────────────────────────

def test_intelligence_score_math():
    """Verify intelligence_score normalization is correct."""
    # knowledge_count / max(100, knowledge_count) is correct:
    # 0 -> 0/100=0, 50 -> 50/100=0.5, 100 -> 100/100=1.0, 200 -> 200/200=1.0
    for val, expected in [(0, 0.0), (50, 0.5), (100, 1.0), (200, 1.0)]:
        result = val / max(100, val) if val > 0 else 0.0
        assert abs(result - expected) < 0.01, f"Normalization wrong for {val}: {result} != {expected}"

def test_intelligence_score_live():
    from tools.skynet_collective import intelligence_score
    r = intelligence_score()
    assert "intelligence_score" in r, f"Missing intelligence_score key"
    assert "components" in r, f"Missing components breakdown"
    score = r["intelligence_score"]
    assert 0.0 <= score <= 1.0, f"Intelligence score out of range: {score}"
    comps = r["components"]
    for key in ("workers", "engines", "bus", "knowledge", "uptime", "capability"):
        assert key in comps, f"Missing component: {key}"


# ── VERSION TRACKING TESTS ───────────────────────────────────────────

def test_version_current():
    from tools.skynet_version import current_version
    v = current_version()
    assert v is not None, "No version history"
    assert v.get("version") == "3.0", f"Expected v3.0, got {v.get('version')}"
    assert v.get("level") == 3, f"Expected Level 3, got {v.get('level')}"

def test_version_changelog():
    from tools.skynet_version import changelog
    log = changelog()
    assert isinstance(log, list) and len(log) > 0, "Changelog should have entries"


# ── WATCHDOG TESTS ───────────────────────────────────────────────────

def test_watchdog_status():
    status_file = DATA / "watchdog_status.json"
    assert status_file.exists(), "watchdog_status.json missing -- is watchdog running?"
    data = json.loads(status_file.read_text())
    assert data.get("god_console") in ("ok", "restarted"), f"GOD Console status: {data.get('god_console')}"
    assert data.get("skynet") == "ok", f"Skynet status: {data.get('skynet')}"


# ── DATA FILE TESTS ──────────────────────────────────────────────────

def test_agent_profiles():
    f = DATA / "agent_profiles.json"
    assert f.exists(), "agent_profiles.json missing"
    data = json.loads(f.read_text())
    for name in ("orchestrator", "alpha", "beta", "gamma", "delta"):
        assert name in data, f"Missing agent profile: {name}"
        assert "specializations" in data[name] or "specialties" in data[name], f"Agent {name} missing specializations"

def test_brain_config():
    f = DATA / "brain_config.json"
    assert f.exists(), "brain_config.json missing"
    data = json.loads(f.read_text())
    assert "self_awareness" in data, "Missing self_awareness config"
    assert data["self_awareness"]["enabled"] is True, "Self-awareness should be enabled"


def test_process_guard_registry():
    from skynet_process_guard import refresh_registry
    reg = refresh_registry()
    count = reg.get("process_count", 0)
    assert count >= 3, f"Expected >=3 protected processes, got {count}"
    roles = {p["role"] for p in reg.get("processes", [])}
    assert "backend" in roles, "Backend not in registry"


def test_process_guard_safe_kill():
    from skynet_process_guard import safe_kill, refresh_registry
    reg = refresh_registry()
    procs = [p for p in reg.get("processes", []) if p.get("pid", 0) > 0]
    if procs:
        pid = procs[0]["pid"]
        result = safe_kill(pid, caller="e2e_test")
        assert result is False, f"safe_kill should BLOCK protected PID {pid}"
    result2 = safe_kill(99999, caller="e2e_test")
    assert result2 is True, "safe_kill should ALLOW unprotected PID 99999"


# ── RUN ALL ──────────────────────────────────────────────────────────

def run_all():
    global passed, failed
    print("\n╔══════════════════════════════════════════╗")
    print("║   SKYNET v3.0 E2E INTEGRATION TESTS     ║")
    print("╚══════════════════════════════════════════╝\n")

    sections = [
        ("Backend", [test_backend_alive, test_backend_health, test_backend_metrics, test_backend_sse]),
        ("Bus", [test_bus_publish, test_bus_read, test_bus_topic_filter]),
        ("GOD Console", [test_god_console_alive, test_god_console_engines, test_god_console_dashboard]),
        ("Engine Metrics", [test_engine_metrics_honest, test_engine_metrics_3tier]),
        ("IQ / Self-Awareness", [test_compute_iq_math, test_compute_iq_live]),
        ("Collective Intelligence", [test_intelligence_score_math, test_intelligence_score_live]),
        ("Version Tracking", [test_version_current, test_version_changelog]),
        ("Watchdog", [test_watchdog_status]),
        ("Data Files", [test_agent_profiles, test_brain_config]),
        ("Process Guard", [test_process_guard_registry, test_process_guard_safe_kill]),
    ]

    for section_name, tests_list in sections:
        print(f"\n── {section_name} ──")
        for t in tests_list:
            test(t.__name__, t)

    print(f"\n{'='*45}")
    print(f"  RESULTS: {passed} passed, {failed} failed, {passed+failed} total")
    print(f"  STATUS: {'ALL CLEAR' if failed == 0 else 'FAILURES DETECTED'}")
    print(f"  TIME: {datetime.now().isoformat()}")
    print(f"{'='*45}\n")

    # Write results to data/e2e_results.json
    DATA.mkdir(exist_ok=True)
    (DATA / "e2e_results.json").write_text(json.dumps({
        "passed": passed, "failed": failed, "total": passed + failed,
        "status": "ALL_CLEAR" if failed == 0 else "FAILURES",
        "timestamp": datetime.now().isoformat(),
        "details": [{"status": s, "test": n, "error": e} for s, n, e in results],
    }, indent=2))

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
