#!/usr/bin/env python3
"""
skynet_bus_persist.py -- Persistent bus message archiver.

Subscribes to the Skynet /stream SSE endpoint and appends every bus message
to data/bus_archive.jsonl (one JSON object per line, append-only).

This solves the ring buffer overflow problem: the Go backend ring buffer
holds only 100 messages with FIFO eviction. During burst traffic, older
messages are silently overwritten. This daemon persists ALL messages to disk
so they survive both ring buffer overflow and server crashes.

Archive format (JSONL -- one JSON per line):
    {"id":"msg_1_alpha","sender":"alpha","topic":"orchestrator","type":"result",
     "content":"task done","timestamp":"2026-03-12T04:30:00Z","archived_at":1741...}

Usage:
    python tools/skynet_bus_persist.py              # Run daemon (foreground)
    python tools/skynet_bus_persist.py --stats       # Show archive stats
    python tools/skynet_bus_persist.py --tail 20     # Show last 20 messages
    python tools/skynet_bus_persist.py --search "keyword"  # Search archive

Daemon management:
    Start: python tools/skynet_bus_persist.py &
    PID file: data/bus_persist.pid
"""
# signed: gamma

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ARCHIVE_FILE = DATA_DIR / "bus_archive.jsonl"
PID_FILE = DATA_DIR / "bus_persist.pid"
STREAM_URL = "http://localhost:8420/stream"
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # 50 MB rotation threshold
ROTATION_CHECK_INTERVAL = 100  # Check rotation every N archived messages
MAX_ROTATED_FILES = 30  # Keep last N rotated files, delete older ones
RECONNECT_DELAY_S = 5
MAX_RECONNECT_DELAY_S = 60
BUS_CATCHUP_URL = "http://localhost:8420/bus/messages?limit=100"  # signed: beta

_running = True


def _signal_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT/SIGBREAK."""
    global _running
    _running = False
    # signed: gamma


def _acquire_pid_lock() -> bool:
    """Acquire PID file lock. Returns False if another instance is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is alive (Windows-compatible)
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return False  # Process still alive
        except (ValueError, OSError, AttributeError):
            pass  # PID file corrupt or process dead
    PID_FILE.write_text(str(os.getpid()))
    return True
    # signed: gamma


def _release_pid_lock():
    """Release PID file lock."""
    try:
        if PID_FILE.exists():
            stored_pid = int(PID_FILE.read_text().strip())
            if stored_pid == os.getpid():
                PID_FILE.unlink()
    except (ValueError, OSError):
        pass
    # signed: gamma


def _rotate_if_needed():
    """Rotate archive if it exceeds MAX_ARCHIVE_BYTES.

    Uses date-based naming: bus_archive_YYYYMMDD.jsonl (with _N suffix
    if multiple rotations occur on the same day). After rotation, cleans
    up old rotated files beyond MAX_ROTATED_FILES retention limit.
    """  # signed: beta
    if not ARCHIVE_FILE.exists():
        return
    try:
        size = ARCHIVE_FILE.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")
            base_name = f"bus_archive_{date_str}"

            # Find unique suffix if same-day rotation already exists
            suffix = 0
            rotated = DATA_DIR / f"{base_name}.jsonl"
            while rotated.exists():
                suffix += 1
                rotated = DATA_DIR / f"{base_name}_{suffix}.jsonl"

            ARCHIVE_FILE.rename(rotated)
            print(f"[BUS_PERSIST] Rotated archive ({size // 1024}KB) -> {rotated.name}")

            _cleanup_old_rotated_files()
    except OSError as e:
        print(f"[BUS_PERSIST] Rotation error: {e}")


def _cleanup_old_rotated_files():
    """Remove rotated archive files beyond MAX_ROTATED_FILES retention limit.

    Keeps the most recent files by modification time. Only targets files
    matching the bus_archive_*.jsonl pattern (not the active archive).
    """  # signed: beta
    try:
        rotated = sorted(
            DATA_DIR.glob("bus_archive_*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if len(rotated) > MAX_ROTATED_FILES:
            for old_file in rotated[MAX_ROTATED_FILES:]:
                old_size = old_file.stat().st_size
                old_file.unlink()
                print(f"[BUS_PERSIST] Deleted old archive: {old_file.name} "
                      f"({old_size // 1024}KB)")
    except OSError as e:
        print(f"[BUS_PERSIST] Cleanup error: {e}")


def _archive_message(msg: dict):
    """Append a single message to the JSONL archive."""
    msg["archived_at"] = time.time()
    try:
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, default=str) + "\n")
    except OSError as e:
        print(f"[BUS_PERSIST] Write error: {e}")
    # signed: gamma


def _parse_sse_messages(line: str) -> list:
    """Parse SSE data line into bus messages.

    The /stream endpoint emits JSON with a 'bus' array containing recent messages.
    We track seen message IDs to avoid duplicates across SSE ticks.
    """
    if not line.startswith("data: "):
        return []
    try:
        payload = json.loads(line[6:])
        return payload.get("bus", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    # signed: gamma


def _catchup_missed_messages(seen_ids: set) -> int:
    """Fetch recent bus messages via HTTP to catch any missed during SSE downtime.

    After an SSE disconnection, the ring buffer may contain messages that were
    published while we were reconnecting. This queries /bus/messages to retrieve
    them before resuming the SSE stream, closing the message loss window.

    Returns the number of newly archived messages.
    """  # signed: beta
    import urllib.request
    import urllib.error

    caught = 0
    try:
        req = urllib.request.Request(BUS_CATCHUP_URL)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        messages = data if isinstance(data, list) else data.get("messages", [])
        for msg in messages:
            msg_id = msg.get("id", "")
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                _archive_message(msg)
                caught += 1
        if caught:
            print(f"[BUS_PERSIST] Caught up {caught} missed messages after reconnect")
    except Exception as e:
        print(f"[BUS_PERSIST] Catchup failed (non-fatal): {e}")
    return caught
    # signed: beta


def run_daemon():
    """Main daemon loop: subscribe to SSE stream, archive all bus messages."""
    import urllib.request
    import urllib.error

    if not _acquire_pid_lock():
        print("[BUS_PERSIST] Another instance is running. Exiting.")
        return

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    print(f"[BUS_PERSIST] Starting persistent bus archiver (PID={os.getpid()})")
    print(f"[BUS_PERSIST] Archive: {ARCHIVE_FILE}")
    print(f"[BUS_PERSIST] Stream: {STREAM_URL}")

    seen_ids: set = set()
    total_archived = 0
    reconnect_delay = RECONNECT_DELAY_S

    try:
        while _running:
            try:
                _rotate_if_needed()
                # Catch up on messages missed during SSE downtime  # signed: beta
                catchup_count = _catchup_missed_messages(seen_ids)
                total_archived += catchup_count
                req = urllib.request.Request(STREAM_URL)
                req.add_header("Accept", "text/event-stream")
                resp = urllib.request.urlopen(req, timeout=30)
                print(f"[BUS_PERSIST] Connected to SSE stream")
                reconnect_delay = RECONNECT_DELAY_S  # Reset on success

                # Keep seen_ids bounded (last 500 IDs)
                if len(seen_ids) > 1000:
                    seen_ids = set(list(seen_ids)[-500:])

                msgs_since_rotation_check = 0  # signed: beta

                while _running:
                    raw = resp.readline()
                    if not raw:
                        break  # Connection closed
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    messages = _parse_sse_messages(line)
                    for msg in messages:
                        msg_id = msg.get("id", "")
                        if msg_id and msg_id not in seen_ids:
                            seen_ids.add(msg_id)
                            _archive_message(msg)
                            total_archived += 1
                            msgs_since_rotation_check += 1
                            if total_archived % 100 == 0:
                                print(f"[BUS_PERSIST] Archived {total_archived} messages")

                    # Periodic rotation check during continuous operation
                    if msgs_since_rotation_check >= ROTATION_CHECK_INTERVAL:
                        _rotate_if_needed()
                        msgs_since_rotation_check = 0  # signed: beta

            except (urllib.error.URLError, OSError, ConnectionError) as e:
                if _running:
                    print(f"[BUS_PERSIST] Connection error: {e}. "
                          f"Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_S)
            except Exception as e:
                if _running:
                    print(f"[BUS_PERSIST] Unexpected error: {e}. "
                          f"Reconnecting in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_S)
    finally:
        _release_pid_lock()
        print(f"[BUS_PERSIST] Stopped. Total archived: {total_archived}")
    # signed: gamma


def show_stats():
    """Display archive statistics."""
    if not ARCHIVE_FILE.exists():
        print("No archive file found.")
        return
    line_count = 0
    senders: dict = {}
    topics: dict = {}
    first_ts = None
    last_ts = None
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    line_count += 1
                    s = msg.get("sender", "unknown")
                    t = msg.get("topic", "unknown")
                    senders[s] = senders.get(s, 0) + 1
                    topics[t] = topics.get(t, 0) + 1
                    ts = msg.get("archived_at") or msg.get("timestamp")
                    if ts and (first_ts is None or ts < first_ts):
                        first_ts = ts
                    if ts and (last_ts is None or ts > last_ts):
                        last_ts = ts
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"Error reading archive: {e}")
        return
    size = ARCHIVE_FILE.stat().st_size
    print(f"Archive: {ARCHIVE_FILE}")
    print(f"Size: {size / 1024:.1f} KB")
    print(f"Messages: {line_count}")
    print(f"Senders: {dict(sorted(senders.items(), key=lambda x: -x[1]))}")
    print(f"Topics: {dict(sorted(topics.items(), key=lambda x: -x[1]))}")
    # signed: gamma


def show_tail(n: int = 20):
    """Show last N messages from the archive."""
    if not ARCHIVE_FILE.exists():
        print("No archive file found.")
        return
    lines = []
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
                    if len(lines) > n:
                        lines.pop(0)
    except OSError as e:
        print(f"Error reading archive: {e}")
        return
    for line in lines:
        try:
            msg = json.loads(line)
            ts = msg.get("timestamp", "?")
            sender = msg.get("sender", "?")
            topic = msg.get("topic", "?")
            mtype = msg.get("type", "?")
            content = msg.get("content", "")[:80]
            print(f"[{ts}] {sender} -> {topic}/{mtype}: {content}")
        except json.JSONDecodeError:
            print(f"  (parse error: {line[:60]}...)")
    # signed: gamma


def search_archive(keyword: str, limit: int = 50):
    """Search archive for messages containing keyword."""
    if not ARCHIVE_FILE.exists():
        print("No archive file found.")
        return
    keyword_lower = keyword.lower()
    found = 0
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if keyword_lower in line.lower():
                    try:
                        msg = json.loads(line.strip())
                        ts = msg.get("timestamp", "?")
                        sender = msg.get("sender", "?")
                        content = msg.get("content", "")[:100]
                        print(f"[{ts}] {sender}: {content}")
                        found += 1
                        if found >= limit:
                            print(f"... (truncated at {limit} results)")
                            break
                    except json.JSONDecodeError:
                        continue
    except OSError as e:
        print(f"Error reading archive: {e}")
        return
    print(f"\nFound: {found} messages matching '{keyword}'")
    # signed: gamma


def diagnose_archive():
    """Analyze the JSONL archive and print comprehensive diagnostics.

    Examines: message count by sender, messages by topic, spam blocked rate,
    peak message rates (per-minute), average message size, and largest messages.
    """
    if not ARCHIVE_FILE.exists():
        print("No archive file found.")
        return

    total = 0
    senders: dict = {}
    topics: dict = {}
    types: dict = {}
    sizes: list = []
    largest: list = []  # (size, sender, content_preview)
    minute_buckets: dict = {}  # minute_key -> count
    first_ts = None
    last_ts = None
    parse_errors = 0

    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    total += 1
                    s = msg.get("sender", "unknown")
                    t = msg.get("topic", "unknown")
                    mt = msg.get("type", "unknown")
                    content = msg.get("content", "")

                    senders[s] = senders.get(s, 0) + 1
                    topics[t] = topics.get(t, 0) + 1
                    types[mt] = types.get(mt, 0) + 1

                    msg_size = len(line)
                    sizes.append(msg_size)

                    # Track largest messages
                    largest.append((msg_size, s, str(content)[:80]))
                    if len(largest) > 10:
                        largest.sort(key=lambda x: -x[0])
                        largest = largest[:10]

                    # Minute bucketing for peak rate analysis
                    ts = msg.get("archived_at") or msg.get("timestamp")
                    if ts:
                        try:
                            if isinstance(ts, (int, float)):
                                minute_key = int(ts) // 60
                            elif isinstance(ts, str) and "T" in ts:
                                from datetime import datetime as _dt
                                dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                                minute_key = int(dt.timestamp()) // 60
                            else:
                                minute_key = None
                            if minute_key:
                                minute_buckets[minute_key] = \
                                    minute_buckets.get(minute_key, 0) + 1
                        except (ValueError, TypeError):
                            pass
                    if ts and (first_ts is None or str(ts) < str(first_ts)):
                        first_ts = ts
                    if ts and (last_ts is None or str(ts) > str(last_ts)):
                        last_ts = ts
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
    except OSError as e:
        print(f"Error reading archive: {e}")
        return

    file_size = ARCHIVE_FILE.stat().st_size

    # Calculate stats
    avg_size = sum(sizes) / len(sizes) if sizes else 0
    peak_rate = max(minute_buckets.values()) if minute_buckets else 0
    peak_minute = None
    if minute_buckets:
        peak_minute_key = max(minute_buckets, key=minute_buckets.get)
        try:
            from datetime import datetime as _dt
            peak_minute = _dt.utcfromtimestamp(
                peak_minute_key * 60).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            peak_minute = str(peak_minute_key)

    largest.sort(key=lambda x: -x[0])

    print("=" * 60)
    print("       SKYNET BUS ARCHIVE DIAGNOSTICS")
    print("=" * 60)
    print(f"\nArchive file:  {ARCHIVE_FILE}")
    print(f"File size:     {file_size / 1024:.1f} KB ({file_size / 1024 / 1024:.2f} MB)")
    print(f"Total msgs:    {total}")
    print(f"Parse errors:  {parse_errors}")
    print(f"Time range:    {first_ts} -> {last_ts}")

    print(f"\n--- Messages by Sender ({len(senders)} senders) ---")
    for sender, count in sorted(senders.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        bar = "#" * int(pct / 2)
        print(f"  {sender:<25} {count:>6} ({pct:5.1f}%) {bar}")

    print(f"\n--- Messages by Topic ({len(topics)} topics) ---")
    for topic, count in sorted(topics.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"  {topic:<25} {count:>6} ({pct:5.1f}%)")

    print(f"\n--- Messages by Type ({len(types)} types) ---")
    for mtype, count in sorted(types.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"  {mtype:<25} {count:>6} ({pct:5.1f}%)")

    print(f"\n--- Message Size ---")
    print(f"  Average:  {avg_size:.0f} bytes")
    print(f"  Min:      {min(sizes) if sizes else 0} bytes")
    print(f"  Max:      {max(sizes) if sizes else 0} bytes")
    print(f"  Total:    {sum(sizes) / 1024:.1f} KB")

    print(f"\n--- Peak Message Rate ---")
    print(f"  Peak rate:  {peak_rate} msgs/min")
    if peak_minute:
        print(f"  Peak time:  {peak_minute} UTC")
    if minute_buckets:
        avg_rate = total / len(minute_buckets) if minute_buckets else 0
        print(f"  Avg rate:   {avg_rate:.1f} msgs/min")

    if largest:
        print(f"\n--- Top {len(largest)} Largest Messages ---")
        for i, (sz, sender, preview) in enumerate(largest[:10], 1):
            print(f"  {i}. {sz:>6} bytes | {sender:<15} | {preview}")

    # Spam detection indicators
    print(f"\n--- Spam Indicators ---")
    spam_file = DATA_DIR / "spam_log.json"
    if spam_file.exists():
        try:
            with open(spam_file, "r", encoding="utf-8") as f:
                spam_log = json.load(f)
            entries = spam_log.get("entries", [])
            print(f"  Spam log entries: {len(entries)}")
            if entries:
                by_sender = {}
                for e in entries:
                    s = e.get("sender", "unknown")
                    by_sender[s] = by_sender.get(s, 0) + 1
                print("  Blocked by sender:")
                for s, c in sorted(by_sender.items(), key=lambda x: -x[1]):
                    print(f"    {s:<20} {c:>5}")
        except (json.JSONDecodeError, OSError):
            print("  Spam log: unreadable")
    else:
        print("  No spam log file found")

    print("\n" + "=" * 60)
    # signed: gamma


def main():
    parser = argparse.ArgumentParser(
        description="Persistent bus message archiver for Skynet"
    )
    parser.add_argument("--stats", action="store_true", help="Show archive statistics")
    parser.add_argument("--tail", type=int, metavar="N", help="Show last N messages")
    parser.add_argument("--search", type=str, metavar="KEYWORD", help="Search archive")
    parser.add_argument("--diagnose", action="store_true",
                        help="Full archive diagnostics: sender/topic/type breakdown, "
                             "peak rates, message sizes, spam indicators")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.tail is not None:
        show_tail(args.tail)
    elif args.search:
        search_archive(args.search)
    elif args.diagnose:
        diagnose_archive()
    else:
        run_daemon()
    # signed: gamma


if __name__ == "__main__":
    main()
