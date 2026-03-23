# GodMode: Semantic Browser Automation Without Pixels
<!-- signed: gamma -->

> **Version:** 1.0 | **Python:** ≥3.9 | **License:** MIT

## Summary

GodMode is a structural perception engine for browser automation that navigates web pages using semantic meaning rather than CSS selectors or pixel coordinates. It builds an 8-layer perception stack — from raw accessibility trees through element embeddings to spatial reasoning — to compress a 100,000+ token DOM into ~1,400 tokens of actionable signal. All interaction is zero-mouse, zero-keyboard: Chrome's DevTools Protocol (CDP) Input domain handles clicks and keystrokes at the renderer level. GodMode can find a "shopping cart" button on any e-commerce site without knowing the HTML structure, detect and dismiss cookie banners automatically, and fill forms by label text instead of CSS selectors.

---

## Requirements

| Requirement | Value |
|-------------|-------|
| Python | ≥ 3.9 |
| OS | Windows (Win32 APIs for desktop scanning) |
| Hardware | CPU-only (no GPU required) |
| Browser | Google Chrome with `--remote-debugging-port` enabled |

### Install

```bash
# Clone the repository
git clone https://github.com/user/screenmemory.git
cd screenmemory

# Install dependencies
pip install -r requirements.txt
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `websocket-client` | ≥1.0 | CDP WebSocket communication |
| `requests` | ≥2.28 | CDP HTTP endpoint discovery |

### Browser Setup

Chrome must be launched with remote debugging enabled:

```bash
# Windows
chrome.exe --remote-debugging-port=9222

# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

---

## Quick Start

```python
from tools.chrome_bridge.god_mode import GodMode

god = GodMode(cdp_port=9222)

# See the current page as structured data
page = god.see(depth='standard')
print(f"Found {len(page.get('actionable_elements', []))} actionable elements")
# Output: Found 23 actionable elements

# Find elements by meaning, not selectors
results = god.find("login button")
for r in results[:3]:
    print(f"  [{r['similarity']:.2f}] {r['role']} \"{r['name']}\"")
# Output:
#   [0.87] button "Log In"
#   [0.72] link "Sign In"
#   [0.61] button "Create Account"
```

---

## API Reference

### `GodMode`

```python
class GodMode:
    """
    Unified semantic browser automation engine combining 8 perception
    layers into a single all-seeing interface.
    """
```

#### Constructor

```python
GodMode(cdp_port: int = 9222)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cdp_port` | `int` | `9222` | Chrome DevTools Protocol port |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `cdp` | `CDP` | Lazy-initialized CDP connection |
| `connected` | `bool` | Whether Chrome is reachable via CDP |

---

#### Methods

##### `see(tab_id: str = None, depth: str = 'standard') → Dict`

Primary perception method. Returns a structured representation of the current page at the requested depth level.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tab_id` | `str` | `None` | Target tab ID. Uses active tab if `None`. |
| `depth` | `str` | `'standard'` | Perception depth: `'minimal'`, `'standard'`, `'deep'`, or `'god'` |

**Depth Levels:**

| Depth | Layers | Time | Data Included |
|-------|--------|------|---------------|
| `minimal` | 1 | ~50ms | Accessibility tree only |
| `standard` | 1-3 | ~200ms | + geometry + occlusion |
| `deep` | 1-4 | ~500ms | + graph topology |
| `god` | 1-5 | ~800ms | + embeddings + spatial + forms + nav + CTA |

**Returns:** `Dict` with keys varying by depth:
- `tab_id`, `timestamp`, `perception_time_ms` (always)
- `accessibility_tree`, `actionable_elements` (minimal+)
- `viewport`, `elements`, `dom_element_count`, `occlusion` (standard+)
- `topology` (deep+)
- `page_type`, `layout`, `grid_analysis`, `form_groups`, `nav_bars` (god — always present)
- `primary_cta`, `overlays` (god — **conditional**, only present when detected; use `.get()` or `in` to check)

**Example:**
```python
page = god.see(depth='god')
print(f"Page type: {page['page_type']}")
print(f"Primary CTA: {page.get('primary_cta', {}).get('name', 'none')}")
print(f"Has modal: {page['occlusion']['has_modal']}")
# Output:
# Page type: login
# Primary CTA: Sign In
# Has modal: False
```

---

##### `scene(tab_id: str = None, max_elements: int = 40) → str`

Generate an ultra-compact LLM-ready page description. Replaces a 100k-token DOM dump with ~1,400 tokens of pure signal.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tab_id` | `str` | `None` | Target tab ID |
| `max_elements` | `int` | `40` | Maximum elements to include |

**Returns:** `str` — Human-readable scene description with element references, roles, names, and normalized coordinates.

**Example:**
```python
print(god.scene())
# Output:
# ## Page State
# Type: search
# Viewport: 1920x1080
# DOM Elements: 847
# Actionable: 23
#
# ## Action Space (normalized 0-1000 grid)
# [0] textbox "Search" @(350,45 300x35) ★ (search)
# [1] button "Search" @(660,45 80x35) (submit)
# [2] link "Images" @(450,12 60x20)
# ...
```

---

##### `find(concept: str, tab_id: str = None) → List[Dict]`

Find elements by semantic concept using hash-based vector embeddings. Works across any website — no CSS selectors needed.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `concept` | `str` | — | Semantic description (e.g., `"shopping cart"`, `"login button"`) |
| `tab_id` | `str` | `None` | Target tab ID |

**Returns:** `List[Dict]` — Elements sorted by similarity score (highest first). Each dict includes `similarity` (0.0–1.0) plus all geometric properties.

**Example:**
```python
results = god.find("shopping cart")
for r in results[:3]:
    print(f"  sim={r['similarity']:.2f}  {r['role']} \"{r['name']}\" at ({r['x']},{r['y']})")
# Output:
#   sim=0.89  link "Cart (3 items)" at (1450, 15)
#   sim=0.71  button "Add to Cart" at (800, 450)
#   sim=0.54  link "View Basket" at (1460, 45)
```

---

##### `click(target, tab_id: str = None) → bool`

Click on a target via CDP Input domain — zero physical mouse.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | `str \| tuple \| dict` | — | Semantic concept, `(x, y)` coordinates, or element dict |
| `tab_id` | `str` | `None` | Target tab ID |

**Returns:** `bool` — Whether the click succeeded.

**Example:**
```python
# By semantic concept
god.click("Submit button")

# By coordinates
god.click((500, 300))

# By element dict from find()
results = god.find("login")
god.click(results[0])
```

---

##### `find_and_fill(label: str, value: str, tab_id: str = None) → bool`

Find an input field by its associated label text and fill it. Uses spatial reasoning to pair labels with their nearest input fields.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `label` | `str` | — | Label text near the target input |
| `value` | `str` | — | Text to enter |
| `tab_id` | `str` | `None` | Target tab ID |

**Returns:** `bool` — Whether the field was found and filled.

**Example:**
```python
god.find_and_fill("Email", "user@example.com")
god.find_and_fill("Password", "secure123")
```

---

##### `fill_form(fields: Dict[str, str], tab_id: str = None) → Dict`

Fill multiple form fields by label text in one call.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fields` | `Dict[str, str]` | — | Mapping of label text → value |
| `tab_id` | `str` | `None` | Target tab ID |

**Returns:** `Dict` — Per-field results: `'filled'` or `'not found'`.

**Example:**
```python
results = god.fill_form({
    "First Name": "Jane",
    "Last Name": "Doe",
    "Email": "jane@example.com",
    "Phone": "+1-555-0123",
})
print(results)
# Output: {'First Name': 'filled', 'Last Name': 'filled',
#          'Email': 'filled', 'Phone': 'filled'}
```

---

##### `navigate(url: str, tab_id: str = None)`

Navigate to a URL via CDP — no address bar interaction.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | — | Target URL |
| `tab_id` | `str` | `None` | Target tab ID |

**Example:**
```python
god.navigate("https://example.com")
```

---

##### `type_text(text: str, tab_id: str = None)`

Type text into the currently focused element via CDP.

---

##### `press(key: str, tab_id: str = None)`

Press a keyboard key via CDP (e.g., `'Enter'`, `'Escape'`, `'Tab'`).

---

##### `scroll(direction: str = 'down', amount: int = 300, tab_id: str = None)`

Scroll the page via CDP — no mouse wheel.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `direction` | `str` | `'down'` | `'down'` or `'up'` |
| `amount` | `int` | `300` | Scroll distance in pixels |

---

##### `dismiss_overlays(tab_id: str = None) → int`

Automatically detect and dismiss modals, cookie banners, and popups by finding dismiss-like buttons within overlay contexts.

**Returns:** `int` — Number of overlays dismissed.

**Example:**
```python
god.navigate("https://news-site.com")
dismissed = god.dismiss_overlays()
print(f"Dismissed {dismissed} overlay(s)")
# Output: Dismissed 1 overlay(s)
```

---

##### `describe(tab_id: str = None) → str`

Generate a natural language spatial description of the page layout using Visualization-of-Thought principles.

**Returns:** `str` — Multi-line layout description organized by regions (header, navigation, content, sidebar, footer).

---

##### `what_is_at(x: int, y: int, tab_id: str = None) → List[Dict]`

Query what elements exist at specific viewport coordinates, sorted by z-index (topmost first).

---

##### `wait_for(text: str = None, selector: str = None, timeout: int = 30, tab_id: str = None) → bool`

Wait for text or an element to appear on the page.

---

##### `action_space(tab_id: str = None) → str`

Generate compact grounded action space as JSON (element refs + normalized coordinates).

---

##### `status() → Dict`

Full diagnostic status: CDP connection, active modules, tab count, monitor count, action history.

---

##### `tabs() → List[Dict]`

List all Chrome tabs.

---

##### `new_tab(url: str = 'about:blank') → str`

Open a new tab. Returns the tab ID.

---

##### `close_tab(tab_id: str = None)`

Close a tab.

---

##### `activate_tab(tab_id: str)`

Bring a tab to the foreground.

---

##### `scan_world(depth: int = 3) → Dict`

Full environment scan combining Win32 windows, UIA accessibility tree, and Chrome DOM into a unified spatial model.

---

##### `eval(js: str, tab_id: str = None)`

Execute arbitrary JavaScript in the browser context.

---

##### `screenshot(filepath: str = None, tab_id: str = None) → bytes`

Take a screenshot via CDP. Optionally save to file.

---

##### `history() → List[Dict]`

Return the last 50 actions performed through GodMode.

---

### Internal Module Classes

GodMode composes 7 internal perception modules, each available as a standalone component:

| Module | Class | Role |
|--------|-------|------|
| Layer 1 | `AccessibilityTreeParser` | Parses Chrome AOM into clean semantic nodes |
| Layer 2 | `SemanticGeometryEngine` | Computes bounding boxes, normalized coords, prominence scores |
| Layer 3 | `OcclusionResolver` | Resolves z-index stacking, visibility, modal detection |
| Layer 4 | `ElementEmbedding` | Hash-based vector embeddings for semantic similarity |
| Layer 5 | `PageTopologyGraph` | GNN-inspired relational graph of element layout |
| Layer 6 | `ActionSpaceOptimizer` | Compresses 100k tokens → ~1.4k token scene |
| Layer 7 | `SpatialReasoner` | Gestalt perception: direction queries, layout regions |

---

## Code Examples

### Example 1: Navigate and Search

```python
from tools.chrome_bridge.god_mode import GodMode

god = GodMode(cdp_port=9222)

# Navigate to a search engine
god.navigate("https://www.google.com")

# Find the search box by meaning
god.find_and_fill("Search", "Python browser automation")

# Click the search button
god.click("Google Search")

# Wait for results
god.wait_for(text="results", timeout=10)

# See what's on the results page
page = god.see(depth='god')
print(f"Page type: {page['page_type']}")
print(f"Found {len(page.get('elements', []))} interactive elements")
# Output:
# Page type: listing
# Found 42 interactive elements
```

### Example 2: Detect and Dismiss Overlays

```python
from tools.chrome_bridge.god_mode import GodMode

god = GodMode(cdp_port=9222)
god.navigate("https://example-news-site.com")

# Check for overlays (cookie banners, modals)
page = god.see(depth='god')
if page.get('overlays'):
    for overlay in page['overlays']:
        print(f"Overlay: {overlay['tag']} (z={overlay['z']}, reason={overlay['reason']})")
        print(f"  Text: \"{overlay.get('text', '')[:60]}\"")
    
    # Automatically dismiss all overlays
    dismissed = god.dismiss_overlays()
    print(f"Dismissed {dismissed} overlay(s)")
# Output:
# Overlay: div (z=9999, reason=high-z-fixed)
#   Text: "We use cookies to improve your experience. Accept All"
# Dismissed 1 overlay(s)
```

### Example 3: Fill a Multi-Field Form

```python
from tools.chrome_bridge.god_mode import GodMode

god = GodMode(cdp_port=9222)
god.navigate("https://example.com/register")
god.wait_for(text="Create Account", timeout=10)

# Fill the entire form by label text
results = god.fill_form({
    "First Name": "Jane",
    "Last Name": "Doe",
    "Email Address": "jane.doe@example.com",
    "Password": "SecureP@ss123",
    "Confirm Password": "SecureP@ss123",
})

# Check which fields were filled
for label, status in results.items():
    print(f"  {label}: {status}")
# Output:
#   First Name: filled
#   Last Name: filled
#   Email Address: filled
#   Password: filled
#   Confirm Password: filled

# Submit the form
god.click("Create Account")
```

### Example 4: Semantic Element Discovery

```python
from tools.chrome_bridge.god_mode import GodMode

god = GodMode(cdp_port=9222)
god.navigate("https://example-shop.com/products")

# Find elements by high-level concept
cart_elements = god.find("shopping cart")
print("Cart-related elements:")
for el in cart_elements[:5]:
    print(f"  [{el['similarity']:.2f}] {el['role']} \"{el['name']}\" at ({el['x']},{el['y']})")

# Identify the primary call-to-action
god._ensure_modules()
cta = god.geometry.find_primary_cta(god._get_active_tab())
if cta:
    print(f"\nPrimary CTA: \"{cta['name']}\" ({cta['role']})")
# Output:
# Cart-related elements:
#   [0.91] link "Cart (2)" at (1400, 18)
#   [0.78] button "Add to Cart" at (820, 520)
#   [0.65] button "Buy Now" at (820, 580)
#
# Primary CTA: "Add to Cart" (button)
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          GodMode Controller                         │
│                     (Unified Coordination Layer)                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Layer 7: SpatialReasoner          Layer 6: ActionSpaceOptimizer    │
│  ┌──────────────────────┐          ┌──────────────────────────┐     │
│  │ • Direction queries   │          │ • 100k→1.4k compression  │     │
│  │ • Layout regions      │          │ • Prominence sorting     │     │
│  │ • Row/column detect   │          │ • LLM prompt generation  │     │
│  │ • Label→input pairing │          │ • Page type classifying  │     │
│  └──────────────────────┘          └──────────────────────────┘     │
│                                                                     │
│  Layer 5: PageTopologyGraph        Layer 4: ElementEmbedding        │
│  ┌──────────────────────┐          ┌──────────────────────────┐     │
│  │ • GNN-inspired graph  │          │ • Screen2Vec hash vectors│     │
│  │ • Alignment edges     │          │ • Concept identification │     │
│  │ • Proximity edges     │          │ • Cosine similarity      │     │
│  │ • Navigation bars     │          │ • Page type heuristics   │     │
│  │ • Grid patterns       │          │ • 96-D embeddings        │     │
│  │ • Form field groups   │          │   (64 lexical + 24 class │     │
│  │ • Message passing     │          │    + 8 spatial)           │     │
│  └──────────────────────┘          └──────────────────────────┘     │
│                                                                     │
│  Layer 3: OcclusionResolver        Layer 2: SemanticGeometryEngine  │
│  ┌──────────────────────┐          ┌──────────────────────────┐     │
│  │ • elementFromPoint    │          │ • Bounding box extraction│     │
│  │ • 5-point visibility  │          │ • Normalized 0-1000 grid │     │
│  │ • Modal/dialog detect │          │ • Prominence scoring     │     │
│  │ • Overlay detection   │          │ • Spatial clustering     │     │
│  │ • z-index stacking    │          │ • Region filtering       │     │
│  └──────────────────────┘          └──────────────────────────┘     │
│                                                                     │
│  Layer 1: AccessibilityTreeParser                                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ • Chrome Computed AOM (Accessibility Object Model)           │   │
│  │ • Semantic role classification (actionable vs structural)    │   │
│  │ • Noise filtering (skips 'none', 'presentation', 'generic') │   │
│  │ • Compact YAML-like representation for LLM consumption      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Foundation: Chrome DevTools Protocol (CDP)                         │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ • WebSocket connection to chrome://devtools                  │   │
│  │ • Input.dispatchMouseEvent (zero physical mouse)             │   │
│  │ • Input.dispatchKeyEvent (zero physical keyboard)            │   │
│  │ • Accessibility.getFullAXTree                                │   │
│  │ • Runtime.evaluate (JS injection)                            │   │
│  │ • Page.captureScreenshot                                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Accessibility Tree Parser** fetches Chrome's computed AOM, strips non-semantic noise, and produces a list of meaningful nodes with roles, names, and states.

2. **Semantic Geometry Engine** injects JavaScript to extract bounding boxes, normalized coordinates (0–1000 grid), z-indices, font metrics, and visual prominence scores for all interactable elements — entirely from computed CSS, no screenshots.

3. **Occlusion Resolver** uses `document.elementFromPoint()` at 5 sample points per element to determine true visibility, detect modals/dialogs, and compute visibility ratios. Elements with <40% visibility are marked non-interactable.

4. **Element Embeddings** generate 96-dimensional vectors (64 lexical + 24 role + 8 spatial) using character trigram hashing and one-hot role encoding. This enables semantic similarity search across any website without ML models.

5. **Page Topology Graph** builds a relational graph with alignment, proximity, and size-parity edges. GNN-inspired message passing enriches nodes with neighbor context. Pattern detection finds navigation bars, grids, and form groups.

6. **Action Space Optimizer** combines all layers, filters by occlusion, sorts by prominence, and compresses to a ~1,400-token JSON scene — the LLM-ready output.

7. **Spatial Reasoner** answers directional queries ("what's below the search box?"), classifies layout regions (header, sidebar, content, footer), and pairs labels with input fields via proximity analysis.

8. **GodMode Controller** exposes all layers through a unified API, manages lazy initialization, tab state, and action history.

---

## Performance

### Benchmarks

| Operation | Typical Time | Conditions |
|-----------|-------------|------------|
| `see(depth='minimal')` | ~50ms | Accessibility tree only |
| `see(depth='standard')` | ~200ms | AOM + geometry + occlusion |
| `see(depth='deep')` | ~500ms | + topology graph |
| `see(depth='god')` | ~800ms | Full stack including embeddings |
| `scene()` | ~250ms | Optimized LLM-ready output |
| `find(concept)` | ~300ms | Geometry extract + embedding search |
| `click(concept)` | ~350ms | find + CDP click dispatch |
| `fill_form(5 fields)` | ~2s | 5× (spatial search + click + type) |
| `dismiss_overlays()` | ~500ms | Overlay detect + dismiss attempt |

### Token Compression

| Source | Tokens | Description |
|--------|--------|-------------|
| Raw DOM | ~100,000 | Full `document.innerHTML` |
| Accessibility Tree | ~5,000 | Chrome AOM text dump |
| `scene()` output | ~1,400 | Optimized action space |
| Compression ratio | **~70:1** | From raw DOM to scene |

### Optimization Tips

- Use `depth='minimal'` for quick checks where you only need element names/roles
- Use `depth='standard'` for most automation tasks (geometry + occlusion is sufficient)
- Reserve `depth='god'` for pages where you need form detection or page classification
- Cache `find()` results if making multiple clicks on the same page without navigation
- The Geometry Engine caches results for 5 seconds (`_cache_ttl`)

---

## Troubleshooting / FAQ

### Chrome not connecting

**Symptom:** `god.connected` returns `False`, all operations fail.

**Cause:** Chrome was not launched with remote debugging, or a different port was used.

**Fix:**
```bash
# Kill existing Chrome instances first
$ taskkill /F /IM chrome.exe
# Relaunch with debugging
$ chrome.exe --remote-debugging-port=9222
```

### No elements found on page

**Symptom:** `god.find("button")` returns an empty list.

**Cause:** The page may still be loading, or it uses a Shadow DOM that the geometry extraction JS cannot traverse.

**Fix:**
```python
# Wait for the page to settle
god.wait_for(text="expected content", timeout=15)

# Then try again
results = god.find("button")
```

### Clicks not working on overlapping elements

**Symptom:** `god.click()` returns `True` but the wrong element responds.

**Cause:** A higher z-index element (modal, overlay, tooltip) is intercepting the click.

**Fix:**
```python
# Dismiss overlays first
god.dismiss_overlays()

# Or check occlusion status
page = god.see(depth='standard')
if page['occlusion']['has_modal']:
    print("Modal detected — dismiss it before clicking")
```

### FAQ

**Q: Does GodMode work with websites that use Shadow DOM?**
A: The Accessibility Tree Parser works with Shadow DOM because Chrome's AOM computes through shadow boundaries. However, the Geometry Engine's JavaScript injection may miss elements inside closed Shadow DOMs. For those cases, use the accessibility tree path (`depth='minimal'`).

**Q: Can GodMode interact with iframes?**
A: GodMode works within the main frame by default. To interact with iframe content, use `god.eval()` to inject JavaScript into the iframe context, or use the CDP's `Target.attachToTarget` method to create a separate session for the iframe's tab target.

**Q: How does semantic search work without an ML model?**
A: GodMode uses character trigram hashing (MD5-based) and word-level hashing to generate deterministic 64-dimensional vectors. These are compared via cosine similarity. The approach trades accuracy for zero dependencies — it works for common UI concepts (login, cart, search, submit) but may miss highly domain-specific terminology.

**Q: What's the difference between `find()` and CSS selectors?**
A: CSS selectors are brittle — they break when class names change. `find()` uses semantic meaning (the visible text and role of elements) to locate targets. It works across different websites with different HTML structures. A `find("login")` call works on sites that use "Log In", "Sign In", "Log in to your account", etc.

---

## CLI Interface

GodMode includes a command-line interface:

```bash
$ python tools/chrome_bridge/god_mode.py status
$ python tools/chrome_bridge/god_mode.py see --depth god --json
$ python tools/chrome_bridge/god_mode.py find "search box"
$ python tools/chrome_bridge/god_mode.py click "Submit"
$ python tools/chrome_bridge/god_mode.py scene
$ python tools/chrome_bridge/god_mode.py describe
$ python tools/chrome_bridge/god_mode.py tabs
$ python tools/chrome_bridge/god_mode.py overlays
$ python tools/chrome_bridge/god_mode.py graph
$ python tools/chrome_bridge/god_mode.py page-type
$ python tools/chrome_bridge/god_mode.py a11y
$ python tools/chrome_bridge/god_mode.py windows
$ python tools/chrome_bridge/god_mode.py monitors
```

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-23 | Initial public documentation |

---

*Generated from ScreenMemory research toolkit. See [TOOL_INVENTORY.md](TOOL_INVENTORY.md) for the full catalog.*
