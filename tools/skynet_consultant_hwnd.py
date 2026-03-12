#!/usr/bin/env python3
"""Consultant HWND probe and bootstrap helper."""  # signed: consultant

from __future__ import annotations

import argparse
import ctypes
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CANDIDATE_FILE = DATA_DIR / "consultant_window_candidates.json"
NEW_CHAT_SCRIPT = ROOT / "tools" / "new_chat.ps1"
sys.path.insert(0, str(ROOT))

from tools.skynet_delivery import (  # noqa: E402
    _enum_vscode_hwnds,
    _get_candidate_window_text,
    _get_window_scan_flags,
    _get_window_title,
    _reserved_skynet_hwnds,
    validate_hwnd,
)

IDENTITIES: Dict[str, Dict[str, Any]] = {
    "consultant": {
        "display_name": "Codex Consultant",
        "session_marker": "cc-start",
        "identity_markers": (
            "codex consultant",
            "you are the codex consultant",
            "sender: consultant",
            "sender=consultant",
            "signed:consultant",
        ),
        "pane_header_markers": ("codex",),
        "pane_model_markers": (),
        "pane_agent_markers": (),
        "pane_action_markers": ("run cc-start bootstrap", "cc-start bootstrap", "cc-start"),
        "reject_markers": (
            "gc-start",
            "gemini consultant",
            "sender: gemini_consultant",
            "sender=gemini_consultant",
            "signed:gemini_consultant",
        ),
    },
    "gemini_consultant": {
        "display_name": "Gemini Consultant",
        "session_marker": "gc-start",
        "identity_markers": (
            "gemini consultant",
            "you are the gemini consultant",
            "sender: gemini_consultant",
            "sender=gemini_consultant",
            "signed:gemini_consultant",
        ),
        "pane_header_markers": ("gemini consultant",),
        "pane_model_markers": ("pick model, gemini 3.1 pro (preview)", "pick model, gemini"),
        "pane_agent_markers": ("autopilot (preview)",),
        "pane_action_markers": ("gc-start",),
        "reject_markers": (
            "cc-start",
            "codex consultant",
            "sender: consultant",
            "sender=consultant",
            "signed:consultant",
        ),
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity_spec(consultant_id: str) -> Dict[str, Any]:
    spec = IDENTITIES.get(str(consultant_id or "").strip())
    if not spec:
        raise ValueError(f"Unknown consultant id: {consultant_id}")
    return spec


def _safe_excerpt(text: str, limit: int = 240) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _candidate_registry() -> Dict[str, Any]:
    if not CANDIDATE_FILE.exists():
        return {"candidates": []}
    try:
        data = json.loads(CANDIDATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"candidates": []}
    except Exception:
        return {"candidates": []}


def _write_candidate_registry(payload: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _slot_name(index: int, total: int) -> str:
    if total == 3:
        return ("left", "middle", "right")[index]
    if total == 2:
        return ("left", "right")[index]
    return f"slot_{index}"


def _scan_window_bands(hwnd: int, band_count: int = 3) -> List[Dict[str, Any]]:
    try:
        import comtypes
        import comtypes.client  # noqa: F401
        from comtypes.gen import UIAutomationClient as UIA
    except Exception:
        return []

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass

    try:
        uia = comtypes.CoCreateInstance(
            comtypes.GUID("{ff48dba4-60ef-4201-aa87-54103eef594e}"),
            interface=UIA.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        if not root:
            return []
        win_rect = root.CurrentBoundingRectangle
        left = int(win_rect.left)
        right = int(win_rect.right)
        width = max(1, right - left)
        bands: List[Dict[str, Any]] = []
        for idx in range(band_count):
            band_left = left + int(width * idx / band_count)
            band_right = left + int(width * (idx + 1) / band_count)
            bands.append({
                "index": idx,
                "slot": _slot_name(idx, band_count),
                "left": band_left,
                "right": band_right,
                "names": [],
            })

        elements = root.FindAll(4, uia.CreateTrueCondition())
        for i in range(elements.Length):
            el = elements.GetElement(i)
            try:
                name = str(el.CurrentName or "").strip()
                if not name:
                    continue
                rect = el.CurrentBoundingRectangle
                if rect.right <= rect.left:
                    continue
                center_x = (float(rect.left) + float(rect.right)) / 2.0
                for band in bands:
                    if center_x >= band["left"] and center_x < band["right"]:
                        band["names"].append(name)
                        break
            except Exception:
                continue

        results: List[Dict[str, Any]] = []
        for band in bands:
            seen = set()
            unique_names = []
            for name in band["names"]:
                if name in seen:
                    continue
                seen.add(name)
                unique_names.append(name)
            haystack = "\n".join(unique_names).lower()
            model = next((name for name in unique_names if name.lower().startswith("pick model,")), "")
            session_target = next((name for name in unique_names if name.lower().startswith("set session target")), "")
            permissions = next((name for name in unique_names if name.lower().startswith("set permissions")), "")
            results.append({
                **band,
                "names": unique_names,
                "haystack": haystack,
                "model": model,
                "session_target": session_target,
                "permissions": permissions,
            })
        return results
    except Exception:
        return []


def _best_band_identity(hwnd: int, consultant_id: str) -> Dict[str, Any]:
    spec = _identity_spec(consultant_id)
    best: Dict[str, Any] = {
        "strong": False,
        "score": 0,
        "slot": "",
        "markers": [],
        "reject_markers": [],
        "model": "",
        "session_target": "",
        "permissions": "",
        "names_excerpt": "",
    }
    for band in _scan_window_bands(hwnd):
        haystack = str(band.get("haystack") or "")
        actual_model = str(band.get("model") or "")
        actual_session_target = str(band.get("session_target") or "")
        actual_permissions = str(band.get("permissions") or "")
        markers: List[str] = []
        reject_markers: List[str] = []
        score = 0
        strong = False
        matched_pane_model = False
        matched_pane_agent = False

        for marker in spec.get("pane_model_markers", ()):
            if marker in haystack:
                markers.append(f"pane_model:{marker}")
                score += 6
                strong = True
                matched_pane_model = True
        for marker in spec.get("pane_header_markers", ()):
            if marker in haystack:
                markers.append(f"pane_header:{marker}")
                score += 4
        for marker in spec.get("pane_agent_markers", ()):
            if marker in haystack:
                markers.append(f"pane_agent:{marker}")
                score += 3
                strong = True
                matched_pane_agent = True
        for marker in spec.get("pane_action_markers", ()):
            if marker in haystack:
                markers.append(f"pane_action:{marker}")
                score += 4
                strong = True
        for marker in spec["identity_markers"]:
            if marker in haystack:
                markers.append(f"pane_identity:{marker}")
                score += 4
                strong = True
        for marker in spec["reject_markers"]:
            if marker in haystack:
                reject_markers.append(marker)
                score -= 5

        # Prefer panes with real consultant controls over transcript-only mentions.
        if matched_pane_model and actual_model:
            markers.append("pane_signal:model_control")
            score += 6
            strong = True
        if (matched_pane_model or matched_pane_agent) and actual_session_target:
            markers.append("pane_signal:session_target")
            score += 4
            strong = True
        if matched_pane_agent and actual_permissions:
            markers.append("pane_signal:permissions")
            score += 4
            strong = True

        if score > int(best.get("score", 0)):
            best = {
                "strong": strong,
                "score": score,
                "slot": band.get("slot", ""),
                "markers": markers,
                "reject_markers": reject_markers,
                "model": band.get("model", ""),
                "session_target": band.get("session_target", ""),
                "permissions": band.get("permissions", ""),
                "names_excerpt": _safe_excerpt("\n".join(band.get("names", [])[:40]), limit=600),
            }
    return best  # signed: consultant


def _score_consultant_candidate(hwnd: int, consultant_id: str) -> Dict[str, Any]:
    spec = _identity_spec(consultant_id)
    validation = validate_hwnd(hwnd, f"consultant:{consultant_id}")
    title = str(validation.get("title") or _get_window_title(hwnd) or "")
    title_lower = title.lower()
    band_identity = _best_band_identity(hwnd, consultant_id)
    if not validation.get("valid"):
        return {
            "hwnd": int(hwnd),
            "accepted": False,
            "visible_surface": False,
            "shared_surface": False,
            "shared_parent_hwnd": 0,
            "pane_slot": "",
            "pane_markers": band_identity.get("markers", []),
            "pane_model": band_identity.get("model", ""),
            "pane_session_target": band_identity.get("session_target", ""),
            "pane_permissions": band_identity.get("permissions", ""),
            "pane_names_excerpt": band_identity.get("names_excerpt", ""),
            "score": -999,
            "title": title,
            "markers": [],
            "reject_markers": [],
            "reason": "hwnd_validation_failed",
            "validation": validation,
            "content_excerpt": "",
            "scan_flags": _get_window_scan_flags(hwnd),
        }

    reserved = _reserved_skynet_hwnds()
    if isinstance(reserved, dict):
        reserved_role = str(reserved.get(hwnd) or "reserved")
        is_reserved = hwnd in reserved
    else:
        reserved_role = "reserved"
        is_reserved = hwnd in reserved

    content = str(_get_candidate_window_text(hwnd) or "")
    content_lower = content.lower()
    scan_flags = _get_window_scan_flags(hwnd)
    markers: List[str] = []
    reject_markers: List[str] = []
    score = 0

    if spec["session_marker"] in content_lower:
        markers.append(f"content:{spec['session_marker']}")
        score += 4
    if spec["session_marker"] in title_lower:
        markers.append(f"title:{spec['session_marker']}")
        score += 1

    for marker in spec["identity_markers"]:
        if marker in content_lower:
            markers.append(f"content:{marker}")
            score += 3
        elif marker in title_lower:
            markers.append(f"title:{marker}")
            score += 1

    for marker in spec["reject_markers"]:
        if marker in content_lower or marker in title_lower:
            reject_markers.append(marker)
            score -= 4

    if scan_flags.get("agent_ok"):
        markers.append("scan:agent_ok")
        score += 1
    if str(scan_flags.get("agent") or "").lower().find("copilot cli") >= 0:
        markers.append("scan:copilot_cli")
        score += 1

    pane_override = bool(band_identity.get("strong") and int(band_identity.get("score", 0) or 0) >= 7)
    if pane_override:
        reject_markers = []

    for pane_marker in band_identity.get("markers", []):
        markers.append(f"{band_identity.get('slot') or 'band'}:{pane_marker}")
    for pane_reject in band_identity.get("reject_markers", []):
        reject_markers.append(f"{band_identity.get('slot') or 'band'}:{pane_reject}")
    score += int(band_identity.get("score", 0) or 0)
    if pane_override:
        reject_markers = []

    strong_identity = any(item.startswith("content:") for item in markers) or bool(band_identity.get("strong"))
    shared_surface = bool(is_reserved and strong_identity and score >= 7 and not reject_markers)
    accepted = bool((not is_reserved) and strong_identity and score >= 7 and not reject_markers)
    visible_surface = bool(accepted or shared_surface)
    if accepted:
        reason = "accepted"
    elif shared_surface:
        reason = "shared_consultant_surface_in_reserved_window"
    elif is_reserved:
        reason = "reserved_skynet_window"
        score = -100
    else:
        reason = "markers_insufficient"

    return {
        "hwnd": int(hwnd),
        "accepted": accepted,
        "visible_surface": visible_surface,
        "shared_surface": shared_surface,
        "shared_parent_hwnd": int(hwnd) if shared_surface else 0,
        "pane_slot": band_identity.get("slot", ""),
        "pane_markers": band_identity.get("markers", []),
        "pane_model": band_identity.get("model", ""),
        "pane_session_target": band_identity.get("session_target", ""),
        "pane_permissions": band_identity.get("permissions", ""),
        "pane_names_excerpt": band_identity.get("names_excerpt", ""),
        "score": score,
        "title": title,
        "markers": markers,
        "reject_markers": reject_markers,
        "reason": reason,
        "validation": validation,
        "content_excerpt": _safe_excerpt(content),
        "scan_flags": scan_flags,
    }  # signed: consultant


def discover_consultant_hwnd(consultant_id: str) -> Dict[str, Any]:
    spec = _identity_spec(consultant_id)
    candidates = [
        _score_consultant_candidate(hwnd, consultant_id)
        for hwnd in _enum_vscode_hwnds()
    ]
    candidates.sort(
        key=lambda item: (
            int(item.get("accepted", False)),
            int(item.get("visible_surface", False)),
            int(item.get("score", -999)),
        ),
        reverse=True,
    )
    accepted = next((item for item in candidates if item.get("accepted")), None)
    visible_surface = next((item for item in candidates if item.get("visible_surface")), None)
    best = accepted or visible_surface or (candidates[0] if candidates else None)
    return {
        "consultant_id": consultant_id,
        "display_name": spec["display_name"],
        "accepted": bool(accepted),
        "hwnd": int(accepted.get("hwnd") or 0) if accepted else 0,
        "visible_surface": bool(accepted or visible_surface),
        "shared_parent_hwnd": int(best.get("shared_parent_hwnd") or 0) if best else 0,
        "pane_slot": str(best.get("pane_slot") or "") if best else "",
        "pane_model": str(best.get("pane_model") or "") if best else "",
        "pane_session_target": str(best.get("pane_session_target") or "") if best else "",
        "pane_permissions": str(best.get("pane_permissions") or "") if best else "",
        "pane_markers": list(best.get("pane_markers") or []) if best else [],
        "best_candidate": best,
        "candidates": candidates[:5],
        "checked_at": _now_iso(),
    }  # signed: consultant


def _record_open_candidate(record: Dict[str, Any]) -> None:
    registry = _candidate_registry()
    candidates = registry.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    candidates = [
        item for item in candidates
        if not (isinstance(item, dict) and item.get("consultant_id") == record.get("consultant_id"))
    ]
    candidates.append(record)
    registry["candidates"] = candidates[-8:]
    registry["updated_at"] = _now_iso()
    registry["updated_by"] = "consultant"
    _write_candidate_registry(registry)


def open_candidate_window(consultant_id: str, skip_empty_check: bool = False) -> Dict[str, Any]:
    if not NEW_CHAT_SCRIPT.exists():
        raise FileNotFoundError(f"new_chat.ps1 not found at {NEW_CHAT_SCRIPT}")

    before = set(_enum_vscode_hwnds())
    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(NEW_CHAT_SCRIPT),
        "-Layout",
        "consultant",
    ]
    if skip_empty_check:
        cmd.append("-SkipEmptyCheck")

    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    ok_match = re.search(r"\bOK HWND=(\d+)\b", stdout)
    hwnd = int(ok_match.group(1)) if ok_match else 0
    if hwnd <= 0:
        after = set(_enum_vscode_hwnds())
        new_hwnds = [item for item in after if item not in before]
        hwnd = int(new_hwnds[0]) if new_hwnds else 0

    scan_flags = _get_window_scan_flags(hwnd) if hwnd > 0 else {}
    title = _get_window_title(hwnd) if hwnd > 0 else ""
    record = {
        "consultant_id": consultant_id,
        "display_name": _identity_spec(consultant_id)["display_name"],
        "opened_at": _now_iso(),
        "hwnd": hwnd,
        "title": title,
        "scan_flags": scan_flags,
        "binding_status": "candidate_only",
        "truth_note": (
            "Detached Copilot CLI window opened. This is only a consultant candidate surface; "
            "it is not a bound consultant HWND until consultant markers are present and the "
            "test-first gate achieves direct delivery."
        ),
        "launcher_stdout": _safe_excerpt(stdout, limit=400),
        "launcher_stderr": _safe_excerpt(stderr, limit=400),
        "launcher_rc": int(result.returncode),
    }
    if hwnd > 0:
        _record_open_candidate(record)
    probe = discover_consultant_hwnd(consultant_id)
    return {
        "consultant_id": consultant_id,
        "success": bool(result.returncode == 0 and hwnd > 0),
        "hwnd": hwnd,
        "title": title,
        "scan_flags": scan_flags,
        "candidate_recorded": bool(hwnd > 0),
        "blocked": "BLOCKED:" in stdout and not ok_match,
        "probe_after_open": probe,
        "binding_status": record["binding_status"],
        "truth_note": record["truth_note"],
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "returncode": int(result.returncode),
    }  # signed: consultant


def main() -> int:
    parser = argparse.ArgumentParser(description="Consultant HWND helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="Discover an existing consultant window by transcript markers")
    p_probe.add_argument("--consultant-id", required=True, help="consultant or gemini_consultant")

    p_open = sub.add_parser("open", help="Open a dedicated consultant candidate window")
    p_open.add_argument("--consultant-id", required=True, help="consultant or gemini_consultant")
    p_open.add_argument("--skip-empty-check", action="store_true", help="Pass -SkipEmptyCheck to new_chat.ps1")

    args = parser.parse_args()

    if args.command == "probe":
        result = discover_consultant_hwnd(args.consultant_id)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "open":
        result = open_candidate_window(args.consultant_id, skip_empty_check=bool(args.skip_empty_check))
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
