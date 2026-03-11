#!/usr/bin/env python3
"""
skynet_consultant_bridge.py -- live presence bridge for consultants.

Supports multiple consultant identities via CLI args:
  --id <consultant_id>          Profile key in agent_profiles.json (default: consultant)
  --display-name <name>         Human-readable name (default: from profile)
  --model <model>               Model name (default: from profile)
  --source <source>             Start trigger (default: CC-Start)
  --state-file <path>           State file path (default: data/consultant_state.json)

Truthful semantics:
- consultant remains an advisory identity, but a live bridge may also accept prompts
- promptable means the live bridge accepts prompts into its queue
- LIVE means this bridge process is actively heartbeating now
- DECLARED means the consultant profile exists but no live bridge is running
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROFILE_FILE = DATA_DIR / "agent_profiles.json"
STATE_FILE = DATA_DIR / "consultant_state.json"
PID_FILE = DATA_DIR / "consultant_bridge.pid"
PROMPT_FILE = DATA_DIR / "consultant_prompt_queue.json"
TASK_FILE = DATA_DIR / "consultant_task_state.json"
SCORES_FILE = DATA_DIR / "worker_scores.json"
SKYNET_URL = "http://localhost:8420"
CONSULTANT_ID = "consultant"
DISPLAY_NAME = "Codex Consultant"
MODEL_NAME = "GPT-5 Codex"
SOURCE_NAME = "CC-Start"
DEFAULT_INTERVAL_S = 2.0
DEFAULT_STALE_AFTER_S = 8.0
DEFAULT_API_PORT = 8422
MAX_PROMPTS = 200
STARTED_AT = datetime.now(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, default=str)
    last_error: Optional[BaseException] = None
    for attempt in range(8):
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{attempt}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            last_error = exc
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _signature_token() -> str:
    return f"signed:{CONSULTANT_ID}"  # signed: consultant


def _signed_content(content: str) -> str:
    text = str(content or "").strip()
    signature = _signature_token()
    if signature.lower() in text.lower():
        return text
    if not text:
        return signature
    return f"{text} {signature}"  # signed: consultant


def _load_score_summary(actor_id: str) -> Dict[str, Any]:
    raw = _read_json(SCORES_FILE)
    scores = raw.get("scores", {}) if isinstance(raw, dict) else {}
    entry = scores.get(actor_id, {}) if isinstance(scores, dict) else {}
    if not isinstance(entry, dict):
        entry = {}
    return {
        "total": round(float(entry.get("total") or 0.0), 6),
        "awards": int(entry.get("awards") or 0),
        "deductions": int(entry.get("deductions") or 0),
        "proactive_ticket_clears": int(entry.get("proactive_ticket_clears") or 0),
        "bug_reports_filed": int(entry.get("bug_reports_filed") or 0),
        "bug_cross_validations": int(entry.get("bug_cross_validations") or 0),
        "zero_ticket_bonus_awards": int(entry.get("zero_ticket_bonus_awards") or 0),
    }  # signed: consultant


def _parse_time(value: Any) -> Optional[float]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i == os.getpid():
        return True
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_i)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid_i, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _http_get(path: str, timeout: float = 3.0) -> Any:
    try:
        with urlopen(f"{SKYNET_URL}{path}", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _http_post(path: str, payload: Dict[str, Any], timeout: float = 3.0) -> bool:
    try:
        req = Request(
            f"{SKYNET_URL}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _load_profile() -> Dict[str, Any]:
    data = _read_json(PROFILE_FILE)
    profile = data.get(CONSULTANT_ID, {}) if isinstance(data, dict) else {}
    return {
        "id": CONSULTANT_ID,
        "display_name": profile.get("name", DISPLAY_NAME),
        "role": profile.get("role", f"{DISPLAY_NAME} -- Co-Equal Advisory Peer"),
        "model": profile.get("model", MODEL_NAME),
        "kind": profile.get("kind", "advisor"),
        "capabilities": profile.get("capabilities", []),
        "specializations": profile.get("specializations", []),
        "declared_status": profile.get("current_status", "REGISTERED (profile-only; not a live backend agent)"),
    }


def _prompt_file() -> Path:
    if CONSULTANT_ID == "consultant":
        return PROMPT_FILE
    return DATA_DIR / f"{CONSULTANT_ID}_prompt_queue.json"


def _task_file() -> Path:
    if CONSULTANT_ID == "consultant":
        return TASK_FILE
    return DATA_DIR / f"{CONSULTANT_ID}_task_state.json"


def _load_prompt_store() -> Dict[str, Any]:
    data = _read_json(_prompt_file())
    prompts = data.get("prompts", [])
    if not isinstance(prompts, list):
        prompts = []
    return {
        "consultant_id": CONSULTANT_ID,
        "updated_at": data.get("updated_at"),
        "prompts": prompts,
    }


def _save_prompt_store(data: Dict[str, Any]) -> None:
    payload = {
        "consultant_id": CONSULTANT_ID,
        "updated_at": _now_iso(),
        "prompts": data.get("prompts", []),
    }
    _atomic_write(_prompt_file(), payload)


def _prompt_stats(prompts: Optional[list[Dict[str, Any]]] = None) -> Dict[str, int]:
    prompts = prompts if prompts is not None else _load_prompt_store().get("prompts", [])
    stats = {"total": 0, "pending": 0, "acknowledged": 0, "completed": 0, "failed": 0}
    for prompt in prompts:
        stats["total"] += 1
        status = str(prompt.get("status", "")).lower()
        if status in stats:
            stats[status] += 1
    return stats


def _load_task_state() -> Dict[str, Any]:
    raw = _read_json(_task_file())
    state = {
        "status": "IDLE",
        "prompt_id": None,
        "task": "",
        "assigned_worker": None,
        "claimed_at": None,
        "delegated_at": None,
        "completed_at": None,
        "updated_at": raw.get("updated_at"),
        "last_result": "",
        "notes": "",
    }
    if isinstance(raw, dict):
        for key in state.keys():
            if key in raw:
                state[key] = raw.get(key)
    return state


def _save_task_state(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "status": str(state.get("status") or "IDLE").upper(),
        "prompt_id": state.get("prompt_id"),
        "task": str(state.get("task") or ""),
        "assigned_worker": state.get("assigned_worker"),
        "claimed_at": state.get("claimed_at"),
        "delegated_at": state.get("delegated_at"),
        "completed_at": state.get("completed_at"),
        "updated_at": _now_iso(),
        "last_result": str(state.get("last_result") or ""),
        "notes": str(state.get("notes") or ""),
    }
    _atomic_write(_task_file(), payload)
    return payload


def _publish_consultant_event(event_type: str, content: str,
                              metadata: Optional[Dict[str, Any]] = None) -> bool:
    clean_metadata: Dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            clean_metadata[str(key)] = value
        elif isinstance(value, (int, float, bool)):
            clean_metadata[str(key)] = str(value)
        else:
            clean_metadata[str(key)] = json.dumps(value, default=str)
    clean_metadata.setdefault("score_actor", CONSULTANT_ID)
    clean_metadata.setdefault("signature", _signature_token())
    payload = {
        "sender": CONSULTANT_ID,
        "topic": "orchestrator",
        "type": event_type,
        "content": _signed_content(content),
        "metadata": clean_metadata,
    }
    try:
        try:
            from tools.skynet_spam_guard import guarded_publish
        except ImportError:
            from skynet_spam_guard import guarded_publish
        result = guarded_publish(payload)
        return bool(result.get("allowed") and result.get("published", True))
    except Exception:
        pass
    return _http_post("/bus/publish", payload, timeout=2.0)


def _normalize_worker_snapshot(workers: Dict[str, Any], source: str, age_s: Optional[float]) -> Dict[str, Any]:
    normalized = {}
    available = []
    busy = []
    offline = []
    for name, info in (workers or {}).items():
        if str(name).lower() == "orchestrator" or not isinstance(info, dict):
            continue
        status = str(info.get("status") or "UNKNOWN").upper()
        queue_depth = int(info.get("queue_depth") or 0)
        current_task = str(info.get("current_task") or "")
        worker_available = status == "IDLE" and queue_depth <= 0 and not current_task.strip()
        if status in ("OFFLINE", "ERROR", "DEAD"):
            worker_available = False
            offline.append(name)
        elif worker_available:
            available.append(name)
        else:
            busy.append(name)
        normalized[name] = {
            "status": status,
            "available": worker_available,
            "queue_depth": queue_depth,
            "current_task": current_task,
            "model": info.get("model"),
            "last_heartbeat": info.get("last_heartbeat"),
        }
    return {
        "source": source,
        "age_s": age_s,
        "workers": normalized,
        "available_workers": available,
        "busy_workers": busy,
        "offline_workers": offline,
        "summary": {
            "total": len(normalized),
            "available": len(available),
            "busy": len(busy),
            "offline": len(offline),
        },
    }


def _load_worker_snapshot(max_age_s: float = 10.0) -> Dict[str, Any]:
    realtime = _read_json(DATA_DIR / "realtime.json")
    if realtime:
        ts = _parse_time(realtime.get("last_update") or realtime.get("timestamp"))
        age_s = round(time.time() - ts, 1) if ts is not None else None
        workers = realtime.get("workers") or realtime.get("agents") or {}
        if isinstance(workers, dict) and age_s is not None and age_s <= max_age_s:
            return _normalize_worker_snapshot(workers, "realtime.json", age_s)

    backend = _http_get("/status", timeout=2.0)
    workers = backend.get("agents", {}) if isinstance(backend, dict) else {}
    if isinstance(workers, dict) and workers:
        return _normalize_worker_snapshot(workers, "backend:/status", 0.0)

    return _normalize_worker_snapshot({}, "unavailable", None)


def list_prompts(status: Optional[str] = None, limit: int = 50) -> list[Dict[str, Any]]:
    prompts = list(_load_prompt_store().get("prompts", []))
    prompts.sort(key=lambda p: str(p.get("created_at", "")))
    if status:
        status_l = status.lower()
        prompts = [p for p in prompts if str(p.get("status", "")).lower() == status_l]
    if limit > 0:
        prompts = prompts[-limit:]
    return prompts


def queue_prompt(sender: str, content: str, prompt_type: str = "directive",
                 metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not content:
        raise ValueError("content required")
    store = _load_prompt_store()
    prompts = store.get("prompts", [])
    prompt_id = f"prompt_{int(time.time() * 1000)}_{len(prompts)}"
    entry = {
        "id": prompt_id,
        "sender": sender or "unknown",
        "type": prompt_type or "directive",
        "content": content,
        "metadata": metadata or {},
        "status": "pending",
        "created_at": _now_iso(),
        "acknowledged_at": None,
        "completed_at": None,
        "result": None,
    }
    prompts.append(entry)
    if len(prompts) > MAX_PROMPTS:
        prompts = prompts[-MAX_PROMPTS:]
    store["prompts"] = prompts
    _save_prompt_store(store)
    return entry


def get_next_prompt() -> Optional[Dict[str, Any]]:
    pending = list_prompts(status="pending", limit=1)
    return pending[0] if pending else None


def acknowledge_prompt(prompt_id: str, consumer: str = "") -> Optional[Dict[str, Any]]:
    store = _load_prompt_store()
    updated = None
    for prompt in store.get("prompts", []):
        if prompt.get("id") != prompt_id:
            continue
        if str(prompt.get("status", "")).lower() == "pending":
            prompt["status"] = "acknowledged"
            prompt["acknowledged_at"] = _now_iso()
            if consumer:
                prompt["consumer"] = consumer
        updated = prompt
        break
    if updated is not None:
        _save_prompt_store(store)
        task_state = _save_task_state({
            "status": "CLAIMED",
            "prompt_id": updated.get("id"),
            "task": updated.get("content", ""),
            "assigned_worker": None,
            "claimed_at": updated.get("acknowledged_at"),
            "delegated_at": None,
            "completed_at": None,
            "last_result": "",
            "notes": f"claimed by {consumer or CONSULTANT_ID}",
        })
        workers = _load_worker_snapshot()
        _publish_consultant_event(
            "task_claim",
            f"{DISPLAY_NAME} accepted prompt {updated.get('id')} and is taking the task: "
            f"{str(updated.get('content') or '')[:200]}",
            metadata={
                "prompt_id": updated.get("id"),
                "consumer": consumer or CONSULTANT_ID,
                "task_state": task_state.get("status"),
                "available_workers": workers.get("available_workers", []),
                "worker_source": workers.get("source"),
            },
        )
    return updated


def complete_prompt(prompt_id: str, result: str = "", status: str = "completed") -> Optional[Dict[str, Any]]:
    status = status.lower()
    if status not in ("completed", "failed"):
        status = "completed"
    store = _load_prompt_store()
    updated = None
    for prompt in store.get("prompts", []):
        if prompt.get("id") != prompt_id:
            continue
        prompt["status"] = status
        prompt["completed_at"] = _now_iso()
        prompt["result"] = result
        updated = prompt
        break
    if updated is not None:
        _save_prompt_store(store)
        state_name = "FAILED" if status == "failed" else "COMPLETED"
        task_state = _load_task_state()
        _save_task_state({
            **task_state,
            "status": state_name,
            "prompt_id": updated.get("id"),
            "task": updated.get("content", "") or task_state.get("task", ""),
            "completed_at": updated.get("completed_at"),
            "last_result": result[:500],
            "notes": f"prompt {updated.get('id')} {state_name.lower()}",
        })
        _publish_consultant_event(
            "error" if status == "failed" else "result",
            f"{DISPLAY_NAME} {state_name.lower()} prompt {updated.get('id')}: {result[:300]}",
            metadata={
                "prompt_id": updated.get("id"),
                "status": state_name,
            },
        )
    return updated


def delegate_prompt(prompt_id: str, worker_name: str = "", task: str = "") -> Dict[str, Any]:
    store = _load_prompt_store()
    target = _find_prompt_in_store(store, prompt_id)
    if target is None:
        return {"success": False, "error": "prompt not found"}
    prompt_status = str(target.get("status") or "").lower()
    if prompt_status in ("completed", "failed"):
        return {"success": False, "error": f"prompt already {prompt_status}"}

    if prompt_status == "pending":
        acknowledge_prompt(prompt_id, consumer=CONSULTANT_ID)
        store = _load_prompt_store()
        target = _find_prompt_in_store(store, prompt_id) or target

    workers = _load_worker_snapshot()
    selected_worker, worker_status = _select_delegation_worker(worker_name, workers)
    if not selected_worker:
        return {"success": False, "error": "no worker available for delegation", "workers": workers}

    task_text = str(task or target.get("content") or "").strip()
    if not task_text:
        return {"success": False, "error": "task content required", "workers": workers}

    try:
        from tools.skynet_dispatch import dispatch_to_worker, load_orch_hwnd, load_workers
        ok = dispatch_to_worker(selected_worker, task_text, workers=load_workers(), orch_hwnd=load_orch_hwnd())
    except Exception as exc:
        return {"success": False, "error": str(exc), "workers": workers}

    if ok:
        return _record_delegation_success(store, target, prompt_id, selected_worker, worker_status, task_text, workers)

    return {"success": False, "error": f"dispatch to {selected_worker} failed",
            "worker": selected_worker, "worker_status": worker_status, "workers": workers}


def _find_prompt_in_store(store: dict, prompt_id: str) -> Optional[dict]:
    for prompt in store.get("prompts", []):
        if prompt.get("id") == prompt_id:
            return prompt
    return None


def _select_delegation_worker(worker_name: str, workers: dict):
    """Select a worker for delegation. Returns (worker_name, worker_status)."""
    normalized = workers.get("workers", {})
    selected = str(worker_name or "").strip()
    if not selected:
        available = workers.get("available_workers", [])
        selected = available[0] if available else ""
    status = normalized.get(selected, {}).get("status") if selected else None
    return selected, status


def _record_delegation_success(store, target, prompt_id, worker, worker_status, task_text, workers):
    """Record successful delegation in store, task state, and bus."""
    now = _now_iso()
    target.setdefault("metadata", {})
    if isinstance(target["metadata"], dict):
        target["metadata"]["delegated_to"] = worker
        target["metadata"]["delegated_at"] = now
    _save_prompt_store(store)
    task_state = _save_task_state({
        "status": "DELEGATED", "prompt_id": prompt_id, "task": task_text,
        "assigned_worker": worker, "claimed_at": target.get("acknowledged_at") or now,
        "delegated_at": now, "completed_at": None, "last_result": "",
        "notes": f"delegated to {worker}",
    })
    _publish_consultant_event("delegation",
        f"{DISPLAY_NAME} delegated prompt {prompt_id} to worker {worker}: {task_text[:200]}",
        metadata={"prompt_id": prompt_id, "worker": worker, "worker_status": worker_status,
                  "task_state": task_state.get("status"), "worker_source": workers.get("source")})
    return {"success": True, "worker": worker, "worker_status": worker_status,
            "prompt": target, "workers": workers, "task_state": task_state}


class ConsultantApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/consultant", "/consultants"):
            self._json_response({"consultant": get_consultant_view()})
            return
        if path in ("/consultant/prompts", "/consultants/prompts"):
            status = query.get("status", [None])[0]
            try:
                limit = int(query.get("limit", ["50"])[0])
            except Exception:
                limit = 50
            prompts = list_prompts(status=status, limit=limit)
            self._json_response({
                "consultant": get_consultant_view(),
                "prompts": prompts,
                "stats": _prompt_stats(prompts=None),
            })
            return
        if path in ("/consultant/prompts/next", "/consultants/prompts/next"):
            prompt = get_next_prompt()
            self._json_response({
                "consultant": get_consultant_view(),
                "prompt": prompt,
                "stats": _prompt_stats(),
            })
            return
        if path in ("/consultant/task", "/consultants/task"):
            self._json_response({
                "consultant": get_consultant_view(),
                "task": _load_task_state(),
                "workers": _load_worker_snapshot(),
            })
            return
        if path in ("/consultant/workers", "/consultants/workers"):
            self._json_response({
                "consultant": get_consultant_view(),
                "workers": _load_worker_snapshot(),
            })
            return
        if path == "/health":
            self._json_response({
                "status": "ok",
                "service": "consultant-bridge",
                "timestamp": _now_iso(),
            })
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()
        if payload is None:
            self.send_error(400, "bad json")
            return

        handler = {
            "/consultant/prompt": self._post_queue_prompt,
            "/consultants/prompt": self._post_queue_prompt,
            "/consultant/prompts/ack": self._post_acknowledge_prompt,
            "/consultants/prompts/ack": self._post_acknowledge_prompt,
            "/consultant/prompts/complete": self._post_complete_prompt,
            "/consultants/prompts/complete": self._post_complete_prompt,
            "/consultant/prompts/delegate": self._post_delegate_prompt,
            "/consultants/prompts/delegate": self._post_delegate_prompt,
        }.get(path)

        if handler:
            handler(payload)
        else:
            self.send_error(404)

    def _post_queue_prompt(self, payload: dict) -> None:
        content = str(payload.get("content") or payload.get("prompt") or "").strip()
        if not content:
            self.send_error(400, "content required")
            return
        try:
            prompt = queue_prompt(
                sender=str(payload.get("sender") or "delivery"),
                content=content,
                prompt_type=str(payload.get("type") or "directive"),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        self._json_response({"status": "queued", "prompt": prompt, "stats": _prompt_stats()}, status=202)

    def _post_acknowledge_prompt(self, payload: dict) -> None:
        prompt_id = str(payload.get("id") or payload.get("prompt_id") or "").strip()
        if not prompt_id:
            self.send_error(400, "id required")
            return
        prompt = acknowledge_prompt(prompt_id, consumer=str(payload.get("consumer") or CONSULTANT_ID))
        if prompt is None:
            self.send_error(404, "prompt not found")
            return
        self._json_response({"status": "acknowledged", "prompt": prompt, "stats": _prompt_stats()})

    def _post_complete_prompt(self, payload: dict) -> None:
        prompt_id = str(payload.get("id") or payload.get("prompt_id") or "").strip()
        if not prompt_id:
            self.send_error(400, "id required")
            return
        prompt = complete_prompt(prompt_id, result=str(payload.get("result") or ""),
                                status=str(payload.get("status") or "completed"))
        if prompt is None:
            self.send_error(404, "prompt not found")
            return
        self._json_response({"status": prompt.get("status"), "prompt": prompt, "stats": _prompt_stats()})

    def _post_delegate_prompt(self, payload: dict) -> None:
        prompt_id = str(payload.get("id") or payload.get("prompt_id") or "").strip()
        if not prompt_id:
            self.send_error(400, "id required")
            return
        result = delegate_prompt(prompt_id=prompt_id, worker_name=str(payload.get("worker") or ""),
                                 task=str(payload.get("task") or ""))
        status = 202 if result.get("success") else 409
        self._json_response(result, status=status)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return None


def _start_api_server(api_port: int) -> Optional[ThreadingHTTPServer]:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", api_port), ConsultantApiHandler)
    except OSError as exc:
        print(f"consultant bridge API unavailable on port {api_port}: {exc}", file=sys.stderr, flush=True)
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="consultant-bridge-api",
    )
    thread.start()
    return server


def _latest_consultant_message(limit: int = 100) -> Optional[Dict[str, Any]]:
    msgs = _http_get(f"/bus/messages?limit={limit}", timeout=2.0)
    if not isinstance(msgs, list):
        return None
    for msg in reversed(msgs):
        if str(msg.get("sender", "")).lower() != CONSULTANT_ID:
            continue
        return {
            "id": msg.get("id"),
            "topic": msg.get("topic"),
            "type": msg.get("type"),
            "content": msg.get("content"),
            "timestamp": msg.get("timestamp"),
        }
    return None


def build_live_state(interval_s: float = DEFAULT_INTERVAL_S,
                     stale_after_s: float = DEFAULT_STALE_AFTER_S,
                     api_port: Optional[int] = DEFAULT_API_PORT) -> Dict[str, Any]:
    profile = _load_profile()
    prompt_stats = _prompt_stats()
    task_state = _load_task_state()
    workers = _load_worker_snapshot()
    score_summary = _load_score_summary(CONSULTANT_ID)
    return {
        "id": CONSULTANT_ID,
        "display_name": profile["display_name"],
        "role": profile["role"],
        "model": profile["model"],
        "kind": "advisor",
        "backend_managed": False,
        "routable": True,
        "accepts_prompts": True,
        "requires_hwnd": False,
        "transport": f"{SOURCE_NAME.lower()}-bridge",
        "prompt_transport": "bridge_queue",
        "source": SOURCE_NAME,
        "status": "LIVE",
        "live": True,
        "bridge_pid": os.getpid(),
        "started_at": STARTED_AT,
        "last_heartbeat": _now_iso(),
        "heartbeat_interval_s": interval_s,
        "stale_after_s": stale_after_s,
        "api_port": api_port,
        "api_url": f"http://localhost:{api_port}/consultants" if api_port else None,
        "backend_connected": bool(_http_get("/status", timeout=2.0)),
        "last_bus_message": _latest_consultant_message(limit=100),
        "prompt_queue": prompt_stats,
        "task_state": task_state,
        "worker_snapshot": workers,
        "score": score_summary.get("total", 0.0),
        "score_summary": score_summary,
    }


def get_consultant_view() -> Dict[str, Any]:
    profile = _load_profile()
    raw = _read_json(STATE_FILE)
    view = _build_base_consultant_view(profile, raw)
    _apply_liveness_status(view, raw)
    return view


def _build_base_consultant_view(profile: dict, raw: dict) -> dict:
    source_name = str(raw.get("source") or SOURCE_NAME)
    raw_score_summary = raw.get("score_summary") if isinstance(raw.get("score_summary"), dict) else _load_score_summary(CONSULTANT_ID)
    return {
        "id": CONSULTANT_ID,
        "display_name": profile["display_name"],
        "role": profile["role"],
        "model": profile["model"],
        "kind": "advisor",
        "backend_managed": False,
        "routable": bool(raw.get("accepts_prompts", False)),
        "accepts_prompts": bool(raw.get("accepts_prompts", False)),
        "requires_hwnd": False,
        "transport": f"{source_name.lower()}-bridge",
        "prompt_transport": raw.get("prompt_transport", "bridge_queue"),
        "source": source_name,
        "declared": True,
        "declared_status": profile["declared_status"],
        "live": False,
        "status": "DECLARED",
        "bridge_pid": raw.get("bridge_pid"),
        "started_at": raw.get("started_at"),
        "last_heartbeat": raw.get("last_heartbeat"),
        "heartbeat_interval_s": raw.get("heartbeat_interval_s", DEFAULT_INTERVAL_S),
        "stale_after_s": raw.get("stale_after_s", DEFAULT_STALE_AFTER_S),
        "api_port": raw.get("api_port", DEFAULT_API_PORT),
        "api_url": raw.get("api_url", f"http://localhost:{DEFAULT_API_PORT}/consultants"),
        "backend_connected": raw.get("backend_connected", False),
        "last_bus_message": raw.get("last_bus_message"),
        "heartbeat_age_s": None,
        "pid_alive": False,
        "prompt_queue": raw.get("prompt_queue", _prompt_stats()),
        "task_state": raw.get("task_state", _load_task_state()),
        "worker_snapshot": raw.get("worker_snapshot", _load_worker_snapshot()),
        "score": raw.get("score", raw_score_summary.get("total", 0.0)),
        "score_summary": raw_score_summary,
    }


def _apply_liveness_status(view: dict, raw: dict) -> None:
    """Compute heartbeat age, PID liveness, and overall status."""
    hb_ts = _parse_time(view["last_heartbeat"])
    age_s = round(time.time() - hb_ts, 1) if hb_ts is not None else None
    pid_alive = _pid_alive(view["bridge_pid"])
    stale_after_s = float(view["stale_after_s"] or DEFAULT_STALE_AFTER_S)
    live = bool(raw) and pid_alive and age_s is not None and age_s <= stale_after_s

    view["heartbeat_age_s"] = age_s
    view["pid_alive"] = pid_alive
    view["live"] = live

    if live:
        view["status"] = "LIVE"
    elif raw and pid_alive:
        view["status"] = "STALE"
    elif raw:
        view["status"] = "OFFLINE"


def _announce_presence() -> None:
    _publish_consultant_event(
        "identity_ack",
        (
            f"{DISPLAY_NAME.upper()} LIVE -- {SOURCE_NAME} bridge active. "
            "Advisory peer is visible in consultant live surfaces. "
            "Prompt transport=bridge_queue."
        ),
        metadata={
            "display_name": DISPLAY_NAME,
            "kind": "advisor",
            "transport": f"{SOURCE_NAME.lower()}-bridge",
            "routable": "true",
            "prompt_transport": "bridge_queue",
        },
    )  # signed: consultant


def relay_consultant_result(content: str, consultant_id: str = None) -> dict:
    """Relay a consultant bus result to the orchestrator via direct-prompt.

    Call this from bus watchers or relay daemons when a consultant posts a
    result with topic=orchestrator. This bridges the consultant bus post
    into an immediate UIA ghost-type notification.

    Returns dict with delivery status.
    """
    cid = consultant_id or CONSULTANT_ID
    try:
        from skynet_delivery import deliver_consultant_result
        return deliver_consultant_result(cid, content)
    except Exception as e:
        return {"success": False, "error": str(e)}


def _write_offline_snapshot() -> None:
    if not STATE_FILE.exists():
        return
    state = _read_json(STATE_FILE)
    if not state:
        return
    state["status"] = "OFFLINE"
    state["live"] = False
    state["last_heartbeat"] = _now_iso()
    _atomic_write(STATE_FILE, state)


def _cleanup_pid(pid_file: Path = PID_FILE) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def run_daemon(interval_s: float = DEFAULT_INTERVAL_S,
               stale_after_s: float = DEFAULT_STALE_AFTER_S,
               api_port: int = DEFAULT_API_PORT,
               pid_file: Path = PID_FILE,
               announce: bool = True,
               once: bool = False) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _existing_daemon_alive(pid_file):
        return 0

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_cleanup_pid, pid_file)

    server = None
    if not once:
        server = _start_api_server(api_port)
        if server is not None:
            atexit.register(server.shutdown)
            atexit.register(server.server_close)

    effective_port = api_port if server is not None else None
    _atomic_write(STATE_FILE, build_live_state(interval_s, stale_after_s, effective_port))

    if announce:
        try:
            _announce_presence()
        except Exception:
            pass

    try:
        if once:
            return 0
        _heartbeat_loop(interval_s, stale_after_s, effective_port)
    except KeyboardInterrupt:
        pass
    finally:
        _write_offline_snapshot()

    return 0


def _existing_daemon_alive(pid_file: Path) -> bool:
    """Check if a daemon is already running from the PID file."""
    if not pid_file.exists():
        return False
    try:
        old_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if _pid_alive(old_pid):
        print(f"consultant bridge already running (PID {old_pid})", flush=True)
        return True
    return False


def _heartbeat_loop(interval_s, stale_after_s, effective_port):
    """Continuously write live state at the heartbeat interval."""
    while True:
        try:
            _atomic_write(STATE_FILE, build_live_state(interval_s, stale_after_s, effective_port))
        except Exception as exc:
            print(
                f"consultant bridge heartbeat write failed for {STATE_FILE.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(interval_s)


def main() -> int:
    global CONSULTANT_ID, DISPLAY_NAME, MODEL_NAME, SOURCE_NAME, STATE_FILE, PID_FILE

    parser = argparse.ArgumentParser(description="Consultant live presence bridge")
    parser.add_argument("--id", type=str, default=CONSULTANT_ID, help="Consultant ID (key in agent_profiles.json)")
    parser.add_argument("--display-name", type=str, default=None, help="Human-readable display name")
    parser.add_argument("--model", type=str, default=None, help="Model name")
    parser.add_argument("--source", type=str, default=None, help="Start trigger (e.g. CC-Start, GC-Start)")
    parser.add_argument("--state-file", type=str, default=None, help="State file path")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="Heartbeat interval in seconds")
    parser.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER_S, help="Seconds before state is stale")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help="Port for consultant bridge JSON API")
    parser.add_argument("--pid-file", type=str, default=None, help="PID file path for singleton control")
    parser.add_argument("--status", action="store_true", help="Print current consultant view as JSON")
    parser.add_argument("--once", action="store_true", help="Write one heartbeat snapshot and exit")
    parser.add_argument("--no-announce", action="store_true", help="Skip startup bus announcement")
    args = parser.parse_args()

    # Apply identity overrides to module globals
    CONSULTANT_ID = args.id
    if args.display_name:
        DISPLAY_NAME = args.display_name
    if args.model:
        MODEL_NAME = args.model
    if args.source:
        SOURCE_NAME = args.source
    if args.state_file:
        STATE_FILE = Path(args.state_file).resolve()
    else:
        # Derive state file from consultant ID
        if CONSULTANT_ID != "consultant":
            STATE_FILE = DATA_DIR / f"{CONSULTANT_ID}_state.json"
    pid_path = Path(args.pid_file).resolve() if args.pid_file else (
        PID_FILE if CONSULTANT_ID == "consultant"
        else DATA_DIR / f"{CONSULTANT_ID}_bridge.pid"
    )
    PID_FILE = pid_path

    if args.status:
        print(json.dumps(get_consultant_view(), indent=2))
        return 0

    return run_daemon(
        interval_s=max(0.5, args.interval),
        stale_after_s=max(1.0, args.stale_after),
        api_port=max(1, args.api_port),
        pid_file=pid_path,
        announce=not args.no_announce,
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
