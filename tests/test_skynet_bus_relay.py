from datetime import datetime, timedelta

import tools.skynet_bus_relay as relay


def _iso_ago(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_relayable_message_is_queued_for_orchestrator_digest():
    delivered_ids = set()
    queue_state = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {
        "id": "msg_1",
        "sender": "beta",
        "topic": "convene",
        "type": "gate-proposal",
        "content": "important finding",
    }

    count = relay._queue_message(msg, delivered_ids, queue_state, dry_run=False)

    assert count == 1
    assert "msg_1" in delivered_ids
    assert len(queue_state["messages"]) == 1
    assert queue_state["messages"][0]["sender"] == "beta"
    assert queue_state["window_started_at"]


def test_queue_does_not_flush_before_hour():
    queue_state = {
        "messages": [{"id": "msg_1", "sender": "beta", "topic": "convene", "type": "gate-proposal", "content": "x", "queued_at": _iso_ago(300)}],
        "window_started_at": _iso_ago(300),
        "last_digest_at": "",
    }

    count = relay._flush_due_queue(queue_state, dry_run=False)

    assert count == 0
    assert len(queue_state["messages"]) == 1
    assert queue_state["last_digest_at"] == ""


def test_queue_flushes_to_orchestrator_after_hour(monkeypatch):
    queue_state = {
        "messages": [{"id": "msg_1", "sender": "beta", "topic": "convene", "type": "gate-proposal", "content": "x", "queued_at": _iso_ago(3700)}],
        "window_started_at": _iso_ago(3700),
        "last_digest_at": "",
    }
    seen = []

    monkeypatch.setattr(
        relay,
        "_send_digest_to_orchestrator",
        lambda messages, dry_run=False: seen.append((messages, dry_run)) or {"bus_ok": True, "prompt_ok": False, "count": len(messages)},
    )

    count = relay._flush_due_queue(queue_state, dry_run=False)

    assert count == 1
    assert len(seen) == 1
    assert seen[0][0][0]["id"] == "msg_1"
    assert queue_state["messages"] == []
    assert queue_state["window_started_at"] == ""
    assert queue_state["last_digest_at"]
