"""
Perception Engine — Deterministic Structural Perception for Digital Environments

Zero-pixel, zero-mouse navigation system.
Builds a 3D spatial graph from accessibility trees (UIA + CDP a11y),
DOM structure, and Win32 window hierarchy. Controls everything through
API-only paths: CDP Input domain for Chrome, PostMessage for Win32.

Architecture:
    ┌─────────────────────────────────────────────────────┐
    │                 Perception Engine                     │
    │                                                       │
    │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
    │  │ Win32    │  │   UIA    │  │  CDP (Chrome)    │   │
    │  │ Z-order  │  │  A11y    │  │  DOM + A11y +    │   │
    │  │ HWND     │  │  Tree    │  │  Input.dispatch  │   │
    │  │ Geometry │  │  Invoke  │  │  (zero mouse)    │   │
    │  └────┬─────┘  └────┬─────┘  └────┬─────────────┘   │
    │       │              │             │                   │
    │       └──────────────┼─────────────┘                   │
    │                      │                                 │
    │            ┌─────────▼──────────┐                     │
    │            │  Spatial Graph      │                     │
    │            │  • Node = element   │                     │
    │            │  • Edge = proximity │                     │
    │            │  • Z = stacking     │                     │
    │            │  • Memory = cache   │                     │
    │            └────────────────────┘                     │
    └─────────────────────────────────────────────────────┘

Rules:
    1. NEVER touch user's mouse or keyboard (no SendInput, no keybd_event)
    2. Chrome interaction = CDP Input domain only (dispatch events inside renderer)
    3. Win32 interaction = PostMessage/SendMessage only (background, no focus steal)
    4. All perception = UIA + CDP accessibility + Win32 EnumWindows
"""

import json
import math
import time
import os
import sys
import subprocess
import socket
from collections import defaultdict
from typing import Optional, List, Dict, Tuple, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, CDPError


# ═══════════════════════════════════════════════════════════════
# SPATIAL NODE — Universal element representation
# ═══════════════════════════════════════════════════════════════

class SpatialNode:
    """A node in the spatial graph. Represents any element from any source."""
    __slots__ = (
        'id', 'source', 'name', 'role', 'value',
        'x', 'y', 'w', 'h', 'z',
        'parent_id', 'children_ids',
        'actionable', 'actions', 'meta',
        '_hash'
    )

    def __init__(self, source: str, name: str, role: str,
                 x=0, y=0, w=0, h=0, z=0, **meta):
        self.id = f"{source}:{name}:{x},{y}".replace(' ', '_')
        self.source = source          # "cdp", "uia", "win32"
        self.name = name
        self.role = role              # "button", "textbox", "window", "link", etc.
        self.value = meta.pop('value', '')
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.z = z                    # 0 = topmost
        self.parent_id = meta.pop('parent_id', None)
        self.children_ids = []
        self.actionable = meta.pop('actionable', False)
        self.actions = meta.pop('actions', [])
        self.meta = meta              # source-specific data (hwnd, nodeId, etc.)
        self._hash = hash((source, name, x, y, w, h))

    @property
    def cx(self):
        return self.x + self.w // 2

    @property
    def cy(self):
        return self.y + self.h // 2

    @property
    def bounds(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def distance_to(self, other: 'SpatialNode') -> float:
        return math.sqrt((self.cx - other.cx)**2 + (self.cy - other.cy)**2)

    def contains_point(self, px, py) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def overlaps(self, other: 'SpatialNode') -> bool:
        return not (self.x + self.w < other.x or other.x + other.w < self.x or
                    self.y + self.h < other.y or other.y + other.h < self.y)

    def to_dict(self):
        return {
            'id': self.id, 'source': self.source, 'name': self.name,
            'role': self.role, 'value': self.value,
            'bounds': [self.x, self.y, self.w, self.h], 'z': self.z,
            'actionable': self.actionable, 'actions': self.actions,
        }

    def __repr__(self):
        return f"<{self.source}:{self.role} '{self.name}' @({self.x},{self.y} {self.w}x{self.h}) z={self.z}>"


# ═══════════════════════════════════════════════════════════════
# SPATIAL GRID — Fast spatial indexing (replaces quadtree)
# ═══════════════════════════════════════════════════════════════

class SpatialGrid:
    """Grid-based spatial index for O(1) element lookup by coordinates."""

    def __init__(self, cell_size=100):
        self.cell_size = cell_size
        self.cells: Dict[Tuple[int, int], List[SpatialNode]] = defaultdict(list)
        self._all: List[SpatialNode] = []

    def clear(self):
        self.cells.clear()
        self._all.clear()

    def insert(self, node: SpatialNode):
        self._all.append(node)
        x0 = node.x // self.cell_size
        y0 = node.y // self.cell_size
        x1 = (node.x + node.w) // self.cell_size
        y1 = (node.y + node.h) // self.cell_size
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                self.cells[(cx, cy)].append(node)

    def at(self, px, py) -> List[SpatialNode]:
        """Get all nodes containing point (px, py), sorted by z-order."""
        cell = (px // self.cell_size, py // self.cell_size)
        hits = [n for n in self.cells.get(cell, []) if n.contains_point(px, py)]
        hits.sort(key=lambda n: n.z)
        return hits

    def nearby(self, node: SpatialNode, radius=200) -> List[SpatialNode]:
        """Get nodes within radius of node's center."""
        results = []
        for n in self._all:
            if n.id != node.id and node.distance_to(n) <= radius:
                results.append((node.distance_to(n), n))
        results.sort(key=lambda x: x[0])
        return [n for _, n in results]

    def find_by_role(self, role: str) -> List[SpatialNode]:
        return [n for n in self._all if n.role == role]

    def find_by_name(self, name: str, fuzzy=True) -> List[SpatialNode]:
        name_lower = name.lower()
        if fuzzy:
            return [n for n in self._all if name_lower in n.name.lower()]
        return [n for n in self._all if n.name.lower() == name_lower]

    def topmost_at(self, px, py) -> Optional[SpatialNode]:
        hits = self.at(px, py)
        return hits[0] if hits else None

    @property
    def all_nodes(self):
        return self._all


# ═══════════════════════════════════════════════════════════════
# TOPOLOGICAL MEMORY — Layout caching / "muscle memory"
# ═══════════════════════════════════════════════════════════════

class TopologicalMemory:
    """Persistent spatial memory that caches known layouts.
    Avoids re-scanning when structure hasn't changed."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}  # key → {nodes, timestamp, fingerprint}
        self._ttl = 30  # seconds before cache expires

    def remember(self, key: str, nodes: List[SpatialNode], fingerprint: str = ''):
        self._cache[key] = {
            'nodes': nodes,
            'timestamp': time.time(),
            'fingerprint': fingerprint,
            'access_count': 0,
        }

    def recall(self, key: str, fingerprint: str = '') -> Optional[List[SpatialNode]]:
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry['timestamp'] > self._ttl:
            del self._cache[key]
            return None
        if fingerprint and entry['fingerprint'] != fingerprint:
            return None
        entry['access_count'] += 1
        return entry['nodes']

    def forget(self, key: str):
        self._cache.pop(key, None)

    def forget_all(self):
        self._cache.clear()

    @property
    def known_layouts(self):
        return list(self._cache.keys())


# ═══════════════════════════════════════════════════════════════
# WIN32 SCANNER — Window hierarchy, z-order, background control
# ═══════════════════════════════════════════════════════════════

class Win32Scanner:
    """Scans Windows desktop without stealing focus or touching mouse."""

    def __init__(self):
        import ctypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self._WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        )

    def enum_windows(self) -> List[SpatialNode]:
        """Get all visible top-level windows with geometry and z-order."""
        import ctypes
        import ctypes.wintypes

        windows = []
        z_index = [0]

        def callback(hwnd, _):
            hwnd_val = hwnd if isinstance(hwnd, int) else ctypes.cast(hwnd, ctypes.c_void_p).value
            node = self._build_window_node(hwnd_val, z_index[0], ctypes)
            if node:
                windows.append(node)
                z_index[0] += 1
            return True

        cb = self._WNDENUMPROC(callback)
        self.user32.EnumWindows(cb, 0)
        return windows

    _SKIP_CLASSES = frozenset(('Progman', 'WorkerW', 'Shell_TrayWnd',
                               'Shell_SecondaryTrayWnd', 'NotifyIconOverflowWindow'))

    def _build_window_node(self, hwnd_val, z, ctypes) -> 'SpatialNode':
        """Build a SpatialNode for a single window, or None if it should be skipped."""
        if not self.user32.IsWindowVisible(hwnd_val):
            return None
        length = self.user32.GetWindowTextLengthW(hwnd_val)
        if length == 0:
            return None
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd_val, buf, length + 1)
        cls_buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd_val, cls_buf, 256)
        if cls_buf.value in self._SKIP_CLASSES:
            return None
        rect = ctypes.wintypes.RECT()
        self.user32.GetWindowRect(hwnd_val, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 10 or h < 10:
            return None
        pid = ctypes.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd_val, ctypes.byref(pid))
        return SpatialNode(
            source='win32', name=buf.value, role='window',
            x=rect.left, y=rect.top, w=w, h=h, z=z,
            actionable=True,
            actions=['focus', 'move', 'resize', 'close', 'minimize', 'maximize'],
            hwnd=hwnd_val, class_name=cls_buf.value, pid=pid.value,
        )

    def send_message(self, hwnd, msg, wparam=0, lparam=0):
        """Send message to window without stealing focus."""
        return self.user32.SendMessageW(hwnd, msg, wparam, lparam)

    def post_message(self, hwnd, msg, wparam=0, lparam=0):
        """Post async message to window without stealing focus."""
        return self.user32.PostMessageW(hwnd, msg, wparam, lparam)

    def get_foreground(self) -> int:
        return self.user32.GetForegroundWindow()

    def get_monitors(self) -> List[Dict]:
        """Get all monitor geometries."""
        import ctypes
        import ctypes.wintypes

        monitors = []
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double
        )

        def cb(hmon, hdc, rect, data):
            monitors.append({
                'x': rect.contents.left, 'y': rect.contents.top,
                'w': rect.contents.right - rect.contents.left,
                'h': rect.contents.bottom - rect.contents.top,
                'handle': hmon,
            })
            return 1

        self.user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
        return monitors

    def move_window(self, hwnd, x, y, w, h):
        """Move/resize window without stealing focus."""
        import ctypes
        SWP_NOACTIVATE = 0x0010
        SWP_NOZORDER = 0x0004
        self.user32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOACTIVATE | SWP_NOZORDER)

    def minimize(self, hwnd):
        SW_MINIMIZE = 6
        self.user32.ShowWindow(hwnd, SW_MINIMIZE)

    def restore(self, hwnd):
        SW_RESTORE = 9
        self.user32.ShowWindow(hwnd, SW_RESTORE)


# ═══════════════════════════════════════════════════════════════
# UIA SCANNER — Accessibility tree for native UI elements
# ═══════════════════════════════════════════════════════════════

class UIAScanner:
    """Wraps uia.exe for accessibility tree scanning."""

    def __init__(self, exe_path=None):
        if exe_path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            candidates = [
                os.path.join(base, 'dist', 'uia.exe'),
                os.path.join(base, 'native', 'uia.exe'),
            ]
            for c in candidates:
                if os.path.isfile(c):
                    exe_path = c
                    break
        self.exe = exe_path

    def available(self) -> bool:
        return self.exe is not None and os.path.isfile(self.exe)

    def _run(self, *args) -> str:
        if not self.available():
            return ''
        result = subprocess.run(
            [self.exe] + list(args),
            capture_output=True, text=True, timeout=10
        )
        return result.stdout

    def scan(self, hwnd=None, depth=3) -> List[SpatialNode]:
        """Scan accessibility tree, optionally rooted at hwnd."""
        cmd_args = ['scan', '--depth', str(depth)]
        if hwnd:
            cmd_args.extend(['--hwnd', str(hwnd)])
        raw = self._run(*cmd_args)
        return self._parse_json_elements(raw)

    def find(self, name: str) -> List[SpatialNode]:
        raw = self._run('find', name)
        return self._parse_json_elements(raw)

    def invoke(self, name: str) -> bool:
        """Invoke a UIA element by name (no mouse needed)."""
        raw = self._run('invoke', name)
        return 'ok' in raw.lower() or 'invoked' in raw.lower()

    def _parse_json_elements(self, raw: str) -> List[SpatialNode]:
        nodes = []
        for line in raw.strip().split('\n'):
            line = line.strip()
            if not line or not line.startswith('{'):
                continue
            try:
                d = json.loads(line)
                node = SpatialNode(
                    source='uia',
                    name=d.get('name', ''),
                    role=d.get('type', d.get('controlType', 'unknown')).lower(),
                    x=d.get('x', 0), y=d.get('y', 0),
                    w=d.get('w', 0), h=d.get('h', 0),
                    actionable=bool(d.get('patterns')),
                    actions=d.get('patterns', []),
                    value=d.get('value', ''),
                )
                nodes.append(node)
            except (json.JSONDecodeError, KeyError):
                continue
        return nodes


# ═══════════════════════════════════════════════════════════════
# CDP PERCEPTION — Chrome DOM + Accessibility + Input (zero mouse)
# ═══════════════════════════════════════════════════════════════

class CDPPerception:
    """Chrome perception and interaction through CDP only.
    NEVER touches physical mouse or keyboard."""

    def __init__(self, port=9222):
        self.port = port
        self._cdp: Optional[CDP] = None

    @property
    def cdp(self) -> CDP:
        if self._cdp is None:
            self._cdp = CDP(port=self.port)
        return self._cdp

    @property
    def connected(self) -> bool:
        try:
            self.cdp.tabs()
            return True
        except Exception:
            self._cdp = None
            return False

    def tabs(self) -> List[Dict]:
        return self.cdp.tabs()

    def active_tab(self) -> Optional[str]:
        tabs = self.tabs()
        return tabs[0]['id'] if tabs else None

    # ─── Perception (read-only, no side effects) ─────────

    _DOM_WALK_JS = '''
    (function() {{
        var root = document.querySelector({selector});
        if (!root) return '[]';
        var elements = [];
        function walk(el, depth) {{
            if (depth <= 0) return;
            var rect = el.getBoundingClientRect();
            if (rect.width < 1 && rect.height < 1) return;
            var tag = el.tagName ? el.tagName.toLowerCase() : '';
            var role = el.getAttribute('role') || el.tagName || '';
            var name = el.getAttribute('aria-label') ||
                       el.getAttribute('title') ||
                       el.getAttribute('alt') ||
                       el.getAttribute('placeholder') ||
                       (el.innerText || '').substring(0, 50).trim();
            var isActionable = ['A','BUTTON','INPUT','SELECT','TEXTAREA'].includes(el.tagName) ||
                               el.getAttribute('onclick') || el.getAttribute('role') === 'button';
            elements.push({{
                tag: tag, role: role.toLowerCase(), name: name,
                x: Math.round(rect.x), y: Math.round(rect.y),
                w: Math.round(rect.width), h: Math.round(rect.height),
                actionable: !!isActionable, id: el.id || '',
                cls: (el.className || '').toString().substring(0, 60),
                href: el.href || '', type: el.type || '',
                value: (el.value || '').substring(0, 100),
            }});
            for (var i = 0; i < el.children.length; i++) {{
                walk(el.children[i], depth - 1);
            }}
        }}
        walk(root, {depth});
        return JSON.stringify(elements);
    }})()
    '''

    def get_dom_tree(self, tab_id: str, selector='body', depth=4) -> List[SpatialNode]:
        """Get DOM elements as spatial nodes with bounding boxes."""
        js = self._DOM_WALK_JS.format(selector=json.dumps(selector), depth=depth)
        raw = self.cdp.eval(tab_id, js)
        if not raw or raw == '[]':
            return []
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        return [self._dom_item_to_node(d) for d in items]

    @staticmethod
    def _dom_item_to_node(d: dict) -> SpatialNode:
        """Convert a raw DOM item dict to a SpatialNode."""
        actions = []
        if d.get('actionable'):
            actions = ['click']
            if d.get('tag') in ('input', 'textarea', 'select'):
                actions.append('type')
        return SpatialNode(
            source='cdp',
            name=d.get('name', '') or d.get('id', '') or d.get('tag', ''),
            role=d.get('role', d.get('tag', 'element')),
            x=d.get('x', 0), y=d.get('y', 0),
            w=d.get('w', 0), h=d.get('h', 0),
            actionable=d.get('actionable', False), actions=actions,
            value=d.get('value', ''), tag=d.get('tag', ''),
            element_id=d.get('id', ''), css_class=d.get('cls', ''),
            href=d.get('href', ''),
        )

    def get_accessibility_tree(self, tab_id: str) -> List[SpatialNode]:
        """Get Chrome's accessibility tree as spatial nodes."""
        try:
            ax_nodes = self.cdp.accessibility_tree(tab_id)
        except Exception:
            return []

        nodes = []
        for ax in ax_nodes:
            name_obj = ax.get('name', {})
            name = name_obj.get('value', '') if isinstance(name_obj, dict) else str(name_obj)
            role_obj = ax.get('role', {})
            role = role_obj.get('value', '') if isinstance(role_obj, dict) else str(role_obj)

            bb = ax.get('backendDOMNodeId')
            node = SpatialNode(
                source='cdp-a11y',
                name=name, role=role,
                actionable=role in ('button', 'link', 'textbox', 'checkbox',
                                    'radio', 'menuitem', 'tab', 'combobox'),
                ax_node_id=ax.get('nodeId', ''),
                backend_node_id=bb,
            )
            nodes.append(node)
        return nodes

    def get_page_fingerprint(self, tab_id: str) -> str:
        """Quick fingerprint of page state (URL + element count + title)."""
        fp = self.cdp.eval(tab_id, 'document.title + "|" + document.querySelectorAll("*").length + "|" + location.href')
        return str(fp)

    # ─── Action (CDP Input domain — zero mouse) ─────────

    def click_element(self, tab_id: str, node: SpatialNode):
        """Click an element via CDP Input.dispatchMouseEvent — no physical mouse."""
        self.cdp.click(tab_id, node.cx, node.cy)

    def click_at(self, tab_id: str, x: int, y: int):
        """Click at coordinates via CDP — no physical mouse."""
        self.cdp.click(tab_id, x, y)

    def type_text(self, tab_id: str, text: str):
        """Type text via CDP Input.dispatchKeyEvent — no physical keyboard."""
        self.cdp.type_text(tab_id, text)

    def press_key(self, tab_id: str, key: str):
        """Press key via CDP — no physical keyboard."""
        self.cdp.press_key(tab_id, key)

    def scroll_to(self, tab_id: str, x=0, y=0, delta_y=-300):
        """Scroll via CDP — no physical mouse wheel."""
        self.cdp.scroll(tab_id, x=x, y=y, delta_y=delta_y)

    def click_by_text(self, tab_id: str, text: str) -> bool:
        """Find and click element by visible text — no mouse."""
        js = f"""
        (function() {{
            var text = {json.dumps(text)};
            var all = document.querySelectorAll('a, button, [role=button], input[type=submit], [onclick]');
            for (var i = 0; i < all.length; i++) {{
                if ((all[i].innerText || '').trim().includes(text) ||
                    (all[i].value || '').includes(text) ||
                    (all[i].getAttribute('aria-label') || '').includes(text)) {{
                    all[i].click();
                    return 'clicked';
                }}
            }}
            return 'not_found';
        }})()
        """
        result = self.cdp.eval(tab_id, js)
        return result == 'clicked'

    def click_by_selector(self, tab_id: str, selector: str) -> bool:
        """Click via JS .click() — no mouse. Works even on hidden elements."""
        result = self.cdp.eval(tab_id, f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (el) {{ el.click(); return 'clicked'; }}
                return 'not_found';
            }})()
        """)
        return result == 'clicked'

    def fill_input(self, tab_id: str, selector: str, value: str):
        """Fill an input field via JS — no keyboard."""
        self.cdp.eval(tab_id, f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (el) {{
                    el.focus();
                    el.value = {json.dumps(value)};
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }})()
        """)

    def navigate(self, tab_id: str, url: str, wait=True):
        """Navigate tab via CDP — no address bar typing."""
        self.cdp.navigate(tab_id, url, wait=wait)

    def screenshot(self, tab_id: str) -> bytes:
        """Screenshot via CDP — no screen capture."""
        return self.cdp.screenshot(tab_id)

    def eval_js(self, tab_id: str, expression: str):
        """Eval JS in tab context."""
        return self.cdp.eval(tab_id, expression)


# ═══════════════════════════════════════════════════════════════
# PERCEPTION ENGINE — The unified spatial model
# ═══════════════════════════════════════════════════════════════

class PerceptionEngine:
    """
    Deterministic Structural Perception Engine.

    Maintains a unified spatial graph of all digital environments:
    - Chrome tabs (via CDP DOM + accessibility tree)
    - Windows desktop (via Win32 EnumWindows + z-order)
    - Native UI elements (via UIA accessibility tree)

    All interaction is API-only:
    - Chrome: CDP Input domain (zero mouse)
    - Win32: PostMessage/SendMessage (background, no focus steal)
    - UIA: Invoke pattern (no mouse)
    """

    def __init__(self, cdp_port=9222):
        self.chrome = CDPPerception(port=cdp_port)
        self.win32 = Win32Scanner()
        self.uia = UIAScanner()
        self.grid = SpatialGrid(cell_size=100)
        self.memory = TopologicalMemory()
        self._scan_count = 0
        self._last_scan_time = 0

    # ─── Full Environment Scan ───────────────────────────

    def scan_world(self, include_chrome_dom=True, depth=3) -> Dict:
        """Build complete spatial model of the digital environment."""
        t0 = time.time()
        self.grid.clear()

        windows = self._scan_win32_layer()
        uia_nodes = self._scan_uia_layer(depth)
        chrome_nodes = self._scan_chrome_layer(depth) if include_chrome_dom else []

        self._scan_count += 1
        self._last_scan_time = time.time() - t0
        return {
            'windows': len(windows), 'uia_elements': len(uia_nodes),
            'chrome_elements': len(chrome_nodes),
            'total_nodes': len(self.grid.all_nodes),
            'scan_time_ms': round(self._last_scan_time * 1000, 1),
            'cached_layouts': len(self.memory.known_layouts),
        }

    def _scan_win32_layer(self):
        """Layer 1: Win32 windows."""
        windows = self.win32.enum_windows()
        for w in windows:
            self.grid.insert(w)
        return windows

    def _scan_uia_layer(self, depth):
        """Layer 2: UIA accessibility elements."""
        if not self.uia.available():
            return []
        try:
            nodes = self.uia.scan(depth=depth)
            for n in nodes:
                self.grid.insert(n)
            return nodes
        except Exception:
            return []

    def _scan_chrome_layer(self, depth):
        """Layer 3: Chrome DOM elements with caching."""
        if not self.chrome.connected:
            return []
        chrome_nodes = []
        try:
            for tab in self.chrome.tabs():
                url = tab.get('url', '')
                if url.startswith('chrome://') or url.startswith('devtools://'):
                    continue
                nodes = self._scan_chrome_tab(tab['id'], depth)
                chrome_nodes.extend(nodes)
        except Exception:
            pass
        return chrome_nodes

    def _scan_chrome_tab(self, tab_id, depth):
        """Scan a single Chrome tab, using cache if available."""
        fp = self.chrome.get_page_fingerprint(tab_id)
        cached = self.memory.recall(f'dom:{tab_id}', fp)
        if cached:
            nodes = cached
        else:
            nodes = self.chrome.get_dom_tree(tab_id, depth=depth)
            self.memory.remember(f'dom:{tab_id}', nodes, fp)
        for n in nodes:
            self.grid.insert(n)
        return nodes

    # ─── Spatial Queries ─────────────────────────────────

    def what_is_at(self, x, y) -> List[Dict]:
        """What elements exist at screen coordinates (x, y)?"""
        nodes = self.grid.at(x, y)
        return [n.to_dict() for n in nodes]

    def find(self, name: str, role: str = None, source: str = None) -> List[SpatialNode]:
        """Find elements by name, optionally filtered by role and source."""
        results = self.grid.find_by_name(name, fuzzy=True)
        if role:
            results = [n for n in results if n.role == role]
        if source:
            results = [n for n in results if n.source == source]
        return results

    def find_actionable(self, name: str = None) -> List[SpatialNode]:
        """Find all actionable elements, optionally filtered by name."""
        nodes = [n for n in self.grid.all_nodes if n.actionable]
        if name:
            name_lower = name.lower()
            nodes = [n for n in nodes if name_lower in n.name.lower()]
        return nodes

    def nearest_to(self, x, y, role=None, count=5) -> List[SpatialNode]:
        """Find nearest elements to coordinates."""
        ref = SpatialNode('query', '', '', x=x, y=y, w=1, h=1)
        candidates = self.grid.all_nodes
        if role:
            candidates = [n for n in candidates if n.role == role]
        by_dist = sorted(candidates, key=lambda n: n.distance_to(ref))
        return by_dist[:count]

    def windows_on_monitor(self, monitor_idx=0) -> List[SpatialNode]:
        """Get windows on a specific monitor."""
        monitors = self.win32.get_monitors()
        if monitor_idx >= len(monitors):
            return []
        mon = monitors[monitor_idx]
        return [n for n in self.grid.all_nodes
                if n.source == 'win32'
                and n.x >= mon['x'] and n.x < mon['x'] + mon['w']]

    def stacking_order(self) -> List[Dict]:
        """Get all windows in z-order (topmost first)."""
        windows = [n for n in self.grid.all_nodes if n.source == 'win32']
        windows.sort(key=lambda n: n.z)
        return [{'z': n.z, 'name': n.name, 'bounds': n.bounds,
                 'hwnd': n.meta.get('hwnd')} for n in windows]

    # ─── Chrome-Specific Perception ──────────────────────

    def chrome_tabs(self) -> List[Dict]:
        """List Chrome tabs without touching mouse."""
        if not self.chrome.connected:
            return []
        return self.chrome.tabs()

    def chrome_page_elements(self, tab_id: str = None, depth=4) -> List[SpatialNode]:
        """Get all interactive elements on current Chrome page."""
        if tab_id is None:
            tab_id = self.chrome.active_tab()
        if not tab_id:
            return []
        return self.chrome.get_dom_tree(tab_id, depth=depth)

    # ─── Chrome Actions (CDP Input — zero mouse) ────────

    def chrome_click(self, tab_id: str, target) -> bool:
        """Click in Chrome via CDP Input domain. Target can be:
        - SpatialNode: click at node center
        - str: CSS selector → JS click
        - tuple (x,y): click at coordinates
        NEVER touches physical mouse."""
        if isinstance(target, SpatialNode):
            self.chrome.click_element(tab_id, target)
            return True
        elif isinstance(target, str):
            return self.chrome.click_by_selector(tab_id, target)
        elif isinstance(target, tuple):
            self.chrome.click_at(tab_id, target[0], target[1])
            return True
        return False

    def chrome_type(self, tab_id: str, text: str):
        """Type in Chrome via CDP — no physical keyboard."""
        self.chrome.type_text(tab_id, text)

    def chrome_navigate(self, tab_id: str, url: str):
        """Navigate Chrome tab via CDP — no address bar."""
        self.chrome.navigate(tab_id, url)

    def chrome_eval(self, tab_id: str, js: str):
        """Run JS in Chrome tab via CDP."""
        return self.chrome.eval_js(tab_id, js)

    def chrome_screenshot(self, tab_id: str = None) -> bytes:
        """Screenshot Chrome tab via CDP — no screen capture."""
        if tab_id is None:
            tab_id = self.chrome.active_tab()
        return self.chrome.screenshot(tab_id)

    # ─── Win32 Actions (PostMessage — no focus steal) ────

    def win32_move(self, window_name: str, x, y, w=None, h=None) -> bool:
        """Move/resize a window by name — no focus steal."""
        windows = self.find(window_name, role='window', source='win32')
        if not windows:
            return False
        hwnd = windows[0].meta.get('hwnd')
        if not hwnd:
            return False
        if w is None:
            w = windows[0].w
        if h is None:
            h = windows[0].h
        self.win32.move_window(hwnd, x, y, w, h)
        return True

    def win32_minimize(self, window_name: str) -> bool:
        windows = self.find(window_name, role='window', source='win32')
        if not windows:
            return False
        self.win32.minimize(windows[0].meta['hwnd'])
        return True

    def win32_post_click(self, hwnd: int, x: int, y: int):
        """Click inside a window via PostMessage — works on background windows."""
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        lparam = y << 16 | (x & 0xFFFF)
        self.win32.post_message(hwnd, WM_LBUTTONDOWN, 1, lparam)
        time.sleep(0.05)
        self.win32.post_message(hwnd, WM_LBUTTONUP, 0, lparam)

    # ─── High-Level Pathfinding ──────────────────────────

    def path_to(self, target_name: str, from_source=None) -> Dict:
        """Find the most efficient path to interact with a named element.
        Returns the element and the method to reach it."""
        # Search in order: CDP (fastest) → UIA (native) → Win32 (window-level)
        results = self.find(target_name)
        if not results:
            return {'found': False, 'name': target_name}

        # Prioritize by source efficiency
        priority = {'cdp': 0, 'cdp-a11y': 1, 'uia': 2, 'win32': 3}
        if from_source:
            results = [n for n in results if n.source == from_source]
        else:
            results.sort(key=lambda n: priority.get(n.source, 99))

        best = results[0]
        method = {
            'cdp': 'CDP Input.dispatch (zero mouse)',
            'cdp-a11y': 'CDP accessibility invoke (zero mouse)',
            'uia': 'UIA Invoke pattern (zero mouse)',
            'win32': 'PostMessage (background, no focus steal)',
        }.get(best.source, 'unknown')

        return {
            'found': True,
            'element': best.to_dict(),
            'method': method,
            'source': best.source,
            'distance_from_origin': math.sqrt(best.cx**2 + best.cy**2),
        }

    # ─── State Summary ───────────────────────────────────

    def summary(self) -> Dict:
        """Complete state summary of the perception engine."""
        monitors = self.win32.get_monitors()
        return {
            'monitors': len(monitors),
            'monitor_layout': monitors,
            'total_nodes': len(self.grid.all_nodes),
            'by_source': {
                'win32': len([n for n in self.grid.all_nodes if n.source == 'win32']),
                'uia': len([n for n in self.grid.all_nodes if n.source == 'uia']),
                'cdp': len([n for n in self.grid.all_nodes if n.source == 'cdp']),
                'cdp-a11y': len([n for n in self.grid.all_nodes if n.source == 'cdp-a11y']),
            },
            'actionable': len([n for n in self.grid.all_nodes if n.actionable]),
            'chrome_connected': self.chrome.connected,
            'chrome_tabs': len(self.chrome.tabs()) if self.chrome.connected else 0,
            'uia_available': self.uia.available(),
            'scans': self._scan_count,
            'last_scan_ms': round(self._last_scan_time * 1000, 1),
            'cached_layouts': len(self.memory.known_layouts),
        }


# ═══════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════

def _cmd_find(engine, args):
    if not args.args:
        print('Usage: perception find <name>'); return
    engine.scan_world(depth=args.depth)
    for n in engine.find(' '.join(args.args))[:20]:
        print(n)


def _cmd_at(engine, args):
    if len(args.args) < 2:
        print('Usage: perception at <x> <y>'); return
    engine.scan_world(depth=args.depth)
    print(json.dumps(engine.what_is_at(int(args.args[0]), int(args.args[1])), indent=2))


def _cmd_tabs(engine, args):
    for t in engine.chrome_tabs():
        print(f"  [{t['id'][:8]}] {t.get('title', '?')[:60]}")
        print(f"             {t.get('url', '?')[:80]}")


def _cmd_click(engine, args):
    if not args.args:
        print('Usage: perception click <selector_or_text>'); return
    ok = engine.chrome_click(engine.chrome.active_tab(), ' '.join(args.args))
    print('clicked' if ok else 'not found')


def _cmd_type(engine, args):
    if not args.args:
        print('Usage: perception type <text>'); return
    engine.chrome_type(engine.chrome.active_tab(), ' '.join(args.args))
    print('typed')


def _cmd_navigate(engine, args):
    if not args.args:
        print('Usage: perception navigate <url>'); return
    engine.chrome_navigate(engine.chrome.active_tab(), args.args[0])
    print(f'navigated to {args.args[0]}')


def _cmd_screenshot(engine, args):
    png = engine.chrome_screenshot(engine.chrome.active_tab())
    out = args.args[0] if args.args else os.path.join('screenshots', 'perception-screenshot.png')
    if not args.args:
        os.makedirs('screenshots', exist_ok=True)
    with open(out, 'wb') as f:
        f.write(png)
    print(f'Saved: {out} ({len(png)} bytes)')


def _cmd_windows(engine, args):
    engine.scan_world(include_chrome_dom=False)
    for n in engine.grid.all_nodes:
        if n.source == 'win32':
            print(f"  z={n.z:2d} hwnd={n.meta.get('hwnd', '?')} [{n.x},{n.y} {n.w}x{n.h}] {n.name[:50]}")


def _cmd_path(engine, args):
    if not args.args:
        print('Usage: perception path <element_name>'); return
    engine.scan_world(depth=args.depth)
    print(json.dumps(engine.path_to(' '.join(args.args)), indent=2, default=str))


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Perception Engine')
    parser.add_argument('command', nargs='?', default='scan',
                        choices=['scan', 'find', 'at', 'tabs', 'click', 'type',
                                 'navigate', 'screenshot', 'windows', 'stacking',
                                 'summary', 'path', 'monitors'],
                        help='Command to execute')
    parser.add_argument('args', nargs='*', help='Command arguments')
    parser.add_argument('--port', type=int, default=9222, help='CDP port')
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--depth', type=int, default=3, help='Scan depth')
    args = parser.parse_args()
    engine = PerceptionEngine(cdp_port=args.port)

    handlers = {
        'scan':       lambda: print(json.dumps(engine.scan_world(depth=args.depth), indent=2)),
        'find':       lambda: _cmd_find(engine, args),
        'at':         lambda: _cmd_at(engine, args),
        'tabs':       lambda: _cmd_tabs(engine, args),
        'click':      lambda: _cmd_click(engine, args),
        'type':       lambda: _cmd_type(engine, args),
        'navigate':   lambda: _cmd_navigate(engine, args),
        'screenshot': lambda: _cmd_screenshot(engine, args),
        'windows':    lambda: _cmd_windows(engine, args),
        'stacking':   lambda: (engine.scan_world(include_chrome_dom=False), [print(f"  z={s['z']:2d} [{s['bounds'][0]},{s['bounds'][1]}->{s['bounds'][2]},{s['bounds'][3]}] {s['name'][:50]}") for s in engine.stacking_order()]),
        'monitors':   lambda: [print(f"  Monitor {i}: {m['x']},{m['y']} {m['w']}x{m['h']}") for i, m in enumerate(engine.win32.get_monitors())],
        'path':       lambda: _cmd_path(engine, args),
        'summary':    lambda: (engine.scan_world(depth=args.depth), print(json.dumps(engine.summary(), indent=2))),
    }
    handlers[args.command]()


if __name__ == '__main__':
    main()
