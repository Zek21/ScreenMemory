# Chrome Bridge vs Playwright — Why Chrome Bridge Replaces Playwright

> **TL;DR**: Chrome Bridge is ScreenMemory's native browser automation stack.
> It operates on your *real* Chrome session with full context (cookies, sessions, extensions, profiles).
> Playwright spawns isolated throwaway browsers with none of that.
> **Always use Chrome Bridge. Playwright is the last resort for non-Chrome sites only.**

---

## What Is Chrome Bridge?

Chrome Bridge is a 3-layer browser automation system built into ScreenMemory:

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: GOD MODE (god_mode.py)                             │
│  Structural perception engine — zero-pixel navigation        │
│  8 modules: A11y parser, geometry, occlusion, embeddings,    │
│  topology graph, action optimizer, spatial reasoner, control  │
│  Reduces 100k DOM tokens → ~1400 semantic tokens for LLM     │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: Bridge Client (bridge.py) + Server (server.py)     │
│  265+ commands over WebSocket hub (port 7777)                │
│  Smart finding, workflows, stealth, session recording,       │
│  visual regression, event streaming, command pipelines        │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: CDP Direct (cdp.py) + Extension (extension/)       │
│  Chrome DevTools Protocol: ~0.5ms latency (direct)           │
│  Extension: content scripts in all frames, 265+ commands     │
│  CDP: tabs, input, DOM, network, performance, emulation      │
└─────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Chrome Extension** (`extension/`) — Manifest V3 service worker + content scripts injected into all pages. Connects to Hub via WebSocket.
2. **Hub Server** (`server.py`) — WebSocket relay on port 7777. Routes commands between Python clients and Chrome extension(s). Supports multi-profile orchestration.
3. **CDP Direct** (`cdp.py`) — Bypasses the extension entirely for ultra-fast operations (~0.5ms vs ~15ms). Talks directly to Chrome's DevTools Protocol on port 9222.
4. **GOD MODE** (`god_mode.py`) — 8-layer structural perception engine. Parses the accessibility tree, builds spatial graphs, resolves occlusion, and generates compact action spaces. All navigation is by *concept* ("Submit", "Email field"), not by CSS selector.
5. **Perception Engine** (`perception.py`) — Unified spatial graph merging Win32 (z-order, HWND), UIA (accessibility tree), and CDP (DOM + a11y). Zero-pixel, zero-mouse.

---

## Feature Comparison

| Capability | Chrome Bridge | Playwright |
|---|---|---|
| **Uses real Chrome session** | ✅ Your actual browser with cookies, logins, extensions | ❌ Spawns isolated Chromium — no state |
| **Existing login sessions** | ✅ Already authenticated everywhere | ❌ Must re-authenticate from scratch |
| **Chrome profiles** | ✅ Multi-profile support, cookie export/import | ❌ Separate browser contexts only |
| **Extensions active** | ✅ All your extensions work (ad blockers, etc.) | ❌ No extensions in automation browser |
| **Latency** | ✅ CDP direct: ~0.5ms; via Hub: ~15ms | ⚠️ ~5-15ms typical |
| **Command count** | ✅ 265+ commands | ⚠️ ~50 core API methods |
| **Stealth / anti-bot** | ✅ Full suite: canvas/WebGL/audio noise, UA rotation, pre-page injection | ⚠️ Basic stealth via plugins only |
| **Accessibility tree parsing** | ✅ Deep AOM parser, semantic roles, actionable detection | ⚠️ Basic `page.accessibility.snapshot()` |
| **Concept-based navigation** | ✅ `god.click("Submit")` — finds by meaning | ❌ Must use selectors: `page.click('#btn')` |
| **Spatial reasoning** | ✅ Gestalt grouping, alignment detection, form detection | ❌ None |
| **Action space compression** | ✅ 100k DOM tokens → ~1400 tokens for LLM | ❌ No compression |
| **Zero-pixel navigation** | ✅ Pure structural analysis, no screenshots needed | ❌ Relies on DOM selectors |
| **Occlusion resolution** | ✅ Z-index stacking context analysis | ❌ None |
| **Network interception** | ✅ Full: mock, block, capture responses, HAR recording | ✅ Full: route, intercept |
| **Session recording/replay** | ✅ Built-in record/replay | ❌ Codegen only |
| **Visual regression** | ✅ Built-in capture/compare | ❌ Requires external tools |
| **Multi-tab orchestration** | ✅ `multi_eval()`, `multi_close()`, `multi_navigate()` | ⚠️ Manual tab management |
| **Workflow engine** | ✅ JSON-defined multi-step automation | ❌ Code-only |
| **Command pipeline** | ✅ Batch multiple commands in one round-trip | ❌ Sequential only |
| **Real-time event streaming** | ✅ Subscribe to network, console, navigation events | ⚠️ Individual event listeners |
| **Performance tracing** | ✅ Chrome timeline traces | ⚠️ Via CDP only |
| **Device emulation** | ✅ Full: viewport, UA, network throttle, CPU throttle, color simulation | ✅ Full emulation |
| **WebAuthn** | ✅ Virtual authenticator for passkey testing | ✅ Via CDP |
| **IndexedDB / Cache API** | ✅ Direct access | ❌ Via eval only |
| **Service Worker control** | ✅ List, unregister | ❌ Via eval only |
| **Tab groups** | ✅ Create, color, collapse, group by domain | ❌ Not supported |
| **Cross-browser** | ❌ Chrome only | ✅ Chrome, Firefox, WebKit |
| **Headless testing** | ⚠️ Stealth headless via CDP | ✅ Native headless |
| **Test framework** | ❌ Not a test framework | ✅ Test runner, assertions, fixtures |
| **CI/CD integration** | ❌ Designed for live automation | ✅ Designed for CI/CD |

---

## When to Use What

### ✅ ALWAYS Use Chrome Bridge When:
- Automating any task in your real Chrome browser
- You need existing cookies/logins/sessions
- Doing prospecting, lead gen, or data collection
- Browser automation as part of ScreenMemory workflows
- Any task where GOD MODE's structural perception helps
- Multi-profile orchestration
- Stealth browsing (anti-bot evasion)
- AI agent browser interaction (concept-based navigation)

### ⚠️ Playwright Is Acceptable ONLY When:
- The target site is not in Chrome (Firefox/WebKit-only testing)
- Running headless browser tests in CI/CD pipelines
- You explicitly need cross-browser compatibility testing
- Chrome Bridge is completely unavailable (server down, extension not loaded)

---

## Code Examples: Side by Side

### Navigate and Click a Button

**Chrome Bridge — GOD MODE (recommended)**
```python
from god_mode import GodMode

god = GodMode(cdp_port=9222)
god.navigate("https://example.com")
god.click("Submit")  # Finds by concept, not selector
```

**Chrome Bridge — CDP Direct**
```python
from cdp import CDP

chrome = CDP(port=9222)
tab = chrome.new_tab("https://example.com")
chrome.click_selector(tab, '#submit-btn')
```

**Chrome Bridge — Hub Client**
```python
from bridge import Hub

with Hub() as hub:
    chrome = hub.chrome()
    tab = chrome.tabs()[0]
    chrome.navigate(tab['id'], "https://example.com")
    chrome.smart_click(tab['id'], "Submit")
```

**Playwright (verbose, no context)**
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()  # New isolated browser!
    page = browser.new_page()
    page.goto("https://example.com")
    page.click('#submit-btn')  # Must know exact selector
    browser.close()
```

### Fill a Form

**Chrome Bridge — GOD MODE**
```python
god = GodMode(cdp_port=9222)
god.fill_form({
    "Email": "user@example.com",
    "Password": "secret123",
    "Company": "Acme Corp"
})
god.click("Submit")
```

**Playwright**
```python
page.fill('#email', 'user@example.com')
page.fill('#password', 'secret123')
page.fill('#company', 'Acme Corp')
page.click('#submit')
# Must know all selectors in advance. If they change, code breaks.
```

### See What's on the Page (for AI)

**Chrome Bridge — GOD MODE**
```python
god = GodMode(cdp_port=9222)
scene = god.scene()  # ~1400 tokens, ready for LLM
# Returns: structured scene description with actionable elements
```

**Playwright**
```python
# No equivalent. You'd have to:
snapshot = page.accessibility.snapshot()  # Raw, uncompressed
# Then manually parse, filter, compress... GOD MODE does this automatically.
```

### Stealth Browsing

**Chrome Bridge**
```python
from bridge import Hub
hub = Hub()
chrome = hub.chrome()
chrome.stealth_enable(tab_id)       # Basic stealth
chrome.stealth_inject(tab_id)       # Nuclear: pre-page injection
chrome.rotate_ua(tab_id)            # Rotate user agent
status = chrome.stealth_check(tab_id)  # Verify undetected
```

**Playwright**
```python
# No built-in stealth. Must use third-party:
# pip install playwright-stealth
from playwright_stealth import stealth_sync
stealth_sync(page)
# Limited: no canvas/WebGL noise, no pre-page injection
```

### Batch Operations (Pipeline)

**Chrome Bridge**
```python
pipe = chrome.pipeline()
pipe.add('tabs.list')
pipe.add('page.info', tabId=tab_id)
pipe.add('screenshot', tabId=tab_id)
results = pipe.execute()  # All 3 in ONE round-trip
```

**Playwright**
```python
# No pipeline. Must do sequentially:
tabs = context.pages
info = await page.evaluate('({title: document.title, url: location.href})')
screenshot = await page.screenshot()
# 3 separate round-trips
```

---

## Setup Guide

### 1. Load the Chrome Extension

```
1. Open chrome://extensions in Chrome
2. Enable "Developer mode" (top-right toggle)
3. Click "Load unpacked"
4. Select: D:\Prospects\ScreenMemory\tools\chrome_bridge\extension
```

To install across all Chrome profiles:
```powershell
cd D:\Prospects\ScreenMemory\tools\chrome_bridge
powershell -File install_all_profiles.ps1
```

### 2. Start the Hub Server (for Bridge Client mode)

```powershell
cd D:\Prospects\ScreenMemory\tools\chrome_bridge
python server.py                    # Default port 7777
python server.py --port 8888        # Custom port
```

### 3. Use CDP Direct (no hub needed)

Launch Chrome with debug port:
```powershell
chrome.exe --remote-debugging-port=9222
```

Or auto-attach to running Chrome:
```python
from cdp import CDP
chrome = CDP()          # Auto-finds debug port
chrome = CDP(port=9222) # Explicit port
chrome = CDP.launch()   # Launch new Chrome instance with CDP
```

### 4. Use GOD MODE

```python
from god_mode import GodMode
god = GodMode(cdp_port=9222)
print(god.see())   # What's on the page
god.click("Login")  # Click by concept
```

### 5. Verify Installation

```powershell
# Check hub is running
curl http://127.0.0.1:7777/healthz

# Check CDP is accessible
curl http://localhost:9222/json/version

# Run smoke test
cd D:\Prospects\ScreenMemory\tools\chrome_bridge
python demo.py

# Run protocol self-test (no Chrome needed)
python test_bridge.py --synthetic
```

---

## Architecture Decision: Why Not Playwright?

1. **Real vs Fake**: Chrome Bridge operates on your *actual* browser. Playwright creates disposable browsers with no history, no cookies, no extensions. For prospecting and data collection, you need *your* authenticated sessions.

2. **Perception vs Selectors**: GOD MODE understands pages structurally — it can find "the login button" without knowing `#login-btn`. Playwright requires exact selectors that break when sites change.

3. **Speed**: CDP direct is ~0.5ms per operation. Playwright's wire protocol adds overhead.

4. **Integration**: Chrome Bridge is deeply integrated with ScreenMemory's perception stack (OCR, capture, UIA, Win32). Playwright is a standalone tool with no awareness of the surrounding system.

5. **Stealth**: Chrome Bridge has a full anti-detection suite. Playwright is trivially detected by most anti-bot systems.

6. **265 commands**: Chrome Bridge exposes 265+ granular commands. Playwright has ~50 API methods. The depth of control is incomparable.

---

*Chrome Bridge v4.0 — ScreenMemory's definitive browser automation layer.*
