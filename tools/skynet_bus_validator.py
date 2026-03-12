#!/usr/bin/env python3
"""
skynet_bus_validator.py -- Message schema validator for the Skynet bus.

Validates bus messages before publish. Checks required fields, topic taxonomy,
type validity per topic, content length limits, and metadata type constraints.

Can be used standalone or integrated with guarded_publish() for pre-publish
validation.

Usage:
    from tools.skynet_bus_validator import validate_message
    errors = validate_message(msg)
    if errors:
        print(f"Invalid message: {errors}")

CLI:
    python tools/skynet_bus_validator.py --validate '{"sender":"alpha","content":"test"}'
    python tools/skynet_bus_validator.py --taxonomy  # Show full topic/type taxonomy
    python tools/skynet_bus_validator.py --test       # Run self-test suite
"""
# signed: gamma

import argparse
import json
import sys
from typing import List, Optional


# ── Topic Taxonomy ────────────────────────────────────────────────
# Authoritative list of known topics and their valid message types.
# Based on Go backend (Skynet/server.go) and documented in
# docs/BUS_COMMUNICATION.md Section 5.

TOPIC_TAXONOMY = {
    "orchestrator": {
        "description": "Worker-to-orchestrator results, alerts, status reports",
        "types": [
            "result",           # Task completion report
            "alert",            # System alert requiring attention
            "identity_ack",     # Identity announcement on boot
            "status",           # Periodic status update
            "urgent",           # ConveneGate bypass -- critical alert
            "report",           # General report
            "message",          # Default message type
            "infra_boot",       # Infrastructure boot announcement
            "directive",        # Orchestrator directive
        ],
    },
    "convene": {
        "description": "Multi-worker consensus proposals and votes",
        "types": [
            "request",              # New convene session request
            "join",                 # Worker joining existing session
            "finding",              # Substantive finding during convene
            "resolve",              # Session resolution by initiator
            "gate-proposal",        # ConveneGate proposal for voting
            "gate-vote",            # Worker vote on pending gate proposal
            "gate-elevated-queued", # Finding elevated and queued for digest
            "message",              # Default
        ],
    },
    "knowledge": {
        "description": "Fact sharing and validation across the network",
        "types": [
            "learning",     # New fact learned (broadcast_learning)
            "validation",   # Vote on existing fact (validate_fact)
            "strategy",     # Evolution strategy sharing
            "incident",     # Incident report
            "message",      # Default
        ],
    },
    "planning": {
        "description": "Consultant proposals and architecture plans",
        "types": [
            "proposal",         # Consultant-originated plan
            "consultant_plan",  # Plan requiring cross-validation
            "review",           # Architecture review
            "message",          # Default
        ],
    },
    "scoring": {
        "description": "Score adjustments (awards, deductions)",
        "types": [
            "award",        # Points awarded to a worker
            "deduction",    # Points deducted from a worker
            "message",      # Default
        ],
    },
    "workers": {
        "description": "Inter-worker requests and sub-delegation",
        "types": [
            "request",      # Help request from a worker
            "sub-task",     # Sub-delegated task
            "response",     # Response to a request
            "message",      # Default
        ],
    },
    "system": {
        "description": "Infrastructure events and boot announcements",
        "types": [
            "infra_boot",   # Infrastructure online announcement
            "shutdown",     # System shutdown notice
            "alert",        # System-level alert
            "health",       # System health report
            "message",      # Default
        ],
    },
    "consultant": {
        "description": "Consultant prompts and responses",
        "types": [
            "prompt",       # Prompt sent to consultant
            "response",     # Consultant response
            "directive",    # Directive for consultant
            "message",      # Default
        ],
    },
    "tasks": {
        "description": "Task queue events (queued, claimed, completed)",
        "types": [
            "queued",       # Task added to queue
            "claimed",      # Task claimed by worker
            "completed",    # Task completed
            "failed",       # Task failed
            "message",      # Default
        ],
    },
    "general": {
        "description": "Default topic for unclassified messages",
        "types": [
            "message",      # Default type
            "info",         # Informational
            "debug",        # Debug message
        ],
    },
}
# signed: gamma

# ── Validation Constants ──────────────────────────────────────────

MAX_CONTENT_LENGTH = 10000      # 10KB max content
MAX_SENDER_LENGTH = 50          # Sender name limit
MAX_TOPIC_LENGTH = 50           # Topic name limit
MAX_TYPE_LENGTH = 50            # Type name limit
MAX_METADATA_KEYS = 20          # Max metadata key-value pairs
MAX_METADATA_KEY_LENGTH = 100   # Max metadata key length
MAX_METADATA_VALUE_LENGTH = 500 # Max metadata value length

KNOWN_SENDERS = {
    "alpha", "beta", "gamma", "delta",
    "orchestrator", "system", "monitor",
    "consultant", "gemini_consultant",
    "convene-gate", "convene", "skynet",
    "idle_monitor", "bus_persist",
}
# signed: gamma

# Valid priority levels for metadata.priority
VALID_PRIORITIES = {"critical", "high", "normal", "low"}


# ── Core Validation ───────────────────────────────────────────────

def validate_message(msg: dict, strict: bool = False) -> List[str]:
    """Validate a bus message against the Skynet schema.

    Args:
        msg: The message dict to validate. Expected keys:
             sender (required), content (required), topic, type, metadata.
        strict: If True, reject unknown topics and types. If False (default),
                warn but allow unknown topics/types (for forward compatibility).

    Returns:
        List of error strings. Empty list = valid message.

    Example:
        errors = validate_message({'sender': 'alpha', 'content': 'hello'})
        if errors:
            for e in errors:
                print(f"  ERROR: {e}")
    """
    errors: List[str] = []

    if not isinstance(msg, dict):
        return ["message must be a dict"]

    # ── Required fields ───────────────────────────────────────
    sender = msg.get("sender")
    content = msg.get("content")

    if not sender:
        errors.append("missing required field: 'sender'")
    elif not isinstance(sender, str):
        errors.append(f"'sender' must be a string, got {type(sender).__name__}")
    elif len(sender) > MAX_SENDER_LENGTH:
        errors.append(f"'sender' exceeds max length ({len(sender)} > {MAX_SENDER_LENGTH})")

    if content is None:
        errors.append("missing required field: 'content'")
    elif not isinstance(content, str):
        errors.append(f"'content' must be a string, got {type(content).__name__}")
    elif len(content) > MAX_CONTENT_LENGTH:
        errors.append(f"'content' exceeds max length ({len(content)} > {MAX_CONTENT_LENGTH})")
    elif len(content) == 0:
        errors.append("'content' must not be empty")

    # ── Topic validation ──────────────────────────────────────
    topic = msg.get("topic", "general")
    if not isinstance(topic, str):
        errors.append(f"'topic' must be a string, got {type(topic).__name__}")
    elif len(topic) > MAX_TOPIC_LENGTH:
        errors.append(f"'topic' exceeds max length ({len(topic)} > {MAX_TOPIC_LENGTH})")
    elif topic not in TOPIC_TAXONOMY:
        if strict:
            errors.append(
                f"unknown topic '{topic}'. "
                f"Valid topics: {', '.join(sorted(TOPIC_TAXONOMY.keys()))}"
            )
        # Non-strict: allow unknown topics for forward compatibility

    # ── Type validation ───────────────────────────────────────
    msg_type = msg.get("type", "message")
    if not isinstance(msg_type, str):
        errors.append(f"'type' must be a string, got {type(msg_type).__name__}")
    elif len(msg_type) > MAX_TYPE_LENGTH:
        errors.append(f"'type' exceeds max length ({len(msg_type)} > {MAX_TYPE_LENGTH})")
    elif isinstance(topic, str) and topic in TOPIC_TAXONOMY:
        valid_types = TOPIC_TAXONOMY[topic]["types"]
        if msg_type not in valid_types:
            if strict:
                errors.append(
                    f"invalid type '{msg_type}' for topic '{topic}'. "
                    f"Valid types: {', '.join(valid_types)}"
                )
            # Non-strict: allow unknown types for forward compatibility

    # ── Metadata validation ───────────────────────────────────
    metadata = msg.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append(f"'metadata' must be a dict, got {type(metadata).__name__}")
        else:
            if len(metadata) > MAX_METADATA_KEYS:
                errors.append(
                    f"'metadata' has too many keys ({len(metadata)} > {MAX_METADATA_KEYS})"
                )
            for k, v in metadata.items():
                if not isinstance(k, str):
                    errors.append(f"metadata key must be string, got {type(k).__name__}")
                elif len(k) > MAX_METADATA_KEY_LENGTH:
                    errors.append(
                        f"metadata key '{k[:30]}...' exceeds max length "
                        f"({len(k)} > {MAX_METADATA_KEY_LENGTH})"
                    )
                if not isinstance(v, str):
                    errors.append(
                        f"metadata value for key '{k}' must be string, "
                        f"got {type(v).__name__}"
                    )
                elif len(v) > MAX_METADATA_VALUE_LENGTH:
                    errors.append(
                        f"metadata value for key '{k}' exceeds max length "
                        f"({len(v)} > {MAX_METADATA_VALUE_LENGTH})"
                    )

            # Validate priority if present
            priority = metadata.get("priority")
            if priority is not None and priority not in VALID_PRIORITIES:
                errors.append(
                    f"invalid metadata.priority '{priority}'. "
                    f"Valid: {', '.join(sorted(VALID_PRIORITIES))}"
                )

    return errors
    # signed: gamma


def validate_or_raise(msg: dict, strict: bool = False):
    """Validate message and raise ValueError if invalid.

    Convenience wrapper around validate_message() for callers that
    prefer exception-based error handling.
    """
    errors = validate_message(msg, strict=strict)
    if errors:
        raise ValueError(f"Invalid bus message: {'; '.join(errors)}")
    # signed: gamma


def get_topic_info(topic: str) -> Optional[dict]:
    """Return taxonomy info for a topic, or None if unknown."""
    return TOPIC_TAXONOMY.get(topic)
    # signed: gamma


def list_topics() -> List[str]:
    """Return sorted list of all known topics."""
    return sorted(TOPIC_TAXONOMY.keys())
    # signed: gamma


def list_types(topic: str) -> List[str]:
    """Return valid types for a given topic, or empty list if unknown."""
    info = TOPIC_TAXONOMY.get(topic)
    return info["types"] if info else []
    # signed: gamma


# ── CLI ───────────────────────────────────────────────────────────

def _print_taxonomy():
    """Print the full topic/type taxonomy."""
    print("=== Skynet Bus Topic Taxonomy ===\n")
    for topic in sorted(TOPIC_TAXONOMY.keys()):
        info = TOPIC_TAXONOMY[topic]
        print(f"  {topic}")
        print(f"    {info['description']}")
        print(f"    Types: {', '.join(info['types'])}")
        print()
    # signed: gamma


def _run_self_test() -> bool:
    """Run validator self-tests."""
    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name}")

    print("=== Bus Validator Self-Test ===\n")

    # Test 1: Valid message passes
    msg1 = {"sender": "alpha", "content": "hello", "topic": "orchestrator",
            "type": "result"}
    check("valid message passes", len(validate_message(msg1)) == 0)

    # Test 2: Missing sender
    msg2 = {"content": "hello"}
    errs2 = validate_message(msg2)
    check("missing sender detected", any("sender" in e for e in errs2))

    # Test 3: Missing content
    msg3 = {"sender": "alpha"}
    errs3 = validate_message(msg3)
    check("missing content detected", any("content" in e for e in errs3))

    # Test 4: Empty content
    msg4 = {"sender": "alpha", "content": ""}
    errs4 = validate_message(msg4)
    check("empty content detected", any("empty" in e for e in errs4))

    # Test 5: Unknown topic in strict mode
    msg5 = {"sender": "alpha", "content": "hi", "topic": "fakeTopic"}
    check("unknown topic passes non-strict",
          len(validate_message(msg5, strict=False)) == 0)
    errs5s = validate_message(msg5, strict=True)
    check("unknown topic fails strict", any("unknown topic" in e for e in errs5s))

    # Test 6: Invalid type for known topic in strict mode
    msg6 = {"sender": "alpha", "content": "hi", "topic": "orchestrator",
            "type": "fakeType"}
    check("invalid type passes non-strict",
          len(validate_message(msg6, strict=False)) == 0)
    errs6s = validate_message(msg6, strict=True)
    check("invalid type fails strict", any("invalid type" in e for e in errs6s))

    # Test 7: Content too long
    msg7 = {"sender": "alpha", "content": "x" * 20000}
    errs7 = validate_message(msg7)
    check("content too long detected", any("max length" in e for e in errs7))

    # Test 8: Non-dict message
    errs8 = validate_message("not a dict")
    check("non-dict message detected", any("dict" in e for e in errs8))

    # Test 9: Metadata validation
    msg9 = {"sender": "alpha", "content": "hi",
            "metadata": {"key": "value"}}
    check("valid metadata passes", len(validate_message(msg9)) == 0)

    # Test 10: Non-string metadata value
    msg10 = {"sender": "alpha", "content": "hi",
             "metadata": {"key": 123}}
    errs10 = validate_message(msg10)
    check("non-string metadata value detected",
          any("metadata value" in e for e in errs10))

    # Test 11: Invalid priority
    msg11 = {"sender": "alpha", "content": "hi",
             "metadata": {"priority": "ultra"}}
    errs11 = validate_message(msg11)
    check("invalid priority detected", any("priority" in e for e in errs11))

    # Test 12: Valid priority
    msg12 = {"sender": "alpha", "content": "hi",
             "metadata": {"priority": "critical"}}
    check("valid priority passes", len(validate_message(msg12)) == 0)

    # Test 13: Default topic/type work
    msg13 = {"sender": "alpha", "content": "hi"}
    check("defaults (general/message) pass", len(validate_message(msg13)) == 0)

    # Test 14: validate_or_raise on valid
    try:
        validate_or_raise({"sender": "alpha", "content": "hi"})
        check("validate_or_raise valid no exception", True)
    except ValueError:
        check("validate_or_raise valid no exception", False)

    # Test 15: validate_or_raise on invalid
    try:
        validate_or_raise({"content": "hi"})
        check("validate_or_raise invalid raises", False)
    except ValueError:
        check("validate_or_raise invalid raises", True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed == 0
    # signed: gamma


def main():
    parser = argparse.ArgumentParser(
        description="Skynet Bus Message Validator")
    parser.add_argument("--validate", type=str, metavar="JSON",
                        help="Validate a JSON message string")
    parser.add_argument("--taxonomy", action="store_true",
                        help="Show full topic/type taxonomy")
    parser.add_argument("--test", action="store_true",
                        help="Run self-test suite")
    parser.add_argument("--strict", action="store_true",
                        help="Use strict mode (reject unknown topics/types)")

    args = parser.parse_args()

    if args.test:
        ok = _run_self_test()
        sys.exit(0 if ok else 1)
    elif args.taxonomy:
        _print_taxonomy()
    elif args.validate:
        try:
            msg = json.loads(args.validate)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            sys.exit(1)
        errors = validate_message(msg, strict=args.strict)
        if errors:
            print(f"INVALID ({len(errors)} errors):")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        else:
            print("VALID")
    else:
        parser.print_help()
    # signed: gamma


if __name__ == "__main__":
    main()
