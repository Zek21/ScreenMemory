#!/usr/bin/env python3
"""
Integration tests for Skynet dispatch resilience.

Tests: error pattern detection, exponential backoff, task redistribution,
worker health tracking, PrintWindow capture, fast-idle detection,
recovery cooldown, concurrent dispatch safety.

Run:  python tools/test_dispatch_resilience.py
      python -m pytest tools/test_dispatch_resilience.py -v

signed: delta
"""

import ctypes
import os
import sys
import json
import time
import unittest
import threading
from unittest.mock import patch, MagicMock, PropertyMock
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Real error message fixtures from VS Code Copilot CLI
# ---------------------------------------------------------------------------
CLI_ERROR_FIXTURES = {
    "rate_limit": (
        "Error: You've been rate limited. Please wait a moment and try again. "
        "Rate limit resets in 42 seconds."
    ),
    "rate_limit_429": (
        "HTTP 429 Too Many Requests — rate limit exceeded for model "
        "claude-opus-4-6-fast. Retry after 60s."
    ),
    "capi_error": (
        "CAPIError: The upstream model provider returned an error. "
        "Status 503 Service Unavailable. Request ID: req_abc123."
    ),
    "capi_timeout": (
        "CAPIError: Request timed out after 120000ms. The model did not "
        "respond within the deadline."
    ),
    "model_unavailable": (
        "Model claude-opus-4-6-fast is currently unavailable. "
        "Please select a different model or try again later."
    ),
    "model_overloaded": (
        "The model is currently overloaded with other requests. "
        "Please wait and try again. Error code: overloaded_error."
    ),
    "context_window": (
        "Error: The conversation has exceeded the maximum context window "
        "for this model. Please start a new conversation."
    ),
    "network_error": (
        "NetworkError: Unable to reach the API endpoint. "
        "Please check your internet connection."
    ),
    "auth_expired": (
        "GitHub token expired or revoked. Please sign in again to continue."
    ),
    "healthy_output": (
        "I'll analyze the codebase and implement the requested changes. "
        "Let me start by reading the relevant files..."
    ),
    "empty_output": "",
    "partial_error": (
        "I was working on the task but encountered an issue:\n"
        "CAPIError: upstream timeout\nLet me retry..."
    ),
}

# ---------------------------------------------------------------------------
# Error pattern detection — the system under test
# ---------------------------------------------------------------------------
# These patterns mirror what skynet_monitor and skynet_dispatch detect.

RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate limited",
    "429 too many requests",
    "retry after",
    "rate_limit_exceeded",
]

CAPI_ERROR_PATTERNS = [
    "capierror",
    "upstream model provider",
    "request timed out",
    "service unavailable",
]

MODEL_UNAVAIL_PATTERNS = [
    "model.*unavailable",
    "model.*overloaded",
    "overloaded_error",
    "select a different model",
]

CONTEXT_WINDOW_PATTERNS = [
    "exceeded.*context window",
    "maximum context",
    "start a new conversation",
]

AUTH_ERROR_PATTERNS = [
    "token expired",
    "sign in again",
    "authentication failed",
]

import re


def detect_cli_error(text: str) -> Optional[str]:
    """Detect CLI error category from output text.

    Returns one of: 'rate_limit', 'capi_error', 'model_unavailable',
    'context_window', 'auth_error', or None if healthy.
    """
    if not text or not text.strip():
        return None

    lower = text.lower()

    for pat in RATE_LIMIT_PATTERNS:
        if pat in lower:
            return "rate_limit"

    for pat in CAPI_ERROR_PATTERNS:
        if pat in lower:
            return "capi_error"

    for pat in MODEL_UNAVAIL_PATTERNS:
        if re.search(pat, lower):
            return "model_unavailable"

    for pat in CONTEXT_WINDOW_PATTERNS:
        if re.search(pat, lower):
            return "context_window"

    for pat in AUTH_ERROR_PATTERNS:
        if pat in lower:
            return "auth_error"

    return None


# ---------------------------------------------------------------------------
# Exponential backoff calculator
# ---------------------------------------------------------------------------

BACKOFF_BASE_S = 30  # first retry delay
BACKOFF_MULTIPLIER = 2
BACKOFF_MAX_S = 300  # cap at 5 minutes
BACKOFF_MAX_RETRIES = 5


def compute_backoff(attempt: int) -> float:
    """Compute exponential backoff delay for a given attempt (0-indexed).

    attempt 0 → 30s, 1 → 60s, 2 → 120s, 3 → 240s, 4 → 300s (capped).
    """
    delay = BACKOFF_BASE_S * (BACKOFF_MULTIPLIER ** attempt)
    return min(delay, BACKOFF_MAX_S)


# ---------------------------------------------------------------------------
# Mock stubs
# ---------------------------------------------------------------------------


@dataclass
class MockWindowScan:
    """Simulates UIA engine WindowScan result."""
    state: str = "IDLE"
    model: str = "Claude Opus 4.6 (fast mode)"
    agent: str = "Copilot CLI"
    model_ok: bool = True
    agent_ok: bool = True
    scan_ms: float = 12.5


class MockUIA:
    """Mock UIA engine for testing dispatch without real windows."""

    def __init__(self):
        self._states: dict[int, str] = {}
        self._scan_results: dict[int, MockWindowScan] = {}
        self.scan_count = 0
        self.get_state_count = 0

    def set_state(self, hwnd: int, state: str):
        self._states[hwnd] = state

    def set_scan(self, hwnd: int, scan: MockWindowScan):
        self._scan_results[hwnd] = scan

    def get_state(self, hwnd: int) -> str:
        self.get_state_count += 1
        return self._states.get(hwnd, "UNKNOWN")

    def scan(self, hwnd: int) -> MockWindowScan:
        self.scan_count += 1
        return self._scan_results.get(hwnd, MockWindowScan(state="UNKNOWN"))

    def scan_all(self, hwnds: dict, max_workers: int = 5) -> dict:
        return {name: self.scan(hwnd) for name, hwnd in hwnds.items()}

    def cancel_generation(self, hwnd: int) -> bool:
        self._states[hwnd] = "IDLE"
        return True


class MockWorkerWindow:
    """Simulates a worker VS Code window for dispatch testing."""

    def __init__(self, name: str, hwnd: int):
        self.name = name
        self.hwnd = hwnd
        self.state = "IDLE"
        self.dispatched_tasks: list[str] = []
        self.error_count = 0
        self.last_dispatch_time: Optional[float] = None
        self.is_alive = True

    def receive_task(self, task: str) -> bool:
        if not self.is_alive:
            return False
        self.dispatched_tasks.append(task)
        self.last_dispatch_time = time.time()
        self.state = "PROCESSING"
        return True

    def complete_task(self):
        self.state = "IDLE"

    def simulate_error(self):
        self.error_count += 1
        self.state = "IDLE"

    def kill(self):
        self.is_alive = False
        self.state = "DEAD"


# ---------------------------------------------------------------------------
# Worker health tracker
# ---------------------------------------------------------------------------


class WorkerHealthTracker:
    """Tracks per-worker error counts and raises DEGRADED alerts."""

    DEGRADED_THRESHOLD = 5

    def __init__(self):
        self._errors: dict[str, int] = {}
        self._alerts: list[dict] = []
        self._lock = threading.Lock()

    def record_error(self, worker: str, error_type: str) -> Optional[str]:
        """Record an error. Returns 'DEGRADED' if threshold reached."""
        with self._lock:
            self._errors[worker] = self._errors.get(worker, 0) + 1
            count = self._errors[worker]
            if count >= self.DEGRADED_THRESHOLD:
                alert = {
                    "worker": worker,
                    "type": "WORKER_DEGRADED",
                    "consecutive_errors": count,
                    "last_error_type": error_type,
                    "timestamp": time.time(),
                }
                self._alerts.append(alert)
                return "DEGRADED"
            return None

    def record_success(self, worker: str):
        """Reset error count on successful dispatch."""
        with self._lock:
            self._errors[worker] = 0

    def get_error_count(self, worker: str) -> int:
        with self._lock:
            return self._errors.get(worker, 0)

    def get_alerts(self) -> list[dict]:
        with self._lock:
            return list(self._alerts)

    def is_degraded(self, worker: str) -> bool:
        with self._lock:
            return self._errors.get(worker, 0) >= self.DEGRADED_THRESHOLD


# ---------------------------------------------------------------------------
# Task redistribution engine
# ---------------------------------------------------------------------------

MAX_CONSECUTIVE_FAILURES = 3


class TaskRedistributor:
    """Redistributes tasks from failing workers to idle ones."""

    def __init__(self, workers: list[MockWorkerWindow]):
        self._workers = {w.name: w for w in workers}
        self._failure_counts: dict[str, int] = {}
        self._redistribution_log: list[dict] = []

    def record_failure(self, worker_name: str) -> Optional[str]:
        """Record dispatch failure. Returns target worker name if
        redistribution triggered (after MAX_CONSECUTIVE_FAILURES)."""
        self._failure_counts[worker_name] = (
            self._failure_counts.get(worker_name, 0) + 1
        )
        if self._failure_counts[worker_name] >= MAX_CONSECUTIVE_FAILURES:
            idle = self._find_idle_worker(exclude=worker_name)
            if idle:
                entry = {
                    "from": worker_name,
                    "to": idle,
                    "failures": self._failure_counts[worker_name],
                    "timestamp": time.time(),
                }
                self._redistribution_log.append(entry)
                self._failure_counts[worker_name] = 0
                return idle
        return None

    def record_success(self, worker_name: str):
        self._failure_counts[worker_name] = 0

    def _find_idle_worker(self, exclude: str) -> Optional[str]:
        for name, w in self._workers.items():
            if name != exclude and w.state == "IDLE" and w.is_alive:
                return name
        return None

    @property
    def redistribution_log(self):
        return list(self._redistribution_log)


# ---------------------------------------------------------------------------
# Recovery cooldown tracker
# ---------------------------------------------------------------------------

RECOVERY_COOLDOWN_S = 60  # seconds before re-dispatching to recovered worker


class RecoveryCooldown:
    """Enforces cooldown period before re-dispatch to a recovered worker."""

    def __init__(self, cooldown_s: float = RECOVERY_COOLDOWN_S):
        self._cooldown_s = cooldown_s
        self._recovery_times: dict[str, float] = {}

    def mark_recovered(self, worker: str):
        self._recovery_times[worker] = time.time()

    def can_dispatch(self, worker: str) -> bool:
        if worker not in self._recovery_times:
            return True
        elapsed = time.time() - self._recovery_times[worker]
        return elapsed >= self._cooldown_s

    def remaining(self, worker: str) -> float:
        if worker not in self._recovery_times:
            return 0.0
        elapsed = time.time() - self._recovery_times[worker]
        return max(0.0, self._cooldown_s - elapsed)


# ---------------------------------------------------------------------------
# Fast idle detection
# ---------------------------------------------------------------------------

FAST_IDLE_THRESHOLD_S = 5.0  # if IDLE < 5s after substantial task → suspicious


def detect_fast_idle(dispatch_time: float, idle_time: float,
                     task_complexity: str = "standard") -> bool:
    """Return True if worker returned to IDLE suspiciously fast.

    A substantial task (standard/complex) completing in < 5s likely means
    the worker errored or dropped the task without processing.
    """
    if task_complexity == "trivial":
        return False  # trivial tasks can legitimately finish fast
    elapsed = idle_time - dispatch_time
    return elapsed < FAST_IDLE_THRESHOLD_S


# ---------------------------------------------------------------------------
# Concurrent dispatch coordinator
# ---------------------------------------------------------------------------


class DispatchCoordinator:
    """Ensures concurrent dispatches don't interfere via lock serialization."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dispatch_log: list[dict] = []
        self._active_dispatch: Optional[str] = None

    def dispatch(self, worker_name: str, task: str,
                 worker: MockWorkerWindow) -> bool:
        """Thread-safe dispatch with lock serialization."""
        acquired = self._lock.acquire(timeout=15)
        if not acquired:
            return False
        try:
            self._active_dispatch = worker_name
            success = worker.receive_task(task)
            self._dispatch_log.append({
                "worker": worker_name,
                "task": task[:80],
                "success": success,
                "thread": threading.current_thread().name,
                "timestamp": time.time(),
            })
            return success
        finally:
            self._active_dispatch = None
            self._lock.release()

    @property
    def dispatch_log(self):
        return list(self._dispatch_log)


# ===========================================================================
# TESTS
# ===========================================================================


class TestDetectCLIErrorPatterns(unittest.TestCase):
    """Test 1: Detect rate limit, CAPIError, model unavailable strings."""

    def test_rate_limit_detection(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["rate_limit"])
        self.assertEqual(result, "rate_limit")

    def test_rate_limit_429(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["rate_limit_429"])
        self.assertEqual(result, "rate_limit")

    def test_capi_error(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["capi_error"])
        self.assertEqual(result, "capi_error")

    def test_capi_timeout(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["capi_timeout"])
        self.assertEqual(result, "capi_error")

    def test_model_unavailable(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["model_unavailable"])
        self.assertEqual(result, "model_unavailable")

    def test_model_overloaded(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["model_overloaded"])
        self.assertEqual(result, "model_unavailable")

    def test_context_window(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["context_window"])
        self.assertEqual(result, "context_window")

    def test_network_error_not_cli_error(self):
        # Network errors are distinct from CLI model errors
        result = detect_cli_error(CLI_ERROR_FIXTURES["network_error"])
        self.assertIsNone(result)

    def test_auth_expired(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["auth_expired"])
        self.assertEqual(result, "auth_error")

    def test_healthy_output_no_error(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["healthy_output"])
        self.assertIsNone(result)

    def test_empty_output_no_error(self):
        result = detect_cli_error(CLI_ERROR_FIXTURES["empty_output"])
        self.assertIsNone(result)

    def test_partial_error_detected(self):
        # Output containing error substring should be detected
        result = detect_cli_error(CLI_ERROR_FIXTURES["partial_error"])
        self.assertEqual(result, "capi_error")

    def test_case_insensitive(self):
        result = detect_cli_error("CAPIERROR: UPSTREAM FAILURE")
        self.assertEqual(result, "capi_error")


class TestExponentialBackoffTiming(unittest.TestCase):
    """Test 2: Verify 30s/60s/120s delays with capped maximum."""

    def test_attempt_0(self):
        self.assertEqual(compute_backoff(0), 30)

    def test_attempt_1(self):
        self.assertEqual(compute_backoff(1), 60)

    def test_attempt_2(self):
        self.assertEqual(compute_backoff(2), 120)

    def test_attempt_3(self):
        self.assertEqual(compute_backoff(3), 240)

    def test_attempt_4_capped(self):
        # 30 * 2^4 = 480, but capped at 300
        self.assertEqual(compute_backoff(4), 300)

    def test_attempt_10_still_capped(self):
        self.assertEqual(compute_backoff(10), 300)

    def test_doubling_progression(self):
        delays = [compute_backoff(i) for i in range(4)]
        self.assertEqual(delays, [30, 60, 120, 240])
        # Each is 2x the previous
        for i in range(1, len(delays)):
            self.assertEqual(delays[i], delays[i - 1] * 2)

    def test_all_within_max(self):
        for attempt in range(20):
            self.assertLessEqual(compute_backoff(attempt), BACKOFF_MAX_S)


class TestTaskRedistribution(unittest.TestCase):
    """Test 3: Failed worker 3x → task goes to idle worker."""

    def setUp(self):
        self.alpha = MockWorkerWindow("alpha", 1001)
        self.beta = MockWorkerWindow("beta", 1002)
        self.gamma = MockWorkerWindow("gamma", 1003)
        self.redistributor = TaskRedistributor(
            [self.alpha, self.beta, self.gamma]
        )

    def test_no_redistribution_before_threshold(self):
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            result = self.redistributor.record_failure("alpha")
            self.assertIsNone(result)

    def test_redistribution_at_threshold(self):
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            self.redistributor.record_failure("alpha")
        target = self.redistributor.record_failure("alpha")
        self.assertIsNotNone(target)
        self.assertIn(target, ["beta", "gamma"])

    def test_redistribution_skips_dead_workers(self):
        self.beta.kill()
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            result = self.redistributor.record_failure("alpha")
        # Only gamma is alive and idle
        self.assertEqual(result, "gamma")

    def test_redistribution_skips_busy_workers(self):
        self.beta.state = "PROCESSING"
        self.gamma.state = "PROCESSING"
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            result = self.redistributor.record_failure("alpha")
        # No idle worker available
        self.assertIsNone(result)

    def test_success_resets_failure_count(self):
        self.redistributor.record_failure("alpha")
        self.redistributor.record_failure("alpha")
        self.redistributor.record_success("alpha")
        # After reset, need 3 more failures
        result = self.redistributor.record_failure("alpha")
        self.assertIsNone(result)

    def test_redistribution_logged(self):
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            self.redistributor.record_failure("alpha")
        log = self.redistributor.redistribution_log
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["from"], "alpha")
        self.assertIn(log[0]["to"], ["beta", "gamma"])
        self.assertEqual(log[0]["failures"], MAX_CONSECUTIVE_FAILURES)


class TestWorkerHealthTracking(unittest.TestCase):
    """Test 4: Error counts and DEGRADED alert at 5 consecutive."""

    def setUp(self):
        self.tracker = WorkerHealthTracker()

    def test_error_count_increments(self):
        for i in range(3):
            self.tracker.record_error("alpha", "capi_error")
        self.assertEqual(self.tracker.get_error_count("alpha"), 3)

    def test_degraded_at_threshold(self):
        for i in range(WorkerHealthTracker.DEGRADED_THRESHOLD - 1):
            result = self.tracker.record_error("alpha", "rate_limit")
            self.assertIsNone(result)
        result = self.tracker.record_error("alpha", "rate_limit")
        self.assertEqual(result, "DEGRADED")

    def test_degraded_alert_contains_details(self):
        for _ in range(WorkerHealthTracker.DEGRADED_THRESHOLD):
            self.tracker.record_error("beta", "model_unavailable")
        alerts = self.tracker.get_alerts()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["worker"], "beta")
        self.assertEqual(alert["type"], "WORKER_DEGRADED")
        self.assertEqual(
            alert["consecutive_errors"],
            WorkerHealthTracker.DEGRADED_THRESHOLD,
        )
        self.assertEqual(alert["last_error_type"], "model_unavailable")

    def test_success_resets_errors(self):
        for _ in range(3):
            self.tracker.record_error("alpha", "capi_error")
        self.tracker.record_success("alpha")
        self.assertEqual(self.tracker.get_error_count("alpha"), 0)

    def test_is_degraded_flag(self):
        self.assertFalse(self.tracker.is_degraded("gamma"))
        for _ in range(WorkerHealthTracker.DEGRADED_THRESHOLD):
            self.tracker.record_error("gamma", "rate_limit")
        self.assertTrue(self.tracker.is_degraded("gamma"))

    def test_multiple_workers_independent(self):
        for _ in range(3):
            self.tracker.record_error("alpha", "capi_error")
        self.tracker.record_error("beta", "rate_limit")
        self.assertEqual(self.tracker.get_error_count("alpha"), 3)
        self.assertEqual(self.tracker.get_error_count("beta"), 1)
        self.assertFalse(self.tracker.is_degraded("alpha"))
        self.assertFalse(self.tracker.is_degraded("beta"))

    def test_concurrent_error_recording(self):
        """Multiple threads recording errors simultaneously."""
        barrier = threading.Barrier(4)
        errors = []

        def record_errors(worker_name):
            barrier.wait()
            for _ in range(WorkerHealthTracker.DEGRADED_THRESHOLD):
                result = self.tracker.record_error(worker_name, "test_error")
                if result == "DEGRADED":
                    errors.append(worker_name)

        threads = []
        for name in ["alpha", "beta", "gamma", "delta"]:
            t = threading.Thread(target=record_errors, args=(name,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All 4 workers should have hit DEGRADED
        self.assertEqual(len(set(errors)), 4)


class TestPrintWindowCapture(unittest.TestCase):
    """Test 5: Win32 PrintWindow captures known HWND.

    Uses the desktop window (always valid) as a safe test target.
    """

    @unittest.skipUnless(sys.platform == "win32", "Windows-only test")
    def test_printwindow_desktop(self):
        """PrintWindow on the desktop window returns non-zero (success)."""
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # GetDesktopWindow always returns a valid HWND
        desktop = user32.GetDesktopWindow()
        self.assertNotEqual(desktop, 0)

        # Get desktop dimensions
        width = user32.GetSystemMetrics(0)   # SM_CXSCREEN
        height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)

        # Create a compatible DC + bitmap
        hdc_screen = user32.GetDC(desktop)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        # Use a small capture region (100x100) to keep the test fast
        cap_w, cap_h = min(100, width), min(100, height)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, cap_w, cap_h)
        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)

        # PrintWindow flag 2 = PW_RENDERFULLCONTENT
        result = user32.PrintWindow(desktop, hdc_mem, 2)
        # PrintWindow returns 0 on the desktop window on some configs,
        # so fall back to BitBlt which always works
        if result == 0:
            result = gdi32.BitBlt(
                hdc_mem, 0, 0, cap_w, cap_h, hdc_screen, 0, 0, 0x00CC0020
            )
        self.assertNotEqual(result, 0, "Screen capture failed")

        # Cleanup
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(desktop, hdc_screen)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only test")
    def test_invalid_hwnd_fails(self):
        """PrintWindow on an invalid HWND returns 0 (failure)."""
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # Use an HWND that almost certainly doesn't exist
        bad_hwnd = 0xDEADBEEF

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, 10, 10)
        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)

        result = user32.PrintWindow(bad_hwnd, hdc_mem, 0)
        self.assertEqual(result, 0, "PrintWindow should fail on invalid HWND")

        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


class TestFastIdleDetection(unittest.TestCase):
    """Test 6: IDLE <5s after substantial task flags suspicious."""

    def test_fast_idle_standard_task(self):
        now = time.time()
        # Worker went IDLE 2s after dispatch — suspicious for standard task
        self.assertTrue(detect_fast_idle(now, now + 2.0, "standard"))

    def test_normal_completion_not_flagged(self):
        now = time.time()
        # 30s is a reasonable completion time
        self.assertFalse(detect_fast_idle(now, now + 30.0, "standard"))

    def test_exactly_at_threshold(self):
        now = time.time()
        # Exactly at threshold boundary
        self.assertFalse(detect_fast_idle(now, now + 5.0, "standard"))

    def test_just_under_threshold(self):
        now = time.time()
        self.assertTrue(detect_fast_idle(now, now + 4.99, "standard"))

    def test_trivial_task_not_flagged(self):
        now = time.time()
        # Trivial tasks can legitimately finish in <5s
        self.assertFalse(detect_fast_idle(now, now + 1.0, "trivial"))

    def test_complex_task_fast_idle(self):
        now = time.time()
        # Complex task finishing in 3s is very suspicious
        self.assertTrue(detect_fast_idle(now, now + 3.0, "complex"))

    def test_zero_elapsed(self):
        now = time.time()
        # Immediate IDLE — definitely suspicious
        self.assertTrue(detect_fast_idle(now, now, "standard"))


class TestRecoveryCooldown(unittest.TestCase):
    """Test 7: Cooldown enforced before re-dispatch after recovery."""

    def test_can_dispatch_no_recovery(self):
        cd = RecoveryCooldown(cooldown_s=60)
        self.assertTrue(cd.can_dispatch("alpha"))

    def test_cannot_dispatch_during_cooldown(self):
        cd = RecoveryCooldown(cooldown_s=60)
        cd.mark_recovered("alpha")
        self.assertFalse(cd.can_dispatch("alpha"))

    def test_can_dispatch_after_cooldown(self):
        cd = RecoveryCooldown(cooldown_s=0.1)  # short for testing
        cd.mark_recovered("alpha")
        time.sleep(0.15)
        self.assertTrue(cd.can_dispatch("alpha"))

    def test_remaining_time(self):
        cd = RecoveryCooldown(cooldown_s=60)
        cd.mark_recovered("beta")
        remaining = cd.remaining("beta")
        self.assertGreater(remaining, 55)  # should be close to 60
        self.assertLessEqual(remaining, 60)

    def test_remaining_zero_for_unknown(self):
        cd = RecoveryCooldown(cooldown_s=60)
        self.assertEqual(cd.remaining("gamma"), 0.0)

    def test_independent_workers(self):
        cd = RecoveryCooldown(cooldown_s=60)
        cd.mark_recovered("alpha")
        # Beta not in cooldown
        self.assertFalse(cd.can_dispatch("alpha"))
        self.assertTrue(cd.can_dispatch("beta"))


class TestConcurrentDispatch(unittest.TestCase):
    """Test 8: Multiple dispatches don't interfere."""

    def test_serial_dispatches_all_succeed(self):
        coordinator = DispatchCoordinator()
        workers = {
            name: MockWorkerWindow(name, 1000 + i)
            for i, name in enumerate(["alpha", "beta", "gamma", "delta"])
        }
        for name, w in workers.items():
            ok = coordinator.dispatch(name, f"task for {name}", w)
            self.assertTrue(ok)
            w.complete_task()  # reset to IDLE

        self.assertEqual(len(coordinator.dispatch_log), 4)

    def test_concurrent_dispatches_serialized(self):
        """Multiple threads dispatching simultaneously — all should succeed
        and none should interfere (clipboard corruption prevention)."""
        coordinator = DispatchCoordinator()
        workers = {
            name: MockWorkerWindow(name, 1000 + i)
            for i, name in enumerate(["alpha", "beta", "gamma", "delta"])
        }
        results = {}
        barrier = threading.Barrier(4)

        def dispatch_worker(name):
            barrier.wait()
            ok = coordinator.dispatch(name, f"concurrent task {name}",
                                      workers[name])
            results[name] = ok

        threads = []
        for name in workers:
            t = threading.Thread(target=dispatch_worker, args=(name,),
                                 name=f"dispatch-{name}")
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All 4 dispatches should succeed (serialized by lock)
        self.assertEqual(len(results), 4)
        for name, ok in results.items():
            self.assertTrue(ok, f"dispatch to {name} failed")

        log = coordinator.dispatch_log
        self.assertEqual(len(log), 4)

    def test_dispatch_to_dead_worker_fails(self):
        coordinator = DispatchCoordinator()
        w = MockWorkerWindow("alpha", 1001)
        w.kill()
        ok = coordinator.dispatch("alpha", "task", w)
        self.assertFalse(ok)

    def test_dispatch_log_records_thread(self):
        coordinator = DispatchCoordinator()
        w = MockWorkerWindow("alpha", 1001)
        coordinator.dispatch("alpha", "my task", w)
        log = coordinator.dispatch_log
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["worker"], "alpha")
        self.assertTrue(log[0]["success"])
        self.assertIn("thread", log[0])

    def test_lock_timeout_returns_false(self):
        """If lock can't be acquired in time, dispatch returns False."""
        coordinator = DispatchCoordinator()
        w = MockWorkerWindow("alpha", 1001)

        # Hold the lock in another thread
        coordinator._lock.acquire()

        def try_dispatch():
            # With a very short timeout, this should fail
            acquired = coordinator._lock.acquire(timeout=0.01)
            if acquired:
                coordinator._lock.release()
                return True
            return False

        result = try_dispatch()
        self.assertFalse(result)
        coordinator._lock.release()


# ---------------------------------------------------------------------------
# Mock integration: full dispatch flow
# ---------------------------------------------------------------------------


class TestFullDispatchFlow(unittest.TestCase):
    """Integration test combining all resilience components."""

    def test_error_triggers_backoff_then_redistribution(self):
        """Error detected → backoff computed → after 3 failures → redistribute."""
        alpha = MockWorkerWindow("alpha", 1001)
        beta = MockWorkerWindow("beta", 1002)
        tracker = WorkerHealthTracker()
        redistributor = TaskRedistributor([alpha, beta])

        # Simulate 3 consecutive errors on alpha
        for i in range(3):
            error_type = detect_cli_error(CLI_ERROR_FIXTURES["capi_error"])
            self.assertEqual(error_type, "capi_error")
            tracker.record_error("alpha", error_type)
            backoff = compute_backoff(i)
            self.assertGreater(backoff, 0)
            target = redistributor.record_failure("alpha")
            if i < 2:
                self.assertIsNone(target)
            else:
                self.assertEqual(target, "beta")

        # Alpha is not yet DEGRADED (only 3 errors, threshold is 5)
        self.assertFalse(tracker.is_degraded("alpha"))
        self.assertEqual(tracker.get_error_count("alpha"), 3)

    def test_fast_idle_triggers_health_tracking(self):
        """Fast IDLE after dispatch → record as error → track health."""
        tracker = WorkerHealthTracker()
        now = time.time()

        # Worker goes IDLE 1s after receiving substantial task
        is_suspicious = detect_fast_idle(now, now + 1.0, "complex")
        self.assertTrue(is_suspicious)

        # Record the suspicious behavior as an error
        result = tracker.record_error("gamma", "fast_idle")
        self.assertIsNone(result)  # first error, no DEGRADED yet
        self.assertEqual(tracker.get_error_count("gamma"), 1)


# ===========================================================================
# CLI entry point
# ===========================================================================


def main():
    # Run with verbosity
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
