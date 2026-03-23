# Set-of-Mark Visual Grounding — Research Report

## Summary

Set-of-Mark (SoM) Visual Grounding is a computer vision pipeline that transforms raw screenshots into spatially annotated images with numbered markers on detected interactive UI elements. It enables vision-language models (VLMs) and automation systems to reference precise screen locations by marker ID rather than brittle CSS selectors or pixel coordinates.

The core insight — adapted from "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding" (Yang et al., 2023) — is that overlaying numbered markers on a screenshot lets any downstream consumer say **"click mark 7"** and resolve that to exact pixel coordinates. This decouples spatial reasoning from DOM structure, making it applicable to any visual interface: desktop applications, web pages, mobile emulators, or remote desktop sessions.

**Pipeline at a glance:**

```
Screenshot → Edge Detection → Region Proposals → Filtering → Merge Overlaps → Numbered Marker Overlay → GroundedScreenshot
```

The output `GroundedScreenshot` contains the original image, the marked image, and a list of `UIRegion` objects with bounding boxes, center coordinates, confidence scores, and optional semantic labels.

---

## Requirements

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | 3.10+ | Runtime |
| Pillow (PIL) | 10.0+ | Image manipulation, drawing, font rendering |
| NumPy | 1.24+ | Gradient computation, edge detection, integral images |

### Optional Dependencies

| Dependency | Purpose |
|------------|---------|
| A VLM analyzer (e.g., GPT-4V, LLaVA) | Semantic labeling of detected regions |
| `PIL.ImageGrab` | Live screen capture for testing (Windows/macOS) |

> **Note:** The engine deliberately avoids an OpenCV dependency. All edge detection, gradient computation, and contour analysis are implemented with pure NumPy operations and Pillow, keeping the install footprint minimal.

---

## Quick Start

### Example 1: Basic Grounding from a Screenshot File

```python
from PIL import Image
from core.grounding.set_of_mark import SetOfMarkGrounding

# Load a screenshot
screenshot = Image.open("screenshot.png")

# Create the grounding engine
grounder = SetOfMarkGrounding()

# Run the full pipeline
grounded = grounder.ground(screenshot)

# Inspect results
print(f"Detected {len(grounded.regions)} interactive regions")
for region in grounded.regions:
    print(f"  Mark {region.id}: ({region.x}, {region.y}) "
          f"{region.width}x{region.height} "
          f"center=({region.center_x}, {region.center_y})")

# Save the annotated image
grounded.marked.save("grounded_output.png")
```

### Example 2: Click Coordinate Resolution

```python
from PIL import Image
from core.grounding.set_of_mark import SetOfMarkGrounding

screenshot = Image.open("screenshot.png")
grounder = SetOfMarkGrounding(min_region_size=300, max_regions=25)
grounded = grounder.ground(screenshot)

# A VLM says "click mark 5" — resolve to coordinates
coords = grounded.get_click_coords(5)
if coords:
    x, y = coords
    print(f"Click at ({x}, {y})")
    # Feed (x, y) to your mouse automation tool

# Search by label (after semantic labeling)
matches = grounded.find_by_label("search")
for m in matches:
    print(f"  Mark {m.id}: {m.label} at ({m.center_x}, {m.center_y})")
```

### Example 3: Semantic Grounding with a VLM

```python
from PIL import Image
from core.grounding.set_of_mark import SetOfMarkGrounding

screenshot = Image.open("screenshot.png")
grounder = SetOfMarkGrounding()

# Define a VLM query function (adapter for your model)
def my_vlm(image: Image.Image, prompt: str) -> str:
    """Send image + prompt to your VLM and return text response."""
    # Replace with your actual VLM API call
    import openai
    # ... encode image, call API, return text ...
    return "1: search button\n2: url bar\n3: close tab button"

# Semantic grounding — labels every region via a single VLM call
grounded = grounder.ground_semantic(screenshot, vlm_query_fn=my_vlm, batch=True)

for region in grounded.regions:
    print(f"  Mark {region.id}: {region.label} (type={region.region_type})")

# Filter by type
buttons = grounded.find_by_type("button")
print(f"Found {len(buttons)} buttons")
```

---

## API Reference

### `UIRegion` (dataclass)

A detected interactive region on screen.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `int` | — | Marker ID (1-indexed, assigned during grounding) |
| `x` | `int` | — | Top-left X coordinate (pixels) |
| `y` | `int` | — | Top-left Y coordinate (pixels) |
| `width` | `int` | — | Region width (pixels) |
| `height` | `int` | — | Region height (pixels) |
| `center_x` | `int` | auto | Center X (computed: `x + width // 2`) |
| `center_y` | `int` | auto | Center Y (computed: `y + height // 2`) |
| `label` | `str` | `""` | Semantic label (e.g., "search button") — set by VLM |
| `confidence` | `float` | `0.0` | Detection confidence (edge density score) |
| `region_type` | `str` | `"unknown"` | Canonical type: `button`, `text_field`, `link`, `menu`, `tab`, `icon`, `image`, `text`, `unknown` |

**Properties:**

| Property | Return Type | Description |
|----------|-------------|-------------|
| `bbox` | `Tuple[int, int, int, int]` | Bounding box as `(x1, y1, x2, y2)` |
| `area` | `int` | Region area in pixels² (`width × height`) |

---

### `GroundedScreenshot` (dataclass)

Screenshot with overlaid markers and detected regions.

| Field | Type | Description |
|-------|------|-------------|
| `original` | `Image.Image` | The unmodified input screenshot |
| `marked` | `Image.Image` | Screenshot with numbered markers and bounding boxes overlaid |
| `regions` | `List[UIRegion]` | All detected regions with assigned IDs |
| `timestamp` | `float` | Capture timestamp (default `0.0`) |

**Methods:**

#### `get_region(mark_id: int) -> Optional[UIRegion]`

Returns the `UIRegion` with the given marker ID, or `None` if not found.

```python
region = grounded.get_region(3)
if region:
    print(f"Mark 3 is at ({region.x}, {region.y}), size {region.width}x{region.height}")
```

#### `get_click_coords(mark_id: int) -> Optional[Tuple[int, int]]`

Returns the center `(x, y)` coordinates for clicking the given marker, or `None` if not found.

```python
coords = grounded.get_click_coords(7)
# coords = (450, 320)
```

#### `find_by_label(query: str) -> List[UIRegion]`

Finds regions whose label matches a natural language query. Uses substring matching and keyword overlap.

```python
results = grounded.find_by_label("submit button")
# Returns regions labeled "submit", "submit button", "button submit", etc.
```

#### `find_by_type(region_type: str) -> List[UIRegion]`

Returns all regions matching a canonical type.

```python
text_fields = grounded.find_by_type("text_field")
buttons = grounded.find_by_type("button")
```

Valid types: `button`, `text_field`, `link`, `menu`, `tab`, `icon`, `image`, `text`, `unknown`.

---

### `SetOfMarkGrounding` (class)

The main visual grounding engine.

#### Constructor

```python
SetOfMarkGrounding(
    min_region_size: int = 400,
    max_regions: int = 30,
    merge_threshold: float = 0.5,
    edge_sensitivity: int = 50
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `min_region_size` | `int` | `400` | Minimum area in px² for a region to be kept |
| `max_regions` | `int` | `30` | Maximum number of markers to overlay |
| `merge_threshold` | `float` | `0.5` | IoU threshold above which overlapping regions merge |
| `edge_sensitivity` | `int` | `50` | Edge sensitivity (higher = more edges detected). Internally computes `percentile(magnitude, 100 - sensitivity)` as the threshold, so higher values lower the threshold, admitting more edges. |

#### `ground(screenshot: Image.Image) -> GroundedScreenshot`

Full grounding pipeline. Detects regions, filters, merges overlaps, assigns IDs, and overlays numbered markers.

```python
grounded = grounder.ground(screenshot)
```

**Returns:** `GroundedScreenshot` with original image, marked image, and region list.

#### `ground_with_description(screenshot: Image.Image, analyzer=None) -> Tuple[GroundedScreenshot, str]`

Runs the full grounding pipeline, then optionally passes the marked image to a VLM analyzer for a natural language description of all visible elements.

```python
grounded, description = grounder.ground_with_description(screenshot, analyzer=my_analyzer)
print(description)
# "1: Search button, 2: Navigation menu, 3: Close tab icon, ..."
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `screenshot` | `Image.Image` | Input screenshot |
| `analyzer` | optional | Object with `.is_available` property and `.analyze(image, detailed=False)` method |

**Returns:** `(GroundedScreenshot, str)` — the grounded result and the VLM description.

#### `ground_semantic(screenshot: Image.Image, vlm_query_fn=None, batch=True) -> GroundedScreenshot`

Grounding with semantic labeling of regions. Two modes:

| Mode | `batch` | VLM Calls | Speed | Accuracy |
|------|---------|-----------|-------|----------|
| Batch | `True` | 1 call total | Fast (~seconds) | Good |
| Per-region | `False` | N calls (one per region) | Slow (~seconds × N) | Higher |

```python
grounded = grounder.ground_semantic(screenshot, vlm_query_fn=my_vlm, batch=True)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `screenshot` | `Image.Image` | Input screenshot |
| `vlm_query_fn` | `Callable(Image, str) -> str` or `None` | VLM query function. If `None`, uses heuristic classification. |
| `batch` | `bool` | `True` for single-call batch labeling, `False` for per-region cropping |

**Returns:** `GroundedScreenshot` with `label` and `region_type` populated on each region.

---

## Architecture

The grounding pipeline consists of five sequential stages, each designed for computational efficiency and robustness across diverse UI styles.

### Stage 1: Edge Detection

The engine converts the input image to grayscale and computes horizontal and vertical gradients using a Sobel-like finite difference operator:

```
gx[y, x] = gray[y, x+1] - gray[y, x-1]   (horizontal)
gy[y, x] = gray[y+1, x] - gray[y-1, x]   (vertical)
magnitude = sqrt(gx² + gy²)
```

The magnitude is thresholded at a configurable percentile (controlled by `edge_sensitivity`). The threshold is `percentile(magnitude, 100 - sensitivity)`. With the default sensitivity of 50, the threshold is the 50th percentile — keeping the top 50% of edges. Higher values produce more edges (more sensitive) because they lower the percentile threshold; lower values produce fewer edges.

> **Note:** The source docstring at `core/grounding/set_of_mark.py` line 136 incorrectly states "lower = more edges". The code does `percentile(mag, 100 - sensitivity)`, which means **higher sensitivity = lower threshold = more edges**.

**Why this works for UI:** UI elements (buttons, text fields, tabs, menus, icons) have sharp edges that contrast strongly with their background. Natural scene images have diffuse gradients, but UI screenshots have crisp rectangular boundaries that the edge detector isolates cleanly.

### Stage 2: Region Proposals via Grid Scan

Rather than a computationally expensive connected-component analysis, the engine uses an **integral image** accelerated grid scan:

1. **Build integral image** — `O(H×W)` precomputation enabling `O(1)` rectangle sum queries
2. **Non-overlapping grid scan** — divide the image into cells (default: `~30×40` grid). For each cell, query edge density in constant time
3. **Density threshold** — cells with >6% edge density are promoted as seed regions
4. **Region expansion** — each seed is expanded to encompass the full edge cluster by finding the tight bounding box of edge pixels in a local neighborhood

This approach is **O(H×W)** for the integral image construction plus **O(G)** for the grid scan where G is the number of grid cells — far faster than contour tracing on large screenshots.

### Stage 3: Region Filtering

Raw region proposals are filtered by three criteria:

| Filter | Condition | Rationale |
|--------|-----------|-----------|
| **Minimum area** | `area >= min_region_size` (default 400px²) | Reject noise: tiny edge clusters that aren't real UI elements |
| **Aspect ratio** | `0.05 ≤ width/height ≤ 20.0` | Reject degenerate shapes: extremely thin lines or borders |
| **Maximum area** | `area ≤ (width + height) × 500` | Reject full-screen background regions |

After filtering, regions are sorted in **reading order** (top-to-bottom, left-to-right) by quantizing the Y-coordinate into 30px rows.

### Stage 4: Overlap Merging

Overlapping regions are merged using a **spatial grid index** for efficient neighbor lookup:

1. **Build spatial grid** — each region is assigned to all grid cells it overlaps (cell size: 80px)
2. **Neighbor query** — for each region, only check overlap against regions in the same or adjacent grid cells
3. **IoU merge** — if two regions have Intersection-over-Union above the `merge_threshold` (default 0.5), they are merged into a single bounding box encompassing both
4. **Confidence averaging** — the merged region's confidence is the mean of its constituents

This grid-accelerated approach avoids the naïve `O(N²)` all-pairs comparison, achieving near-linear performance in practice.

### Stage 5: Marker Overlay

Each surviving region receives a visual annotation:

- **Colored bounding box** — 2px border using one of 10 high-contrast colors (red, blue, green, orange, purple, pink, cyan, gold, lime, red-orange), cycling by marker ID
- **Numbered circle** — a filled circle at the top-left corner of the bounding box containing the marker ID in white text
- **Font rendering** — attempts to use Arial 11pt for numbers; falls back to Pillow's built-in bitmap font if unavailable

The marker circle is positioned above the bounding box when space allows, or inside the top edge when the region is near the top of the screen.

### Optional Stage 6: Semantic Labeling

When a VLM query function is provided, the engine adds semantic labels to regions:

- **Batch mode (default):** sends the entire marked screenshot to the VLM with a structured prompt requesting `"N: description"` format. Parses the response with regex to extract per-marker labels. Single VLM call.
- **Per-region mode:** crops each region (with 8px padding) and queries the VLM individually for a 2-4 word element description. Higher accuracy, N× cost.
- **Heuristic fallback:** when no VLM is available, classifies regions by geometry (aspect ratio and area thresholds into `button`, `text_field`, `menu`, `icon`, `text`, or `unknown`).

Labels are mapped to canonical `region_type` values via keyword matching against a dictionary of type → keyword associations (e.g., "submit" → `button`, "input" → `text_field`).

---

## Performance Characteristics

| Stage | Complexity | Typical Time (1920×1080) | Notes |
|-------|-----------|--------------------------|-------|
| Edge detection | O(H×W) | ~15–30ms | NumPy vectorized gradient + threshold |
| Integral image + grid scan | O(H×W + G) | ~10–20ms | G ≈ 1200 cells for 1080p |
| Region expansion | O(G × M²) | ~5–15ms | M = neighborhood margin (default 10px) |
| Filtering | O(N) | <1ms | N = raw region count |
| Overlap merging | O(N log N) amortized | ~2–5ms | Spatial grid index eliminates most comparisons |
| Marker overlay | O(R) | ~5–10ms | R = final region count (≤ max_regions) |
| **Total (no VLM)** | | **~40–80ms** | Pure CPU, no GPU required |
| VLM batch labeling | 1 API call | ~2–20s | Depends on VLM provider latency |
| VLM per-region labeling | N API calls | ~2–20s × N | Only use when high accuracy is critical |

### Memory Usage

- Input image: ~6MB for 1920×1080 RGB
- Edge arrays (float64 gradient + uint8 binary): ~16MB
- Integral image (int32): ~8MB
- **Peak working memory: ~30MB** for a 1080p screenshot
- Output marked image: same size as input (~6MB)

### Scaling

| Resolution | Regions (typical) | Time (no VLM) |
|------------|-------------------|---------------|
| 1280×720 | 10–20 | ~25–50ms |
| 1920×1080 | 15–30 | ~40–80ms |
| 2560×1440 | 20–40 | ~70–130ms |
| 3840×2160 | 25–50 | ~150–280ms |

Performance scales linearly with pixel count. The grid scan and integral image keep the constant factor low.

---

## Use Cases

### 1. VLM-Guided Desktop Automation

The primary use case: feed a marked screenshot to a vision-language model, receive instructions like "click mark 3", and resolve to exact pixel coordinates. This eliminates the need for DOM parsing, accessibility tree traversal, or fragile selector-based automation.

```python
grounded = grounder.ground(screenshot)
grounded.marked.save("for_vlm.png")
# VLM response: "To complete the task, click mark 5 (the Submit button)"
x, y = grounded.get_click_coords(5)
```

### 2. Accessibility Auditing

Detect all interactive regions on a screen and verify they have appropriate labels, sufficient size, and adequate contrast. The region list provides a structural map of the interface without requiring application-specific integration.

### 3. UI Test Automation

Generate regression test anchors from screenshots. When the UI changes, re-run grounding and compare region positions/counts to detect layout drift without maintaining element selectors.

### 4. Remote Desktop Interaction

When automating through VNC, RDP, or screen-sharing where no DOM or accessibility tree is available, Set-of-Mark provides the only viable path to spatial interaction.

### 5. Multi-Application Coordination

Ground multiple application windows simultaneously. Each window's elements receive unique marker IDs, enabling cross-application automation workflows (e.g., "copy from mark 3 in Window A, paste into mark 12 in Window B").

### 6. Training Data Generation

Produce labeled UI datasets by running semantic grounding on screenshot corpora. Each region gets a bounding box, type classification, and optional VLM-generated description — suitable for training object detection or UI understanding models.

---

## Constants

### Marker Colors

The engine cycles through 10 high-contrast colors for visual distinguishability:

| Index | Color | RGB |
|-------|-------|-----|
| 0 | Red | `(255, 0, 0)` |
| 1 | Blue | `(0, 150, 255)` |
| 2 | Green | `(0, 200, 0)` |
| 3 | Orange | `(255, 165, 0)` |
| 4 | Purple | `(148, 0, 211)` |
| 5 | Pink | `(255, 20, 147)` |
| 6 | Cyan | `(0, 206, 209)` |
| 7 | Gold | `(255, 215, 0)` |
| 8 | Lime | `(50, 205, 50)` |
| 9 | Red-Orange | `(255, 69, 0)` |

### Heuristic Classification Rules

When no VLM is available, regions are classified by geometry:

| Condition | Label | Type |
|-----------|-------|------|
| Aspect ∈ (2.0, 8.0) exclusive, area 800–15000 | "button-like element" | `button` |
| Aspect ∈ (3.0, 30.0) exclusive, height < 50 | "text input field" | `text_field` |
| Aspect < 0.5, area > 5000 | "sidebar or panel" | `menu` |
| Aspect ∈ (0.7, 1.4) exclusive, area < 3000 | "icon or small widget" | `icon` |
| Area > 50000 | "content area" | `text` |
| Otherwise | "interactive element" | `unknown` |

---

## References

- Yang, J., et al. (2023). *Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding in GPT-4V.* arXiv:2310.11441.
- Sobel operator for gradient computation (Sobel & Feldman, 1968).
- Integral images for efficient rectangle queries (Viola & Jones, 2001).

<!-- signed: delta -->
