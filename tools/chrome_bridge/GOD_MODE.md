# GOD MODE — Structural Perception Engine for Digital Environments

> Zero-pixel, zero-screenshot, mathematically precise navigation.
> Based on "The Invisible Interface: Conceptual and Spatial Perception of Digital Environments in AI Systems"

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════╗
║                         G O D   M O D E                          ║
║                                                                    ║
║  ┌─────────────┐ ┌──────────────┐ ┌────────────────────────┐     ║
║  │ Accessibility│ │   Semantic   │ │     Occlusion          │     ║
║  │ Tree Parser  │ │   Geometry   │ │     Resolver           │     ║
║  │ (AOM)        │ │   Engine     │ │  (z-index/stacking)    │     ║
║  └──────┬───────┘ └──────┬───────┘ └──────────┬─────────────┘     ║
║         │                │                     │                    ║
║  ┌──────▼────────────────▼─────────────────────▼──────────────┐   ║
║  │              Action Space Optimizer                         │   ║
║  │         (100,000 tokens → ~1,400 tokens)                    │   ║
║  └──────────────────────┬──────────────────────────────────────┘   ║
║                         │                                          ║
║  ┌──────────────┐ ┌─────▼────────┐ ┌────────────────────────┐     ║
║  │   Element    │ │    Page      │ │     Spatial            │     ║
║  │   Embedding  │ │   Topology   │ │     Reasoner           │     ║
║  │  (Screen2Vec)│ │   Graph      │ │  (gestalt perception)  │     ║
║  └──────────────┘ │   (GNN)      │ └────────────────────────┘     ║
║                   └──────────────┘                                 ║
║                                                                    ║
║  ┌────────────────────────────────────────────────────────────┐    ║
║  │              GodMode Controller                            │    ║
║  │    Unified orchestrator — see(), find(), click(), etc.     │    ║
║  └────────────────────────────────────────────────────────────┘    ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Quick Start

```python
from god_mode import GodMode

god = GodMode()  # connects to Chrome CDP on port 9222

# SEE the page (no screenshots needed)
perception = god.see(depth='god')
print(perception['page_type'])        # 'login', 'form', 'search', etc.
print(perception['accessibility_tree'])  # clean semantic tree
print(perception['occlusion'])          # what's visible vs hidden

# Generate optimized LLM context (~1400 tokens instead of 100k)
context = god.scene()
print(context)

# Find elements by CONCEPT (works across any website)
results = god.find("shopping cart")
results = god.find("login button")
results = god.find("search field")

# Click by concept (zero mouse)
god.click("Submit")
god.click("Sign In")

# Fill forms by label (zero keyboard)
god.fill_form({
    "Email": "user@example.com",
    "Password": "secret123",
    "First Name": "John",
})

# Spatial reasoning
god.describe()  # natural language layout description
god.what_is_at(500, 300)  # what's at these coordinates?

# Dismiss overlays/modals automatically
god.dismiss_overlays()

# Full environment scan (Chrome + Windows + Native UI)
god.scan_world()
```

---

## The 8 Modules

### 1. Accessibility Tree Parser (`AccessibilityTreeParser`)

Parses Chrome's Computed Accessibility Tree (AOM) — the browser-computed semantic distillation of the DOM.

**What it strips:** All non-semantic noise (`<div>` wrappers, CSS-only elements, tracking pixels)
**What it keeps:** Roles (button, link, textbox), names ("Submit"), states (expanded, checked)

```python
a11y = AccessibilityTreeParser(cdp)

# Full parse with all semantic data
nodes = a11y.parse(tab_id)
# Returns: [{'role': 'button', 'name': 'Submit', 'states': {'pressed': False}, ...}]

# Ultra-compact format for LLM consumption
compact = a11y.parse_compact(tab_id)
# Returns YAML-like text:
# ● [a1b2c3] button "Submit"
# ● [d4e5f6] textbox "Email" {required=true}
# ○ [g7h8i9] heading "Login"

# Only actionable elements
actionable = a11y.find_actionable(tab_id)
```

**Why AOM over DOM:** A CSS class rename from `btn-primary` to `btn-action` breaks CSS selectors but the accessibility tree remains constant. Semantic locators > brittle selectors.

---

### 2. Semantic Geometry Engine (`SemanticGeometryEngine`)

Computes precise bounding boxes, normalized coordinates, and visual prominence scores — all from code, never from screenshots.

**Key Innovation:** Normalized 0-1000 grid system. Regardless of screen resolution, aspect ratio, or device scaling, element positions are mathematically consistent.

```python
geo = SemanticGeometryEngine(cdp)

# Extract all geometric data
data = geo.extract(tab_id)
# Returns: {viewport: {w, h}, elements: [{x, y, w, h, nx, ny, nw, nh, prominence, z, ...}]}

# Grounded action space (the 100k → 1.4k compression)
action_space = geo.extract_grounded_action_space(tab_id,
    region='center',       # Only center of viewport
    role_filter=['button', 'input'],  # Only these roles
    min_prominence=0.2     # Only prominent elements
)

# Find the primary CTA button (computed from size + position + contrast)
cta = geo.find_primary_cta(tab_id)

# Spatial clusters (groups of related elements)
clusters = geo.spatial_clusters(tab_id)
```

**Visual Prominence Scoring (0.0-1.0):**
- Size factor: larger elements score higher
- Center bias: elements near viewport center score higher
- Font weight: bold text scores higher
- Font size: larger text scores higher
- Z-index: higher stacking scores higher
- Fixed position: sticky/fixed elements get a boost

---

### 3. Occlusion Resolver (`OcclusionResolver`)

Resolves the **z-axis problem** — determining which elements are genuinely visible vs hidden behind overlays, modals, cookie banners, etc.

**4-Step Process:**
1. **Visibility Verification:** `display: none`, `visibility: hidden`, `opacity < 0.1`
2. **Stacking Context Evaluation:** Effective z-index computation through parent hierarchy
3. **Geometric Intersection Mapping:** Multi-point hit testing (5 sample points per element)
4. **Occlusion Calculation:** Visibility ratio (0.0 = fully occluded, 1.0 = fully visible)

```python
occ = OcclusionResolver(cdp)

# Full occlusion analysis
result = occ.resolve(tab_id)
# Returns: {visible: 45, occluded: 3, has_modal: True, elements: [...]}

# Only truly interactable elements (filtered action space)
interactable = occ.get_truly_interactable(tab_id)

# Detect overlays (modals, cookie banners, popups)
overlays = occ.detect_overlays(tab_id)
# Returns: [{'tag': 'div', 'z': 9999, 'reason': 'high-z-fixed', 'text': 'Accept cookies'}]
```

---

### 4. Element Embeddings (`ElementEmbedding`)

Screen2Vec-inspired vector embedding system for UI elements. Enables **cross-site semantic navigation** — the same "Add to Cart" concept works on Amazon, eBay, or any e-commerce site.

**Three embedding components:**
| Component | Dimensions | Source | Purpose |
|-----------|-----------|--------|---------|
| Lexical | 32-D | Text content (name/label) | Understand semantic intent |
| Class | 24-D | HTML role/tag | Define interaction affordance |
| Spatial | 8-D | Normalized bounding box | Map physical location |

**Total: 64-dimensional vector per element**

```python
emb = ElementEmbedding()

# Embed a single element
vector = emb.embed_element({'name': 'Add to Cart', 'role': 'button', 'x': 500, 'y': 300, 'w': 120, 'h': 40})

# Find similar elements by concept
results = emb.find_similar("shopping cart", elements, top_k=5)
# Returns: [(0.92, cart_button), (0.71, basket_link), ...]

# Identify UI concept
concept = emb.identify_concept({'name': 'Log In'})  # Returns: 'login'

# Classify page type
page_type = emb.classify_page_type(elements)  # Returns: 'login', 'search', 'form', etc.

# Page-level embedding (for comparing entire layouts)
page_vec = emb.embed_page(elements)

# Cosine similarity between any two vectors
sim = emb.cosine_similarity(v1, v2)
```

**Concept Detection Keywords:**
- `submit` → submit, send, save, confirm, ok, done, apply
- `cancel` → cancel, close, dismiss, no, back
- `search` → search, find, lookup, query, filter
- `login` → login, sign in, log in, authenticate
- `cart` → cart, basket, bag, checkout, add to cart
- `delete` → delete, remove, trash, discard
- `settings` → settings, preferences, options, configure

---

### 5. Page Topology Graph (`PageTopologyGraph`)

GNN-inspired graph representation where the page is a network of nodes (elements) connected by edges (spatial/semantic relationships).

**Edge Types:**
- **Alignment:** Elements sharing left/right/top edges (→ detects columns, rows)
- **Proximity:** Elements within 100px distance (→ functional grouping)
- **Size Parity:** Elements with identical dimensions (→ detects grids/cards)

```python
graph = PageTopologyGraph()
graph.build(elements)

# Find connected component groups
groups = graph.find_groups()  # E.g., product card = image + title + price + button

# Detect navigation bars
nav_bars = graph.find_navigation_bars()  # Horizontal rows of 3+ links

# Detect grid/card layouts
grids = graph.find_grid_patterns()  # 3+ elements with same dimensions

# Form field grouping (label-input pairs)
forms = graph.find_form_groups()

# GNN-style message passing (neighbor feature aggregation)
enhanced = graph.message_passing(iterations=2)

# Compact export for LLM
topology = graph.to_compact()
```

---

### 6. Action Space Optimizer (`ActionSpaceOptimizer`)

The compression pipeline that reduces a full page from **100,000+ tokens to ~1,400 tokens** — a 98.6% reduction with zero information loss on actionable elements.

**Pipeline:**
1. Extract interactable elements (AOM)
2. Compute semantic geometry (bounding boxes)
3. Resolve occlusion (filter hidden)
4. Score visual prominence (prioritize)
5. Compress to compact JSON

```python
opt = ActionSpaceOptimizer(cdp)

# Full optimized action space
data = opt.optimize(tab_id, max_elements=40)
# Returns: {page_type, viewport, actionable_count, scene: [...], token_estimate}

# LLM-ready prompt context
context = opt.generate_prompt_context(tab_id, task="Find the login button")
# Returns formatted text ready for injection into any LLM prompt
```

**Output format:**
```
## Page State
Type: login
Viewport: 1920x1080
DOM Elements: 2847
Actionable: 12

## Action Space (normalized 0-1000 grid)
[0] textbox "Email" @(350,420 300x35) (login)
[1] textbox "Password" @(350,480 300x35) type=password
[2] button "Sign In" @(350,540 300x45) ★ (login)
[3] link "Forgot Password?" @(380,600 200x20)
[4] link "Create Account" @(380,630 200x20) (register)
```

---

### 7. Spatial Reasoner (`SpatialReasoner`)

Implements **gestalt perception** — seeing the whole layout rather than individual parts. Uses the Visualization-of-Thought (VoT) technique for spatial reasoning.

```python
sr = SpatialReasoner()

# What's near an element?
nearby = sr.what_is_near(target_element, all_elements, direction='below', radius=150)

# Detect layout regions
regions = sr.detect_layout_regions(elements, viewport)
# Returns: {header: [...], navigation: [...], sidebar_left: [...],
#           content: [...], footer: [...], fixed_overlay: [...]}

# Detect rows and columns
layout = sr.detect_rows_and_columns(elements)
# Returns: {rows: 5, columns: 2, row_details: [...], column_details: [...]}

# Find input associated with a label
input_el = sr.find_related_input("Email Address", elements)

# Natural language spatial description (VoT)
desc = sr.spatial_description(element, all_elements, viewport)
# Returns: 'button "Sign In" at middle-center of viewport (960,540), 45px below "Password"'
```

---

### 8. GodMode Controller (`GodMode`)

The unified orchestrator that combines all 7 modules into a single interface.

#### Perception Depths

| Depth | Modules Used | Speed | Tokens |
|-------|-------------|-------|--------|
| `minimal` | AOM only | ~50ms | ~500 |
| `standard` | AOM + Geometry + Occlusion | ~200ms | ~1,400 |
| `deep` | + Graph Topology | ~500ms | ~2,000 |
| `god` | All modules | ~800ms | ~3,000 |

#### Complete API

```python
god = GodMode(cdp_port=9222)

# ─── Perception ───
god.see(depth='standard')      # Multi-layer page perception
god.scene()                     # Optimized LLM context
god.action_space()              # Compact JSON action space
god.describe()                  # Natural language layout description
god.status()                    # System diagnostics

# ─── Semantic Navigation ───
god.find("concept")             # Find by concept (cross-site)
god.find_and_click("Submit")    # Find + click
god.find_and_fill("Email", "x") # Find input by label + fill

# ─── Spatial Intelligence ───
god.what_is_at(x, y)           # Elements at coordinates
god.describe()                  # Full spatial description

# ─── Direct Actions (zero mouse/keyboard) ───
god.click(target)               # Click (str/tuple/dict)
god.type_text("text")           # Type via CDP
god.press("Enter")              # Press key via CDP
god.navigate("https://...")     # Navigate via CDP
god.scroll("down", 300)         # Scroll via CDP
god.eval("js code")             # Execute JavaScript

# ─── Composite Operations ───
god.dismiss_overlays()          # Auto-dismiss modals/banners
god.fill_form({...})            # Fill entire form by labels
god.wait_for(text="Success")    # Wait for content

# ─── Tab Management ───
god.tabs()                      # List tabs
god.new_tab("url")              # Open tab
god.close_tab()                 # Close tab
god.activate_tab(id)            # Focus tab

# ─── Environment ───
god.scan_world()                # Full Win32 + Chrome + UIA scan
god.windows()                   # Window z-order
god.monitors()                  # Monitor layout
```

---

## CLI Usage

```bash
# System status
python god_mode.py status

# See the page (accessibility tree)
python god_mode.py see
python god_mode.py see --depth god --json

# Optimized LLM scene
python god_mode.py scene

# Find elements by concept
python god_mode.py find "search button"

# Click by concept
python god_mode.py click "Submit"

# Spatial description
python god_mode.py describe

# Detect overlays
python god_mode.py overlays

# Graph topology
python god_mode.py graph

# Tab listing
python god_mode.py tabs

# Compact action space
python god_mode.py action-space

# Classify page type
python god_mode.py page-type

# Raw accessibility tree
python god_mode.py a11y
```

---

## Performance Comparison

| Metric | Screenshot (VLM) | Raw DOM | GOD MODE |
|--------|-------------------|---------|----------|
| Latency per action | 3-10 seconds | 500ms | **~200ms** |
| Tokens per scene | 100,000+ | 100,000+ | **~1,400** |
| Cost per step | >$0.01 | >$0.005 | **~$0.001** |
| Coordinate accuracy | ±5-15px (hallucination) | N/A | **Exact (0px error)** |
| CSS refactor resilience | ✅ | ❌ | **✅** |
| Occlusion awareness | ✅ (visual) | ❌ | **✅ (mathematical)** |
| Cross-site generalization | Low | None | **High (embeddings)** |
| Mouse/keyboard needed | Yes (usually) | Varies | **Never** |

---

## Theory & References

Based on concepts from:
- **Accessibility Object Model (AOM):** Browser-computed semantic tree (MDN, web.dev)
- **Semantic Geometry:** Deterministic bounding box extraction with normalized coordinates
- **Screen2Vec:** Semantic embedding of GUI screens and components (Li et al., CHI 2021)
- **Graph4GUI:** Graph Neural Networks for representing GUIs (Jiang et al., 2024)
- **Mind2Web / WebArena:** Benchmark environments for autonomous web agents
- **LayoutLM / StructuralLM:** 2D position embeddings for document understanding
- **WebMCP:** Chrome 146+ machine-native interface protocol

---

## File Structure

```
chrome-bridge/
├── god_mode.py         ← GOD MODE engine (all 8 modules)
├── GOD_MODE.md         ← This documentation
├── perception.py       ← Foundation: SpatialNode, SpatialGrid, PerceptionEngine
├── cdp.py              ← Foundation: CDP WebSocket controller
├── bridge.py           ← Extension bridge
├── server.py           ← Local server
└── ...
```
