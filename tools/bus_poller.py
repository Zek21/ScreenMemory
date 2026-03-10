#!/usr/bin/env python3
"""Skynet Bus Poller — monitors worker messages and reports to orchestrator.

Usage:
  python bus_poller.py                 # One-shot: print recent bus messages
  python bus_poller.py --watch         # Continuous: poll every 3s, print new messages
  python bus_poller.py --since MSG_ID  # Messages after a specific ID
  python bus_poller.py --topic X       # Filter by topic
  python bus_poller.py --sender X      # Filter by sender
  python bus_poller.py --publish "msg" # Publish a message as orchestrator
  python bus_poller.py --collect alpha,beta,gamma,delta --timeout 120  # Block until all report
  python bus_poller.py --subscribe     # SSE real-time listener
"""

import argparse
import json
import sys
import time
import threading
import urllib.request
import urllib.error

SKYNET_URL = "http://localhost:8420"


def bus_messages(limit=20, sender=None, topic=None):
    """Fetch recent bus messages with optional filters."""
    params = [f"limit={limit}"]
    if sender:
        params.append(f"sender={sender}")
    if topic:
        params.append(f"topic={topic}")
    url = f"{SKYNET_URL}/bus/messages?{'&'.join(params)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"[BUS] Connection failed: {e}", file=sys.stderr)
        return []


def bus_publish(sender, topic, msg_type, content, metadata=None):
    """Publish a message to the bus."""
    payload = json.dumps({
        "sender": sender,
        "topic": topic,
        "type": msg_type,
        "content": content,
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        f"{SKYNET_URL}/bus/publish",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"[BUS] Publish failed: {e}", file=sys.stderr)
        return None


def format_message(msg):
    """Pretty-print a bus message."""
    ts = msg.get("timestamp", "")[:19].replace("T", " ")
    sender = msg.get("sender", "?").upper()
    topic = msg.get("topic", "?")
    mtype = msg.get("type", "?")
    content = msg.get("content", "")
    meta = msg.get("metadata", {})
    line = f"[{ts}] {sender} → {topic} ({mtype}): {content}"
    if meta:
        line += f"  meta={json.dumps(meta)}"
    return line


def watch_loop(interval=3, sender=None, topic=None):
    """Continuously poll for new messages."""
    seen_ids = set()
    print(f"[BUS POLLER] Watching (interval={interval}s, sender={sender}, topic={topic})")
    print(f"[BUS POLLER] Press Ctrl+C to stop\n")

    # Load initial messages
    msgs = bus_messages(limit=50, sender=sender, topic=topic)
    for m in msgs:
        seen_ids.add(m.get("id", ""))
        print(format_message(m))
    if msgs:
        print(f"--- {len(msgs)} existing messages loaded ---\n")

    while True:
        try:
            time.sleep(interval)
            msgs = bus_messages(limit=50, sender=sender, topic=topic)
            for m in msgs:
                mid = m.get("id", "")
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    print(format_message(m))
                    sys.stdout.flush()
        except KeyboardInterrupt:
            print("\n[BUS POLLER] Stopped.")
            break


def publish_result(sender, content):
    """Shortcut: publish a type=result message to topic=orchestrator."""
    return bus_publish(sender, "orchestrator", "result", content)


def collect_results(expected_senders, timeout=120, poll_interval=3):
    """Block until bus messages with type=result from ALL expected_senders are received or timeout.

    Args:
        expected_senders: list of sender names to wait for (e.g. ["alpha", "beta"])
        timeout: max seconds to wait
        poll_interval: seconds between polls

    Returns:
        dict of sender -> message dict for each collected result
    """
    collected = {}
    pending = set(s.lower() for s in expected_senders)
    seen_ids = set()
    deadline = time.time() + timeout

    print(f"[COLLECT] Waiting for results from: {', '.join(sorted(pending))}")
    print(f"[COLLECT] Timeout: {timeout}s, poll: {poll_interval}s\n")

    while pending and time.time() < deadline:
        msgs = bus_messages(limit=100, topic="orchestrator")
        for m in msgs:
            mid = m.get("id", "")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            sender = (m.get("sender") or "").lower()
            mtype = (m.get("type") or "").lower()
            if mtype == "result" and sender in pending:
                collected[sender] = m
                pending.discard(sender)
                elapsed = timeout - (deadline - time.time())
                print(f"[COLLECT] ✓ {sender.upper()} reported ({elapsed:.1f}s): {(m.get('content') or '')[:120]}")
                sys.stdout.flush()
        if pending:
            time.sleep(poll_interval)

    if pending:
        print(f"\n[COLLECT] ✗ Timed out after {timeout}s. Missing: {', '.join(sorted(pending))}")
    else:
        elapsed = timeout - (deadline - time.time())
        print(f"\n[COLLECT] ✓ All {len(collected)} results collected in {elapsed:.1f}s")

    return collected


def subscribe_sse(callback, stop_event=None):
    """Connect to SSE stream endpoint and call callback(message) for each event.

    Runs until stop_event is set or connection drops.
    callback receives parsed JSON dict for each event.
    """
    url = f"{SKYNET_URL}/stream"
    stop = stop_event or threading.Event()

    while not stop.is_set():
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                buffer = ""
                while not stop.is_set():
                    chunk = resp.read(1).decode("utf-8", errors="replace")
                    if not chunk:
                        break
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_block, buffer = buffer.split("\n\n", 1)
                        data_lines = []
                        for line in event_block.split("\n"):
                            if line.startswith("data:"):
                                data_lines.append(line[5:].strip())
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                msg = json.loads(raw)
                                callback(msg)
                            except json.JSONDecodeError:
                                callback({"raw": raw})
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if not stop.is_set():
                print(f"[SSE] Connection lost ({e}), reconnecting in 3s...", file=sys.stderr)
                time.sleep(3)
        except Exception as e:
            if not stop.is_set():
                print(f"[SSE] Error: {e}, reconnecting in 5s...", file=sys.stderr)
                time.sleep(5)


def subscribe_sse_threaded(callback):
    """Start SSE listener in a background daemon thread. Returns (thread, stop_event)."""
    stop = threading.Event()
    t = threading.Thread(target=subscribe_sse, args=(callback, stop), daemon=True)
    t.start()
    return t, stop


def main():
    parser = argparse.ArgumentParser(description="Skynet Bus Poller")
    parser.add_argument("--watch", action="store_true", help="Continuous polling mode")
    parser.add_argument("--interval", type=int, default=3, help="Poll interval in seconds")
    parser.add_argument("--limit", type=int, default=20, help="Max messages to fetch")
    parser.add_argument("--sender", type=str, help="Filter by sender")
    parser.add_argument("--topic", type=str, help="Filter by topic")
    parser.add_argument("--publish", type=str, help="Publish a message as orchestrator")
    parser.add_argument("--pub-topic", type=str, default="workers", help="Topic for --publish")
    parser.add_argument("--pub-type", type=str, default="broadcast", help="Type for --publish")
    parser.add_argument("--collect", type=str, help="Comma-separated senders to collect results from")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout for --collect in seconds")
    parser.add_argument("--subscribe", action="store_true", help="SSE real-time listener")
    parser.add_argument("--result", type=str, nargs=2, metavar=("SENDER", "CONTENT"),
                        help="Publish a result message: --result gamma 'task done'")
    args = parser.parse_args()

    if args.result:
        sender, content = args.result
        result = publish_result(sender, content)
        if result:
            print(f"[BUS] Result published: {json.dumps(result)}")
        return

    if args.publish:
        result = bus_publish("orchestrator", args.pub_topic, args.pub_type, args.publish)
        if result:
            print(f"[BUS] Published: {json.dumps(result)}")
        return

    if args.collect:
        senders = [s.strip() for s in args.collect.split(",") if s.strip()]
        collected = collect_results(senders, timeout=args.timeout, poll_interval=args.interval)
        print(f"\n[COLLECT] Results ({len(collected)}/{len(senders)}):")
        for s, m in sorted(collected.items()):
            print(f"  {s.upper()}: {(m.get('content') or '')[:200]}")
        if len(collected) < len(senders):
            sys.exit(1)
        return

    if args.subscribe:
        print("[SSE] Subscribing to bus stream... Press Ctrl+C to stop\n")
        try:
            subscribe_sse(lambda m: print(format_message(m)) or sys.stdout.flush())
        except KeyboardInterrupt:
            print("\n[SSE] Stopped.")
        return

    if args.watch:
        watch_loop(interval=args.interval, sender=args.sender, topic=args.topic)
    else:
        msgs = bus_messages(limit=args.limit, sender=args.sender, topic=args.topic)
        for m in msgs:
            print(format_message(m))
        if not msgs:
            print("[BUS] No messages.")


if __name__ == "__main__":
    main()
