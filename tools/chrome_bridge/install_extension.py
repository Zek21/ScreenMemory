#!/usr/bin/env python3
"""Persistently install Chrome Bridge extension into Chrome profiles.

Registers the extension as an unpacked developer-mode extension by writing
directly to the profile Preferences JSON.  Chrome loads it on every subsequent
launch -- no --load-extension flag required.

Approach
--------
1. Kill Chrome (cannot modify Preferences while Chrome holds the lock).
2. Read the target profile's Preferences JSON.
3. Enable developer mode (extensions.ui.developer_mode = true).
4. Add the extension to extensions.settings with location=4 (UNPACKED).
5. Remove Secure Preferences so Chrome regenerates HMACs on next launch.
   (Secure Preferences stores HMAC-SHA256 integrity checks.  We cannot
    compute valid MACs without Chrome's internal seed, so we delete the
    file and let Chrome rebuild it from current Preferences state.
    Chrome treats a missing Secure Preferences as first-run -- no settings
    are lost, only HMACs are regenerated.)
6. Relaunch Chrome.  The extension appears in chrome://extensions.

Usage
-----
    python install_extension.py --profile SOCIALS
    python install_extension.py --all
    python install_extension.py --list
    python install_extension.py --verify
"""
# signed: alpha

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(SCRIPT_DIR, "extension")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"
)

# Seconds between Windows FILETIME epoch (1601-01-01) and Unix epoch (1970-01-01)
_CHROME_EPOCH_OFFSET = 11644473600


# ── Helpers ────────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _chrome_time_now() -> str:
    """Chrome-format timestamp: microseconds since 1601-01-01 as a string."""
    return str(int((time.time() + _CHROME_EPOCH_OFFSET) * 1_000_000))


def compute_extension_id(ext_path: str) -> str:
    """Compute Chrome extension ID from filesystem path.

    Chrome's algorithm for unpacked extensions (no manifest key):
      1. GetLongPathName (resolve 8.3 short names)
      2. Lowercase (ASCII only)
      3. SHA-256 hash
      4. First 16 bytes → each nibble mapped to 'a'-'p'
    """
    normalized = os.path.abspath(ext_path)
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(512)
            if ctypes.windll.kernel32.GetLongPathNameW(normalized, buf, 512):
                normalized = buf.value
        except Exception:
            pass
        normalized = normalized.lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    chars = []
    for i in range(32):
        byte = digest[i // 2]
        nibble = (byte >> (4 * (1 - i % 2))) & 0x0F
        chars.append(chr(ord("a") + nibble))
    return "".join(chars)


# ── Profile Management ────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    """List Chrome profiles from Local State info_cache."""
    local_state = os.path.join(USER_DATA, "Local State")
    if not os.path.isfile(local_state):
        return []
    try:
        with open(local_state, "r", encoding="utf-8") as f:
            data = json.load(f)
        info_cache = data.get("profile", {}).get("info_cache", {})
        return [
            {
                "directory": d,
                "name": v.get("name", d),
                "path": os.path.join(USER_DATA, d),
            }
            for d, v in info_cache.items()
        ]
    except Exception:
        return []


def resolve_profile(query: str) -> dict:
    """Resolve profile by display name or directory (exact then substring)."""
    profiles = list_profiles()
    q = query.strip().lower()
    # Exact match
    for p in profiles:
        if q in (p["directory"].lower(), p["name"].lower()):
            return p
    # Substring match
    for p in profiles:
        if q in p["directory"].lower() or q in p["name"].lower():
            return p
    available = ", ".join(
        f'{p["directory"]} ({p["name"]})' for p in sorted(profiles, key=lambda x: x["directory"])
    )
    raise ValueError(f'Profile "{query}" not found. Available: {available}')


# ── Chrome Process Control ─────────────────────────────────────────────

def is_chrome_running() -> bool:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process -Name chrome -EA SilentlyContinue | Measure).Count"],
        capture_output=True, text=True,
    )
    return int(result.stdout.strip() or "0") > 0


def kill_chrome() -> int:
    """Kill all Chrome processes.  Returns count killed."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process -Name chrome -EA SilentlyContinue | Select -Expand Id"],
        capture_output=True, text=True,
    )
    pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
    if pids:
        cmds = "; ".join(
            f"Stop-Process -Id {p} -Force -EA SilentlyContinue" for p in pids
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", cmds], capture_output=True)
        time.sleep(3)
    return len(pids)


# ── Installation ───────────────────────────────────────────────────────

def _build_extension_entry(ext_dir: str) -> dict:
    """Build the extensions.settings entry for Preferences JSON."""
    manifest_path = os.path.join(ext_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    now = _chrome_time_now()
    permissions = manifest.get("permissions", [])
    host_perms = manifest.get("host_permissions", [])

    # Collect content script match patterns
    cs_hosts = []
    for cs in manifest.get("content_scripts", []):
        cs_hosts.extend(cs.get("matches", []))

    return {
        "active_permissions": {
            "api": permissions,
            "explicit_host": host_perms,
            "manifest_permissions": [],
            "scriptable_host": cs_hosts or host_perms,
        },
        "commands": {},
        "content_settings": [],
        "creation_flags": 1,
        "first_install_time": now,
        "from_bookmark": False,
        "from_webstore": False,
        "granted_permissions": {
            "api": permissions,
            "explicit_host": host_perms,
            "manifest_permissions": [],
            "scriptable_host": cs_hosts or host_perms,
        },
        "install_time": now,
        "location": 4,   # 4 = UNPACKED (developer mode)
        "manifest": manifest,
        "path": os.path.abspath(ext_dir),
        "state": 1,      # 1 = ENABLED
        "was_installed_by_default": False,
        "was_installed_by_oem": False,
    }


def install_via_preferences(profile_path: str, ext_dir: str) -> tuple[bool, str]:
    """Install extension by writing to Chrome Preferences JSON.

    1. Backs up Preferences and Secure Preferences.
    2. Enables developer mode.
    3. Adds the extension as an unpacked (location=4) entry.
    4. Removes Secure Preferences (Chrome regenerates HMACs on next launch).
    """
    prefs_path = os.path.join(profile_path, "Preferences")
    secure_path = os.path.join(profile_path, "Secure Preferences")

    if not os.path.isfile(prefs_path):
        return False, f"No Preferences file at {prefs_path}"

    ext_path = os.path.abspath(ext_dir)
    ext_id = compute_extension_id(ext_path)

    # Build the extension settings entry
    try:
        ext_entry = _build_extension_entry(ext_dir)
    except Exception as e:
        return False, f"Failed to read extension manifest: {e}"

    # Backup originals
    shutil.copy2(prefs_path, prefs_path + ".install_bak")

    # Read and modify Preferences
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"Corrupt Preferences JSON: {e}"

    # Enable developer mode
    prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True

    # Register extension
    prefs["extensions"].setdefault("settings", {})[ext_id] = ext_entry

    # Write Preferences (compact, matching Chrome's output format)
    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, separators=(",", ":"))

    # Handle Secure Preferences: back up and remove so Chrome regenerates
    if os.path.isfile(secure_path):
        shutil.copy2(secure_path, secure_path + ".install_bak")
        os.remove(secure_path)

    return True, f"Installed as {ext_id} (developer mode, UNPACKED)"


def verify_in_preferences(profile_path: str, ext_id: str) -> bool:
    """Check if extension is registered in the profile Preferences."""
    prefs_path = os.path.join(profile_path, "Preferences")
    if not os.path.isfile(prefs_path):
        return False
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        settings = prefs.get("extensions", {}).get("settings", {})
        entry = settings.get(ext_id, {})
        return entry.get("state", 0) == 1 and entry.get("location") == 4
    except Exception:
        return False


def verify_post_launch(profile_path: str, ext_id: str, timeout: int = 10) -> bool:
    """After Chrome relaunches, verify the extension survived.

    Chrome reads Preferences on startup and may remove entries it deems
    invalid.  This re-checks after a short delay.
    """
    time.sleep(timeout)
    return verify_in_preferences(profile_path, ext_id)


# ── CLI ────────────────────────────────────────────────────────────────

def cmd_list(ext_id: str):
    profiles = list_profiles()
    print(f"Chrome profiles ({len(profiles)}):")
    for p in sorted(profiles, key=lambda x: x["directory"]):
        installed = verify_in_preferences(p["path"], ext_id)
        icon = "\u2713" if installed else "\u2717"
        print(f"  {icon} {p['directory']:15s}  {p['name']}")
    print(f"\nExtension ID: {ext_id}")
    print(f"Extension dir: {os.path.abspath(EXT_DIR)}")


def cmd_verify(ext_id: str) -> bool:
    profiles = list_profiles()
    all_ok = True
    for p in sorted(profiles, key=lambda x: x["directory"]):
        installed = verify_in_preferences(p["path"], ext_id)
        icon = "\u2713" if installed else "\u2717"
        print(f"  {icon} {p['directory']:15s}  {p['name']}")
        if not installed:
            all_ok = False
    return all_ok


def cmd_install(targets: list[dict], ext_id: str, force: bool, relaunch: bool):
    # Ensure Chrome is not running
    if is_chrome_running():
        print("Chrome is running -- killing all Chrome processes...")
        killed = kill_chrome()
        print(f"  Killed {killed} processes")
        time.sleep(2)
        if is_chrome_running():
            print("ERROR: Chrome still running. Close it manually and retry.")
            sys.exit(1)

    results = {}
    for profile in targets:
        label = f"{profile['directory']} ({profile['name']})"
        print(f"\n[{label}]")

        if not force and verify_in_preferences(profile["path"], ext_id):
            print(f"  Already installed ({ext_id})")
            results[label] = True
            continue

        ok, msg = install_via_preferences(profile["path"], EXT_DIR)
        print(f"  {'OK' if ok else 'FAILED'}: {msg}")
        results[label] = ok

    # Summary
    ok_n = sum(1 for v in results.values() if v)
    fail_n = sum(1 for v in results.values() if not v)
    print(f"\n{'=' * 50}")
    print(f"Results: {ok_n} succeeded, {fail_n} failed")
    print(f"Extension ID: {ext_id}")

    # Relaunch Chrome
    if relaunch and ok_n > 0:
        chrome = _find_chrome()
        if chrome:
            print("\nRelaunching Chrome...")
            subprocess.Popen(
                [chrome, "--no-first-run"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Verify extension survived Chrome startup
            first_profile = targets[0]["path"]
            print(f"Waiting for Chrome to start and validate extensions...")
            survived = verify_post_launch(first_profile, ext_id, timeout=8)
            if survived:
                print("  Extension survived Chrome startup validation!")
            else:
                print("  WARNING: Extension entry may have been removed by Chrome.")
                print("  Check chrome://extensions manually.")

    if fail_n > 0:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Persistently install Chrome Bridge extension into Chrome profiles"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", "-p", help="Profile name or directory")
    group.add_argument("--all", "-a", action="store_true",
                       help="Install to all profiles")
    group.add_argument("--list", "-l", action="store_true",
                       help="List profiles and installation status")
    group.add_argument("--verify", "-v", action="store_true",
                       help="Verify installation across all profiles")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Force reinstall even if already present")
    parser.add_argument("--no-relaunch", action="store_true",
                        help="Do not relaunch Chrome after install")
    args = parser.parse_args()

    # Validate extension directory
    manifest = os.path.join(EXT_DIR, "manifest.json")
    if not os.path.isfile(manifest):
        print(f"ERROR: Extension manifest not found at {manifest}")
        sys.exit(1)

    ext_id = compute_extension_id(os.path.abspath(EXT_DIR))

    if args.list:
        cmd_list(ext_id)
        return

    if args.verify:
        ok = cmd_verify(ext_id)
        sys.exit(0 if ok else 1)

    # Resolve targets
    if args.all:
        targets = list_profiles()
        if not targets:
            print("No Chrome profiles found")
            sys.exit(1)
    else:
        try:
            targets = [resolve_profile(args.profile)]
        except ValueError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    cmd_install(targets, ext_id, args.force, relaunch=not args.no_relaunch)


if __name__ == "__main__":
    main()
