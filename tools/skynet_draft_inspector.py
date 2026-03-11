#!/usr/bin/env python3
"""Inspect workers for hanging unsent drafts and classify likely source.

Usage:
    python tools/skynet_draft_inspector.py scan
    python tools/skynet_draft_inspector.py scan --all
    python tools/skynet_draft_inspector.py scan --worker beta
    python tools/skynet_draft_inspector.py scan --json
    python tools/skynet_draft_inspector.py watch --interval 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
MONITOR_PID_FILE = DATA_DIR / "monitor.pid"
WORKER_HEALTH_FILE = DATA_DIR / "worker_health.json"
SKYNET_URL = "http://localhost:8420"
DEFAULT_BUS_LIMIT = 80

GATE_ID_RE = re.compile(r"gate_\d+_[a-z]+", re.IGNORECASE)
TOPIC_KV_RE = re.compile(r"topic=([a-z_]+)", re.IGNORECASE)
TYPE_KV_RE = re.compile(r"\[BUS RELAY\]\s+([A-Z_-]+)\s+from", re.IGNORECASE)
JSON_FIELD_RE = re.compile(r'"([a-z_]+)"\s*:\s*"([^"]+)"')


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_workers() -> list[dict]:
    data = _read_json(WORKERS_FILE, {})
    workers = data.get("workers", []) if isinstance(data, dict) else []
    return [w for w in workers if isinstance(w, dict)]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ts_to_age_s(value: str) -> float | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return max(0.0, time.time() - ts)
    except Exception:
        return None


def _monitor_status() -> dict:
    pid = 0
    if MONITOR_PID_FILE.exists():
        try:
            pid = int(MONITOR_PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0

    health = _read_json(WORKER_HEALTH_FILE, {})
    updated = str(health.get("updated") or "")
    health_age_s = _ts_to_age_s(updated)
    return {
        "running": _pid_alive(pid),
        "pid": pid,
        "health_updated": updated,
        "health_age_s": None if health_age_s is None else round(health_age_s, 1),
        "health_stale": health_age_s is None or health_age_s > 90,
    }


def _fetch_bus_messages(limit: int = DEFAULT_BUS_LIMIT) -> list[dict]:
    url = f"{SKYNET_URL}/bus/messages?" + urllib.parse.urlencode({"limit": limit})
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read())
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _sanitize_text(text: str, limit: int = 220) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit]


def _extract_signals(text: str) -> dict:
    raw = str(text or "")
    lower = raw.lower()
    gate_match = GATE_ID_RE.search(raw)
    topic_match = TOPIC_KV_RE.search(raw)
    relay_type_match = TYPE_KV_RE.search(raw)
    json_pairs = {k.lower(): v for k, v in JSON_FIELD_RE.findall(raw)}
    topic = topic_match.group(1).lower() if topic_match else json_pairs.get("topic", "").lower()
    sender = json_pairs.get("sender", "")
    report = json_pairs.get("report", "")
    proposer = json_pairs.get("proposer", "")
    return {
        "is_bus_relay": "[bus relay]" in lower,
        "has_reply_template": "reply via bus:" in lower,
        "is_convene_related": "topic=convene" in lower or topic == "convene" or "gate-proposal" in lower,
        "gate_id": gate_match.group(0) if gate_match else "",
        "topic": topic,
        "relay_type": relay_type_match.group(1).lower() if relay_type_match else "",
        "sender": sender,
        "proposer": proposer,
        "report_preview": _sanitize_text(report, 160),
    }


def classify_draft(text: str) -> str:
    signals = _extract_signals(text)
    lower = str(text or "").lower()
    if not lower.strip():
        return "empty"
    if signals["is_bus_relay"] and signals["is_convene_related"] and signals["gate_id"]:
        return "bus_relay_convene_gate_proposal"
    if signals["is_bus_relay"] and signals["is_convene_related"]:
        return "bus_relay_convene"
    if signals["is_bus_relay"] and signals["has_reply_template"]:
        return "bus_relay_reply_template"
    if signals["is_bus_relay"]:
        return "bus_relay"
    if "[convene" in lower or "gate-proposal" in lower:
        return "convene_related"
    return "unclassified_draft"


def _correlate_with_bus(text: str, messages: list[dict]) -> dict | None:
    signals = _extract_signals(text)
    gate_id = signals["gate_id"]
    report_preview = signals["report_preview"]
    topic = signals["topic"]
    sender = signals["sender"] or signals["proposer"]

    for msg in messages:
        content = str(msg.get("content") or "")
        if gate_id and gate_id in content:
            return {
                "match": "gate_id",
                "id": msg.get("id", ""),
                "sender": msg.get("sender", ""),
                "topic": msg.get("topic", ""),
                "type": msg.get("type", ""),
                "timestamp": msg.get("timestamp", ""),
            }

    for msg in messages:
        content = _sanitize_text(msg.get("content") or "", 180)
        if report_preview and report_preview[:80] and report_preview[:80] in content:
            return {
                "match": "report_preview",
                "id": msg.get("id", ""),
                "sender": msg.get("sender", ""),
                "topic": msg.get("topic", ""),
                "type": msg.get("type", ""),
                "timestamp": msg.get("timestamp", ""),
            }

    for msg in messages:
        if sender and str(msg.get("sender") or "").lower() != sender.lower():
            continue
        if topic and str(msg.get("topic") or "").lower() != topic.lower():
            continue
        return {
            "match": "sender_topic",
            "id": msg.get("id", ""),
            "sender": msg.get("sender", ""),
            "topic": msg.get("topic", ""),
            "type": msg.get("type", ""),
            "timestamp": msg.get("timestamp", ""),
        }
    return None


def _build_entry(worker: dict, scan, messages: list[dict]) -> dict:
    edit_value = str(getattr(scan, "edit_value", "") or "")
    classification = classify_draft(edit_value)
    correlation = _correlate_with_bus(edit_value, messages) if edit_value.strip() else None
    signals = _extract_signals(edit_value)
    return {
        "name": worker.get("name", ""),
        "grid": worker.get("grid", ""),
        "hwnd": int(worker.get("hwnd") or 0),
        "state": str(getattr(scan, "state", "UNKNOWN") or "UNKNOWN"),
        "scan_ms": round(float(getattr(scan, "scan_ms", 0.0) or 0.0), 1),
        "model": str(getattr(scan, "model", "") or ""),
        "agent": str(getattr(scan, "agent", "") or ""),
        "draft_chars": len(edit_value),
        "draft_preview": _sanitize_text(edit_value, 260),
        "classification": classification,
        "signals": {
            "topic": signals["topic"],
            "relay_type": signals["relay_type"],
            "sender": signals["sender"],
            "gate_id": signals["gate_id"],
            "has_reply_template": signals["has_reply_template"],
        },
        "bus_correlation": correlation,
    }


def inspect_workers(worker_name: str = "", include_all: bool = False, bus_limit: int = DEFAULT_BUS_LIMIT) -> dict:
    from tools.uia_engine import get_engine

    workers = _load_workers()
    if worker_name:
        workers = [w for w in workers if str(w.get("name") or "").lower() == worker_name.lower()]

    hwnds = {
        str(w.get("name") or ""): int(w.get("hwnd") or 0)
        for w in workers
        if int(w.get("hwnd") or 0) > 0
    }
    scans = get_engine().scan_all(hwnds, max_workers=min(max(len(hwnds), 1), 4)) if hwnds else {}
    messages = _fetch_bus_messages(limit=bus_limit)

    entries = []
    for worker in workers:
        name = str(worker.get("name") or "")
        scan = scans.get(name)
        if scan is None:
            continue
        entry = _build_entry(worker, scan, messages)
        if include_all or entry["state"] == "TYPING":
            entries.append(entry)

    entries.sort(key=lambda item: (item["state"] != "TYPING", item["name"]))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "monitor": _monitor_status(),
        "typing_count": sum(1 for entry in entries if entry["state"] == "TYPING"),
        "workers": entries,
    }


def _print_report(report: dict) -> int:
    monitor = report.get("monitor", {})
    status = "RUNNING" if monitor.get("running") else "STOPPED"
    age = monitor.get("health_age_s")
    age_text = "unknown" if age is None else f"{age:.1f}s"
    print(f"monitor={status} pid={monitor.get('pid', 0)} health_age={age_text}")

    workers = report.get("workers", [])
    if not workers:
        print("No matching worker drafts found.")
        return 0

    for entry in workers:
        print(f"\n{entry['name']} [{entry['grid']}] hwnd={entry['hwnd']} state={entry['state']} scan={entry['scan_ms']}ms")
        print(f"classification={entry['classification']} chars={entry['draft_chars']}")
        if entry["signals"].get("gate_id"):
            print(f"gate_id={entry['signals']['gate_id']}")
        if entry["signals"].get("topic"):
            print(f"topic={entry['signals']['topic']} relay_type={entry['signals'].get('relay_type', '')}")
        if entry["bus_correlation"]:
            c = entry["bus_correlation"]
            print(
                f"bus_match={c.get('match')} id={c.get('id')} sender={c.get('sender')} "
                f"topic={c.get('topic')} type={c.get('type')}"
            )
        print(f"draft={entry['draft_preview']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find and dissect hanging worker drafts.")
    sub = parser.add_subparsers(dest="cmd")

    scan_p = sub.add_parser("scan", help="Scan workers for unsent drafts")
    scan_p.add_argument("--all", action="store_true", help="Show all scanned workers, not just TYPING")
    scan_p.add_argument("--worker", default="", help="Only inspect one worker")
    scan_p.add_argument("--json", action="store_true", help="Emit JSON")
    scan_p.add_argument("--bus-limit", type=int, default=DEFAULT_BUS_LIMIT, help="Recent bus messages to inspect")

    watch_p = sub.add_parser("watch", help="Continuously rescan")
    watch_p.add_argument("--interval", type=float, default=5.0, help="Seconds between scans")
    watch_p.add_argument("--all", action="store_true", help="Show all scanned workers, not just TYPING")
    watch_p.add_argument("--worker", default="", help="Only inspect one worker")
    watch_p.add_argument("--json", action="store_true", help="Emit JSON")
    watch_p.add_argument("--bus-limit", type=int, default=DEFAULT_BUS_LIMIT, help="Recent bus messages to inspect")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1

    if args.cmd == "scan":
        report = inspect_workers(worker_name=args.worker, include_all=args.all, bus_limit=args.bus_limit)
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        return _print_report(report)

    if args.cmd == "watch":
        while True:
            report = inspect_workers(worker_name=args.worker, include_all=args.all, bus_limit=args.bus_limit)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print("\x1bc", end="")
                _print_report(report)
            time.sleep(max(0.5, float(args.interval)))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
