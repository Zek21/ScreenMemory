#!/usr/bin/env python3
"""skynet_security_audit.py -- End-to-end security audit framework for Skynet.

Audits the full dispatch pipeline, bus message integrity, worker registry,
and configuration files for security vulnerabilities, tampering, and drift.

Usage:
    python tools/skynet_security_audit.py              # full audit
    python tools/skynet_security_audit.py --fix        # audit + auto-remediate
    python tools/skynet_security_audit.py --component dispatch  # audit one component
    python tools/skynet_security_audit.py --component bus
    python tools/skynet_security_audit.py --component registry
    python tools/skynet_security_audit.py --component config
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import sys
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TOOLS_DIR = ROOT / "tools"

WORKERS_FILE = DATA_DIR / "workers.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"
BRAIN_CONFIG = DATA_DIR / "brain_config.json"
AGENT_PROFILES = DATA_DIR / "agent_profiles.json"
TODOS_FILE = DATA_DIR / "todos.json"
DISPATCH_LOG = DATA_DIR / "dispatch_log.json"

_VSCODE_PROCESS_NAMES = ("Code - Insiders.exe", "Code.exe")
_GRID_BOUNDS = {"x": (0, 4000), "y": (0, 2000), "w": (200, 2000), "h": (200, 1500)}

# Bus message safety limits
BUS_MAX_PAYLOAD_BYTES = 10240  # 10KB
BUS_SUSPICIOUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"del\s+/[sS]",
    r"Stop-Process",
    r"taskkill",
    r"os\.kill",
    r"eval\(",
    r"exec\(",
    r"__import__",
    r"subprocess\.call",
    r"base64\.b64decode",
]


class AuditResult:
    """Accumulates audit findings."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.critical = 0
        self.details = []

    def ok(self, check: str, detail: str = ""):
        self.passed += 1
        self.details.append({"status": "PASS", "check": check, "detail": detail})

    def fail(self, check: str, detail: str = ""):
        self.failed += 1
        self.details.append({"status": "FAIL", "check": check, "detail": detail})

    def warn(self, check: str, detail: str = ""):
        self.warnings += 1
        self.details.append({"status": "WARN", "check": check, "detail": detail})

    def crit(self, check: str, detail: str = ""):
        self.critical += 1
        self.details.append({"status": "CRITICAL", "check": check, "detail": detail})

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "warnings": self.warnings,
            "critical": self.critical,
            "total": self.passed + self.failed + self.warnings + self.critical,
            "details": self.details,
        }


# ── Win32 Helpers ────────────────────────────────────────────────

def _is_window(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


def _get_window_pid(hwnd: int) -> int:
    try:
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _get_process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            if ok:
                return os.path.basename(buf.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _get_window_title(hwnd: int) -> str:
    try:
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def _is_vscode_hwnd(hwnd: int) -> tuple:
    """Returns (is_valid, pid, process_name, title)."""
    if not _is_window(hwnd):
        return False, 0, "", ""
    pid = _get_window_pid(hwnd)
    proc = _get_process_name(pid)
    title = _get_window_title(hwnd)
    is_vsc = any(proc.lower() == v.lower() for v in _VSCODE_PROCESS_NAMES) if proc else False
    has_title = "Visual Studio Code" in title or "VS Code" in title if title else False
    return (is_vsc and has_title), pid, proc, title


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── Audit Functions ──────────────────────────────────────────────

def _audit_dispatch_source_check(r, dispatch_src):
    """Run source-level security checks on dispatch pipeline."""
    if "validate_hwnd" in dispatch_src:
        r.ok("dispatch_hwnd_validation", "validate_hwnd() call found in dispatch pipeline")
    else:
        r.crit("dispatch_hwnd_validation",
               "NO validate_hwnd() in dispatch_to_worker -- HWND injection possible")

    if "Clipboard]::SetText" in dispatch_src and "savedClip" in dispatch_src:
        r.ok("clipboard_save_restore", "Clipboard save/restore pattern found")
    else:
        r.fail("clipboard_save_restore", "Clipboard may not be saved/restored during dispatch")

    if "_get_self_identity" in dispatch_src or "self-dispatch" in dispatch_src.lower():
        r.ok("self_dispatch_guard", "Self-dispatch prevention found")
    else:
        r.warn("self_dispatch_guard", "No explicit self-dispatch guard detected")

    if "sign_dispatch" in dispatch_src or "hmac" in dispatch_src.lower():
        r.ok("dispatch_signing", "Dispatch signing/HMAC found")
    else:
        r.warn("dispatch_signing", "No HMAC dispatch signing detected")


def _audit_delivery_module(r):
    """Audit skynet_delivery.py for HWND validation."""
    delivery_path = TOOLS_DIR / "skynet_delivery.py"
    if delivery_path.exists():
        delivery_src = delivery_path.read_text(encoding="utf-8")
        if "validate_hwnd" in delivery_src and "IsWindow" in delivery_src:
            r.ok("delivery_hwnd_validation",
                 "skynet_delivery.py has validate_hwnd with IsWindow+process checks")
        else:
            r.fail("delivery_hwnd_validation",
                   "skynet_delivery.py missing proper HWND validation")
    else:
        r.fail("delivery_hwnd_validation", "skynet_delivery.py not found")


def audit_dispatch_pipeline() -> AuditResult:
    """Audit the full dispatch pipeline for security gaps."""
    r = AuditResult()
    dispatch_src = (TOOLS_DIR / "skynet_dispatch.py").read_text(encoding="utf-8")

    _audit_dispatch_source_check(r, dispatch_src)

    # Dispatch log integrity
    if DISPATCH_LOG.exists():
        log = _load_json(DISPATCH_LOG)
        if isinstance(log, list):
            r.ok("dispatch_log_integrity", f"Dispatch log has {len(log)} entries")
        else:
            r.warn("dispatch_log_integrity", "Dispatch log exists but is not a valid list")
    else:
        r.warn("dispatch_log_integrity", "No dispatch log file found")

    _audit_delivery_module(r)

    if WORKERS_FILE.exists():
        r.ok("workers_file_exists", f"workers.json exists at {WORKERS_FILE}")
    else:
        r.fail("workers_file_exists", "workers.json not found")

    return r


def _analyze_bus_messages(r, messages):
    """Analyze bus messages for security issues (size, injection, unknown senders)."""
    valid_senders = {"alpha", "beta", "gamma", "delta", "orchestrator",
                     "convene-gate", "monitor", "system", "delivery",
                     "consultant", "gemini_consultant", "god_bridge",
                     "watchdog", "self-prompt", "learner", "brain"}

    oversized = 0
    suspicious = 0
    spoofed_topics = 0

    for msg in messages:
        if not isinstance(msg, dict):
            r.fail("bus_message_format", f"Non-dict message found: {type(msg)}")
            continue

        content = msg.get("content", "")
        sender = msg.get("sender", "")

        if len(json.dumps(msg)) > BUS_MAX_PAYLOAD_BYTES:
            oversized += 1

        for pattern in BUS_SUSPICIOUS_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                suspicious += 1
                r.warn("bus_suspicious_content",
                       f"Suspicious pattern '{pattern}' from sender={sender}: "
                       f"{content[:100]}")
                break

        if sender and sender not in valid_senders:
            spoofed_topics += 1

    if oversized:
        r.warn("bus_oversized_payloads", f"{oversized} messages exceed {BUS_MAX_PAYLOAD_BYTES}B")
    else:
        r.ok("bus_payload_sizes", "All messages within size limits")

    if suspicious:
        r.fail("bus_injection_attempts", f"{suspicious} messages contain suspicious patterns")
    else:
        r.ok("bus_injection_scan", "No injection patterns detected")

    if spoofed_topics:
        r.warn("bus_unknown_senders", f"{spoofed_topics} messages from unknown senders")
    else:
        r.ok("bus_sender_verification", "All senders are known entities")


def audit_bus_messages(limit: int = 100) -> AuditResult:
    """Scan recent bus messages for security issues."""
    r = AuditResult()

    try:
        import urllib.request
        url = f"http://localhost:8420/bus/messages?limit={limit}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            messages = json.loads(resp.read())
    except Exception as e:
        r.warn("bus_connectivity", f"Cannot reach bus: {e}")
        return r

    if not isinstance(messages, list):
        r.fail("bus_response_format", "Bus /messages did not return a list")
        return r

    r.ok("bus_connectivity", f"Retrieved {len(messages)} bus messages")
    _analyze_bus_messages(r, messages)

    return r


def _validate_worker_entry(r, w, seen_names, seen_hwnds, stale_entries):
    """Validate a single worker registry entry."""
    name = w.get("name", "?")
    hwnd = w.get("hwnd", 0)

    if name in seen_names:
        r.fail("registry_duplicate_name", f"Duplicate worker name: {name}")
    seen_names.add(name)

    if hwnd in seen_hwnds:
        r.fail("registry_duplicate_hwnd", f"Duplicate HWND {hwnd} for {name}")
    seen_hwnds.add(hwnd)

    if hwnd:
        valid, pid, proc, title = _is_vscode_hwnd(hwnd)
        if valid:
            r.ok(f"registry_hwnd_{name}", f"HWND {hwnd} is alive, pid={pid}, proc={proc}")
        elif _is_window(hwnd):
            r.warn(f"registry_hwnd_{name}", f"HWND {hwnd} alive but NOT VS Code: proc={proc}")
        else:
            r.fail(f"registry_hwnd_{name}", f"HWND {hwnd} is DEAD (IsWindow=False)")
            stale_entries.append(name)
    else:
        r.fail(f"registry_hwnd_{name}", f"HWND is zero for {name}")

    # Grid position bounds check
    x, y = w.get("x", 0), w.get("y", 0)
    width, h = w.get("w", 0), w.get("h", 0)
    for label, val, bounds in [("x", x, _GRID_BOUNDS["x"]), ("y", y, _GRID_BOUNDS["y"]),
                                ("w", width, _GRID_BOUNDS["w"]), ("h", h, _GRID_BOUNDS["h"])]:
        if not (bounds[0] <= val <= bounds[1]):
            r.warn(f"registry_grid_{name}", f"{label}={val} out of bounds")


def audit_worker_registry(auto_fix: bool = False) -> AuditResult:
    """Validate the worker registry (data/workers.json)."""
    r = AuditResult()

    if not WORKERS_FILE.exists():
        r.crit("registry_exists", "workers.json not found")
        return r

    data = _load_json(WORKERS_FILE)
    if data is None:
        r.crit("registry_valid_json", "workers.json is not valid JSON")
        return r

    r.ok("registry_valid_json", "workers.json parses as valid JSON")

    workers = data.get("workers", data) if isinstance(data, dict) else data
    if not isinstance(workers, list):
        r.fail("registry_format", "workers field is not a list")
        return r

    r.ok("registry_format", f"Registry contains {len(workers)} workers")

    seen_names = set()
    seen_hwnds = set()
    stale_entries = []

    for w in workers:
        _validate_worker_entry(r, w, seen_names, seen_hwnds, stale_entries)

    if stale_entries and auto_fix:
        r.warn("registry_auto_fix",
               f"Auto-fix: would remove stale entries {stale_entries} (not implemented yet)")

    return r


def _audit_brain_config(r):
    """Audit brain_config.json for required keys and compliance guard."""
    if not BRAIN_CONFIG.exists():
        r.warn("config_brain_exists", "brain_config.json not found")
        return
    bc = _load_json(BRAIN_CONFIG)
    if bc is None:
        r.fail("config_brain_json", "brain_config.json is not valid JSON")
        return
    r.ok("config_brain_json", "brain_config.json is valid JSON")
    for key in ["difficulty_thresholds", "routing", "learning", "compliance"]:
        if key in bc:
            r.ok(f"config_brain_{key}", f"Required key '{key}' present")
        else:
            r.fail(f"config_brain_{key}", f"Required key '{key}' MISSING")
    comp = bc.get("compliance", {})
    if comp.get("guard_enabled", False):
        r.ok("config_compliance_guard", "Compliance guard is enabled")
    else:
        r.warn("config_compliance_guard", "Compliance guard is DISABLED")


def _audit_orchestrator_config(r):
    """Audit orchestrator.json for valid HWND and role."""
    if not ORCH_FILE.exists():
        r.warn("config_orch_exists", "orchestrator.json not found")
        return
    oc = _load_json(ORCH_FILE)
    if oc is None:
        r.fail("config_orch_json", "orchestrator.json is not valid JSON")
        return
    r.ok("config_orch_json", "orchestrator.json is valid JSON")
    hwnd = oc.get("hwnd") or oc.get("orchestrator_hwnd") or 0
    if hwnd:
        if _is_window(hwnd):
            r.ok("config_orch_hwnd", f"Orchestrator HWND {hwnd} is alive")
        else:
            r.fail("config_orch_hwnd", f"Orchestrator HWND {hwnd} is DEAD")
    else:
        r.fail("config_orch_hwnd", "No orchestrator HWND set")
    if oc.get("role") == "orchestrator":
        r.ok("config_orch_role", "Role is correctly 'orchestrator'")
    else:
        r.warn("config_orch_role", f"Role is '{oc.get('role')}' not 'orchestrator'")


def _audit_agent_profiles(r):
    """Audit agent_profiles.json for completeness."""
    if not AGENT_PROFILES.exists():
        r.warn("config_profiles_exists", "agent_profiles.json not found")
        return
    ap = _load_json(AGENT_PROFILES)
    if ap is None:
        r.fail("config_profiles_json", "agent_profiles.json is not valid JSON")
        return
    r.ok("config_profiles_json", "agent_profiles.json is valid JSON")
    expected_agents = {"orchestrator", "alpha", "beta", "gamma", "delta"}
    found = set()
    if isinstance(ap, dict):
        profiles = ap.get("profiles", ap)
        if isinstance(profiles, dict):
            found = set(profiles.keys())
        elif isinstance(profiles, list):
            found = {p.get("name", "") for p in profiles if isinstance(p, dict)}
    missing = expected_agents - found
    if missing:
        r.warn("config_profiles_completeness", f"Missing agent profiles: {missing}")
    else:
        r.ok("config_profiles_completeness", "All expected agents have profiles")


def audit_config_files() -> AuditResult:
    """Validate critical configuration files."""
    r = AuditResult()

    _audit_brain_config(r)
    _audit_orchestrator_config(r)
    _audit_agent_profiles(r)

    # todos.json (optional)
    if TODOS_FILE.exists():
        td = _load_json(TODOS_FILE)
        if td is None:
            r.fail("config_todos_json", "todos.json is not valid JSON")
        else:
            r.ok("config_todos_json", "todos.json is valid JSON")

    return r


def full_audit(auto_fix: bool = False) -> dict:
    """Run ALL security audits and return consolidated results."""
    combined = AuditResult()
    component_results = {}

    for name, fn in [
        ("dispatch", audit_dispatch_pipeline),
        ("bus", lambda: audit_bus_messages(100)),
        ("registry", lambda: audit_worker_registry(auto_fix)),
        ("config", audit_config_files),
    ]:
        try:
            result = fn()
            component_results[name] = result.to_dict()
            combined.passed += result.passed
            combined.failed += result.failed
            combined.warnings += result.warnings
            combined.critical += result.critical
            combined.details.extend(result.details)
        except Exception as e:
            combined.fail(f"{name}_audit_error", f"Audit crashed: {e}")
            component_results[name] = {"error": str(e)}

    out = combined.to_dict()
    out["components"] = component_results
    return out


def main():
    parser = argparse.ArgumentParser(description="Skynet Security Audit")
    parser.add_argument("--fix", action="store_true", help="Auto-remediate fixable issues")
    parser.add_argument("--component", type=str, default=None,
                        choices=["dispatch", "bus", "registry", "config"],
                        help="Audit a single component")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.component:
        fns = {
            "dispatch": audit_dispatch_pipeline,
            "bus": lambda: audit_bus_messages(100),
            "registry": lambda: audit_worker_registry(args.fix),
            "config": audit_config_files,
        }
        result = fns[args.component]()
        data = result.to_dict()
    else:
        data = full_audit(auto_fix=args.fix)

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        total = data["passed"] + data["failed"] + data["warnings"] + data.get("critical", 0)
        print(f"\n{'='*60}")
        print(f"SKYNET SECURITY AUDIT {'(' + args.component + ')' if args.component else ''}")
        print(f"{'='*60}")
        print(f"  PASS: {data['passed']}  FAIL: {data['failed']}  "
              f"WARN: {data['warnings']}  CRITICAL: {data.get('critical', 0)}  "
              f"TOTAL: {total}")
        print(f"{'='*60}")
        for d in data["details"]:
            icon = {"PASS": "+", "FAIL": "X", "WARN": "!", "CRITICAL": "!!"}
            print(f"  [{icon.get(d['status'], '?')}] {d['status']}: {d['check']}")
            if d.get("detail"):
                print(f"      {d['detail'][:120]}")
        print()

    return 1 if data.get("critical", 0) > 0 or data["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
