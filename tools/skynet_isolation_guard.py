#!/usr/bin/env python3
"""
Skynet Isolation Guard — prevents cli.isolationOption.enabled from drifting to True.

INCIDENT: 2026-03-16
  User setting `github.copilot.chat.cli.isolationOption.enabled` silently changed
  from False to True between sessions. When enabled, CLI sessions are isolated and
  CANNOT delegate to workers — causing all dispatch/delegation to be cancelled.
  This is a catastrophic failure for Skynet's multi-worker architecture.

This script:
  1. Checks both user and workspace settings for the isolation option
  2. Fixes the user setting if it drifted to True
  3. Ensures workspace setting has the explicit override to False
  4. Can be run standalone or imported as a guard function

Usage:
  python tools/skynet_isolation_guard.py          # Check and fix
  python tools/skynet_isolation_guard.py --check   # Check only, exit 1 if bad
  python tools/skynet_isolation_guard.py --watch   # Monitor continuously

# signed: orchestrator
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Settings paths
APPDATA = os.environ.get("APPDATA", "")
USER_SETTINGS = Path(APPDATA) / "Code - Insiders" / "User" / "settings.json"
WORKSPACE_SETTINGS = Path("D:/Prospects/ScreenMemory/.vscode/settings.json")
SETTING_KEY = "github.copilot.chat.cli.isolationOption.enabled"

# Other dangerous settings that could break delegation
DANGEROUS_SETTINGS = {
    "github.copilot.chat.cli.isolationOption.enabled": {
        "required_value": False,
        "description": "When True, CLI sessions are isolated and cannot delegate to workers"
    },
}


def _load_json_lenient(path: Path) -> dict:
    """Load JSON that may have trailing commas (common in VS Code settings)."""
    text = path.read_text(encoding="utf-8")
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


def check_user_setting() -> tuple:
    """Check if user setting is correct. Returns (exists, value)."""
    if not USER_SETTINGS.exists():
        return False, None
    try:
        data = _load_json_lenient(USER_SETTINGS)
        val = data.get(SETTING_KEY)
        return True, val
    except (json.JSONDecodeError, OSError):
        return False, None


def check_workspace_setting() -> tuple:
    """Check if workspace setting has the override. Returns (exists, value)."""
    if not WORKSPACE_SETTINGS.exists():
        return False, None
    try:
        data = _load_json_lenient(WORKSPACE_SETTINGS)
        val = data.get(SETTING_KEY)
        return True, val
    except (json.JSONDecodeError, OSError):
        return False, None


def fix_user_setting() -> bool:
    """Fix the user setting back to False."""
    if not USER_SETTINGS.exists():
        print(f"  [WARN] User settings file not found: {USER_SETTINGS}")
        return False
    try:
        text = USER_SETTINGS.read_text(encoding="utf-8")
        # Use regex replacement to avoid JSON formatting issues with trailing commas
        if '"github.copilot.chat.cli.isolationOption.enabled": true' in text:
            text = text.replace(
                '"github.copilot.chat.cli.isolationOption.enabled": true',
                '"github.copilot.chat.cli.isolationOption.enabled": false'
            )
            USER_SETTINGS.write_text(text, encoding="utf-8")
            print(f"  [FIXED] User {SETTING_KEY}: true -> false (regex replace)")
            return True
        else:
            # Parse leniently to check and add
            data = _load_json_lenient(USER_SETTINGS)
            old_val = data.get(SETTING_KEY)
            data[SETTING_KEY] = False
            USER_SETTINGS.write_text(
                json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8"
            )
            print(f"  [FIXED] User {SETTING_KEY}: {old_val} -> false")
            return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [ERROR] Failed to fix user setting: {e}")
        return False


def fix_workspace_setting() -> bool:
    """Ensure workspace setting has the False override."""
    if not WORKSPACE_SETTINGS.exists():
        print(f"  [WARN] Workspace settings file not found: {WORKSPACE_SETTINGS}")
        return False
    try:
        data = json.loads(WORKSPACE_SETTINGS.read_text(encoding="utf-8"))
        if data.get(SETTING_KEY) is False:
            return True  # Already correct
        data[SETTING_KEY] = False
        WORKSPACE_SETTINGS.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )
        print(f"  [FIXED] Workspace {SETTING_KEY} set to False")
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [ERROR] Failed to fix workspace setting: {e}")
        return False


def guard_isolation(fix: bool = True) -> bool:
    """
    Main guard function. Returns True if settings are correct (or were fixed).
    
    Call this at boot time from Orch-Start.ps1, CC-Start.ps1, GC-Start.ps1,
    or skynet_start.py to prevent delegation failures.
    """
    all_ok = True
    
    # Check user setting
    exists, val = check_user_setting()
    if exists and val is True:
        print(f"  [DANGER] User {SETTING_KEY} = True (blocks delegation!)")
        if fix:
            fix_user_setting()
        else:
            all_ok = False
    elif exists and val is False:
        print(f"  [OK] User {SETTING_KEY} = False")
    elif exists and val is None:
        print(f"  [OK] User {SETTING_KEY} not set (defaults to False)")
    else:
        print(f"  [WARN] User settings file not found")
    
    # Check workspace setting
    exists, val = check_workspace_setting()
    if exists and val is False:
        print(f"  [OK] Workspace {SETTING_KEY} = False (override active)")
    elif exists and val is True:
        print(f"  [DANGER] Workspace {SETTING_KEY} = True!")
        if fix:
            fix_workspace_setting()
        else:
            all_ok = False
    elif exists and val is None:
        print(f"  [WARN] Workspace {SETTING_KEY} not set — adding override")
        if fix:
            fix_workspace_setting()
        else:
            all_ok = False
    else:
        print(f"  [WARN] Workspace settings file not found")
    
    return all_ok


def watch_isolation(interval: int = 30):
    """Continuously monitor the isolation setting and fix if it drifts."""
    print(f"Watching {SETTING_KEY} every {interval}s...")
    while True:
        _, val = check_user_setting()
        if val is True:
            print(f"\n[{time.strftime('%H:%M:%S')}] DRIFT DETECTED! Fixing...")
            fix_user_setting()
            # Alert on bus
            try:
                import requests
                from tools.skynet_spam_guard import guarded_publish
                guarded_publish({
                    "sender": "isolation_guard",
                    "topic": "orchestrator",
                    "type": "alert",
                    "content": "ISOLATION_DRIFT: cli.isolationOption.enabled changed to True — auto-fixed"
                })
            except Exception:
                pass
        time.sleep(interval)


if __name__ == "__main__":
    print("=== Skynet Isolation Guard ===")
    
    if "--watch" in sys.argv:
        watch_isolation()
    elif "--check" in sys.argv:
        ok = guard_isolation(fix=False)
        sys.exit(0 if ok else 1)
    else:
        guard_isolation(fix=True)
        print("\nDone. Delegation should work correctly.")
