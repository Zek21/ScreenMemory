#!/usr/bin/env python3
"""
Skynet Pipeline — Composable task execution with chaining, parallelism, and retry.

Provides a SkynetPipeline class for building multi-step workflows that can
chain sequential steps (output → input), run parallel branches, and retry
on failure with exponential backoff.

Usage:
    from tools.skynet_pipeline import SkynetPipeline

    pipe = SkynetPipeline()

    # Sequential chain: each step receives previous output
    result = pipe.chain([
        lambda _: scan_workers(),
        lambda states: pick_idle(states),
        lambda worker: dispatch(worker, "do X"),
    ])

    # Parallel: run tasks simultaneously, merge results
    results = pipe.parallel({
        "audit": lambda: audit_endpoints(),
        "scan":  lambda: scan_stubs(),
        "bench": lambda: run_benchmarks(),
    })

    # Retry a flaky operation
    result = pipe.retry(lambda: fragile_api_call(), max_retries=3)
"""

import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import urlopen, Request

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SKYNET_URL = "http://localhost:8420"


def log(msg, level="SYS"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"OK": "\u2705", "ERR": "\u274c", "WARN": "\u26a0\ufe0f", "SYS": "\u2699\ufe0f"}.get(level, "\u2699\ufe0f")
    print(f"[{ts}] {prefix} {msg}", flush=True)


def bus_post(sender, content):
    """Post result to Skynet bus via SpamGuard."""
    msg = {"sender": sender, "topic": "orchestrator", "type": "result", "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(msg)
    except ImportError:
        try:
            body = json.dumps(msg).encode()
            req = Request(f"{SKYNET_URL}/bus/publish", data=body, headers={"Content-Type": "application/json"})
            urlopen(req, timeout=5)
        except Exception:
            pass
    # signed: gamma


class StepResult:
    """Result of a single pipeline step."""
    __slots__ = ("name", "success", "output", "error", "duration_ms")

    def __init__(self, name, success, output=None, error=None, duration_ms=0):
        self.name = name
        self.success = success
        self.output = output
        self.error = error
        self.duration_ms = duration_ms

    def __repr__(self):
        status = "\u2705" if self.success else "\u274c"
        return f"<Step {self.name} {status} {self.duration_ms:.0f}ms>"


class SkynetPipeline:
    """Composable task execution pipeline with chain, parallel, and retry."""

    def __init__(self, name="pipeline"):
        self.name = name
        self.history: List[StepResult] = []

    def chain(self, steps: List[Callable], initial_input=None, step_names=None) -> Any:
        """Run steps sequentially, passing output of each to the next.

        Args:
            steps: List of callables. Each receives the output of the previous step.
            initial_input: Input to the first step.
            step_names: Optional list of names for logging.

        Returns:
            Output of the last step.

        Raises:
            Exception from the first failing step (pipeline stops on failure).
        """
        current = initial_input
        for i, step in enumerate(steps):
            name = step_names[i] if step_names and i < len(step_names) else f"step_{i}"
            t0 = time.perf_counter()
            try:
                current = step(current)
                ms = (time.perf_counter() - t0) * 1000
                result = StepResult(name, True, current, duration_ms=ms)
                self.history.append(result)
                log(f"chain [{i+1}/{len(steps)}] {name}: OK ({ms:.0f}ms)", "OK")
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                result = StepResult(name, False, error=str(e), duration_ms=ms)
                self.history.append(result)
                log(f"chain [{i+1}/{len(steps)}] {name}: FAILED — {e}", "ERR")
                raise
        return current

    def parallel(self, tasks: Dict[str, Callable], max_workers=8, timeout=120) -> Dict[str, Any]:
        """Run all tasks simultaneously and merge results.

        Args:
            tasks: Dict of name → callable (no args).
            max_workers: Thread pool size.
            timeout: Max seconds to wait for all tasks.

        Returns:
            Dict of name → output (or Exception if failed).
        """
        results = {}
        t0 = time.perf_counter()
        n = min(max_workers, len(tasks))
        log(f"parallel: launching {len(tasks)} tasks ({n} threads)", "SYS")

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futures, timeout=timeout):
                name = futures[fut]
                step_t0 = time.perf_counter()
                try:
                    output = fut.result()
                    ms = (time.perf_counter() - t0) * 1000
                    results[name] = output
                    self.history.append(StepResult(name, True, output, duration_ms=ms))
                    log(f"parallel {name}: OK", "OK")
                except Exception as e:
                    ms = (time.perf_counter() - t0) * 1000
                    results[name] = e
                    self.history.append(StepResult(name, False, error=str(e), duration_ms=ms))
                    log(f"parallel {name}: FAILED — {e}", "ERR")

        total_ms = (time.perf_counter() - t0) * 1000
        ok = sum(1 for v in results.values() if not isinstance(v, Exception))
        log(f"parallel complete: {ok}/{len(tasks)} succeeded ({total_ms:.0f}ms)", "OK" if ok == len(tasks) else "WARN")
        return results

    def retry(self, fn: Callable, max_retries=3, backoff_base=1.0, backoff_max=10.0) -> Any:
        """Retry a callable on failure with exponential backoff.

        Args:
            fn: Callable (no args) to execute.
            max_retries: Maximum number of retry attempts after first failure.
            backoff_base: Initial backoff delay in seconds.
            backoff_max: Maximum backoff delay.

        Returns:
            Output of fn on success.

        Raises:
            Last exception if all retries exhausted.
        """
        last_error = None
        for attempt in range(max_retries + 1):
            t0 = time.perf_counter()
            try:
                result = fn()
                ms = (time.perf_counter() - t0) * 1000
                self.history.append(StepResult(f"retry_attempt_{attempt}", True, result, duration_ms=ms))
                if attempt > 0:
                    log(f"retry: succeeded on attempt {attempt + 1}", "OK")
                return result
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                last_error = e
                self.history.append(StepResult(f"retry_attempt_{attempt}", False, error=str(e), duration_ms=ms))
                if attempt < max_retries:
                    delay = min(backoff_base * (2 ** attempt), backoff_max)
                    log(f"retry: attempt {attempt + 1} failed ({e}), waiting {delay:.1f}s", "WARN")
                    time.sleep(delay)
                else:
                    log(f"retry: all {max_retries + 1} attempts failed", "ERR")
        raise last_error

    def summary(self) -> Dict:
        """Return pipeline execution summary."""
        total_ms = sum(s.duration_ms for s in self.history)
        successes = sum(1 for s in self.history if s.success)
        failures = sum(1 for s in self.history if not s.success)
        return {
            "pipeline": self.name,
            "total_steps": len(self.history),
            "successes": successes,
            "failures": failures,
            "total_ms": round(total_ms, 1),
            "steps": [
                {"name": s.name, "success": s.success, "duration_ms": round(s.duration_ms, 1),
                 "error": s.error}
                for s in self.history
            ],
        }


if __name__ == "__main__":
    import math

    pipe = SkynetPipeline(name="self_test")

    # Test 1: chain
    log("=== Test 1: chain ===", "SYS")
    result = pipe.chain(
        [
            lambda _: list(range(10)),
            lambda nums: [x * 2 for x in nums],
            lambda doubled: sum(doubled),
        ],
        step_names=["generate", "double", "sum"],
    )
    assert result == 90, f"Chain failed: expected 90, got {result}"
    log(f"Chain result: {result}", "OK")

    # Test 2: parallel
    log("=== Test 2: parallel ===", "SYS")
    par_results = pipe.parallel({
        "squares": lambda: [x**2 for x in range(100)],
        "cubes": lambda: [x**3 for x in range(50)],
        "primes": lambda: [x for x in range(2, 200) if all(x % d != 0 for d in range(2, int(math.sqrt(x)) + 1))],
    })
    assert not any(isinstance(v, Exception) for v in par_results.values()), "Parallel had failures"
    log(f"Parallel: squares={len(par_results['squares'])}, cubes={len(par_results['cubes'])}, primes={len(par_results['primes'])}", "OK")

    # Test 3: retry (succeeds on attempt 2)
    log("=== Test 3: retry ===", "SYS")
    call_count = [0]
    def flaky():
        call_count[0] += 1
        if call_count[0] < 2:
            raise ConnectionError("simulated failure")
        return "recovered"

    retry_result = pipe.retry(flaky, max_retries=3, backoff_base=0.1)
    assert retry_result == "recovered", f"Retry failed: {retry_result}"
    log(f"Retry result: {retry_result} (took {call_count[0]} attempts)", "OK")

    # Test 4: retry exhaustion
    log("=== Test 4: retry exhaustion ===", "SYS")
    try:
        pipe.retry(lambda: 1/0, max_retries=2, backoff_base=0.1)
        assert False, "Should have raised"
    except ZeroDivisionError:
        log("Retry correctly raised after exhaustion", "OK")

    # Summary
    s = pipe.summary()
    log(f"Pipeline summary: {s['successes']} ok, {s['failures']} fail, {s['total_ms']:.0f}ms total", "OK")

    # Post to bus
    bus_post("alpha", f"skynet_pipeline.py self-test: {s['successes']} pass, {s['failures']} expected-fail, {s['total_ms']:.0f}ms")
    print(json.dumps(s, indent=2))
