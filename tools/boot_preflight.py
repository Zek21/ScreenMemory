#!/usr/bin/env python3
"""boot_preflight.py — Pre-flight validation for Skynet boot sequence.

Validates ALL INCIDENT 014 prerequisites before any worker window opens.
Auto-fixes what it can, reports what it can't.

Usage:
    python tools/boot_preflight.py           # Full check with details
    python tools/boot_preflight.py --fix     # Auto-fix what's possible
    python tools/boot_preflight.py --json    # Machine-readable output
    python tools/boot_preflight.py --quiet   # Pass/fail only

Exit codes:
    0 = all checks passed
    1 = critical check failed (boot should NOT proceed)
    2 = non-critical warning (boot can proceed with caution)

References: data/boot_config.json, INCIDENT 014
# signed: orchestrator
"""

import json, os, sys, socket, ctypes, re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
BOOT_CONFIG = os.path.join(DATA, "boot_config.json")

def parse_jsonc(text):
    """Parse JSONC (JSON with comments and trailing commas) — VS Code settings format."""
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)

def load_boot_config():
    try:
        with open(BOOT_CONFIG) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
        print(f"[preflight] CRITICAL: Cannot load {BOOT_CONFIG}: {e}", file=sys.stderr)
        return {}  # return empty dict so callers degrade gracefully  # signed: gamma

def set_jsonc_bool_key(path, key, value):
    """Set a boolean key in a VS Code JSONC settings file without requiring full JSONC preservation."""
    raw = open(path, encoding="utf-8").read()
    value_str = "true" if value else "false"
    pattern = re.compile(rf'("{re.escape(key)}"\s*:\s*)(true|false|null)', re.IGNORECASE)
    replaced, count = pattern.subn(rf"\1{value_str}", raw, count=1)
    if count:
        with open(path, "w", encoding="utf-8") as f:
            f.write(replaced)
        return

    idx = raw.rfind("}")
    if idx < 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write("{\n" + f'  "{key}": {value_str}\n' + "}\n")
        return

    before = raw[:idx].rstrip()
    after = raw[idx:].lstrip()
    separator = "\n" if before.endswith("{") or before.endswith(",") else ",\n"
    updated = before + separator + f'  "{key}": {value_str}\n' + after
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)

def check_boolean_setting(key, desired, prereq_id, name, severity, fix=False, rationale=""):
    """Check a boolean VS Code setting in user/workspace settings and optionally fix it."""
    settings_path = os.path.join(
        os.environ.get("APPDATA", ""),
        "Code - Insiders", "User", "settings.json"
    )
    workspace_settings = os.path.join(REPO, ".vscode", "settings.json")

    results = []
    for label, path in [("user", settings_path), ("workspace", workspace_settings)]:
        if not os.path.exists(path):
            results.append({"location": label, "status": "MISSING", "path": path})
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            data = parse_jsonc(content)
            value = data.get(key)

            if value is desired:
                results.append({"location": label, "status": "PASS", "value": value})
            elif value in (True, False):
                if fix:
                    set_jsonc_bool_key(path, key, desired)
                    results.append({"location": label, "status": "FIXED", "old": value, "new": desired})
                else:
                    results.append({
                        "location": label,
                        "status": "FAIL",
                        "value": value,
                        "fix": f"Set {key} to {str(desired).lower()} in {path}",
                    })
            elif value is None:
                if fix:
                    set_jsonc_bool_key(path, key, desired)
                    results.append({"location": label, "status": "FIXED", "old": None, "new": desired})
                else:
                    results.append({
                        "location": label,
                        "status": "MISSING_KEY",
                        "fix": f"Add {key}: {str(desired).lower()} to {path}",
                    })
        except Exception as e:
            results.append({"location": label, "status": "ERROR", "error": str(e)})

    all_pass = all(r["status"] in ("PASS", "FIXED") for r in results)
    payload = {
        "id": prereq_id,
        "name": name,
        "passed": all_pass,
        "severity": severity,
        "details": results,
    }
    if rationale:
        payload["rationale"] = rationale
    return payload

def check_isolation_option(fix=False):
    """PREREQ_001: isolationOption.enabled must be true."""
    return check_boolean_setting(
        key="github.copilot.chat.cli.isolationOption.enabled",
        desired=True,
        prereq_id="PREREQ_001",
        name="Isolation Option",
        severity="CRITICAL",
        fix=fix,
        rationale="Copilot CLI worktree/session behavior depends on this remaining enabled.",
    )

def check_chat_restore_setting(fix=False):
    """PREREQ_002: restoreLastPanelSession must be false to avoid stale copilotcli untitled restores."""
    return check_boolean_setting(
        key="chat.restoreLastPanelSession",
        desired=False,
        prereq_id="PREREQ_002",
        name="Chat Session Restore",
        severity="HIGH",
        fix=fix,
        rationale="Restoring the last chat panel session can reopen stale copilotcli:/untitled-* sessions and trigger provider resolution failures.",
    )

def check_port(port, name):
    """Check if a TCP port is reachable (IPv4 and IPv6)."""
    for family, host in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]:
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((host, port))
            s.close()
            return {"id": f"PORT_{port}", "name": name, "passed": True, 
                    "severity": "CRITICAL" if port == 8420 else "HIGH"}
        except OSError:
            pass  # signed: gamma
    return {"id": f"PORT_{port}", "name": name, "passed": False,
            "severity": "CRITICAL" if port == 8420 else "HIGH",
            "fix": f"Start the service on port {port}"}

def check_workers_json():
    """Check workers.json integrity."""
    path = os.path.join(DATA, "workers.json")
    if not os.path.exists(path):
        return {"id": "WORKERS_JSON", "name": "Worker Registry", "passed": True,
                "note": "No workers.json yet (fresh boot)"}
    try:
        with open(path) as f:
            data = json.load(f)
        workers = data.get("workers", data) if isinstance(data, dict) else data
        if not isinstance(workers, list):
            return {"id": "WORKERS_JSON", "name": "Worker Registry", "passed": False,
                    "severity": "HIGH", "error": "workers field is not a list"}
        
        user32 = ctypes.windll.user32
        alive = 0
        dead = 0
        for w in workers:
            hwnd = w.get("hwnd", 0)
            if hwnd and user32.IsWindow(hwnd):
                alive += 1
            else:
                dead += 1
        
        hwnds = [w.get("hwnd") for w in workers]
        dupes = len(hwnds) != len(set(hwnds))
        
        return {"id": "WORKERS_JSON", "name": "Worker Registry", "passed": not dupes,
                "alive": alive, "dead": dead, "total": len(workers),
                "duplicates": dupes, "severity": "HIGH" if dupes else "INFO"}
    except Exception as e:
        return {"id": "WORKERS_JSON", "name": "Worker Registry", "passed": False,
                "error": str(e), "severity": "HIGH"}

def check_ghost_type_fixes():
    """Verify INCIDENT 014 fixes are present in skynet_dispatch.py."""
    dispatch_path = os.path.join(REPO, "tools", "skynet_dispatch.py")
    if not os.path.exists(dispatch_path):
        return {"id": "GHOST_TYPE", "name": "Ghost-Type Fixes", "passed": False,
                "severity": "CRITICAL", "error": "skynet_dispatch.py not found"}
    
    with open(dispatch_path, encoding="utf-8") as f:
        content = f.read()
    
    checks = {
        "hardware_enter": "keybd_event" in content or "HardwareEnter" in content,
        "accessibility_filter": "not accessible" in content or "screen reader" in content,
        "chrome_render_fallback": "FindRender" in content or "Chrome_RenderWidgetHostHWND" in content,
    }
    
    all_pass = all(checks.values())
    return {"id": "GHOST_TYPE", "name": "Ghost-Type INCIDENT 014 Fixes", "passed": all_pass,
            "severity": "CRITICAL", "checks": checks}

def check_guard_bypass_fixes():
    """Verify permissions guard has escape pre-clear."""
    guard_path = os.path.join(REPO, "tools", "guard_bypass.ps1")
    if not os.path.exists(guard_path):
        return {"id": "GUARD_BYPASS", "name": "Permissions Guard Fixes", "passed": False,
                "severity": "HIGH", "error": "guard_bypass.ps1 not found"}
    
    with open(guard_path, encoding="utf-8") as f:
        content = f.read()
    
    checks = {
        "escape_pre_clear": "escape" in content.lower(),
        "bypass_text_cleanup": "ctrl" in content.lower() and "backspace" in content.lower(),
    }
    
    all_pass = all(checks.values())
    return {"id": "GUARD_BYPASS", "name": "Permissions Guard Fixes", "passed": all_pass,
            "severity": "HIGH", "checks": checks}

def check_empty_files():
    """Find 0-byte stale files that should be cleaned up."""
    empty = []
    for root, dirs, files in os.walk(DATA):
        for f in files:
            if f.endswith((".md", ".txt")):
                path = os.path.join(root, f)
                if os.path.getsize(path) == 0:
                    empty.append(os.path.relpath(path, REPO))
    return {"id": "EMPTY_FILES", "name": "Empty/Stale Files", "passed": len(empty) == 0,
            "severity": "LOW", "empty_files": empty}

def run_preflight(fix=False, json_output=False, quiet=False):
    results = []
    
    # Critical checks
    results.append(check_isolation_option(fix=fix))
    results.append(check_chat_restore_setting(fix=fix))
    results.append(check_port(8420, "Skynet Backend"))
    results.append(check_port(8421, "GOD Console"))
    results.append(check_ghost_type_fixes())
    
    # High checks
    results.append(check_guard_bypass_fixes())
    results.append(check_workers_json())
    
    # Low checks
    results.append(check_empty_files())
    
    critical_fail = any(not r["passed"] and r.get("severity") == "CRITICAL" for r in results)
    any_fail = any(not r["passed"] for r in results)
    
    if json_output:
        print(json.dumps({"passed": not critical_fail, "results": results}, indent=2))
    elif quiet:
        print("PASS" if not critical_fail else "FAIL")
    else:
        print("=" * 60)
        print("SKYNET BOOT PRE-FLIGHT CHECK")
        print("=" * 60)
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            icon = "+" if r["passed"] else "X"
            sev = r.get("severity", "INFO")
            print(f"  [{icon}] {r['name']} [{sev}]: {status}")
            if not r["passed"]:
                if "fix" in r:
                    print(f"      Fix: {r['fix']}")
                if "checks" in r:
                    for k, v in r["checks"].items():
                        print(f"      {k}: {'OK' if v else 'MISSING'}")
                if "empty_files" in r and r["empty_files"]:
                    for ef in r["empty_files"]:
                        print(f"      Empty: {ef}")
                if "error" in r:
                    print(f"      Error: {r['error']}")
            if r.get("details"):
                for d in r["details"]:
                    print(f"      {d['location']}: {d['status']}")
        
        print()
        if critical_fail:
            print("RESULT: FAIL — Critical prerequisites not met. DO NOT proceed with boot.")
        elif any_fail:
            print("RESULT: WARN — Non-critical issues found. Boot can proceed with caution.")
        else:
            print("RESULT: PASS — All prerequisites met. Safe to boot.")
    
    return 0 if not critical_fail else 1

if __name__ == "__main__":
    fix = "--fix" in sys.argv
    json_out = "--json" in sys.argv
    quiet = "--quiet" in sys.argv
    sys.exit(run_preflight(fix=fix, json_output=json_out, quiet=quiet))
