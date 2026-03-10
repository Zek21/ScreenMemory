"""Skynet Chrome Manager -- ensures Chrome is always running with CDP enabled.

Usage:
    python tools/skynet_chrome.py              # Ensure Chrome + CDP is running
    python tools/skynet_chrome.py --status     # Check CDP status
    python tools/skynet_chrome.py --restart    # Restart Chrome with CDP
    python tools/skynet_chrome.py --open URL   # Open URL in CDP Chrome

RULE: Skynet ALWAYS uses CDP (Chrome DevTools Protocol) for browser automation.
Playwright is forbidden. See tools/chrome_bridge/PLAYWRIGHT_REPLACEMENT.md.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")


def load_config():
    """Load browser config from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        return cfg.get("browser", {})
    except Exception:
        return {}


def cdp_alive():
    """Check if Chrome CDP is responding."""
    try:
        r = urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
        data = json.loads(r.read())
        return data.get("Browser", "Chrome")
    except Exception:
        return None


def get_chrome_path():
    """Get Chrome executable path."""
    cfg = load_config()
    path = cfg.get("chrome_path", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if os.path.exists(path):
        return path
    # Fallback locations
    for p in [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        if os.path.exists(p):
            return p
    return None


def get_chrome_flags():
    """Get Chrome launch flags from config."""
    cfg = load_config()
    flags = cfg.get("chrome_flags", [
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
    ])
    # Always ensure these are present
    has_port = any("remote-debugging-port" in f for f in flags)
    has_origins = any("remote-allow-origins" in f for f in flags)
    if not has_port:
        flags.append(f"--remote-debugging-port={CDP_PORT}")
    if not has_origins:
        flags.append("--remote-allow-origins=*")
    return flags


def launch_chrome(url=None, user_data_dir=None):
    """Launch Chrome with CDP debugging enabled."""
    chrome = get_chrome_path()
    if not chrome:
        print("ERROR: Chrome not found")
        return False

    flags = get_chrome_flags()
    cfg = load_config()
    udd = user_data_dir or cfg.get("user_data_dir")
    if udd:
        flags.append(f"--user-data-dir={udd}")

    args = [chrome] + flags
    if url:
        args.append(url)

    subprocess.Popen(args)
    print(f"Chrome launched with CDP on port {CDP_PORT}")

    # Wait for CDP to be ready
    for _ in range(15):
        time.sleep(1)
        if cdp_alive():
            print("CDP ready")
            return True
    print("WARNING: Chrome launched but CDP not responding after 15s")
    return False


def ensure_cdp(url=None):
    """Ensure Chrome is running with CDP. Launch if needed."""
    browser = cdp_alive()
    if browser:
        print(f"CDP already active: {browser}")
        if url:
            open_url(url)
        return True
    print("CDP not active, launching Chrome...")
    return launch_chrome(url)


def restart_chrome(url=None):
    """Kill Chrome and relaunch with CDP."""
    import ctypes
    # Find and kill Chrome processes
    try:
        # Use tasklist to find Chrome PIDs
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split("\n"):
            if "chrome.exe" in line:
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    pid = int(parts[1])
                    try:
                        os.kill(pid, 9)
                    except Exception:
                        pass
    except Exception as e:
        print(f"Kill phase error: {e}")

    time.sleep(3)
    return launch_chrome(url or "http://localhost:8421/")


def open_url(url):
    """Open a URL in existing CDP Chrome."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from tools.chrome_bridge.cdp import CDP
        cdp = CDP()
        tabs = cdp.tabs()
        # Find existing page tab or create new
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if page_tabs:
            tid = page_tabs[0]["id"]
            cdp.navigate(tid, url)
            print(f"Navigated to {url}")
        else:
            cdp.new_tab(url)
            print(f"Opened new tab: {url}")
        return True
    except Exception as e:
        print(f"Open URL failed: {e}")
        return False


def status():
    """Print full CDP status."""
    browser = cdp_alive()
    if not browser:
        print("CDP STATUS: OFFLINE")
        print(f"  Port {CDP_PORT} not responding")
        print("  Run: python tools/skynet_chrome.py --restart")
        return False

    print(f"CDP STATUS: ONLINE")
    print(f"  Browser: {browser}")
    print(f"  Port: {CDP_PORT}")

    try:
        r = urllib.request.urlopen(f"{CDP_URL}/json", timeout=3)
        tabs = json.loads(r.read())
        pages = [t for t in tabs if t.get("type") == "page"]
        print(f"  Tabs: {len(tabs)} ({len(pages)} pages)")
        for p in pages:
            print(f"    {p.get('title', '?')[:50]} -- {p.get('url', '?')[:60]}")
    except Exception:
        pass

    return True


def get_cdp():
    """Get a CDP instance, ensuring Chrome is running. Use this from other modules."""
    ensure_cdp()
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from tools.chrome_bridge.cdp import CDP
    return CDP()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--status" in args:
        status()
    elif "--restart" in args:
        url = None
        if "--open" in args:
            idx = args.index("--open")
            url = args[idx + 1] if idx + 1 < len(args) else None
        restart_chrome(url)
    elif "--open" in args:
        idx = args.index("--open")
        url = args[idx + 1] if idx + 1 < len(args) else "http://localhost:8421/"
        ensure_cdp(url)
    else:
        ensure_cdp("http://localhost:8421/")
