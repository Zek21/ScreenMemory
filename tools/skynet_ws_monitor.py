#!/usr/bin/env python3
"""
Skynet WebSocket Monitor — Real-time event listener via stdlib WebSocket.

Connects to ws://localhost:8420/ws and receives security alerts, bus events,
and system notifications in real time. Logs to data/ws_events.log.

Usage:
    python tools/skynet_ws_monitor.py             # Listen for 10 seconds
    python tools/skynet_ws_monitor.py --duration 60  # Listen for 60 seconds
    python tools/skynet_ws_monitor.py --forever    # Listen until Ctrl+C
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "ws_monitor.pid"  # signed: alpha
LOG_FILE = ROOT / "data" / "ws_events.log"
WS_HOST = "localhost"
WS_PORT = 8420
WS_PATH = "/ws"
WS_MAGIC = "258EAFA5-E914-47DA-95CA-5AB9964C0DA2"


def _generate_key():
    """Generate a random WebSocket key."""
    return base64.b64encode(os.urandom(16)).decode()


def _build_handshake(host, port, path, key):
    """Build the HTTP upgrade request for WebSocket handshake."""
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()


def _validate_handshake(response_bytes, key):
    """Validate the server's handshake response."""
    response = response_bytes.decode(errors="replace")
    if "101" not in response.split("\r\n")[0]:
        raise ConnectionError(f"Handshake failed: {response.split(chr(10))[0]}")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()
    if expected_accept not in response:
        raise ConnectionError("Invalid Sec-WebSocket-Accept")
    return True


def _recv_frame(sock):
    """Read a single WebSocket frame. Returns (opcode, payload_bytes)."""
    header = _recv_exact(sock, 2)
    if not header or len(header) < 2:
        return None, b""

    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        raw = _recv_exact(sock, 2)
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = _recv_exact(sock, 8)
        length = struct.unpack("!Q", raw)[0]

    if masked:
        mask_key = _recv_exact(sock, 4)
        payload = _recv_exact(sock, length)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    else:
        payload = _recv_exact(sock, length)

    return opcode, payload


def _recv_exact(sock, n):
    """Receive exactly n bytes from socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)


def _send_frame(sock, opcode, payload):
    """Send a masked WebSocket frame (client must mask)."""
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN + opcode

    length = len(payload)
    if length < 126:
        frame.append(0x80 | length)  # MASK bit set
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack("!Q", length))

    mask_key = os.urandom(4)
    frame.extend(mask_key)
    frame.extend(bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload)))
    sock.sendall(frame)


def _send_pong(sock, payload):
    """Send a pong frame in response to ping."""
    _send_frame(sock, 0xA, payload)


def _send_close(sock):
    """Send a close frame."""
    try:
        _send_frame(sock, 0x8, b"")
    except Exception:
        pass


class WSMonitor:
    """WebSocket client for Skynet real-time event monitoring."""

    def __init__(self, host=WS_HOST, port=WS_PORT, path=WS_PATH):
        self.host = host
        self.port = port
        self.path = path
        self._sock = None
        self._connected = False
        self._callbacks = []
        self._listen_thread = None
        self._running = False
        self.events_received = 0

    def connect(self):
        """Perform WebSocket handshake and establish connection."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        self._sock.connect((self.host, self.port))

        key = _generate_key()
        self._sock.sendall(_build_handshake(self.host, self.port, self.path, key))

        # Read handshake response (up to 4KB)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("No handshake response")
            response += chunk

        _validate_handshake(response, key)
        self._connected = True
        self._sock.settimeout(1.0)  # non-blocking reads for listen loop
        return True

    def on_event(self, callback):
        """Register a callback for received events. callback(event_dict)."""
        self._callbacks.append(callback)

    def _dispatch(self, event):
        """Dispatch event to all registered callbacks."""
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _log_event(self, event):
        """Append event to ws_events.log."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] {json.dumps(event, separators=(',', ':'))}\n"
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def listen(self, duration=None):
        """Listen for WebSocket frames. Blocks for `duration` seconds (None=forever)."""
        if not self._connected:
            raise RuntimeError("Not connected — call connect() first")

        self._running = True
        deadline = time.time() + duration if duration else None

        while self._running:
            if deadline and time.time() >= deadline:
                break
            try:
                opcode, payload = _recv_frame(self._sock)
            except socket.timeout:
                continue
            except ConnectionError:
                self._connected = False
                break

            if opcode is None:
                break

            # Ping → Pong
            if opcode == 0x9:
                _send_pong(self._sock, payload)
                continue

            # Close
            if opcode == 0x8:
                _send_close(self._sock)
                self._connected = False
                break

            # Text frame
            if opcode == 0x1:
                text = payload.decode("utf-8", errors="replace")
                self.events_received += 1
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = {"raw": text}

                event["_received_at"] = datetime.now().isoformat()
                self._log_event(event)
                self._dispatch(event)

        self._running = False

    def listen_background(self, duration=None):
        """Start listening in a background thread."""
        self._listen_thread = threading.Thread(
            target=self.listen, args=(duration,), daemon=True)
        self._listen_thread.start()

    def close(self):
        """Close the WebSocket connection."""
        self._running = False
        if self._connected and self._sock:
            _send_close(self._sock)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._connected = False
        if self._listen_thread:
            self._listen_thread.join(timeout=3)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet WebSocket Monitor")
    parser.add_argument("--duration", type=int, default=10, help="Listen duration in seconds")
    parser.add_argument("--forever", action="store_true", help="Listen until Ctrl+C")
    args = parser.parse_args()

    # ── Atomic PID guard for long-running mode ──  # signed: alpha
    if args.forever:
        from tools.skynet_pid_guard import acquire_pid_guard
        if not acquire_pid_guard(PID_FILE, "skynet_ws_monitor"):
            print("WS monitor already running -- exiting.")
            sys.exit(0)

    monitor = WSMonitor()

    def print_event(event):
        ts = datetime.now().strftime("%H:%M:%S")
        etype = event.get("type", event.get("topic", "event"))
        sender = event.get("sender", "?")
        content = event.get("content", event.get("text", json.dumps(event, separators=(",", ":"))))
        if isinstance(content, str) and len(content) > 100:
            content = content[:100] + "..."
        print(f"  [{ts}] {etype:12s} [{sender}] {content}")

    monitor.on_event(print_event)

    try:
        print(f"Connecting to ws://{WS_HOST}:{WS_PORT}{WS_PATH}...")
        monitor.connect()
        print(f"Connected. Listening for {'∞' if args.forever else args.duration}s...\n")
        monitor.listen(duration=None if args.forever else args.duration)
    except ConnectionError as e:
        print(f"Connection failed: {e}")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        monitor.close()
        print(f"\nReceived {monitor.events_received} events. Log: {LOG_FILE}")
