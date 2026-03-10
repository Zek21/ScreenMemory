#!/usr/bin/env python3
"""
Skynet Bus Watcher — Background daemon for orchestrator total bus awareness.

Polls the Skynet message bus, tracks worker activity, auto-routes requests
to idle workers, and maintains a rolling activity log.

Usage:
    python tools/skynet_bus_watcher.py          # Start daemon (Ctrl+C to stop)
    python tools/skynet_bus_watcher.py --once   # Single poll
    python tools/skynet_bus_watcher.py --status # Print current stats
"""

import json
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SKYNET_URL = "http://localhost:8420"
POLL_INTERVAL = 2.0
MAX_LOG = 50


class BusWatcher:
    """Polls Skynet bus for messages, tracks activity, auto-routes requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

        # Tracked state
        self.messages_processed = 0
        self.worker_activity = {}      # worker -> last_seen ISO timestamp
        self.pending_requests = []     # unrouted requests
        self.last_poll_ms = 0.0
        self._seen_ids = set()
        self._activity_log = deque(maxlen=MAX_LOG)

    # ── Event Handlers ───────────────────────────────────────────

    def on_result(self, msg):
        """Handle worker result messages — log and record metrics."""
        sender = msg.get("sender", "?")
        content = msg.get("content", "")[:120]
        self._log("result", f"[{sender}] {content}")

        with self._lock:
            self.worker_activity[sender] = datetime.now().isoformat()

        try:
            from tools.skynet_metrics import SkynetMetrics
            m = SkynetMetrics()
            m.record_bus_result(sender, content, self.last_poll_ms)
        except Exception:
            pass

    def on_request(self, msg):
        """Handle help/request messages — auto-route to idle workers."""
        sender = msg.get("sender", "?")
        content = msg.get("content", "")[:120]
        self._log("request", f"[{sender}] {content}")

        with self._lock:
            self.pending_requests.append({
                "id": msg.get("id", ""),
                "sender": sender,
                "content": content,
                "time": datetime.now().isoformat(),
            })

        try:
            from tools.skynet_dispatch import dispatch_to_idle
            result = dispatch_to_idle(content, exclude=[sender])
            if result:
                self._log("routed", f"Request from {sender} routed to idle worker")
                with self._lock:
                    self.pending_requests = [
                        r for r in self.pending_requests if r["id"] != msg.get("id")]
        except Exception as e:
            self._log("route_fail", f"Auto-route failed: {e}")

    def on_alert(self, msg):
        """Handle critical alert messages."""
        sender = msg.get("sender", "?")
        content = msg.get("content", "")[:120]
        self._log("ALERT", f"[{sender}] {content}")

    # ── Activity Log ─────────────────────────────────────────────

    def _log(self, event_type, message):
        entry = {
            "time": datetime.now().isoformat(),
            "type": event_type,
            "message": message,
        }
        with self._lock:
            self._activity_log.append(entry)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] {event_type:12s} {message}")

    def get_activity_log(self):
        with self._lock:
            return list(self._activity_log)

    def get_worker_activity(self):
        with self._lock:
            return dict(self.worker_activity)

    # ── Poll Loop ────────────────────────────────────────────────

    def _fetch_messages(self, limit=50):
        """Fetch recent bus messages from Skynet."""
        try:
            req = urllib.request.Request(
                f"{SKYNET_URL}/bus/messages?limit={limit}", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return data if isinstance(data, list) else data.get("messages", [])
        except Exception as e:
            self._log("error", f"Bus fetch failed: {e}")
            return []

    def poll_once(self):
        """Single poll cycle: fetch new messages and dispatch to handlers."""
        t0 = time.perf_counter()
        messages = self._fetch_messages()
        self.last_poll_ms = round((time.perf_counter() - t0) * 1000, 2)

        new_count = 0
        for msg in messages:
            msg_id = msg.get("id", "")
            if not msg_id or msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            new_count += 1

            with self._lock:
                self.messages_processed += 1
                sender = msg.get("sender", "")
                if sender:
                    self.worker_activity[sender] = datetime.now().isoformat()

            msg_type = msg.get("type", "").lower()
            if msg_type in ("result", "report"):
                self.on_result(msg)
            elif msg_type in ("request", "help", "ask"):
                self.on_request(msg)
            elif msg_type in ("alert", "critical", "error"):
                self.on_alert(msg)

        return new_count

    def _poll_loop(self):
        """Background polling thread."""
        self._log("system", f"Bus watcher started — polling every {POLL_INTERVAL}s")
        while self._running:
            try:
                self.poll_once()
            except Exception as e:
                self._log("error", f"Poll error: {e}")
            time.sleep(POLL_INTERVAL)
        self._log("system", "Bus watcher stopped")

    # ── Start / Stop ─────────────────────────────────────────────

    def start(self):
        """Begin polling in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_stats(self):
        """Return current watcher statistics."""
        with self._lock:
            return {
                "messages_processed": self.messages_processed,
                "seen_ids": len(self._seen_ids),
                "worker_activity": dict(self.worker_activity),
                "pending_requests": len(self.pending_requests),
                "last_poll_ms": self.last_poll_ms,
                "log_entries": len(self._activity_log),
                "running": self._running,
            }


def print_status():
    """Quick status check — single poll and print stats."""
    watcher = BusWatcher()
    new = watcher.poll_once()
    stats = watcher.get_stats()
    print(f"\n{'='*50}")
    print(f"  SKYNET BUS WATCHER — Status")
    print(f"{'='*50}")
    print(f"  Messages processed:  {stats['messages_processed']}")
    print(f"  Unique IDs seen:     {stats['seen_ids']}")
    print(f"  Last poll:           {stats['last_poll_ms']:.1f}ms")
    print(f"  New messages:        {new}")
    print(f"  Pending requests:    {stats['pending_requests']}")
    if stats['worker_activity']:
        print(f"  Worker activity:")
        for w, ts in sorted(stats['worker_activity'].items()):
            print(f"    {w:12s} last seen {ts[:19]}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Bus Watcher Daemon")
    parser.add_argument("--once", action="store_true", help="Single poll cycle")
    parser.add_argument("--status", action="store_true", help="Print stats and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.once:
        watcher = BusWatcher()
        new = watcher.poll_once()
        print(f"Polled: {new} new messages, {watcher.last_poll_ms:.1f}ms")
    else:
        watcher = BusWatcher()
        watcher.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            watcher.stop()
