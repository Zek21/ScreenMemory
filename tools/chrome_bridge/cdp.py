"""
Chrome Bridge CDP — Direct Chrome DevTools Protocol Controller
Ultra-fast browser automation via CDP, bypassing the extension relay chain.

Architecture:
    EXE → Chrome CDP WebSocket (1 hop, ~0.5ms latency)
    vs Extension path: Python → Hub → Extension → Chrome (3 hops, ~15ms latency)

Usage:
    from cdp import CDP

    chrome = CDP()                              # auto-attach to running Chrome
    chrome = CDP(port=9222)                     # specific debug port
    chrome = CDP.launch()                       # launch Chrome with debug port

    # Tab management
    tabs = chrome.tabs()
    tab = chrome.new_tab('https://example.com')
    chrome.close_tab(tab)
    chrome.activate_tab(tab)

    # JavaScript (no debugger banner, no extension needed)
    result = chrome.eval(tab, 'document.title')
    result = chrome.eval(tab, 'fetch("/api/data").then(r=>r.json())', await_promise=True)

    # Screenshots & PDF
    png_bytes = chrome.screenshot(tab)
    pdf_bytes = chrome.pdf(tab)

    # Input simulation (CDP Input domain — zero mouse interference)
    chrome.click(tab, x=100, y=200)
    chrome.click_selector(tab, '#submit-btn')
    chrome.type_text(tab, 'Hello World')
    chrome.press_key(tab, 'Enter')
    chrome.scroll(tab, x=0, y=500)

    # Network interception
    chrome.intercept_requests(tab, url_pattern='*/api/*')
    chrome.on_request(tab, lambda req: print(req['url']))
    chrome.on_response(tab, lambda resp: print(resp['status']))
    chrome.block_urls(tab, ['*.ads.*', '*/tracking/*'])

    # DOM queries
    node = chrome.query(tab, '#main-content')
    nodes = chrome.query_all(tab, '.item')
    html = chrome.outer_html(tab, node)
    chrome.set_attribute(tab, node, 'class', 'active')

    # Performance
    metrics = chrome.performance_metrics(tab)
    trace = chrome.start_trace(tab)
    chrome.stop_trace(tab)

    # Device emulation
    chrome.emulate_device(tab, 'iPhone 12')
    chrome.set_viewport(tab, width=1920, height=1080)
    chrome.throttle_network(tab, latency=100, download=1000, upload=500)

    # Cookies & Storage
    cookies = chrome.get_cookies(tab)
    chrome.set_cookie(tab, name='session', value='abc123', domain='.example.com')
    chrome.clear_cookies(tab)
    storage = chrome.get_local_storage(tab)

    # Console & Errors
    chrome.on_console(tab, lambda msg: print(msg))
    chrome.on_exception(tab, lambda err: print(err))

    # File downloads
    chrome.set_download_path(tab, 'C:/Downloads')
"""

import json
import time
import threading
import subprocess
import base64
import os
import sys
import re

# Used for window placement on Windows (optional)
import ctypes
import ctypes.wintypes
import socket
import struct
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

try:
    import websockets
    import asyncio
    HAS_ASYNC_WS = True
except ImportError:
    HAS_ASYNC_WS = False


# ─── CDP Connection ─────────────────────────────────────────────

class CDPError(Exception):
    pass


class CDPTab:
    """Represents a single Chrome tab with its own CDP WebSocket."""

    def __init__(self, ws_url, tab_info, timeout=30):
        self._ws_url = ws_url
        self._info = tab_info
        self._id = tab_info.get('id', '')
        self._msg_id = 0
        self._ws = None
        self._timeout = timeout
        self._callbacks = {}       # event_name -> [callback, ...]
        self._pending = {}         # msg_id -> threading.Event
        self._results = {}         # msg_id -> result
        self._listener = None
        self._connected = False
        self._lock = threading.Lock()

    @property
    def id(self):
        return self._id

    @property
    def url(self):
        return self._info.get('url', '')

    @property
    def title(self):
        return self._info.get('title', '')

    def connect(self):
        if self._connected:
            return
        if not HAS_WS:
            raise CDPError('websocket-client package required: pip install websocket-client')
        self._ws = websocket.WebSocket()
        self._ws.settimeout(self._timeout)
        self._ws.connect(self._ws_url)
        self._connected = True
        self._listener = threading.Thread(target=self._listen, daemon=True)
        self._listener.start()

    def disconnect(self):
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _listen(self):
        while self._connected:
            try:
                raw = self._ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                if 'id' in msg:
                    mid = msg['id']
                    with self._lock:
                        self._results[mid] = msg
                        evt = self._pending.get(mid)
                    if evt:
                        evt.set()
                elif 'method' in msg:
                    method = msg['method']
                    params = msg.get('params', {})
                    cbs = self._callbacks.get(method, [])
                    for cb in cbs:
                        try:
                            cb(method, params)
                        except Exception:
                            pass
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                if self._connected:
                    self._connected = False
                break

    def send(self, method, params=None, timeout=None):
        if not self._connected:
            self.connect()
        with self._lock:
            self._msg_id += 1
            mid = self._msg_id
            evt = threading.Event()
            self._pending[mid] = evt

        msg = {'id': mid, 'method': method}
        if params:
            msg['params'] = params

        self._ws.send(json.dumps(msg))
        t = timeout or self._timeout
        if not evt.wait(t):
            raise CDPError(f'Timeout waiting for {method} (id={mid})')

        with self._lock:
            result = self._results.pop(mid, None)
            self._pending.pop(mid, None)

        if result and 'error' in result:
            err = result['error']
            raise CDPError(f"CDP error {err.get('code')}: {err.get('message')}")
        return result.get('result', {}) if result else {}

    def on(self, event, callback):
        self._callbacks.setdefault(event, []).append(callback)

    def off(self, event, callback=None):
        if callback:
            cbs = self._callbacks.get(event, [])
            self._callbacks[event] = [c for c in cbs if c != callback]
        else:
            self._callbacks.pop(event, None)

    def enable(self, domain):
        self.send(f'{domain}.enable')

    def disable(self, domain):
        self.send(f'{domain}.disable')


# ─── Main CDP Controller ────────────────────────────────────────

DEVICE_PRESETS = {
    'iphone 12':     {'w': 390,  'h': 844,  'dpr': 3, 'mobile': True,  'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)'},
    'iphone 14 pro': {'w': 393,  'h': 852,  'dpr': 3, 'mobile': True,  'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'},
    'pixel 7':       {'w': 412,  'h': 915,  'dpr': 2.625, 'mobile': True, 'ua': 'Mozilla/5.0 (Linux; Android 13; Pixel 7)'},
    'ipad air':      {'w': 820,  'h': 1180, 'dpr': 2, 'mobile': True,  'ua': 'Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X)'},
    'desktop 1080p': {'w': 1920, 'h': 1080, 'dpr': 1, 'mobile': False, 'ua': ''},
    'desktop 1440p': {'w': 2560, 'h': 1440, 'dpr': 1, 'mobile': False, 'ua': ''},
}


class CDP:
    """
    Chrome DevTools Protocol direct controller.
    Zero-dependency Chrome automation — no extension, no hub, no mouse.
    """

    def __init__(self, host='127.0.0.1', port=9222, timeout=30):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._tabs = {}  # tab_id -> CDPTab
        self._base = f'http://{host}:{port}'
        self._verify_connection()

    @classmethod
    def launch(cls, chrome_path=None, port=9222, user_data_dir=None,
               headless=False, extra_args=None, timeout=30):
        """Launch Chrome with remote debugging enabled."""
        if not chrome_path:
            chrome_path = cls._find_chrome()
        if not chrome_path:
            raise CDPError('Chrome not found. Provide chrome_path=...')

        args = [chrome_path, f'--remote-debugging-port={port}', '--remote-allow-origins=*']
        if user_data_dir:
            args.append(f'--user-data-dir={user_data_dir}')
        if headless:
            args.append('--headless=new')

        extra_args = list(extra_args or [])

        # Auto-position window to avoid overlap (Windows only)
        if sys.platform == 'win32' and not headless:
            try:
                has_pos = any(a.startswith('--window-position') for a in extra_args)
                has_size = any(a.startswith('--window-size') for a in extra_args)
                has_max = any(a in ('--start-maximized', '--kiosk') or a.startswith('--start-fullscreen') for a in extra_args)

                def _virtual_screen():
                    user32 = ctypes.windll.user32
                    SM_XVIRTUALSCREEN = 76
                    SM_YVIRTUALSCREEN = 77
                    SM_CXVIRTUALSCREEN = 78
                    SM_CYVIRTUALSCREEN = 79
                    return {
                        'x': int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
                        'y': int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
                        'w': int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                        'h': int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
                    }

                def _list_chrome_rects():
                    user32 = ctypes.windll.user32
                    rects = []
                    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

                    def cb(hwnd, lparam):
                        if not user32.IsWindowVisible(hwnd):
                            return True
                        try:
                            if user32.IsIconic(hwnd):
                                return True
                        except Exception:
                            pass

                        cls_buf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cls_buf, 256)
                        cls_name = cls_buf.value
                        if not cls_name.startswith('Chrome_WidgetWin_'):
                            return True

                        r = ctypes.wintypes.RECT()
                        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                            return True
                        w = r.right - r.left
                        h = r.bottom - r.top
                        if w < 200 or h < 200:
                            return True
                        rects.append({'x': int(r.left), 'y': int(r.top), 'w': int(w), 'h': int(h)})
                        return True

                    user32.EnumWindows(EnumProc(cb), 0)
                    return rects

                def _intersect(a, b):
                    return not (
                        (a['x'] + a['w']) <= b['x'] or (b['x'] + b['w']) <= a['x'] or
                        (a['y'] + a['h']) <= b['y'] or (b['y'] + b['h']) <= a['y']
                    )

                def _pick_slot(w, h, margin=16):
                    screen = _virtual_screen()
                    existing = _list_chrome_rects()
                    max_x = screen['x'] + max(0, screen['w'] - w - margin)
                    max_y = screen['y'] + max(0, screen['h'] - h - margin)
                    step_x = max(40, w + margin)
                    step_y = max(40, h + margin)

                    y = screen['y'] + margin
                    while y <= max_y:
                        x = screen['x'] + margin
                        while x <= max_x:
                            cand = {'x': x, 'y': y, 'w': w, 'h': h}
                            if not any(_intersect(cand, r) for r in existing):
                                return x, y
                            x += step_x
                        y += step_y

                    n = len(existing)
                    return (
                        screen['x'] + margin + (n * 40) % max(1, (screen['w'] - w - margin)),
                        screen['y'] + margin + (n * 40) % max(1, (screen['h'] - h - margin)),
                    )

                if not has_pos and not has_max:
                    w, h = 1280, 800
                    if has_size:
                        for a in extra_args:
                            if a.startswith('--window-size='):
                                try:
                                    w_s, h_s = a.split('=', 1)[1].split(',', 1)
                                    w, h = int(w_s), int(h_s)
                                except Exception:
                                    pass
                                break

                    x, y = _pick_slot(w, h)
                    if not has_size:
                        extra_args.append(f'--window-size={w},{h}')
                    extra_args.append(f'--window-position={x},{y}')
            except Exception:
                pass

        args.extend(extra_args)

        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for Chrome to be ready
        for _ in range(timeout * 2):
            try:
                urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1)
                return cls(port=port, timeout=timeout)
            except Exception:
                time.sleep(0.5)
        raise CDPError(f'Chrome did not start on port {port} within {timeout}s')

    @classmethod
    def attach(cls, timeout=30):
        """Auto-attach to a running Chrome with debug port."""
        port = cls._find_debug_port()
        if port:
            return cls(port=port, timeout=timeout)
        raise CDPError(
            'No Chrome with remote debugging found. '
            'Start Chrome with --remote-debugging-port=9222 or use CDP.launch()'
        )

    @staticmethod
    def _find_chrome():
        candidates = [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    @staticmethod
    def _find_debug_port():
        """Find Chrome's debug port from its command line args."""
        if sys.platform == 'win32':
            try:
                out = subprocess.check_output(
                    ['wmic', 'process', 'where', "name='chrome.exe'",
                     'get', 'CommandLine', '/format:list'],
                    text=True, stderr=subprocess.DEVNULL, timeout=5
                )
                m = re.search(r'--remote-debugging-port=(\d+)', out)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
        else:
            try:
                out = subprocess.check_output(
                    ['ps', 'aux'], text=True, timeout=5
                )
                for line in out.splitlines():
                    if 'chrome' in line.lower():
                        m = re.search(r'--remote-debugging-port=(\d+)', line)
                        if m:
                            return int(m.group(1))
            except Exception:
                pass
        # Try common ports
        for port in [9222, 9229, 9223, 9224]:
            try:
                s = socket.create_connection(('127.0.0.1', port), timeout=0.5)
                s.close()
                urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1)
                return port
            except Exception:
                continue
        return None

    def _verify_connection(self):
        try:
            data = self._http_get('/json/version')
            self._browser_info = data
        except Exception as e:
            raise CDPError(
                f'Cannot connect to Chrome at {self._base}. '
                f'Start Chrome with --remote-debugging-port={self._port}\n{e}'
            )

    def _http_get(self, path):
        url = self._base + path
        req = Request(url, headers={'Accept': 'application/json'})
        with urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    def _http_put(self, path):
        url = self._base + path
        req = Request(url, method='PUT', headers={'Accept': 'application/json'})
        with urlopen(req, timeout=self._timeout) as resp:
            return resp.read()

    def _get_tab(self, tab_id):
        if tab_id in self._tabs:
            t = self._tabs[tab_id]
            if t._connected:
                return t
        # Refresh tab list and connect
        tabs = self._http_get('/json')
        for t in tabs:
            if t['id'] == tab_id and t.get('webSocketDebuggerUrl'):
                cdp_tab = CDPTab(t['webSocketDebuggerUrl'], t, self._timeout)
                cdp_tab.connect()
                self._tabs[tab_id] = cdp_tab
                return cdp_tab
        raise CDPError(f'Tab {tab_id} not found or not debuggable')

    # ─── Browser Info ──────────────────────────────────────

    def version(self):
        return self._http_get('/json/version')

    def protocol(self):
        return self._http_get('/json/protocol')

    # ─── Tab Management ────────────────────────────────────

    def tabs(self):
        """List all tabs."""
        return [t for t in self._http_get('/json') if t.get('type') == 'page']

    def new_tab(self, url='about:blank'):
        """Create a new tab."""
        from urllib.parse import quote
        data = self._http_get(f'/json/new?{quote(url, safe="")}')
        return data

    def close_tab(self, tab_id):
        """Close a tab."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        self._cleanup_tab(tab_id)
        self._http_get(f'/json/close/{tab_id}')

    def activate_tab(self, tab_id):
        """Bring a tab to foreground."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        self._http_get(f'/json/activate/{tab_id}')

    def _cleanup_tab(self, tab_id):
        t = self._tabs.pop(tab_id, None)
        if t:
            t.disconnect()

    # ─── Navigation ────────────────────────────────────────

    def navigate(self, tab_id, url, wait=True, timeout=30):
        """Navigate to URL. Optionally waits for load."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        result = tab.send('Page.navigate', {'url': url})

        if wait:
            self.wait_for_load(tab_id, timeout)
        return result

    def reload(self, tab_id, ignore_cache=False):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        return tab.send('Page.reload', {'ignoreCache': ignore_cache})

    def go_back(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        history = tab.send('Page.getNavigationHistory')
        idx = history.get('currentIndex', 0)
        if idx > 0:
            entries = history.get('entries', [])
            tab.send('Page.navigateToHistoryEntry', {'entryId': entries[idx - 1]['id']})

    def go_forward(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        history = tab.send('Page.getNavigationHistory')
        idx = history.get('currentIndex', 0)
        entries = history.get('entries', [])
        if idx < len(entries) - 1:
            tab.send('Page.navigateToHistoryEntry', {'entryId': entries[idx + 1]['id']})

    def wait_for_load(self, tab_id, timeout=30):
        """Wait for page load complete."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Page')
        loaded = threading.Event()
        def on_load(method, params):
            if method == 'Page.loadEventFired':
                loaded.set()
        tab.on('Page.loadEventFired', on_load)
        # Check if already loaded
        state = tab.send('Runtime.evaluate', {
            'expression': 'document.readyState',
            'returnByValue': True
        })
        if state.get('result', {}).get('value') == 'complete':
            tab.off('Page.loadEventFired', on_load)
            return
        loaded.wait(timeout)
        tab.off('Page.loadEventFired', on_load)

    def get_url(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        r = tab.send('Runtime.evaluate', {
            'expression': 'window.location.href',
            'returnByValue': True
        })
        return r.get('result', {}).get('value', '')

    # ─── JavaScript Evaluation ─────────────────────────────

    def eval(self, tab_id, expression, await_promise=False, return_by_value=True):
        """Execute JavaScript. No debugger banner, no extension needed."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        params = {
            'expression': expression,
            'returnByValue': return_by_value,
            'userGesture': True,
        }
        if await_promise:
            params['awaitPromise'] = True
        result = tab.send('Runtime.evaluate', params)
        r = result.get('result', {})
        if r.get('type') == 'undefined':
            return None
        if 'exceptionDetails' in result:
            exc = result['exceptionDetails']
            raise CDPError(f"JS error: {exc.get('text', '')} {exc.get('exception', {}).get('description', '')}")
        return r.get('value', r)

    def eval_function(self, tab_id, function_declaration, *args):
        """Call a function with arguments."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        result = tab.send('Runtime.callFunctionOn', {
            'functionDeclaration': function_declaration,
            'arguments': [{'value': a} for a in args],
            'executionContextId': 1,
            'returnByValue': True,
            'userGesture': True,
        })
        return result.get('result', {}).get('value')

    # ─── Screenshots & Visual ──────────────────────────────

    def screenshot(self, tab_id, format='png', quality=None, full_page=False,
                   clip=None, file_path=None):
        """Take screenshot. Returns bytes or saves to file."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        params = {'format': format}
        if quality is not None:
            params['quality'] = quality
        if full_page:
            metrics = tab.send('Page.getLayoutMetrics')
            content = metrics.get('contentSize', metrics.get('cssContentSize', {}))
            params['clip'] = {
                'x': 0, 'y': 0,
                'width': content.get('width', 1920),
                'height': content.get('height', 1080),
                'scale': 1,
            }
        elif clip:
            params['clip'] = {**clip, 'scale': clip.get('scale', 1)}

        result = tab.send('Page.captureScreenshot', params)
        data = base64.b64decode(result.get('data', ''))
        if file_path:
            with open(file_path, 'wb') as f:
                f.write(data)
        return data

    def pdf(self, tab_id, file_path=None, landscape=False,
            print_background=True, scale=1, paper_width=8.5, paper_height=11):
        """Generate PDF. Returns bytes or saves to file."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        result = tab.send('Page.printToPDF', {
            'landscape': landscape,
            'printBackground': print_background,
            'scale': scale,
            'paperWidth': paper_width,
            'paperHeight': paper_height,
        })
        data = base64.b64decode(result.get('data', ''))
        if file_path:
            with open(file_path, 'wb') as f:
                f.write(data)
        return data

    # ─── Input Simulation (CDP Input domain — NO MOUSE) ───

    def click(self, tab_id, x, y, button='left', click_count=1, modifiers=0):
        """Click at coordinates via CDP. Does NOT move real mouse."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        for event_type in ['mousePressed', 'mouseReleased']:
            tab.send('Input.dispatchMouseEvent', {
                'type': event_type,
                'x': x, 'y': y,
                'button': button,
                'clickCount': click_count,
                'modifiers': modifiers,
            })

    def click_selector(self, tab_id, selector, button='left'):
        """Click element by CSS selector. Finds center coordinates via JS."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        coords = self.eval(tab_id, f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                el.scrollIntoView({{block:'center'}});
                const r = el.getBoundingClientRect();
                return {{x: r.x + r.width/2, y: r.y + r.height/2}};
            }})()
        """)
        if not coords:
            raise CDPError(f'Element not found: {selector}')
        self.click(tab_id, coords['x'], coords['y'], button)

    def double_click(self, tab_id, x, y):
        self.click(tab_id, x, y, click_count=2)

    def right_click(self, tab_id, x, y):
        self.click(tab_id, x, y, button='right')

    def hover(self, tab_id, x, y):
        """Hover at coordinates via CDP."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Input.dispatchMouseEvent', {
            'type': 'mouseMoved', 'x': x, 'y': y,
        })

    def type_text(self, tab_id, text, delay=0):
        """Type text character by character. No keyboard needed."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        for char in text:
            tab.send('Input.dispatchKeyEvent', {
                'type': 'char', 'text': char,
            })
            if delay:
                time.sleep(delay / 1000)

    def press_key(self, tab_id, key, modifiers=0):
        """Press a key (Enter, Tab, Escape, ArrowDown, etc.)."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        key_map = {
            'Enter': {'code': 'Enter', 'key': 'Enter', 'windowsVirtualKeyCode': 13},
            'Tab': {'code': 'Tab', 'key': 'Tab', 'windowsVirtualKeyCode': 9},
            'Escape': {'code': 'Escape', 'key': 'Escape', 'windowsVirtualKeyCode': 27},
            'Backspace': {'code': 'Backspace', 'key': 'Backspace', 'windowsVirtualKeyCode': 8},
            'Delete': {'code': 'Delete', 'key': 'Delete', 'windowsVirtualKeyCode': 46},
            'ArrowUp': {'code': 'ArrowUp', 'key': 'ArrowUp', 'windowsVirtualKeyCode': 38},
            'ArrowDown': {'code': 'ArrowDown', 'key': 'ArrowDown', 'windowsVirtualKeyCode': 40},
            'ArrowLeft': {'code': 'ArrowLeft', 'key': 'ArrowLeft', 'windowsVirtualKeyCode': 37},
            'ArrowRight': {'code': 'ArrowRight', 'key': 'ArrowRight', 'windowsVirtualKeyCode': 39},
            'Home': {'code': 'Home', 'key': 'Home', 'windowsVirtualKeyCode': 36},
            'End': {'code': 'End', 'key': 'End', 'windowsVirtualKeyCode': 35},
            'PageUp': {'code': 'PageUp', 'key': 'PageUp', 'windowsVirtualKeyCode': 33},
            'PageDown': {'code': 'PageDown', 'key': 'PageDown', 'windowsVirtualKeyCode': 34},
            'Space': {'code': 'Space', 'key': ' ', 'windowsVirtualKeyCode': 32},
        }
        info = key_map.get(key, {'code': f'Key{key.upper()}', 'key': key, 'windowsVirtualKeyCode': ord(key.upper()) if len(key) == 1 else 0})
        for event_type in ['keyDown', 'keyUp']:
            tab.send('Input.dispatchKeyEvent', {
                'type': event_type,
                'key': info['key'],
                'code': info['code'],
                'windowsVirtualKeyCode': info['windowsVirtualKeyCode'],
                'modifiers': modifiers,
            })

    def scroll(self, tab_id, x=0, y=0, delta_x=0, delta_y=-100):
        """Scroll via CDP mouse wheel event."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Input.dispatchMouseEvent', {
            'type': 'mouseWheel',
            'x': x, 'y': y,
            'deltaX': delta_x, 'deltaY': delta_y,
        })

    def touch_tap(self, tab_id, x, y):
        """Simulate touch tap."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        touch_point = [{'x': x, 'y': y}]
        tab.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': touch_point})
        tab.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})

    def drag(self, tab_id, start_x, start_y, end_x, end_y, steps=10):
        """Drag from one point to another."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': start_x, 'y': start_y, 'button': 'left',
        })
        for i in range(1, steps + 1):
            ratio = i / steps
            x = start_x + (end_x - start_x) * ratio
            y = start_y + (end_y - start_y) * ratio
            tab.send('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': x, 'y': y, 'button': 'left',
            })
            time.sleep(0.01)
        tab.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': end_x, 'y': end_y, 'button': 'left',
        })

    def select_all(self, tab_id):
        """Ctrl+A."""
        self.press_key(tab_id, 'a', modifiers=2)  # 2 = Ctrl

    def copy(self, tab_id):
        """Ctrl+C."""
        self.press_key(tab_id, 'c', modifiers=2)

    def paste(self, tab_id):
        """Ctrl+V."""
        self.press_key(tab_id, 'v', modifiers=2)

    # ─── DOM Queries ───────────────────────────────────────

    def query(self, tab_id, selector):
        """Get DOM node by CSS selector."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('DOM')
        doc = tab.send('DOM.getDocument')
        root = doc['root']['nodeId']
        result = tab.send('DOM.querySelector', {
            'nodeId': root, 'selector': selector,
        })
        return result.get('nodeId', 0)

    def query_all(self, tab_id, selector):
        """Get all DOM nodes matching CSS selector."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('DOM')
        doc = tab.send('DOM.getDocument')
        root = doc['root']['nodeId']
        result = tab.send('DOM.querySelectorAll', {
            'nodeId': root, 'selector': selector,
        })
        return result.get('nodeIds', [])

    def outer_html(self, tab_id, node_id_or_selector):
        """Get outer HTML of a node."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        node_id = node_id_or_selector
        if isinstance(node_id_or_selector, str):
            node_id = self.query(tab_id, node_id_or_selector)
        result = tab.send('DOM.getOuterHTML', {'nodeId': node_id})
        return result.get('outerHTML', '')

    def set_attribute(self, tab_id, node_id_or_selector, name, value):
        """Set an attribute on a DOM node."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        node_id = node_id_or_selector
        if isinstance(node_id_or_selector, str):
            node_id = self.query(tab_id, node_id_or_selector)
        tab.send('DOM.setAttributeValue', {
            'nodeId': node_id, 'name': name, 'value': value,
        })

    def remove_node(self, tab_id, node_id_or_selector):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        node_id = node_id_or_selector
        if isinstance(node_id_or_selector, str):
            node_id = self.query(tab_id, node_id_or_selector)
        tab.send('DOM.removeNode', {'nodeId': node_id})

    def get_text(self, tab_id, selector=None):
        """Get text content of page or element."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        if selector:
            return self.eval(tab_id, f'document.querySelector({json.dumps(selector)})?.innerText || ""')
        return self.eval(tab_id, 'document.body.innerText')

    # ─── Network ───────────────────────────────────────────

    def enable_network(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')

    def intercept_requests(self, tab_id, url_patterns=None):
        """Enable request interception with URL patterns."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Fetch')
        patterns = []
        if url_patterns:
            for p in url_patterns:
                patterns.append({'urlPattern': p})
        else:
            patterns.append({'urlPattern': '*'})
        tab.send('Fetch.enable', {'patterns': patterns})

    def on_request(self, tab_id, callback):
        """Listen for network requests."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')
        tab.on('Network.requestWillBeSent', callback)

    def on_response(self, tab_id, callback):
        """Listen for network responses."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')
        tab.on('Network.responseReceived', callback)

    def block_urls(self, tab_id, urls):
        """Block specific URLs."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')
        tab.send('Network.setBlockedURLs', {'urls': urls})

    def get_response_body(self, tab_id, request_id):
        """Get response body for a specific request."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        result = tab.send('Network.getResponseBody', {'requestId': request_id})
        body = result.get('body', '')
        if result.get('base64Encoded'):
            body = base64.b64decode(body).decode('utf-8', errors='replace')
        return body

    def set_extra_headers(self, tab_id, headers):
        """Set extra HTTP headers for all requests."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')
        tab.send('Network.setExtraHTTPHeaders', {'headers': headers})

    def set_user_agent(self, tab_id, user_agent):
        """Override user agent."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Network.setUserAgentOverride', {'userAgent': user_agent})

    def clear_cache(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Network.clearBrowserCache')

    # ─── Cookies & Storage ─────────────────────────────────

    def get_cookies(self, tab_id=None, urls=None):
        """Get cookies."""
        if tab_id and isinstance(tab_id, dict):
            tab_id = tab_id['id']
        if tab_id:
            tab = self._get_tab(tab_id)
        else:
            tabs = self.tabs()
            if not tabs:
                return []
            tab = self._get_tab(tabs[0]['id'])
        params = {}
        if urls:
            params['urls'] = urls
        result = tab.send('Network.getCookies', params)
        return result.get('cookies', [])

    def set_cookie(self, tab_id, name, value, domain=None, path='/',
                   secure=False, http_only=False, same_site='Lax', expires=None):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        params = {
            'name': name, 'value': value, 'path': path,
            'secure': secure, 'httpOnly': http_only, 'sameSite': same_site,
        }
        if domain:
            params['domain'] = domain
        if expires:
            params['expires'] = expires
        return tab.send('Network.setCookie', params)

    def delete_cookies(self, tab_id, name, domain=None, url=None):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        params = {'name': name}
        if domain:
            params['domain'] = domain
        if url:
            params['url'] = url
        tab.send('Network.deleteCookies', params)

    def clear_cookies(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Network.clearBrowserCookies')

    def get_local_storage(self, tab_id, origin=None):
        """Get localStorage contents via JS eval."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        return self.eval(tab_id, '''
            (() => {
                const items = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    items[key] = localStorage.getItem(key);
                }
                return items;
            })()
        ''')

    def set_local_storage(self, tab_id, key, value):
        return self.eval(tab_id, f'localStorage.setItem({json.dumps(key)}, {json.dumps(value)})')

    def get_session_storage(self, tab_id):
        return self.eval(tab_id, '''
            (() => {
                const items = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    items[key] = sessionStorage.getItem(key);
                }
                return items;
            })()
        ''')

    # ─── Console & Errors ──────────────────────────────────

    def on_console(self, tab_id, callback):
        """Listen for console messages."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Runtime')
        def handler(method, params):
            if method == 'Runtime.consoleAPICalled':
                msg_type = params.get('type', 'log')
                args = params.get('args', [])
                text = ' '.join(a.get('value', a.get('description', '')) or '' for a in args)
                callback({'type': msg_type, 'text': text, 'args': args})
        tab.on('Runtime.consoleAPICalled', handler)

    def on_exception(self, tab_id, callback):
        """Listen for unhandled exceptions."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Runtime')
        def handler(method, params):
            if method == 'Runtime.exceptionThrown':
                callback(params.get('exceptionDetails', {}))
        tab.on('Runtime.exceptionThrown', handler)

    # ─── Performance ───────────────────────────────────────

    def performance_metrics(self, tab_id):
        """Get performance metrics."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Performance')
        result = tab.send('Performance.getMetrics')
        return {m['name']: m['value'] for m in result.get('metrics', [])}

    def start_trace(self, tab_id, categories=None):
        """Start performance trace."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        params = {}
        if categories:
            params['categories'] = ','.join(categories)
        tab.send('Tracing.start', params or {
            'categories': '-*,devtools.timeline,v8.execute,disabled-by-default-devtools.timeline'
        })

    def stop_trace(self, tab_id):
        """Stop performance trace and return data."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        events = []
        done = threading.Event()
        def on_data(method, params):
            if method == 'Tracing.dataCollected':
                events.extend(params.get('value', []))
            elif method == 'Tracing.tracingComplete':
                done.set()
        tab.on('Tracing.dataCollected', on_data)
        tab.on('Tracing.tracingComplete', on_data)
        tab.send('Tracing.end')
        done.wait(30)
        tab.off('Tracing.dataCollected')
        tab.off('Tracing.tracingComplete')
        return events

    # ─── Device Emulation ──────────────────────────────────

    def emulate_device(self, tab_id, device_name):
        """Emulate a mobile device."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        preset = DEVICE_PRESETS.get(device_name.lower())
        if not preset:
            raise CDPError(f'Unknown device: {device_name}. Available: {", ".join(DEVICE_PRESETS.keys())}')
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setDeviceMetricsOverride', {
            'width': preset['w'], 'height': preset['h'],
            'deviceScaleFactor': preset['dpr'], 'mobile': preset['mobile'],
        })
        if preset.get('ua'):
            tab.send('Network.setUserAgentOverride', {'userAgent': preset['ua']})

    def set_viewport(self, tab_id, width=1920, height=1080, device_scale_factor=1, mobile=False):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height,
            'deviceScaleFactor': device_scale_factor, 'mobile': mobile,
        })

    def clear_device_override(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.clearDeviceMetricsOverride')

    def throttle_network(self, tab_id, offline=False, latency=0,
                         download_throughput=-1, upload_throughput=-1):
        """Throttle network conditions."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Network')
        tab.send('Network.emulateNetworkConditions', {
            'offline': offline,
            'latency': latency,
            'downloadThroughput': download_throughput,
            'uploadThroughput': upload_throughput,
        })

    def set_geolocation(self, tab_id, latitude, longitude, accuracy=1):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setGeolocationOverride', {
            'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy,
        })

    def set_timezone(self, tab_id, timezone_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setTimezoneOverride', {'timezoneId': timezone_id})

    def set_locale(self, tab_id, locale):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setLocaleOverride', {'locale': locale})

    def dark_mode(self, tab_id, enabled=True):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Emulation.setEmulatedMedia', {
            'features': [{'name': 'prefers-color-scheme', 'value': 'dark' if enabled else 'light'}]
        })

    # ─── Downloads ─────────────────────────────────────────

    def set_download_path(self, tab_id, path):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.send('Browser.setDownloadBehavior', {
            'behavior': 'allow', 'downloadPath': os.path.abspath(path),
        })

    # ─── CSS ───────────────────────────────────────────────

    def get_computed_style(self, tab_id, selector):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        return self.eval(tab_id, f'''
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                const cs = getComputedStyle(el);
                const result = {{}};
                for (const prop of cs) result[prop] = cs.getPropertyValue(prop);
                return result;
            }})()
        ''')

    def inject_css(self, tab_id, css):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('CSS')
        return tab.send('CSS.addRule', {
            'styleSheetId': '', 'ruleText': css, 'location': {'startLine': 0, 'startColumn': 0},
        })

    # ─── Accessibility ─────────────────────────────────────

    def accessibility_tree(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Accessibility')
        result = tab.send('Accessibility.getFullAXTree')
        return result.get('nodes', [])

    # ─── Security ──────────────────────────────────────────

    def security_info(self, tab_id):
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        tab.enable('Security')
        return self.eval(tab_id, '''
            (() => ({
                protocol: location.protocol,
                secure: location.protocol === 'https:',
                host: location.host,
            }))()
        ''')

    # ─── Utility ───────────────────────────────────────────

    def wait(self, seconds):
        time.sleep(seconds)

    def wait_for_selector(self, tab_id, selector, timeout=30):
        """Wait for an element to appear in the DOM."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        start = time.time()
        while time.time() - start < timeout:
            found = self.eval(tab_id, f'!!document.querySelector({json.dumps(selector)})')
            if found:
                return True
            time.sleep(0.25)
        raise CDPError(f'Timeout waiting for {selector}')

    def wait_for_text(self, tab_id, text, timeout=30):
        """Wait for text to appear on page."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        start = time.time()
        while time.time() - start < timeout:
            page_text = self.eval(tab_id, 'document.body.innerText')
            if text in (page_text or ''):
                return True
            time.sleep(0.25)
        raise CDPError(f'Timeout waiting for text: {text}')

    def wait_for_url(self, tab_id, url_pattern, timeout=30):
        """Wait for URL to match pattern."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        start = time.time()
        while time.time() - start < timeout:
            current = self.get_url(tab_id)
            if re.search(url_pattern, current):
                return current
            time.sleep(0.25)
        raise CDPError(f'Timeout waiting for URL: {url_pattern}')

    def fill_form(self, tab_id, data):
        """Fill form fields. data = {selector: value, ...}"""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        for selector, value in data.items():
            self.click_selector(tab_id, selector)
            self.select_all(tab_id)
            self.type_text(tab_id, str(value))

    def get_page_info(self, tab_id):
        """Get comprehensive page info."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        return self.eval(tab_id, '''
            (() => ({
                title: document.title,
                url: location.href,
                readyState: document.readyState,
                domain: document.domain,
                referrer: document.referrer,
                charset: document.characterSet,
                doctype: document.doctype ? document.doctype.name : null,
                links: document.links.length,
                images: document.images.length,
                forms: document.forms.length,
                scripts: document.scripts.length,
                stylesheets: document.styleSheets.length,
                cookies: document.cookie ? document.cookie.split(';').length : 0,
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                },
                performance: {
                    loadTime: performance.timing.loadEventEnd - performance.timing.navigationStart,
                    domReady: performance.timing.domContentLoadedEventEnd - performance.timing.navigationStart,
                },
            }))()
        ''')

    def extract_links(self, tab_id, filter_pattern=None):
        """Extract all links from page."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        links = self.eval(tab_id, '''
            Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: a.innerText.trim().substring(0, 200),
                href: a.href,
                target: a.target || '_self',
            }))
        ''')
        if filter_pattern and links:
            links = [l for l in links if re.search(filter_pattern, l.get('href', ''))]
        return links

    def extract_meta(self, tab_id):
        """Extract meta tags."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        return self.eval(tab_id, '''
            Array.from(document.querySelectorAll('meta')).map(m => ({
                name: m.name || m.getAttribute('property') || '',
                content: m.content || '',
                httpEquiv: m.httpEquiv || '',
            }))
        ''')

    def extract_tables(self, tab_id, selector='table'):
        """Extract table data."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        return self.eval(tab_id, f'''
            Array.from(document.querySelectorAll({json.dumps(selector)})).map(table => {{
                const headers = Array.from(table.querySelectorAll('th')).map(th => th.innerText.trim());
                const rows = Array.from(table.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())
                ).filter(r => r.length > 0);
                return {{headers, rows, rowCount: rows.length}};
            }})
        ''')

    # ─── Advanced: Raw CDP ─────────────────────────────────

    def raw(self, tab_id, method, params=None):
        """Send raw CDP command."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']
        tab = self._get_tab(tab_id)
        return tab.send(method, params)

    # ─── Cleanup ───────────────────────────────────────────

    def close(self):
        """Disconnect all tabs."""
        for tab in self._tabs.values():
            tab.disconnect()
        self._tabs.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


# ─── Convenience: auto-attach ──────────────────────────────────

def connect(port=None, timeout=30):
    """Connect to Chrome. Auto-discovers port if not specified."""
    if port:
        return CDP(port=port, timeout=timeout)
    return CDP.attach(timeout=timeout)


def launch(url=None, headless=False, port=9222, **kwargs):
    """Launch Chrome and connect."""
    chrome = CDP.launch(port=port, headless=headless, **kwargs)
    if url:
        tabs = chrome.tabs()
        if tabs:
            chrome.navigate(tabs[0]['id'], url)
    return chrome
