"""Open a Chrome profile and a bookmark folder safely on Windows.

This avoids PowerShell Start-Process quoting issues with profile directories
that contain spaces, such as "Profile 3".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, Iterable, List, Optional


CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str:
    for path in CHROME_PATHS:
        resolved = os.path.expandvars(path)
        if os.path.exists(resolved):
            return resolved
    raise FileNotFoundError("Chrome executable not found")


def get_user_data_dir() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google",
        "Chrome",
        "User Data",
    )


def load_local_state() -> Dict:
    path = os.path.join(get_user_data_dir(), "Local State")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_profiles(info_cache: Dict) -> Iterable[Dict]:
    for directory, data in info_cache.items():
        yield {
            "directory": directory,
            "name": data.get("name", directory),
            "shortcut": data.get("shortcut_name", ""),
            "user": data.get("user_name", ""),
        }


def resolve_profile(query: str) -> Dict:
    state = load_local_state()
    info_cache = state.get("profile", {}).get("info_cache", {})
    profiles = list(iter_profiles(info_cache))
    lowered = query.strip().lower()

    for profile in profiles:
        candidates = [
            profile["directory"],
            profile["name"],
            profile["shortcut"],
            profile["user"],
        ]
        if any(lowered == value.lower() for value in candidates if value):
            return profile

    for profile in profiles:
        candidates = [
            profile["directory"],
            profile["name"],
            profile["shortcut"],
            profile["user"],
        ]
        if any(lowered in value.lower() for value in candidates if value):
            return profile

    available = ", ".join(
        f'{p["directory"]} ({p["name"]})' for p in sorted(profiles, key=lambda p: p["directory"])
    )
    raise ValueError(f'Profile "{query}" not found. Available: {available}')


def load_bookmarks(profile_directory: str) -> Dict:
    path = os.path.join(get_user_data_dir(), profile_directory, "Bookmarks")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def walk_folders(node: Optional[Dict], target_name: str) -> Iterable[Dict]:
    if not node:
        return
    if node.get("type") == "folder" and node.get("name") == target_name:
        yield node
    for child in node.get("children", []) or []:
        yield from walk_folders(child, target_name)


def find_bookmark_folder(bookmarks: Dict, folder_name: str) -> Dict:
    roots = bookmarks.get("roots", {})
    for root_name in ("bookmark_bar", "other", "synced"):
        root = roots.get(root_name)
        for folder in walk_folders(root, folder_name):
            return folder
    raise ValueError(f'Bookmark folder "{folder_name}" not found')


def collect_folder_urls(folder: Dict) -> List[str]:
    return [
        child["url"]
        for child in folder.get("children", []) or []
        if child.get("type") == "url" and child.get("url")
    ]


def build_args(profile_directory: str, folder_id: str, urls: List[str], mode: str) -> List[str]:
    args = [f"--profile-directory={profile_directory}"]
    if mode in {"manager", "both"}:
        args.append(f"chrome://bookmarks/?id={folder_id}")
    if mode in {"urls", "both"}:
        args.extend(urls)
    return args


def is_profile_window_open(profile_name: str) -> Optional[int]:
    """Check if a Chrome window for this profile is already open.

    Returns the hwnd if found, None otherwise.
    Uses Win32 EnumWindows to scan visible Chrome windows.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        found_hwnd = None

        def callback(hwnd, _):
            nonlocal found_hwnd
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if "Google Chrome" in title:
                        # Check window class to confirm it's Chrome
                        cls_buf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cls_buf, 256)
                        if cls_buf.value == "Chrome_WidgetWin_1":
                            found_hwnd = hwnd
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        # We can't easily distinguish which profile a window belongs to from
        # the title alone, so we check if *any* Chrome window is running for
        # this profile by scanning command lines.
        import subprocess as _sp
        result = _sp.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        profile_flag = f"--profile-directory="
        for line in result.stdout.splitlines():
            if profile_name in line or f"Profile {profile_name}" in line:
                # Profile is running — now find its window
                user32.EnumWindows(WNDENUMPROC(callback), 0)
                return found_hwnd
    except Exception:
        pass
    return None


def bring_to_front(hwnd: int) -> None:
    """Bring a window to the foreground."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open a Chrome profile and bookmark folder without quoting bugs."
    )
    parser.add_argument("profile", help='Profile directory or display name, e.g. "Mak" or "Profile 3"')
    parser.add_argument("folder", nargs="?", default=None,
                        help='Bookmark folder name, e.g. "AI". If omitted, just opens/focuses the profile.')
    parser.add_argument(
        "--mode",
        choices=("manager", "urls", "both"),
        default="both",
        help="Open the bookmark manager, the folder URLs, or both",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the profile and folder, then print the command without launching Chrome",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Open even if the profile window is already open",
    )
    args = parser.parse_args()

    chrome_path = find_chrome()
    profile = resolve_profile(args.profile)

    # Check if profile is already open
    if not args.force:
        hwnd = is_profile_window_open(profile["directory"])
        if hwnd and not args.folder:
            bring_to_front(hwnd)
            print(f'Profile "{profile["name"]}" is already open. Brought to foreground.')
            return 0

    if not args.folder:
        # Just open/focus the profile without a specific folder
        subprocess.Popen(
            [chrome_path, f"--profile-directory={profile['directory']}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f'Opened profile "{profile["name"]}" ({profile["directory"]}).')
        return 0

    bookmarks = load_bookmarks(profile["directory"])
    folder = find_bookmark_folder(bookmarks, args.folder)
    urls = collect_folder_urls(folder)

    if args.mode in {"urls", "both"} and not urls:
        raise ValueError(f'Bookmark folder "{args.folder}" has no URL entries')

    launch_args = build_args(profile["directory"], folder["id"], urls, args.mode)

    if args.dry_run:
        print(json.dumps({
            "chrome_path": chrome_path,
            "profile": profile,
            "folder": {
                "id": folder["id"],
                "name": folder["name"],
                "url_count": len(urls),
            },
            "launch_args": launch_args,
        }, indent=2))
        return 0

    # If profile is already open, just bring to front — don't re-open bookmarks
    if not args.force:
        hwnd = is_profile_window_open(profile["directory"])
        if hwnd:
            bring_to_front(hwnd)
            print(
                f'Profile "{profile["name"]}" is already open with these tabs. '
                f'Brought to foreground. Use --force to open duplicates.'
            )
            return 0

    subprocess.Popen([chrome_path, *launch_args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(
        f'Opened profile "{profile["name"]}" ({profile["directory"]}) '
        f'with bookmark folder "{folder["name"]}" ({len(urls)} URLs).'
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
