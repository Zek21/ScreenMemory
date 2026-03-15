"""Unified Skynet bus messaging — single source of truth for all bus operations.

Replaces duplicated _bus_post() implementations across:
  skynet_brain_dispatch.py, skynet_knowledge.py, skynet_brain.py,
  skynet_collective.py, skynet_convene.py, and inline bus posts.

Uses urllib (stdlib) to avoid external dependency. Thread-safe.

Usage:
    from tools.shared.bus import bus_post, bus_post_fields, bus_get

    # Dict-style (knowledge/brain pattern)
    bus_post({"sender": "beta", "topic": "orchestrator", "type": "result", "content": "done"})

    # Field-style (dispatch/collective pattern)
    bus_post_fields("beta", "orchestrator", "result", "task complete")

    # Read messages
    msgs = bus_get(limit=20)
"""
import json
import urllib.error
import urllib.request
from typing import Any, Optional

BUS_URL = "http://localhost:8420"
PUBLISH_ENDPOINT = f"{BUS_URL}/bus/publish"
MESSAGES_ENDPOINT = f"{BUS_URL}/bus/messages"
DEFAULT_TIMEOUT = 5


def _raw_bus_post(message: dict, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """Low-level POST to bus. Only used as fallback when SpamGuard is unavailable."""
    try:
        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            PUBLISH_ENDPOINT,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def bus_post(message: dict, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """POST a message dict to the Skynet bus via SpamGuard.

    Uses guarded_publish() for spam filtering and rate limiting.
    Falls back to raw urllib only if SpamGuard cannot be imported.

    Args:
        message: Dict with sender, topic, type, content fields.
        timeout: HTTP timeout in seconds.

    Returns:
        True on success, False on failure.
    """
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(message)
        return bool(result and result.get("allowed", False))
    except ImportError:
        return _raw_bus_post(message, timeout)
    # signed: gamma


def bus_post_fields(
    sender: str,
    topic: str,
    msg_type: str,
    content: Any,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """POST a message to the bus using individual field arguments.

    Args:
        sender: Who is sending (e.g. "beta", "monitor").
        topic: Message topic (e.g. "orchestrator", "convene", "workers").
        msg_type: Message type (e.g. "result", "alert", "request").
        content: Message content — string or dict (auto-serialized).
        timeout: HTTP timeout in seconds.

    Returns:
        True on success, False on failure.
    """
    if not isinstance(content, str):
        content = json.dumps(content)
    return bus_post(
        {"sender": sender, "topic": topic, "type": msg_type, "content": content},
        timeout=timeout,
    )


def bus_get(
    limit: int = 20,
    topic: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list:
    """GET messages from the Skynet bus.

    Args:
        limit: Max messages to retrieve.
        topic: Optional topic filter.
        timeout: HTTP timeout in seconds.

    Returns:
        List of message dicts, or empty list on failure.
    """
    try:
        url = f"{MESSAGES_ENDPOINT}?limit={limit}"
        if topic:
            url += f"&topic={topic}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


def bus_status(timeout: int = DEFAULT_TIMEOUT) -> dict:
    """GET Skynet status (/status endpoint).

    Returns:
        Status dict with agent info, or empty dict on failure.
    """
    try:
        req = urllib.request.Request(f"{BUS_URL}/status")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}
