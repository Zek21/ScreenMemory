#!/usr/bin/env python3
"""
Skynet Agent Telemetry — truthful live visibility for doing/typing/thinking.

What this provides:
- doing: inferred from current task, transport state, and bridge task state
- typing_visible: only text that is actually visible in a UIA Edit field or
  explicitly self-reported by an agent
- thinking_summary: explicit self-report only, except the public orchestrator
  feed which is already an exposed system surface

What this does NOT do:
- it does not invent hidden chain-of-thought
- it does not guess keystrokes that are not visible
- it does not claim activity beyond observed state transitions

HTTP API:
- GET  /health
- GET  /telemetry
- GET  /telemetry/<agent_id>
- POST /telemetry    # explicit self-report / override

CLI:
- python tools/skynet_agent_telemetry.py once
- python tools/skynet_agent_telemetry.py start
- python tools/skynet_agent_telemetry.py publish --agent alpha --thinking-summary "Reviewing test failures"
- python tools/skynet_agent_telemetry.py status
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
OUT_FILE = DATA_DIR / "agent_telemetry.json"
MANUAL_FILE = DATA_DIR / "agent_telemetry_manual.json"
PID_FILE = DATA_DIR / "agent_telemetry.pid"
SKYNET_STATUS_URL = "http://localhost:8420/status"
DEFAULT_PORT = 8426
DEFAULT_STALE_AFTER_S = 4.0
CONSULTANT_PORTS = (8422, 8425)

# Load telemetry interval from brain_config.json, fallback to 60s
BRAIN_CONFIG_FILE = DATA_DIR / "brain_config.json"

def _load_telemetry_interval() -> float:
    """Load TELEMETRY_INTERVAL from brain_config.json, default 60s."""
    try:
        if BRAIN_CONFIG_FILE.exists():
            cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
            val = cfg.get("telemetry_interval", 60.0)
            return max(1.0, float(val))
    except Exception:
        pass
    return 60.0

DEFAULT_INTERVAL_S = _load_telemetry_interval()

sys.path.insert(0, str(ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fetch_json(url: str, timeout: float = 1.5) -> Any:
    try:
        with urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (OSError, URLError, json.JSONDecodeError):
        return None


def _parse_time(value: Any) -> Optional[float]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_i)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid_i, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _claim_pid(label: str) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                old_pid = 0
            if _pid_alive(old_pid):
                print(f"{label} already running (PID {old_pid})", flush=True)
                return False
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                print(f"{label} found stale PID file but could not clear it: {PID_FILE}", flush=True)
                return False
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            return True
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                if PID_FILE.exists():
                    PID_FILE.unlink()
            except Exception:
                pass
            raise


def _load_workers_registry() -> tuple[Dict[str, int], Optional[int]]:
    raw = _read_json(WORKERS_FILE)
    hwnds: Dict[str, int] = {}
    for worker in raw.get("workers", []):
        if not isinstance(worker, dict):
            continue
        name = str(worker.get("name") or "").strip().lower()
        hwnd = worker.get("hwnd")
        if name and isinstance(hwnd, int) and hwnd > 0:
            hwnds[name] = hwnd
    orch_hwnd = raw.get("orchestrator_hwnd")
    if isinstance(orch_hwnd, int) and orch_hwnd > 0:
        hwnds["orchestrator"] = orch_hwnd
        return hwnds, orch_hwnd
    return hwnds, None


def _state_doing(state: str) -> str:
    st = str(state or "").upper()
    if st == "PROCESSING":
        return "Processing visible task"
    if st == "TYPING":
        return "Editing visible input"
    if st == "STEERING":
        return "In steering UI"
    if st == "IDLE":
        return "Standing by"
    return "Unknown"


def _build_base_entry(agent_id: str, kind: str) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "kind": kind,
        "status": "UNKNOWN",
        "phase": "unknown",
        "doing": "Unknown",
        "current_task": "",
        "typing_visible": "",
        "typing_source": "none",
        "thinking_summary": "unknown",
        "thinking_source": "explicit_only",
        "observed_at": _now_iso(),
        "source": "unavailable",
        "live": False,
    }


def _collect_window_telemetry(backend_status: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    from tools.uia_engine import get_engine

    hwnds, _ = _load_workers_registry()
    if not hwnds:
        return {}

    backend_agents = {}
    public_orch_thought = ""
    if isinstance(backend_status, dict):
        backend_agents = backend_status.get("agents", {}) or {}
        thoughts = backend_status.get("orch_thinking", []) or []
        if thoughts and isinstance(thoughts[-1], dict):
            public_orch_thought = _sanitize_text(thoughts[-1].get("text"), 180)

    scans = get_engine().scan_all(hwnds, max_workers=min(max(len(hwnds), 1), 5))
    entries: Dict[str, Dict[str, Any]] = {}

    for agent_id, scan in scans.items():
        kind = "orchestrator" if agent_id == "orchestrator" else "worker"
        entry = _build_base_entry(agent_id, kind)
        backend_agent = backend_agents.get(agent_id, {}) if isinstance(backend_agents, dict) else {}
        current_task = _sanitize_text(backend_agent.get("current_task"), 180)
        typing_visible = _sanitize_text(getattr(scan, "edit_value", ""), 180)
        state = str(getattr(scan, "state", "UNKNOWN") or "UNKNOWN").upper()

        entry.update({
            "status": state,
            "phase": state.lower(),
            "current_task": current_task,
            "doing": current_task or _state_doing(state),
            "typing_visible": typing_visible,
            "typing_source": "uia_edit_value" if typing_visible else "none",
            "source": "uia+backend",
            "live": state != "UNKNOWN",
            "model": _sanitize_text(backend_agent.get("model") or getattr(scan, "model", ""), 120),
            "hwnd": getattr(scan, "hwnd", hwnds.get(agent_id)),
            "queue_depth": backend_agent.get("queue_depth", 0),
        })
        if agent_id == "orchestrator" and public_orch_thought:
            entry["thinking_summary"] = public_orch_thought
            entry["thinking_source"] = "public_orchestrator_feed"
        entries[agent_id] = entry

    return entries


def _consultant_prompt_file(consultant_id: str) -> Path:
    if consultant_id == "consultant":
        return DATA_DIR / "consultant_prompt_queue.json"
    return DATA_DIR / f"{consultant_id}_prompt_queue.json"


def _consultant_task_file(consultant_id: str) -> Path:
    if consultant_id == "consultant":
        return DATA_DIR / "consultant_task_state.json"
    return DATA_DIR / f"{consultant_id}_task_state.json"


def _process_single_consultant(path: Path, raw: dict, consultant_id: str) -> Dict[str, Any]:
    """Build a telemetry entry for one consultant from its state and task files."""
    live_payload = None
    api_url = raw.get("api_url")
    if api_url:
        live_payload = _fetch_json(str(api_url), timeout=1.0)
        if isinstance(live_payload, dict) and "consultant" in live_payload:
            raw = live_payload.get("consultant") or raw

    entry = _build_base_entry(consultant_id, "consultant")
    task_state = _read_json(_consultant_task_file(consultant_id))
    prompt_store = _read_json(_consultant_prompt_file(consultant_id))
    prompts = prompt_store.get("prompts", []) if isinstance(prompt_store.get("prompts"), list) else []
    pending = [p for p in prompts if str(p.get("status", "")).lower() == "pending"]
    latest_pending = pending[-1] if pending else None

    task_status = str(task_state.get("status") or raw.get("status") or "UNKNOWN").upper()
    current_task = _sanitize_text(task_state.get("task"), 180)
    if not current_task and latest_pending:
        current_task = _sanitize_text(latest_pending.get("content"), 180)

    doing = current_task or ("Queued prompt awaiting acceptance" if latest_pending else _state_doing(task_status))
    live = bool(raw.get("live")) and str(raw.get("status", "")).upper() == "LIVE"
    if raw and str(raw.get("status", "")).upper() in ("STALE", "OFFLINE"):
        live = False

    entry.update({
        "status": task_status if task_status not in ("", "UNKNOWN") else str(raw.get("status") or "UNKNOWN").upper(),
        "phase": str(task_state.get("status") or raw.get("status") or "unknown").lower(),
        "current_task": current_task,
        "doing": doing,
        "source": "consultant_bridge",
        "live": live,
        "transport": raw.get("prompt_transport") or raw.get("transport"),
        "typing_visible": "",
        "typing_source": "not_observable",
    })
    if latest_pending:
        entry["pending_prompt_id"] = latest_pending.get("id")
        entry["pending_prompt_created_at"] = latest_pending.get("created_at")
    return entry


def _collect_consultant_telemetry() -> Dict[str, Dict[str, Any]]:
    entries: Dict[str, Dict[str, Any]] = {}
    state_files = sorted(
        path for path in DATA_DIR.glob("*consultant_state.json")
        if not path.name.endswith("_task_state.json")
    )
    seen_ids = set()

    for path in state_files:
        raw = _read_json(path)
        consultant_id = str(raw.get("id") or path.stem.replace("_state", "")).strip()
        if not consultant_id or consultant_id in seen_ids:
            continue
        seen_ids.add(consultant_id)
        entries[consultant_id] = _process_single_consultant(path, raw, consultant_id)

    return entries


def _load_manual_store() -> Dict[str, Any]:
    data = _read_json(MANUAL_FILE)
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        agents = {}
    return {"agents": agents}


def _save_manual_store(store: Dict[str, Any]) -> None:
    _atomic_write(MANUAL_FILE, {"agents": store.get("agents", {})})


MANUAL_STORE_MAX_AGE_S = 300.0  # 5 minutes


def _cleanup_stale_manual_entries() -> int:
    """Remove manual store entries older than 5 minutes. Returns count removed."""
    store = _load_manual_store()
    agents = store.get("agents", {})
    if not agents:
        return 0
    now = time.time()
    stale_keys = []
    for agent_id, entry in agents.items():
        if not isinstance(entry, dict):
            stale_keys.append(agent_id)
            continue
        updated_at = _parse_time(entry.get("updated_at"))
        if updated_at is None or (now - updated_at) > MANUAL_STORE_MAX_AGE_S:
            stale_keys.append(agent_id)
    if stale_keys:
        for k in stale_keys:
            del agents[k]
        _save_manual_store(store)
    return len(stale_keys)


def publish_manual(agent_id: str, doing: Optional[str] = None,
                   typing_visible: Optional[str] = None,
                   thinking_summary: Optional[str] = None,
                   status: Optional[str] = None,
                   phase: Optional[str] = None,
                   source: str = "self_report",
                   ttl_s: float = 15.0) -> Dict[str, Any]:
    if not agent_id:
        raise ValueError("agent_id required")
    store = _load_manual_store()
    agents = store.setdefault("agents", {})
    existing = agents.get(agent_id, {}) if isinstance(agents.get(agent_id), dict) else {}
    payload = {
        **existing,
        "agent_id": agent_id,
        "updated_at": _now_iso(),
        "stale_after_s": max(1.0, float(ttl_s or 15.0)),
        "source": source or "self_report",
    }
    if doing is not None:
        payload["doing"] = _sanitize_text(doing, 180)
    if typing_visible is not None:
        payload["typing_visible"] = _sanitize_text(typing_visible, 180)
    if thinking_summary is not None:
        payload["thinking_summary"] = _sanitize_text(thinking_summary, 220)
    if status is not None:
        payload["status"] = str(status).upper()
    if phase is not None:
        payload["phase"] = _sanitize_text(phase, 60).lower()
    agents[agent_id] = payload
    _save_manual_store(store)
    return payload


def _merge_manual(entries: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    for agent_id, manual in _load_manual_store().get("agents", {}).items():
        if not isinstance(manual, dict):
            continue
        updated_ts = _parse_time(manual.get("updated_at"))
        stale_after_s = float(manual.get("stale_after_s") or 15.0)
        if updated_ts is None or (now - updated_ts) > stale_after_s:
            continue
        entry = entries.setdefault(agent_id, _build_base_entry(agent_id, "agent"))
        if manual.get("doing"):
            entry["doing"] = _sanitize_text(manual.get("doing"), 180)
        if manual.get("typing_visible"):
            entry["typing_visible"] = _sanitize_text(manual.get("typing_visible"), 180)
            entry["typing_source"] = str(manual.get("source") or "self_report")
        if manual.get("thinking_summary"):
            entry["thinking_summary"] = _sanitize_text(manual.get("thinking_summary"), 220)
            entry["thinking_source"] = str(manual.get("source") or "self_report")
        if manual.get("status"):
            entry["status"] = str(manual.get("status")).upper()
        if manual.get("phase"):
            entry["phase"] = str(manual.get("phase")).lower()
        entry["manual_updated_at"] = manual.get("updated_at")
        if entry.get("source") == "unavailable":
            entry["source"] = "manual"
    return entries


def collect_snapshot() -> Dict[str, Any]:
    # Cleanup stale manual entries before collecting
    _cleanup_stale_manual_entries()
    backend_status = _fetch_json(SKYNET_STATUS_URL, timeout=1.0)
    entries: Dict[str, Dict[str, Any]] = {}
    try:
        entries.update(_collect_window_telemetry(backend_status))
    except Exception:
        pass
    try:
        entries.update(_collect_consultant_telemetry())
    except Exception:
        pass
    entries = _merge_manual(entries)

    typing_agents = 0
    thinking_known = 0
    live_agents = 0
    for entry in entries.values():
        entry["observed_at"] = _now_iso()
        if entry.get("typing_visible"):
            typing_agents += 1
        if str(entry.get("thinking_summary") or "").lower() not in ("", "unknown"):
            thinking_known += 1
        if entry.get("live"):
            live_agents += 1

    snapshot = {
        "generated_at": _now_iso(),
        "agents": entries,
        "summary": {
            "total_agents": len(entries),
            "live_agents": live_agents,
            "typing_agents": typing_agents,
            "thinking_known_agents": thinking_known,
        },
        "truth_notes": {
            "doing": "Derived from visible task state, queue state, or explicit self-report.",
            "typing_visible": "Only visible UI input or explicit self-report is shown.",
            "thinking_summary": "Only explicit self-report or public orchestrator feed is shown.",
        },
    }
    _atomic_write(OUT_FILE, snapshot)
    return snapshot


_snapshot_lock = threading.Lock()
_cached_snapshot: Optional[Dict[str, Any]] = None
_cached_snapshot_t = 0.0


def get_snapshot(max_age_s: float = DEFAULT_STALE_AFTER_S) -> Dict[str, Any]:
    global _cached_snapshot, _cached_snapshot_t
    now = time.time()
    with _snapshot_lock:
        if _cached_snapshot is not None and (now - _cached_snapshot_t) <= max_age_s:
            return _cached_snapshot
    snapshot = collect_snapshot()
    with _snapshot_lock:
        _cached_snapshot = snapshot
        _cached_snapshot_t = time.time()
    return snapshot


class TelemetryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response({
                "status": "ok",
                "service": "agent-telemetry",
                "timestamp": _now_iso(),
                "snapshot_fresh": bool(_cached_snapshot),
            })
            return
        if self.path == "/telemetry":
            self._json_response(get_snapshot())
            return
        if self.path.startswith("/telemetry/"):
            agent_id = self.path.split("/telemetry/", 1)[1].strip().lower()
            snap = get_snapshot()
            entry = snap.get("agents", {}).get(agent_id)
            if entry is None:
                self._json_response({"error": "agent not found", "agent_id": agent_id}, status=404)
                return
            self._json_response({"generated_at": snap.get("generated_at"), "agent": entry})
            return
        self._json_response({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path != "/telemetry":
            self._json_response({"error": "not found"}, status=404)
            return
        payload = self._read_json()
        agent_id = str(payload.get("agent_id") or payload.get("agent") or "").strip().lower()
        if not agent_id:
            self._json_response({"error": "agent_id required"}, status=400)
            return
        try:
            result = publish_manual(
                agent_id=agent_id,
                doing=payload.get("doing"),
                typing_visible=payload.get("typing_visible"),
                thinking_summary=payload.get("thinking_summary"),
                status=payload.get("status"),
                phase=payload.get("phase"),
                source=str(payload.get("source") or "self_report"),
                ttl_s=float(payload.get("ttl_s") or 15.0),
            )
        except (TypeError, ValueError) as exc:
            self._json_response({"error": str(exc)}, status=400)
            return
        snapshot = get_snapshot(max_age_s=0.0)
        self._json_response({"status": "ok", "agent": result, "snapshot": snapshot})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _collector_loop(interval_s: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            get_snapshot(max_age_s=0.0)
        except Exception:
            pass
        stop_event.wait(interval_s)


def _cleanup_pid() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


def run_daemon(port: int = DEFAULT_PORT, interval_s: float = DEFAULT_INTERVAL_S) -> int:
    if not _claim_pid("agent telemetry"):
        return 0
    atexit.register(_cleanup_pid)
    get_snapshot(max_age_s=0.0)

    server = ThreadingHTTPServer(("127.0.0.1", port), TelemetryHandler)
    stop_event = threading.Event()
    collector = threading.Thread(
        target=_collector_loop,
        args=(interval_s, stop_event),
        daemon=True,
        name="agent-telemetry-collector",
    )
    collector.start()

    def _shutdown() -> None:
        stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    atexit.register(_shutdown)

    print(f"[telemetry] live on http://127.0.0.1:{port}/telemetry interval={interval_s:.1f}s", flush=True)
    try:
        server.serve_forever()
    finally:
        _shutdown()
    return 0


def _status_text() -> str:
    snap = get_snapshot()
    lines = [
        f"generated_at={snap.get('generated_at')}",
        (
            f"summary total={snap.get('summary', {}).get('total_agents', 0)} "
            f"live={snap.get('summary', {}).get('live_agents', 0)} "
            f"typing={snap.get('summary', {}).get('typing_agents', 0)} "
            f"thinking_known={snap.get('summary', {}).get('thinking_known_agents', 0)}"
        ),
    ]
    for agent_id, entry in sorted(snap.get("agents", {}).items()):
        lines.append(
            f"{agent_id}: {entry.get('status')} | doing={entry.get('doing')} | "
            f"typing={entry.get('typing_visible') or '-'} | thinking={entry.get('thinking_summary')}"
        )
    return "\n".join(lines)


def _build_telemetry_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(description="Skynet live agent telemetry")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_once = sub.add_parser("once", help="Collect one snapshot and print JSON")
    p_once.add_argument("--pretty", action="store_true", help="Pretty-print JSON")

    p_start = sub.add_parser("start", help="Run telemetry daemon")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_start.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)

    p_publish = sub.add_parser("publish", help="Publish explicit agent telemetry")
    p_publish.add_argument("--agent", required=True)
    p_publish.add_argument("--doing")
    p_publish.add_argument("--typing-visible")
    p_publish.add_argument("--thinking-summary")
    p_publish.add_argument("--status")
    p_publish.add_argument("--phase")
    p_publish.add_argument("--ttl-s", type=float, default=15.0)
    p_publish.add_argument("--source", default="self_report")

    sub.add_parser("status", help="Show latest telemetry summary")
    return parser


def _run_telemetry_command(args: argparse.Namespace) -> int:
    """Dispatch parsed CLI args to the appropriate handler."""
    if args.cmd == "once":
        snap = get_snapshot(max_age_s=0.0)
        indent = 2 if args.pretty else None
        print(json.dumps(snap, indent=indent, default=str))
        return 0
    if args.cmd == "start":
        return run_daemon(port=args.port, interval_s=args.interval)
    if args.cmd == "publish":
        result = publish_manual(
            agent_id=args.agent.strip().lower(),
            doing=args.doing,
            typing_visible=args.typing_visible,
            thinking_summary=args.thinking_summary,
            status=args.status,
            phase=args.phase,
            source=args.source,
            ttl_s=args.ttl_s,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.cmd == "status":
        print(_status_text())
        return 0
    return 1


def main() -> int:
    parser = _build_telemetry_parser()
    args = parser.parse_args()
    return _run_telemetry_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
