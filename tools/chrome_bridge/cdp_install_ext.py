"""Install Chrome Bridge extension on current profile via CDP + Win32 file dialog."""
import sys, os, time, ctypes, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP

EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extension')
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

def install_extension(chrome, tab_id):
    """Install extension via CDP eval + Win32 file dialog handling."""
    
    # Navigate to chrome://extensions
    print("  Navigating to chrome://extensions...")
    chrome.navigate(tab_id, 'chrome://extensions')
    time.sleep(3)
    
    url = chrome.eval(tab_id, 'window.location.href')
    print(f"  URL: {url}")
    
    # Enable Developer Mode
    print("  Enabling Developer Mode...")
    dev_result = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
            if (!toolbar || !toolbar.shadowRoot) return 'no-toolbar';
            var toggle = toolbar.shadowRoot.querySelector('#devMode');
            if (!toggle) return 'no-toggle';
            if (!toggle.checked) {
                toggle.click();
                return 'enabled';
            }
            return 'already-on';
        })()
    """)
    print(f"  Developer Mode: {dev_result}")
    time.sleep(1)
    
    # Click Load Unpacked
    print("  Clicking Load unpacked...")
    load_result = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
            var btn = toolbar.shadowRoot.querySelector('#loadUnpacked');
            if (!btn) return 'no-button';
            btn.click();
            return 'clicked';
        })()
    """)
    print(f"  Load unpacked: {load_result}")
    
    if load_result != 'clicked':
        print("  FAILED: Could not click Load unpacked")
        return False
    
    # Wait for file dialog
    print("  Waiting for folder picker dialog...")
    time.sleep(2)
    
    dialog = user32.FindWindowW('#32770', None)
    if not dialog:
        time.sleep(2)
        dialog = user32.FindWindowW('#32770', None)
    
    if not dialog:
        print("  FAILED: No file dialog appeared")
        return False
    
    print(f"  Dialog found: hwnd={dialog}")
    
    # Focus the dialog
    user32.SetForegroundWindow(dialog)
    time.sleep(0.5)
    
    # Alt+D to focus address bar
    hotkey(0x12, 0x44)  # Alt+D
    time.sleep(0.5)
    
    # Set clipboard to extension path
    subprocess.run(
        ['powershell', '-NoProfile', '-Command', f'Set-Clipboard -Value "{EXTENSION_PATH}"'],
        check=True, capture_output=True
    )
    time.sleep(0.2)
    
    # Ctrl+V to paste path
    hotkey(0x11, 0x56)  # Ctrl+V
    time.sleep(0.5)
    
    # Enter to navigate to the folder
    press(0x0D)
    time.sleep(1.5)
    
    # Enter again to select the folder
    press(0x0D)
    time.sleep(3)
    
    # Check if dialog closed
    dialog2 = user32.FindWindowW('#32770', None)
    if dialog2 == dialog:
        print("  WARNING: Dialog still open, trying again...")
        press(0x0D)
        time.sleep(2)
        dialog2 = user32.FindWindowW('#32770', None)
    
    if dialog2 and dialog2 == dialog:
        print("  FAILED: Dialog did not close")
        return False
    
    print("  Dialog closed, checking extension...")
    time.sleep(2)
    
    # Verify extension was loaded
    verify = chrome.eval(tab_id, """
        (function() {
            var mgr = document.querySelector('extensions-manager');
            if (!mgr || !mgr.shadowRoot) return 'no-manager';
            var list = mgr.shadowRoot.querySelector('extensions-item-list');
            if (!list || !list.shadowRoot) return 'no-list';
            var items = list.shadowRoot.querySelectorAll('.items-container extensions-item');
            var names = [];
            for (var i = 0; i < items.length; i++) {
                var name = items[i].shadowRoot ? 
                    items[i].shadowRoot.querySelector('#name') : null;
                if (name) names.push(name.textContent.trim());
            }
            return JSON.stringify({count: items.length, names: names});
        })()
    """)
    print(f"  Extensions: {verify}")
    
    has_bridge = 'Chrome Bridge' in str(verify)
    if has_bridge:
        print("  SUCCESS: Chrome Bridge extension installed!")
    else:
        print("  Extension list doesn't show Chrome Bridge yet")
    
    return has_bridge


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    print(f"Connecting to Chrome CDP on port {port}...")
    
    chrome = CDP(port=port)
    tabs = chrome.tabs()
    print(f"Connected! {len(tabs)} tab(s)")
    
    if not tabs:
        print("No tabs available")
        return
    
    tab = tabs[0]['id']
    success = install_extension(chrome, tab)
    
    if success:
        print("\n✓ Extension installed successfully via CDP!")
    else:
        print("\n✗ Extension installation needs manual verification")


if __name__ == '__main__':
    main()
