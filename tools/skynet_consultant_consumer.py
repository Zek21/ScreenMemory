#!/usr/bin/env python3
"""Consultant bridge prompt queue consumer daemon.

Polls GET /consultants/prompts/next on the consultant bridge, ACKs pending
prompts, relays them to the Skynet bus as topic=consultant type=directive,
and marks them complete.

Usage:
    python tools/skynet_consultant_consumer.py --port 8422 --consultant-id consultant
    python tools/skynet_consultant_consumer.py --port 8425 --consultant-id gemini_consultant

# signed: alpha
"""

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_DIR = ROOT / "data"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("consultant_consumer")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)

POLL_INTERVAL = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0
_shutdown = False


def _pid_path(port: int) -> Path:
    return ROOT / "data" / f"consultant_consumer_{port}.pid"  # signed: alpha


def _acquire_pid_lock(port: int) -> bool:
    """PID file singleton lock. Returns True if lock acquired."""  # signed: alpha
    pid_file = _pid_path(port)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if process is still alive (Windows-compatible)
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                logger.error("Another consumer already running on port %d (PID %d)", port, old_pid)
                return False
            # Stale PID file — process is dead
            logger.info("Removing stale PID file for port %d (PID %d dead)", port, old_pid)
        except (ValueError, OSError):
            pass
    pid_file.write_text(str(os.getpid()))
    logger.info("PID lock acquired: %s (PID %d)", pid_file, os.getpid())
    return True


def _release_pid_lock(port: int) -> None:
    """Remove PID file on exit."""  # signed: alpha
    pid_file = _pid_path(port)
    try:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
            logger.info("PID lock released: %s", pid_file)
    except Exception:
        pass


def _http_get(url: str, timeout: float = 5.0):
    """HTTP GET returning parsed JSON or None on error."""  # signed: alpha
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug("GET %s failed: %s", url, e)
        return None


def _http_post(url: str, data: dict, timeout: float = 5.0):
    """HTTP POST returning parsed JSON or None on error."""  # signed: alpha
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug("POST %s failed: %s", url, e)
        return None


def _guarded_bus_publish(msg: dict) -> bool:
    """Publish to Skynet bus via SpamGuard. Returns True on success.

    No raw fallback — bypassing SpamGuard costs -1.0 score per anti-spam rules.
    If guarded_publish fails, we log and return False rather than risk penalty.
    """  # signed: delta
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(msg)
        return True
    except Exception as e:
        logger.warning("guarded_publish failed: %s — message dropped (no raw fallback)", e)
        return False


def _process_prompt(base_url: str, consultant_id: str, prompt: dict) -> bool:
    """Process a single prompt: ACK → bus relay → complete. Returns True on success."""
    # signed: alpha
    prompt_id = prompt.get("id", "")
    content = prompt.get("content", "")
    sender = prompt.get("sender", "unknown")
    prompt_type = prompt.get("type", "directive")
    metadata = prompt.get("metadata", {})

    if not prompt_id or not content:
        logger.warning("Skipping prompt with missing id or content: %s", prompt)
        return False

    logger.info("Processing prompt %s from %s (type=%s, %d chars)",
                prompt_id, sender, prompt_type, len(content))

    # Step 1: ACK the prompt
    ack_result = None
    for attempt in range(MAX_RETRIES):
        ack_result = _http_post(
            f"{base_url}/consultants/prompts/ack",
            {"prompt_id": prompt_id, "consumer": consultant_id},
        )
        if ack_result is not None:
            break
        logger.warning("ACK attempt %d/%d failed for %s", attempt + 1, MAX_RETRIES, prompt_id)
        time.sleep(RETRY_DELAY)

    if ack_result is None:
        logger.error("Failed to ACK prompt %s after %d attempts", prompt_id, MAX_RETRIES)
        return False

    logger.info("ACK'd prompt %s", prompt_id)

    # Step 2: Relay to Skynet bus as consultant directive
    bus_msg = {
        "sender": sender,
        "topic": "consultant",
        "type": "directive",
        "content": content,
        "metadata": {
            "prompt_id": prompt_id,
            "consultant_id": consultant_id,
            "original_type": prompt_type,
            **(metadata if isinstance(metadata, dict) else {}),
        },
    }
    bus_ok = _guarded_bus_publish(bus_msg)
    if not bus_ok:
        logger.error("Failed to relay prompt %s to bus", prompt_id)
        # Still mark complete to avoid infinite retry on bus failures

    # Step 3: Mark complete
    complete_result = None
    for attempt in range(MAX_RETRIES):
        complete_result = _http_post(
            f"{base_url}/consultants/prompts/complete",
            {
                "prompt_id": prompt_id,
                "result": "relayed_to_bus" if bus_ok else "bus_relay_failed",
                "status": "completed",
            },
        )
        if complete_result is not None:
            break
        logger.warning("Complete attempt %d/%d failed for %s", attempt + 1, MAX_RETRIES, prompt_id)
        time.sleep(RETRY_DELAY)

    if complete_result is None:
        logger.error("Failed to mark prompt %s complete after %d attempts", prompt_id, MAX_RETRIES)
        return False

    logger.info("Prompt %s processed successfully (bus_relay=%s)", prompt_id, bus_ok)
    return True


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""  # signed: alpha
    global _shutdown
    logger.info("Received signal %d, shutting down...", signum)
    _shutdown = True


def run_consumer(port: int, consultant_id: str) -> None:
    """Main consumer loop."""  # signed: alpha
    global _shutdown

    base_url = f"http://localhost:{port}"
    logger.info("Starting consultant consumer for %s on port %d", consultant_id, port)

    # Verify bridge is reachable
    health = _http_get(f"{base_url}/health")
    if health is None:
        logger.warning("Bridge at %s not reachable — will retry in poll loop", base_url)
    else:
        logger.info("Bridge healthy: %s", health.get("service", "unknown"))

    # Announce on bus
    _guarded_bus_publish({
        "sender": consultant_id,
        "topic": "system",
        "type": "daemon_start",
        "content": f"Consultant consumer daemon started for {consultant_id} on port {port}",
    })

    consecutive_errors = 0
    prompts_processed = 0

    while not _shutdown:
        try:
            resp = _http_get(f"{base_url}/consultants/prompts/next")
            if resp is None:
                consecutive_errors += 1
                if consecutive_errors % 15 == 1:  # Log every ~30s
                    logger.warning("Bridge unreachable (%d consecutive errors)", consecutive_errors)
                time.sleep(POLL_INTERVAL)
                continue

            consecutive_errors = 0
            prompt = resp.get("prompt")

            if prompt is None:
                # No pending prompts — normal idle
                time.sleep(POLL_INTERVAL)
                continue

            # Process the prompt
            success = _process_prompt(base_url, consultant_id, prompt)
            if success:
                prompts_processed += 1
                if prompts_processed % 10 == 0:
                    logger.info("Total prompts processed: %d", prompts_processed)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
            break
        except Exception as e:
            logger.error("Unexpected error in poll loop: %s", e)
            consecutive_errors += 1
            time.sleep(POLL_INTERVAL)

    logger.info("Consumer shutdown. Total prompts processed: %d", prompts_processed)


def main():
    """CLI entry point."""  # signed: alpha
    parser = argparse.ArgumentParser(description="Consultant bridge prompt queue consumer")
    parser.add_argument("--port", type=int, required=True,
                        help="Consultant bridge port (8422 for Codex, 8425 for Gemini)")
    parser.add_argument("--consultant-id", type=str, required=True,
                        help="Consultant sender ID (consultant or gemini_consultant)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between polls (default: 2)")
    parser.add_argument("--log-file", type=str, default=None,
                        help="Log file path (default: data/consultant_consumer_PORT.log)")
    args = parser.parse_args()

    global POLL_INTERVAL
    POLL_INTERVAL = args.poll_interval

    # File logging
    log_path = args.log_file or str(ROOT / "data" / f"consultant_consumer_{args.port}.log")
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    except Exception as e:
        logger.warning("Could not set up file logging: %s", e)

    # PID lock
    if not _acquire_pid_lock(args.port):
        logger.error("Failed to acquire PID lock — exiting")
        sys.exit(1)

    # Register cleanup
    atexit.register(_release_pid_lock, args.port)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    try:
        run_consumer(args.port, args.consultant_id)
    finally:
        _release_pid_lock(args.port)


if __name__ == "__main__":
    main()
