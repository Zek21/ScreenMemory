#!/usr/bin/env python3
"""
skynet_consultant_protocol.py -- queue consultant plans and require worker cross-validation.

This is the durable operating protocol for consultant-originated plans:
1. Queue the plan into a consultant bridge.
2. Publish the plan packet to the Skynet bus.
3. Dispatch independent worker reviews before execution proceeds.
4. Persist the activation record for later audit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNS_DIR = DATA / "consultant_protocol_runs"
BRAIN_CONFIG = DATA / "brain_config.json"

sys.path.insert(0, str(ROOT))

DEFAULT_PROTOCOL = {
    "enabled": True,
    "queue_plans_to_consultants": True,
    "require_task_claim": True,
    "publish_plan_to_bus": True,
    "plan_topic": "planning",
    "plan_type": "consultant_plan",
    "cross_validation_required": True,
    "min_worker_reviewers": 3,
    "review_worker_pool": ["alpha", "beta", "gamma", "delta"],
    "prefer_available_workers": True,
    "require_distinct_reviewers": True,
    "require_worker_verdicts_before_execution": True,
    "review_topic": "planning",
    "review_type": "consultant_plan_review",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_defaults(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_protocol_config() -> dict:
    config = _read_json(BRAIN_CONFIG)
    return _merge_defaults(DEFAULT_PROTOCOL, config.get("consultant_protocol", {}))


def load_plan_text(plan_file: Path) -> str:
    return plan_file.read_text(encoding="utf-8")


def build_plan_packet(title: str, consultant_id: str, plan_file: Path, protocol: dict) -> dict:
    plan_text = load_plan_text(plan_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    packet_id = f"consultant-plan-{consultant_id}-{timestamp}"
    summary = ""
    for line in plan_text.splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            summary = text[:220]
            break
    if not summary:
        summary = f"Consultant protocol packet from {consultant_id}"
    return {
        "id": packet_id,
        "title": title,
        "consultant_id": consultant_id,
        "created_at": _now_iso(),
        "artifact_path": str(plan_file.relative_to(ROOT)),
        "summary": summary,
        "protocol": protocol,
        "plan_text": plan_text,
    }


def _load_worker_snapshot() -> dict:
    try:
        from tools.skynet_consultant_bridge import _load_worker_snapshot as bridge_worker_snapshot
        return bridge_worker_snapshot()
    except Exception:
        return {"workers": {}, "available_workers": [], "summary": {"total": 0, "available": 0, "busy": 0, "offline": 0}}


def select_reviewers(protocol: dict, requested: List[str] | None = None) -> List[str]:
    requested = [w.strip().lower() for w in (requested or []) if w and w.strip()]
    if requested:
        return requested

    snapshot = _load_worker_snapshot()
    pool = [str(w).lower() for w in protocol.get("review_worker_pool", [])]
    available = [str(w).lower() for w in snapshot.get("available_workers", [])]

    reviewers: List[str] = []
    if protocol.get("prefer_available_workers", True):
        for worker in available:
            if worker in pool and worker not in reviewers:
                reviewers.append(worker)

    for worker in pool:
        if worker not in reviewers:
            reviewers.append(worker)

    min_reviewers = max(1, int(protocol.get("min_worker_reviewers", 2)))
    return reviewers[:min_reviewers]


def build_consultant_prompt(packet: dict) -> str:
    return (
        f"CONSULTANT PROTOCOL PACKET {packet['id']}\n"
        f"Title: {packet['title']}\n"
        f"Artifact: {packet['artifact_path']}\n"
        f"Summary: {packet['summary']}\n\n"
        "This plan must be independently cross-validated by workers before execution.\n"
        "If a real consultant consumer is attached, review the artifact and either endorse, revise, "
        "or reject it. Do not assume execution approval without worker verdicts."
    )


def publish_plan_packet(packet: dict, protocol: dict) -> bool:
    from tools.shared.bus import bus_post

    metadata = {
        "plan_id": packet["id"],
        "consultant_id": packet["consultant_id"],
        "artifact_path": packet["artifact_path"],
        "min_reviewers": str(protocol.get("min_worker_reviewers", 0)),
        "cross_validation_required": str(bool(protocol.get("cross_validation_required", True))),
    }
    return bus_post({
        "sender": "consultant_protocol",
        "topic": protocol.get("plan_topic", "planning"),
        "type": protocol.get("plan_type", "consultant_plan"),
        "content": (
            f"{packet['title']} [{packet['id']}]\n"
            f"Artifact: {packet['artifact_path']}\n"
            f"Summary: {packet['summary']}"
        ),
        "metadata": metadata,
    })


def queue_plan_to_consultant(packet: dict, protocol: dict) -> dict:
    from tools.skynet_delivery import deliver_to_consultant

    if not protocol.get("queue_plans_to_consultants", True):
        return {"success": False, "skipped": True, "detail": "queue_plans_to_consultants disabled"}
    prompt = build_consultant_prompt(packet)
    result = deliver_to_consultant(
        packet["consultant_id"],
        prompt,
        sender="consultant_protocol",
        msg_type=protocol.get("plan_type", "consultant_plan"),
    )
    if result.get("success"):
        return result
    fallback = _queue_plan_via_bridge_http(
        packet["consultant_id"],
        prompt,
        protocol.get("plan_type", "consultant_plan"),
    )
    if fallback.get("success"):
        return fallback
    return result


def _consultant_ports(consultant_id: str) -> List[int]:
    if consultant_id == "gemini_consultant":
        return [8425]
    return [8422, 8424]


def _queue_plan_via_bridge_http(consultant_id: str, prompt: str, prompt_type: str) -> dict:
    payload = json.dumps({
        "sender": "consultant_protocol",
        "type": prompt_type,
        "content": prompt,
        "metadata": {"fallback": "http_retry"},
    }).encode("utf-8")
    last_error = ""
    for attempt in range(5):
        for port in _consultant_ports(consultant_id):
            try:
                req = urllib.request.Request(
                    f"http://localhost:{port}/consultants/prompt",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                if isinstance(data, dict) and data.get("status") == "queued":
                    prompt_info = data.get("prompt", {}) if isinstance(data.get("prompt"), dict) else {}
                    return {
                        "target": f"consultant:{consultant_id}",
                        "method": "bridge_http_retry",
                        "success": False,  # queued != delivered -- TRUTH PRINCIPLE (INCIDENT 011) # signed: gamma
                        "delivery_status": "queued",
                        "detail": f"prompt_id={prompt_info.get('id', 'unknown')}, port={port}",
                    }
            except Exception as exc:
                last_error = str(exc)
        time.sleep(0.5 * (attempt + 1))
    return {
        "target": f"consultant:{consultant_id}",
        "method": "bridge_http_retry",
        "success": False,
        "detail": last_error or "bridge retry failed",
    }


def build_review_task(packet: dict, worker_name: str) -> str:
    return (
        f"CROSS-VALIDATION TASK {packet['id']}\n"
        f"Reviewer: {worker_name}\n"
        f"Consultant target: {packet['consultant_id']}\n"
        f"Read: {packet['artifact_path']}\n\n"
        "Perform an independent review. Challenge assumptions. Do not rubber-stamp.\n"
        "Reply on the bus with type=result and include the packet id in the first line.\n"
        "Required sections:\n"
        f"1. PACKET: {packet['id']}\n"
        "2. VERDICT: approve / revise / reject\n"
        "3. RISKS: top protocol gaps or execution risks\n"
        "4. CHANGES: exact improvements required before execution\n"
        "5. GO_NO_GO: whether Skynet should execute this plan now\n"
    )


def dispatch_cross_validation(packet: dict, reviewers: List[str]) -> List[dict]:
    from tools.skynet_dispatch import dispatch_to_worker, load_orch_hwnd, load_workers

    workers = load_workers()
    orch_hwnd = load_orch_hwnd()
    results = []
    for reviewer in reviewers:
        task = build_review_task(packet, reviewer)
        ok = dispatch_to_worker(reviewer, task, workers=workers, orch_hwnd=orch_hwnd)
        results.append({"worker": reviewer, "success": bool(ok)})
    return results


def persist_run(record: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{record['packet']['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def activate_protocol(consultant_id: str, title: str, plan_file: Path,
                      requested_reviewers: List[str] | None = None) -> dict:
    protocol = load_protocol_config()
    packet = build_plan_packet(title, consultant_id, plan_file, protocol)
    queue_result = queue_plan_to_consultant(packet, protocol)
    publish_ok = publish_plan_packet(packet, protocol) if protocol.get("publish_plan_to_bus", True) else False
    reviewers = select_reviewers(protocol, requested_reviewers)
    dispatch_results = dispatch_cross_validation(packet, reviewers) if protocol.get("cross_validation_required", True) else []
    record = {
        "activated_at": _now_iso(),
        "packet": {
            "id": packet["id"],
            "title": packet["title"],
            "consultant_id": packet["consultant_id"],
            "artifact_path": packet["artifact_path"],
            "summary": packet["summary"],
        },
        "protocol": protocol,
        "queue_result": queue_result,
        "plan_published": publish_ok,
        "reviewers": reviewers,
        "dispatch_results": dispatch_results,
    }
    record["run_file"] = str(persist_run(record).relative_to(ROOT))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Consultant plan queue + mandatory worker cross-validation")
    sub = parser.add_subparsers(dest="command")

    p_activate = sub.add_parser("activate", help="Queue a consultant plan and dispatch worker cross-validation")
    p_activate.add_argument("--consultant", required=True, help="Consultant id (consultant or gemini_consultant)")
    p_activate.add_argument("--title", required=True, help="Human-readable plan title")
    p_activate.add_argument("--plan-file", required=True, help="Markdown file containing the plan")
    p_activate.add_argument("--reviewers", help="Comma-separated worker reviewers; default from protocol config")

    p_show = sub.add_parser("show-config", help="Print consultant protocol config")

    args = parser.parse_args()

    if args.command == "show-config":
        print(json.dumps(load_protocol_config(), indent=2))
        return 0

    if args.command == "activate":
        reviewers = args.reviewers.split(",") if args.reviewers else None
        result = activate_protocol(
            consultant_id=args.consultant.strip(),
            title=args.title.strip(),
            plan_file=Path(args.plan_file).resolve(),
            requested_reviewers=reviewers,
        )
        print(json.dumps(result, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
