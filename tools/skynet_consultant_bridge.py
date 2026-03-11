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
- consultant remains an advisory, non-routable identity
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
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROFILE_FILE = DATA_DIR / "agent_profiles.json"
STATE_FILE = DATA_DIR / "consultant_state.json"
PID_FILE = DATA_DIR / "consultant_bridge.pid"
SKYNET_URL = "http://localhost:8420"
CONSULTANT_ID = "consultant"
DISPLAY_NAME = "Codex Consultant"
MODEL_NAME = "GPT-5 Codex"
SOURCE_NAME = "CC-Start"
DEFAULT_INTERVAL_S = 2.0
DEFAULT_STALE_AFTER_S = 8.0
DEFAULT_API_PORT = 8422
STARTED_AT = datetime.now(timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class ConsultantApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/consultant", "/consultants"):
            self._json_response({"consultant": get_consultant_view()})
            return
        if self.path == "/health":
            self._json_response({
                "status": "ok",
                "service": "consultant-bridge",
                "timestamp": _now_iso(),
            })
            return
        self.send_error(404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
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
    return {
        "id": CONSULTANT_ID,
        "display_name": profile["display_name"],
        "role": profile["role"],
        "model": profile["model"],
        "kind": "advisor",
        "backend_managed": False,
        "routable": False,
        "requires_hwnd": False,
        "transport": f"{SOURCE_NAME.lower()}-bridge",
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
    }


def get_consultant_view() -> Dict[str, Any]:
    profile = _load_profile()
    raw = _read_json(STATE_FILE)
    view = {
        "id": CONSULTANT_ID,
        "display_name": profile["display_name"],
        "role": profile["role"],
        "model": profile["model"],
        "kind": "advisor",
        "backend_managed": False,
        "routable": False,
        "requires_hwnd": False,
        "transport": f"{SOURCE_NAME.lower()}-bridge",
        "source": raw.get("source", SOURCE_NAME),
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
    }

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

    return view


def _announce_presence() -> None:
    _http_post("/bus/publish", {
        "sender": CONSULTANT_ID,
        "topic": "orchestrator",
        "type": "identity_ack",
        "content": (
            f"{DISPLAY_NAME.upper()} LIVE -- {SOURCE_NAME} bridge active. "
            "Advisory peer is visible in consultant live surfaces. "
            "Routable=false."
        ),
        "metadata": {
            "display_name": DISPLAY_NAME,
            "kind": "advisor",
            "transport": f"{SOURCE_NAME.lower()}-bridge",
            "routable": "false",
        },
    }, timeout=2.0)

    # Also direct-prompt the orchestrator for immediate visibility
    try:
        from skynet_delivery import deliver_to_orchestrator
        deliver_to_orchestrator(
            f"[CONSULTANT LIVE] {DISPLAY_NAME} ({MODEL_NAME}) -- advisory peer online via {SOURCE_NAME}",
            sender=CONSULTANT_ID,
            also_bus=False,  # Bus post above is the durable record
        )
    except Exception:
        pass  # Non-critical, bus post above is sufficient


def relay_consultant_result(content: str, consultant_id: str = None) -> dict:
    """Relay a consultant bus result to the orchestrator via direct-prompt.

    Call this from bus watchers or relay daemons when a consultant posts a
    result with topic=orchestrator. This bridges the advisory-only bus post
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

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            old_pid = 0
        if _pid_alive(old_pid):
            print(f"consultant bridge already running (PID {old_pid})", flush=True)
            return 0

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_cleanup_pid, pid_file)

    server = None
    if not once:
        server = _start_api_server(api_port)
        if server is not None:
            atexit.register(server.shutdown)
            atexit.register(server.server_close)

    if announce:
        _announce_presence()

    try:
        while True:
            _atomic_write(
                STATE_FILE,
                build_live_state(interval_s, stale_after_s, api_port if server is not None else None),
            )
            if once:
                return 0
            time.sleep(interval_s)
    except KeyboardInterrupt:
        pass
    finally:
        _write_offline_snapshot()

    return 0


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
