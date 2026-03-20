"""Install Chrome Bridge extension via CDP Shadow DOM interaction.
Uses Win32 PostMessage for file dialog (no pyautogui).
# signed: gamma
"""
import sys, json, time, ctypes, ctypes.wintypes, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../tools/chrome_bridge'))

from cdp import CDP

EXTENSION_DIR = r'D:\Prospects\ScreenMemory\tools\chrome_bridge\extension'
CDP_PORT = 9222

# Win32 constants
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
VK_RETURN = 0x0D
IDOK = 1

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

FindWindowW = user32.FindWindowW
FindWindowExW = user32.FindWindowExW
SendMessageW = user32.SendMessageW
PostMessageW = user32.PostMessageW
EnumChildWindows = user32.EnumChildWindows
GetClassNameW = user32.GetClassNameW
GetWindowTextW = user32.GetWindowTextW
SetForegroundWindow = user32.SetForegroundWindow

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    GetWindowTextW(hwnd, buf, 512)
    return buf.value


def find_child_by_class(parent, cls_name):
    """Find first child window with given class name."""
    result = []
    def callback(hwnd, _):
        if get_class_name(hwnd) == cls_name:
            result.append(hwnd)
            return False  # stop enumeration
        return True
    EnumChildWindows(parent, WNDENUMPROC(callback), 0)
    return result[0] if result else None


def find_all_children(parent):
    """List all child windows with class and text."""
    children = []
    def callback(hwnd, _):
        cls = get_class_name(hwnd)
        txt = get_window_text(hwnd)
        children.append((hwnd, cls, txt))
        return True
    EnumChildWindows(parent, WNDENUMPROC(callback), 0)
    return children


def handle_file_dialog_win32(ext_path):
    """Handle the 'Select Folder' file dialog using Win32 APIs only.
    No pyautogui, no keybd_event, no SendInput.
    """
    print("[DIALOG] Waiting for file dialog...")
    dialog_hwnd = None
    for attempt in range(30):  # wait up to 15 seconds
        # Enumerate ALL #32770 windows and find the file dialog
        candidates = []
        def enum_toplevel(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                cls = get_class_name(hwnd)
                if cls == '#32770':
                    txt = get_window_text(hwnd)
                    candidates.append((hwnd, txt))
            return True
        user32.EnumWindows(WNDENUMPROC(enum_toplevel), 0)
        
        for hwnd, txt in candidates:
            # File dialog usually has title like "Select Folder", "Open", "Browse For Folder"
            # or is a Chrome-owned dialog (no title but has DUIViewWndClassName child)
            children = find_all_children(hwnd)
            child_classes = [c[1] for c in children]
            has_edit = any(c == 'Edit' for c in child_classes)
            has_combo = any('ComboBox' in c for c in child_classes)
            has_dui = any('DUIViewWnd' in c for c in child_classes)
            has_shelldll = any('ShellDll' in c for c in child_classes)
            has_button_stop = any(c[2] == 'Stop Sharing' for c in children if c[1] == 'Button')
            
            # Skip the screen sharing notification
            if has_button_stop:
                continue
            
            # File dialog should have Edit/ComboBox or DUI/ShellDll
            if has_edit or has_combo or has_dui or has_shelldll:
                dialog_hwnd = hwnd
                print(f"[DIALOG] Found file dialog: hwnd={hwnd}, title='{txt}', children={len(children)}")
                break
        
        if dialog_hwnd:
            break
        time.sleep(0.5)

    if not dialog_hwnd:
        print("[DIALOG] ERROR: No file dialog found after 15s")
        # List all visible #32770 windows for debugging
        candidates2 = []
        def enum2(hwnd, _):
            if user32.IsWindowVisible(hwnd) and get_class_name(hwnd) == '#32770':
                candidates2.append((hwnd, get_window_text(hwnd)))
            return True
        user32.EnumWindows(WNDENUMPROC(enum2), 0)
        print(f"[DIALOG] Visible #32770 windows: {candidates2}")
        return False

    time.sleep(1)  # let dialog fully render

    # List all children to find the path edit control
    children = find_all_children(dialog_hwnd)
    print(f"[DIALOG] Found {len(children)} child windows")
    for hwnd, cls, txt in children[:20]:
        print(f"  hwnd={hwnd} class='{cls}' text='{txt[:60]}'")

    # The file dialog has this hierarchy:
    # #32770 (dialog)
    #   DUIViewWndClassName (main content)
    #     DirectUIHWND
    #       ... (file browser)
    #   ComboBoxEx32 (address bar area)
    #     ComboBox
    #       Edit (the path input)
    #   Button "Select Folder" (or OK)

    # Strategy 1: Find Edit control in ComboBoxEx32 hierarchy
    edit_hwnd = None
    
    # Try finding ComboBoxEx32 -> ComboBox -> Edit
    combo_ex = find_child_by_class(dialog_hwnd, 'ComboBoxEx32')
    if combo_ex:
        combo = find_child_by_class(combo_ex, 'ComboBox')
        if combo:
            edit_hwnd = find_child_by_class(combo, 'Edit')
            if edit_hwnd:
                print(f"[DIALOG] Found Edit via ComboBoxEx32 chain: {edit_hwnd}")

    # Strategy 2: Look for any Edit control
    if not edit_hwnd:
        for hwnd, cls, txt in children:
            if cls == 'Edit':
                edit_hwnd = hwnd
                print(f"[DIALOG] Found Edit control: {edit_hwnd}")
                break

    if not edit_hwnd:
        print("[DIALOG] ERROR: No Edit control found in dialog")
        return False

    # Set the path text via WM_SETTEXT (no cursor movement needed)
    # Use c_wchar_p for proper 64-bit pointer handling
    path_ptr = ctypes.c_wchar_p(ext_path)
    # Define SendMessageW with proper LRESULT/LPARAM types for 64-bit
    _SendMessageW = ctypes.windll.user32.SendMessageW
    _SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.c_void_p]
    _SendMessageW.restype = ctypes.c_longlong
    
    _SendMessageW(edit_hwnd, WM_SETTEXT, 0, path_ptr)
    time.sleep(0.5)

    # Verify text was set
    verify_buf = ctypes.create_unicode_buffer(1024)
    _SendMessageW(edit_hwnd, 0x000D, 1024, verify_buf)  # WM_GETTEXT
    print(f"[DIALOG] Edit text set to: '{verify_buf.value}'")

    # Press Enter in the edit control to navigate to the path
    PostMessageW(edit_hwnd, WM_KEYDOWN, VK_RETURN, 0)
    time.sleep(0.3)
    PostMessageW(edit_hwnd, WM_KEYUP, VK_RETURN, 0)
    time.sleep(1)

    # Now we need to press Enter again or click "Select Folder" to confirm
    # The folder dialog may have navigated to the folder, now needs a second Enter
    # or we click the "Select Folder" button
    
    # Find the "Select Folder" or OK button
    select_btn = None
    for hwnd, cls, txt in find_all_children(dialog_hwnd):
        if cls == 'Button' and ('Select' in txt or 'OK' in txt or 'Open' in txt):
            select_btn = hwnd
            print(f"[DIALOG] Found button: hwnd={select_btn}, text='{txt}'")
            break

    if select_btn:
        SendMessageW(select_btn, BM_CLICK, 0, 0)
        print("[DIALOG] Clicked Select Folder button via BM_CLICK")
    else:
        # Fallback: press Enter on dialog
        PostMessageW(dialog_hwnd, WM_KEYDOWN, VK_RETURN, 0)
        time.sleep(0.1)
        PostMessageW(dialog_hwnd, WM_KEYUP, VK_RETURN, 0)
        print("[DIALOG] Sent Enter to dialog as fallback")

    time.sleep(2)

    # Verify dialog closed
    if not user32.IsWindow(dialog_hwnd):
        print("[DIALOG] Dialog closed successfully")
        return True
    else:
        # Try another Enter
        print("[DIALOG] Dialog still open, trying Enter again...")
        PostMessageW(dialog_hwnd, WM_KEYDOWN, VK_RETURN, 0)
        time.sleep(0.1)
        PostMessageW(dialog_hwnd, WM_KEYUP, VK_RETURN, 0)
        time.sleep(2)
        if not user32.IsWindow(dialog_hwnd):
            print("[DIALOG] Dialog closed on second attempt")
            return True
        print("[DIALOG] Dialog still open after 2 attempts")
        return False


def main():
    print(f"[INIT] Connecting to Chrome CDP on port {CDP_PORT}...")
    c = CDP(port=CDP_PORT)
    tabs = c.tabs()
    print(f"[INIT] {len(tabs)} tabs found")

    # Navigate to chrome://extensions
    tab_id = tabs[0]['id']
    print("[NAV] Navigating to chrome://extensions...")
    try:
        c.navigate(tab_id, 'chrome://extensions', wait=True, timeout=10)
    except Exception as e:
        print(f"[NAV] Navigate exception (may be OK for chrome:// URLs): {e}")
        time.sleep(3)

    title = c.eval(tab_id, 'document.title')
    print(f"[NAV] Page title: {title}")
    url = c.eval(tab_id, 'window.location.href')
    print(f"[NAV] URL: {url}")

    # Check if already on extensions page
    if 'extensions' not in str(url).lower():
        print("[NAV] Not on extensions page, retrying...")
        try:
            c.navigate(tab_id, 'chrome://extensions', wait=False)
        except Exception:  # signed: beta
            pass
        time.sleep(3)
        url = c.eval(tab_id, 'window.location.href')
        print(f"[NAV] URL after retry: {url}")

    # Step 1: Check existing extensions
    js_check_exts = '''
    (function() {
        var mgr = document.querySelector('extensions-manager');
        if (!mgr || !mgr.shadowRoot) return JSON.stringify({error: 'no-manager'});
        var il = mgr.shadowRoot.querySelector('extensions-item-list');
        if (!il || !il.shadowRoot) return JSON.stringify({error: 'no-item-list'});
        var items = il.shadowRoot.querySelectorAll('extensions-item');
        var result = [];
        items.forEach(function(item) {
            var sr = item.shadowRoot;
            if (sr) {
                var n = sr.querySelector('#name');
                result.push(n ? n.textContent.trim() : 'unknown');
            }
        });
        return JSON.stringify({extensions: result});
    })()
    '''
    exts_json = c.eval(tab_id, js_check_exts)
    print(f"[CHECK] Current extensions: {exts_json}")

    exts_data = json.loads(exts_json) if isinstance(exts_json, str) else {}
    if 'Chrome Bridge' in str(exts_data.get('extensions', [])):
        print("[CHECK] Chrome Bridge already installed!")
        return True

    # Step 2: Enable Developer Mode
    js_enable_dev = '''
    (function() {
        var mgr = document.querySelector('extensions-manager');
        if (!mgr || !mgr.shadowRoot) return 'no-manager';
        var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
        if (!toolbar || !toolbar.shadowRoot) return 'no-toolbar';
        var toggle = toolbar.shadowRoot.querySelector('#devMode');
        if (!toggle) return 'no-devMode-toggle';
        // Check if already enabled
        if (toggle.checked) return 'already-enabled';
        toggle.click();
        return 'enabled';
    })()
    '''
    dev_result = c.eval(tab_id, js_enable_dev)
    print(f"[DEVMODE] Result: {dev_result}")
    time.sleep(1)

    # Step 3: Click "Load unpacked"
    js_load_unpacked = '''
    (function() {
        var mgr = document.querySelector('extensions-manager');
        if (!mgr || !mgr.shadowRoot) return 'no-manager';
        var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
        if (!toolbar || !toolbar.shadowRoot) return 'no-toolbar';
        var btn = toolbar.shadowRoot.querySelector('#loadUnpacked');
        if (!btn) return 'no-loadUnpacked-button';
        btn.click();
        return 'clicked';
    })()
    '''
    load_result = c.eval(tab_id, js_load_unpacked)
    print(f"[LOAD] Click result: {load_result}")

    if load_result != 'clicked':
        print(f"[LOAD] FAILED to click Load Unpacked: {load_result}")
        return False

    time.sleep(2)

    # Step 4: Handle the file dialog
    dialog_ok = handle_file_dialog_win32(EXTENSION_DIR)
    if not dialog_ok:
        print("[INSTALL] File dialog handling failed")
        return False

    time.sleep(3)

    # Step 5: Verify extension installed
    exts_json2 = c.eval(tab_id, js_check_exts)
    print(f"[VERIFY] Extensions after install: {exts_json2}")

    exts_data2 = json.loads(exts_json2) if isinstance(exts_json2, str) else {}
    installed = exts_data2.get('extensions', [])
    if 'Chrome Bridge' in installed:
        print("[SUCCESS] Chrome Bridge extension installed successfully!")
        return True
    else:
        print(f"[VERIFY] Chrome Bridge not found in: {installed}")
        # It may have a different name, check more broadly
        for ext in installed:
            if 'bridge' in ext.lower() or 'chrome' in ext.lower():
                print(f"[VERIFY] Possible match: {ext}")
                return True
        return False


if __name__ == '__main__':
    try:
        success = main()
        print(f"\n{'SUCCESS' if success else 'FAILED'}: Extension installation {'completed' if success else 'failed'}")
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
