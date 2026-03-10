"""
Structured activity logger for ScreenMemory.

Provides human-readable, timestamped logs of every system action —
designed so a human operator can trace exactly what the system did,
why it did it, and where things went wrong.

Log Format:
    [TIMESTAMP] [LEVEL] [COMPONENT] ACTION — DETAIL
    
Example:
    [06:28:01] [INFO] [CAPTURE] frame_acquired — monitor=0, 1920x1080, 32ms
    [06:28:01] [INFO] [CHANGE]  change_detected — hamming=142, pct=59.2%, regions=[1,3,4,5]
    [06:28:22] [INFO] [VLM]    analysis_complete — model=moondream, 20412ms, app=Chrome, activity=browsing
    [06:28:22] [INFO] [DB]     record_stored — id=47, fts=ok, vec=ok
    [06:28:22] [WARN] [EMBED]  image_embedding_skipped — reason=no_torch, fallback=text_only
"""
import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Any


class ActivityLogger:
    """
    Central logging system that writes both to console and structured log files.
    
    Produces two log streams:
    1. Human-readable console output (colored, concise)
    2. JSON-lines file for programmatic analysis (logs/activity.jsonl)
    """

    COMPONENTS = {
        "CAPTURE", "CHANGE", "VLM", "EMBED", "DB", "SEARCH",
        "PLANNER", "NAVIGATOR", "MEMORY", "GROUNDING", "SYSTEM",
    }

    def __init__(self, log_dir: str = "logs", console: bool = True, file: bool = True):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._console = console
        self._file = file
        self._jsonl_path = self.log_dir / "activity.jsonl"
        self._text_path = self.log_dir / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        # Metrics counters
        self._counters: dict[str, int] = {}
        self._timers: dict[str, list[float]] = {}
        self._errors: list[dict] = []
        self._session_start = time.time()

        # Set up Python logging integration
        self._setup_python_logging()

    def _setup_python_logging(self):
        """Wire into Python's logging module for library logs."""
        root = logging.getLogger("screenmemory")
        root.setLevel(logging.DEBUG)

        if self._console:
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.INFO)
            ch.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
            root.addHandler(ch)

        if self._file:
            fh = logging.FileHandler(str(self._text_path), encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root.addHandler(fh)

    def log(self, component: str, action: str, level: str = "INFO",
            detail: Optional[str] = None, data: Optional[dict] = None):
        """
        Log a structured activity event.
        
        Args:
            component: System component (CAPTURE, VLM, DB, etc.)
            action: What happened (frame_acquired, analysis_complete, etc.)
            level: INFO, WARN, ERROR, DEBUG
            detail: Human-readable detail string
            data: Structured data dict for JSON log
        """
        ts = datetime.now()
        ts_str = ts.strftime("%H:%M:%S")

        # Counter
        key = f"{component}.{action}"
        self._counters[key] = self._counters.get(key, 0) + 1

        # Console output
        if self._console:
            color = {"INFO": "\033[92m", "WARN": "\033[93m", "ERROR": "\033[91m", "DEBUG": "\033[90m"}.get(level, "")
            reset = "\033[0m"
            comp_padded = f"[{component}]".ljust(12)
            msg = f"[{ts_str}] {color}[{level}]{reset} {comp_padded} {action}"
            if detail:
                msg += f" — {detail}"
            print(msg)

        # JSON-lines log
        if self._file:
            entry = {
                "ts": ts.isoformat(),
                "level": level,
                "component": component,
                "action": action,
                "detail": detail,
            }
            if data:
                entry["data"] = data
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        # Track errors
        if level == "ERROR":
            self._errors.append({"ts": ts.isoformat(), "component": component, "action": action, "detail": detail})

    def timer_start(self, name: str) -> float:
        """Start a named timer, returns start time."""
        return time.perf_counter()

    def timer_end(self, name: str, start: float) -> float:
        """End a named timer, returns elapsed ms."""
        elapsed = (time.perf_counter() - start) * 1000
        if name not in self._timers:
            self._timers[name] = []
        self._timers[name].append(elapsed)
        return elapsed

    def get_stats(self) -> dict:
        """Get session statistics."""
        runtime = time.time() - self._session_start
        timer_stats = {}
        for name, times in self._timers.items():
            timer_stats[name] = {
                "count": len(times),
                "avg_ms": sum(times) / len(times) if times else 0,
                "min_ms": min(times) if times else 0,
                "max_ms": max(times) if times else 0,
            }

        return {
            "runtime_seconds": runtime,
            "counters": dict(self._counters),
            "timers": timer_stats,
            "error_count": len(self._errors),
            "recent_errors": self._errors[-5:],
        }

    def print_session_summary(self):
        """Print a formatted session summary."""
        stats = self.get_stats()
        runtime = stats["runtime_seconds"]

        print("\n" + "=" * 60)
        print("  SCREENMEMORY SESSION SUMMARY")
        print("=" * 60)
        print(f"  Runtime: {runtime/60:.1f} minutes ({runtime:.0f}s)")
        print(f"  Errors:  {stats['error_count']}")

        if stats["counters"]:
            print("\n  Activity Counts:")
            for key, count in sorted(stats["counters"].items()):
                print(f"    {key}: {count}")

        if stats["timers"]:
            print("\n  Performance:")
            for name, t in stats["timers"].items():
                print(f"    {name}: avg={t['avg_ms']:.0f}ms, min={t['min_ms']:.0f}ms, max={t['max_ms']:.0f}ms (n={t['count']})")

        if stats["recent_errors"]:
            print("\n  Recent Errors:")
            for err in stats["recent_errors"]:
                print(f"    [{err['ts']}] {err['component']}.{err['action']}: {err['detail']}")

        print("=" * 60 + "\n")


# Global logger instance
_logger: Optional[ActivityLogger] = None


def get_logger() -> ActivityLogger:
    """Get or create the global activity logger."""
    global _logger
    if _logger is None:
        _logger = ActivityLogger()
    return _logger
