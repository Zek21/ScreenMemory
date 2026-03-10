# GOD MODE + Agent + Brain — Complete Function Map

> **god_mode.py**: 61 methods across 8 classes — perception engine.
> **agent.py**: 35+ methods across 5 classes — autonomous execution.
> **brain.py**: 40+ methods across 8 classes — cognitive layer (LLM, recovery, extraction).
> Every function documented with signature, return, purpose.

---

## Class 1: AccessibilityTreeParser

Parses Chrome's Computed Accessibility Tree (AOM) into machine-readable format.
Strips all non-semantic noise (layout divs, CSS wrappers, tracking pixels).

### `__init__(self, cdp: CDP)`
Initialize with active CDP connection.

### `parse(self, tab_id: str) → List[Dict]`
Extract full accessibility tree. Each node has:
- `role` (str): button, link, textbox, heading, etc.
- `name` (str): visible text/label
- `description` (str): aria-description
- `states` (List[str]): focused, expanded, checked, disabled, etc.
- `level` (int): depth in hierarchy
- `children_count` (int): number of children
- `actionable` (bool): True if clickable/typeable
- `value` (str): current value for inputs

### `parse_compact(self, tab_id: str) → str`
Ultra-compact YAML-like representation. ~200 tokens for typical page.
Format: `[role] "name" {state1, state2}` with indentation for hierarchy.

### `find_actionable(self, tab_id: str) → List[Dict]`
Filters to only actionable elements (buttons, links, inputs, etc.).

### `_extract_value(obj) → str` [static]
Extract string value from AOM property dict or plain object.

---

## Class 2: SemanticGeometryEngine

Computes absolute bounding boxes, normalizes to 0-1000 grid, scores visual prominence.

### `__init__(self, cdp: CDP)`
Initialize with CDP. Contains embedded JavaScript for geometry extraction.

### `extract(self, tab_id: str) → Dict`
Returns:
```json
{
  "viewport": {"w": 1920, "h": 1080},
  "elements": [
    {
      "tag": "button", "role": "button", "name": "Submit",
      "x": 400, "y": 300, "w": 120, "h": 40,
      "nx": 208, "ny": 278, "nw": 63, "nh": 37,
      "areaRatio": 0.003, "fontWeight": 700,
      "prominence": 0.72
    }
  ]
}
```
- `x,y,w,h`: absolute pixel coordinates
- `nx,ny,nw,nh`: normalized 0-1000 coordinates (resolution-independent)
- `prominence`: visual importance score (0.0 - 1.0)

### `extract_grounded_action_space(self, tab_id, region=None, role_filter=None, min_prominence=0.0) → str`
Compact JSON string optimized for LLM consumption.
- `region`: "top", "bottom", "left", "right", "center" — filter by viewport region
- `role_filter`: ["button", "input"] — filter by role
- `min_prominence`: 0.0-1.0 — minimum prominence threshold

### `find_primary_cta(self, tab_id: str) → Optional[Dict]`
Returns the most prominent actionable element (highest prominence score).

### `spatial_clusters(self, tab_id, threshold=50) → List[List[Dict]]`
Groups elements into spatial clusters using agglomerative clustering.
`threshold`: max pixel distance to merge clusters.

### `_compute_prominence(self, el, viewport) → float`
Scores element importance based on: size ratio, center bias, font weight,
element type, and interactive role. Returns 0.0-1.0.

### `_filter_by_region(self, elements, region, viewport) → List[Dict]`
Filters elements to viewport quadrants (top/bottom/left/right/center).

### `_cluster_distance(c1, c2) → float` [static]
Minimum Euclidean distance between any pair of elements in two clusters.

---

## Class 3: OcclusionResolver

Resolves CSS z-index stacking, detects overlays, computes element visibility.

### `__init__(self, cdp: CDP)`
Initialize with CDP. Contains embedded JavaScript for 5-point visibility testing.

### `resolve(self, tab_id: str) → Dict`
Returns:
```json
{
  "total": 45,
  "visible": 38,
  "occluded": 4,
  "partially_occluded": 3,
  "in_modal": true,
  "elements": [...],
  "modal_elements": [...]
}
```
Uses `document.elementFromPoint()` at 5 sample points per element
(center + 4 corners inset by 2px) to compute `visibilityRatio`.

### `get_truly_interactable(self, tab_id: str) → List[Dict]`
Returns only elements with `visibilityRatio >= 0.6` and not occluded.

### `detect_overlays(self, tab_id: str) → List[Dict]`
Detects modals, popups, cookie banners, and overlay elements.
Criteria: z-index > 100 OR position:fixed/sticky OR has dialog/modal role.

---

## Class 4: ElementEmbedding

Hash-based vector embeddings for UI elements. 96-dimensional vectors
(64 lexical + 24 role + 8 spatial).

### `__init__(self, lexical_dim=64, class_dim=24, spatial_dim=8)`
Pre-computes concept vectors for: login, search, navigation, submit, cancel,
settings, cart, close, profile, help, checkout, signup, logout, delete,
download, upload, share, filter, sort, menu, notification, message, media, ok.

### `embed_element(self, element: Dict) → List[float]`
Generates 96-D vector by concatenating:
1. Lexical vector (64-D): MD5 trigram hashing of text content
2. Role vector (24-D): One-hot encoding of element role
3. Spatial vector (8-D): Normalized position, size, area, aspect, center

### `embed_page(self, elements: List[Dict]) → List[float]`
Page-level embedding by averaging all element vectors.

### `cosine_similarity(self, v1, v2) → float`
Standard cosine similarity. Returns 0.0 if either vector has zero magnitude.

### `find_similar(self, target_text, elements, top_k=5) → List[Tuple[float, Dict]]`
Returns top-k elements sorted by cosine similarity to target text.
Each result: `(similarity_score, element_dict)`.

### `identify_concept(self, element: Dict) → Optional[str]`
Maps element to one of 24 pre-defined UI concepts (login, search, cart, etc.).
Returns None if no concept matches above threshold (0.35).

### `classify_page_type(self, elements: List[Dict]) → str`
Classifies page as one of: login, search, search_results, form, listing,
article, dashboard, checkout, media, generic.
Uses concept distribution + role distribution heuristics.

### `_text_to_hash_vector(self, text: str) → List[float]`
Deterministic hash embedding: lowercase → trigram → MD5 → bucket into 64-D.
Words get 2x weight vs character trigrams. L2-normalized output.

### `_role_to_vector(self, role: str) → List[float]`
One-hot encoding against 24-role vocabulary. Unknown roles get zero vector.

### `_spatial_vector(self, element: Dict) → List[float]`
8 features: [nx, ny, nw, nh, area, aspect_ratio, center_x, center_y].
All normalized to 0-1 range using the 0-1000 grid.

### `_mean_vector(vectors) → List[float]` [static]
Element-wise average. Returns `[]` if input is empty.

---

## Class 5: PageTopologyGraph

Graph representation of page layout with typed edges.

### Constants
- `ALIGNMENT_THRESHOLD = 10` — pixels for alignment detection
- `PROXIMITY_THRESHOLD = 100` — pixels for proximity edges
- `SIZE_PARITY_THRESHOLD = 5` — pixels for size matching

### `__init__(self)`
Initialize empty graph (nodes, edges, adjacency lists).

### `build(self, elements: List[Dict]) → PageTopologyGraph`
Constructs graph. Returns self for chaining.
- Nodes: `{idx, role, name, box, element}`
- Edges computed pairwise (O(n²)): alignment, proximity, size_parity

### `find_groups(self) → List[List[int]]`
Connected components via BFS. Returns groups ≥2 elements.

### `find_form_groups(self) → List[Dict]`
Detects label-input pairs. Returns:
```json
[{"label_idx": 0, "label": "Email", "input_idx": 1, "input_role": "textbox"}]
```

### `find_navigation_bars(self) → List[List[int]]`
Horizontal rows of ≥3 links/buttons with shared y-alignment.

### `find_grid_patterns(self) → List[List[int]]`
Elements with matching dimensions (≥3 elements per group).

### `message_passing(self, iterations=2) → List[Dict]`
GNN-inspired feature aggregation:
1. Collects neighbor roles and names
2. Marks elements as `in_nav_bar`, `in_grid`, or `in_form`
3. Returns enriched feature dicts

### `to_compact(self) → Dict`
Export as `{"nodes": [...], "edges": [...]}` for LLM.

### `_compute_edges(self, i, j) → List[Dict]`
Checks three edge types between nodes i and j:
- **h_aligned**: Top edges within ALIGNMENT_THRESHOLD
- **v_aligned**: Left edges within ALIGNMENT_THRESHOLD
- **proximate**: Center distance < PROXIMITY_THRESHOLD
- **size_parity**: Width and height both within SIZE_PARITY_THRESHOLD

### `_element_distance(a, b) → float` [static]
Euclidean distance between element centers.

---

## Class 6: ActionSpaceOptimizer

Compresses full page into ~1400 tokens for LLM consumption.

### `__init__(self, cdp: CDP)`
Initializes geometry, occlusion, and embedding sub-modules.

### `optimize(self, tab_id, max_elements=50, include_embeddings=False, include_page_type=True) → Dict`
Full pipeline:
1. Extract geometry → bounding boxes
2. Resolve occlusion → filter to visible
3. Score prominence → sort by importance
4. Truncate to max_elements
5. Classify page type
6. Return compact dict

### `generate_prompt_context(self, tab_id, task=None, max_elements=40) → str`
Generates ready-to-paste LLM context string:
```
PAGE: login | 12 elements | viewport: 1920x1080
TASK: Fill in the login form
ELEMENTS:
[1] button "Submit" @(400,300) 120x40 prominence:0.72
[2] textbox "Email" @(300,250) 200x30 prominence:0.65
...
```

---

## Class 7: SpatialReasoner

Implements gestalt perception: proximity, alignment, common region, similarity.

### `__init__(self)`
No initialization needed — stateless utility.

### `what_is_near(self, target, elements, direction=None, radius=150) → List[Dict]`
Find elements within `radius` pixels of target.
- `direction`: "above", "below", "left", "right" — optional directional filter
- Returns sorted by distance (closest first)

### `detect_layout_regions(self, elements, viewport) → Dict`
Classifies elements into regions:
- `header`: top 15% of viewport
- `footer`: bottom 15% of viewport
- `sidebar`: left/right 20% of viewport
- `nav`: horizontal rows of ≥3 links in header
- `content`: everything else

### `detect_rows_and_columns(self, elements, tolerance=15) → Dict`
Groups elements by shared y-center (rows) and x-center (columns).
Returns `{rows, columns, row_details, column_details}`.
**Guard**: tolerance clamped to ≥15 to prevent division by zero.

### `find_related_input(self, label_text, elements) → Optional[Dict]`
Gestalt proximity: finds input field associated with label text.
Strategy: find label element → find nearest input below/right.

### `spatial_description(self, element, all_elements, viewport) → str`
Visualization-of-Thought (VoT) — generates natural language description:
```
"Submit" button is in the center-right of the page.
It is in the content area.
Nearby elements: "Cancel" button (52px right), "Email" textbox (80px above).
```

---

## Class 8: GodMode

Unified orchestrator. **Use this class for all operations.**

### Properties
- `cdp → CDP`: Lazy-initialized CDP connection
- `connected → bool`: True if Chrome is reachable on CDP port

### High-Level (start here)
| Method | Best For |
|--------|---------|
| `see(depth='standard')` | Understanding what's on page |
| `scene()` | LLM-optimized page description |
| `find(concept)` | Locating elements semantically |
| `click(target)` | Interacting with elements |
| `fill_form(fields)` | Form automation |
| `describe()` | Full spatial narration |

### All Methods (see DECISION_TREE.md for routing)

Initialization: `__init__`, `cdp` (prop), `connected` (prop), `_ensure_modules`
Perception: `see`, `scene`, `action_space`, `describe`, `what_is_at`
Navigation: `find`, `find_and_click`, `find_and_fill`
Actions: `click`, `type_text`, `press`, `navigate`, `scroll`, `eval`, `screenshot`
Tabs: `tabs`, `new_tab`, `close_tab`, `activate_tab`
Composite: `dismiss_overlays`, `fill_form`, `wait_for`
Environment: `scan_world`, `windows`, `monitors`
Status: `status`, `history`, `_get_active_tab`

---

## Data Flow

```
Chrome Browser
    ↓ CDP WebSocket (port 9222)
    ↓
┌───────────────────────────────────────┐
│ AccessibilityTreeParser               │ → semantic nodes (role, name, states)
│ SemanticGeometryEngine                │ → bounding boxes + normalized coords
│ OcclusionResolver                     │ → visibility status + overlay detection
│         ↓                             │
│ ElementEmbedding                      │ → 96-D vectors per element
│ PageTopologyGraph                     │ → relational graph (alignment/proximity)
│ SpatialReasoner                       │ → regions, rows, columns, descriptions
│ ActionSpaceOptimizer                  │ → compressed LLM-ready output
│         ↓                             │
│ GodMode Controller                    │ → unified API + action execution
└───────────────────────────────────────┘
    ↓
  LLM Context (~1,400 tokens)
    ↓
┌───────────────────────────────────────┐  ← agent.py
│ ActionProtocol                        │ → structured JSON action format
│ ActionExecutor                        │ → maps LLM decisions → CDP commands
│ SessionManager                        │ → history, loop detection, rollback
│ AutonomousAgent                       │ → perceive→decide→execute→verify loop
│ StealthLauncher                       │ → invisible Chrome (headless/hidden/offscreen)
└───────────────────────────────────────┘
```

---

## agent.py — Autonomous Agent Classes

### Class 9: StealthLauncher

Launch Chrome completely invisible to the user.

#### `find_chrome() → Optional[str]` [static]
Find Chrome executable on the system.

#### `get_user_data_dir() → str` [static]
Get default Chrome user data directory.

#### `list_profiles() → List[Dict]` [static]
List all Chrome profiles with names.
Returns: `[{"directory": "Profile 3", "name": "makhalem", "path": "..."}]`

#### `launch(self, mode=HEADLESS, profile="Default", url="about:blank", extra_args=None) → CDP`
Launch Chrome invisibly and return CDP connection.
- `mode`: `StealthLauncher.Mode.HEADLESS` | `HIDDEN` | `OFFSCREEN`
- `profile`: Chrome profile directory name

#### `stop(self)`
Terminate launched Chrome process.

#### `running → bool` [property]
True if Chrome process is still alive.

---

### Class 10: Action (dataclass)

A single action the LLM outputs.

Fields: `action, target, value, direction, amount, timeout, reason, uid`

#### `to_dict() → Dict`
Convert to dict, omitting None values.

#### `from_dict(d: Dict) → Action` [static]
Parse from dict.

#### `from_json(text: str) → Action` [static]
**Smart parser** — extracts JSON from:
- Raw JSON string
- Markdown code blocks
- JSON embedded in prose
- Returns `Action(action='fail')` if unparseable

---

### Class 11: Observation (dataclass)

What the agent observed after executing an action.

Fields: `success, page_url, page_title, page_type, scene, elements_count, action_result, error, step, elapsed_ms`

#### `to_prompt() → str`
Format as LLM prompt context.

#### `to_dict() → Dict`
Convert to dict.

---

### Class 12: ActionProtocol

LLM ↔ Agent communication protocol.

#### `SYSTEM_PROMPT` (class constant)
Complete system prompt that instructs the LLM how to respond.

#### `format_task_prompt(task, observation) → str` [static]
Format a complete prompt with task + current page state.

#### `format_initial_prompt(task, scene, url, page_type) → str` [static]
Format the initial prompt before any actions.

---

### Class 13: ActionExecutor

Maps LLM decisions to CDP commands.

#### `__init__(self, god: GodMode)`
Initialize with GodMode instance.

#### `update_elements(self, elements: List[Dict])`
Update UID → element mapping after perception cycle.

#### `execute(self, action: Action, tab_id=None) → Dict`
Execute any action. Returns `{"success": bool, "result": str, "error": str}`.
Handles: click, type, press, scroll, navigate, wait, hover, dismiss, select, extract, screenshot, done, fail.

---

### Class 14: SessionManager

Tracks session state, detects loops, enforces safety limits.

#### `start(self, task, max_steps=50)`
Start new session.

#### `record_step(self, step_num, action, observation)`
Record a completed step.

#### `should_stop(self) → Optional[str]`
Check safety limits. Detects:
- Max steps exceeded
- Action loops (same action 3x on same URL)
- 5 consecutive failures

#### `summary(self) → Dict`
Generate complete session summary.

#### `save(self, filepath=None)`
Save session to JSON file.

---

### Class 15: AutonomousAgent

**The main class for autonomous operation.**

#### `__init__(self, cdp_port=9222, god=None)`
Initialize with CDP port or existing GodMode.

#### `perceive(self, tab_id=None, depth='standard') → Observation`
Run full GOD MODE perception cycle. Returns compressed scene + element UIDs.

#### `act(self, action: Action, tab_id=None) → Observation`
Execute one action → wait → re-perceive → return observation.

#### `run(self, task, decide_fn, max_steps=30, tab_id=None, on_step=None) → Dict`
**The full autonomous loop.**
- `decide_fn(system_prompt, user_prompt) → str`: Your LLM function
- Returns session summary when done or failed

#### `run_script(self, actions: List[Dict], tab_id=None, delay=0.5) → Dict`
Execute pre-scripted actions (no LLM needed).

#### `interactive(self, tab_id=None)`
Interactive REPL for debugging.

---

### Convenience Functions

#### `quick_script(url, actions, profile=None, port=9222) → Dict`
One-liner scripted automation. Optionally launches stealth Chrome.

#### `stealth_open(url, profile="Default", mode="headless", port=9222) → (StealthLauncher, GodMode)`
Open URL invisibly, return launcher + god mode for interactive use.
**Caller must call `launcher.stop()` when done.**

---

## brain.py — Cognitive Layer Classes

### Class 16: LLMConnector

Universal LLM interface supporting 5 providers.

#### `__init__(self, provider, api_key=None, model=None, base_url=None, temperature=0.1, max_tokens=500, custom_fn=None)`
Providers: `"openai"`, `"claude"`, `"gemini"`, `"ollama"`, `"mock"`, `"custom"`
Auto-detects API keys from env vars: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`

#### `decide(self, system_prompt, user_prompt) → str`
**The function you pass to `AutonomousAgent.run()` as `decide_fn`.**

#### `stats → Dict` [property]
Returns: `{provider, model, calls, total_tokens}`

---

### Class 17: PageDiffer

Detects what changed between two perception snapshots.

#### `diff(before, after) → Dict` [static]
Returns: `{url_changed, new_url, elements_added, elements_removed, new_elements, page_type_changed, overlay_appeared, overlay_dismissed, content_changed, summary}`

---

### Class 18: ContentExtractor

Pulls structured data from pages via JavaScript injection.

#### `__init__(self, god: GodMode)`
#### `extract_text(self, tab_id=None, max_chars=5000) → str`
All visible text in reading order.
#### `extract_links(self, tab_id=None) → List[Dict]`
All visible links with text, href, coordinates.
#### `extract_tables(self, tab_id=None) → List[Dict]`
All tables as `{index, rows: [[cell, ...], ...]}`.
#### `extract_forms(self, tab_id=None) → List[Dict]`
All form fields with `{type, name, label, value, required, x, y, w, h}`.
#### `extract_metadata(self, tab_id=None) → Dict`
`{title, url, description, h1, h2, canonical}`
#### `extract_all(self, tab_id=None) → Dict`
All of the above combined.
#### `summarize_for_llm(self, tab_id=None, max_tokens=800) → str`
Ultra-compact content summary for LLM.

---

### Class 19: ErrorRecovery

Smart error recovery with 5 strategies.

#### `__init__(self, god, max_retries=3)`
#### `recover(self, action, error, tab_id=None) → Optional[Action]`
Returns a recovery action:
1. Not found → scroll down
2. Still not found → dismiss overlays
3. Still not found → scroll up
4. CDP/timeout error → wait and retry
5. Any → try alternative target text

#### `should_retry(self, action) → bool`
#### `record_failure(self, action)`
#### `reset(self)`

---

### Class 20: HumanBehavior

Anti-detection engine with realistic interaction patterns.

#### `__init__(self, speed=1.0)`
`speed`: 0.5=fast, 1.0=normal, 2.0=slow
#### `delay_before_action(self, action_type) → float`
Gaussian-distributed delay with familiarity effect.
#### `delay_after_navigation(self) → float`
Reading time after page load.
#### `typing_delay(self) → float`
Between-keystroke delay with occasional pauses.
#### `scroll_amount(self, requested) → List[int]`
Breaks large scrolls into human-like chunks.
#### `should_pause(self) → bool`
10% chance of reading pause.
#### `pause_duration(self) → float`
Random pause 0.5-2.0s × speed.

---

### Class 21: BlockDetector

CAPTCHA, rate-limit, and block detection.

#### `__init__(self, god)`
#### `check(self, tab_id=None) → Dict`
Returns: `{blocked, captcha, rate_limited, signals, severity, recommendation}`
Detects: reCAPTCHA, hCaptcha, Cloudflare Turnstile, 403/429, rate limits, IP bans.

---

### Class 22: MultiTabOrchestrator

Coordinate actions across multiple browser tabs.

#### `__init__(self, god)`
#### `open_in_new_tab(self, url) → str`
Open URL, push current tab to stack.
#### `return_to_previous(self)`
Close current, pop stack, go to previous.
#### `extract_and_return(self, url, extract_fn=None) → Any`
Open → extract → close → return data.
#### `parallel_extract(self, urls, extract_fn=None) → List[Any]`
Open multiple tabs, extract from each, close all.

---

### Class 23: Brain

**THE ULTIMATE CONTROLLER. Use this class for everything.**

#### `__init__(self, cdp_port=9222, llm_provider="mock", llm_api_key=None, llm_model=None, stealth_mode=None, chrome_profile=None, human_speed=1.0)`

#### `execute_mission(self, objective, start_url=None, max_steps=30, check_blocks=True) → Dict`
**Full LLM-driven autonomous web navigation with recovery + human-like behavior + block detection.**

#### `execute_script(self, start_url, actions, delay=0.5) → Dict`
Pre-scripted automation (no LLM).

#### `extract(self, url=None, tab_id=None) → Dict`
Pull all structured data from page.

#### `see(depth='standard') → Dict`
GOD MODE perception shortcut.

#### `scene() → str`
Compressed scene shortcut.

#### `check_blocks() → Dict`
CAPTCHA/block detection.

#### `open_in_tab(url) → str`
Multi-tab orchestration.

#### `status() → Dict`
Complete system status including LLM stats.

#### `shutdown()`
Clean shutdown (stops stealth Chrome if launched).
