#!/usr/bin/env python3
"""
skynet_consultant_bridge.py -- live presence bridge for Codex Consultant.

Truthful semantics:
- consultant remains an advisory, non-routable identity
- LIVE means this bridge process is actively heartbeating now
- DECLARED means the consultant profile exists but no live bridge is running
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
from datetime import datetime, timezone
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
DEFAULT_INTERVAL_S = 2.0
DEFAULT_STALE_AFTER_S = 8.0
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
        "display_name": profile.get("name", "Codex Consultant"),
        "role": profile.get("role", "Codex Consultant -- Co-Equal Advisory Peer"),
        "model": profile.get("model", "GPT-5 Codex"),
        "kind": profile.get("kind", "advisor"),
        "capabilities": profile.get("capabilities", []),
        "specializations": profile.get("specializations", []),
        "declared_status": profile.get("current_status", "REGISTERED (profile-only; not a live backend agent)"),
    }


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
                     stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Dict[str, Any]:
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
        "transport": "cc-start-bridge",
        "source": "CC-Start",
        "status": "LIVE",
        "live": True,
        "bridge_pid": os.getpid(),
        "started_at": STARTED_AT,
        "last_heartbeat": _now_iso(),
        "heartbeat_interval_s": interval_s,
        "stale_after_s": stale_after_s,
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
        "transport": "cc-start-bridge",
        "source": raw.get("source", "CC-Start"),
        "declared": True,
        "declared_status": profile["declared_status"],
        "live": False,
        "status": "DECLARED",
        "bridge_pid": raw.get("bridge_pid"),
        "started_at": raw.get("started_at"),
        "last_heartbeat": raw.get("last_heartbeat"),
        "heartbeat_interval_s": raw.get("heartbeat_interval_s", DEFAULT_INTERVAL_S),
        "stale_after_s": raw.get("stale_after_s", DEFAULT_STALE_AFTER_S),
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
            "CODEX CONSULTANT LIVE -- CC-Start bridge active. "
            "Advisory peer is visible in consultant live surfaces. "
            "Routable=false."
        ),
        "metadata": {
            "display_name": "Codex Consultant",
            "kind": "advisor",
            "transport": "cc-start-bridge",
            "routable": "false",
        },
    }, timeout=2.0)


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


def _cleanup_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def run_daemon(interval_s: float = DEFAULT_INTERVAL_S,
               stale_after_s: float = DEFAULT_STALE_AFTER_S,
               announce: bool = True,
               once: bool = False) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            old_pid = 0
        if _pid_alive(old_pid):
            print(f"consultant bridge already running (PID {old_pid})", flush=True)
            return 0

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_cleanup_pid)

    if announce:
        _announce_presence()

    try:
        while True:
            _atomic_write(STATE_FILE, build_live_state(interval_s, stale_after_s))
            if once:
                return 0
            time.sleep(interval_s)
    except KeyboardInterrupt:
        pass
    finally:
        _write_offline_snapshot()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Consultant live presence bridge")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="Heartbeat interval in seconds")
    parser.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER_S, help="Seconds before state is stale")
    parser.add_argument("--status", action="store_true", help="Print current consultant view as JSON")
    parser.add_argument("--once", action="store_true", help="Write one heartbeat snapshot and exit")
    parser.add_argument("--no-announce", action="store_true", help="Skip startup bus announcement")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(get_consultant_view(), indent=2))
        return 0

    return run_daemon(
        interval_s=max(0.5, args.interval),
        stale_after_s=max(1.0, args.stale_after),
        announce=not args.no_announce,
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
