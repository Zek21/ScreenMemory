#!/usr/bin/env python3
"""
Skynet Isolation Guard — ensures cli.isolationOption.enabled stays True.

INCIDENT 014 (2026-03-15): VS Code's isWorktreeIsolationSelected() has INVERTED logic:
  - When enabled=False → worktree isolation is FORCED (creates worktrees, breaks inline execution)
  - When enabled=True → worktree isolation is OPTIONAL (CLI runs inline, which is what we need)

INCIDENT 016 (2026-03-16) incorrectly set this to False, which CAUSED the worktree problem.

CORRECTED 2026-03-18: The correct value is True. This guard ensures it stays True.

This script:
  1. Checks both user and workspace settings for the isolation option
  2. Fixes the setting if it drifted to False (which forces worktree creation)
  3. Ensures workspace setting has the explicit override to True
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
        "required_value": True,
        "description": "Must be True — False forces worktree isolation which breaks inline CLI execution"
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
    """Fix the user setting to True (prevents forced worktree isolation)."""
    if not USER_SETTINGS.exists():
        print(f"  [WARN] User settings file not found: {USER_SETTINGS}")
        return False
    try:
        text = USER_SETTINGS.read_text(encoding="utf-8")
        if '"github.copilot.chat.cli.isolationOption.enabled": false' in text:
            text = text.replace(
                '"github.copilot.chat.cli.isolationOption.enabled": false',
                '"github.copilot.chat.cli.isolationOption.enabled": true'
            )
            USER_SETTINGS.write_text(text, encoding="utf-8")
            print(f"  [FIXED] User {SETTING_KEY}: false -> true (regex replace)")
            return True
        else:
            data = _load_json_lenient(USER_SETTINGS)
            old_val = data.get(SETTING_KEY)
            data[SETTING_KEY] = True
            USER_SETTINGS.write_text(
                json.dumps(data, indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8"
            )
            print(f"  [FIXED] User {SETTING_KEY}: {old_val} -> true")
            return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [ERROR] Failed to fix user setting: {e}")
        return False


def fix_workspace_setting() -> bool:
    """Ensure workspace setting has the True override."""
    if not WORKSPACE_SETTINGS.exists():
        print(f"  [WARN] Workspace settings file not found: {WORKSPACE_SETTINGS}")
        return False
    try:
        data = json.loads(WORKSPACE_SETTINGS.read_text(encoding="utf-8"))
        if data.get(SETTING_KEY) is True:
            return True  # Already correct
        data[SETTING_KEY] = True
        WORKSPACE_SETTINGS.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )
        print(f"  [FIXED] Workspace {SETTING_KEY} set to True")
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [ERROR] Failed to fix workspace setting: {e}")
        return False


def guard_isolation(fix: bool = True) -> bool:
    """
    Main guard function. Returns True if settings are correct (or were fixed).
    
    The CORRECT value is True — this prevents forced worktree isolation.
    False is WRONG — it forces worktree creation which breaks inline CLI execution.
    """
    all_ok = True
    
    # Check user setting — must be True
    exists, val = check_user_setting()
    if exists and val is False:
        print(f"  [DANGER] User {SETTING_KEY} = False (forces worktree isolation!)")
        if fix:
            fix_user_setting()
        else:
            all_ok = False
    elif exists and val is True:
        print(f"  [OK] User {SETTING_KEY} = True")
    elif exists and val is None:
        print(f"  [WARN] User {SETTING_KEY} not set — adding True override")
        if fix:
            fix_user_setting()
        else:
            all_ok = False
    else:
        print(f"  [WARN] User settings file not found")
    
    # Check workspace setting — must be True
    exists, val = check_workspace_setting()
    if exists and val is True:
        print(f"  [OK] Workspace {SETTING_KEY} = True (override active)")
    elif exists and val is False:
        print(f"  [DANGER] Workspace {SETTING_KEY} = False (forces worktree isolation!)")
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
        if val is False:
            print(f"\n[{time.strftime('%H:%M:%S')}] DRIFT DETECTED! Value is False (forces worktrees). Fixing...")
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
