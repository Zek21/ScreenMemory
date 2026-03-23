# Unified Spatial Perception Engine — Research Report

## Summary

The **PerceptionEngine** is a deterministic structural perception system that builds a unified spatial graph of an entire digital environment by fusing three independent data sources:

1. **Win32 window hierarchy** — enumerates all visible top-level windows with geometry, z-order, HWND handles, and process information.
2. **UIA accessibility tree** — scans native UI elements (buttons, text boxes, menus) exposed through Microsoft UI Automation.
3. **CDP (Chrome DevTools Protocol)** — walks the DOM of every open Chrome tab, extracting elements with bounding boxes, roles, and interactivity metadata.

The engine merges all discovered elements into a single **SpatialGrid** — a cell-based spatial index that supports O(1) coordinate lookups, proximity queries, role/name searches, and z-order resolution. A built-in **TopologicalMemory** cache avoids redundant rescans when page layouts haven't changed.

All interaction is **API-only**:

| Target | Method | Physical Input |
|--------|--------|---------------|
| Chrome elements | CDP `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` | None — events are injected directly into the renderer |
| Win32 windows | `PostMessage` / `SendMessage` | None — messages are posted to the window's message queue |
| Native UI elements | UIA `InvokePattern` | None — automation patterns fire without mouse or keyboard |

The engine never moves the user's mouse cursor, never sends physical keyboard events, and never steals window focus.

---

## Requirements

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | 3.10+ | Runtime |
| `comtypes` | any | COM interface access for UI Automation |
| `pywin32` | any | Win32 API bindings (`ctypes.windll`) |
| Chrome/Chromium | any | Must be launched with `--remote-debugging-port=9222` for CDP |
| `uia.exe` (optional) | bundled | Pre-compiled UIA scanner binary (falls back gracefully if absent) |

Install Python dependencies:

```bash
pip install comtypes pywin32
```

Chrome must be started with remote debugging enabled:

```bash
chrome.exe --remote-debugging-port=9222
```

---

## Quick Start

### Example 1 — Scan the entire environment

```python
from tools.chrome_bridge.perception import PerceptionEngine

engine = PerceptionEngine(cdp_port=9222)
result = engine.scan_world(depth=3)

print(f"Windows: {result['windows']}")
print(f"UIA elements: {result['uia_elements']}")
print(f"Chrome elements: {result['chrome_elements']}")
print(f"Total spatial nodes: {result['total_nodes']}")
print(f"Scan time: {result['scan_time_ms']}ms")
```

### Example 2 — Find and click a button in Chrome

```python
from tools.chrome_bridge.perception import PerceptionEngine

engine = PerceptionEngine()
engine.scan_world()

# Find all elements named "Submit"
buttons = engine.find("Submit", role="button")
if buttons:
    tab_id = engine.chrome.active_tab()
    engine.chrome_click(tab_id, buttons[0])
    print(f"Clicked: {buttons[0]}")
```

### Example 3 — Query what's at a screen coordinate

```python
from tools.chrome_bridge.perception import PerceptionEngine

engine = PerceptionEngine()
engine.scan_world()

# Ask "what element is at pixel (500, 300)?"
elements = engine.what_is_at(500, 300)
for el in elements:
    print(f"  [{el['source']}] {el['role']}: {el['name']}  z={el['z']}")

# Get the topmost element at that point
topmost = engine.grid.topmost_at(500, 300)
if topmost:
    print(f"Topmost: {topmost}")
```

---

## API Reference

### `PerceptionEngine`

The main entry point. Instantiate with an optional CDP port (default `9222`).

```python
engine = PerceptionEngine(cdp_port=9222)
```

#### Environment Scanning

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `scan_world` | `(include_chrome_dom=True, depth=3)` | `Dict` | Full 3-source scan. Populates the spatial grid with all discovered nodes. Returns scan statistics: `windows`, `uia_elements`, `chrome_elements`, `total_nodes`, `scan_time_ms`, `cached_layouts`. |
| `summary` | `()` | `Dict` | Complete state summary: monitor count/layout, node counts by source, actionable count, Chrome connection status, UIA availability, scan history, cache state. |

#### Spatial Queries

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `what_is_at` | `(x: int, y: int)` | `List[Dict]` | All elements containing the screen point `(x, y)`, sorted by z-order (topmost first). |
| `find` | `(name: str, role=None, source=None)` | `List[SpatialNode]` | Fuzzy name search across all sources. Optionally filter by `role` (e.g. `"button"`) and `source` (e.g. `"cdp"`, `"uia"`, `"win32"`). |
| `find_actionable` | `(name: str = None)` | `List[SpatialNode]` | All elements that can be interacted with (buttons, links, inputs). Optional name filter. |
| `nearest_to` | `(x, y, role=None, count=5)` | `List[SpatialNode]` | The `count` nearest elements to `(x, y)`, optionally filtered by role. Sorted by Euclidean distance. |
| `windows_on_monitor` | `(monitor_idx=0)` | `List[SpatialNode]` | All Win32 windows on the specified monitor (0-indexed). |
| `stacking_order` | `()` | `List[Dict]` | All windows sorted by z-order. Each entry: `{z, name, bounds, hwnd}`. |
| `path_to` | `(target_name: str, from_source=None)` | `Dict` | Finds the most efficient interaction path to a named element. Prioritizes CDP (fastest) → UIA (native) → Win32 (window-level). Returns `{found, element, method, source, distance_from_origin}`. |

#### Chrome Interaction (CDP — Zero Mouse)

All Chrome interactions use the CDP Input domain. No physical mouse or keyboard events are generated.

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `chrome_tabs` | `()` | `List[Dict]` | List all open Chrome tabs. |
| `chrome_page_elements` | `(tab_id=None, depth=4)` | `List[SpatialNode]` | Get all DOM elements with bounding boxes for a tab. Defaults to active tab. |
| `chrome_click` | `(tab_id, target)` | `bool` | Click in Chrome. `target` can be a `SpatialNode` (click center), `str` (CSS selector), or `tuple(x, y)` (coordinates). |
| `chrome_type` | `(tab_id, text)` | `None` | Type text into the focused element via CDP key events. |
| `chrome_navigate` | `(tab_id, url)` | `None` | Navigate a tab to a URL via CDP `Page.navigate`. |
| `chrome_eval` | `(tab_id, js)` | `Any` | Execute JavaScript in a tab's context. |
| `chrome_screenshot` | `(tab_id=None)` | `bytes` | Capture a tab screenshot as PNG bytes via CDP. Defaults to active tab. |

#### Win32 Interaction (PostMessage — No Focus Steal)

All Win32 interactions use `PostMessage`/`SendMessage`. The target window does not need to be focused.

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `win32_move` | `(window_name, x, y, w=None, h=None)` | `bool` | Move/resize a window by name. Uses `SetWindowPos` with `SWP_NOACTIVATE`. |
| `win32_minimize` | `(window_name)` | `bool` | Minimize a window by name. |
| `win32_post_click` | `(hwnd, x, y)` | `None` | Click inside a window via `WM_LBUTTONDOWN`/`WM_LBUTTONUP`. Works on background windows. |

---

### `SpatialGrid`

A cell-based spatial index for O(1) element lookups by coordinates. Used internally by `PerceptionEngine` and exposed via `engine.grid`.

```python
grid = engine.grid  # Access after scan_world()
```

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `at` | `(px, py)` | `List[SpatialNode]` | All nodes containing point `(px, py)`, sorted by z-order. |
| `topmost_at` | `(px, py)` | `Optional[SpatialNode]` | The topmost (lowest z-index) node at the given point. |
| `nearby` | `(node, radius=200)` | `List[SpatialNode]` | All nodes within `radius` pixels of `node`'s center, sorted by distance. |
| `find_by_role` | `(role: str)` | `List[SpatialNode]` | All nodes with matching role (e.g. `"button"`, `"window"`, `"link"`). |
| `find_by_name` | `(name: str, fuzzy=True)` | `List[SpatialNode]` | Name search. Fuzzy mode (default) matches substrings case-insensitively. |
| `insert` | `(node: SpatialNode)` | `None` | Insert a node into the index. |
| `clear` | `()` | `None` | Remove all nodes from the index. |
| `all_nodes` | *(property)* | `List[SpatialNode]` | All nodes currently in the grid. |

**Constructor:**

```python
SpatialGrid(cell_size=100)
```

The `cell_size` parameter controls the granularity of the spatial grid. Smaller cells give faster lookups but use more memory. The default of 100 pixels is optimal for typical desktop resolutions.

---

### `SpatialNode`

The universal element representation. Every element from every source (Win32, UIA, CDP) is normalized into this structure.

| Property | Type | Description |
|----------|------|-------------|
| `id` | `str` | Unique identifier: `"{source}:{name}:{x},{y}"` |
| `source` | `str` | Origin: `"win32"`, `"uia"`, `"cdp"`, or `"cdp-a11y"` |
| `name` | `str` | Human-readable label (window title, button text, aria-label) |
| `role` | `str` | Semantic role: `"window"`, `"button"`, `"textbox"`, `"link"`, etc. |
| `value` | `str` | Current value (input field content, etc.) |
| `x`, `y` | `int` | Top-left screen coordinates |
| `w`, `h` | `int` | Width and height in pixels |
| `z` | `int` | Z-order index (0 = topmost) |
| `actionable` | `bool` | Whether the element can be interacted with |
| `actions` | `List[str]` | Available actions: `"click"`, `"type"`, `"focus"`, `"close"`, etc. |
| `meta` | `Dict` | Source-specific metadata (`hwnd`, `pid`, `element_id`, `css_class`, etc.) |

| Computed Property | Returns | Description |
|-------------------|---------|-------------|
| `cx` | `int` | Center X coordinate |
| `cy` | `int` | Center Y coordinate |
| `bounds` | `Tuple[int,int,int,int]` | Bounding box as `(left, top, right, bottom)` |

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `distance_to` | `(other: SpatialNode)` | `float` | Euclidean distance between centers |
| `contains_point` | `(px, py)` | `bool` | Whether the point is inside this node's bounds |
| `overlaps` | `(other: SpatialNode)` | `bool` | Whether this node's bounds overlap another's |
| `to_dict` | `()` | `Dict` | Serializable dictionary representation. **Note:** `to_dict()['bounds']` returns `[x, y, w, h]` (origin + size list), which differs from the `bounds` property that returns `(left, top, right, bottom)` (edge tuple). |

---

### `TopologicalMemory`

Layout caching system — avoids redundant rescans when page structure hasn't changed. Accessible via `engine.memory`.

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `remember` | `(key, nodes, fingerprint='')` | `None` | Cache a set of nodes under a key with an optional fingerprint. |
| `recall` | `(key, fingerprint='')` | `Optional[List[SpatialNode]]` | Retrieve cached nodes. Returns `None` if expired (TTL: 30s) or fingerprint mismatch. |
| `forget` | `(key)` | `None` | Remove a specific cache entry. |
| `forget_all` | `()` | `None` | Clear entire cache. |
| `known_layouts` | *(property)* | `List[str]` | List of currently cached layout keys. |

---

### CLI Interface

The engine includes a command-line interface for interactive exploration:

```bash
python tools/chrome_bridge/perception.py <command> [args] [--port 9222] [--depth 3] [--json]
```

| Command | Arguments | Description |
|---------|-----------|-------------|
| `scan` | — | Full environment scan, print statistics |
| `find` | `<name>` | Search for elements by name |
| `at` | `<x> <y>` | Query elements at screen coordinates |
| `tabs` | — | List Chrome tabs |
| `click` | `<selector_or_text>` | Click element in Chrome by selector or text |
| `type` | `<text>` | Type text into focused Chrome element |
| `navigate` | `<url>` | Navigate active Chrome tab to URL |
| `screenshot` | `[output_path]` | Capture Chrome tab screenshot |
| `windows` | — | List all visible windows with geometry |
| `stacking` | — | Show window z-order |
| `monitors` | — | List monitor geometries |
| `path` | `<element_name>` | Find optimal interaction path to element |
| `summary` | — | Full engine state summary |

---

## Architecture

### Three-Source Fusion Model

The PerceptionEngine achieves comprehensive environment awareness by merging three complementary data sources, each contributing unique information that the others cannot provide:

```
┌─────────────────────────────────────────────────────────────┐
│                    PerceptionEngine                          │
│                                                             │
│  ┌───────────────┐  ┌───────────────┐  ┌────────────────┐  │
│  │  Win32Scanner  │  │  UIAScanner   │  │ CDPPerception  │  │
│  │               │  │               │  │                │  │
│  │ • EnumWindows │  │ • a11y tree   │  │ • DOM walk     │  │
│  │ • z-order     │  │ • InvokePtr   │  │ • a11y tree    │  │
│  │ • GetWindowRect│ │ • find/scan   │  │ • Input.disp.  │  │
│  │ • PostMessage │  │ • Patterns    │  │ • Page.nav     │  │
│  │ • MoveWindow  │  │               │  │ • JS eval      │  │
│  └───────┬───────┘  └───────┬───────┘  └───────┬────────┘  │
│          │                  │                   │            │
│          └──────────────────┼───────────────────┘            │
│                             │                                │
│                   ┌─────────▼──────────┐                    │
│                   │    SpatialGrid     │                    │
│                   │                    │                    │
│                   │  Cell-based index  │                    │
│                   │  O(1) coord lookup │                    │
│                   │  z-order sorting   │                    │
│                   │  proximity queries │                    │
│                   └─────────┬──────────┘                    │
│                             │                                │
│                   ┌─────────▼──────────┐                    │
│                   │ TopologicalMemory  │                    │
│                   │                    │                    │
│                   │  Layout caching    │                    │
│                   │  Fingerprint match │                    │
│                   │  30s TTL           │                    │
│                   └────────────────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

### Source Contributions

| Source | Provides | Cannot Provide |
|--------|----------|----------------|
| **Win32** | Window titles, HWNDs, z-order, geometry, process IDs, class names | In-window UI elements, web page content |
| **UIA** | Native UI controls (buttons, menus, text fields), invoke patterns, accessibility names | Web page DOM, window z-order |
| **CDP** | DOM elements with bounding boxes, JavaScript context, page navigation, tab management, accessibility tree | Desktop windows, native UI controls |

### Scan Pipeline

When `scan_world()` is called, the engine executes three independent scans:

1. **Layer 1 — Win32:** `EnumWindows` iterates all visible top-level windows. Each window becomes a `SpatialNode` with `source='win32'`, carrying HWND, PID, class name, and z-index. System windows (taskbar, desktop) are filtered out.

2. **Layer 2 — UIA:** The `uia.exe` binary scans the accessibility tree to a configurable depth. Each accessible element (button, checkbox, menu item) becomes a `SpatialNode` with `source='uia'`, carrying available automation patterns (Invoke, Toggle, Value, etc.).

3. **Layer 3 — CDP:** For each non-internal Chrome tab, a JavaScript function walks the DOM tree extracting elements with `getBoundingClientRect()`. Results are cached per-tab using a page fingerprint (title + element count + URL). Cache TTL is 30 seconds. Elements become `SpatialNode` instances with `source='cdp'`.

All nodes are inserted into the `SpatialGrid`, which partitions space into cells of configurable size (default 100px). This enables O(1) lookups by coordinate and efficient spatial proximity queries.

### Interaction Priority

When `path_to()` resolves the best way to interact with a named element, sources are prioritized by speed and reliability:

| Priority | Source | Method | Latency |
|----------|--------|--------|---------|
| 1 | `cdp` | `Input.dispatchMouseEvent` | ~1ms |
| 2 | `cdp-a11y` | CDP accessibility invoke | ~5ms |
| 3 | `uia` | UIA `InvokePattern` | ~10ms |
| 4 | `win32` | `PostMessage` | ~1ms (window-level only) |

CDP is fastest because events are injected directly into the Chromium renderer process. UIA is slightly slower due to the cross-process COM call. Win32 PostMessage is fast but can only interact at the window level, not with individual UI elements inside the window.

---

## Performance Characteristics

| Metric | Typical Value | Notes |
|--------|---------------|-------|
| Full `scan_world()` | 50–200ms | Depends on number of windows and Chrome tabs |
| Win32 layer only | 5–15ms | Fast — pure Win32 API calls |
| UIA layer | 20–80ms | Depends on UI complexity and scan depth |
| CDP layer (cached) | <1ms | Fingerprint match skips re-scan |
| CDP layer (uncached) | 30–120ms | JavaScript DOM walk execution |
| Coordinate lookup (`at`) | <0.1ms | O(1) grid cell lookup |
| Proximity query (`nearby`) | 1–5ms | Linear scan of all nodes |
| Name search (`find`) | 1–5ms | Linear scan with substring match |
| Chrome click (CDP) | ~1ms | Input.dispatchMouseEvent |
| Win32 PostMessage click | ~1ms | Asynchronous message post |
| UIA invoke | ~10ms | Cross-process COM call |
| Memory per node | ~500 bytes | SpatialNode with `__slots__` optimization |
| Cache TTL | 30 seconds | TopologicalMemory expiry |
| Grid cell size | 100px | Configurable; trades memory for lookup speed |

### Scalability

- **Nodes:** Handles 10,000+ spatial nodes without degradation. `SpatialGrid` cell-based indexing keeps coordinate lookups constant-time.
- **Tabs:** Each Chrome tab adds one DOM scan. Caching prevents redundant rescans for static pages.
- **Monitors:** Multi-monitor layouts are fully supported. `windows_on_monitor()` filters by monitor geometry.
- **Memory:** `TopologicalMemory` auto-expires entries after 30 seconds. No unbounded growth.

---

## Use Cases

### 1. Desktop Environment Mapping

Enumerate all windows across all monitors with their positions, sizes, and z-order:

```python
engine = PerceptionEngine()
engine.scan_world(include_chrome_dom=False)

for entry in engine.stacking_order():
    print(f"z={entry['z']} | {entry['name'][:40]} | bounds={entry['bounds']}")
```

### 2. Cross-Application Element Discovery

Find a UI element by name regardless of whether it lives in a Win32 window, a native UI control, or a Chrome web page:

```python
engine = PerceptionEngine()
engine.scan_world()

results = engine.find("Settings")
for node in results:
    print(f"[{node.source}] {node.role}: '{node.name}' at ({node.x},{node.y})")
```

### 3. Zero-Input Browser Automation

Automate Chrome interactions entirely through CDP without touching the physical mouse or keyboard:

```python
engine = PerceptionEngine()
tab = engine.chrome.active_tab()

engine.chrome_navigate(tab, "https://example.com")
engine.chrome_type(tab, "search query")
engine.chrome_click(tab, "button.submit")

screenshot_png = engine.chrome_screenshot(tab)
```

### 4. Spatial Proximity Queries

Find elements near a specific screen location — useful for context-aware automation:

```python
engine = PerceptionEngine()
engine.scan_world()

# Find the 5 nearest buttons to coordinate (1000, 500)
nearby_buttons = engine.nearest_to(1000, 500, role="button", count=5)
for btn in nearby_buttons:
    print(f"  {btn.name} — distance: {btn.distance_to(SpatialNode('q','','',x=1000,y=500))}px")
```

### 5. Optimal Interaction Path Resolution

Automatically determine the fastest way to interact with a named element:

```python
engine = PerceptionEngine()
engine.scan_world()

path = engine.path_to("Save")
if path['found']:
    print(f"Element: {path['element']['name']}")
    print(f"Source: {path['source']}")
    print(f"Method: {path['method']}")
    # e.g. "CDP Input.dispatch (zero mouse)" or "UIA Invoke pattern (zero mouse)"
```

### 6. Background Window Control

Move, resize, and minimize windows without stealing focus or interrupting the user:

```python
engine = PerceptionEngine()
engine.scan_world(include_chrome_dom=False)

# Move "Notepad" to a specific position without activating it
engine.win32_move("Notepad", x=100, y=100, w=800, h=600)

# Minimize a window by name
engine.win32_minimize("Calculator")
```

### 7. Monitor-Aware Layout Management

Query and organize windows by monitor:

```python
engine = PerceptionEngine()
engine.scan_world(include_chrome_dom=False)

# Get all monitors
monitors = engine.win32.get_monitors()
for i, mon in enumerate(monitors):
    print(f"Monitor {i}: {mon['w']}x{mon['h']} at ({mon['x']},{mon['y']})")
    windows = engine.windows_on_monitor(i)
    for w in windows:
        print(f"  - {w.name[:40]}")
```

### 8. Occlusion-Aware Click Targeting

Determine which element is truly visible at a point, accounting for window stacking:

```python
engine = PerceptionEngine()
engine.scan_world()

# What's actually visible (topmost) at this coordinate?
topmost = engine.grid.topmost_at(500, 300)
if topmost:
    print(f"Topmost element: [{topmost.source}] {topmost.name}")
    print(f"Occluded elements below:")
    all_at = engine.grid.at(500, 300)
    for node in all_at[1:]:
        print(f"  z={node.z} [{node.source}] {node.name}")
```

---

*Research report generated from source analysis of `tools/chrome_bridge/perception.py` (1020 lines). All API signatures and behavior descriptions reflect the actual implementation.*

<!-- signed: delta -->
