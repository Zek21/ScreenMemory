# CDP: Chrome DevTools Protocol Direct Controller

> **Version:** 1.0 | **Python:** ≥3.8 | **License:** Proprietary

## Summary

CDP is a zero-dependency Chrome automation library that communicates directly with Google Chrome via the Chrome DevTools Protocol WebSocket interface. It provides ultra-fast browser control (~0.5ms latency per command) by establishing a single WebSocket hop from Python to Chrome, bypassing traditional extension relay chains that introduce 15ms+ latency across three intermediary hops. The library covers the full spectrum of browser automation — tab management, JavaScript evaluation, DOM manipulation, input simulation, network interception, device emulation, performance profiling, and visual capture — all without moving the physical mouse cursor or requiring a browser extension.

---

## Requirements

| Requirement | Value |
|-------------|-------|
| Python | ≥ 3.8 |
| OS | Windows / Linux / macOS (cross-platform) |
| Hardware | CPU-only (no GPU required) |

### Install

```bash
pip install websocket-client
# The CDP module is part of the ScreenMemory toolkit:
# tools/chrome_bridge/cdp.py
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `websocket-client` | ≥1.0 | WebSocket communication with Chrome DevTools |
| `websockets` (optional) | ≥10.0 | Async WebSocket support |

> **Note:** The core stdlib modules `json`, `threading`, `subprocess`, `base64`, `ctypes`, `socket`, and `urllib` are used. No heavy external frameworks are required.

---

## Quick Start

```python
from tools.chrome_bridge.cdp import CDP

# Auto-attach to a running Chrome with debugging enabled
chrome = CDP(port=9222)

# List open tabs
tabs = chrome.tabs()
tab = tabs[0]

# Navigate and capture
chrome.navigate(tab, "https://example.com")
title = chrome.eval(tab, "document.title")
print(title)  # → "Example Domain"

# Take a screenshot
png_bytes = chrome.screenshot(tab)
```

---

## API Reference

### `CDPError`

```python
class CDPError(Exception):
    """Raised for all CDP communication and protocol errors."""
```

### `CDPTab`

```python
class CDPTab:
    """
    Represents a single Chrome tab with its own CDP WebSocket connection.
    """
```

#### Constructor

```python
CDPTab(ws_url: str, tab_info: dict, timeout: int = 30)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ws_url` | `str` | — | WebSocket debugger URL for this tab |
| `tab_info` | `dict` | — | Tab metadata from Chrome's `/json` endpoint |
| `timeout` | `int` | `30` | Timeout in seconds for CDP commands |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `id` | `str` | Unique tab identifier |
| `url` | `str` | Current URL of the tab |
| `title` | `str` | Current page title |

#### Methods

##### `connect() → None`

Establish the WebSocket connection and start the background listener thread.

##### `disconnect() → None`

Close the WebSocket connection and stop the listener thread.

##### `send(method: str, params: dict = None, timeout: int = None) → dict`

Send a raw CDP command and wait for the response.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `method` | `str` | — | CDP method name (e.g., `"Page.navigate"`) |
| `params` | `dict` | `None` | CDP method parameters |
| `timeout` | `int` | `None` | Override the default timeout |

**Returns:** `dict` — The CDP response result object.

**Raises:** `CDPError` — On timeout or protocol error.

##### `on(event: str, callback: Callable) → None`

Register a callback for a CDP event (e.g., `"Network.requestWillBeSent"`).

##### `off(event: str, callback: Callable = None) → None`

Unregister event callbacks. If `callback` is None, removes all callbacks for the event.

##### `enable(domain: str) → None`

Enable a CDP domain (e.g., `"Page"`, `"Network"`, `"DOM"`).

##### `disable(domain: str) → None`

Disable a CDP domain.

---

### `CDP`

```python
class CDP:
    """
    Chrome DevTools Protocol direct controller.
    Zero-dependency Chrome automation — no extension, no hub, no mouse.
    """
```

#### Constructor

```python
CDP(host: str = '127.0.0.1', port: int = 9222, timeout: int = 30)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | `str` | `'127.0.0.1'` | Chrome debug host |
| `port` | `int` | `9222` | Chrome remote debugging port |
| `timeout` | `int` | `30` | Default timeout for all operations |

**Raises:** `CDPError` — If Chrome is not reachable at the specified host:port.

#### Class Methods

##### `CDP.launch(chrome_path=None, port=9222, user_data_dir=None, headless=False, extra_args=None, timeout=30, profile_directory=None) → CDP`

Launch a new Chrome instance with remote debugging enabled and return a connected CDP instance.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `chrome_path` | `str` | `None` | Path to Chrome executable (auto-detected if None) |
| `port` | `int` | `9222` | Remote debugging port |
| `user_data_dir` | `str` | `None` | Chrome user data directory (auto-generated if None) |
| `headless` | `bool` | `False` | Run Chrome in headless mode |
| `extra_args` | `list` | `None` | Additional Chrome command-line arguments |
| `timeout` | `int` | `30` | Seconds to wait for Chrome to start |
| `profile_directory` | `str` | `None` | Chrome profile directory name (e.g., `"Profile 17"`) |

**Returns:** `CDP` — A connected CDP controller instance.

**Example:**
```python
chrome = CDP.launch(headless=True, port=9333)
tabs = chrome.tabs()
```

##### `CDP.attach(timeout=30) → CDP`

Auto-attach to an already-running Chrome instance with remote debugging.

**Returns:** `CDP` — A connected CDP controller.

**Raises:** `CDPError` — If no debuggable Chrome instance is found.

---

#### Browser Info

##### `version() → dict`

Get Chrome browser version information.

##### `protocol() → dict`

Get the full CDP protocol schema.

---

#### Tab Management

##### `tabs() → list`

List all open page tabs (excludes service workers, extensions, etc.).

**Returns:** `list[dict]` — List of tab info dictionaries with `id`, `url`, `title` fields.

##### `new_tab(url: str = 'about:blank') → dict`

Create a new browser tab.

**Returns:** `dict` — Info for the newly created tab.

##### `close_tab(tab_id: str | dict) → None`

Close a tab by ID or tab info dict.

##### `activate_tab(tab_id: str | dict) → None`

Bring a tab to the foreground.

---

#### Navigation

##### `navigate(tab_id, url: str, wait: bool = True, timeout: int = 30) → dict`

Navigate a tab to a URL. By default, waits for the page load event.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tab_id` | `str \| dict` | — | Tab identifier or info dict |
| `url` | `str` | — | Target URL |
| `wait` | `bool` | `True` | Wait for page load to complete |
| `timeout` | `int` | `30` | Load timeout in seconds |

##### `reload(tab_id, ignore_cache: bool = False) → dict`

Reload the current page.

##### `go_back(tab_id) → None`

Navigate back in history.

##### `go_forward(tab_id) → None`

Navigate forward in history.

##### `wait_for_load(tab_id, timeout: int = 30) → None`

Block until the page fires its load event.

##### `get_url(tab_id) → str`

Get the current URL of a tab.

---

#### JavaScript Evaluation

##### `eval(tab_id, expression: str, await_promise: bool = False, return_by_value: bool = True) → Any`

Execute JavaScript in the tab's context. No debugger banner, no extension needed.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tab_id` | `str \| dict` | — | Tab identifier |
| `expression` | `str` | — | JavaScript expression to evaluate |
| `await_promise` | `bool` | `False` | Await promise resolution before returning |
| `return_by_value` | `bool` | `True` | Return the value directly (vs. remote object) |

**Returns:** `Any` — The JavaScript evaluation result.

**Raises:** `CDPError` — On JavaScript exceptions.

**Example:**
```python
title = chrome.eval(tab, "document.title")
data = chrome.eval(tab, 'fetch("/api").then(r=>r.json())', await_promise=True)
```

##### `eval_function(tab_id, function_declaration: str, *args) → Any`

Call a JavaScript function with arguments.

---

#### Screenshots & Visual

##### `screenshot(tab_id, format='png', quality=None, full_page=False, clip=None, file_path=None) → bytes`

Capture a screenshot of the tab.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `format` | `str` | `'png'` | Image format: `'png'` or `'jpeg'` |
| `quality` | `int` | `None` | JPEG quality (0–100) |
| `full_page` | `bool` | `False` | Capture the entire scrollable page |
| `clip` | `dict` | `None` | Region clip: `{x, y, width, height}` |
| `file_path` | `str` | `None` | Save to file path if specified |

**Returns:** `bytes` — PNG or JPEG image data.

##### `pdf(tab_id, file_path=None, landscape=False, print_background=True, scale=1, paper_width=8.5, paper_height=11) → bytes`

Generate a PDF of the page.

---

#### Input Simulation (CDP Input Domain)

All input methods use the CDP `Input` domain — they dispatch events directly into Chrome's rendering engine **without moving the real mouse cursor or pressing physical keys**.

##### `click(tab_id, x: int, y: int, button='left', click_count=1, modifiers=0) → None`

Click at coordinates inside the page viewport.

##### `click_selector(tab_id, selector: str, button='left') → None`

Click an element by CSS selector. Scrolls the element into view and computes center coordinates automatically.

**Raises:** `CDPError` — If the selector matches no element.

##### `double_click(tab_id, x: int, y: int) → None`

Double-click at coordinates.

##### `right_click(tab_id, x: int, y: int) → None`

Right-click at coordinates.

##### `hover(tab_id, x: int, y: int) → None`

Hover the mouse over coordinates (dispatches `mouseMoved` event).

##### `type_text(tab_id, text: str, delay: int = 0) → None`

Type text character by character. No physical keyboard input.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | `str` | — | Text to type |
| `delay` | `int` | `0` | Delay between characters in milliseconds |

##### `press_key(tab_id, key: str, modifiers: int = 0) → None`

Press a named key (Enter, Tab, Escape, ArrowDown, Backspace, etc.).

| Key | Value | Key | Value |
|-----|-------|-----|-------|
| Enter | `'Enter'` | Backspace | `'Backspace'` |
| Tab | `'Tab'` | Delete | `'Delete'` |
| Escape | `'Escape'` | Space | `'Space'` |
| ArrowUp/Down/Left/Right | `'ArrowUp'` etc. | Home/End | `'Home'`/`'End'` |
| PageUp/PageDown | `'PageUp'`/`'PageDown'` | | |

**Modifiers:** `0` = none, `1` = Alt, `2` = Ctrl, `4` = Meta, `8` = Shift (combinable).

##### `scroll(tab_id, x=0, y=0, delta_x=0, delta_y=-100) → None`

Scroll using the CDP mouse wheel event.

##### `touch_tap(tab_id, x: int, y: int) → None`

Simulate a touch tap (for mobile-emulated pages).

##### `drag(tab_id, start_x, start_y, end_x, end_y, steps=10) → None`

Drag from one point to another with smooth interpolation.

##### `select_all(tab_id) → None`

Send Ctrl+A.

##### `copy(tab_id) → None`

Send Ctrl+C.

##### `paste(tab_id) → None`

Send Ctrl+V.

---

#### DOM Queries

##### `query(tab_id, selector: str) → int`

Get a single DOM node ID by CSS selector.

**Returns:** `int` — The CDP node ID.

##### `query_all(tab_id, selector: str) → list[int]`

Get all matching DOM node IDs.

##### `outer_html(tab_id, node_id_or_selector: int | str) → str`

Get the outer HTML of a node. Accepts either a node ID or CSS selector string.

##### `set_attribute(tab_id, node_id_or_selector, name: str, value: str) → None`

Set an attribute on a DOM node.

##### `remove_node(tab_id, node_id_or_selector) → None`

Remove a DOM node from the tree.

##### `get_text(tab_id, selector: str = None) → str`

Get text content of the entire page or a specific element.

---

#### Network

##### `enable_network(tab_id) → None`

Enable the Network domain for request/response monitoring.

##### `intercept_requests(tab_id, url_patterns: list = None) → None`

Enable request interception with optional URL pattern filters.

##### `on_request(tab_id, callback) → None`

Register a listener for outgoing network requests.

##### `on_response(tab_id, callback) → None`

Register a listener for network responses.

##### `block_urls(tab_id, urls: list) → None`

Block requests matching URL patterns.

**Example:**
```python
chrome.block_urls(tab, ['*.ads.*', '*/tracking/*', '*/analytics/*'])
```

##### `get_response_body(tab_id, request_id: str) → str`

Get the response body for a captured network request.

##### `set_extra_headers(tab_id, headers: dict) → None`

Set custom HTTP headers for all subsequent requests.

##### `set_user_agent(tab_id, user_agent: str) → None`

Override the browser's User-Agent string.

##### `clear_cache(tab_id) → None`

Clear the browser cache.

---

#### Cookies & Storage

##### `get_cookies(tab_id=None, urls=None) → list`

Get browser cookies, optionally filtered by URLs.

##### `set_cookie(tab_id, name, value, domain=None, path='/', secure=False, http_only=False, same_site='Lax', expires=None) → dict`

Set a cookie.

##### `delete_cookies(tab_id, name, domain=None, url=None) → None`

Delete a specific cookie.

##### `clear_cookies(tab_id) → None`

Clear all browser cookies.

##### `get_local_storage(tab_id, origin=None) → dict`

Get all localStorage key-value pairs.

##### `set_local_storage(tab_id, key: str, value: str) → None`

Set a localStorage item.

##### `get_session_storage(tab_id) → dict`

Get all sessionStorage key-value pairs.

---

#### Console & Errors

##### `on_console(tab_id, callback) → None`

Listen for `console.log`, `console.warn`, `console.error`, etc.

**Callback receives:** `dict` with `type`, `text`, and `args` fields.

##### `on_exception(tab_id, callback) → None`

Listen for unhandled JavaScript exceptions.

---

#### Performance

##### `performance_metrics(tab_id) → dict`

Get Chrome's built-in performance metrics (e.g., `JSHeapUsedSize`, `Frames`, `Documents`).

##### `start_trace(tab_id, categories=None) → None`

Start a Chrome performance trace.

##### `stop_trace(tab_id) → list`

Stop the trace and return collected trace events.

---

#### Device Emulation

##### `emulate_device(tab_id, device_name: str) → None`

Emulate a mobile device using built-in presets.

**Available presets:** `iPhone 12`, `iPhone 14 Pro`, `Pixel 7`, `iPad Air`, `Desktop 1080p`, `Desktop 1440p`

##### `set_viewport(tab_id, width=1920, height=1080, device_scale_factor=1, mobile=False) → None`

Set custom viewport dimensions.

##### `clear_device_override(tab_id) → None`

Remove device emulation overrides.

##### `throttle_network(tab_id, offline=False, latency=0, download_throughput=-1, upload_throughput=-1) → None`

Simulate network conditions (slow 3G, offline, etc.).

##### `set_geolocation(tab_id, latitude, longitude, accuracy=1) → None`

Override the browser's geolocation.

##### `set_timezone(tab_id, timezone_id: str) → None`

Override the timezone (e.g., `"America/New_York"`).

##### `set_locale(tab_id, locale: str) → None`

Override the browser locale.

##### `dark_mode(tab_id, enabled=True) → None`

Toggle `prefers-color-scheme: dark` media feature.

---

#### Downloads

##### `set_download_path(tab_id, path: str) → None`

Set the download directory for files downloaded via Chrome.

---

#### CSS

##### `get_computed_style(tab_id, selector: str) → dict`

Get the computed CSS styles for an element.

##### `inject_css(tab_id, css: str) → dict`

Inject a CSS rule into the page.

---

#### Accessibility

##### `accessibility_tree(tab_id) → list`

Get the full accessibility tree (AX tree) for the page.

---

#### Security

##### `security_info(tab_id) → dict`

Get security information (protocol, host, secure status).

---

#### Utility

##### `wait(seconds: float) → None`

Simple sleep utility.

##### `wait_for_selector(tab_id, selector: str, timeout=30) → bool`

Block until a CSS selector matches an element in the DOM.

**Raises:** `CDPError` — On timeout.

##### `wait_for_text(tab_id, text: str, timeout=30) → bool`

Block until specific text appears on the page.

##### `wait_for_url(tab_id, url_pattern: str, timeout=30) → str`

Block until the tab's URL matches a regex pattern.

##### `fill_form(tab_id, data: dict) → None`

Fill form fields. Keys are CSS selectors, values are the text to enter.

**Example:**
```python
chrome.fill_form(tab, {
    '#username': 'john@example.com',
    '#password': 'secret123',
})
```

##### `get_page_info(tab_id) → dict`

Get comprehensive page info: title, URL, readyState, link/image/form/script counts, viewport dimensions, and load performance timing.

##### `extract_links(tab_id, filter_pattern=None) → list`

Extract all anchor links from the page, optionally filtered by regex.

##### `extract_meta(tab_id) → list`

Extract all `<meta>` tags from the page.

##### `extract_tables(tab_id, selector='table') → list`

Extract tabular data (headers + rows) from HTML tables.

---

#### Advanced

##### `raw(tab_id, method: str, params=None) → dict`

Send any raw CDP protocol command.

**Example:**
```python
result = chrome.raw(tab, 'Runtime.getHeapUsage')
```

---

#### Cleanup

##### `close() → None`

Disconnect all tab WebSocket connections. Also available via context manager (`with CDP(...) as chrome:`).

---

### Data Classes / Device Presets

#### `DEVICE_PRESETS`

| Device | Width | Height | DPR | Mobile |
|--------|-------|--------|-----|--------|
| `iphone 12` | 390 | 844 | 3.0 | Yes |
| `iphone 14 pro` | 393 | 852 | 3.0 | Yes |
| `pixel 7` | 412 | 915 | 2.625 | Yes |
| `ipad air` | 820 | 1180 | 2.0 | Yes |
| `desktop 1080p` | 1920 | 1080 | 1.0 | No |
| `desktop 1440p` | 2560 | 1440 | 1.0 | No |

---

## Code Examples

### Example 1: Web Scraping — Extract Search Results

```python
from tools.chrome_bridge.cdp import CDP

chrome = CDP(port=9222)
tabs = chrome.tabs()
tab = tabs[0]

# Navigate to a search engine
chrome.navigate(tab, "https://www.google.com")
chrome.wait_for_selector(tab, 'textarea[name="q"]')

# Type a query and submit
chrome.click_selector(tab, 'textarea[name="q"]')
chrome.type_text(tab, "Chrome DevTools Protocol automation")
chrome.press_key(tab, "Enter")

# Wait for results and extract links
chrome.wait_for_selector(tab, "#search")
links = chrome.extract_links(tab, filter_pattern=r"https?://(?!www\.google)")
for link in links[:5]:
    print(f"{link['text'][:60]} → {link['href']}")

# Output:
# Chrome DevTools Protocol - Chrome for Developers → https://developer.chrome.com/...
# Getting Started with CDP → https://chromedevtools.github.io/...
```

### Example 2: JavaScript Evaluation and DOM Manipulation

```python
from tools.chrome_bridge.cdp import CDP

chrome = CDP(port=9222)
tabs = chrome.tabs()
tab = tabs[0]

chrome.navigate(tab, "https://example.com")

# Evaluate JavaScript expressions
title = chrome.eval(tab, "document.title")
print(f"Page title: {title}")
# Output: Page title: Example Domain

# Count all elements on the page
count = chrome.eval(tab, "document.querySelectorAll('*').length")
print(f"Total elements: {count}")
# Output: Total elements: 14

# Execute async JavaScript (fetch API)
response = chrome.eval(tab,
    'fetch("https://jsonplaceholder.typicode.com/todos/1").then(r=>r.json())',
    await_promise=True
)
print(f"API response: {response}")
# Output: API response: {'userId': 1, 'id': 1, 'title': '...', 'completed': False}

# Modify the DOM
chrome.eval(tab, """
    document.querySelector('h1').innerText = 'Modified by CDP!';
    document.querySelector('h1').style.color = 'red';
""")

# Read back the modification
html = chrome.outer_html(tab, "h1")
print(f"Modified HTML: {html}")
# Output: Modified HTML: <h1 style="color: red;">Modified by CDP!</h1>
```

### Example 3: Full-Page Screenshot with Device Emulation

```python
from tools.chrome_bridge.cdp import CDP

chrome = CDP(port=9222)
tab = chrome.new_tab("https://example.com")

# Emulate iPhone 12
chrome.emulate_device(tab, "iPhone 12")
chrome.navigate(tab, "https://example.com")

# Take a mobile screenshot
mobile_png = chrome.screenshot(tab, file_path="mobile_view.png")
print(f"Mobile screenshot: {len(mobile_png)} bytes")
# Output: Mobile screenshot: 28456 bytes

# Switch to desktop and take full-page screenshot
chrome.clear_device_override(tab)
chrome.set_viewport(tab, width=1920, height=1080)
chrome.navigate(tab, "https://example.com")
full_png = chrome.screenshot(tab, full_page=True, file_path="full_page.png")
print(f"Full-page screenshot: {len(full_png)} bytes")
# Output: Full-page screenshot: 45892 bytes

# Generate a PDF
pdf_data = chrome.pdf(tab, file_path="page.pdf", landscape=True)
print(f"PDF generated: {len(pdf_data)} bytes")
# Output: PDF generated: 12340 bytes

chrome.close_tab(tab)
```

### Example 4: Network Interception and Monitoring

```python
from tools.chrome_bridge.cdp import CDP

chrome = CDP(port=9222)
tab = chrome.new_tab()

# Block ads and tracking
chrome.block_urls(tab, ['*.doubleclick.net*', '*/analytics/*', '*.facebook.com/tr*'])

# Monitor network requests
requests_log = []
def on_request(method, params):
    url = params.get('request', {}).get('url', '')
    requests_log.append(url)

chrome.on_request(tab, on_request)

# Set custom headers
chrome.set_extra_headers(tab, {
    'X-Custom-Header': 'CDP-Automation',
    'Accept-Language': 'en-US,en;q=0.9',
})

# Navigate and observe
chrome.navigate(tab, "https://example.com")
print(f"Network requests captured: {len(requests_log)}")
# Output: Network requests captured: 3

# Get cookies
cookies = chrome.get_cookies(tab)
print(f"Cookies: {len(cookies)}")

chrome.close_tab(tab)
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         CDP Controller                        │
│                                                              │
│  ┌──────────────┐    ┌──────────────────────────────────┐    │
│  │   HTTP API   │    │        WebSocket Pool             │    │
│  │  /json/*     │    │  ┌────────┐ ┌────────┐ ┌────────┐│    │
│  │  tab mgmt,   │    │  │CDPTab 1│ │CDPTab 2│ │CDPTab N││    │
│  │  discovery   │    │  │  ws:// │ │  ws:// │ │  ws:// ││    │
│  └──────┬───────┘    │  └───┬────┘ └───┬────┘ └───┬────┘│    │
│         │            └──────┼──────────┼──────────┼─────┘    │
│         │                   │          │          │           │
└─────────┼───────────────────┼──────────┼──────────┼──────────┘
          │                   │          │          │
          ▼                   ▼          ▼          ▼
┌──────────────────────────────────────────────────────────────┐
│                Chrome (--remote-debugging-port=9222)          │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐  │
│  │  Page     │  │ Runtime  │  │ Network  │  │  Input      │  │
│  │ navigate, │  │ eval,    │  │ intercept│  │ click, type │  │
│  │ capture   │  │ console  │  │ cookies  │  │ scroll, key │  │
│  └──────────┘  └──────────┘  └──────────┘  └─────────────┘  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐  │
│  │  DOM     │  │Emulation │  │  Tracing │  │Accessibility│  │
│  │ query,   │  │ device,  │  │ profiling│  │  AX tree    │  │
│  │ modify   │  │ viewport │  │          │  │             │  │
│  └──────────┘  └──────────┘  └──────────┘  └─────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

The CDP architecture follows a **direct WebSocket** pattern:

1. **HTTP Discovery Layer** — Tab listing, creation, and closure use Chrome's HTTP JSON API (`/json`, `/json/new`, `/json/close`). This is lightweight and stateless.

2. **WebSocket Command Layer** — Each tab gets its own persistent WebSocket connection (`CDPTab`). Commands are sent as JSON-RPC messages with auto-incrementing IDs. A background listener thread receives responses and event notifications, routing them via `threading.Event` synchronization for request/response pairs and callback lists for subscribed events.

3. **CDP Domain Multiplexing** — Chrome exposes functionality through domains (Page, Runtime, DOM, Network, Input, Emulation, etc.). Each domain must be explicitly enabled before its events can be received. The CDP class provides high-level methods that handle domain enablement automatically.

4. **Auto-Position on Windows** — When launching Chrome, the controller uses Win32 APIs (`EnumWindows`, `GetWindowRect`) to detect existing Chrome windows and computes a non-overlapping position for the new window. This prevents window stacking during multi-instance automation.

**Key design decisions:**
- **Thread-per-tab listener** — Each CDPTab runs a daemon thread for WebSocket message dispatch, enabling concurrent multi-tab automation.
- **Synchronous API with async internals** — Public methods block until the CDP response arrives, but the WebSocket listener runs asynchronously. This provides a simple API while maintaining responsiveness.
- **Tab-ID polymorphism** — All methods accept either a tab ID string or a full tab info dict, reducing boilerplate.

---

## Performance

### Benchmarks

| Operation | Time | Conditions |
|-----------|------|------------|
| `eval()` (simple expression) | ~0.5ms | Local Chrome, warm connection |
| `navigate()` (without wait) | ~1ms | Command dispatch only |
| `screenshot()` (viewport) | ~15–30ms | 1920×1080 viewport, PNG |
| `screenshot()` (full page) | ~50–200ms | Depends on page height |
| `click_selector()` | ~2ms | Includes JS eval for coordinates |
| `query_all()` | ~3ms | DOM.getDocument + querySelectorAll |
| Connection establishment | ~50ms | WebSocket handshake per tab |

### Complexity

| Operation | Time | Space |
|-----------|------|-------|
| `eval()` | O(1) round-trip | O(n) response size |
| `query_all()` | O(n) DOM nodes matched | O(n) node IDs |
| `screenshot()` | O(w×h) pixels | O(w×h×4) PNG bytes |
| `extract_links()` | O(n) anchors | O(n) link objects |

### Optimization Tips

- **Reuse tab connections** — Creating a new `CDPTab` requires a WebSocket handshake. Keep tabs open and reuse them.
- **Batch JavaScript** — Instead of multiple `eval()` calls, combine operations into a single JS expression that returns all needed data.
- **Disable unused domains** — Enabled domains generate events that consume bandwidth. Disable `Network` when not intercepting requests.
- **Use `clip` for partial screenshots** — When you only need a region, specify a `clip` rect to avoid capturing the entire viewport.
- **Prefer `eval()` for data extraction** — Using `eval()` with inline JS is faster than `query()` → `outer_html()` chains because it avoids multiple round-trips.

---

## Troubleshooting / FAQ

### Chrome not connecting

**Symptom:** `CDPError: Cannot connect to Chrome at http://127.0.0.1:9222`

**Cause:** Chrome is not running with the `--remote-debugging-port` flag, or another process is already bound to that port.

**Fix:**
```bash
# Launch Chrome with debugging enabled
chrome.exe --remote-debugging-port=9222 --remote-allow-origins=*
# Or use the built-in launcher:
python -c "from tools.chrome_bridge.cdp import CDP; CDP.launch(port=9222)"
```

### WebSocket package missing

**Symptom:** `CDPError: websocket-client package required`

**Cause:** The `websocket-client` package is not installed.

**Fix:**
```bash
pip install websocket-client
```

### CDP v146+ user data directory error

**Symptom:** Chrome refuses to bind the debugging port when using the default user data directory.

**Cause:** Chrome v146+ blocks `--remote-debugging-port` with the default profile directory for security.

**Fix:**
```python
# The CDP.launch() method auto-generates a non-default user data dir:
chrome = CDP.launch()  # Automatically uses data/chrome_cdp_userdata/

# Or specify your own:
chrome = CDP.launch(user_data_dir="C:/my_chrome_data")
```

### FAQ

**Q: Does CDP move my mouse cursor?**
A: No. All input events (click, type, scroll) are dispatched via the CDP Input domain directly into Chrome's rendering engine. Your physical mouse and keyboard are never touched.

**Q: Can I automate multiple tabs simultaneously?**
A: Yes. Each tab has its own WebSocket connection and listener thread. You can issue commands to different tabs from different Python threads.

**Q: Does this work with headless Chrome?**
A: Yes. Pass `headless=True` to `CDP.launch()`. All operations work identically in headless mode except window positioning.

**Q: What's the difference between `eval()` and `eval_function()`?**
A: `eval()` evaluates a raw JavaScript expression string. `eval_function()` calls a function declaration with explicit arguments, which is safer for parameterized calls.

---

## Convenience Functions

Two module-level functions provide simplified entry points:

```python
from tools.chrome_bridge.cdp import connect, launch

# Auto-discover and connect to running Chrome
chrome = connect()

# Launch Chrome and navigate to a URL
chrome = launch("https://example.com", headless=True)
```

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-23 | Initial research report from source analysis |

---

*Generated from ScreenMemory research toolkit. See [TOOL_INVENTORY.md](TOOL_INVENTORY.md) for the full catalog.*
