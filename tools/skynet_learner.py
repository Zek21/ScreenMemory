#!/usr/bin/env python3
"""
skynet_learner.py -- Learning Feedback Loop Daemon for Skynet.

Closes the intelligence gap: polls the bus for completed task results,
extracts learnings, stores facts in LearningStore, updates evolution
fitness, and broadcasts knowledge to peers.

Without this daemon, workers complete tasks and the results evaporate.
With it, every task makes the system smarter.

Usage:
    python tools/skynet_learner.py --daemon          # Run as daemon (loop every 30s)
    python tools/skynet_learner.py --once             # Single pass
    python tools/skynet_learner.py --stats            # Show learner stats
    python tools/skynet_learner.py --extract "text"   # Extract learnings from text
"""

import argparse
import hashlib
import json
import logging
import re
import signal
import sys
import time
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# ─── Logging ───────────────────────────────────────────

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("skynet_learner")
logger.setLevel(logging.INFO)

_file_handler = RotatingFileHandler(
    LOG_DIR / "skynet_learner.log", maxBytes=2 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("[LEARNER] %(message)s"))
logger.addHandler(_console_handler)

# ─── Constants ─────────────────────────────────────────

BUS_URL = "http://localhost:8420"
STATE_FILE = ROOT / "data" / "learner_state.json"
DISPATCH_LOG = ROOT / "data" / "dispatch_log.json"
BRAIN_CONFIG = ROOT / "data" / "brain_config.json"
PID_FILE = ROOT / "data" / "learner.pid"

DEFAULT_LOOP_INTERVAL = 30
HEALTH_REPORT_INTERVAL = 600  # 10 minutes (was 5min — root cause fix to reduce bus noise)
# Root cause fix (2026-03-23): 973 daemon_health messages in archive flooding the
# 100-message ring buffer. With 4 workers + learner + monitor all posting health,
# real results get evicted before the orchestrator can read them.  signed: orchestrator
_bus_fetch_failures = 0  # backoff counter for bus connectivity  # signed: delta

# Domain keyword mappings for task categorization
DOMAIN_KEYWORDS = {
    "infrastructure": [
        "daemon", "watchdog", "monitor", "overseer", "pid", "singleton",
        "process", "service", "backend", "server", "port", "health",
        "sse", "websocket", "stuck", "detector", "self-prompt",
    ],
    "browser": [
        "chrome", "cdp", "god_mode", "playwright", "browser", "dom",
        "accessibility", "tab", "navigate", "click", "god mode",
    ],
    "dashboard": [
        "dashboard", "god_console", "html", "css", "ui", "panel",
        "widget", "chart", "visualization", "frontend", "sse_stream",
    ],
    "dispatch": [
        "dispatch", "routing", "task", "worker", "assign", "queue",
        "decompose", "brain", "orchestrate", "pipeline",
    ],
    "security": [
        "credential", "password", "secret", "auth", "guard", "security",
        "injection", "sanitize", "input_guard", "ceasefire",
    ],
    "perception": [
        "capture", "screenshot", "ocr", "grounding", "vision",
        "perception", "dxgi", "screen", "window", "uia",
    ],
    "email": [
        "email", "ses", "smtp", "send_email", "verify", "domain",
    ],
    "prospecting": [
        "prospect", "lead", "dns", "whois", "scrape", "pipeline",
    ],
    "code": [
        "refactor", "fix", "bug", "test", "lint", "build", "compile",
        "syntax", "import", "module", "function", "class",
    ],
}

# Success/failure signal words
SUCCESS_SIGNALS = [
    "fixed", "completed", "created", "refactored", "upgraded",
    "validated", "verified", "passed", "working", "done", "ok",
    "success", "applied", "implemented", "built", "added",
]
FAILURE_SIGNALS = [
    "failed", "error", "broken", "crash", "exception", "timeout",
    "stuck", "blocked", "rejected", "denied", "missing", "bug",
]


# ─── Bus Helpers ───────────────────────────────────────

def bus_post(sender: str, topic: str, msg_type: str, content: str) -> bool:
    """POST a message to the Skynet bus via SpamGuard (MANDATORY).
    Raw fallback removed to prevent SpamGuard bypass -- all bus posts
    must go through guarded_publish() per Anti-Spam Rule 0.4.
    """  # signed: alpha
    payload = {
        "sender": sender, "topic": topic,
        "type": msg_type, "content": content,
    }
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(payload)
        return True
    except ImportError:
        logger.warning("SpamGuard unavailable -- bus post suppressed to prevent bypass")
        return False
    except Exception as e:
        logger.warning(f"SpamGuard rejected bus post: {e}")
        return False


def bus_get(topic: Optional[str] = None, limit: int = 200) -> List[dict]:
    """GET messages from the Skynet bus."""
    global _bus_fetch_failures
    try:
        url = f"{BUS_URL}/bus/messages?limit={limit}"
        if topic:
            url += f"&topic={topic}"
        with urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            _bus_fetch_failures = 0  # reset on success  # signed: delta
            return data if isinstance(data, list) else []
    except (ConnectionError, TimeoutError, URLError, OSError):
        _bus_fetch_failures += 1
        if _bus_fetch_failures <= 3 or _bus_fetch_failures % 10 == 0:
            logger.warning(f"Bus fetch failed (attempt {_bus_fetch_failures})")  # signed: gamma
        return []
    except (json.JSONDecodeError, ValueError):
        _bus_fetch_failures += 1
        logger.warning(f"Bus fetch parse error (attempt {_bus_fetch_failures})")  # signed: gamma
        return []


# ─── State Management ──────────────────────────────────

def _load_state() -> dict:
    """Load learner state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "seen_ids": [],
        "total_processed": 0,
        "total_learnings": 0,
        "total_evolution_updates": 0,
        "total_broadcasts": 0,
        "last_run": None,
        "started_at": datetime.now().isoformat(),
    }


def _save_state(state: dict):
    """Persist learner state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep seen_ids bounded (last 500)
    if len(state.get("seen_ids", [])) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _load_dispatch_log() -> List[dict]:
    """Load dispatch log for task correlation."""
    if DISPATCH_LOG.exists():
        try:
            return json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _load_config() -> dict:
    """Load learner config from brain_config.json."""
    if BRAIN_CONFIG.exists():
        try:
            cfg = json.loads(BRAIN_CONFIG.read_text(encoding="utf-8"))
            return cfg.get("learner", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ─── Task Categorization ──────────────────────────────

def categorize_task(text: str) -> Tuple[str, List[str]]:
    """Infer domain category and tags from task text.

    Returns:
        (primary_category, [tags])
    """
    text_lower = text.lower()
    scores = {}
    matched_tags = []

    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in text_lower:
                score += 1
                if kw not in matched_tags:
                    matched_tags.append(kw)
        if score > 0:
            scores[domain] = score

    if not scores:
        return "general", matched_tags

    primary = max(scores, key=scores.get)
    # Add secondary domains as tags
    for domain, score in sorted(scores.items(), key=lambda x: -x[1]):
        if domain not in matched_tags:
            matched_tags.append(domain)

    return primary, matched_tags[:8]


def detect_success(content: str) -> bool:
    """Heuristic: did the task succeed?"""
    content_lower = content.lower()
    success_count = sum(1 for w in SUCCESS_SIGNALS if w in content_lower)
    failure_count = sum(1 for w in FAILURE_SIGNALS if w in content_lower)
    return success_count >= failure_count


def extract_insights(task_text: str, result_text: str, success: bool) -> List[str]:
    """Extract learnable insights from task + result.

    Generates concise factual statements that can be stored in LearningStore.
    """
    insights = []

    # Core insight: what was done and outcome
    task_summary = task_text[:150].replace("\n", " ").strip()
    if success:
        insights.append(f"Successfully completed: {task_summary}")
    else:
        insights.append(f"Failed task: {task_summary}")

    # Extract specific patterns from result text
    result_lower = result_text.lower()

    # File-level learnings
    file_patterns = re.findall(r'(?:tools|core|data|Skynet)/[\w/_.]+\.(?:py|go|json|html)', result_text)
    if file_patterns:
        files = list(set(file_patterns))[:5]
        insights.append(f"Files involved: {', '.join(files)}")

    # Bug fix learnings
    if "fix" in result_lower or "bug" in result_lower:
        # Try to extract what was fixed
        fix_match = re.search(r'(?:fixed|fix applied|root cause)[:\s]+(.{20,100})', result_text, re.IGNORECASE)
        if fix_match:
            insights.append(f"Fix pattern: {fix_match.group(1).strip()}")

    # Refactoring learnings
    if "refactor" in result_lower:
        insights.append(f"Refactoring applied in task: {task_summary}")

    # Threshold/config learnings
    config_match = re.findall(r'(\w+)\s*[=:]\s*(\d+)', result_text)
    for name, val in config_match[:3]:
        if len(name) > 3 and name not in ("http", "port", "localhost"):
            insights.append(f"Configuration: {name} = {val}")

    # Credential/security learnings
    if "credential" in result_lower or "security" in result_lower:
        insights.append("Security pattern: centralized credential management used")

    return insights[:6]  # Cap at 6 insights per task


def _msg_fingerprint(msg: dict) -> str:
    """Generate a stable fingerprint for deduplication."""
    # Use message id if available, else hash content
    msg_id = msg.get("id", "")
    if msg_id:
        return str(msg_id)
    raw = f"{msg.get('sender', '')}:{msg.get('content', '')[:200]}:{msg.get('timestamp', '')}"
    return hashlib.md5(raw.encode()).hexdigest()


# ─── Core Learning Pipeline ───────────────────────────

class SkynetLearner:
    """Daemon that extracts learnings from task results and feeds them back."""

    def __init__(self):
        self.state = _load_state()
        self.seen_ids = set(self.state.get("seen_ids", []))
        self._learning_system = None
        self._evolution_system = None
        self._last_health_report = 0
        self._cycle_count = 0
        self._start_time = time.time()
        self._last_episode_time = time.time()
        self._shutting_down = False
        self._stale_alerted = False

    @property
    def learning_system(self):
        if self._learning_system is None:
            try:
                from core.learning_store import PersistentLearningSystem
                self._learning_system = PersistentLearningSystem()
                logger.info("PersistentLearningSystem connected")
            except Exception as e:
                logger.warning(f"Could not init PersistentLearningSystem: {e}")
        return self._learning_system

    @property
    def evolution_system(self):
        if self._evolution_system is None:
            try:
                from core.self_evolution import SelfEvolutionSystem
                self._evolution_system = SelfEvolutionSystem()
                logger.info("SelfEvolutionSystem connected")
            except Exception as e:
                logger.warning(f"Could not init SelfEvolutionSystem: {e}")
        return self._evolution_system

    def _correlate_with_dispatch(self, worker: str, result_content: str) -> Optional[dict]:
        """Find the dispatch log entry that matches this result."""
        dispatch_log = _load_dispatch_log()
        # Walk backwards to find most recent dispatch to this worker
        for entry in reversed(dispatch_log):
            if entry.get("worker") == worker:
                return entry
        return None

    def _store_insights(self, result: dict, task_text: str, category: str,
                        success: bool, insights: list, sender: str):
        """Store insights in LearningStore via PersistentLearningSystem."""
        if not (self.learning_system and insights):
            return
        try:
            fact_ids = self.learning_system.learn_from_task(
                task_description=task_text[:300],
                category=category, success=success, insights=insights,
            )
            result["stored"] = len(fact_ids)
            self.state["total_learnings"] = self.state.get("total_learnings", 0) + len(fact_ids)
            logger.info(f"Stored {len(fact_ids)} facts from {sender}'s result (category={category})")
        except Exception as e:
            logger.warning(f"LearningStore error: {e}")

    def _update_evolution(self, result: dict, dispatch: dict | None,
                          dispatch_time: str, category: str, success: bool):
        """Update evolution fitness from task result."""
        if not self.evolution_system:
            return
        try:
            latency_ms = 0
            if dispatch_time:
                try:
                    dt = datetime.fromisoformat(dispatch_time)
                    latency_ms = int((datetime.now() - dt).total_seconds() * 1000)
                except (ValueError, TypeError):
                    pass
            strategy_id = (dispatch.get("strategy_id") or dispatch.get("strategy", f"default_{category}")
                           if dispatch else f"default_{category}")
            task_result = {
                "task_id": str(uuid.uuid4()), "category": category,
                "strategy_id": strategy_id, "success": success,
                "latency_ms": min(latency_ms, 3600000), "quality_score": 0.8 if success else 0.3,
                "tokens_used": 0, "memory_hits": 0, "memory_queries": 1,
            }
            fitness = self.evolution_system.record_task(task_result)
            result["evolution_updated"] = True
            result["fitness"] = fitness
            self.state["total_evolution_updates"] = self.state.get("total_evolution_updates", 0) + 1
            logger.info(f"Evolution fitness updated: {fitness:.3f} (category={category})")
        except Exception as e:
            logger.warning(f"SelfEvolution error: {e}")

    def _broadcast_top_insight(self, result: dict, insights: list,
                               category: str, tags: list):
        """Broadcast top insight to knowledge bus."""
        if not insights:
            return
        try:
            from tools.skynet_knowledge import broadcast_learning
            ok = broadcast_learning(sender="learner", fact=insights[0],
                                    category=category, tags=tags)
            if ok:
                result["broadcast"] = 1
                self.state["total_broadcasts"] = self.state.get("total_broadcasts", 0) + 1
        except Exception as e:
            logger.warning(f"Broadcast error: {e}")

    def _run_distill_hook(self, result: dict, sender: str, task_text: str,
                          content: str, success: bool):
        """Run KnowledgeDistiller auto-pattern extraction."""
        try:
            from tools.skynet_distill_hook import distill_result
            dr = distill_result(worker=sender, task_text=task_text[:300],
                                result_text=content[:500], success=success)
            result["distill_patterns"] = dr.get("patterns_extracted", 0)
            result["distill_semantic"] = dr.get("semantic_promoted", 0)
            if dr.get("patterns_extracted", 0) > 0:
                logger.info(f"Distilled {dr['patterns_extracted']} patterns, "
                            f"{dr['semantic_promoted']} promoted to semantic")
        except Exception as e:
            logger.warning(f"Distill hook error: {e}")

    def process_result(self, msg: dict) -> dict:
        """Process a single bus result message into learnings.

        Returns:
            Dict with: worker, task_summary, success, category, insights, stored, broadcast
        """
        sender = msg.get("sender", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")

        dispatch = self._correlate_with_dispatch(sender, content)
        task_text = dispatch.get("task_summary", content) if dispatch else content
        dispatch_time = dispatch.get("timestamp", "") if dispatch else ""

        category, tags = categorize_task(task_text)
        success = detect_success(content)
        insights = extract_insights(task_text, content, success)

        result = {
            "worker": sender, "task_summary": task_text[:200],
            "result_summary": content[:200], "success": success,
            "category": category, "tags": tags, "insights": insights,
            "timestamp": timestamp, "dispatch_timestamp": dispatch_time,
            "strategy_id": dispatch.get("strategy_id", "") if dispatch else "",
            "stored": 0, "broadcast": 0, "evolution_updated": False,
        }

        self._store_insights(result, task_text, category, success, insights, sender)
        self._update_evolution(result, dispatch, dispatch_time, category, success)
        self._broadcast_top_insight(result, insights, category, tags)
        self._run_distill_hook(result, sender, task_text, content, success)

        return result

    def scan_bus(self) -> List[dict]:
        """Scan bus for new task results and process them.

        Returns:
            List of processed result dicts.
        """
        messages = bus_get(topic="orchestrator", limit=200)
        results = []
        new_count = 0

        for msg in messages:
            # Only process type=result messages
            if msg.get("type") != "result":
                continue

            # Skip our own messages
            if msg.get("sender") == "learner":
                continue

            # Dedup
            fp = _msg_fingerprint(msg)
            if fp in self.seen_ids:
                continue

            self.seen_ids.add(fp)
            new_count += 1

            # Process
            try:
                result = self.process_result(msg)
                results.append(result)
                self.state["total_processed"] = self.state.get("total_processed", 0) + 1
                
                # Root cause fix (2026-03-23): Mark dispatch as having received result
                # This closes the coherence gap where ALL dispatches showed result_received=False
                sender = msg.get("sender", "")
                if sender in ("alpha", "beta", "gamma", "delta"):
                    try:
                        from tools.skynet_task_tracker import mark_dispatch_result
                        mark_dispatch_result(sender, success=result.get("success", True))
                    except Exception as e:
                        logger.debug(f"Dispatch result marking failed: {e}")
                # signed: orchestrator
            except Exception as e:
                logger.error(f"Error processing message: {e}")

        if new_count > 0:
            logger.info(f"Processed {new_count} new results, extracted {sum(r['stored'] for r in results)} learnings")
        return results

    def report_health(self):
        """Post health status to bus."""
        now = time.time()
        if now - self._last_health_report < HEALTH_REPORT_INTERVAL:
            return
        self._last_health_report = now

        health = {
            "daemon": "skynet_learner",
            "uptime_cycles": self._cycle_count,
            "total_processed": self.state.get("total_processed", 0),
            "total_learnings": self.state.get("total_learnings", 0),
            "total_evolution_updates": self.state.get("total_evolution_updates", 0),
            "total_broadcasts": self.state.get("total_broadcasts", 0),
            "seen_ids_count": len(self.seen_ids),
            "timestamp": datetime.now().isoformat(),
        }
        bus_post("learner", "orchestrator", "daemon_health", json.dumps(health))
        logger.info(f"Health report: processed={health['total_processed']}, learnings={health['total_learnings']}")

    def run_once(self) -> List[dict]:
        """Single pass: scan bus, extract learnings, save state."""
        results = self.scan_bus()
        self.state["last_run"] = datetime.now().isoformat()
        self.state["seen_ids"] = list(self.seen_ids)
        _save_state(self.state)
        return results

    def _handle_signal(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT."""
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info(f"Received {sig_name} -- shutting down gracefully")
        self._shutting_down = True

    def _check_stale(self):
        """Post LEARNER_STALE alert if no new episodes for >600s."""
        # Root cause fix (2026-03-23): was 300s, producing false stale alerts when workers
        # are legitimately between tasks. 600s matches the old stuck threshold.  signed: orchestrator
        STALE_THRESHOLD = 600
        elapsed = time.time() - self._last_episode_time
        if elapsed > STALE_THRESHOLD and not self._stale_alerted:
            self._stale_alerted = True
            bus_post("learner", "orchestrator", "alert",
                     f"LEARNER_STALE: no new episodes for {int(elapsed)}s")
            logger.warning(f"Stale alert: no new episodes for {int(elapsed)}s")
        elif elapsed <= STALE_THRESHOLD and self._stale_alerted:
            self._stale_alerted = False

    def get_health(self) -> dict:
        """Return health info for the /learner/health endpoint."""
        import os
        return {
            "status": "running" if not self._shutting_down else "shutting_down",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._start_time, 1),
            "cycles": self._cycle_count,
            "episodes_processed": self.state.get("total_processed", 0),
            "total_learnings": self.state.get("total_learnings", 0),
            "last_activity": datetime.fromtimestamp(self._last_episode_time).isoformat(),
            "stale": self._stale_alerted,
            "stale_seconds": round(time.time() - self._last_episode_time, 1),
        }

    def run_daemon(self, interval: int = DEFAULT_LOOP_INTERVAL):
        """Run as a continuous daemon."""
        # PID file singleton
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                import psutil
                if psutil.pid_exists(old_pid):
                    try:
                        proc = psutil.Process(old_pid)
                        if "skynet_learner" in " ".join(proc.cmdline()):
                            logger.error(f"Another learner daemon running (PID {old_pid}). Exiting.")
                            sys.exit(1)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except ValueError:
                pass
            except ImportError:
                # psutil not available -- check PID via OS  # signed: delta
                try:
                    os.kill(old_pid, 0)
                    logger.error(f"PID {old_pid} still alive and psutil unavailable. Exiting to be safe.")
                    sys.exit(1)
                except (OSError, ProcessLookupError):
                    pass  # PID is dead, safe to take over

        import os
        PID_FILE.write_text(str(os.getpid()))

        # Register atexit for PID cleanup  # signed: delta
        import atexit
        def _cleanup_pid():
            try:
                if PID_FILE.exists() and int(PID_FILE.read_text().strip()) == os.getpid():
                    PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
        atexit.register(_cleanup_pid)

        logger.info(f"Learner daemon started (PID {os.getpid()}, interval={interval}s)")

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            while not self._shutting_down:
                self._cycle_count += 1
                try:
                    results = self.run_once()
                    if results:
                        self._last_episode_time = time.time()
                    self._check_stale()
                    self.report_health()
                    self._consecutive_loop_errors = 0  # reset on successful cycle  # signed: gamma
                except (ConnectionError, TimeoutError, URLError, OSError) as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    logger.error(f"Cycle network error ({self._consecutive_loop_errors}): {e}")
                except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    logger.error(f"Cycle data error ({self._consecutive_loop_errors}): {e}")
                except Exception as e:
                    self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
                    logger.error(f"Cycle error ({self._consecutive_loop_errors}): {e}")
                if getattr(self, '_consecutive_loop_errors', 0) >= 10 and self._consecutive_loop_errors % 10 == 0:
                    bus_post("learner", "orchestrator", "alert",
                             f"DAEMON_DEGRADED: skynet_learner hit {self._consecutive_loop_errors} consecutive errors")  # signed: gamma
                # Backoff: if bus is unreachable, slow down polling  # signed: alpha
                backoff = min(_bus_fetch_failures * 2, 30)
                time.sleep(interval + backoff)
        except KeyboardInterrupt:
            logger.info("Learner daemon stopped by keyboard interrupt")
        finally:
            logger.info("Learner daemon shutting down")
            if PID_FILE.exists():
                try:
                    # Only delete if we own the PID file  # signed: delta
                    if int(PID_FILE.read_text().strip()) == os.getpid():
                        PID_FILE.unlink()
                except (OSError, ValueError):
                    pass

    def get_stats(self) -> dict:
        """Return current learner statistics."""
        state = _load_state()

        # Also get LearningStore stats if available
        store_stats = {}
        if self.learning_system:
            try:
                store_stats = self.learning_system.store.stats()
            except Exception:
                pass

        return {
            "learner": {
                "total_processed": state.get("total_processed", 0),
                "total_learnings": state.get("total_learnings", 0),
                "total_evolution_updates": state.get("total_evolution_updates", 0),
                "total_broadcasts": state.get("total_broadcasts", 0),
                "seen_ids_count": len(state.get("seen_ids", [])),
                "last_run": state.get("last_run"),
                "started_at": state.get("started_at"),
            },
            "learning_store": store_stats,
        }


# ─── Inline Extract Function (for dispatch integration) ──

def extract_and_store(worker: str, task_text: str, result_text: str) -> dict:
    """Inline learning extraction -- call from dispatch pipeline on task completion.

    Args:
        worker: Worker name that completed the task.
        task_text: Original task description.
        result_text: Result summary from the worker.

    Returns:
        Dict with: category, success, insights, stored count.
    """
    category, tags = categorize_task(task_text)
    success = detect_success(result_text)
    insights = extract_insights(task_text, result_text, success)

    stored = 0
    try:
        from core.learning_store import PersistentLearningSystem
        pls = PersistentLearningSystem()
        fact_ids = pls.learn_from_task(
            task_description=task_text[:300],
            category=category,
            success=success,
            insights=insights,
        )
        stored = len(fact_ids)
    except Exception as e:
        logger.warning(f"Inline store error: {e}")

    return {
        "worker": worker,
        "category": category,
        "success": success,
        "insights": insights,
        "stored": stored,
    }


# ─── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Learning Feedback Loop Daemon")
    parser.add_argument("--daemon", action="store_true", help="Run as continuous daemon")
    parser.add_argument("--once", action="store_true", help="Single pass scan")
    parser.add_argument("--stats", action="store_true", help="Show learner statistics")
    parser.add_argument("--extract", type=str, metavar="TEXT", help="Extract learnings from text")
    parser.add_argument("--strategy", type=str, metavar="ID", help="Query episodes by strategy_id")
    parser.add_argument("--interval", type=int, default=DEFAULT_LOOP_INTERVAL, help="Daemon loop interval (seconds)")
    args = parser.parse_args()

    learner = SkynetLearner()

    if args.strategy:
        try:
            from skynet_brain import query_episodes_by_strategy
            episodes = query_episodes_by_strategy(args.strategy)
            if episodes:
                print(f"Found {len(episodes)} episode(s) for strategy {args.strategy}:")
                for ep in episodes:
                    print(json.dumps(ep, indent=2))
            else:
                print(f"No episodes found for strategy_id: {args.strategy}")
        except Exception as e:
            print(f"Error querying episodes: {e}")
        return

    if args.stats:
        stats = learner.get_stats()
        print(json.dumps(stats, indent=2, default=str))
        return

    if args.extract:
        category, tags = categorize_task(args.extract)
        success = detect_success(args.extract)
        insights = extract_insights(args.extract, args.extract, success)
        print(f"Category: {category}")
        print(f"Tags: {tags}")
        print(f"Success: {success}")
        print(f"Insights:")
        for i, insight in enumerate(insights, 1):
            print(f"  {i}. {insight}")
        return

    if args.once:
        results = learner.run_once()
        print(f"Processed {len(results)} new results")
        for r in results:
            status = "OK" if r["success"] else "FAIL"
            print(f"  [{status}] {r['worker']}: {r['category']} -- {r['stored']} facts stored")
        return

    if args.daemon:
        learner.run_daemon(interval=args.interval)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
