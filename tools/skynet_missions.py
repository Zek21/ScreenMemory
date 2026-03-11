#!/usr/bin/env python3
"""Mission tracking for Skynet multi-agent operations.

A Mission represents a high-level objective that may span multiple waves,
workers, and dispatches. MissionControl manages the lifecycle of missions
and persists them to data/missions.json.

Mission States:
  - planned: mission defined but not started
  - active: at least one task dispatched
  - paused: temporarily suspended (manual or auto)
  - completed: all objectives met
  - failed: mission abandoned or unrecoverable
  - cancelled: manually cancelled before completion

CLI:
  python tools/skynet_missions.py list [--status active]
  python tools/skynet_missions.py create --title "..." [--owner orchestrator] [--priority 1]
  python tools/skynet_missions.py status MISSION_ID
  python tools/skynet_missions.py timeline MISSION_ID
  python tools/skynet_missions.py update MISSION_ID --status completed
  python tools/skynet_missions.py add-event MISSION_ID --event "description"
"""

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MISSIONS_FILE = DATA_DIR / "missions.json"

_lock = threading.Lock()


class MissionStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Mission:
    """A single mission with subtasks, dependencies, timeline events and metadata."""

    def __init__(
        self,
        title: str,
        mission_id: Optional[str] = None,
        status: str = MissionStatus.PLANNED.value,
        owner: str = "orchestrator",
        priority: int = 2,
        description: str = "",
        tags: Optional[list] = None,
        workers: Optional[list] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        timeline: Optional[list] = None,
        metadata: Optional[dict] = None,
        subtasks: Optional[list] = None,
        dependencies: Optional[list] = None,
        results: Optional[dict] = None,
    ):
        self.mission_id = mission_id or f"mission-{uuid4().hex[:8]}"
        self.title = title
        self.status = status
        self.owner = owner
        self.priority = priority
        self.description = description
        self.tags = tags or []
        self.workers = workers or []
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = created_at or now
        self.updated_at = updated_at or now
        self.completed_at = completed_at
        self.timeline = timeline or [
            {"event": "Mission created", "timestamp": self.created_at, "actor": owner}
        ]
        self.metadata = metadata or {}
        self.subtasks = subtasks or []
        self.dependencies = dependencies or []
        self.results = results or {}

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "title": self.title,
            "status": self.status,
            "owner": self.owner,
            "priority": self.priority,
            "description": self.description,
            "tags": self.tags,
            "workers": self.workers,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "timeline": self.timeline,
            "metadata": self.metadata,
            "subtasks": self.subtasks,
            "dependencies": self.dependencies,
            "results": self.results,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Mission":
        return cls(
            title=data.get("title", "Untitled"),
            mission_id=data.get("mission_id"),
            status=data.get("status", MissionStatus.PLANNED.value),
            owner=data.get("owner", "orchestrator"),
            priority=data.get("priority", 2),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            workers=data.get("workers", []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            completed_at=data.get("completed_at"),
            timeline=data.get("timeline", []),
            metadata=data.get("metadata", {}),
            subtasks=data.get("subtasks", []),
            dependencies=data.get("dependencies", []),
            results=data.get("results", {}),
        )

    def add_event(self, event: str, actor: str = "system") -> dict:
        entry = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
        }
        self.timeline.append(entry)
        self.updated_at = entry["timestamp"]
        return entry

    def set_status(self, new_status: str, actor: str = "system") -> None:
        old = self.status
        self.status = MissionStatus(new_status).value
        self.add_event(f"Status: {old} -> {self.status}", actor=actor)

    def assign_worker(self, worker: str) -> None:
        if worker not in self.workers:
            self.workers.append(worker)
            self.add_event(f"Worker assigned: {worker}", actor="orchestrator")

    def duration_s(self) -> float:
        try:
            start = datetime.fromisoformat(self.created_at)
            end = datetime.fromisoformat(self.updated_at)
            return (end - start).total_seconds()
        except (ValueError, TypeError):
            return 0.0

    def is_terminal(self) -> bool:
        return self.status in (
            MissionStatus.COMPLETED.value,
            MissionStatus.FAILED.value,
            MissionStatus.CANCELLED.value,
        )

    def all_subtasks_done(self) -> bool:
        """Return True if every subtask is completed or failed."""
        if not self.subtasks:
            return False
        return all(s.get("status") in ("completed", "failed") for s in self.subtasks)

    def subtask_progress(self) -> dict:
        """Return subtask completion stats."""
        total = len(self.subtasks)
        if total == 0:
            return {"total": 0, "completed": 0, "pending": 0, "pct": 0.0}
        done = sum(1 for s in self.subtasks if s.get("status") == "completed")
        return {
            "total": total,
            "completed": done,
            "pending": total - done,
            "pct": round(done / total * 100, 1),
        }


class MissionControl:
    """Manages mission lifecycle with JSON persistence."""

    def __init__(self, missions_file: Optional[Path] = None):
        self.missions_file = missions_file or MISSIONS_FILE
        self._missions: dict[str, Mission] = {}
        self._load()

    def _load(self) -> None:
        if self.missions_file.exists():
            try:
                data = json.loads(self.missions_file.read_text(encoding="utf-8"))
                missions_list = data.get("missions", []) if isinstance(data, dict) else data
                for m in missions_list:
                    mission = Mission.from_dict(m)
                    self._missions[mission.mission_id] = mission
            except (json.JSONDecodeError, OSError):
                self._missions = {}

    def _save(self) -> None:
        with _lock:
            self.missions_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "missions": [m.to_dict() for m in self._missions.values()],
                "count": len(self._missions),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.missions_file.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def create(
        self,
        title: str,
        owner: str = "orchestrator",
        priority: int = 2,
        description: str = "",
        tags: Optional[list] = None,
    ) -> Mission:
        mission = Mission(
            title=title, owner=owner, priority=priority,
            description=description, tags=tags,
        )
        self._missions[mission.mission_id] = mission
        self._save()
        return mission

    def get(self, mission_id: str) -> Optional[Mission]:
        return self._missions.get(mission_id)

    def list_missions(
        self,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        limit: int = 50,
    ) -> list[Mission]:
        result = []
        for m in sorted(
            self._missions.values(),
            key=lambda x: x.priority,
        ):
            if status and m.status != status:
                continue
            if owner and m.owner != owner:
                continue
            result.append(m)
            if len(result) >= limit:
                break
        return result

    def active_missions(self) -> list[Mission]:
        return self.list_missions(status=MissionStatus.ACTIVE.value)

    def update_status(
        self, mission_id: str, new_status: str, actor: str = "system"
    ) -> Optional[Mission]:
        m = self.get(mission_id)
        if not m:
            return None
        m.set_status(new_status, actor=actor)
        self._save()
        return m

    def add_event(
        self, mission_id: str, event: str, actor: str = "system"
    ) -> Optional[dict]:
        m = self.get(mission_id)
        if not m:
            return None
        entry = m.add_event(event, actor=actor)
        self._save()
        return entry

    def assign_worker(self, mission_id: str, worker: str) -> Optional[Mission]:
        m = self.get(mission_id)
        if not m:
            return None
        m.assign_worker(worker)
        self._save()
        return m

    def get_timeline(self, mission_id: str) -> Optional[list]:
        m = self.get(mission_id)
        return m.timeline if m else None

    def stats(self) -> dict:
        by_status = {}
        for m in self._missions.values():
            by_status[m.status] = by_status.get(m.status, 0) + 1
        return {
            "total": len(self._missions),
            "by_status": by_status,
            "active_count": by_status.get(MissionStatus.ACTIVE.value, 0),
        }

    def delete(self, mission_id: str) -> bool:
        if mission_id in self._missions:
            del self._missions[mission_id]
            self._save()
            return True
        return False

    def to_dict_list(self, missions: Optional[list] = None) -> list[dict]:
        items = missions if missions is not None else list(self._missions.values())
        return [m.to_dict() for m in items]

    def decompose_mission(
        self, mission_id: str, subtask_specs: list[dict]
    ) -> Optional[Mission]:
        """Add subtasks to a mission.

        Each spec in subtask_specs should have at least ``title``; optional keys:
        ``description``, ``dependencies`` (list of subtask indices this depends on).
        """
        m = self.get(mission_id)
        if not m:
            return None
        for i, spec in enumerate(subtask_specs):
            subtask = {
                "idx": len(m.subtasks),
                "title": spec.get("title", f"Subtask {len(m.subtasks)}"),
                "description": spec.get("description", ""),
                "status": "pending",
                "assigned_worker": None,
                "result": None,
                "dependencies": spec.get("dependencies", []),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
            }
            m.subtasks.append(subtask)
        m.add_event(
            f"Decomposed into {len(subtask_specs)} subtasks (total: {len(m.subtasks)})",
            actor="orchestrator",
        )
        self._save()
        return m

    def assign_subtask(
        self, mission_id: str, subtask_idx: int, worker: str
    ) -> Optional[dict]:
        """Assign a subtask to a worker. Returns the subtask dict or None."""
        m = self.get(mission_id)
        if not m or subtask_idx < 0 or subtask_idx >= len(m.subtasks):
            return None
        st = m.subtasks[subtask_idx]
        # Check dependency satisfaction
        for dep_idx in st.get("dependencies", []):
            if dep_idx < len(m.subtasks) and m.subtasks[dep_idx].get("status") != "completed":
                return None  # dependency not met
        st["assigned_worker"] = worker
        st["status"] = "active"
        m.assign_worker(worker)
        m.add_event(
            f"Subtask {subtask_idx} ({st['title']}) assigned to {worker}",
            actor="orchestrator",
        )
        if m.status == MissionStatus.PLANNED.value:
            m.set_status(MissionStatus.ACTIVE.value, actor="orchestrator")
        self._save()
        return st

    def complete_subtask(
        self, mission_id: str, subtask_idx: int, result: str
    ) -> Optional[Mission]:
        """Mark a subtask as completed with a result. Auto-completes mission if all done."""
        m = self.get(mission_id)
        if not m or subtask_idx < 0 or subtask_idx >= len(m.subtasks):
            return None
        st = m.subtasks[subtask_idx]
        st["status"] = "completed"
        st["result"] = result
        st["completed_at"] = datetime.now(timezone.utc).isoformat()
        m.results[str(subtask_idx)] = result
        m.add_event(
            f"Subtask {subtask_idx} ({st['title']}) completed",
            actor=st.get("assigned_worker", "system"),
        )
        if m.all_subtasks_done():
            m.set_status(MissionStatus.COMPLETED.value, actor="system")
            m.completed_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return m

    def get_mission_timeline(self) -> list[dict]:
        """Return a gantt-style timeline across all missions."""
        gantt = []
        for m in sorted(self._missions.values(), key=lambda x: x.created_at):
            entry = {
                "mission_id": m.mission_id,
                "title": m.title,
                "status": m.status,
                "start": m.created_at,
                "end": m.completed_at or m.updated_at,
                "workers": m.workers,
                "progress": m.subtask_progress(),
                "subtasks": [],
            }
            for st in m.subtasks:
                entry["subtasks"].append({
                    "idx": st["idx"],
                    "title": st["title"],
                    "status": st["status"],
                    "worker": st.get("assigned_worker"),
                    "start": st.get("created_at"),
                    "end": st.get("completed_at"),
                })
            gantt.append(entry)
        return gantt


def _build_missions_parser():
    """Build the missions CLI parser."""
    parser = argparse.ArgumentParser(description="Skynet Mission Control")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List missions")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--limit", type=int, default=50)

    p_create = sub.add_parser("create", help="Create a mission")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--owner", default="orchestrator")
    p_create.add_argument("--priority", type=int, default=2)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--tags", default="")

    p_status = sub.add_parser("status", help="Show mission status")
    p_status.add_argument("mission_id")

    p_timeline = sub.add_parser("timeline", help="Show mission timeline")
    p_timeline.add_argument("mission_id")

    p_update = sub.add_parser("update", help="Update mission status")
    p_update.add_argument("mission_id")
    p_update.add_argument("--status", required=True)
    p_update.add_argument("--actor", default="cli")

    p_event = sub.add_parser("add-event", help="Add timeline event")
    p_event.add_argument("mission_id")
    p_event.add_argument("--event", required=True)
    p_event.add_argument("--actor", default="cli")

    return parser


def _dispatch_missions_command(args, mc: "MissionControl") -> int:
    """Dispatch parsed CLI command to the appropriate handler."""
    if args.command == "list":
        for m in mc.list_missions(status=args.status, limit=args.limit):
            print(f"[{m.status}] P{m.priority} {m.mission_id}: {m.title}")
        return 0
    if args.command == "create":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        m = mc.create(title=args.title, owner=args.owner, priority=args.priority,
                      description=args.description, tags=tags)
        print(json.dumps(m.to_dict(), indent=2))
        return 0
    if args.command == "status":
        m = mc.get(args.mission_id)
        if not m:
            print(f"Mission {args.mission_id} not found")
            return 1
        print(json.dumps(m.to_dict(), indent=2))
        return 0
    if args.command == "timeline":
        tl = mc.get_timeline(args.mission_id)
        if tl is None:
            print(f"Mission {args.mission_id} not found")
            return 1
        for e in tl:
            print(f"  [{e['timestamp']}] {e['actor']}: {e['event']}")
        return 0
    if args.command == "update":
        m = mc.update_status(args.mission_id, args.status, actor=args.actor)
        if not m:
            print(f"Mission {args.mission_id} not found")
            return 1
        print(f"Updated to {m.status}")
        return 0
    if args.command == "add-event":
        entry = mc.add_event(args.mission_id, args.event, actor=args.actor)
        if not entry:
            print(f"Mission {args.mission_id} not found")
            return 1
        print(json.dumps(entry, indent=2))
        return 0
    return -1


def main() -> int:
    parser = _build_missions_parser()
    args = parser.parse_args()
    mc = MissionControl()
    result = _dispatch_missions_command(args, mc)
    if result == -1:
        parser.print_help()
        return 0
    return result


if __name__ == "__main__":
    raise SystemExit(main())
