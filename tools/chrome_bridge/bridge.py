"""
Chrome Bridge v3.1 — Python Client
High-speed browser automation client with direct worker transport,
smart element finding, workflow engine, visual regression,
session recording, cookie profiles, multi-tab orchestration,
real-time event streaming, command pipeline, and smart navigation.

Usage:
    from bridge import Hub

    hub = Hub()                          # connect (auto-retry on failures)
    chrome = hub.chrome()                # use first available profile

    # ── Smart Element Finding (AI-like, finds by description) ──
    chrome.smart_click(tab_id, 'Login')               # finds and clicks login button
    chrome.smart_fill(tab_id, 'Email', 'x@y.com')     # finds email field and fills
    chrome.smart_wait(tab_id, 'Submit')                # waits for submit to appear
    chrome.heal_click(tab_id, '#btn', text='Submit')   # auto-heals if selector breaks

    # ── Real-Time Event Streaming (v3.0) ──
    chrome.subscribe('network.request', 'console.message', tab_id=tab_id)
    chrome.on('network.request', lambda e, d: print(f"→ {d['url']}"))
    chrome.on('console.message', lambda e, d: print(f"[{d['type']}] {d['text']}"))
    chrome.on('navigation.completed', lambda e, d: print(f"Loaded: {d['url']}"))

    # ── Command Pipeline (v3.0 — massive speed boost) ──
    pipe = chrome.pipeline()
    pipe.add('tabs.list')
    pipe.add('page.info', tabId=tab_id)
    pipe.add('screenshot', tabId=tab_id)
    results = pipe.execute()  # sends all 3 at once, returns all results

    # ── Smart Navigation (v3.0 — auto-waits for page load) ──
    chrome.navigate(tab_id, 'https://example.com')     # waits for load automatically
    chrome.navigate(tab_id, 'https://fast.com', wait=False)  # fire-and-forget

    # ── Workflow Engine (multi-step automation as JSON) ──
    chrome.workflow([
        {'command': 'tabs.create', 'params': {'url': 'https://example.com'}, 'as': 'tab'},
        {'command': 'wait.element', 'params': {'tabId': '{{tab.id}}', 'selector': 'h1'}},
        {'command': 'screenshot', 'params': {'tabId': '{{tab.id}}'}},
    ])

    # ── Stealth Mode (anti-bot detection) ──
    chrome.stealth_enable(tab_id)         # basic stealth
    chrome.stealth_inject(tab_id)         # nuclear: inject BEFORE page JS runs
    chrome.rotate_ua(tab_id)              # random user agent rotation
    chrome.stealth_check(tab_id)          # verify detection status

    # ── Session Recording ──
    chrome.record_start(tab_id)
    # ... user does stuff ...
    events = chrome.record_stop(tab_id)
    chrome.record_replay(tab_id, events['events'], speed=2)

    # ── Visual Regression ──
    chrome.visual_capture(tab_id, 'homepage')
    # ... page changes ...
    diff = chrome.visual_compare(tab_id, 'homepage')   # match: True/False

    # ── Multi-Tab Orchestration ──
    chrome.multi_eval('document.title')                # run on ALL tabs
    chrome.multi_close(url_pattern='spam.com')         # close matching tabs
    chrome.multi_navigate([{'tabId': 1, 'url': 'a'}, {'tabId': 2, 'url': 'b'}])

    # ── Cookie Profiles ──
    chrome.export_cookies('cookies.json')
    chrome.import_cookies(filepath='cookies.json')

    # ── Coverage & HAR ──
    report = chrome.coverage_report(tab_id)
    chrome.har_start(tab_id)
    har = chrome.har_stop(tab_id, 'traffic.json')

    # ── Response Interception ──
    chrome.fetch_enable(tab_id)
    chrome.fetch_disable(tab_id)

    # ── Hub Diagnostics ──
    chrome.hub_health()                    # agents, latency, uptime
    chrome.hub_log(limit=20)               # recent command log

    # Async support
    from bridge import AsyncHub
    async with AsyncHub() as hub:
        chrome = hub.chrome()
        await chrome.smart_click(tab_id, 'Submit')
"""
import asyncio
import json
import base64
import os
import time
import threading
from collections import defaultdict

try:
    import websockets
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets


def _normalize_bridge_result(result):
    if isinstance(result, dict):
        if "__error" in result:
            raise RuntimeError(result["__error"])
        if "error" in result and len(result) == 1:
            raise RuntimeError(result["error"])
    return result


DEFAULT_HUB_URL = "ws://127.0.0.1:7777"


def _resolve_hub_url(url=None):
    return url or os.environ.get("CHROME_BRIDGE_URL", DEFAULT_HUB_URL)


class Chrome:
    """Controls a single Chrome profile through the hub — 130+ commands."""

    def __init__(self, hub, target=None):
        self._hub = hub
        self._target = target

    def _cmd(self, command, **params):
        return _normalize_bridge_result(self._hub._send(command, params, self._target))

    # ════════════════════════════════════════
    # ── Tabs ──
    # ════════════════════════════════════════

    def tabs(self):
        """List all tabs."""
        return self._cmd('tabs.list')

    def tab(self, tab_id):
        """Get a specific tab by ID."""
        return self._cmd('tabs.get', tabId=tab_id)

    def find(self, url='', title=''):
        """Find tabs matching URL or title substring."""
        results = self._cmd('tabs.find', url=url, title=title)
        return results if isinstance(results, list) else []

    def find_one(self, url='', title=''):
        """Find first tab matching URL or title."""
        results = self.find(url=url, title=title)
        return results[0] if results else None

    def new_tab(self, url='about:blank', active=True):
        """Create a new tab."""
        return self._cmd('tabs.create', url=url, active=active)

    def navigate(self, tab_id, url, wait=True, timeout=30):
        """Navigate tab to URL. wait=True uses smart navigation wait (no more sleep!)."""
        if wait:
            self._cmd('tabs.navigate', tabId=tab_id, url=url, waitUntil='load', timeout=timeout * 1000)
        else:
            self._cmd('tabs.navigate', tabId=tab_id, url=url)
        return self

    def close_tab(self, tab_id):
        """Close tab(s). Accepts single ID or list."""
        return self._cmd('tabs.close', tabId=tab_id)

    def activate(self, tab_id, focus_window=True):
        """Activate/focus a tab."""
        return self._cmd('tabs.activate', tabId=tab_id, focusWindow=focus_window)

    def reload(self, tab_id, bypass_cache=False):
        """Reload a tab."""
        return self._cmd('tabs.reload', tabId=tab_id, bypassCache=bypass_cache)

    def duplicate(self, tab_id):
        """Duplicate a tab."""
        return self._cmd('tabs.duplicate', tabId=tab_id)

    def move_tab(self, tab_id, index=-1, window_id=None):
        """Move tab to position."""
        return self._cmd('tabs.move', tabId=tab_id, index=index, windowId=window_id)

    def pin(self, tab_id, pinned=True):
        """Pin/unpin a tab."""
        return self._cmd('tabs.pin', tabId=tab_id, pinned=pinned)

    def mute(self, tab_id, muted=True):
        """Mute/unmute a tab."""
        return self._cmd('tabs.mute', tabId=tab_id, muted=muted)

    def discard(self, tab_id):
        """Discard tab to free memory."""
        return self._cmd('tabs.discard', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Tab Groups ──
    # ════════════════════════════════════════

    def tab_groups(self):
        """List all tab groups."""
        return self._cmd('tabGroups.list')

    def group_tabs(self, tab_ids, title=None, color=None):
        """Group tabs together."""
        return self._cmd('tabGroups.create', tabIds=tab_ids, title=title, color=color)

    def update_group(self, group_id, **kwargs):
        """Update tab group properties."""
        return self._cmd('tabGroups.update', groupId=group_id, **kwargs)

    def ungroup_tabs(self, tab_ids):
        """Remove tabs from their group."""
        return self._cmd('tabGroups.ungroup', tabIds=tab_ids)

    def add_to_group(self, group_id, tab_ids):
        """Add tabs to an existing group."""
        return self._cmd('tabGroups.addTabs', groupId=group_id, tabIds=tab_ids)

    # ════════════════════════════════════════
    # ── Windows ──
    # ════════════════════════════════════════

    def _virtual_screen_rect(self):
        """Best-effort virtual screen rect (Windows only)."""
        if os.name != 'nt':
            return None
        try:
            import ctypes
            SM_XVIRTUALSCREEN = 76
            SM_YVIRTUALSCREEN = 77
            SM_CXVIRTUALSCREEN = 78
            SM_CYVIRTUALSCREEN = 79
            user32 = ctypes.windll.user32
            return {
                'x': int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
                'y': int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
                'w': int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                'h': int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
            }
        except Exception:
            return None

    @staticmethod
    def _rects_intersect(a, b):
        return not (
            (a['x'] + a['w']) <= b['x'] or (b['x'] + b['w']) <= a['x'] or
            (a['y'] + a['h']) <= b['y'] or (b['y'] + b['h']) <= a['y']
        )

    def _find_open_window_slot(self, width, height, margin=16):
        """Find a non-overlapping (left, top) within the virtual screen."""
        try:
            wins = self.windows() or []
        except Exception:
            wins = []

        existing = []
        for w in wins:
            if w.get('state') == 'minimized':
                continue
            left = w.get('left')
            top = w.get('top')
            ww = w.get('width')
            hh = w.get('height')
            if left is None or top is None or not ww or not hh:
                continue
            existing.append({'x': int(left), 'y': int(top), 'w': int(ww), 'h': int(hh)})

        screen = self._virtual_screen_rect()
        if not screen and existing:
            min_x = min(r['x'] for r in existing)
            min_y = min(r['y'] for r in existing)
            max_x = max(r['x'] + r['w'] for r in existing)
            max_y = max(r['y'] + r['h'] for r in existing)
            screen = {'x': min_x, 'y': min_y, 'w': max_x - min_x, 'h': max_y - min_y}
        if not screen:
            screen = {'x': 0, 'y': 0, 'w': 1920, 'h': 1080}

        # Grid scan (true non-overlap if a free cell exists)
        max_x = screen['x'] + max(0, screen['w'] - width - margin)
        max_y = screen['y'] + max(0, screen['h'] - height - margin)
        step_x = max(40, width + margin)
        step_y = max(40, height + margin)

        y = screen['y'] + margin
        while y <= max_y:
            x = screen['x'] + margin
            while x <= max_x:
                cand = {'x': x, 'y': y, 'w': width, 'h': height}
                if not any(self._rects_intersect(cand, r) for r in existing):
                    return x, y
                x += step_x
            y += step_y

        # Fallback: cascade
        n = len(existing)
        return screen['x'] + margin + (n * 40) % max(1, (screen['w'] - width - margin)), \
               screen['y'] + margin + (n * 40) % max(1, (screen['h'] - height - margin))

    def windows(self):
        """List all windows."""
        return self._cmd('windows.list')

    def focus_window(self, window_id):
        """Focus a window."""
        return self._cmd('windows.focus', windowId=window_id)

    def new_window(self, url=None, **kwargs):
        """Create a new window (auto-positions to avoid overlap by default)."""
        state = (kwargs.get('state') or 'normal').lower() if isinstance(kwargs.get('state'), str) or kwargs.get('state') is None else kwargs.get('state')
        if state in ('maximized', 'fullscreen', 'minimized'):
            return self._cmd('windows.create', url=url, **kwargs)

        if 'left' not in kwargs and 'top' not in kwargs:
            width = kwargs.get('width')
            height = kwargs.get('height')

            if not width or not height:
                try:
                    wins = self.windows() or []
                    base = next((w for w in wins if w.get('state') != 'minimized' and w.get('width') and w.get('height')), None)
                    if base:
                        width = width or base.get('width')
                        height = height or base.get('height')
                except Exception:
                    pass

            width = int(width or 1280)
            height = int(height or 800)
            left, top = self._find_open_window_slot(width, height)
            kwargs.setdefault('width', width)
            kwargs.setdefault('height', height)
            kwargs.setdefault('left', int(left))
            kwargs.setdefault('top', int(top))

        return self._cmd('windows.create', url=url, **kwargs)

    def update_window(self, window_id, **kwargs):
        """Update window properties (state, size, position)."""
        return self._cmd('windows.update', windowId=window_id, **kwargs)

    def close_window(self, window_id):
        """Close a window."""
        return self._cmd('windows.close', windowId=window_id)

    # ════════════════════════════════════════
    # ── JavaScript Execution ──
    # ════════════════════════════════════════

    def eval(self, tab_id, expression, await_promise=False):
        """Execute JavaScript via chrome.scripting (no debugger banner). Pass use_cdp=True for CDP."""
        return self._cmd('eval', tabId=tab_id, expression=expression, awaitPromise=await_promise)

    def eval_cdp(self, tab_id, expression, await_promise=False):
        """Execute JavaScript via CDP debugger (shows banner, but bypasses CSP)."""
        return self._cmd('eval.cdp', tabId=tab_id, expression=expression, awaitPromise=await_promise)

    def eval_safe(self, tab_id, expression, isolated=False):
        """Execute JavaScript via chrome.scripting (no debugger bar)."""
        return self._cmd('eval.safe', tabId=tab_id, expression=expression, isolated=isolated)

    # ════════════════════════════════════════
    # ── Screenshots ──
    # ════════════════════════════════════════

    def screenshot(self, tab_id=None, filepath=None, quality=80, fmt='png'):
        """Capture screenshot. Auto-finds active tab if tab_id is None."""
        if tab_id is None:
            tabs = self.tabs()
            active = [t for t in tabs if t.get('active')]
            if not active:
                raise RuntimeError("No active tab found")
            tab_id = active[0]['id']
        result = self._cmd('screenshot', tabId=tab_id, format=fmt, quality=quality)
        if result and filepath and 'dataUrl' in result:
            header, data = result['dataUrl'].split(',', 1)
            with open(filepath, 'wb') as f:
                f.write(base64.b64decode(data))
            return filepath
        return result

    def screenshot_full(self, tab_id, filepath=None, quality=80, fmt='jpeg'):
        """Full page screenshot. Saves to file if filepath given."""
        result = self._cmd('screenshot.full', tabId=tab_id, format=fmt, quality=quality)
        return self._save_binary(result, filepath)

    def screenshot_element(self, tab_id, selector, filepath=None, quality=90, fmt='png'):
        """Screenshot a specific DOM element."""
        result = self._cmd('screenshot.element', tabId=tab_id, selector=selector, format=fmt, quality=quality)
        return self._save_binary(result, filepath)

    def _save_binary(self, result, filepath):
        if result and filepath and 'data' in result:
            with open(filepath, 'wb') as f:
                f.write(base64.b64decode(result['data']))
            return filepath
        return result

    # ════════════════════════════════════════
    # ── Click / Type (JS injection) ──
    # ════════════════════════════════════════

    def click(self, tab_id, selector=None, x=None, y=None):
        """Click element by CSS selector or coordinates."""
        return self._cmd('click', tabId=tab_id, selector=selector, x=x, y=y)

    def click_text(self, tab_id, text, tag='*', partial=False):
        """Click element containing text."""
        return self._cmd('click.text', tabId=tab_id, text=text, tag=tag, partial=partial)

    def type(self, tab_id, selector_or_text, text=None, clear=False):
        """Type text. If only 2 args, types into active element."""
        if text is None:
            text = selector_or_text
            selector = None
        else:
            selector = selector_or_text
        return self._cmd('type', tabId=tab_id, selector=selector, text=text, clear=clear)

    def scroll(self, tab_id, delta_y=300, delta_x=0, selector=None, behavior=None):
        """Scroll the page or a specific element."""
        return self._cmd('scroll', tabId=tab_id, deltaY=delta_y, deltaX=delta_x,
                        selector=selector, behavior=behavior)

    def scroll_to(self, tab_id, selector=None, x=None, y=None, behavior='smooth'):
        """Scroll to element or coordinates."""
        return self._cmd('scroll.to', tabId=tab_id, selector=selector, x=x, y=y, behavior=behavior)

    def select(self, tab_id, selector):
        """Get element info by CSS selector."""
        return self._cmd('select', tabId=tab_id, selector=selector)

    def select_all(self, tab_id, selector, limit=100):
        """Get all matching elements."""
        return self._cmd('selectAll', tabId=tab_id, selector=selector, limit=limit)

    # ════════════════════════════════════════
    # ── CDP Passthrough ──
    # ════════════════════════════════════════

    def cdp(self, tab_id, method, params=None):
        """Send raw CDP command."""
        return self._cmd('cdp', tabId=tab_id, method=method, params=params or {})

    def add_binding(self, tab_id, name):
        """Add a CDP runtime binding exposed as window[name]."""
        return self._cmd('binding.add', tabId=tab_id, name=name)

    # ════════════════════════════════════════
    # ── CDP Input (mouse/keyboard) ──
    # ════════════════════════════════════════

    def mouse_click(self, tab_id, x, y, button='left', click_count=1):
        """Click at viewport coordinates via CDP."""
        return self._cmd('input.mouse', tabId=tab_id, x=x, y=y,
                         button=button, clickCount=click_count, type='click')

    def mouse_move(self, tab_id, x, y):
        """Move mouse to viewport coordinates."""
        return self._cmd('input.mouse', tabId=tab_id, x=x, y=y, type='move')

    def key_press(self, tab_id, key, modifiers=0):
        """Press a key (Enter, Tab, Escape, etc)."""
        return self._cmd('input.key', tabId=tab_id, key=key, modifiers=modifiers)

    def key_type(self, tab_id, text):
        """Type text character by character via CDP."""
        return self._cmd('input.key', tabId=tab_id, text=text)

    def insert_text(self, tab_id, text):
        """Insert text instantly via CDP (like paste)."""
        return self._cmd('input.insertText', tabId=tab_id, text=text)

    def touch(self, tab_id, x, y):
        """Touch tap at coordinates."""
        return self._cmd('input.touch', tabId=tab_id, x=x, y=y)

    # ════════════════════════════════════════
    # ── XPath & Shadow DOM (via content script) ──
    # ════════════════════════════════════════

    def xpath(self, tab_id, expression, context=None):
        """Query element by XPath expression."""
        return self._cmd('xpath', tabId=tab_id, expression=expression, contextNode=context)

    def xpath_all(self, tab_id, expression, limit=50):
        """Query all elements matching XPath."""
        return self._cmd('xpath.all', tabId=tab_id, expression=expression, limit=limit)

    def shadow(self, tab_id, selector, host_selector=None):
        """Query inside shadow DOM."""
        return self._cmd('shadow', tabId=tab_id, selector=selector, hostSelector=host_selector)

    # ════════════════════════════════════════
    # ── Element Highlighting ──
    # ════════════════════════════════════════

    def highlight(self, tab_id, selector, color=None, duration=3000):
        """Highlight an element on the page."""
        return self._cmd('highlight', tabId=tab_id, selector=selector, color=color, duration=duration)

    def highlight_clear(self, tab_id):
        """Clear element highlight."""
        return self._cmd('highlight.clear', tabId=tab_id)

    # ════════════════════════════════════════
    # ── DOM Mutation Observer ──
    # ════════════════════════════════════════

    def watch_mutations(self, tab_id, selector=None, **config):
        """Start watching DOM mutations."""
        return self._cmd('mutation.start', tabId=tab_id, selector=selector, config=config)

    def stop_mutations(self, tab_id):
        """Stop mutation observer."""
        return self._cmd('mutation.stop', tabId=tab_id)

    def get_mutations(self, tab_id):
        """Get buffered mutations."""
        return self._cmd('mutation.flush', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Smart Data Extraction ──
    # ════════════════════════════════════════

    def elements_at(self, tab_id, x, y):
        """Get elements at viewport coordinates."""
        return self._cmd('elements.at', tabId=tab_id, x=x, y=y)

    def element_bounds(self, tab_id, selector):
        """Get element bounding rect and visibility."""
        return self._cmd('element.bounds', tabId=tab_id, selector=selector)

    def computed_style(self, tab_id, selector, properties=None):
        """Get computed CSS styles of an element."""
        return self._cmd('element.computed', tabId=tab_id, selector=selector, properties=properties)

    def extract_links(self, tab_id, filter=None):
        """Extract all links from the page."""
        return self._cmd('links.extract', tabId=tab_id, filter=filter)

    def extract_tables(self, tab_id, selector=None):
        """Extract HTML tables as structured data."""
        return self._cmd('tables.extract', tabId=tab_id, selector=selector)

    def extract_meta(self, tab_id):
        """Extract page metadata (title, OG tags, structured data, etc)."""
        return self._cmd('meta.extract', tabId=tab_id)

    def search_text(self, tab_id, query, highlight=False):
        """Search for text on the page."""
        return self._cmd('search.text', tabId=tab_id, query=query, highlight=highlight)

    def extract(self, tab_id, selectors):
        """Extract structured data using CSS selectors dict."""
        return self._cmd('dom.extract', tabId=tab_id, selectors=selectors)

    # ════════════════════════════════════════
    # ── Forms ──
    # ════════════════════════════════════════

    def forms(self, tab_id):
        """Detect all forms on the page with their fields."""
        return self._cmd('forms.detect', tabId=tab_id)

    def fill_form(self, tab_id, data, form_index=0):
        """Smart fill form fields by matching names/labels."""
        return self._cmd('forms.fill', tabId=tab_id, data=data, formIndex=form_index)

    # ════════════════════════════════════════
    # ── Network ──
    # ════════════════════════════════════════

    def network_requests(self, tab_id, filter=None):
        """Get recent network requests."""
        return self._cmd('network.getRequests', tabId=tab_id, filter=filter)

    def block_urls(self, tab_id, urls):
        """Block specific URLs from loading."""
        return self._cmd('network.block', tabId=tab_id, urls=urls)

    def emulate_network(self, tab_id, preset='fast3g'):
        """Emulate network conditions (slow3g, fast3g, offline, none)."""
        return self._cmd('network.emulateConditions', tabId=tab_id, preset=preset)

    def enable_network(self, tab_id):
        """Enable low-level Network domain events for the tab."""
        return self._cmd('network.enable', tabId=tab_id)

    def intercept_network(self, tab_id, patterns=None, intercept_stage='HeadersReceived'):
        """Enable Network interception with URL patterns."""
        return self._cmd(
            'network.intercept',
            tabId=tab_id,
            patterns=patterns,
            interceptStage=intercept_stage,
        )

    # ════════════════════════════════════════
    # ── Console ──
    # ════════════════════════════════════════

    def console_logs(self, tab_id, level=None):
        """Get console logs from the page."""
        return self._cmd('console.getLogs', tabId=tab_id, level=level)

    def enable_console(self, tab_id):
        """Enable Console domain events for the tab."""
        return self._cmd('console.enable', tabId=tab_id)

    def console_clear(self, tab_id):
        """Clear captured console logs."""
        return self._cmd('console.clear', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Storage ──
    # ════════════════════════════════════════

    def local_storage(self, tab_id, key=None):
        """Get localStorage value(s)."""
        return self._cmd('storage.local.get', tabId=tab_id, key=key)

    def set_local_storage(self, tab_id, key, value):
        """Set localStorage value."""
        return self._cmd('storage.local.set', tabId=tab_id, key=key, value=value)

    def session_storage(self, tab_id, key=None):
        """Get sessionStorage value(s)."""
        return self._cmd('storage.session.get', tabId=tab_id, key=key)

    def set_session_storage(self, tab_id, key, value):
        """Set sessionStorage value."""
        return self._cmd('storage.session.set', tabId=tab_id, key=key, value=value)

    def clear_local_storage(self, tab_id):
        """Clear all localStorage."""
        return self._cmd('storage.local.clear', tabId=tab_id)

    def remove_local_storage(self, tab_id, key):
        """Remove a localStorage key."""
        return self._cmd('storage.local.remove', tabId=tab_id, key=key)

    # ════════════════════════════════════════
    # ── CSS Injection ──
    # ════════════════════════════════════════

    def inject_css(self, tab_id, css):
        """Inject CSS into the page."""
        return self._cmd('css.inject', tabId=tab_id, css=css)

    def remove_css(self, tab_id, css):
        """Remove injected CSS."""
        return self._cmd('css.remove', tabId=tab_id, css=css)

    # ════════════════════════════════════════
    # ── PDF ──
    # ════════════════════════════════════════

    def pdf(self, tab_id, filepath=None, **kwargs):
        """Generate PDF from page."""
        result = self._cmd('page.pdf', tabId=tab_id, **kwargs)
        return self._save_binary(result, filepath)

    # ════════════════════════════════════════
    # ── Device Emulation ──
    # ════════════════════════════════════════

    def emulate(self, tab_id, device='iphone14', **kwargs):
        """Emulate device (iphone14, ipad, pixel7, desktop1080, desktop4k)."""
        return self._cmd('emulate.device', tabId=tab_id, device=device, **kwargs)

    def emulate_clear(self, tab_id):
        """Clear device emulation."""
        return self._cmd('emulate.clear', tabId=tab_id)

    def set_geolocation(self, tab_id, latitude, longitude, accuracy=100):
        """Set geolocation override."""
        return self._cmd('emulate.geolocation', tabId=tab_id,
                        latitude=latitude, longitude=longitude, accuracy=accuracy)

    def set_timezone(self, tab_id, timezone_id):
        """Set timezone override."""
        return self._cmd('emulate.timezone', tabId=tab_id, timezoneId=timezone_id)

    def emulate_media(self, tab_id, media='', features=None):
        """Emulate CSS media (e.g., 'print', prefers-color-scheme)."""
        return self._cmd('emulate.media', tabId=tab_id, media=media, features=features or [])

    # ════════════════════════════════════════
    # ── Performance ──
    # ════════════════════════════════════════

    def performance_metrics(self, tab_id):
        """Get Chrome performance metrics."""
        return self._cmd('performance.metrics', tabId=tab_id)

    def timing(self, tab_id):
        """Get page load timing breakdown."""
        return self._cmd('performance.timing', tabId=tab_id)

    def web_vitals(self, tab_id):
        """Get Core Web Vitals (LCP, FCP, CLS, TBT)."""
        return self._cmd('performance.webVitals', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Accessibility ──
    # ════════════════════════════════════════

    def a11y_snapshot(self, tab_id, depth=4, limit=500):
        """Get accessibility tree via CDP."""
        return self._cmd('a11y.snapshot', tabId=tab_id, depth=depth, limit=limit)

    def a11y_query(self, tab_id, root=None):
        """Get lightweight accessibility snapshot via content script."""
        return self._cmd('a11y.query', tabId=tab_id, root=root)

    # ════════════════════════════════════════
    # ── Clipboard ──
    # ════════════════════════════════════════

    def clipboard_write(self, tab_id, text):
        """Write text to clipboard."""
        return self._cmd('clipboard.write', tabId=tab_id, text=text)

    def clipboard_read(self, tab_id):
        """Read text from clipboard."""
        return self._cmd('clipboard.read', tabId=tab_id)

    # ════════════════════════════════════════
    # ── File Upload ──
    # ════════════════════════════════════════

    def upload(self, tab_id, selector, files):
        """Set files on a file input element."""
        if isinstance(files, str): files = [files]
        return self._cmd('file.upload', tabId=tab_id, selector=selector, files=files)

    # ════════════════════════════════════════
    # ── Cookies ──
    # ════════════════════════════════════════

    def cookies(self, url=None, domain=None, name=None):
        """Get cookies."""
        return self._cmd('cookies.get', url=url, domain=domain, name=name)

    def set_cookie(self, **kwargs):
        """Set a cookie."""
        return self._cmd('cookies.set', **kwargs)

    def delete_cookie(self, url, name):
        """Delete a specific cookie."""
        return self._cmd('cookies.remove', url=url, name=name)

    def clear_cookies(self, url):
        """Clear all cookies for a URL."""
        return self._cmd('cookies.clearAll', url=url)

    # ════════════════════════════════════════
    # ── Page Info ──
    # ════════════════════════════════════════

    def page_info(self, tab_id):
        """Get page info (title, URL, dimensions, scroll, element count)."""
        return self._cmd('page.info', tabId=tab_id)

    def html(self, tab_id, selector=None):
        """Get HTML of page or specific element."""
        return self._cmd('page.html', tabId=tab_id, selector=selector)

    def text(self, tab_id, selector=None):
        """Get visible text content."""
        return self._cmd('page.text', tabId=tab_id, selector=selector)

    # ════════════════════════════════════════
    # ── History & Bookmarks ──
    # ════════════════════════════════════════

    def history_search(self, text='', max_results=50):
        """Search browser history."""
        return self._cmd('history.search', text=text, maxResults=max_results)

    def delete_history(self, url):
        """Delete a specific URL from browser history."""
        return self._cmd('history.delete', url=url)

    def bookmark(self, title, url, parent_id=None):
        """Create a bookmark."""
        return self._cmd('bookmarks.create', title=title, url=url, parentId=parent_id)

    def search_bookmarks(self, query):
        """Search bookmarks."""
        return self._cmd('bookmarks.search', query=query)

    # ════════════════════════════════════════
    # ── Notifications ──
    # ════════════════════════════════════════

    def notify(self, message, title='Chrome Bridge'):
        """Show a browser notification."""
        return self._cmd('notify', message=message, title=title)

    # ════════════════════════════════════════
    # ── Downloads ──
    # ════════════════════════════════════════

    def downloads(self, **query):
        """List recent downloads."""
        return self._cmd('downloads.list', query=query)

    def download(self, url, filename=None):
        """Start a download."""
        return self._cmd('downloads.start', url=url, filename=filename)

    # ════════════════════════════════════════
    # ── Wait ──
    # ════════════════════════════════════════

    def wait_for(self, tab_id, selector=None, text=None, timeout=10000):
        """Wait for element or text to appear."""
        if selector:
            return self._cmd('wait.element', tabId=tab_id, selector=selector, timeout=timeout)
        if text:
            return self._cmd('wait.text', tabId=tab_id, text=text, timeout=timeout)

    def wait_navigation(self, tab_id, timeout=15000):
        """Wait for page navigation to complete."""
        return self._cmd('wait.navigation', tabId=tab_id, timeout=timeout)

    def wait_idle(self, tab_id, timeout=30000, idle_time=2000):
        """Wait for network idle (no requests for idle_time ms)."""
        return self._cmd('wait.idle', tabId=tab_id, timeout=timeout, idleTime=idle_time)

    # ════════════════════════════════════════
    # ── Infinite Scroll ──
    # ════════════════════════════════════════

    def infinite_scroll(self, tab_id, max_scrolls=10, delay=1500):
        """Scroll to load lazy content (infinite scroll pages)."""
        return self._cmd('scroll.infinite', tabId=tab_id, maxScrolls=max_scrolls, delay=delay)

    # ════════════════════════════════════════
    # ── Smart Element Finding ──
    # ════════════════════════════════════════

    def smart_find(self, tab_id, description, action=None):
        """Find element by description (label, placeholder, aria, text, role).
        Like Playwright's getByRole/getByText but more flexible."""
        return self._cmd('smart.find', tabId=tab_id, description=description, action=action)

    def smart_find_all(self, tab_id, description, limit=20):
        """Find all elements matching a description."""
        return self._cmd('smart.findAll', tabId=tab_id, description=description, limit=limit)

    def smart_click(self, tab_id, description):
        """Click element by description (e.g., 'Submit', 'Login', 'Search')."""
        return self._cmd('smart.click', tabId=tab_id, description=description)

    def smart_fill(self, tab_id, description, value):
        """Fill input by description (e.g., 'Email', 'Password', 'Search')."""
        return self._cmd('smart.fill', tabId=tab_id, description=description, value=value)

    def smart_wait(self, tab_id, description, timeout=10000):
        """Wait for element matching description to appear."""
        return self._cmd('smart.wait', tabId=tab_id, description=description, timeout=timeout)

    def wait_interactive(self, tab_id, selector, timeout=10000):
        """Wait for element to be visible and interactive (not just present)."""
        return self._cmd('element.interactive', tabId=tab_id, selector=selector, timeout=timeout)

    # ════════════════════════════════════════
    # ── Element Traversal & Advanced Wait ──
    # ════════════════════════════════════════

    def element_wait_gone(self, tab_id, selector, timeout=10000):
        """Wait for element to disappear from DOM."""
        return self._cmd('element.waitGone', tabId=tab_id, selector=selector, timeout=timeout)

    def dom_wait_stable(self, tab_id, selector='body', idle_ms=500, timeout=10000):
        """Wait for DOM subtree to stop changing."""
        return self._cmd('dom.waitStable', tabId=tab_id, selector=selector, idleMs=idle_ms, timeout=timeout)

    def element_attributes(self, tab_id, selector):
        """Get all attributes of an element."""
        return self._cmd('element.attributes', tabId=tab_id, selector=selector)

    def element_set_attribute(self, tab_id, selector, name, value):
        """Set an attribute on an element."""
        return self._cmd('element.setAttribute', tabId=tab_id, selector=selector, name=name, value=value)

    def element_xpath(self, tab_id, selector):
        """Get absolute XPath for an element."""
        return self._cmd('element.xpath', tabId=tab_id, selector=selector)

    def element_dispatch_event(self, tab_id, selector, event_type, detail=None):
        """Dispatch a custom event on an element."""
        return self._cmd('element.dispatchEvent', tabId=tab_id, selector=selector, eventType=event_type, detail=detail)

    def element_parent(self, tab_id, selector, levels=1):
        """Get parent element info."""
        return self._cmd('element.parent', tabId=tab_id, selector=selector, levels=levels)

    def element_children(self, tab_id, selector, filter_tag=None):
        """Get children of an element."""
        return self._cmd('element.children', tabId=tab_id, selector=selector, filter=filter_tag)

    def element_siblings(self, tab_id, selector):
        """Get sibling elements."""
        return self._cmd('element.siblings', tabId=tab_id, selector=selector)

    def scroll_position(self, tab_id, selector=None):
        """Get scroll position of page or element."""
        return self._cmd('scroll.position', tabId=tab_id, selector=selector)

    # ════════════════════════════════════════
    # ── Intersection Observer ──
    # ════════════════════════════════════════

    def intersection_observe(self, tab_id, selector, threshold=0.5):
        """Start observing element intersection with viewport."""
        return self._cmd('intersection.observe', tabId=tab_id, selector=selector, threshold=threshold)

    def intersection_check(self, tab_id, selector):
        """Check last recorded intersection for element."""
        return self._cmd('intersection.check', tabId=tab_id, selector=selector)

    def intersection_stop(self, tab_id, selector=None):
        """Stop observing intersections (specific selector or all)."""
        return self._cmd('intersection.stop', tabId=tab_id, selector=selector)

    # ════════════════════════════════════════
    # ── Canvas & Visual ──
    # ════════════════════════════════════════

    def canvas_read_pixels(self, tab_id, selector, x=0, y=0, width=1, height=1):
        """Read pixels from a canvas element."""
        return self._cmd('canvas.readPixels', tabId=tab_id, selector=selector, x=x, y=y, width=width, height=height)

    def element_highlight_multiple(self, tab_id, selectors, duration=2000):
        """Highlight multiple elements simultaneously."""
        return self._cmd('element.highlight.multiple', tabId=tab_id, selectors=selectors, duration=duration)

    # ════════════════════════════════════════
    # ── Network XHR Observation ──
    # ════════════════════════════════════════

    def network_observe_xhr(self, tab_id, filter_url=None):
        """Start intercepting fetch/XHR requests in page context."""
        return self._cmd('network.observeXHR', tabId=tab_id, filterUrl=filter_url)

    def network_flush_xhr(self, tab_id):
        """Get captured XHR/fetch logs and clear buffer."""
        return self._cmd('network.flushXHR', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Smart Select ──
    # ════════════════════════════════════════

    def smart_select(self, tab_id, selector, value=None, text=None, index=None):
        """Select dropdown option by value, text, or index."""
        return self._cmd('smart.select', tabId=tab_id, selector=selector, value=value, text=text, index=index)

    # ════════════════════════════════════════
    # ── Advanced Navigation & Wait ──
    # ════════════════════════════════════════

    def wait_for_function(self, tab_id, expression, timeout=10000, poll_ms=200):
        """Wait for a JS expression to return truthy."""
        return self._cmd('wait.function', tabId=tab_id, expression=expression, timeout=timeout, pollMs=poll_ms)

    def wait_for_url(self, tab_id, pattern, timeout=15000, poll_ms=300):
        """Wait for tab URL to match a pattern (substring or regex)."""
        return self._cmd('wait.url', tabId=tab_id, pattern=pattern, timeout=timeout, pollMs=poll_ms)

    def tabs_create_and_wait(self, url, timeout=30000):
        """Create tab and wait for it to fully load."""
        return self._cmd('tabs.createAndWait', url=url, timeout=timeout)

    def tabs_navigate_post(self, tab_id, url, post_data):
        """Navigate with POST data via CDP."""
        return self._cmd('tabs.navigatePost', tabId=tab_id, url=url, postData=post_data)

    # ════════════════════════════════════════
    # ── DOM Snapshot & Diff ──
    # ════════════════════════════════════════

    def dom_snapshot(self, tab_id, snapshot_id='default'):
        """Capture a DOM snapshot for later diffing."""
        return self._cmd('dom.snapshot', tabId=tab_id, snapshotId=snapshot_id)

    def dom_diff(self, tab_id, snapshot_id='default'):
        """Diff current DOM against a previous snapshot."""
        return self._cmd('dom.diff', tabId=tab_id, snapshotId=snapshot_id)

    def cdp_snapshot(self, tab_id, full=False, **kwargs):
        """Capture full CDP DOM snapshot with layout info."""
        return self._cmd('dom.cdpSnapshot', tabId=tab_id, full=full, **kwargs)

    # ════════════════════════════════════════
    # ── Drag and Drop ──
    # ════════════════════════════════════════

    def drag(self, tab_id, from_selector, to_selector):
        """Drag element to another element (HTML5 drag-and-drop)."""
        return self._cmd('drag', tabId=tab_id, **{'from': from_selector, 'to': to_selector})

    def drag_coords(self, tab_id, from_x, from_y, to_x, to_y, steps=10):
        """Drag from coordinates to coordinates via CDP."""
        return self._cmd('input.dragCDP', tabId=tab_id,
                        fromX=from_x, fromY=from_y, toX=to_x, toY=to_y, steps=steps)

    # ════════════════════════════════════════
    # ── Session Recording ──
    # ════════════════════════════════════════

    def record_start(self, tab_id):
        """Start recording user interactions."""
        return self._cmd('record.start', tabId=tab_id)

    def record_stop(self, tab_id):
        """Stop recording and get events."""
        return self._cmd('record.stop', tabId=tab_id)

    def record_replay(self, tab_id, events, speed=1):
        """Replay recorded events."""
        return self._cmd('record.replay', tabId=tab_id, events=events, speed=speed)

    def record_save(self, tab_id, filepath):
        """Record, stop, and save to file."""
        result = self.record_stop(tab_id)
        if result and 'events' in result:
            with open(filepath, 'w') as f:
                json.dump(result['events'], f, indent=2)
            return {'saved': filepath, 'events': len(result['events'])}
        return result

    def record_load_and_replay(self, tab_id, filepath, speed=1):
        """Load events from file and replay."""
        with open(filepath) as f:
            events = json.load(f)
        return self.record_replay(tab_id, events, speed)

    # ════════════════════════════════════════
    # ── Response Interception (Fetch API) ──
    # ════════════════════════════════════════

    def fetch_enable(self, tab_id, patterns=None, handle_auth=False):
        """Enable request/response interception via CDP Fetch."""
        return self._cmd('fetch.enable', tabId=tab_id, patterns=patterns, handleAuth=handle_auth)

    def fetch_disable(self, tab_id):
        """Disable request interception."""
        return self._cmd('fetch.disable', tabId=tab_id)

    def fetch_body(self, tab_id, request_id):
        """Get intercepted response body."""
        return self._cmd('fetch.getBody', tabId=tab_id, requestId=request_id)

    def fetch_fulfill(self, tab_id, request_id, body=None, response_code=200, headers=None):
        """Fulfill an intercepted request with custom response."""
        return self._cmd('fetch.fulfill', tabId=tab_id, requestId=request_id,
                        body=body, responseCode=response_code, responseHeaders=headers or [])

    def fetch_continue(self, tab_id, request_id, url=None, method=None, headers=None):
        """Continue an intercepted request (optionally modified)."""
        return self._cmd('fetch.continue', tabId=tab_id, requestId=request_id,
                        url=url, method=method, headers=headers)

    def fetch_fail(self, tab_id, request_id, reason='Failed'):
        """Fail an intercepted request."""
        return self._cmd('fetch.fail', tabId=tab_id, requestId=request_id, reason=reason)

    # ════════════════════════════════════════
    # ── HAR Recording ──
    # ════════════════════════════════════════

    def har_start(self, tab_id):
        """Start recording network activity as HAR."""
        return self._cmd('har.start', tabId=tab_id)

    def har_stop(self, tab_id, filepath=None):
        """Stop HAR recording and get entries."""
        result = self._cmd('har.stop', tabId=tab_id)
        if result and filepath and 'entries' in result:
            with open(filepath, 'w') as f:
                json.dump(result, f, indent=2)
            return {'saved': filepath, 'entries': result.get('entryCount', 0)}
        return result

    # ════════════════════════════════════════
    # ── JS/CSS Coverage ──
    # ════════════════════════════════════════

    def coverage_start_js(self, tab_id, detailed=False):
        """Start collecting JS code coverage."""
        return self._cmd('coverage.startJS', tabId=tab_id, detailed=detailed)

    def coverage_stop_js(self, tab_id):
        """Stop and get JS coverage report."""
        return self._cmd('coverage.stopJS', tabId=tab_id)

    def coverage_start_css(self, tab_id):
        """Start collecting CSS rule usage."""
        return self._cmd('coverage.startCSS', tabId=tab_id)

    def coverage_stop_css(self, tab_id, limit=100):
        """Stop and get CSS coverage report."""
        return self._cmd('coverage.stopCSS', tabId=tab_id, limit=limit)

    def coverage_report(self, tab_id):
        """Run full coverage analysis (JS + CSS) and return summary."""
        self.coverage_start_js(tab_id)
        self.coverage_start_css(tab_id)
        time.sleep(2)  # Let page run
        js = self.coverage_stop_js(tab_id)
        css = self.coverage_stop_css(tab_id)
        return {'js': js, 'css': css}

    # ════════════════════════════════════════
    # ── Stealth Mode ──
    # ════════════════════════════════════════

    def stealth_enable(self, tab_id):
        """Apply anti-detection measures (hide webdriver, fix fingerprint)."""
        return self._cmd('stealth.enable', tabId=tab_id)

    def stealth_check(self, tab_id):
        """Check current detection status."""
        return self._cmd('stealth.check', tabId=tab_id)

    # ════════════════════════════════════════
    # ── CDP Input ──
    # ════════════════════════════════════════

    def touch(self, tab_id, x, y):
        """Touch tap at coordinates."""
        return self._cmd('input.touch', tabId=tab_id, x=x, y=y)

    def key_type(self, tab_id, text):
        """Type text character by character via CDP."""
        return self._cmd('input.key', tabId=tab_id, text=text)

    def key_press(self, tab_id, key, modifiers=0):
        """Press key via CDP (Enter, Tab, Escape, etc.)."""
        return self._cmd('input.key', tabId=tab_id, key=key, modifiers=modifiers)

    def mouse_click(self, tab_id, x, y, button='left'):
        """Click at coordinates via CDP."""
        return self._cmd('input.mouse', tabId=tab_id, x=x, y=y, button=button, type='click')

    # ════════════════════════════════════════
    # ── Page Readiness & iframes ──
    # ════════════════════════════════════════

    def page_readiness(self, tab_id):
        """Check page readiness (DOM, images, fonts, scripts, styles)."""
        return self._cmd('page.readiness', tabId=tab_id)

    def iframes(self, tab_id):
        """List all iframes on the page."""
        return self._cmd('iframe.list', tabId=tab_id)

    def iframe_eval(self, tab_id, index, expression):
        """Evaluate JS in a specific iframe by index."""
        return self._cmd('iframe.eval', tabId=tab_id, index=index, expression=expression)

    # ════════════════════════════════════════
    # ── Memory & Security ──
    # ════════════════════════════════════════

    def memory_info(self, tab_id):
        """Get JS heap memory usage."""
        return self._cmd('memory.info', tabId=tab_id)

    def security_info(self, tab_id):
        """Get page security/SSL info."""
        return self._cmd('security.info', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Pre-Page Stealth Injection ──
    # ════════════════════════════════════════

    def stealth_inject(self, tab_id, script=None):
        """Inject script BEFORE any page JS runs. Nuclear stealth option.
        Script persists across navigations until removed."""
        return self._cmd('stealth.inject', tabId=tab_id, script=script)

    def stealth_remove_script(self, tab_id, script_id):
        """Remove a pre-page injected script."""
        return self._cmd('stealth.removeScript', tabId=tab_id, scriptId=script_id)

    def rotate_ua(self, tab_id, user_agent=None):
        """Rotate to a random user agent (or specify one)."""
        return self._cmd('stealth.rotateUA', tabId=tab_id, userAgent=user_agent)

    # ════════════════════════════════════════
    # ── WebSocket Interception ──
    # ════════════════════════════════════════

    def ws_enable(self, tab_id):
        """Enable WebSocket frame capture."""
        return self._cmd('ws.enable', tabId=tab_id)

    def ws_frames(self, tab_id, limit=100):
        """Get captured WebSocket frames."""
        return self._cmd('ws.getFrames', tabId=tab_id, limit=limit)

    def ws_clear(self, tab_id):
        """Clear captured WebSocket frames."""
        return self._cmd('ws.clearFrames', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Network Power Commands ──
    # ════════════════════════════════════════

    def response_body(self, tab_id, request_id):
        """Get response body for a network request."""
        return self._cmd('network.getResponseBody', tabId=tab_id, requestId=request_id)

    def post_data(self, tab_id, request_id):
        """Get POST data for a network request."""
        return self._cmd('network.getPostData', tabId=tab_id, requestId=request_id)

    def set_headers(self, tab_id, headers):
        """Set extra HTTP headers for all requests."""
        return self._cmd('network.setHeaders', tabId=tab_id, headers=headers)

    def set_user_agent(self, tab_id, user_agent, platform=None):
        """Override user agent via CDP."""
        return self._cmd('network.setUserAgent', tabId=tab_id, userAgent=user_agent, platform=platform)

    def disable_cache(self, tab_id, disabled=True):
        """Disable/enable browser cache for this tab."""
        return self._cmd('network.setCacheDisabled', tabId=tab_id, disabled=disabled)

    def bypass_service_worker(self, tab_id, bypass=True):
        """Bypass service worker for network requests."""
        return self._cmd('network.bypassServiceWorker', tabId=tab_id, bypass=bypass)

    # ════════════════════════════════════════
    # ── Navigation History ──
    # ════════════════════════════════════════

    def nav_history(self, tab_id):
        """Get full navigation history entries."""
        return self._cmd('navigation.history', tabId=tab_id)

    def nav_back(self, tab_id):
        """Navigate back in history."""
        return self._cmd('navigation.back', tabId=tab_id)

    def nav_forward(self, tab_id):
        """Navigate forward in history."""
        return self._cmd('navigation.forward', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Frame Tree ──
    # ════════════════════════════════════════

    def frame_tree(self, tab_id):
        """Get complete frame tree (all iframes) via CDP."""
        return self._cmd('frames.tree', tabId=tab_id)

    def frame_eval(self, tab_id, expression, context_id=None, await_promise=False):
        """Execute JS in a specific frame context."""
        return self._cmd('frames.eval', tabId=tab_id, expression=expression,
                        contextId=context_id, awaitPromise=await_promise)

    # ════════════════════════════════════════
    # ── Layout Metrics ──
    # ════════════════════════════════════════

    def layout_metrics(self, tab_id):
        """Get viewport, visual viewport, and content size."""
        return self._cmd('page.layoutMetrics', tabId=tab_id)

    # ════════════════════════════════════════
    # ── Cookie Profiles ──
    # ════════════════════════════════════════

    def export_cookies(self, filepath=None, **filter_kwargs):
        """Export all cookies (optionally filtered). Save to file if filepath given."""
        result = self._cmd('cookies.export', filter=filter_kwargs if filter_kwargs else None)
        if result and filepath and 'cookies' in result:
            with open(filepath, 'w') as f:
                json.dump(result['cookies'], f, indent=2)
            return {'saved': filepath, 'count': result['count']}
        return result

    def import_cookies(self, cookies=None, filepath=None):
        """Import cookies from list or JSON file."""
        if filepath and not cookies:
            with open(filepath) as f:
                cookies = json.load(f)
        return self._cmd('cookies.import', cookies=cookies or [])

    # ════════════════════════════════════════
    # ── Multi-Tab Orchestration ──
    # ════════════════════════════════════════

    def multi_eval(self, expression, tab_ids=None):
        """Execute same JS on multiple tabs simultaneously."""
        return self._cmd('multi.eval', expression=expression, tabIds=tab_ids)

    def multi_screenshot(self, tab_ids=None, filepath_prefix=None, **kwargs):
        """Screenshot multiple tabs. Save to files if prefix given."""
        result = self._cmd('multi.screenshot', tabIds=tab_ids, **kwargs)
        if filepath_prefix and isinstance(result, dict):
            saved = {}
            for tid, data in result.items():
                if 'dataUrl' in data:
                    path = f"{filepath_prefix}_{tid}.jpg"
                    header, b64 = data['dataUrl'].split(',', 1)
                    with open(path, 'wb') as f:
                        f.write(base64.b64decode(b64))
                    saved[tid] = path
            return saved
        return result

    def multi_navigate(self, tasks):
        """Navigate multiple tabs to different URLs. tasks=[{tabId, url}, ...]"""
        return self._cmd('multi.navigate', tasks=tasks)

    def multi_close(self, url_pattern=None, title_pattern=None):
        """Close all tabs matching URL or title pattern."""
        return self._cmd('multi.close', urlPattern=url_pattern, titlePattern=title_pattern)

    # ════════════════════════════════════════
    # ── Workflow Engine ──
    # ════════════════════════════════════════

    def workflow(self, steps, vars=None, stop_on_error=False):
        """Execute multi-step workflow defined as JSON.

        Each step: {command, params, as?, delay?, condition?, onError?}
        Variables: use {{varName}} in param values to reference earlier results.

        Example:
            chrome.workflow([
                {'command': 'tabs.create', 'params': {'url': 'https://example.com'}, 'as': 'tab'},
                {'command': 'wait.element', 'params': {'tabId': '{{tab.id}}', 'selector': 'h1'}, 'delay': 1000},
                {'command': 'page.text', 'params': {'tabId': '{{tab.id}}'}},
            ])
        """
        return self._cmd('workflow.run', steps=steps, vars=vars or {}, stopOnError=stop_on_error)

    # ════════════════════════════════════════
    # ── Auto-Healing ──
    # ════════════════════════════════════════

    def heal_click(self, tab_id, selector=None, text=None, xpath=None):
        """Click element trying multiple strategies (CSS → text → aria → XPath).
        Auto-heals if the primary selector breaks."""
        return self._cmd('heal.click', tabId=tab_id, selector=selector, text=text, xpath=xpath)

    # ════════════════════════════════════════
    # ── Visual Regression ──
    # ════════════════════════════════════════

    def visual_capture(self, tab_id=None, name=None):
        """Capture a visual baseline for later comparison."""
        return self._cmd('visual.capture', tabId=tab_id, name=name)

    def visual_compare(self, tab_id=None, name=None):
        """Compare current screenshot against captured baseline."""
        return self._cmd('visual.compare', tabId=tab_id, name=name)

    # ════════════════════════════════════════
    # ── Page Lifecycle ──
    # ════════════════════════════════════════

    def stop_loading(self, tab_id):
        """Stop page loading."""
        return self._cmd('page.stopLoading', tabId=tab_id)

    def handle_dialog(self, tab_id, accept=True, prompt_text=None):
        """Accept or dismiss JavaScript dialog (alert/confirm/prompt)."""
        return self._cmd('page.handleDialog', tabId=tab_id, accept=accept, promptText=prompt_text)

    def set_content(self, tab_id, html):
        """Replace page content with custom HTML."""
        return self._cmd('page.setContent', tabId=tab_id, html=html)

    # ════════════════════════════════════════
    # ── Error-Safe Execution ──
    # ════════════════════════════════════════

    def try_command(self, tab_id, command, **params):
        """Execute command with auto-screenshot on failure."""
        return self._cmd('try', tabId=tab_id, command=command, commandParams={'tabId': tab_id, **params})

    # ════════════════════════════════════════
    # ── Hub Diagnostics ──
    # ════════════════════════════════════════

    def hub_health(self):
        """Get hub health check (agents, latency, uptime)."""
        return self._hub._send('bridge.hub.health')

    def hub_log(self, limit=50):
        """Get recent command log from hub."""
        return self._hub._send('bridge.hub.log', {'limit': limit})

    # ════════════════════════════════════════
    # ── Debugger ──
    # ════════════════════════════════════════

    def attach_debugger(self, tab_id):
        """Explicitly attach debugger."""
        return self._cmd('debugger.attach', tabId=tab_id)

    def detach_debugger(self, tab_id):
        """Detach debugger."""
        return self._cmd('debugger.detach', tabId=tab_id)

    def detach_all(self):
        """Detach all debuggers."""
        return self._cmd('debugger.detachAll')

    # ════════════════════════════════════════
    # ── Bridge ──
    # ════════════════════════════════════════

    def ping(self):
        """Ping the extension (latency test)."""
        return self._cmd('bridge.ping')

    def bridge_metrics(self):
        """Get extension metrics."""
        return self._cmd('bridge.metrics')

    def capabilities(self):
        """Get the extension transport and feature capabilities."""
        return self._cmd('bridge.capabilities')

    def status(self):
        """Get extension connection and diagnostic status."""
        return self._cmd('bridge.status')

    def configure_bridge(self, hub_url, apply_now=False):
        """Update the extension's hub URL. apply_now=True reconnects after the response."""
        return self._cmd('bridge.configure', hubUrl=hub_url, applyNow=apply_now)

    def reconnect_bridge(self):
        """Force the extension to reconnect to its configured hub."""
        return self._cmd('bridge.reconnect')

    def reload_bridge(self):
        """Reload the extension (picks up new code from disk). Connection will drop and reconnect."""
        return self._cmd('bridge.reload')

    def detach_all_debuggers(self):
        """Detach all debuggers to dismiss the 'Chrome Bridge started debugging' banner."""
        return self._cmd('bridge.detachAll')

    def clear_cache(self):
        """Clear browser cache."""
        return self._cmd('browsing.clearCache')

    # ════════════════════════════════════════
    # ── Event Streaming (v3.0) ──
    # ════════════════════════════════════════

    def on(self, event_type, handler):
        """Register event handler for real-time Chrome events.

        Events: network.request, network.response, network.failed,
                console.message, navigation.started, navigation.completed,
                navigation.error, tab.created, tab.removed, tab.updated,
                dialog.opened, runtime.exception, download.created,
                download.changed, fetch.paused, * (all)

        handler(event_type: str, data: dict) is called when the event fires.
        """
        with self._hub._event_lock:
            self._hub._event_handlers[event_type].append(handler)
        return self

    def off(self, event_type, handler=None):
        """Remove event handler (or all handlers for event_type if handler is None)."""
        with self._hub._event_lock:
            if handler is None:
                self._hub._event_handlers.pop(event_type, None)
            else:
                handlers = self._hub._event_handlers.get(event_type, [])
                self._hub._event_handlers[event_type] = [h for h in handlers if h != handler]
        return self

    def once(self, event_type, handler):
        """Register one-shot event handler (auto-removes after first call)."""
        def wrapper(et, data):
            self.off(event_type, wrapper)
            handler(et, data)
        return self.on(event_type, wrapper)

    def subscribe(self, *event_types, tab_id=None):
        """Subscribe to real-time Chrome events. Enables push for given types.

        For CDP events (network, console, dialog), pass tab_id to enable per-tab.
        """
        events = list(event_types)
        return self._cmd('events.subscribe', events=events, tabId=tab_id)

    def unsubscribe(self, *event_types):
        """Unsubscribe from Chrome events."""
        return self._cmd('events.unsubscribe', events=list(event_types))

    def subscriptions(self):
        """List active event subscriptions."""
        return self._cmd('events.list')

    # ════════════════════════════════════════
    # ── Command Pipeline (v3.0) ──
    # ════════════════════════════════════════

    def pipeline(self):
        """Create a command pipeline for parallel execution.

        Sends multiple commands without waiting for each response.
        Returns all results at once. Massive speed boost.

        Usage:
            pipe = chrome.pipeline()
            pipe.add('tabs.list')
            pipe.add('page.info', tabId=tab_id)
            pipe.add('screenshot', tabId=tab_id)
            results = pipe.execute()

            # Or as context manager:
            with chrome.pipeline() as pipe:
                pipe.tabs()
                pipe.screenshot(tab_id)
            print(pipe.results)
        """
        return Pipeline(self._hub, self._target)

    # ════════════════════════════════════════
    # ── Batch Operations ──
    # ════════════════════════════════════════

    def batch(self):
        """Create a batch context for parallel command execution."""
        return BatchContext(self._hub, self._target)


class BatchContext:
    """Collects commands and executes them in a single round-trip."""

    def __init__(self, hub, target=None):
        self._hub = hub
        self._target = target
        self._commands = []
        self._next_id = 1
        self.results = []

    def _add(self, command, **params):
        cmd_id = self._next_id
        self._next_id += 1
        self._commands.append({'id': cmd_id, 'command': command, 'params': params})
        return cmd_id

    def __enter__(self):
        self._commands = []
        self.results = []
        return self

    def __exit__(self, *args):
        if self._commands:
            self.results = self._hub._send('batch', {'commands': self._commands}, self._target)

    # Convenience methods that queue commands
    def tabs(self): return self._add('tabs.list')
    def eval(self, tab_id, expr): return self._add('eval', tabId=tab_id, expression=expr)
    def page_info(self, tab_id): return self._add('page.info', tabId=tab_id)
    def screenshot(self, tab_id, **kw): return self._add('screenshot', tabId=tab_id, **kw)
    def click(self, tab_id, selector): return self._add('click', tabId=tab_id, selector=selector)
    def navigate(self, tab_id, url): return self._add('tabs.navigate', tabId=tab_id, url=url)
    def text(self, tab_id): return self._add('page.text', tabId=tab_id)
    def html(self, tab_id): return self._add('page.html', tabId=tab_id)


class Pipeline:
    """Send multiple commands without waiting for each response.
    Returns all results at once — massive speed boost for sequential operations.

    Usage:
        pipe = Pipeline(hub)
        pipe.add('tabs.list')
        pipe.add('page.info', tabId=42)
        pipe.add('screenshot', tabId=42)
        results = pipe.execute()  # one round-trip

        # Chainable:
        results = Pipeline(hub).add('tabs.list').add('screenshot', tabId=42).execute()

        # Context manager:
        with Pipeline(hub) as pipe:
            pipe.tabs()
            pipe.screenshot(42)
        print(pipe.results)
    """

    def __init__(self, hub, target=None):
        self._hub = hub
        self._target = target
        self._commands = []
        self._next_id = 1
        self.results = []

    def add(self, command, **params):
        """Add command to pipeline. Returns self for chaining."""
        cmd_id = self._next_id
        self._next_id += 1
        self._commands.append({'id': cmd_id, 'command': command, 'params': params})
        return self

    def execute(self):
        """Send all commands in one round-trip and return results."""
        if not self._commands:
            return []
        self.results = self._hub._send('batch', {'commands': self._commands}, self._target)
        return self.results

    def __enter__(self):
        self._commands = []
        self.results = []
        return self

    def __exit__(self, *args):
        if self._commands:
            self.execute()

    def __len__(self):
        return len(self._commands)

    # Convenience methods (chainable)
    def tabs(self): return self.add('tabs.list')
    def eval(self, tab_id, expr): return self.add('eval', tabId=tab_id, expression=expr)
    def screenshot(self, tab_id, **kw): return self.add('screenshot', tabId=tab_id, **kw)
    def page_info(self, tab_id): return self.add('page.info', tabId=tab_id)
    def click(self, tab_id, selector): return self.add('click', tabId=tab_id, selector=selector)
    def navigate(self, tab_id, url): return self.add('tabs.navigate', tabId=tab_id, url=url)
    def text(self, tab_id): return self.add('page.text', tabId=tab_id)
    def html(self, tab_id): return self.add('page.html', tabId=tab_id)
    def smart_find(self, tab_id, desc): return self.add('smart.find', tabId=tab_id, description=desc)
    def smart_click(self, tab_id, desc): return self.add('smart.click', tabId=tab_id, description=desc)


class Hub:
    """Connection to the Chrome Bridge hub server with auto-retry."""

    def __init__(self, url=None, auto_connect=True, retry=3, retry_delay=0.5):
        self._url = _resolve_hub_url(url)
        self._ws = None
        self._agents = []
        self._pending = {}
        self._next_id = 0
        self._loop = None
        self._thread = None
        self._connected = threading.Event()
        self._closing = threading.Event()
        self._retry = retry
        self._retry_delay = retry_delay
        self._event_handlers = defaultdict(list)
        self._event_lock = threading.Lock()

        if auto_connect:
            self.connect()

    def connect(self):
        """Connect to the hub server."""
        if self._thread and self._thread.is_alive():
            if self._connected.wait(timeout=5):
                return
            raise ConnectionError(f"Could not connect to hub at {self._url}")

        self._closing.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5):
            raise ConnectionError(f"Could not connect to hub at {self._url}")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self):
        attempt = 0
        while not self._closing.is_set():
            try:
                async with websockets.connect(self._url, max_size=50 * 1024 * 1024) as ws:
                    self._ws = ws
                    self._agents = []
                    await ws.send(json.dumps({"type": "client"}))
                    self._connected.set()
                    attempt = 0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if msg.get("type") == "agents":
                            self._agents = msg["agents"]
                            continue

                        if msg.get("type") == "event":
                            self._dispatch_event(msg)
                            continue

                        msg_id = msg.get("id")
                        if msg_id is not None and msg_id in self._pending:
                            future = self._pending[msg_id]
                            if not future.done():
                                future.set_result(msg)
                            continue
            except Exception:
                if self._closing.is_set():
                    break
            finally:
                self._ws = None
                self._agents = []
                self._connected.clear()
                self._fail_pending(ConnectionError("Hub connection lost"))

            if not self._closing.is_set():
                delay = min(self._retry_delay * (2 ** attempt), 5.0)
                attempt += 1
                await asyncio.sleep(delay)

    def _fail_pending(self, error):
        pending = list(self._pending.items())
        self._pending.clear()
        for _, future in pending:
            if not future.done():
                future.set_exception(error)

    def _dispatch_event(self, msg):
        """Dispatch event to registered handlers."""
        event_type = msg.get("event", "")
        data = msg.get("data", {})
        with self._event_lock:
            handlers = list(self._event_handlers.get(event_type, []))
            handlers += list(self._event_handlers.get("*", []))
        for handler in handlers:
            try:
                handler(event_type, data)
            except Exception:
                pass

    def _send(self, command, params=None, target=None):
        """Send command and wait for response (sync) with auto-retry."""
        last_error = None
        for attempt in range(self._retry):
            try:
                if not self._connected.wait(timeout=5):
                    raise ConnectionError("Not connected to hub")
                if not self._ws:
                    raise ConnectionError("Hub socket is unavailable")

                self._next_id += 1
                msg_id = self._next_id
                msg = {"id": msg_id, "command": command, "params": params or {}}
                if target:
                    msg["target"] = target

                future = self._loop.create_future()
                self._pending[msg_id] = future

                send_future = asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(msg)), self._loop)
                send_future.result(timeout=5)

                result_msg = self._wait_future(future, timeout=30)
                self._pending.pop(msg_id, None)

                if "error" in result_msg:
                    raise RuntimeError(result_msg["error"])
                return result_msg.get("result")

            except (TimeoutError, ConnectionError, OSError) as e:
                last_error = e
                self._pending.pop(msg_id, None) if 'msg_id' in dir() else None
                if attempt < self._retry - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))
                continue
            except RuntimeError:
                raise  # Don't retry command errors

        raise last_error or RuntimeError(f"Command '{command}' failed after {self._retry} attempts")

    def _wait_future(self, future, timeout=30):
        """Wait for an asyncio future from the sync thread."""
        import concurrent.futures
        cf = concurrent.futures.Future()

        def _on_done(f):
            try: cf.set_result(f.result())
            except Exception as e: cf.set_exception(e)

        future.add_done_callback(
            lambda f: self._loop.call_soon_threadsafe(_on_done, f)
        )

        try:
            return cf.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Command timed out after {timeout}s")

    def profiles(self):
        """List connected Chrome profiles."""
        return [{
            "profileId": a["profileId"],
            "email": a.get("email", ""),
            "tabs": len(a.get("tabs", [])),
            "windows": a.get("windowCount", 0),
            "version": a.get("version", "1.x")
        } for a in self._agents]

    def chrome(self, target=None):
        """Get a Chrome controller for a specific profile."""
        return Chrome(self, target)

    def wait_for_agent(self, timeout=10, min_agents=1):
        """Wait until at least one extension agent is connected."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._agents) >= min_agents:
                return self.profiles()
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for {min_agents} agent(s)")

    def hub_metrics(self):
        """Get hub server metrics."""
        return self._send("bridge.hub.metrics")

    def close(self):
        """Close the hub connection."""
        self._closing.set()
        self._connected.clear()
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try: self.close()
        except Exception: pass

    def __repr__(self):
        return f"Hub(agents={len(self._agents)}, connected={self._connected.is_set()})"


# ════════════════════════════════════════
# ── Async Client ──
# ════════════════════════════════════════

class AsyncChrome:
    """Async Chrome controller — all methods are coroutines."""

    def __init__(self, hub, target=None):
        self._hub = hub
        self._target = target

    async def _cmd(self, command, **params):
        return _normalize_bridge_result(await self._hub._send_async(command, params, self._target))

    async def tabs(self): return await self._cmd('tabs.list')
    async def tab(self, tab_id): return await self._cmd('tabs.get', tabId=tab_id)
    async def find(self, url='', title=''):
        r = await self._cmd('tabs.find', url=url, title=title)
        return r if isinstance(r, list) else []
    async def find_one(self, url='', title=''):
        r = await self.find(url=url, title=title)
        return r[0] if r else None
    async def new_tab(self, url='about:blank', active=True):
        return await self._cmd('tabs.create', url=url, active=active)
    async def navigate(self, tab_id, url):
        return await self._cmd('tabs.navigate', tabId=tab_id, url=url)
    async def close_tab(self, tab_id):
        return await self._cmd('tabs.close', tabId=tab_id)
    async def activate(self, tab_id, focus_window=True):
        return await self._cmd('tabs.activate', tabId=tab_id, focusWindow=focus_window)
    async def reload(self, tab_id, bypass_cache=False):
        return await self._cmd('tabs.reload', tabId=tab_id, bypassCache=bypass_cache)
    async def eval(self, tab_id, expression, await_promise=False):
        return await self._cmd('eval', tabId=tab_id, expression=expression, awaitPromise=await_promise)
    async def click(self, tab_id, selector=None, x=None, y=None):
        return await self._cmd('click', tabId=tab_id, selector=selector, x=x, y=y)
    async def click_text(self, tab_id, text, tag='*', partial=False):
        return await self._cmd('click.text', tabId=tab_id, text=text, tag=tag, partial=partial)
    async def type(self, tab_id, selector, text, clear=False):
        return await self._cmd('type', tabId=tab_id, selector=selector, text=text, clear=clear)
    async def screenshot(self, tab_id, filepath=None, quality=80, fmt='png'):
        result = await self._cmd('screenshot', tabId=tab_id, format=fmt, quality=quality)
        if result and filepath and 'dataUrl' in result:
            header, data = result['dataUrl'].split(',', 1)
            with open(filepath, 'wb') as f: f.write(base64.b64decode(data))
            return filepath
        return result
    async def screenshot_full(self, tab_id, filepath=None, quality=80, fmt='jpeg'):
        result = await self._cmd('screenshot.full', tabId=tab_id, format=fmt, quality=quality)
        if result and filepath and 'data' in result:
            with open(filepath, 'wb') as f: f.write(base64.b64decode(result['data']))
            return filepath
        return result
    async def page_info(self, tab_id):
        return await self._cmd('page.info', tabId=tab_id)
    async def html(self, tab_id, selector=None):
        return await self._cmd('page.html', tabId=tab_id, selector=selector)
    async def text(self, tab_id, selector=None):
        return await self._cmd('page.text', tabId=tab_id, selector=selector)
    async def scroll(self, tab_id, delta_y=300, delta_x=0):
        return await self._cmd('scroll', tabId=tab_id, deltaY=delta_y, deltaX=delta_x)
    async def xpath(self, tab_id, expression):
        return await self._cmd('xpath', tabId=tab_id, expression=expression)
    async def xpath_all(self, tab_id, expression, limit=50):
        return await self._cmd('xpath.all', tabId=tab_id, expression=expression, limit=limit)
    async def extract_links(self, tab_id, filter=None):
        return await self._cmd('links.extract', tabId=tab_id, filter=filter)
    async def extract_tables(self, tab_id, selector=None):
        return await self._cmd('tables.extract', tabId=tab_id, selector=selector)
    async def extract_meta(self, tab_id):
        return await self._cmd('meta.extract', tabId=tab_id)
    async def forms(self, tab_id):
        return await self._cmd('forms.detect', tabId=tab_id)
    async def fill_form(self, tab_id, data, form_index=0):
        return await self._cmd('forms.fill', tabId=tab_id, data=data, formIndex=form_index)
    async def pdf(self, tab_id, filepath=None, **kwargs):
        result = await self._cmd('page.pdf', tabId=tab_id, **kwargs)
        if result and filepath and 'data' in result:
            with open(filepath, 'wb') as f: f.write(base64.b64decode(result['data']))
            return filepath
        return result
    async def emulate(self, tab_id, device='iphone14'):
        return await self._cmd('emulate.device', tabId=tab_id, device=device)
    async def web_vitals(self, tab_id):
        return await self._cmd('performance.webVitals', tabId=tab_id)
    async def timing(self, tab_id):
        return await self._cmd('performance.timing', tabId=tab_id)
    async def a11y_snapshot(self, tab_id, depth=4):
        return await self._cmd('a11y.snapshot', tabId=tab_id, depth=depth)
    async def wait_for(self, tab_id, selector=None, text=None, timeout=10000):
        if selector: return await self._cmd('wait.element', tabId=tab_id, selector=selector, timeout=timeout)
        if text: return await self._cmd('wait.text', tabId=tab_id, text=text, timeout=timeout)
    async def wait_idle(self, tab_id, timeout=30000, idle_time=2000):
        return await self._cmd('wait.idle', tabId=tab_id, timeout=timeout, idleTime=idle_time)
    async def cookies(self, url=None, domain=None):
        return await self._cmd('cookies.get', url=url, domain=domain)
    async def local_storage(self, tab_id, key=None):
        return await self._cmd('storage.local.get', tabId=tab_id, key=key)
    async def remove_local_storage(self, tab_id, key):
        return await self._cmd('storage.local.remove', tabId=tab_id, key=key)
    async def console_logs(self, tab_id, level=None):
        return await self._cmd('console.getLogs', tabId=tab_id, level=level)
    async def enable_console(self, tab_id):
        return await self._cmd('console.enable', tabId=tab_id)
    async def cdp(self, tab_id, method, params=None):
        return await self._cmd('cdp', tabId=tab_id, method=method, params=params or {})
    async def add_binding(self, tab_id, name):
        return await self._cmd('binding.add', tabId=tab_id, name=name)
    async def ping(self):
        return await self._cmd('bridge.ping')
    async def capabilities(self):
        return await self._cmd('bridge.capabilities')
    async def status(self):
        return await self._cmd('bridge.status')
    async def configure_bridge(self, hub_url, apply_now=False):
        return await self._cmd('bridge.configure', hubUrl=hub_url, applyNow=apply_now)
    async def reconnect_bridge(self):
        return await self._cmd('bridge.reconnect')
    async def reload_bridge(self):
        return await self._cmd('bridge.reload')
    async def detach_all_debuggers(self):
        return await self._cmd('bridge.detachAll')
    async def notify(self, message, title='Chrome Bridge'):
        return await self._cmd('notify', message=message, title=title)
    async def inject_css(self, tab_id, css):
        return await self._cmd('css.inject', tabId=tab_id, css=css)
    async def search_text(self, tab_id, query, highlight=False):
        return await self._cmd('search.text', tabId=tab_id, query=query, highlight=highlight)
    async def infinite_scroll(self, tab_id, max_scrolls=10, delay=1500):
        return await self._cmd('scroll.infinite', tabId=tab_id, maxScrolls=max_scrolls, delay=delay)

    # Smart element finding
    async def smart_find(self, tab_id, description, action=None):
        return await self._cmd('smart.find', tabId=tab_id, description=description, action=action)
    async def smart_click(self, tab_id, description):
        return await self._cmd('smart.click', tabId=tab_id, description=description)
    async def smart_fill(self, tab_id, description, value):
        return await self._cmd('smart.fill', tabId=tab_id, description=description, value=value)
    async def smart_wait(self, tab_id, description, timeout=10000):
        return await self._cmd('smart.wait', tabId=tab_id, description=description, timeout=timeout)
    async def wait_interactive(self, tab_id, selector, timeout=10000):
        return await self._cmd('element.interactive', tabId=tab_id, selector=selector, timeout=timeout)

    # Element traversal & advanced wait
    async def element_wait_gone(self, tab_id, selector, timeout=10000):
        return await self._cmd('element.waitGone', tabId=tab_id, selector=selector, timeout=timeout)
    async def dom_wait_stable(self, tab_id, selector='body', idle_ms=500, timeout=10000):
        return await self._cmd('dom.waitStable', tabId=tab_id, selector=selector, idleMs=idle_ms, timeout=timeout)
    async def element_attributes(self, tab_id, selector):
        return await self._cmd('element.attributes', tabId=tab_id, selector=selector)
    async def element_set_attribute(self, tab_id, selector, name, value):
        return await self._cmd('element.setAttribute', tabId=tab_id, selector=selector, name=name, value=value)
    async def element_xpath(self, tab_id, selector):
        return await self._cmd('element.xpath', tabId=tab_id, selector=selector)
    async def element_dispatch_event(self, tab_id, selector, event_type, detail=None):
        return await self._cmd('element.dispatchEvent', tabId=tab_id, selector=selector, eventType=event_type, detail=detail)
    async def element_parent(self, tab_id, selector, levels=1):
        return await self._cmd('element.parent', tabId=tab_id, selector=selector, levels=levels)
    async def element_children(self, tab_id, selector, filter_tag=None):
        return await self._cmd('element.children', tabId=tab_id, selector=selector, filter=filter_tag)
    async def element_siblings(self, tab_id, selector):
        return await self._cmd('element.siblings', tabId=tab_id, selector=selector)
    async def scroll_position(self, tab_id, selector=None):
        return await self._cmd('scroll.position', tabId=tab_id, selector=selector)

    # Intersection observer
    async def intersection_observe(self, tab_id, selector, threshold=0.5):
        return await self._cmd('intersection.observe', tabId=tab_id, selector=selector, threshold=threshold)
    async def intersection_check(self, tab_id, selector):
        return await self._cmd('intersection.check', tabId=tab_id, selector=selector)
    async def intersection_stop(self, tab_id, selector=None):
        return await self._cmd('intersection.stop', tabId=tab_id, selector=selector)

    # Canvas & visual
    async def canvas_read_pixels(self, tab_id, selector, x=0, y=0, width=1, height=1):
        return await self._cmd('canvas.readPixels', tabId=tab_id, selector=selector, x=x, y=y, width=width, height=height)
    async def element_highlight_multiple(self, tab_id, selectors, duration=2000):
        return await self._cmd('element.highlight.multiple', tabId=tab_id, selectors=selectors, duration=duration)

    # Network XHR observation
    async def network_observe_xhr(self, tab_id, filter_url=None):
        return await self._cmd('network.observeXHR', tabId=tab_id, filterUrl=filter_url)
    async def network_flush_xhr(self, tab_id):
        return await self._cmd('network.flushXHR', tabId=tab_id)

    # Smart select
    async def smart_select(self, tab_id, selector, value=None, text=None, index=None):
        return await self._cmd('smart.select', tabId=tab_id, selector=selector, value=value, text=text, index=index)

    # Advanced navigation & wait
    async def wait_for_function(self, tab_id, expression, timeout=10000, poll_ms=200):
        return await self._cmd('wait.function', tabId=tab_id, expression=expression, timeout=timeout, pollMs=poll_ms)
    async def wait_for_url(self, tab_id, pattern, timeout=15000, poll_ms=300):
        return await self._cmd('wait.url', tabId=tab_id, pattern=pattern, timeout=timeout, pollMs=poll_ms)
    async def tabs_create_and_wait(self, url, timeout=30000):
        return await self._cmd('tabs.createAndWait', url=url, timeout=timeout)
    async def tabs_navigate_post(self, tab_id, url, post_data):
        return await self._cmd('tabs.navigatePost', tabId=tab_id, url=url, postData=post_data)

    # DOM snapshot & diff
    async def dom_snapshot(self, tab_id, snapshot_id='default'):
        return await self._cmd('dom.snapshot', tabId=tab_id, snapshotId=snapshot_id)
    async def dom_diff(self, tab_id, snapshot_id='default'):
        return await self._cmd('dom.diff', tabId=tab_id, snapshotId=snapshot_id)

    # Drag
    async def drag(self, tab_id, from_sel, to_sel):
        return await self._cmd('drag', tabId=tab_id, **{'from': from_sel, 'to': to_sel})
    async def drag_coords(self, tab_id, fx, fy, tx, ty, steps=10):
        return await self._cmd('input.dragCDP', tabId=tab_id, fromX=fx, fromY=fy, toX=tx, toY=ty, steps=steps)

    # Recording
    async def record_start(self, tab_id):
        return await self._cmd('record.start', tabId=tab_id)
    async def record_stop(self, tab_id):
        return await self._cmd('record.stop', tabId=tab_id)
    async def record_replay(self, tab_id, events, speed=1):
        return await self._cmd('record.replay', tabId=tab_id, events=events, speed=speed)

    # Interception
    async def fetch_enable(self, tab_id, patterns=None):
        return await self._cmd('fetch.enable', tabId=tab_id, patterns=patterns)
    async def fetch_disable(self, tab_id):
        return await self._cmd('fetch.disable', tabId=tab_id)
    async def fetch_body(self, tab_id, request_id):
        return await self._cmd('fetch.getBody', tabId=tab_id, requestId=request_id)
    async def fetch_fulfill(self, tab_id, request_id, body=None, code=200, headers=None):
        return await self._cmd('fetch.fulfill', tabId=tab_id, requestId=request_id, body=body, responseCode=code, responseHeaders=headers or [])

    # HAR
    async def har_start(self, tab_id):
        return await self._cmd('har.start', tabId=tab_id)
    async def har_stop(self, tab_id):
        return await self._cmd('har.stop', tabId=tab_id)

    # Coverage
    async def coverage_start_js(self, tab_id):
        return await self._cmd('coverage.startJS', tabId=tab_id)
    async def coverage_stop_js(self, tab_id):
        return await self._cmd('coverage.stopJS', tabId=tab_id)
    async def coverage_start_css(self, tab_id):
        return await self._cmd('coverage.startCSS', tabId=tab_id)
    async def coverage_stop_css(self, tab_id):
        return await self._cmd('coverage.stopCSS', tabId=tab_id)

    # Stealth
    async def stealth_enable(self, tab_id):
        return await self._cmd('stealth.enable', tabId=tab_id)
    async def stealth_check(self, tab_id):
        return await self._cmd('stealth.check', tabId=tab_id)

    # Page readiness & iframes
    async def page_readiness(self, tab_id):
        return await self._cmd('page.readiness', tabId=tab_id)
    async def iframes(self, tab_id):
        return await self._cmd('iframe.list', tabId=tab_id)
    async def iframe_eval(self, tab_id, index, expression):
        return await self._cmd('iframe.eval', tabId=tab_id, index=index, expression=expression)

    # Memory
    async def memory_info(self, tab_id):
        return await self._cmd('memory.info', tabId=tab_id)

    # Stealth
    async def stealth_inject(self, tab_id, script=None):
        return await self._cmd('stealth.inject', tabId=tab_id, script=script)
    async def rotate_ua(self, tab_id, user_agent=None):
        return await self._cmd('stealth.rotateUA', tabId=tab_id, userAgent=user_agent)

    # WebSocket interception
    async def ws_enable(self, tab_id):
        return await self._cmd('ws.enable', tabId=tab_id)
    async def ws_frames(self, tab_id, limit=100):
        return await self._cmd('ws.getFrames', tabId=tab_id, limit=limit)

    # Network power
    async def response_body(self, tab_id, request_id):
        return await self._cmd('network.getResponseBody', tabId=tab_id, requestId=request_id)
    async def enable_network(self, tab_id):
        return await self._cmd('network.enable', tabId=tab_id)
    async def intercept_network(self, tab_id, patterns=None, intercept_stage='HeadersReceived'):
        return await self._cmd(
            'network.intercept',
            tabId=tab_id,
            patterns=patterns,
            interceptStage=intercept_stage,
        )
    async def set_headers(self, tab_id, headers):
        return await self._cmd('network.setHeaders', tabId=tab_id, headers=headers)
    async def set_user_agent(self, tab_id, user_agent):
        return await self._cmd('network.setUserAgent', tabId=tab_id, userAgent=user_agent)

    # Navigation
    async def nav_back(self, tab_id):
        return await self._cmd('navigation.back', tabId=tab_id)
    async def nav_forward(self, tab_id):
        return await self._cmd('navigation.forward', tabId=tab_id)
    async def nav_history(self, tab_id):
        return await self._cmd('navigation.history', tabId=tab_id)
    async def delete_history(self, url):
        return await self._cmd('history.delete', url=url)

    # Frame tree
    async def frame_tree(self, tab_id):
        return await self._cmd('frames.tree', tabId=tab_id)

    # Cookie profiles
    async def export_cookies(self, **kwargs):
        return await self._cmd('cookies.export', filter=kwargs if kwargs else None)
    async def import_cookies(self, cookies):
        return await self._cmd('cookies.import', cookies=cookies)

    # Multi-tab
    async def multi_eval(self, expression, tab_ids=None):
        return await self._cmd('multi.eval', expression=expression, tabIds=tab_ids)
    async def multi_close(self, url_pattern=None, title_pattern=None):
        return await self._cmd('multi.close', urlPattern=url_pattern, titlePattern=title_pattern)

    # Workflow
    async def workflow(self, steps, vars=None):
        return await self._cmd('workflow.run', steps=steps, vars=vars or {})

    # Auto-healing
    async def heal_click(self, tab_id, selector=None, text=None, xpath=None):
        return await self._cmd('heal.click', tabId=tab_id, selector=selector, text=text, xpath=xpath)

    # Visual regression
    async def visual_capture(self, tab_id=None, name=None):
        return await self._cmd('visual.capture', tabId=tab_id, name=name)
    async def visual_compare(self, tab_id=None, name=None):
        return await self._cmd('visual.compare', tabId=tab_id, name=name)

    # Page lifecycle
    async def stop_loading(self, tab_id):
        return await self._cmd('page.stopLoading', tabId=tab_id)
    async def handle_dialog(self, tab_id, accept=True, prompt_text=None):
        return await self._cmd('page.handleDialog', tabId=tab_id, accept=accept, promptText=prompt_text)
    async def set_content(self, tab_id, html):
        return await self._cmd('page.setContent', tabId=tab_id, html=html)


class AsyncHub:
    """Async hub connection for use with asyncio."""

    def __init__(self, url=None):
        self._url = _resolve_hub_url(url)
        self._ws = None
        self._agents = []
        self._pending = {}
        self._next_id = 0
        self._listener_task = None

    async def connect(self):
        self._ws = await websockets.connect(self._url, max_size=50 * 1024 * 1024)
        await self._ws.send(json.dumps({"type": "client"}))
        self._listener_task = asyncio.create_task(self._listen())
        # Wait for initial agent list
        await asyncio.sleep(0.2)

    async def _listen(self):
        try:
            async for raw in self._ws:
                try: msg = json.loads(raw)
                except (ValueError, TypeError): continue
                if msg.get("type") == "agents":
                    self._agents = msg["agents"]
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    self._pending[msg_id].set_result(msg)
        except websockets.ConnectionClosed:
            for _, future in list(self._pending.items()):
                if not future.done():
                    future.set_exception(ConnectionError("Hub connection lost"))
            self._pending.clear()

    async def _send_async(self, command, params=None, target=None):
        self._next_id += 1
        msg_id = self._next_id
        msg = {"id": msg_id, "command": command, "params": params or {}}
        if target: msg["target"] = target

        future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        await self._ws.send(json.dumps(msg))

        try:
            result_msg = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"Command timed out after 30s")

        self._pending.pop(msg_id, None)
        if "error" in result_msg:
            raise RuntimeError(result_msg["error"])
        return result_msg.get("result")

    def profiles(self):
        return [{
            "profileId": a["profileId"],
            "email": a.get("email", ""),
            "tabs": len(a.get("tabs", [])),
            "windows": a.get("windowCount", 0)
        } for a in self._agents]

    def chrome(self, target=None):
        return AsyncChrome(self, target)

    async def wait_for_agent(self, timeout=10, min_agents=1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._agents) >= min_agents:
                return self.profiles()
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for {min_agents} agent(s)")

    async def close(self):
        if self._listener_task: self._listener_task.cancel()
        if self._ws: await self._ws.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()


# ── Quick helpers ──

def quick(url=None):
    """Quick-connect: returns (hub, chrome) for immediate use."""
    hub = Hub(url)
    return hub, hub.chrome()


if __name__ == "__main__":
    print("Chrome Bridge v3.1 — Direct Worker Transport Client")
    print("=" * 40)
    try:
        hub = Hub()
        print(f"Connected to hub")
        print(f"Profiles: {hub.profiles()}")

        chrome = hub.chrome()
        tabs = chrome.tabs()
        print(f"\nOpen tabs ({len(tabs)}):")
        for t in tabs:
            print(f"  [{t['id']:>4}] {t['title'][:50]:50s} {t['url'][:60]}")

        # Latency test
        start = time.time()
        chrome.ping()
        latency = (time.time() - start) * 1000
        print(f"\nPing latency: {latency:.0f}ms")

    except ConnectionError:
        print("Hub not running. Start it with: python server.py")
    except Exception as e:
        print(f"Error: {e}")
