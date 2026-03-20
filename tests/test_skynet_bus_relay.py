# signed: gamma
"""Comprehensive tests for tools/skynet_bus_relay.py."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import tools.skynet_bus_relay as relay


def _iso_ago(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ── Existing tests (original 3) ──────────────────────────────────────────

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


# ── NEW: Message filtering and topic routing ──────────────────────────────

def test_duplicate_message_id_is_skipped():
    delivered_ids = {"msg_dup"}
    queue_state = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {"id": "msg_dup", "sender": "alpha", "topic": "convene", "type": "request", "content": "x"}

    count = relay._queue_message(msg, delivered_ids, queue_state)
    assert count == 0
    assert len(queue_state["messages"]) == 0


def test_empty_message_id_is_skipped():
    delivered_ids = set()
    queue_state = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {"id": "", "sender": "alpha", "topic": "convene", "type": "request", "content": "x"}

    count = relay._queue_message(msg, delivered_ids, queue_state)
    assert count == 0


def test_non_relay_topic_is_skipped():
    delivered_ids = set()
    queue_state = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {"id": "msg_2", "sender": "alpha", "topic": "orchestrator", "type": "request", "content": "x"}

    count = relay._queue_message(msg, delivered_ids, queue_state)
    assert count == 0
    assert len(queue_state["messages"]) == 0


def test_non_relay_type_is_skipped():
    delivered_ids = set()
    queue_state = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {"id": "msg_3", "sender": "alpha", "topic": "convene", "type": "heartbeat", "content": "x"}

    count = relay._queue_message(msg, delivered_ids, queue_state)
    assert count == 0
    assert "msg_3" in delivered_ids


def test_relay_types_accepted():
    """Each valid relay type should be queued."""
    for rtype in ("request", "proposal", "vote", "task", "sub-task",
                  "urgent", "directive", "gate-proposal", "alert"):
        delivered = set()
        qs = {"messages": [], "window_started_at": "", "last_digest_at": ""}
        msg = {"id": f"msg_{rtype}", "sender": "beta", "topic": "convene",
               "type": rtype, "content": "x"}
        count = relay._queue_message(msg, delivered, qs)
        assert count == 1, f"Type '{rtype}' should be relayed"


def test_relay_topics_include_worker_names():
    """Messages targeted to a specific worker name should be relayable."""
    for worker in ("alpha", "beta", "gamma", "delta"):
        delivered = set()
        qs = {"messages": [], "window_started_at": "", "last_digest_at": ""}
        msg = {"id": f"msg_{worker}", "sender": "orchestrator",
               "topic": worker, "type": "task", "content": "do something"}
        count = relay._queue_message(msg, delivered, qs)
        assert count == 1, f"Topic '{worker}' should be relayable"


# ── NEW: Target determination ─────────────────────────────────────────────

def test_determine_targets_specific_worker():
    msg = {"topic": "alpha", "sender": "beta"}
    targets = relay._determine_targets(msg)
    assert targets == {"alpha"}


def test_determine_targets_excludes_sender():
    msg = {"topic": "alpha", "sender": "alpha"}
    targets = relay._determine_targets(msg)
    assert targets == set()


def test_determine_targets_broadcast_workers():
    msg = {"topic": "workers", "sender": "orchestrator"}
    targets = relay._determine_targets(msg)
    assert targets == {"alpha", "beta", "gamma", "delta"}


def test_determine_targets_convene_excludes_sender():
    msg = {"topic": "convene", "sender": "gamma"}
    targets = relay._determine_targets(msg)
    assert "gamma" not in targets
    assert len(targets) == 3


def test_determine_targets_unknown_topic():
    msg = {"topic": "orchestrator", "sender": "alpha"}
    targets = relay._determine_targets(msg)
    assert targets == set()


# ── NEW: Message formatting ───────────────────────────────────────────────

def test_format_bus_message_plain():
    msg = {"sender": "alpha", "type": "request", "topic": "workers", "content": "help needed"}
    formatted = relay._format_bus_message(msg)
    assert "REQUEST" in formatted
    assert "alpha" in formatted
    assert "help needed" in formatted


def test_format_bus_message_convene_json():
    content = json.dumps({"session": "conv_1", "question": "Should we refactor?", "vote_request": "Vote GO"})
    msg = {"sender": "beta", "type": "gate-proposal", "topic": "convene", "content": content}
    formatted = relay._format_bus_message(msg)
    assert "conv_1" in formatted
    assert "Should we refactor?" in formatted


def test_format_bus_message_invalid_json_content():
    msg = {"sender": "delta", "type": "alert", "topic": "workers",
           "content": "{not valid json"}
    formatted = relay._format_bus_message(msg)
    assert "ALERT" in formatted
    assert "{not valid json" in formatted


# ── NEW: Queue entry creation ─────────────────────────────────────────────

def test_queue_entry_structure():
    msg = {"id": "q1", "sender": "gamma", "topic": "workers", "type": "task",
           "content": "scan files"}
    entry = relay._queue_entry(msg)
    assert entry["id"] == "q1"
    assert entry["sender"] == "gamma"
    assert entry["topic"] == "workers"
    assert entry["type"] == "task"
    assert entry["content"] == "scan files"
    assert "queued_at" in entry


def test_queue_entry_missing_fields_use_defaults():
    entry = relay._queue_entry({})
    assert entry["id"] == ""
    assert entry["sender"] == "unknown"
    assert entry["topic"] == ""


# ── NEW: Digest formatting ────────────────────────────────────────────────

def test_format_digest_structure():
    messages = [
        {"id": "m1", "sender": "alpha", "topic": "workers", "type": "task",
         "content": "fix bug", "queued_at": "2026-01-01T00:00:00"},
        {"id": "m2", "sender": "beta", "topic": "convene", "type": "vote",
         "content": "GO", "queued_at": "2026-01-01T00:01:00"},
    ]
    digest = relay._format_digest(messages)
    assert "BUS RELAY DIGEST" in digest
    assert "count=2" in digest
    assert "m1" in digest
    assert "m2" in digest
    assert "alpha" in digest
    assert "beta" in digest


def test_format_digest_truncates_long_content():
    messages = [{"id": "m1", "sender": "a", "topic": "t", "type": "x",
                 "content": "A" * 1000, "queued_at": "2026-01-01T00:00:00"}]
    digest = relay._format_digest(messages)
    assert len(digest) <= 4000


def test_format_digest_empty_messages():
    digest = relay._format_digest([])
    assert "count=0" in digest


# ── NEW: Delivered ID persistence ─────────────────────────────────────────

def test_save_and_load_delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "DELIVERED_FILE", tmp_path / "delivered.json")
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)

    ids = {"id_1", "id_2", "id_3"}
    relay._save_delivered(ids)
    loaded = relay._load_delivered()
    assert loaded == ids


def test_save_delivered_trims_to_max(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "DELIVERED_FILE", tmp_path / "delivered.json")
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)

    ids = {f"id_{i}" for i in range(600)}
    relay._save_delivered(ids)
    loaded = relay._load_delivered()
    assert len(loaded) == relay.MAX_DELIVERED_IDS


def test_load_delivered_handles_corrupt_file(tmp_path, monkeypatch):
    corrupt_file = tmp_path / "delivered.json"
    corrupt_file.write_text("NOT JSON!!!")
    monkeypatch.setattr(relay, "DELIVERED_FILE", corrupt_file)

    result = relay._load_delivered()
    assert result == set()


def test_load_delivered_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "DELIVERED_FILE", tmp_path / "nonexistent.json")
    result = relay._load_delivered()
    assert result == set()


# ── NEW: Queue state persistence ──────────────────────────────────────────

def test_save_and_load_queue_state(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)

    state = {"messages": [{"id": "q1"}], "window_started_at": "2026-01-01", "last_digest_at": ""}
    relay._save_queue_state(state)
    loaded = relay._load_queue_state()
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0]["id"] == "q1"


def test_save_queue_state_trims_excess_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)

    state = {"messages": [{"id": f"m{i}"} for i in range(600)],
             "window_started_at": "", "last_digest_at": ""}
    relay._save_queue_state(state)
    loaded = relay._load_queue_state()
    assert len(loaded["messages"]) == relay.MAX_QUEUED_MESSAGES


def test_load_queue_state_handles_corrupt_file(tmp_path, monkeypatch):
    corrupt = tmp_path / "queue.json"
    corrupt.write_text("{bad json")
    monkeypatch.setattr(relay, "QUEUE_FILE", corrupt)

    state = relay._load_queue_state()
    assert isinstance(state["messages"], list)
    assert state["messages"] == []


def test_load_queue_state_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "QUEUE_FILE", tmp_path / "nonexistent.json")
    state = relay._load_queue_state()
    assert state["messages"] == []
    assert state["window_started_at"] == ""


# ── NEW: Fetch messages / network errors ──────────────────────────────────

def test_fetch_messages_connection_error(monkeypatch):
    old_val = relay._consecutive_fetch_failures
    monkeypatch.setattr(relay, "_consecutive_fetch_failures", 0)

    def mock_urlopen(*a, **kw):
        raise ConnectionError("connection refused")
    monkeypatch.setattr(relay.urllib.request, "urlopen", mock_urlopen)

    result = relay._fetch_messages()
    assert result == []
    assert relay._consecutive_fetch_failures == 1

    relay._consecutive_fetch_failures = old_val


def test_fetch_messages_json_error(monkeypatch):
    old_val = relay._consecutive_fetch_failures
    monkeypatch.setattr(relay, "_consecutive_fetch_failures", 0)

    class FakeResp:
        def read(self): return b"NOT JSON"
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(relay.urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    result = relay._fetch_messages()
    assert result == []

    relay._consecutive_fetch_failures = old_val


# ── NEW: Dry-run mode ─────────────────────────────────────────────────────

def test_queue_message_dry_run():
    delivered = set()
    qs = {"messages": [], "window_started_at": "", "last_digest_at": ""}
    msg = {"id": "dry1", "sender": "beta", "topic": "convene",
           "type": "proposal", "content": "test"}

    count = relay._queue_message(msg, delivered, qs, dry_run=True)
    assert count == 1
    assert "dry1" in delivered
    assert len(qs["messages"]) == 0  # dry-run doesn't actually queue


# ── NEW: Daemon backoff and constants ─────────────────────────────────────

def test_poll_interval_minimum():
    assert relay.POLL_INTERVAL >= relay.MIN_POLL_INTERVAL


def test_hold_interval_is_one_hour():
    assert relay.HOLD_INTERVAL_S == 3600


def test_worker_names_complete():
    assert relay.WORKER_NAMES == {"alpha", "beta", "gamma", "delta"}


# ── NEW: Bus post / digest delivery ──────────────────────────────────────

def test_send_digest_dry_run():
    messages = [{"id": "m1", "sender": "a", "content": "x"}]
    result = relay._send_digest_to_orchestrator(messages, dry_run=True)
    assert result["bus_ok"] is True
    assert result["count"] == 1


def test_send_digest_live(monkeypatch):
    posted = []
    monkeypatch.setattr(relay, "_bus_post",
                        lambda s, t, mt, c: posted.append((s, t, mt)) or True)

    messages = [{"id": "m1", "sender": "a", "content": "x"}]
    result = relay._send_digest_to_orchestrator(messages, dry_run=False)
    assert result["bus_ok"] is True
    assert len(posted) == 1
    assert posted[0] == ("bus_relay", "orchestrator", "bus_relay_digest")


# ── NEW: Full poll_and_relay cycle ────────────────────────────────────────

def test_poll_and_relay_integration(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "DELIVERED_FILE", tmp_path / "delivered.json")
    monkeypatch.setattr(relay, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)

    messages = [
        {"id": "int1", "sender": "alpha", "topic": "convene",
         "type": "proposal", "content": "let's refactor"},
        {"id": "int2", "sender": "beta", "topic": "orchestrator",
         "type": "result", "content": "done"},  # non-relay topic
    ]
    monkeypatch.setattr(relay, "_fetch_messages", lambda limit=50: messages)

    n = relay.poll_and_relay(dry_run=False)
    assert n >= 1  # at least the convene message was queued

    # Verify queue state persisted
    qs = relay._load_queue_state()
    assert any(m["id"] == "int1" for m in qs["messages"])


# ── NEW: Signal handler ──────────────────────────────────────────────────

def test_handle_signal_sets_shutdown_flag(monkeypatch):
    import signal
    monkeypatch.setattr(relay, "_shutting_down", False)
    relay._handle_signal(signal.SIGTERM, None)
    assert relay._shutting_down is True

    # Reset
    relay._shutting_down = False


# ── NEW: Read brain config ───────────────────────────────────────────────

def test_read_brain_config_flag_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)
    result = relay._read_brain_config_flag("some.key", default=True)
    assert result is True


def test_read_brain_config_flag_existing(tmp_path, monkeypatch):
    cfg_file = tmp_path / "brain_config.json"
    cfg_file.write_text(json.dumps({"test_flag": True}))
    monkeypatch.setattr(relay, "DATA_DIR", tmp_path)
    result = relay._read_brain_config_flag("test_flag", default=False)
    assert result is True
