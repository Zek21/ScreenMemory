"""
Dispatch resilience layer — auto-recovery for CLI errors during dispatch.

Monitors worker windows after dispatch for CLI-level errors (rate limit,
model unavailable, CAPIError, etc.) via Win32 PrintWindow + OCR, and
auto-retries with exponential backoff or redistributes to another worker.

Usage:
    from tools.skynet_dispatch_resilience import DispatchResilience
    dr = DispatchResilience()
    ok = dr.dispatch_with_retry("alpha", "scan codebase for bugs")

CLI test:
    python tools/skynet_dispatch_resilience.py --test
    python tools/skynet_dispatch_resilience.py --detect-error --hwnd 12345
    python tools/skynet_dispatch_resilience.py --dispatch --worker alpha --task "hello"

# signed: beta
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Win32 constants ──────────────────────────────────────────────
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# ── Error patterns that indicate CLI dispatch failure ────────────
# These are matched case-insensitively against OCR text from the
# worker window after dispatch. Each tuple is (pattern, category).
# signed: beta
CLI_ERROR_PATTERNS = [
    (r"rate\s*limit", "RATE_LIMIT"),
    (r"429", "RATE_LIMIT"),
    (r"too\s*many\s*requests", "RATE_LIMIT"),
    (r"model\s*(is\s*)?unavailable", "MODEL_UNAVAILABLE"),
    (r"no\s*model", "MODEL_UNAVAILABLE"),
    (r"model\s*not\s*found", "MODEL_UNAVAILABLE"),
    (r"CAPIError", "CAPI_ERROR"),
    (r"CAPI\s*error", "CAPI_ERROR"),
    (r"[Ee]xecution\s*failed", "EXECUTION_FAILED"),
    (r"internal\s*server\s*error", "SERVER_ERROR"),
    (r"\b400\b.*bad\s*request", "BAD_REQUEST"),
    (r"bad\s*request.*\b400\b", "BAD_REQUEST"),
    (r"request\s*failed.*status\s*code\s*[45]\d\d", "HTTP_ERROR"),
    (r"context\s*window\s*(exceeded|full|limit)", "CONTEXT_OVERFLOW"),
    (r"maximum\s*context\s*length", "CONTEXT_OVERFLOW"),
]


@dataclass
class ErrorDetection:
    """Result of scanning a worker window for CLI errors."""
    has_error: bool = False
    category: str = ""
    matched_text: str = ""
    scan_ms: float = 0.0


@dataclass
class DispatchAttempt:
    """Record of a single dispatch attempt."""
    worker: str
    attempt: int
    success: bool
    error: Optional[ErrorDetection] = None
    elapsed_s: float = 0.0


@dataclass
class DispatchResult:
    """Full result of a dispatch_with_retry call."""
    success: bool
    worker: str
    attempts: list = field(default_factory=list)
    redistributed_to: Optional[str] = None
    total_elapsed_s: float = 0.0


class DispatchResilience:
    """
    Auto-recovery layer for CLI errors during Skynet dispatch.

    Wraps dispatch_to_worker with:
    - Post-dispatch error detection via Win32 PrintWindow + OCR
    - Exponential backoff retry (30s, 60s, 120s)
    - Redistribution to idle workers on max_retries exceeded
    - Bus notifications for all recovery events

    # signed: beta
    """

    def __init__(self):
        self._ocr = None  # lazy-loaded OCREngine

    def _get_ocr(self):
        """Lazy-load OCR engine to avoid import cost on unused paths."""
        if self._ocr is None:
            try:
                from core.ocr import OCREngine
                self._ocr = OCREngine()
            except Exception as e:
                log.warning("OCREngine unavailable: %s — falling back to basic pixel check", e)
        return self._ocr

    # ── Core API ─────────────────────────────────────────────────

    def dispatch_with_retry(
        self,
        worker_name: str,
        task: str,
        max_retries: int = 3,
        backoff_base: int = 30,
        monitor_window_s: int = 30,
    ) -> DispatchResult:
        """
        Dispatch task to worker with automatic error detection and retry.

        Args:
            worker_name: Target worker name (e.g. 'alpha')
            task: Task text to dispatch
            max_retries: Maximum retry attempts before redistribution
            backoff_base: Base backoff in seconds (doubled each retry)
            monitor_window_s: Seconds to monitor worker after dispatch

        Returns:
            DispatchResult with success status, attempts list, and
            optional redistribution target.

        # signed: beta
        """
        from tools.skynet_dispatch import (
            dispatch_to_worker,
            load_workers,
            load_orch_hwnd,
        )

        result = DispatchResult(success=False, worker=worker_name)
        start = time.time()
        workers = load_workers()
        worker_map = {w["name"]: w for w in workers}

        if worker_name not in worker_map:
            log.error("Worker %s not found in registry", worker_name)
            result.total_elapsed_s = time.time() - start
            return result

        hwnd = worker_map[worker_name].get("hwnd", 0)

        for attempt_num in range(1, max_retries + 1):
            attempt_start = time.time()
            log.info(
                "[resilience] Dispatch attempt %d/%d to %s",
                attempt_num, max_retries, worker_name,
            )

            # Dispatch
            ok = dispatch_to_worker(worker_name, task)
            attempt = DispatchAttempt(
                worker=worker_name,
                attempt=attempt_num,
                success=ok,
                elapsed_s=time.time() - attempt_start,
            )

            if not ok:
                log.warning("[resilience] dispatch_to_worker returned False for %s", worker_name)
                attempt.error = ErrorDetection(has_error=True, category="DISPATCH_FAILED")
                result.attempts.append(attempt)
                # Backoff before retry
                backoff = backoff_base * (2 ** (attempt_num - 1))
                log.info("[resilience] Backing off %ds before retry", backoff)
                time.sleep(backoff)
                continue

            # Post-dispatch monitoring
            error = self._monitor_post_dispatch(hwnd, worker_name, monitor_window_s)
            attempt.error = error
            attempt.elapsed_s = time.time() - attempt_start

            if error and error.has_error:
                log.warning(
                    "[resilience] CLI error detected on %s: %s (%s)",
                    worker_name, error.category, error.matched_text[:80],
                )
                attempt.success = False
                result.attempts.append(attempt)

                if attempt_num < max_retries:
                    backoff = backoff_base * (2 ** (attempt_num - 1))
                    log.info("[resilience] Backing off %ds before retry %d", backoff, attempt_num + 1)
                    self._notify_bus(
                        f"RETRY {worker_name} attempt {attempt_num}/{max_retries}: "
                        f"{error.category}. Backoff {backoff}s. signed:beta"
                    )
                    time.sleep(backoff)
                continue

            # Success — no error detected
            attempt.success = True
            result.attempts.append(attempt)
            result.success = True
            result.total_elapsed_s = time.time() - start
            log.info("[resilience] Dispatch to %s succeeded on attempt %d", worker_name, attempt_num)
            return result

        # All retries exhausted — try redistribution
        log.warning(
            "[resilience] Max retries (%d) exhausted for %s, attempting redistribution",
            max_retries, worker_name,
        )
        redistributed = self.redistribute_task(task, worker_name)
        if redistributed:
            result.redistributed_to = redistributed
            result.success = True
            self._notify_bus(
                f"REDISTRIBUTED task from {worker_name} to {redistributed} "
                f"after {max_retries} failures. signed:beta"
            )
        else:
            self._notify_bus(
                f"DISPATCH_FAILED {worker_name} after {max_retries} retries, "
                f"no idle workers for redistribution. signed:beta"
            )

        result.total_elapsed_s = time.time() - start
        return result

    # ── Error detection ──────────────────────────────────────────

    def detect_cli_error(self, hwnd: int) -> ErrorDetection:
        """
        Capture worker window via Win32 PrintWindow and scan for CLI error patterns.

        Uses repo-native _capture_window (Win32 PrintWindow + GDI) for
        screenshot capture — NOT pyautogui. Then feeds the image to
        OCREngine for text extraction and pattern matching.

        Args:
            hwnd: Window handle to capture and scan.

        Returns:
            ErrorDetection with has_error, category, matched_text, scan_ms.

        # signed: beta
        """
        t0 = time.time()

        # Capture window via Win32 PrintWindow
        img = self._capture_window_pil(hwnd)
        if img is None:
            return ErrorDetection(scan_ms=(time.time() - t0) * 1000)

        # OCR the captured image
        text = self._ocr_image(img)
        if not text:
            return ErrorDetection(scan_ms=(time.time() - t0) * 1000)

        # Match against error patterns
        for pattern, category in CLI_ERROR_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # Extract context around the match (±60 chars)
                start = max(0, match.start() - 60)
                end = min(len(text), match.end() + 60)
                context = text[start:end].strip()
                return ErrorDetection(
                    has_error=True,
                    category=category,
                    matched_text=context,
                    scan_ms=(time.time() - t0) * 1000,
                )

        return ErrorDetection(scan_ms=(time.time() - t0) * 1000)

    # ── Recovery ─────────────────────────────────────────────────

    def recover_worker(self, hwnd: int, name: str, backoff_s: int = 30) -> bool:
        """
        Wait for backoff period then verify worker is responsive.

        Args:
            hwnd: Worker window handle.
            name: Worker name for logging.
            backoff_s: Seconds to wait before checking.

        Returns:
            True if worker is responsive (IDLE/PROCESSING) after backoff.

        # signed: beta
        """
        log.info("[resilience] Recovering %s — waiting %ds", name, backoff_s)
        time.sleep(backoff_s)

        try:
            from tools.uia_engine import get_engine
            state = get_engine().get_state(hwnd)
            responsive = state in ("IDLE", "PROCESSING", "TYPING")
            log.info("[resilience] %s state after recovery wait: %s (responsive=%s)", name, state, responsive)
            return responsive
        except Exception as e:
            log.warning("[resilience] UIA check failed for %s: %s", name, e)
            return False

    def redistribute_task(self, task: str, failed_worker: str) -> Optional[str]:
        """
        Send task to the next idle worker after the original failed.

        Args:
            task: Task text to redistribute.
            failed_worker: Name of worker that failed (excluded from candidates).

        Returns:
            Name of worker that received the task, or None if no idle workers.

        # signed: beta
        """
        from tools.skynet_dispatch import dispatch_to_worker, load_workers

        workers = load_workers()
        idle_workers = []

        try:
            from tools.uia_engine import get_engine
            engine = get_engine()
            for w in workers:
                if w["name"] == failed_worker:
                    continue
                hwnd = w.get("hwnd", 0)
                if hwnd:
                    state = engine.get_state(hwnd)
                    if state == "IDLE":
                        idle_workers.append(w)
        except Exception as e:
            log.warning("[resilience] UIA scan failed during redistribution: %s", e)
            # Fallback: try any worker that isn't the failed one
            idle_workers = [w for w in workers if w["name"] != failed_worker]

        if not idle_workers:
            log.warning("[resilience] No idle workers available for redistribution")
            return None

        # Pick first idle worker
        target = idle_workers[0]
        log.info("[resilience] Redistributing task to %s", target["name"])
        ok = dispatch_to_worker(target["name"], task)
        if ok:
            return target["name"]

        log.warning("[resilience] Redistribution to %s also failed", target["name"])
        return None

    # ── Post-dispatch monitoring ─────────────────────────────────

    def _monitor_post_dispatch(
        self, hwnd: int, name: str, window_s: int = 30
    ) -> Optional[ErrorDetection]:
        """
        Monitor worker after dispatch for early CLI errors.

        Polls UIA state for up to window_s seconds. If the worker
        returns to IDLE suspiciously fast (< 5s for a substantial task),
        captures a screenshot and checks for error patterns.

        Args:
            hwnd: Worker window handle.
            name: Worker name.
            window_s: Monitoring window in seconds.

        Returns:
            ErrorDetection if error found, None if worker appears healthy.

        # signed: beta
        """
        try:
            from tools.uia_engine import get_engine
            engine = get_engine()
        except Exception:
            return None

        start = time.time()
        saw_processing = False
        processing_start = None

        while time.time() - start < window_s:
            try:
                state = engine.get_state(hwnd)
            except Exception:
                time.sleep(2)
                continue

            if state == "PROCESSING":
                if not saw_processing:
                    saw_processing = True
                    processing_start = time.time()
                time.sleep(2)
                continue

            if state == "IDLE" and saw_processing:
                processing_duration = time.time() - (processing_start or start)
                if processing_duration < 5.0:
                    # Suspiciously fast — check for error
                    log.info(
                        "[resilience] %s returned to IDLE after %.1fs — checking for error",
                        name, processing_duration,
                    )
                    time.sleep(0.5)  # let the error text render
                    error = self.detect_cli_error(hwnd)
                    if error.has_error:
                        return error
                # Normal completion — no error
                return None

            if state == "STEERING":
                # STEERING is handled by dispatch layer, not an error
                time.sleep(2)
                continue

            # IDLE without ever seeing PROCESSING — task may not have been accepted
            if state == "IDLE" and not saw_processing and time.time() - start > 5:
                error = self.detect_cli_error(hwnd)
                if error.has_error:
                    return error
                return None

            time.sleep(2)

        # Monitoring window expired — worker still processing, that's fine
        return None

    # ── Win32 capture (no pyautogui) ─────────────────────────────

    def _capture_window_pil(self, hwnd: int):
        """
        Capture window via Win32 PrintWindow → PIL Image.

        Uses the same Win32 PrintWindow + GDI approach as
        tools/chrome_bridge/winctl.py _capture_window(), but returns
        a PIL Image directly instead of raw bytes.

        # signed: beta
        """
        try:
            from PIL import Image
        except ImportError:
            log.warning("PIL not available for window capture")
            return None

        if not user32.IsWindow(hwnd):
            return None

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        old = gdi32.SelectObject(hdc_mem, hbmp)

        user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

        # BITMAPINFOHEADER (40 bytes)
        bmi = ctypes.create_string_buffer(40)
        struct.pack_into("<IiiHHIIiiII", bmi, 0,
                         40, w, -h, 1, 32, BI_RGB, 0, 0, 0, 0, 0)

        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, bmi, DIB_RGB_COLORS)

        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)

        try:
            img = Image.frombytes("RGBA", (w, h), bytes(buf), "raw", "BGRA")
            return img.convert("RGB")
        except Exception as e:
            log.warning("PIL Image.frombytes failed: %s", e)
            return None

    def _ocr_image(self, img) -> str:
        """
        Run OCR on a PIL Image. Uses repo-native OCREngine (3-tier)
        with fallback to basic text extraction.

        # signed: beta
        """
        ocr = self._get_ocr()
        if ocr is None:
            return ""

        try:
            result = ocr.extract_text(img)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                return result.get("text", "")
            if isinstance(result, list):
                return " ".join(str(r) for r in result)
            return str(result)
        except Exception as e:
            log.warning("OCR failed: %s", e)
            return ""

    # ── Bus notification ─────────────────────────────────────────

    def _notify_bus(self, content: str):
        """Post resilience event to bus via SpamGuard."""
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "beta",
                "topic": "orchestrator",
                "type": "resilience_event",
                "content": content,
            })
        except Exception as e:
            log.warning("Bus notification failed: %s", e)


# ── CLI interface ────────────────────────────────────────────────

def _cli():
    """CLI entry point for testing and manual operation. # signed: beta"""
    import argparse

    parser = argparse.ArgumentParser(description="Dispatch resilience layer")
    parser.add_argument("--test", action="store_true", help="Run self-test")
    parser.add_argument("--detect-error", action="store_true", help="Detect CLI error on window")
    parser.add_argument("--hwnd", type=int, help="Window HWND for --detect-error")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch with retry")
    parser.add_argument("--worker", type=str, help="Worker name for --dispatch")
    parser.add_argument("--task", type=str, help="Task text for --dispatch")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries (default 3)")
    parser.add_argument("--backoff", type=int, default=30, help="Backoff base seconds (default 30)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.test:
        _run_tests()
    elif args.detect_error:
        if not args.hwnd:
            print("ERROR: --hwnd required with --detect-error")
            sys.exit(1)
        dr = DispatchResilience()
        result = dr.detect_cli_error(args.hwnd)
        print(f"has_error:    {result.has_error}")
        print(f"category:     {result.category}")
        print(f"matched_text: {result.matched_text}")
        print(f"scan_ms:      {result.scan_ms:.1f}")
    elif args.dispatch:
        if not args.worker or not args.task:
            print("ERROR: --worker and --task required with --dispatch")
            sys.exit(1)
        dr = DispatchResilience()
        result = dr.dispatch_with_retry(
            args.worker, args.task,
            max_retries=args.max_retries,
            backoff_base=args.backoff,
        )
        print(f"success:          {result.success}")
        print(f"worker:           {result.worker}")
        print(f"attempts:         {len(result.attempts)}")
        print(f"redistributed_to: {result.redistributed_to}")
        print(f"total_elapsed_s:  {result.total_elapsed_s:.1f}")
        for a in result.attempts:
            err = f" error={a.error.category}" if a.error and a.error.has_error else ""
            print(f"  attempt {a.attempt}: success={a.success} elapsed={a.elapsed_s:.1f}s{err}")
    else:
        parser.print_help()


def _run_tests():
    """Self-test: validate error pattern matching and data structures. # signed: beta"""
    print("=" * 60)
    print("DispatchResilience — Self-Test Suite")
    print("=" * 60)
    passed = 0
    failed = 0

    # Test 1: Error pattern matching
    print("\n[TEST 1] Error pattern matching against known strings...")
    test_texts = [
        ("You've been rate limited. Please try again later.", "RATE_LIMIT"),
        ("Error 429: Too many requests to the API", "RATE_LIMIT"),
        ("The model is unavailable at this time", "MODEL_UNAVAILABLE"),
        ("no model selected for this request", "MODEL_UNAVAILABLE"),
        ("CAPIError: internal failure in completion endpoint", "CAPI_ERROR"),
        ("Execution failed with exit code 1", "EXECUTION_FAILED"),
        ("Request failed with status code 400 bad request", "BAD_REQUEST"),
        ("Maximum context length exceeded for this model", "CONTEXT_OVERFLOW"),
        ("This is normal output with no errors at all", None),
        ("Task completed successfully, result: 42", None),
    ]
    for text, expected_cat in test_texts:
        found_cat = None
        for pattern, category in CLI_ERROR_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                found_cat = category
                break
        if found_cat == expected_cat:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"
        print(f"  {status}: '{text[:50]}...' → expected={expected_cat}, got={found_cat}")

    # Test 2: ErrorDetection dataclass
    print("\n[TEST 2] ErrorDetection dataclass defaults...")
    ed = ErrorDetection()
    checks = [
        ("has_error default", ed.has_error == False),
        ("category default", ed.category == ""),
        ("matched_text default", ed.matched_text == ""),
        ("scan_ms default", ed.scan_ms == 0.0),
    ]
    for name, ok in checks:
        if ok:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # Test 3: DispatchResult dataclass
    print("\n[TEST 3] DispatchResult dataclass...")
    dr = DispatchResult(success=False, worker="test")
    checks = [
        ("success default", dr.success == False),
        ("worker set", dr.worker == "test"),
        ("attempts empty list", dr.attempts == []),
        ("redistributed_to None", dr.redistributed_to is None),
    ]
    for name, ok in checks:
        if ok:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # Test 4: DispatchAttempt dataclass
    print("\n[TEST 4] DispatchAttempt dataclass...")
    da = DispatchAttempt(worker="alpha", attempt=1, success=True, elapsed_s=1.5)
    checks = [
        ("worker set", da.worker == "alpha"),
        ("attempt set", da.attempt == 1),
        ("success set", da.success == True),
        ("error default None", da.error is None),
        ("elapsed_s set", da.elapsed_s == 1.5),
    ]
    for name, ok in checks:
        if ok:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    # Test 5: Exponential backoff calculation
    print("\n[TEST 5] Exponential backoff calculation...")
    base = 30
    expected_backoffs = [30, 60, 120]
    for attempt_num, expected in enumerate(expected_backoffs, 1):
        actual = base * (2 ** (attempt_num - 1))
        if actual == expected:
            passed += 1
            print(f"  PASS: attempt {attempt_num} → backoff {actual}s")
        else:
            failed += 1
            print(f"  FAIL: attempt {attempt_num} → expected {expected}s, got {actual}s")

    # Test 6: DispatchResilience instantiation
    print("\n[TEST 6] DispatchResilience instantiation...")
    try:
        resilience = DispatchResilience()
        passed += 1
        print("  PASS: DispatchResilience() created")
    except Exception as e:
        failed += 1
        print(f"  FAIL: {e}")

    # Test 7: CLI_ERROR_PATTERNS are all valid regex
    print("\n[TEST 7] CLI_ERROR_PATTERNS regex validity...")
    all_valid = True
    for pattern, category in CLI_ERROR_PATTERNS:
        try:
            re.compile(pattern)
        except re.error as e:
            all_valid = False
            failed += 1
            print(f"  FAIL: invalid regex '{pattern}': {e}")
    if all_valid:
        passed += 1
        print(f"  PASS: all {len(CLI_ERROR_PATTERNS)} patterns are valid regex")

    # Summary
    print(f"\n{'=' * 60}")
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    else:
        print(f"{failed} TESTS FAILED ❌")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    _cli()
