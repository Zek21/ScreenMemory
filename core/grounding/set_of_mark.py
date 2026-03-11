"""
Set-of-Mark (SoM) Visual Grounding Engine.

Overlays numbered markers and bounding boxes onto screenshots,
enabling spatial interaction with UI elements based on visual position
rather than DOM/accessibility tree parsing.

This is the bridge between "seeing" an interface and "knowing where to click."

Architecture:
    1. Screenshot → Edge/Contour Detection → Region Proposals
    2. Region Proposals → Filtering (min size, merge overlaps)
    3. Filtered Regions → Numbered Marker Overlay
    4. Marked Screenshot → VLM Analysis → Action Selection
    5. Selected Mark ID → Click Coordinates

Reference: "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding"
           (Yang et al., 2023) — adapted for desktop UI navigation.
"""
import io
import math
import logging
from typing import List, Tuple, Optional
from dataclasses import dataclass, field
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class UIRegion:
    """A detected interactive region on screen."""
    id: int
    x: int
    y: int
    width: int
    height: int
    center_x: int = 0
    center_y: int = 0
    label: str = ""
    confidence: float = 0.0
    region_type: str = "unknown"  # button, text_field, link, menu, tab, icon

    def __post_init__(self):
        self.center_x = self.x + self.width // 2
        self.center_y = self.y + self.height // 2

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class GroundedScreenshot:
    """Screenshot with overlaid markers and detected regions."""
    original: Image.Image
    marked: Image.Image
    regions: List[UIRegion]
    timestamp: float = 0.0

    def get_region(self, mark_id: int) -> Optional[UIRegion]:
        """Get region by its marker ID."""
        for r in self.regions:
            if r.id == mark_id:
                return r
        return None

    def get_click_coords(self, mark_id: int) -> Optional[Tuple[int, int]]:
        """Get click coordinates for a marker ID."""
        region = self.get_region(mark_id)
        if region:
            return (region.center_x, region.center_y)
        return None

    def find_by_label(self, query: str) -> List[UIRegion]:
        """Find regions whose label matches a natural language query."""
        query_lower = query.lower()
        matches = []
        for r in self.regions:
            if not r.label:
                continue
            # Check for substring match or keyword overlap
            if query_lower in r.label or r.label in query_lower:
                matches.append(r)
            else:
                q_words = set(query_lower.split())
                l_words = set(r.label.split())
                if q_words & l_words:
                    matches.append(r)
        return matches

    def find_by_type(self, region_type: str) -> List[UIRegion]:
        """Find all regions of a specific type."""
        return [r for r in self.regions if r.region_type == region_type]


# Marker colors — high contrast, distinguishable
MARKER_COLORS = [
    (255, 0, 0),      # Red
    (0, 150, 255),    # Blue
    (0, 200, 0),      # Green
    (255, 165, 0),    # Orange
    (148, 0, 211),    # Purple
    (255, 20, 147),   # Pink
    (0, 206, 209),    # Cyan
    (255, 215, 0),    # Gold
    (50, 205, 50),    # Lime
    (255, 69, 0),     # Red-Orange
]


class SetOfMarkGrounding:
    """
    Visual grounding engine that detects interactive UI regions and overlays
    numbered markers for VLM-guided spatial interaction.

    Pipeline:
        screenshot → detect_regions → overlay_markers → grounded_screenshot
        
    The VLM then sees the marked screenshot and can say "click mark 7"
    which resolves to precise pixel coordinates.
    """

    def __init__(self, min_region_size: int = 400, max_regions: int = 30,
                 merge_threshold: float = 0.5, edge_sensitivity: int = 50):
        """
        Args:
            min_region_size: Minimum area (px²) for a region to be considered
            max_regions: Maximum number of markers to overlay
            merge_threshold: IoU threshold for merging overlapping regions
            edge_sensitivity: Canny edge detection sensitivity (lower = more edges)
        """
        self.min_region_size = min_region_size
        self.max_regions = max_regions
        self.merge_threshold = merge_threshold
        self.edge_sensitivity = edge_sensitivity

    def ground(self, screenshot: Image.Image) -> GroundedScreenshot:
        """
        Full grounding pipeline: detect regions → overlay markers → return grounded screenshot.
        
        LOG FORMAT:
            [GROUNDING] detect_regions — found=47 raw, filtered=23, merged=18
            [GROUNDING] overlay_markers — marked 18 regions on 1920x1080 screenshot
        """
        # Step 1: Detect interactive regions
        raw_regions = self._detect_regions(screenshot)
        logger.info(f"Raw regions detected: {len(raw_regions)}")

        # Step 2: Filter by size and position
        filtered = self._filter_regions(raw_regions)
        logger.info(f"After filtering: {len(filtered)}")

        # Step 3: Merge overlapping regions
        merged = self._merge_overlapping(filtered)
        logger.info(f"After merging: {len(merged)}")

        # Step 4: Assign IDs and limit count
        regions = merged[:self.max_regions]
        for i, r in enumerate(regions):
            r.id = i + 1

        # Step 5: Overlay markers
        marked = self._overlay_markers(screenshot, regions)

        logger.info(f"Grounded screenshot: {len(regions)} markers on {screenshot.width}x{screenshot.height}")

        return GroundedScreenshot(
            original=screenshot,
            marked=marked,
            regions=regions,
        )

    def _detect_regions(self, image: Image.Image) -> List[UIRegion]:
        """
        Detect interactive UI regions using edge detection and contour analysis.
        
        Strategy: Convert to grayscale → Apply Canny-like edge detection →
        Find contour bounding boxes → Each bbox is a potential UI element.
        
        This works because UI elements (buttons, text fields, tabs, menus)
        have well-defined edges that contrast with their background.
        """
        # Convert to grayscale numpy array
        gray = np.array(image.convert("L"), dtype=np.float64)
        h, w = gray.shape

        # Compute gradients (Sobel-like)
        # Horizontal gradient
        gx = np.zeros_like(gray)
        gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]

        # Vertical gradient
        gy = np.zeros_like(gray)
        gy[1:-1, :] = gray[2:, :] - gray[:-2, :]

        # Edge magnitude
        magnitude = np.sqrt(gx**2 + gy**2)

        # Threshold to binary edge map
        threshold = np.percentile(magnitude, 100 - self.edge_sensitivity)
        edges = (magnitude > threshold).astype(np.uint8)

        # Find connected components (simple flood-fill approach)
        regions = self._find_contour_bboxes(edges, w, h)

        return regions

    def _find_contour_bboxes(self, edges: np.ndarray, w: int, h: int) -> List[UIRegion]:
        """
        Find bounding boxes of edge clusters using integral-image acceleration.
        Uses precomputed integral image for O(1) density queries per cell,
        then non-overlapping grid scan to cap raw region count.
        """
        regions = []
        cell_h = max(24, h // 30)
        cell_w = max(24, w // 40)

        # Integral image for O(1) rectangle sums
        integral = np.cumsum(np.cumsum(edges.astype(np.int32), axis=0), axis=1)

        def rect_sum(y1, x1, y2, x2):
            s = int(integral[y2 - 1, x2 - 1])
            if y1 > 0: s -= int(integral[y1 - 1, x2 - 1])
            if x1 > 0: s -= int(integral[y2 - 1, x1 - 1])
            if y1 > 0 and x1 > 0: s += int(integral[y1 - 1, x1 - 1])
            return s

        # Non-overlapping grid scan (stride = cell size, not half)
        for row in range(0, h - cell_h, cell_h):
            for col in range(0, w - cell_w, cell_w):
                area = cell_h * cell_w
                edge_count = rect_sum(row, col, row + cell_h, col + cell_w)
                density = edge_count / area

                if density > 0.06:
                    bbox = self._expand_region(edges, col, row, cell_w, cell_h, w, h)
                    if bbox:
                        x1, y1, x2, y2 = bbox
                        rw, rh = x2 - x1, y2 - y1
                        if rw * rh >= self.min_region_size:
                            regions.append(UIRegion(
                                id=0, x=x1, y=y1, width=rw, height=rh,
                                confidence=density,
                            ))

        return regions

    def _expand_region(self, edges: np.ndarray, x: int, y: int,
                       cw: int, ch: int, w: int, h: int) -> Optional[Tuple[int, int, int, int]]:
        """Expand a seed region to encompass the full edge cluster."""
        # Look for edges in the neighborhood
        margin = 10
        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(w, x + cw + margin)
        y2 = min(h, y + ch + margin)

        region = edges[y1:y2, x1:x2]
        if region.sum() < 3:
            return None

        # Find tight bounding box of edges
        rows = np.any(region, axis=1)
        cols = np.any(region, axis=0)

        if not rows.any() or not cols.any():
            return None

        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]

        return (x1 + cmin, y1 + rmin, x1 + cmax + 1, y1 + rmax + 1)

    def _filter_regions(self, regions: List[UIRegion]) -> List[UIRegion]:
        """
        Filter regions by:
        - Minimum area
        - Aspect ratio (reject very thin or very wide regions)
        - Position (reject regions at extreme edges)
        """
        filtered = []
        for r in regions:
            # Size filter
            if r.area < self.min_region_size:
                continue

            # Aspect ratio filter (reject >20:1 or <1:20)
            aspect = r.width / max(r.height, 1)
            if aspect > 20 or aspect < 0.05:
                continue

            # Extremely large regions are likely background
            if r.area > (r.width + r.height) * 500:
                continue

            filtered.append(r)

        # Sort by y then x (reading order)
        filtered.sort(key=lambda r: (r.y // 30, r.x))

        return filtered

    def _merge_overlapping(self, regions: List[UIRegion]) -> List[UIRegion]:
        """Merge overlapping regions using spatial grid index to avoid O(n^2)."""
        if not regions:
            return []

        grid_size = 80
        grid = self._build_spatial_grid(regions, grid_size)
        return self._merge_with_grid(regions, grid, grid_size)

    @staticmethod
    def _build_spatial_grid(regions: List[UIRegion], grid_size: int) -> dict:
        """Build a spatial grid index for fast neighbor lookup."""
        grid = {}
        for i, r in enumerate(regions):
            cx1, cy1 = r.x // grid_size, r.y // grid_size
            cx2, cy2 = (r.x + r.width) // grid_size, (r.y + r.height) // grid_size
            for gy in range(cy1, cy2 + 1):
                for gx in range(cx1, cx2 + 1):
                    grid.setdefault((gx, gy), []).append(i)
        return grid

    def _merge_with_grid(self, regions: List[UIRegion], grid: dict, grid_size: int) -> List[UIRegion]:
        """Merge overlapping region groups using grid-accelerated neighbor lookup."""
        merged = []
        used = set()

        for i, r1 in enumerate(regions):
            if i in used:
                continue

            candidates = self._grid_neighbors(r1, grid, grid_size, i, used)
            group = [r1]
            for j in candidates:
                if self._iou(r1, regions[j]) > self.merge_threshold:
                    group.append(regions[j])
                    used.add(j)

            x1 = min(r.x for r in group)
            y1 = min(r.y for r in group)
            x2 = max(r.x + r.width for r in group)
            y2 = max(r.y + r.height for r in group)
            avg_conf = sum(r.confidence for r in group) / len(group)
            merged.append(UIRegion(id=0, x=x1, y=y1, width=x2 - x1, height=y2 - y1, confidence=avg_conf))
            used.add(i)

        return merged

    @staticmethod
    def _grid_neighbors(region: UIRegion, grid: dict, grid_size: int, idx: int, used: set) -> set:
        """Find candidate neighbor indices from the spatial grid."""
        cx1, cy1 = region.x // grid_size, region.y // grid_size
        cx2, cy2 = (region.x + region.width) // grid_size, (region.y + region.height) // grid_size
        candidates = set()
        for gy in range(cy1, cy2 + 1):
            for gx in range(cx1, cx2 + 1):
                for j in grid.get((gx, gy), []):
                    if j > idx and j not in used:
                        candidates.add(j)
        return candidates

    def _iou(self, r1: UIRegion, r2: UIRegion) -> float:
        """Compute Intersection over Union of two regions."""
        x1 = max(r1.x, r2.x)
        y1 = max(r1.y, r2.y)
        x2 = min(r1.x + r1.width, r2.x + r2.width)
        y2 = min(r1.y + r1.height, r2.y + r2.height)

        if x2 <= x1 or y2 <= y1:
            return 0.0

        intersection = (x2 - x1) * (y2 - y1)
        union = r1.area + r2.area - intersection
        return intersection / max(union, 1)

    def _overlay_markers(self, image: Image.Image, regions: List[UIRegion]) -> Image.Image:
        """
        Overlay numbered markers and bounding boxes onto the screenshot.
        Each region gets:
        - A colored bounding box (2px border)
        - A numbered circle marker at top-left corner
        """
        marked = image.copy()
        draw = ImageDraw.Draw(marked)

        # Try to get a readable font
        try:
            font = ImageFont.truetype("arial.ttf", 14)
            small_font = ImageFont.truetype("arial.ttf", 11)
        except (OSError, IOError):
            font = ImageFont.load_default()
            small_font = font

        for region in regions:
            color = MARKER_COLORS[(region.id - 1) % len(MARKER_COLORS)]

            # Draw bounding box
            draw.rectangle(region.bbox, outline=color, width=2)

            # Draw marker circle with number
            marker_r = 12
            mx = region.x - 2
            my = region.y - marker_r * 2 - 2
            if my < 0:
                my = region.y + 2

            # Circle background
            draw.ellipse(
                [mx, my, mx + marker_r * 2, my + marker_r * 2],
                fill=color, outline=(255, 255, 255), width=1,
            )

            # Number text (centered in circle)
            text = str(region.id)
            text_bbox = draw.textbbox((0, 0), text, font=small_font)
            tw = text_bbox[2] - text_bbox[0]
            th = text_bbox[3] - text_bbox[1]
            tx = mx + marker_r - tw // 2
            ty = my + marker_r - th // 2 - 1
            draw.text((tx, ty), text, fill=(255, 255, 255), font=small_font)

        return marked

    def ground_with_description(self, screenshot: Image.Image,
                                 analyzer=None) -> Tuple[GroundedScreenshot, str]:
        """
        Full grounding + VLM description of the marked screenshot.
        Returns grounded screenshot and VLM's description of visible elements.
        """
        grounded = self.ground(screenshot)

        description = ""
        if analyzer and analyzer.is_available:
            # Use the marked image for VLM analysis
            prompt = (
                "This screenshot has numbered markers (colored circles with numbers) "
                "overlaid on interactive UI elements. For each numbered marker, briefly "
                "describe what UI element it marks (e.g., '1: Search button', '3: URL bar', "
                "'7: Close tab button'). List only the markers you can see."
            )
            result = analyzer.analyze(grounded.marked, detailed=False)
            if result:
                description = result.description

        return grounded, description

    def ground_semantic(self, screenshot: Image.Image,
                        vlm_query_fn=None, batch=True) -> GroundedScreenshot:
        """
        Grounding with VLM semantic labeling.

        Two modes:
        - batch=True (default): Send the full marked screenshot to VLM once,
          parse labels from response. ~20s total (1 VLM call).
        - batch=False: Crop each region individually and query VLM per-region.
          More accurate but ~20s * N regions.

        Args:
            screenshot: Raw screenshot to ground.
            vlm_query_fn: Callable(image: Image.Image, prompt: str) -> str.
                          If None, uses heuristic classification.
            batch: If True, uses single VLM call for all regions.

        Returns:
            GroundedScreenshot with semantically labeled regions.
        """
        grounded = self.ground(screenshot)

        if vlm_query_fn and batch:
            # Single VLM call on the marked screenshot
            return self._batch_vlm_label(grounded, vlm_query_fn)

        for region in grounded.regions:
            if vlm_query_fn and not batch:
                # Per-region cropping
                pad = 8
                x1 = max(0, region.x - pad)
                y1 = max(0, region.y - pad)
                x2 = min(screenshot.width, region.x + region.width + pad)
                y2 = min(screenshot.height, region.y + region.height + pad)
                crop = screenshot.crop((x1, y1, x2, y2))
                try:
                    label = vlm_query_fn(
                        crop,
                        "What UI element is this? Reply in 2-4 words only "
                        "(e.g., 'search button', 'text input', 'close icon', "
                        "'navigation menu', 'tab bar')."
                    )
                    region.label = label.strip().lower()[:60]
                    region.region_type = self._classify_from_label(region.label)
                except Exception:
                    region.label, region.region_type = self._heuristic_classify(region)
            else:
                region.label, region.region_type = self._heuristic_classify(region)

        return grounded

    def _batch_vlm_label(self, grounded: GroundedScreenshot,
                         vlm_query_fn) -> GroundedScreenshot:
        """Label all regions with a single VLM call on the marked screenshot."""
        import re
        mark_ids = [str(r.id) for r in grounded.regions]
        prompt = (
            f"I see numbered colored circles ({', '.join(mark_ids)}) on this screenshot. "
            "Each circle marks a UI element. "
            "For each number, tell me what the UI element is. "
            "Use this exact format:\n"
            "1: description\n2: description\n"
            "Keep each description to 2-5 words."
        )
        try:
            response = vlm_query_fn(grounded.marked, prompt)
            # Parse "N: description" or "N. description" or "N - description"
            labels = {}
            for line in response.strip().split('\n'):
                m = re.match(r'(\d+)\s*[:\-\.\)]\s*(.+)', line.strip())
                if m:
                    mark_id = int(m.group(1))
                    label = m.group(2).strip(' "\'').lower()[:60]
                    if label and len(label) > 1:
                        labels[mark_id] = label

            for region in grounded.regions:
                if region.id in labels:
                    region.label = labels[region.id]
                    region.region_type = self._classify_from_label(region.label)
                else:
                    region.label, region.region_type = self._heuristic_classify(region)
        except Exception:
            for region in grounded.regions:
                region.label, region.region_type = self._heuristic_classify(region)

        return grounded

    def _classify_from_label(self, label: str) -> str:
        """Map a VLM label to a canonical region_type."""
        label_lower = label.lower()
        type_keywords = {
            "button": ["button", "btn", "submit", "click", "send", "ok", "cancel"],
            "text_field": ["input", "text field", "text box", "search bar", "textarea"],
            "link": ["link", "hyperlink", "url", "anchor"],
            "menu": ["menu", "dropdown", "select", "combobox", "navigation"],
            "tab": ["tab", "tab bar"],
            "icon": ["icon", "close", "minimize", "maximize", "logo"],
            "image": ["image", "photo", "picture", "thumbnail"],
            "text": ["text", "label", "heading", "title", "paragraph"],
        }
        for rtype, keywords in type_keywords.items():
            if any(kw in label_lower for kw in keywords):
                return rtype
        return "unknown"

    def _heuristic_classify(self, region: UIRegion) -> Tuple[str, str]:
        """Classify a region by geometry when no VLM is available."""
        aspect = region.width / max(region.height, 1)
        area = region.area

        if 2.0 < aspect < 8.0 and 800 < area < 15000:
            return "button-like element", "button"
        elif 3.0 < aspect < 30.0 and region.height < 50:
            return "text input field", "text_field"
        elif aspect < 0.5 and area > 5000:
            return "sidebar or panel", "menu"
        elif 0.7 < aspect < 1.4 and area < 3000:
            return "icon or small widget", "icon"
        elif area > 50000:
            return "content area", "text"
        else:
            return "interactive element", "unknown"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from PIL import ImageGrab
    import time

    print("=== Set-of-Mark Visual Grounding Test ===\n")

    grounder = SetOfMarkGrounding(min_region_size=300, max_regions=25)

    # Capture current screen
    img = ImageGrab.grab()
    print(f"Screenshot: {img.width}x{img.height}")

    # Ground it
    start = time.perf_counter()
    grounded = grounder.ground(img)
    elapsed = (time.perf_counter() - start) * 1000

    print(f"Grounding: {elapsed:.0f}ms")
    print(f"Regions found: {len(grounded.regions)}")

    for r in grounded.regions[:10]:
        print(f"  Mark {r.id}: ({r.x},{r.y}) {r.width}x{r.height} center=({r.center_x},{r.center_y})")

    # Save marked screenshot
    grounded.marked.save("logs/grounded_test.png")
    print(f"\nMarked screenshot saved to logs/grounded_test.png")
