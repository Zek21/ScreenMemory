"""Install Chrome Bridge on ALL Chrome profiles via CDP.
Cycles through each profile: kill Chrome → relaunch with CDP → install → next."""
import sys, os, time, ctypes, subprocess, json, socket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP

EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extension')
CHROME_PATH = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
USER_DATA = os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'User Data')
CDP_PORT = 9222

user32 = ctypes.windll.user32
KEYEVENTF_KEYUP = 0x0002


def press(vk):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def hotkey(*vks):
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    time.sleep(0.05)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def kill_chrome():
    """Kill all Chrome processes."""
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-Process -Name chrome -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id'],
        capture_output=True, text=True
    )
    pids = [int(p.strip()) for p in result.stdout.strip().split('\n') if p.strip().isdigit()]
    if pids:
        # Kill all in one command
        pid_cmds = '; '.join(f'Stop-Process -Id {p} -Force -ErrorAction SilentlyContinue' for p in pids)
        subprocess.run(['powershell', '-NoProfile', '-Command', pid_cmds], capture_output=True)
        time.sleep(4)
        # Verify all dead
        result2 = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-Process -Name chrome -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count'],
            capture_output=True, text=True
        )
        remaining = int(result2.stdout.strip()) if result2.stdout.strip().isdigit() else 0
        if remaining:
            time.sleep(3)
    return len(pids)


def launch_chrome(profile_dir):
    """Launch Chrome with --load-extension for a specific profile.
    Uses --load-extension flag to auto-register the extension, avoiding the
    fragile chrome://extensions UI automation entirely.
    Direct subprocess launch avoids PowerShell ArgumentList splitting profile
    names like "Profile 3" into a bogus trailing URL ("0.0.0.3")."""  # signed: gamma
    args = [
        CHROME_PATH,
        f'--remote-debugging-port={CDP_PORT}',
        '--remote-allow-origins=*',
        f'--user-data-dir={USER_DATA}',
        f'--profile-directory="{profile_dir}"',
        f'--load-extension={EXTENSION_PATH}',
        '--no-first-run',
        'chrome://extensions',
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_cdp(timeout=30):
    """Wait for CDP to be ready, return CDP instance."""
    for i in range(timeout):
        time.sleep(1)
        try:
            s = socket.create_connection(('127.0.0.1', CDP_PORT), timeout=1)
            s.close()
            chrome = CDP(port=CDP_PORT)
            return chrome
        except Exception:
            pass
    return None


def _handle_file_dialog():
    """Handle the file dialog for loading unpacked extension."""
    time.sleep(2)
    dialog = user32.FindWindowW('#32770', None)
    if not dialog:
        time.sleep(2)
        dialog = user32.FindWindowW('#32770', None)

    if not dialog:
        print("    No dialog appeared")
        return False

    user32.SetForegroundWindow(dialog)
    time.sleep(0.5)

    hotkey(0x12, 0x44)  # Alt+D
    time.sleep(0.5)

    subprocess.run(
        ['powershell', '-NoProfile', '-Command', f'Set-Clipboard -Value "{EXTENSION_PATH}"'],
        check=True, capture_output=True
    )
    time.sleep(0.2)

    hotkey(0x11, 0x56)  # Ctrl+V
    time.sleep(0.5)
    press(0x0D)  # Enter (navigate)
    time.sleep(1.5)
    press(0x0D)  # Enter (select folder)
    return True


def install_on_tab(chrome, tab_id):
    """Install extension via CDP on the given tab."""
    # Navigate to extensions
    chrome.navigate(tab_id, 'chrome://extensions')
    time.sleep(3)

    # Enable Developer Mode
    dev = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
            if (!toolbar || !toolbar.shadowRoot) return 'no-toolbar';
            var toggle = toolbar.shadowRoot.querySelector('#devMode');
            if (!toggle) return 'no-toggle';
            if (!toggle.checked) { toggle.click(); return 'enabled'; }
            return 'already-on';
        })()
    """)
    print(f"    DevMode: {dev}")
    time.sleep(1)

    # Check if Chrome Bridge is already installed
    check = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var list = mgr.shadowRoot.querySelector('extensions-item-list');
            if (!list || !list.shadowRoot) return 'no-list';
            var items = list.shadowRoot.querySelectorAll('.items-container extensions-item');
            for (var i = 0; i < items.length; i++) {
                var n = items[i].shadowRoot ? items[i].shadowRoot.querySelector('#name') : null;
                if (n && n.textContent.trim() === 'Chrome Bridge') return 'already-installed';
            }
            return 'not-installed';
        })()
    """)

    if check == 'already-installed':
        print("    Already installed, skipping")
        return True

    # Click Load Unpacked
    load = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
            var btn = toolbar.shadowRoot.querySelector('#loadUnpacked');
            if (!btn) return 'no-button';
            btn.click();
            return 'clicked';
        })()
    """)
    print(f"    LoadUnpacked: {load}")

    if load != 'clicked':
        return False

    if not _handle_file_dialog():
        return False
    time.sleep(3)

    # Verify
    verify = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var list = mgr.shadowRoot.querySelector('extensions-item-list');
            if (!list || !list.shadowRoot) return 'no-list';
            var items = list.shadowRoot.querySelectorAll('.items-container extensions-item');
            for (var i = 0; i < items.length; i++) {
                var n = items[i].shadowRoot ? items[i].shadowRoot.querySelector('#name') : null;
                if (n && n.textContent.trim() === 'Chrome Bridge') return 'installed';
            }
            return 'not-found';
        })()
    """)
    return verify == 'installed'


def get_profiles():
    """Get all Chrome profile directories."""
    profiles = []
    for d in sorted(os.listdir(USER_DATA)):
        full = os.path.join(USER_DATA, d)
        prefs = os.path.join(full, 'Preferences')
        if (d == 'Default' or d.startswith('Profile ')) and os.path.exists(prefs):
            try:
                data = json.loads(open(prefs, 'r', encoding='utf-8').read())
                name = data.get('profile', {}).get('name', d)
                profiles.append((d, name))
            except Exception:
                profiles.append((d, d))
    return profiles


def main():
    profiles = get_profiles()
    print(f"Chrome Bridge Multi-Profile Installer (CDP)")
    print(f"Extension: {EXTENSION_PATH}")
    print(f"Profiles: {len(profiles)}")
    print()

    # If a specific profile is requested
    if len(sys.argv) > 1:
        target = sys.argv[1]
        profiles = [(d, n) for d, n in profiles if d == target or n.lower() == target.lower()]
        if not profiles:
            print(f"Profile '{target}' not found")
            return

    results = {}


def _install_profile(profile_dir, profile_name):
    """Install extension on a single Chrome profile.
    Uses --load-extension flag (set in launch_chrome) for automatic registration.
    Falls back to UI-based install_on_tab if CDP verification shows extension
    was not loaded by the flag."""  # signed: gamma
    print(f"[{profile_dir}] ({profile_name})")

    killed = kill_chrome()
    if killed:
        print(f"    Killed {killed} Chrome processes")

    print(f"    Launching Chrome with --load-extension...")
    launch_chrome(profile_dir)

    chrome = wait_for_cdp()
    if not chrome:
        print("    FAILED: CDP not available")
        return False

    tabs = chrome.tabs()
    if not tabs:
        print("    FAILED: No tabs")
        return False

    tab = tabs[0]['id']
    # --load-extension should have auto-registered it; verify via CDP
    time.sleep(3)
    check = chrome.eval(tab, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var list = mgr.shadowRoot.querySelector('extensions-item-list');
            if (!list || !list.shadowRoot) return 'no-list';
            var items = list.shadowRoot.querySelectorAll('.items-container extensions-item');
            for (var i = 0; i < items.length; i++) {
                var n = items[i].shadowRoot ? items[i].shadowRoot.querySelector('#name') : null;
                if (n && n.textContent.trim() === 'Chrome Bridge') return 'installed';
            }
            return 'not-found';
        })()
    """)
    if check == 'installed':
        print(f"    OK (auto-loaded via --load-extension)")
        print()
        return True
    # Fallback: try UI-based install
    print(f"    --load-extension did not register, falling back to UI install...")
    success = install_on_tab(chrome, tab)
    print(f"    Result: {'OK' if success else 'FAILED'}")
    print()
    return success

    for profile_dir, profile_name in profiles:
        results[profile_dir] = _install_profile(profile_dir, profile_name)

    # Summary
    print("=" * 50)
    print("Installation Summary:")
    ok = sum(1 for v in results.values() if v)
    fail = sum(1 for v in results.values() if not v)
    for d, s in results.items():
        icon = "✓" if s else "✗"
        print(f"  {icon} {d}")
    print(f"\n{ok} succeeded, {fail} failed out of {len(results)} profiles")

    # Relaunch Chrome normally at the end (Default profile, with CDP)
    if results:
        print("\nRelaunching Chrome (Default profile)...")
        kill_chrome()
        launch_chrome('Default')
        time.sleep(3)
        print("Done!")


if __name__ == '__main__':
    main()
