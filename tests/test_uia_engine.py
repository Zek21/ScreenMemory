#!/usr/bin/env python3
"""Tests for the UIA Engine — validates COM-based window scanning."""

import sys
import json
import time

sys.path.insert(0, "D:\\Prospects\\ScreenMemory")

import pytest

WORKERS_FILE = "D:\\Prospects\\ScreenMemory\\data\\workers.json"
ORCHESTRATOR_FILE = "D:\\Prospects\\ScreenMemory\\data\\orchestrator.json"

VALID_STATES = ("IDLE", "PROCESSING", "STEERING", "TYPING", "UNKNOWN")


@pytest.fixture(scope="module")
def engine():
    from tools.uia_engine import UIAEngine
    return UIAEngine()


@pytest.fixture(scope="module")
def workers_data():
    with open(WORKERS_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def first_worker_hwnd(workers_data):
    return workers_data["workers"][0]["hwnd"]


@pytest.fixture(scope="module")
def orchestrator_hwnd(workers_data):
    return workers_data.get("orchestrator_hwnd", 0)


@pytest.fixture(scope="module")
def all_hwnds(workers_data):
    hwnds = {w["name"]: w["hwnd"] for w in workers_data["workers"]}
    orch = workers_data.get("orchestrator_hwnd")
    if orch:
        hwnds["orchestrator"] = orch
    return hwnds


def test_engine_instantiates():
    from tools.uia_engine import UIAEngine
    engine = UIAEngine()
    assert engine is not None


def test_scan_worker(engine, first_worker_hwnd):
    result = engine.scan(first_worker_hwnd)
    assert result.state in VALID_STATES, f"Unexpected state: {result.state}"
    assert "Pick Model" in result.model, f"Model button not found: {result.model!r}"
    assert result.scan_ms < 500, f"Scan too slow: {result.scan_ms:.1f}ms"


def test_scan_orchestrator(engine, orchestrator_hwnd):
    result = engine.scan(orchestrator_hwnd)
    assert result.model_ok is True, f"Model not OK: {result.model!r}"
    assert result.agent_ok is True, f"Agent not OK: {result.agent!r}"


def test_scan_all(engine, all_hwnds):
    results = engine.scan_all(all_hwnds)
    assert len(results) == len(all_hwnds), f"Expected {len(all_hwnds)} results, got {len(results)}"
    for name, scan in results.items():
        assert scan.state in VALID_STATES, f"{name}: unexpected state {scan.state}"


def test_get_state(engine, first_worker_hwnd):
    state = engine.get_state(first_worker_hwnd)
    assert state in VALID_STATES, f"Unexpected state: {state}"


def test_performance(engine, first_worker_hwnd):
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        engine.scan(first_worker_hwnd)
        times.append((time.perf_counter() - t0) * 1000)
    avg = sum(times) / len(times)
    assert avg < 200, f"Average scan time too slow: {avg:.1f}ms (target <200ms)"
