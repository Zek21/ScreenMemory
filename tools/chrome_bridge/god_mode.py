"""
╔══════════════════════════════════════════════════════════════════════════╗
║                         G O D   M O D E                                 ║
║                                                                          ║
║  Structural Perception Engine for Digital Environments                    ║
║  Zero-pixel, zero-screenshot, mathematically precise navigation          ║
║                                                                          ║
║  Based on: "The Invisible Interface: Conceptual and Spatial Perception   ║
║  of Digital Environments in AI Systems"                                  ║
║                                                                          ║
║  Architecture Layers:                                                    ║
║    1. Accessibility Tree Parser (AOM) — semantic identity                ║
║    2. Semantic Geometry Engine — bounding boxes + normalization           ║
║    3. Occlusion Resolver — z-index stacking contexts + visibility        ║
║    4. Element Embeddings — vector similarity (Screen2Vec-inspired)       ║
║    5. Page Topology Graph — GNN-inspired relational mapping              ║
║    6. Action Space Optimizer — 100k→1.4k token compression              ║
║    7. Spatial Reasoner — gestalt grouping + alignment detection           ║
║    8. GodMode Controller — unified orchestrator                          ║
║                                                                          ║
║  Rules:                                                                  ║
║    • NEVER touch physical mouse or keyboard                              ║
║    • Chrome → CDP Input domain only                                      ║
║    • Win32 → PostMessage/SendMessage only                                ║
║    • All perception = structural code analysis, never pixels             ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import json
import math
import time
import hashlib
import re
import os
import sys
import logging
from collections import defaultdict
from typing import Optional, List, Dict, Tuple, Any, Set

logger = logging.getLogger('god_mode')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from cdp import CDP, CDPError
except ImportError as e:
    logger.error(f"CDP module not found: {e}")
    raise

try:
    from perception import (
        SpatialNode, SpatialGrid, TopologicalMemory,
        Win32Scanner, UIAScanner, CDPPerception, PerceptionEngine
    )
except ImportError as e:
    logger.error(f"Perception module not found: {e}")
    raise


# ═══════════════════════════════════════════════════════════════════════
# MODULE 1: ACCESSIBILITY TREE ENHANCED PARSER
# ═══════════════════════════════════════════════════════════════════════

class AccessibilityTreeParser:
    """
    Parses Chrome's Computed Accessibility Tree (AOM) into a clean,
    machine-readable hierarchy. Strips all non-semantic noise.

    The AOM provides: roles, names, descriptions, states (aria-expanded, etc.)
    This is the browser-computed distilled view — no DOM bloat.

    Reduces 100k+ DOM tokens to ~1400 semantic tokens.
    """

    # Roles that represent actionable/meaningful elements
    ACTIONABLE_ROLES = frozenset({
        'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox',
        'menuitem', 'tab', 'switch', 'slider', 'spinbutton', 'searchbox',
        'option', 'menuitemcheckbox', 'menuitemradio', 'treeitem',
    })

    # Roles that provide structural/semantic meaning
    SEMANTIC_ROLES = frozenset({
        'heading', 'navigation', 'main', 'banner', 'contentinfo',
        'complementary', 'form', 'search', 'region', 'article',
        'list', 'listitem', 'table', 'row', 'cell', 'columnheader',
        'rowheader', 'dialog', 'alertdialog', 'alert', 'status',
        'toolbar', 'menu', 'menubar', 'tablist', 'tabpanel',
        'tree', 'grid', 'group', 'img', 'figure',
    })

    # Roles to skip entirely (pure layout/noise)
    SKIP_ROLES = frozenset({
        'none', 'presentation', 'generic', 'LineBreak',
    })

    def __init__(self, cdp: CDP):
        self.cdp = cdp

    def _parse_node(self, node: Dict) -> Optional[Dict]:
        """Parse a single accessibility tree node. Returns None if noise."""
        role = self._extract_value(node.get('role', {}))
        name = self._extract_value(node.get('name', {}))
        desc = self._extract_value(node.get('description', {}))

        if role in self.SKIP_ROLES:
            return None
        if not role and not name:
            return None
        if role == 'generic' and not name:
            return None

        states = {}
        for prop in node.get('properties', []):
            prop_name = prop.get('name', '')
            prop_val = self._extract_value(prop.get('value', {}))
            if prop_val is not None and prop_val != '':
                states[prop_name] = prop_val

        return {
            'ax_id': node.get('nodeId', ''),
            'backend_node_id': node.get('backendDOMNodeId'),
            'role': role,
            'name': name,
            'description': desc,
            'states': states,
            'actionable': role in self.ACTIONABLE_ROLES,
            'children_ids': node.get('childIds', []),
            'parent_id': node.get('parentId'),
        }

    def parse(self, tab_id: str) -> List[Dict]:
        """
        Extract the full accessibility tree and return a clean,
        hierarchical list of meaningful nodes.
        """
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']

        try:
            raw_nodes = self.cdp.accessibility_tree(tab_id)
        except CDPError as e:
            logger.warning(f"CDP accessibility_tree failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in accessibility_tree: {e}")
            return []

        parsed = []
        for node in raw_nodes:
            entry = self._parse_node(node)
            if entry is not None:
                parsed.append(entry)
        return parsed

    def parse_compact(self, tab_id: str) -> str:
        """
        Generate an ultra-compact YAML-like representation of the page.
        This is what gets fed to the LLM — minimal tokens, maximum signal.

        Format:
            [ref] role "name" {state1, state2}
              [ref] child_role "child_name"
        """
        nodes = self.parse(tab_id)
        if not nodes:
            return "# Empty page"

        lines = []
        for n in nodes:
            if not n['role'] or n['role'] in self.SKIP_ROLES:
                continue

            role = n['role']
            name = n['name']
            if not name and role not in self.SEMANTIC_ROLES:
                continue

            # Build compact line
            ref = n['ax_id'][:6] if n['ax_id'] else '?'
            states_str = ''
            if n['states']:
                important_states = {k: v for k, v in n['states'].items()
                                    if k in ('expanded', 'checked', 'selected',
                                             'disabled', 'required', 'invalid',
                                             'pressed', 'focused')}
                if important_states:
                    states_str = ' {' + ', '.join(
                        f'{k}={v}' for k, v in important_states.items()
                    ) + '}'

            marker = '●' if n['actionable'] else '○'
            name_str = f' "{name}"' if name else ''
            lines.append(f'{marker} [{ref}] {role}{name_str}{states_str}')

        return '\n'.join(lines)

    def find_actionable(self, tab_id: str) -> List[Dict]:
        """Return only actionable elements from the accessibility tree."""
        return [n for n in self.parse(tab_id) if n['actionable']]

    @staticmethod
    def _extract_value(obj):
        if isinstance(obj, dict):
            return obj.get('value', '')
        return str(obj) if obj else ''


# ═══════════════════════════════════════════════════════════════════════
# MODULE 2: SEMANTIC GEOMETRY ENGINE
# ═══════════════════════════════════════════════════════════════════════

class SemanticGeometryEngine:
    """
    Computes precise bounding boxes, normalizes coordinates, and extracts
    visual prominence scores — all without screenshots.

    Bridges the gap between code and spatial layout:
    - Absolute coordinate system (origin = top-left of viewport)
    - Normalized grid (0-1000 on both axes)
    - Visual prominence scoring (size, position, contrast)
    - Spatial filtering by region/role
    """

    # JavaScript injection for extracting complete geometric data
    GEOMETRY_EXTRACTION_JS = """
    (function() {
        var vw = window.innerWidth || document.documentElement.clientWidth;
        var vh = window.innerHeight || document.documentElement.clientHeight;
        var scrollX = window.scrollX || window.pageXOffset;
        var scrollY = window.scrollY || window.pageYOffset;
        var elements = [];
        var interactable = 'A,BUTTON,INPUT,SELECT,TEXTAREA,[role=button],[role=link],' +
                          '[role=checkbox],[role=radio],[role=textbox],[role=combobox],' +
                          '[role=menuitem],[role=tab],[role=switch],[onclick],[tabindex]';
        var all = document.querySelectorAll(interactable);

        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            var rect = el.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) continue;

            // Compute visibility
            var style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (parseFloat(style.opacity) < 0.1) continue;

            // Get semantic properties
            var tag = el.tagName.toLowerCase();
            var role = el.getAttribute('role') || tag;
            var name = el.getAttribute('aria-label') ||
                       el.getAttribute('title') ||
                       el.getAttribute('alt') ||
                       el.getAttribute('placeholder') ||
                       (el.innerText || '').substring(0, 80).trim();
            var ariaDesc = el.getAttribute('aria-describedby') || '';

            // Compute visual prominence
            var area = rect.width * rect.height;
            var viewportArea = vw * vh;
            var areaRatio = area / viewportArea;

            var fontSize = parseFloat(style.fontSize) || 14;
            var fontWeight = parseInt(style.fontWeight) || 400;
            var bgColor = style.backgroundColor;
            var color = style.color;

            // Z-index and stacking
            var zIndex = 0;
            var current = el;
            while (current && current !== document.documentElement) {
                var cs = window.getComputedStyle(current);
                var z = parseInt(cs.zIndex);
                if (!isNaN(z)) { zIndex = Math.max(zIndex, z); }
                current = current.parentElement;
            }

            // Position classification
            var position = style.position;
            var isFixed = position === 'fixed' || position === 'sticky';

            elements.push({
                tag: tag,
                role: role,
                name: name,
                desc: ariaDesc,
                // Absolute coordinates (viewport-relative)
                x: Math.round(rect.left),
                y: Math.round(rect.top),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                // Normalized coordinates (0-1000 grid)
                nx: Math.round((rect.left / vw) * 1000),
                ny: Math.round((rect.top / vh) * 1000),
                nw: Math.round((rect.width / vw) * 1000),
                nh: Math.round((rect.height / vh) * 1000),
                // Visual properties
                z: zIndex,
                opacity: parseFloat(style.opacity),
                fontSize: fontSize,
                fontWeight: fontWeight,
                areaRatio: Math.round(areaRatio * 10000) / 10000,
                isFixed: isFixed,
                // State
                type: el.type || '',
                value: (el.value || '').substring(0, 100),
                checked: el.checked || false,
                disabled: el.disabled || false,
                href: el.href || '',
                id: el.id || '',
                cls: (el.className || '').toString().substring(0, 60),
            });
        }

        return JSON.stringify({
            viewport: { w: vw, h: vh },
            scroll: { x: scrollX, y: scrollY },
            totalElements: document.querySelectorAll('*').length,
            actionableElements: elements.length,
            elements: elements
        });
    })()
    """

    def __init__(self, cdp: CDP):
        self.cdp = cdp
        self._cache = {}
        self._cache_ttl = 5  # seconds

    def extract(self, tab_id: str) -> Dict:
        """
        Extract complete geometric data for all interactable elements.
        Returns viewport info + list of elements with absolute and
        normalized coordinates.
        """
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']

        raw = self.cdp.eval(tab_id, self.GEOMETRY_EXTRACTION_JS)
        if not raw:
            return {'viewport': {'w': 0, 'h': 0}, 'elements': []}

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {'viewport': {'w': 0, 'h': 0}, 'elements': []}

        # Score visual prominence
        viewport = data.get('viewport', {'w': 0, 'h': 0})
        for el in data.get('elements', []):
            el['prominence'] = self._compute_prominence(el, viewport)

        return data

    @staticmethod
    def _compress_element(i: int, el: Dict) -> Dict:
        """Compress a single element into a compact LLM-friendly dict."""
        entry = {
            'ref': i,
            'role': el['role'],
            'name': el['name'][:60] if el['name'] else '',
            'box': [el['nx'], el['ny'], el['nw'], el['nh']],
            'z': el['z'],
        }
        if el.get('value'):
            entry['val'] = el['value'][:50]
        if el.get('checked'):
            entry['checked'] = True
        if el.get('disabled'):
            entry['disabled'] = True
        if el.get('prominence', 0) > 0.3:
            entry['primary'] = True
        return entry

    def extract_grounded_action_space(self, tab_id: str,
                                       region: str = None,
                                       role_filter: List[str] = None,
                                       min_prominence: float = 0.0) -> str:
        """
        Generate the "grounded action space" -- a compact JSON representation
        optimized for LLM consumption (~1,400 tokens instead of ~100,000).
        """
        data = self.extract(tab_id)
        elements = data.get('elements', [])

        if region:
            elements = self._filter_by_region(elements, region, data['viewport'])
        if role_filter:
            elements = [e for e in elements if e['role'] in role_filter]
        if min_prominence > 0:
            elements = [e for e in elements if e.get('prominence', 0) >= min_prominence]

        compact = [self._compress_element(i, el) for i, el in enumerate(elements)]

        return json.dumps({
            'viewport': [data['viewport']['w'], data['viewport']['h']],
            'count': len(compact),
            'scene': compact
        }, separators=(',', ':'))

    def find_primary_cta(self, tab_id: str) -> Optional[Dict]:
        """Find the primary call-to-action on the page based on
        visual prominence scoring (size, position, contrast)."""
        data = self.extract(tab_id)
        elements = data.get('elements', [])
        if not elements:
            return None

        buttons = [e for e in elements if e['role'] in ('button', 'a', 'submit')]
        if not buttons:
            buttons = [e for e in elements if e.get('prominence', 0) > 0.3]

        if buttons:
            buttons.sort(key=lambda e: e.get('prominence', 0), reverse=True)
            return buttons[0]
        return None

    def spatial_clusters(self, tab_id: str, threshold=50) -> List[List[Dict]]:
        """Group elements into spatial clusters based on proximity."""
        data = self.extract(tab_id)
        elements = data.get('elements', [])
        if not elements:
            return []

        # Simple agglomerative clustering by center distance
        clusters = [[e] for e in elements]
        merged = True
        while merged:
            merged = False
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    if self._cluster_distance(clusters[i], clusters[j]) < threshold:
                        clusters[i].extend(clusters[j])
                        clusters.pop(j)
                        merged = True
                        break
                if merged:
                    break

        return [c for c in clusters if len(c) > 1]

    def _compute_prominence(self, el: Dict, viewport: Dict) -> float:
        """
        Compute visual prominence score (0.0 - 1.0) purely from
        computed CSS properties — no screenshots needed.
        """
        score = 0.0

        # Size factor (larger = more prominent)
        area_ratio = el.get('areaRatio', 0)
        score += min(area_ratio * 10, 0.3)

        # Center-bias (elements near viewport center are more prominent)
        cx = el['x'] + el['w'] / 2
        cy = el['y'] + el['h'] / 2
        vw, vh = viewport.get('w', 0), viewport.get('h', 0)
        dx = abs(cx - vw / 2) / max(vw / 2, 1)
        dy = abs(cy - vh / 2) / max(vh / 2, 1)
        center_factor = 1.0 - (dx + dy) / 2
        score += center_factor * 0.2

        # Font weight (bolder = more prominent)
        fw = el.get('fontWeight', 400)
        if fw >= 700:
            score += 0.15
        elif fw >= 600:
            score += 0.1

        # Font size (larger = more prominent)
        fs = el.get('fontSize', 14)
        if fs >= 20:
            score += 0.15
        elif fs >= 16:
            score += 0.1

        # Z-index (higher = more prominent)
        z = el.get('z', 0)
        if z > 100:
            score += 0.1
        elif z > 0:
            score += 0.05

        # Fixed position elements get slight boost
        if el.get('isFixed'):
            score += 0.05

        return min(score, 1.0)

    def _filter_by_region(self, elements: List[Dict], region: str,
                          viewport: Dict) -> List[Dict]:
        """Filter elements by viewport region."""
        vw, vh = viewport['w'], viewport['h']
        filters = {
            'top': lambda e: e['y'] < vh * 0.33,
            'bottom': lambda e: e['y'] + e['h'] > vh * 0.67,
            'left': lambda e: e['x'] < vw * 0.33,
            'right': lambda e: e['x'] + e['w'] > vw * 0.67,
            'center': lambda e: (vw * 0.25 < e['x'] + e['w']/2 < vw * 0.75 and
                                 vh * 0.25 < e['y'] + e['h']/2 < vh * 0.75),
        }
        fn = filters.get(region)
        return [e for e in elements if fn(e)] if fn else elements

    @staticmethod
    def _cluster_distance(c1: List[Dict], c2: List[Dict]) -> float:
        """Min distance between any two elements in two clusters."""
        min_d = float('inf')
        for a in c1:
            for b in c2:
                ax, ay = a['x'] + a['w']/2, a['y'] + a['h']/2
                bx, by = b['x'] + b['w']/2, b['y'] + b['h']/2
                d = math.sqrt((ax-bx)**2 + (ay-by)**2)
                min_d = min(min_d, d)
        return min_d


# ═══════════════════════════════════════════════════════════════════════
# MODULE 3: OCCLUSION RESOLVER
# ═══════════════════════════════════════════════════════════════════════

class OcclusionResolver:
    """
    Resolves depth, z-index, and CSS stacking contexts to determine
    which elements are genuinely visible and interactable.

    Simulates the browser's rendering engine z-axis logic:
    1. Visibility Verification (display, visibility, opacity)
    2. Stacking Context Evaluation (z-index hierarchy)
    3. Geometric Intersection Mapping (bounding box overlaps)
    4. Occlusion Calculation (overlap ratio computation)

    This eliminates the need for visual verification via screenshots.
    """

    OCCLUSION_JS = """
    (function() {
        var results = [];
        var interactable = document.querySelectorAll(
            'a,button,input,select,textarea,[role=button],[role=link],' +
            '[role=checkbox],[role=radio],[role=textbox],[onclick],[tabindex]'
        );

        for (var i = 0; i < interactable.length; i++) {
            var el = interactable[i];
            var rect = el.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) continue;

            var style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;

            // Check if element is truly visible using elementFromPoint
            var cx = rect.left + rect.width / 2;
            var cy = rect.top + rect.height / 2;
            var topEl = document.elementFromPoint(cx, cy);

            var occluded = false;
            var occluder = null;
            if (topEl && topEl !== el && !el.contains(topEl) && !topEl.contains(el)) {
                occluded = true;
                var topRect = topEl.getBoundingClientRect();
                occluder = {
                    tag: topEl.tagName.toLowerCase(),
                    role: topEl.getAttribute('role') || topEl.tagName.toLowerCase(),
                    name: (topEl.getAttribute('aria-label') ||
                           topEl.innerText || '').substring(0, 50),
                    box: [Math.round(topRect.left), Math.round(topRect.top),
                          Math.round(topRect.width), Math.round(topRect.height)],
                };
            }

            // Check multiple sample points for partial occlusion
            var samplePoints = [
                [rect.left + 5, rect.top + 5],
                [rect.right - 5, rect.top + 5],
                [rect.left + 5, rect.bottom - 5],
                [rect.right - 5, rect.bottom - 5],
                [cx, cy]
            ];
            var visiblePoints = 0;
            for (var j = 0; j < samplePoints.length; j++) {
                var px = samplePoints[j][0], py = samplePoints[j][1];
                if (px >= 0 && py >= 0 &&
                    px < window.innerWidth && py < window.innerHeight) {
                    var hitEl = document.elementFromPoint(px, py);
                    if (hitEl === el || el.contains(hitEl)) visiblePoints++;
                }
            }
            var visibilityRatio = visiblePoints / samplePoints.length;

            // Get effective z-index
            var effectiveZ = 0;
            var curr = el;
            while (curr && curr !== document.documentElement) {
                var cs = window.getComputedStyle(curr);
                var z = parseInt(cs.zIndex);
                if (!isNaN(z) && cs.position !== 'static') {
                    effectiveZ = z;
                    break;
                }
                curr = curr.parentElement;
            }

            // Detect if inside a modal/dialog/overlay
            var inModal = false;
            curr = el;
            while (curr && curr !== document.body) {
                var cr = curr.getAttribute('role');
                if (cr === 'dialog' || cr === 'alertdialog' ||
                    curr.tagName === 'DIALOG' ||
                    curr.classList.contains('modal') ||
                    curr.classList.contains('overlay')) {
                    inModal = true;
                    break;
                }
                curr = curr.parentElement;
            }

            var tag = el.tagName.toLowerCase();
            results.push({
                tag: tag,
                role: el.getAttribute('role') || tag,
                name: (el.getAttribute('aria-label') ||
                       el.innerText || '').substring(0, 60),
                box: [Math.round(rect.left), Math.round(rect.top),
                      Math.round(rect.width), Math.round(rect.height)],
                z: effectiveZ,
                occluded: occluded,
                occluder: occluder,
                visibilityRatio: Math.round(visibilityRatio * 100) / 100,
                inModal: inModal,
                interactable: visibilityRatio >= 0.4 && !el.disabled,
                id: el.id || '',
            });
        }

        return JSON.stringify(results);
    })()
    """

    def __init__(self, cdp: CDP):
        self.cdp = cdp

    def resolve(self, tab_id: str) -> Dict:
        """
        Compute full occlusion analysis for all interactable elements.

        Returns:
            {
                'total': int,
                'visible': int,
                'occluded': int,
                'partially_occluded': int,
                'in_modal': bool,
                'elements': [...],
                'modal_elements': [...],
            }
        """
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']

        raw = self.cdp.eval(tab_id, self.OCCLUSION_JS)
        if not raw:
            return {'total': 0, 'visible': 0, 'elements': []}

        try:
            elements = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {'total': 0, 'visible': 0, 'elements': []}

        visible = [e for e in elements if e.get('visibilityRatio', 0) >= 0.8]
        occluded = [e for e in elements if e.get('occluded')]
        partial = [e for e in elements
                   if 0.2 < e.get('visibilityRatio', 0) < 0.8]
        modal_els = [e for e in elements if e.get('inModal')]
        has_modal = bool(modal_els)

        return {
            'total': len(elements),
            'visible': len(visible),
            'occluded': len(occluded),
            'partially_occluded': len(partial),
            'has_modal': has_modal,
            'elements': elements,
            'visible_elements': visible,
            'modal_elements': modal_els,
        }

    def get_truly_interactable(self, tab_id: str) -> List[Dict]:
        """
        Return ONLY elements that are genuinely clickable/interactable.
        Filters out occluded, disabled, and invisible elements.
        This is the "culled action space" described in the research.
        """
        result = self.resolve(tab_id)
        return [e for e in result.get('elements', [])
                if e.get('interactable', False)]

    _OVERLAY_DETECT_JS = """
    (function() {
        var overlays = [];
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            var style = window.getComputedStyle(el);
            var role = el.getAttribute('role') || '';
            var isOverlay = false;
            var reason = '';

            if (role === 'dialog' || role === 'alertdialog' || el.tagName === 'DIALOG') {
                isOverlay = true; reason = 'dialog';
            } else if (style.position === 'fixed' || style.position === 'sticky') {
                var rect = el.getBoundingClientRect();
                var area = rect.width * rect.height;
                var viewArea = window.innerWidth * window.innerHeight;
                if (area > viewArea * 0.3) {
                    isOverlay = true; reason = 'large-fixed';
                }
                if (parseInt(style.zIndex) > 1000 && area > viewArea * 0.1) {
                    isOverlay = true; reason = 'high-z-fixed';
                }
            }

            if (isOverlay) {
                var rect = el.getBoundingClientRect();
                overlays.push({
                    tag: el.tagName.toLowerCase(),
                    role: role,
                    reason: reason,
                    box: [Math.round(rect.left), Math.round(rect.top),
                          Math.round(rect.width), Math.round(rect.height)],
                    z: parseInt(style.zIndex) || 0,
                    text: (el.innerText || '').substring(0, 100),
                });
            }
        }
        return JSON.stringify(overlays);
    })()
    """

    def detect_overlays(self, tab_id: str) -> List[Dict]:
        """Detect modal dialogs, cookie banners, popups, etc."""
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']

        raw = self.cdp.eval(tab_id, self._OVERLAY_DETECT_JS)
        try:
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (json.JSONDecodeError, TypeError):
            return []


# ═══════════════════════════════════════════════════════════════════════
# MODULE 4: ELEMENT EMBEDDINGS (Screen2Vec-inspired)
# ═══════════════════════════════════════════════════════════════════════

class ElementEmbedding:
    """
    Pure-Python vector embedding system for UI elements.
    Inspired by Screen2Vec — no external ML models needed.

    Generates multi-dimensional vectors combining:
    - Lexical embedding (text content → hash-based vector)
    - Class embedding (role/tag → one-hot)
    - Spatial embedding (normalized bounding box)

    Enables semantic similarity search across any website.
    """

    # Role vocabulary for class embeddings
    ROLE_VOCAB = [
        'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox',
        'select', 'textarea', 'img', 'heading', 'list', 'listitem',
        'navigation', 'form', 'dialog', 'menu', 'menuitem', 'tab',
        'table', 'input', 'a', 'div', 'span', 'label',
    ]

    # Common UI concept keywords for lexical hashing
    CONCEPT_SEEDS = {
        'submit': ['submit', 'send', 'save', 'confirm', 'done', 'apply', 'post'],
        'cancel': ['cancel', 'close', 'dismiss', 'no', 'back', 'nevermind', 'abort', 'cancel order'],
        'search': ['search', 'find', 'lookup', 'query', 'filter'],
        'login': ['login', 'sign in', 'log in', 'signin', 'authenticate'],
        'register': ['register', 'sign up', 'signup', 'create account', 'join'],
        'cart': ['cart', 'basket', 'bag', 'checkout', 'add to cart'],
        'navigate': ['next', 'previous', 'forward', 'back', 'home', 'menu'],
        'delete': ['delete', 'remove', 'trash', 'discard', 'erase'],
        'edit': ['edit', 'modify', 'update', 'change', 'rename'],
        'settings': ['settings', 'preferences', 'options', 'configure', 'gear', 'config'],
        'ok': ['ok', 'okay', 'yes', 'accept', 'approve', 'allow'],
    }

    def __init__(self, lexical_dim=64, class_dim=24, spatial_dim=8):
        self.lexical_dim = lexical_dim
        self.class_dim = class_dim
        self.spatial_dim = spatial_dim
        self.total_dim = lexical_dim + class_dim + spatial_dim

        # Pre-compute concept vectors
        self._concept_vectors = {}
        for concept, keywords in self.CONCEPT_SEEDS.items():
            vectors = [self._text_to_hash_vector(kw) for kw in keywords]
            self._concept_vectors[concept] = self._mean_vector(vectors)

    def embed_element(self, element: Dict) -> List[float]:
        """
        Generate a unified vector embedding for a UI element.

        Components:
        - Lexical (32-D): Hash-based text embedding of name/label
        - Class (24-D): One-hot role encoding
        - Spatial (8-D): Normalized bounding box + area + aspect ratio

        Returns: List[float] of length total_dim (64)
        """
        # Lexical embedding
        text = element.get('name', '') or element.get('text', '') or ''
        lexical = self._text_to_hash_vector(text)

        # Class embedding
        role = element.get('role', element.get('tag', ''))
        class_vec = self._role_to_vector(role)

        # Spatial embedding
        spatial = self._spatial_vector(element)

        return lexical + class_vec + spatial

    def embed_page(self, elements: List[Dict]) -> List[float]:
        """
        Generate a holistic page-level embedding by aggregating
        all element embeddings (mean pooling).
        """
        if not elements:
            return [0.0] * self.total_dim

        vectors = [self.embed_element(e) for e in elements]
        return self._mean_vector(vectors)

    def cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(a * b for a, b in zip(v1, v2))
        mag1 = math.sqrt(sum(a * a for a in v1))
        mag2 = math.sqrt(sum(b * b for b in v2))
        if mag1 == 0 or mag2 == 0:
            return 0.0
        return dot / (mag1 * mag2)

    def find_similar(self, target_text: str, elements: List[Dict],
                     top_k: int = 5) -> List[Tuple[float, Dict]]:
        """
        Find elements most semantically similar to a text description.
        Works across different websites — the key generalizability feature.
        """
        target_vec = self._text_to_hash_vector(target_text)
        # Pad to full dimension with zeros for comparison
        target_full = target_vec + [0.0] * (self.total_dim - len(target_vec))

        scored = []
        for el in elements:
            el_vec = self.embed_element(el)
            # Compare lexical portion primarily
            sim = self.cosine_similarity(target_vec, el_vec[:self.lexical_dim])
            scored.append((sim, el))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def identify_concept(self, element: Dict) -> Optional[str]:
        """
        Identify which high-level UI concept an element represents.
        E.g., "Add to Basket" → 'cart', "Log In" → 'login'
        """
        text = (element.get('name', '') or '').lower()
        if not text:
            return None

        text_vec = self._text_to_hash_vector(text)
        best_concept = None
        best_sim = 0.3  # minimum threshold

        for concept, concept_vec in self._concept_vectors.items():
            sim = self.cosine_similarity(text_vec, concept_vec)
            if sim > best_sim:
                best_sim = sim
                best_concept = concept

        return best_concept

    def classify_page_type(self, elements: List[Dict]) -> str:
        """
        Classify the page type based on element composition.
        Returns: 'login', 'search', 'form', 'listing', 'article',
                 'dashboard', 'checkout', 'unknown'
        """
        if not elements:
            return 'unknown'

        roles = [e.get('role', '') for e in elements]
        names = [e.get('name', '').lower() for e in elements]
        all_text = ' '.join(names)

        # Heuristic classification based on element composition
        textbox_count = sum(1 for r in roles if r in ('textbox', 'input', 'textarea'))
        button_count = sum(1 for r in roles if r in ('button', 'submit'))
        link_count = sum(1 for r in roles if r in ('link', 'a'))

        if any(kw in all_text for kw in ('password', 'login', 'sign in')):
            return 'login'
        if any(kw in all_text for kw in ('search', 'find', 'query')):
            if textbox_count >= 1:
                return 'search'
        if any(kw in all_text for kw in ('checkout', 'payment', 'billing')):
            return 'checkout'
        if textbox_count >= 4:
            return 'form'
        if link_count > 20:
            return 'listing'
        if textbox_count <= 1 and button_count <= 2 and link_count < 10:
            return 'article'
        if button_count > 5 and link_count > 10:
            return 'dashboard'

        return 'unknown'

    def _text_to_hash_vector(self, text: str) -> List[float]:
        """
        Convert text to a deterministic hash-based embedding vector.
        Uses character n-gram hashing for language-agnostic representation.
        """
        text = text.lower().strip()
        if not text:
            return [0.0] * self.lexical_dim

        vector = [0.0] * self.lexical_dim

        # Character trigram hashing
        for i in range(len(text) - 2):
            trigram = text[i:i+3]
            h = int(hashlib.md5(trigram.encode()).hexdigest(), 16)
            idx = h % self.lexical_dim
            vector[idx] += 1.0

        # Word-level hashing
        words = text.split()
        for word in words:
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % self.lexical_dim
            vector[idx] += 2.0  # Words get more weight

        # Normalize
        mag = math.sqrt(sum(v * v for v in vector))
        if mag > 0:
            vector = [v / mag for v in vector]

        return vector

    def _role_to_vector(self, role: str) -> List[float]:
        """One-hot encode the element role."""
        vector = [0.0] * self.class_dim
        role = role.lower()
        if role in self.ROLE_VOCAB:
            idx = self.ROLE_VOCAB.index(role)
            if idx < self.class_dim:
                vector[idx] = 1.0
        return vector

    def _spatial_vector(self, element: Dict) -> List[float]:
        """
        Encode spatial properties into a fixed-size vector.
        Uses normalized coordinates (0-1000) or raw coords.
        """
        # Prefer normalized coordinates
        nx = element.get('nx', element.get('x', 0)) / 1000.0
        ny = element.get('ny', element.get('y', 0)) / 1000.0
        nw = element.get('nw', element.get('w', 0)) / 1000.0
        nh = element.get('nh', element.get('h', 0)) / 1000.0

        area = nw * nh
        aspect = nw / nh if nh > 0 else 1.0
        cx = nx + nw / 2  # center x
        cy = ny + nh / 2  # center y

        return [nx, ny, nw, nh, area, aspect, cx, cy]

    @staticmethod
    def _mean_vector(vectors: List[List[float]]) -> List[float]:
        """Average multiple vectors element-wise. Returns zero vector if empty."""
        if not vectors:
            return []
        dim = len(vectors[0])
        if dim == 0:
            return []
        result = [0.0] * dim
        for v in vectors:
            for i in range(min(len(v), dim)):
                result[i] += v[i]
        n = len(vectors)
        return [x / n for x in result]


# ═══════════════════════════════════════════════════════════════════════
# MODULE 5: PAGE TOPOLOGY GRAPH (GNN-inspired)
# ═══════════════════════════════════════════════════════════════════════

class PageTopologyGraph:
    """
    GNN-inspired graph representation of page layout.

    Nodes = UI elements (with embeddings)
    Edges = spatial/semantic relationships:
        - alignment (shared left/right/top/bottom edges)
        - dimension parity (same width/height)
        - containment (parent-child in DOM)
        - proximity (within threshold distance)
        - functional grouping (label-input pairs, etc.)

    Enables gestalt perception: seeing the whole rather than parts.
    """

    ALIGNMENT_THRESHOLD = 10   # pixels
    PROXIMITY_THRESHOLD = 100  # pixels
    SIZE_PARITY_THRESHOLD = 5  # pixels

    def __init__(self):
        self.nodes: List[Dict] = []
        self.edges: List[Dict] = []
        self._adjacency: Dict[int, List[int]] = defaultdict(list)

    def build(self, elements: List[Dict]) -> 'PageTopologyGraph':
        """
        Construct the complete topology graph from page elements.
        Computes all edge types: alignment, proximity, dimension parity.
        """
        self.nodes = []
        self.edges = []
        self._adjacency = defaultdict(list)

        # Build nodes
        for i, el in enumerate(elements):
            self.nodes.append({
                'idx': i,
                'role': el.get('role', ''),
                'name': el.get('name', ''),
                'box': [el.get('x', 0), el.get('y', 0),
                        el.get('w', 0), el.get('h', 0)],
                'element': el,
            })

        # Build edges
        n = len(self.nodes)
        for i in range(n):
            for j in range(i + 1, n):
                edges = self._compute_edges(i, j)
                for edge in edges:
                    self.edges.append(edge)
                    self._adjacency[i].append(j)
                    self._adjacency[j].append(i)

        return self

    def find_groups(self) -> List[List[int]]:
        """
        Find connected components (functional groups of elements).
        E.g., a product card with image + title + price + button.
        """
        visited: Set[int] = set()
        groups = []

        for i in range(len(self.nodes)):
            if i in visited:
                continue
            # BFS from node i
            group = []
            queue = [i]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                group.append(node)
                for neighbor in self._adjacency[node]:
                    if neighbor not in visited:
                        queue.append(neighbor)
            if len(group) > 1:
                groups.append(group)

        return groups

    def find_form_groups(self) -> List[Dict]:
        """
        Detect form field groups: label + input pairs.
        Uses proximity + alignment to pair labels with their inputs.
        """
        form_groups = []
        labels = [n for n in self.nodes if n['role'] in ('label', 'heading', 'text')]
        inputs = [n for n in self.nodes
                  if n['role'] in ('textbox', 'input', 'textarea',
                                   'combobox', 'select', 'checkbox', 'radio')]

        for inp in inputs:
            best_label = None
            best_dist = self.PROXIMITY_THRESHOLD

            for lbl in labels:
                dist = self._element_distance(lbl, inp)
                if dist < best_dist:
                    # Check alignment (label should be above or to the left)
                    lx = lbl['box'][0] + lbl['box'][2] / 2
                    ly = lbl['box'][1] + lbl['box'][3] / 2
                    ix = inp['box'][0] + inp['box'][2] / 2
                    iy = inp['box'][1] + inp['box'][3] / 2

                    if ly < iy + 5 or lx < ix:  # label above or to the left
                        best_dist = dist
                        best_label = lbl

            form_groups.append({
                'input': inp,
                'label': best_label,
                'paired': best_label is not None,
            })

        return form_groups

    def find_navigation_bars(self) -> List[List[int]]:
        """
        Detect navigation bars: horizontal rows of links/buttons
        with shared y-alignment.
        """
        links = [n for n in self.nodes if n['role'] in ('link', 'a', 'tab', 'menuitem')]
        if len(links) < 3:
            return []

        # Group by y-coordinate (horizontal alignment)
        y_groups: Dict[int, List] = defaultdict(list)
        for link in links:
            y_center = link['box'][1] + link['box'][3] / 2
            y_bucket = int(y_center) // max(self.ALIGNMENT_THRESHOLD, 1)
            y_groups[y_bucket].append(link)

        nav_bars = []
        for bucket, group in y_groups.items():
            if len(group) >= 3:
                nav_bars.append([n['idx'] for n in group])

        return nav_bars

    def find_grid_patterns(self) -> List[List[int]]:
        """
        Detect grid/card layouts: elements with same dimensions
        arranged in rows/columns.
        """
        # Group by size (width × height)
        size_groups: Dict[Tuple[int, int], List] = defaultdict(list)
        for node in self.nodes:
            w, h = node['box'][2], node['box'][3]
            if w < 20 or h < 20:
                continue
            size_key = (int(w) // max(self.SIZE_PARITY_THRESHOLD, 1),
                        int(h) // max(self.SIZE_PARITY_THRESHOLD, 1))
            size_groups[size_key].append(node)

        grids = []
        for key, group in size_groups.items():
            if len(group) >= 3:
                grids.append([n['idx'] for n in group])

        return grids

    def message_passing(self, iterations: int = 2) -> List[Dict]:
        """
        GNN-inspired message passing: each node aggregates features
        from its neighbors to build a richer representation.

        Returns enhanced node representations with neighbor context.
        """
        # Initialize node features (simple text-based)
        features = {}
        for node in self.nodes:
            features[node['idx']] = {
                'role': node['role'],
                'name': node['name'],
                'neighbor_roles': [],
                'neighbor_names': [],
                'cluster_size': 1,
                'is_nav': False,
                'is_form_field': False,
                'is_grid_item': False,
            }

        # Iterative message passing
        for _ in range(iterations):
            new_features = {}
            for i, feat in features.items():
                neighbors = self._adjacency.get(i, [])
                n_roles = [features[n]['role'] for n in neighbors if n in features]
                n_names = [features[n]['name'] for n in neighbors if n in features]

                new_features[i] = {
                    **feat,
                    'neighbor_roles': list(set(n_roles)),
                    'neighbor_names': n_names[:10],
                    'cluster_size': len(neighbors) + 1,
                }
            features = new_features

        # Detect patterns
        nav_bars = self.find_navigation_bars()
        nav_nodes = set(idx for bar in nav_bars for idx in bar)

        grids = self.find_grid_patterns()
        grid_nodes = set(idx for grid in grids for idx in grid)

        for i, feat in features.items():
            feat['is_nav'] = i in nav_nodes
            feat['is_grid_item'] = i in grid_nodes

        return [features[i] for i in sorted(features.keys())]

    def to_compact(self) -> Dict:
        """Export graph as compact JSON for LLM consumption."""
        return {
            'nodes': len(self.nodes),
            'edges': len(self.edges),
            'groups': self.find_groups(),
            'nav_bars': self.find_navigation_bars(),
            'grids': self.find_grid_patterns(),
            'edge_types': dict(
                alignment=sum(1 for e in self.edges if e['type'] == 'alignment'),
                proximity=sum(1 for e in self.edges if e['type'] == 'proximity'),
                size_parity=sum(1 for e in self.edges if e['type'] == 'size_parity'),
            ),
        }

    def _compute_edges(self, i: int, j: int) -> List[Dict]:
        """Compute all edge types between two nodes."""
        edges = []
        a, b = self.nodes[i], self.nodes[j]

        ax, ay, aw, ah = a['box']
        bx, by, bw, bh = b['box']

        # Alignment edges
        if abs(ax - bx) <= self.ALIGNMENT_THRESHOLD:
            edges.append({'type': 'alignment', 'subtype': 'left',
                          'source': i, 'target': j})
        if abs((ax + aw) - (bx + bw)) <= self.ALIGNMENT_THRESHOLD:
            edges.append({'type': 'alignment', 'subtype': 'right',
                          'source': i, 'target': j})
        if abs(ay - by) <= self.ALIGNMENT_THRESHOLD:
            edges.append({'type': 'alignment', 'subtype': 'top',
                          'source': i, 'target': j})

        # Proximity edge
        dist = self._element_distance(a, b)
        if dist < self.PROXIMITY_THRESHOLD:
            edges.append({'type': 'proximity', 'distance': round(dist),
                          'source': i, 'target': j})

        # Size parity edge
        if (abs(aw - bw) <= self.SIZE_PARITY_THRESHOLD and
                abs(ah - bh) <= self.SIZE_PARITY_THRESHOLD):
            edges.append({'type': 'size_parity', 'source': i, 'target': j})

        return edges

    @staticmethod
    def _element_distance(a: Dict, b: Dict) -> float:
        acx = a['box'][0] + a['box'][2] / 2
        acy = a['box'][1] + a['box'][3] / 2
        bcx = b['box'][0] + b['box'][2] / 2
        bcy = b['box'][1] + b['box'][3] / 2
        return math.sqrt((acx - bcx)**2 + (acy - bcy)**2)


# ═══════════════════════════════════════════════════════════════════════
# MODULE 6: ACTION SPACE OPTIMIZER
# ═══════════════════════════════════════════════════════════════════════

class ActionSpaceOptimizer:
    """
    Compresses the full DOM/page into a minimal action space
    optimized for LLM consumption.

    Reduction: 100,000+ tokens → ~1,400 tokens per scene.

    Pipeline:
    1. Extract interactable elements (Accessibility Tree)
    2. Apply semantic geometry (bounding boxes + normalization)
    3. Resolve occlusion (filter hidden elements)
    4. Score visual prominence (prioritize primary elements)
    5. Compress to compact JSON token stream
    """

    def __init__(self, cdp: CDP):
        self.cdp = cdp
        self.geometry = SemanticGeometryEngine(cdp)
        self.occlusion = OcclusionResolver(cdp)
        self.embeddings = ElementEmbedding()

    @staticmethod
    def _compress_scene_element(i: int, el: Dict,
                                 include_embeddings: bool,
                                 embeddings: 'ElementEmbedding') -> Dict:
        """Compress a single element for the optimized scene."""
        entry = {
            'ref': i,
            'role': el.get('role', ''),
            'name': (el.get('name', '') or '')[:60],
            'box': [el.get('nx', 0), el.get('ny', 0),
                    el.get('nw', 0), el.get('nh', 0)],
        }
        if el.get('z', 0) > 0:
            entry['z'] = el['z']
        if el.get('value'):
            entry['val'] = el['value'][:40]
        if el.get('disabled'):
            entry['disabled'] = True
        if el.get('prominence', 0) > 0.4:
            entry['primary'] = True
        if el.get('type'):
            entry['type'] = el['type']
        if el.get('href'):
            entry['href'] = el['href'][:80]
        if include_embeddings:
            concept = embeddings.identify_concept(el)
            if concept:
                entry['concept'] = concept
        return entry

    def optimize(self, tab_id: str,
                 max_elements: int = 50,
                 include_embeddings: bool = False,
                 include_page_type: bool = True) -> Dict:
        """
        Generate the fully optimized action space for a page.

        Returns a dict with: page_type, viewport, scene (compact element
        descriptors), token_estimate.
        """
        if isinstance(tab_id, dict):
            tab_id = tab_id['id']

        geo_data = self.geometry.extract(tab_id)
        elements = geo_data.get('elements', [])

        # Filter by occlusion
        truly_interactable = self.occlusion.get_truly_interactable(tab_id)
        interactable_ids = set(
            e.get('id', '') for e in truly_interactable if e.get('id'))
        if interactable_ids:
            elements = [e for e in elements
                        if e.get('id') in interactable_ids or not e.get('id')]

        # Sort by prominence and limit
        elements.sort(key=lambda e: e.get('prominence', 0), reverse=True)
        elements = elements[:max_elements]

        scene = [self._compress_scene_element(i, el, include_embeddings,
                                               self.embeddings)
                 for i, el in enumerate(elements)]

        result = {
            'viewport': [geo_data['viewport']['w'], geo_data['viewport']['h']],
            'total_dom_elements': geo_data.get('totalElements', 0),
            'actionable_count': len(scene),
            'scene': scene,
        }
        if include_page_type:
            result['page_type'] = self.embeddings.classify_page_type(elements)

        json_str = json.dumps(result, separators=(',', ':'))
        result['token_estimate'] = len(json_str) // 4
        return result

    def generate_prompt_context(self, tab_id: str,
                                 task: str = None,
                                 max_elements: int = 40) -> str:
        """
        Generate a complete LLM-ready context string for the current page.

        This is the final output that replaces a 100k-token DOM dump
        with ~1400 tokens of pure signal.
        """
        data = self.optimize(tab_id, max_elements=max_elements,
                             include_embeddings=True, include_page_type=True)

        lines = [
            f"## Page State",
            f"Type: {data.get('page_type', 'unknown')}",
            f"Viewport: {data['viewport'][0]}x{data['viewport'][1]}",
            f"DOM Elements: {data.get('total_dom_elements', '?')}",
            f"Actionable: {data['actionable_count']}",
            "",
            "## Action Space (normalized 0-1000 grid)",
        ]

        for el in data['scene']:
            box = el['box']
            parts = [f"[{el['ref']}]", el['role']]
            if el.get('name'):
                parts.append(f'"{el["name"]}"')
            parts.append(f'@({box[0]},{box[1]} {box[2]}x{box[3]})')
            if el.get('primary'):
                parts.append('★')
            if el.get('concept'):
                parts.append(f'({el["concept"]})')
            if el.get('val'):
                parts.append(f'val="{el["val"]}"')
            if el.get('disabled'):
                parts.append('[disabled]')
            lines.append(' '.join(parts))

        if task:
            lines.extend(["", f"## Task: {task}"])

        context = '\n'.join(lines)
        return context


# ═══════════════════════════════════════════════════════════════════════
# MODULE 7: SPATIAL REASONER
# ═══════════════════════════════════════════════════════════════════════

class SpatialReasoner:
    """
    Spatial reasoning engine implementing gestalt perception principles.

    Capabilities:
    - Direction queries (what's above/below/left/right of X?)
    - Alignment detection (rows, columns, grids)
    - Proximity grouping (functionally related elements)
    - Layout classification (navigation bar, sidebar, content area, footer)
    - Relative positioning (nearest input to a label)
    """

    def __init__(self):
        pass

    def what_is_near(self, target: Dict, elements: List[Dict],
                     direction: str = None, radius: int = 150) -> List[Dict]:
        """
        Find elements near a target, optionally filtered by direction.
        Directions: 'above', 'below', 'left', 'right', 'diagonal-up-right', etc.
        """
        tcx = target['x'] + target['w'] / 2
        tcy = target['y'] + target['h'] / 2

        results = []
        for el in elements:
            if el is target or el == target:
                continue
            ecx = el['x'] + el['w'] / 2
            ecy = el['y'] + el['h'] / 2

            dist = math.sqrt((tcx - ecx)**2 + (tcy - ecy)**2)
            if dist > radius:
                continue

            # Direction filter
            if direction:
                dx = ecx - tcx
                dy = ecy - tcy

                match = {
                    'above': dy < -10,
                    'below': dy > 10,
                    'left': dx < -10,
                    'right': dx > 10,
                    'diagonal-up-right': dy < -10 and dx > 10,
                    'diagonal-up-left': dy < -10 and dx < -10,
                    'diagonal-down-right': dy > 10 and dx > 10,
                    'diagonal-down-left': dy > 10 and dx < -10,
                }.get(direction, True)

                if not match:
                    continue

            results.append({**el, '_distance': round(dist)})

        results.sort(key=lambda e: e['_distance'])
        return results

    def detect_layout_regions(self, elements: List[Dict],
                               viewport: Dict) -> Dict:
        """
        Classify elements into layout regions:
        header, navigation, sidebar, content, footer.
        """
        vw = viewport.get('w', 1920)
        vh = viewport.get('h', 1080)

        regions = {
            'header': [],
            'navigation': [],
            'sidebar_left': [],
            'sidebar_right': [],
            'content': [],
            'footer': [],
            'fixed_overlay': [],
        }

        for el in elements:
            cx = el.get('x', 0) + el.get('w', 0) / 2
            cy = el.get('y', 0) + el.get('h', 0) / 2

            if el.get('isFixed'):
                regions['fixed_overlay'].append(el)
            elif cy < vh * 0.12:
                if el.get('role') in ('link', 'a', 'menuitem', 'tab'):
                    regions['navigation'].append(el)
                else:
                    regions['header'].append(el)
            elif cy > vh * 0.85:
                regions['footer'].append(el)
            elif cx < vw * 0.2:
                regions['sidebar_left'].append(el)
            elif cx > vw * 0.8:
                regions['sidebar_right'].append(el)
            else:
                regions['content'].append(el)

        return regions

    def detect_rows_and_columns(self, elements: List[Dict],
                                 tolerance: int = 15) -> Dict:
        """
        Detect horizontal rows and vertical columns of elements.
        Returns groups of aligned elements.
        """
        if tolerance <= 0:
            tolerance = 15

        # Find rows (shared y-center)
        y_groups: Dict[int, List[Dict]] = defaultdict(list)
        for el in elements:
            cy = el.get('y', 0) + el.get('h', 0) / 2
            bucket = int(cy) // tolerance
            y_groups[bucket].append(el)

        rows = [group for group in y_groups.values() if len(group) >= 2]
        rows.sort(key=lambda r: r[0].get('y', 0))

        # Find columns (shared x-center)
        x_groups: Dict[int, List[Dict]] = defaultdict(list)
        for el in elements:
            cx = el.get('x', 0) + el.get('w', 0) / 2
            bucket = int(cx) // tolerance
            x_groups[bucket].append(el)

        columns = [group for group in x_groups.values() if len(group) >= 2]
        columns.sort(key=lambda c: c[0].get('x', 0))

        return {
            'rows': len(rows),
            'columns': len(columns),
            'row_details': [
                {'y': r[0].get('y', 0), 'count': len(r),
                 'elements': [e.get('name', '')[:30] for e in r]}
                for r in rows
            ],
            'column_details': [
                {'x': c[0].get('x', 0), 'count': len(c),
                 'elements': [e.get('name', '')[:30] for e in c]}
                for c in columns
            ],
        }

    def find_related_input(self, label_text: str,
                           elements: List[Dict]) -> Optional[Dict]:
        """
        Find the input field most likely associated with a label.
        Uses proximity + spatial position (input below or right of label).
        """
        # Find the label element
        label = None
        label_lower = label_text.lower()
        for el in elements:
            name = (el.get('name', '') or '').lower()
            if label_lower in name:
                label = el
                break

        if not label:
            return None

        # Find nearest input-like element
        inputs = [e for e in elements
                  if e.get('role') in ('textbox', 'input', 'textarea',
                                       'combobox', 'select', 'checkbox', 'radio')]

        nearby = self.what_is_near(label, inputs, radius=200)
        # Prefer elements below or to the right
        for el in nearby:
            ey = el.get('y', 0)
            ly = label.get('y', 0)
            ex = el.get('x', 0)
            lx = label.get('x', 0)
            if ey >= ly - 5 or ex > lx:
                return el

        return nearby[0] if nearby else None

    def spatial_description(self, element: Dict,
                            all_elements: List[Dict],
                            viewport: Dict) -> str:
        """
        Generate a natural language spatial description of an element.
        This is the 'Visualization-of-Thought' technique.
        """
        vw, vh = viewport.get('w', 1920), viewport.get('h', 1080)
        cx = element.get('x', 0) + element.get('w', 0) / 2
        cy = element.get('y', 0) + element.get('h', 0) / 2

        # Position in viewport
        h_pos = 'left' if cx < vw * 0.33 else ('right' if cx > vw * 0.67 else 'center')
        v_pos = 'top' if cy < vh * 0.33 else ('bottom' if cy > vh * 0.67 else 'middle')

        name = element.get('name', 'unnamed')
        role = element.get('role', 'element')

        # Find nearest neighbors
        nearby = self.what_is_near(element, all_elements, radius=100)
        neighbor_desc = ''
        if nearby:
            nn = nearby[0]
            nn_name = nn.get('name', 'element')[:30]
            nn_dist = nn.get('_distance', 0)

            # Direction from element to neighbor
            nx = nn.get('x', 0) + nn.get('w', 0) / 2
            ny = nn.get('y', 0) + nn.get('h', 0) / 2
            dx, dy = nx - cx, ny - cy
            if abs(dx) > abs(dy):
                dir_str = 'right' if dx > 0 else 'left'
            else:
                dir_str = 'below' if dy > 0 else 'above'
            neighbor_desc = f', {nn_dist}px {dir_str} of "{nn_name}"'

        return (f'{role} "{name}" at {v_pos}-{h_pos} of viewport '
                f'({int(cx)},{int(cy)}){neighbor_desc}')


# ═══════════════════════════════════════════════════════════════════════
# MODULE 8: GOD MODE CONTROLLER — THE UNIFIED ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

class GodMode:
    """
    ╔══════════════════════════════════════════════════════════════╗
    ║                    G O D   M O D E                          ║
    ║                                                              ║
    ║  The unified orchestrator that combines all perception       ║
    ║  modules into a single, all-seeing interface.                ║
    ║                                                              ║
    ║  Capabilities:                                               ║
    ║  • See any page as structured data (no screenshots)          ║
    ║  • Navigate by semantic meaning (not CSS selectors)          ║
    ║  • Understand spatial layout (rows, columns, regions)        ║
    ║  • Detect overlays/modals (z-index occlusion)                ║
    ║  • Find elements by concept ("shopping cart", "login")       ║
    ║  • Generate ultra-compact LLM prompts (~1400 tokens)         ║
    ║  • Classify page types automatically                         ║
    ║  • Build relational topology graphs                          ║
    ║  • Zero mouse, zero keyboard — pure API interaction          ║
    ╚══════════════════════════════════════════════════════════════╝
    """

    def __init__(self, cdp_port=9222):
        # Core CDP connection
        self._cdp = None
        self._port = cdp_port

        # Perception modules
        self.perception = PerceptionEngine(cdp_port=cdp_port)
        self.a11y = None          # Lazy init
        self.geometry = None      # Lazy init
        self.occlusion = None     # Lazy init
        self.embeddings = ElementEmbedding()
        self.graph = PageTopologyGraph()
        self.optimizer = None     # Lazy init
        self.spatial = SpatialReasoner()

        # State
        self._active_tab = None
        self._page_cache = {}
        self._action_history = []

    @property
    def cdp(self) -> CDP:
        if self._cdp is None:
            self._cdp = CDP(port=self._port)
        return self._cdp

    @property
    def connected(self) -> bool:
        try:
            self.cdp.tabs()
            return True
        except Exception:
            self._cdp = None
            return False

    def _ensure_modules(self):
        """Lazy-initialize modules that need CDP."""
        if self.a11y is None:
            self.a11y = AccessibilityTreeParser(self.cdp)
        if self.geometry is None:
            self.geometry = SemanticGeometryEngine(self.cdp)
        if self.occlusion is None:
            self.occlusion = OcclusionResolver(self.cdp)
        if self.optimizer is None:
            self.optimizer = ActionSpaceOptimizer(self.cdp)

    # ═══════════════════════════════════════════════════════════
    # HIGH-LEVEL PERCEPTION
    # ═══════════════════════════════════════════════════════════

    def _see_minimal(self, tab_id: str, result: Dict, t0: float) -> Dict:
        """Layer 1: Accessibility tree only."""
        result['accessibility_tree'] = self.a11y.parse_compact(tab_id)
        result['actionable_elements'] = self.a11y.find_actionable(tab_id)
        result['perception_time_ms'] = round((time.time() - t0) * 1000)
        return result

    def _see_standard(self, tab_id: str, result: Dict, t0: float) -> Dict:
        """Layers 2-3: Geometry + occlusion."""
        geo = self.geometry.extract(tab_id)
        result['viewport'] = geo.get('viewport', {})
        result['elements'] = geo.get('elements', [])
        result['dom_element_count'] = geo.get('totalElements', 0)

        occ = self.occlusion.resolve(tab_id)
        result['occlusion'] = {
            'visible': occ.get('visible', 0),
            'occluded': occ.get('occluded', 0),
            'has_modal': occ.get('has_modal', False),
        }
        result['perception_time_ms'] = round((time.time() - t0) * 1000)
        return result

    def _see_deep(self, tab_id: str, result: Dict, t0: float) -> Dict:
        """Layer 4: Graph topology."""
        if result['elements']:
            self.graph.build(result['elements'])
            result['topology'] = self.graph.to_compact()
        result['perception_time_ms'] = round((time.time() - t0) * 1000)
        return result

    def _see_god(self, tab_id: str, result: Dict, t0: float) -> Dict:
        """Layer 5: Full GOD mode -- embeddings, spatial, forms, nav, CTA."""
        if not result['elements']:
            result['perception_time_ms'] = round((time.time() - t0) * 1000)
            return result

        elements = result['elements']
        result['page_type'] = self.embeddings.classify_page_type(elements)
        result['layout'] = self.spatial.detect_layout_regions(
            elements, result['viewport'])
        result['grid_analysis'] = self.spatial.detect_rows_and_columns(elements)
        result['form_groups'] = self.graph.find_form_groups()
        result['nav_bars'] = self.graph.find_navigation_bars()

        cta = self.geometry.find_primary_cta(tab_id)
        if cta:
            result['primary_cta'] = {
                'name': cta.get('name', ''),
                'role': cta.get('role', ''),
                'box': [cta.get('x', 0), cta.get('y', 0),
                        cta.get('w', 0), cta.get('h', 0)],
            }

        overlays = self.occlusion.detect_overlays(tab_id)
        if overlays:
            result['overlays'] = overlays

        result['perception_time_ms'] = round((time.time() - t0) * 1000)
        return result

    def see(self, tab_id: str = None, depth: str = 'standard') -> Dict:
        """
        SEE the current page. The primary perception method.

        depth levels:
        - 'minimal': Just accessibility tree (fastest, ~50ms)
        - 'standard': AOM + geometry + occlusion (~200ms)
        - 'deep': All modules including graph topology (~500ms)
        - 'god': Everything + embeddings + spatial reasoning (~800ms)
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()
        if not tab_id:
            return {'error': 'No active tab'}

        t0 = time.time()
        result = {'tab_id': tab_id, 'timestamp': time.time()}

        # Layer 1 always runs
        self._see_minimal(tab_id, result, t0)
        if depth == 'minimal':
            return result

        # Layer 2-3
        self._see_standard(tab_id, result, t0)
        if depth == 'standard':
            return result

        # Layer 4
        self._see_deep(tab_id, result, t0)
        if depth == 'deep':
            return result

        # Layer 5 (god)
        return self._see_god(tab_id, result, t0)

    def scene(self, tab_id: str = None, max_elements: int = 40) -> str:
        """
        Generate the optimized LLM-ready scene description.
        This is what replaces 100k DOM tokens with ~1400 tokens.
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()
        return self.optimizer.generate_prompt_context(tab_id,
                                                      max_elements=max_elements)

    def action_space(self, tab_id: str = None) -> str:
        """Generate compact grounded action space JSON."""
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()
        return self.geometry.extract_grounded_action_space(tab_id)

    # ═══════════════════════════════════════════════════════════
    # SEMANTIC NAVIGATION
    # ═══════════════════════════════════════════════════════════

    def find(self, concept: str, tab_id: str = None) -> List[Dict]:
        """
        Find elements by semantic concept rather than CSS selector.
        Works across any website — true generalizability.

        Examples:
            god.find("shopping cart")
            god.find("login button")
            god.find("search field")
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()

        geo = self.geometry.extract(tab_id)
        elements = geo.get('elements', [])

        results = self.embeddings.find_similar(concept, elements, top_k=10)
        return [{'similarity': round(sim, 3), **el}
                for sim, el in results if sim > 0.1]

    def find_and_click(self, concept: str, tab_id: str = None) -> bool:
        """
        Find an element by semantic concept and click it.
        Zero mouse — uses CDP Input domain.
        """
        results = self.find(concept, tab_id)
        if not results:
            return False

        best = results[0]
        tab_id = tab_id or self._get_active_tab()
        x = int(best['x'] + best['w'] / 2)
        y = int(best['y'] + best['h'] / 2)
        self.cdp.click(tab_id, x, y)
        self._action_history.append({
            'action': 'click', 'concept': concept,
            'element': best.get('name', ''), 'coords': (x, y),
            'time': time.time()
        })
        return True

    def find_and_fill(self, label: str, value: str,
                      tab_id: str = None) -> bool:
        """
        Find an input by its associated label and fill it.

        Example: god.find_and_fill("Email", "user@example.com")
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()

        geo = self.geometry.extract(tab_id)
        elements = geo.get('elements', [])

        target = self.spatial.find_related_input(label, elements)
        if not target:
            # Fallback: find by name similarity
            results = self.embeddings.find_similar(label, elements, top_k=5)
            inputs = [(s, e) for s, e in results
                      if e.get('role') in ('textbox', 'input', 'textarea',
                                           'combobox', 'select')]
            if inputs:
                target = inputs[0][1]

        if not target:
            return False

        x = int(target['x'] + target['w'] / 2)
        y = int(target['y'] + target['h'] / 2)

        # Click to focus, then type
        self.cdp.click(tab_id, x, y)
        time.sleep(0.1)
        # Select all existing text
        self.cdp.press_key(tab_id, 'a', modifiers=2)  # Ctrl+A
        time.sleep(0.05)
        self.cdp.type_text(tab_id, value)

        self._action_history.append({
            'action': 'fill', 'label': label, 'value': value,
            'element': target.get('name', ''), 'time': time.time()
        })
        return True

    # ═══════════════════════════════════════════════════════════
    # SPATIAL INTELLIGENCE
    # ═══════════════════════════════════════════════════════════

    def what_is_at(self, x: int, y: int, tab_id: str = None) -> List[Dict]:
        """What elements exist at these coordinates?"""
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()
        geo = self.geometry.extract(tab_id)
        elements = geo.get('elements', [])

        hits = []
        for el in elements:
            if (el['x'] <= x <= el['x'] + el['w'] and
                    el['y'] <= y <= el['y'] + el['h']):
                hits.append(el)

        hits.sort(key=lambda e: e.get('z', 0), reverse=True)
        return hits

    def describe(self, tab_id: str = None) -> str:
        """
        Generate a complete spatial description of the page.
        Uses Visualization-of-Thought for LLM spatial reasoning.
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()

        geo = self.geometry.extract(tab_id)
        elements = geo.get('elements', [])
        viewport = geo.get('viewport', {'w': 1920, 'h': 1080})

        lines = [f"Page layout ({viewport['w']}x{viewport['h']} viewport):"]
        lines.append("")

        # Layout regions
        regions = self.spatial.detect_layout_regions(elements, viewport)
        for region_name, region_els in regions.items():
            if region_els:
                lines.append(f"  {region_name.upper()} ({len(region_els)} elements):")
                for el in region_els[:5]:
                    desc = self.spatial.spatial_description(el, elements, viewport)
                    lines.append(f"    • {desc}")

        # Overlays
        overlays = self.occlusion.detect_overlays(tab_id)
        if overlays:
            lines.append("")
            lines.append("  ⚠️ OVERLAYS DETECTED:")
            for ov in overlays:
                lines.append(f"    • {ov.get('tag')} z={ov.get('z')} "
                           f"reason={ov.get('reason')} "
                           f"\"{ov.get('text', '')[:40]}\"")

        return '\n'.join(lines)

    # ═══════════════════════════════════════════════════════════
    # DIRECT ACTIONS (CDP — zero mouse/keyboard)
    # ═══════════════════════════════════════════════════════════

    def click(self, target, tab_id: str = None) -> bool:
        """
        Click on a target. Target can be:
        - str: semantic concept ("Submit button", "Search")
        - tuple: (x, y) coordinates
        - dict: element dict with x,y,w,h
        """
        tab_id = tab_id or self._get_active_tab()
        if not tab_id:
            return False

        if isinstance(target, str):
            return self.find_and_click(target, tab_id)
        elif isinstance(target, tuple):
            self.cdp.click(tab_id, target[0], target[1])
            return True
        elif isinstance(target, dict):
            x = int(target.get('x', 0) + target.get('w', 0) / 2)
            y = int(target.get('y', 0) + target.get('h', 0) / 2)
            self.cdp.click(tab_id, x, y)
            return True
        return False

    def type_text(self, text: str, tab_id: str = None):
        """Type text via CDP — zero keyboard."""
        tab_id = tab_id or self._get_active_tab()
        self.cdp.type_text(tab_id, text)

    def press(self, key: str, tab_id: str = None):
        """Press a key via CDP — zero keyboard."""
        tab_id = tab_id or self._get_active_tab()
        self.cdp.press_key(tab_id, key)

    def navigate(self, url: str, tab_id: str = None):
        """Navigate via CDP — zero address bar."""
        tab_id = tab_id or self._get_active_tab()
        self.cdp.navigate(tab_id, url)

    def scroll(self, direction: str = 'down', amount: int = 300,
               tab_id: str = None):
        """Scroll via CDP — zero mouse wheel."""
        tab_id = tab_id or self._get_active_tab()
        delta = -amount if direction == 'down' else amount
        self.cdp.scroll(tab_id, x=0, y=0, delta_y=delta)

    def eval(self, js: str, tab_id: str = None):
        """Execute JavaScript."""
        tab_id = tab_id or self._get_active_tab()
        return self.cdp.eval(tab_id, js)

    def screenshot(self, filepath: str = None, tab_id: str = None) -> bytes:
        """Take screenshot via CDP (for verification only)."""
        tab_id = tab_id or self._get_active_tab()
        data = self.cdp.screenshot(tab_id)
        if filepath and data:
            try:
                with open(filepath, 'wb') as f:
                    f.write(data)
            except (IOError, OSError) as e:
                logger.error(f"Failed to save screenshot to {filepath}: {e}")
        return data

    # ═══════════════════════════════════════════════════════════
    # TAB MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    def tabs(self) -> List[Dict]:
        """List all tabs."""
        return self.cdp.tabs()

    def new_tab(self, url: str = 'about:blank') -> str:
        """Open new tab, return tab ID."""
        tab = self.cdp.new_tab(url)
        return tab.get('id', '') if isinstance(tab, dict) else str(tab)

    def close_tab(self, tab_id: str = None):
        """Close a tab."""
        tab_id = tab_id or self._get_active_tab()
        self.cdp.close_tab(tab_id)

    def activate_tab(self, tab_id: str):
        """Activate (focus) a tab."""
        self.cdp.activate_tab(tab_id)
        self._active_tab = tab_id

    # ═══════════════════════════════════════════════════════════
    # COMPOSITE OPERATIONS
    # ═══════════════════════════════════════════════════════════

    def dismiss_overlays(self, tab_id: str = None) -> int:
        """
        Automatically detect and dismiss overlays (modals, cookie banners).
        Returns count of dismissed overlays.
        """
        self._ensure_modules()
        tab_id = tab_id or self._get_active_tab()

        overlays = self.occlusion.detect_overlays(tab_id)
        dismissed = 0

        for overlay in overlays:
            text = (overlay.get('text', '') or '').lower()

            # Try to find dismiss buttons within overlay context
            dismiss_keywords = ['close', 'dismiss', 'accept', 'ok', 'got it',
                                'agree', 'continue', 'no thanks', '×', 'x']

            for kw in dismiss_keywords:
                if kw in text:
                    # Try clicking by text
                    clicked = self.perception.chrome.click_by_text(tab_id, kw.title())
                    if clicked:
                        dismissed += 1
                        time.sleep(0.5)
                        break

            # Try pressing Escape as fallback
            if not dismissed:
                self.press('Escape', tab_id)
                time.sleep(0.3)

        return dismissed

    def fill_form(self, fields: Dict[str, str], tab_id: str = None) -> Dict:
        """
        Fill a form by field labels.

        Example:
            god.fill_form({
                "First Name": "John",
                "Last Name": "Doe",
                "Email": "john@example.com",
            })
        """
        results = {}
        for label, value in fields.items():
            success = self.find_and_fill(label, value, tab_id)
            results[label] = 'filled' if success else 'not found'
            time.sleep(0.2)
        return results

    def wait_for(self, text: str = None, selector: str = None,
                 timeout: int = 30, tab_id: str = None) -> bool:
        """Wait for text or element to appear."""
        tab_id = tab_id or self._get_active_tab()
        try:
            if text:
                self.cdp.wait_for_text(tab_id, text, timeout)
            elif selector:
                self.cdp.wait_for_selector(tab_id, selector, timeout)
            return True
        except CDPError:
            return False

    # ═══════════════════════════════════════════════════════════
    # ENVIRONMENT SCAN (Win32 + Chrome + UIA)
    # ═══════════════════════════════════════════════════════════

    def scan_world(self, depth: int = 3) -> Dict:
        """Full environment scan via PerceptionEngine."""
        return self.perception.scan_world(depth=depth)

    def windows(self) -> List[Dict]:
        """List all windows with z-order."""
        return self.perception.stacking_order()

    def monitors(self) -> List[Dict]:
        """List all monitors."""
        return self.perception.win32.get_monitors()

    # ═══════════════════════════════════════════════════════════
    # STATUS & DIAGNOSTICS
    # ═══════════════════════════════════════════════════════════

    def status(self) -> Dict:
        """Complete system status."""
        tabs = []
        chrome_ok = False
        try:
            raw_tabs = self.cdp.tabs()
            chrome_ok = True
            tabs = [{'id': t['id'][:8], 'title': t.get('title', '?')[:50],
                      'url': t.get('url', '?')[:80]}
                    for t in raw_tabs]
        except Exception:
            pass

        return {
            'god_mode': True,
            'chrome_connected': chrome_ok,
            'cdp_port': self._port,
            'tabs': tabs,
            'active_tab': self._active_tab,
            'modules': {
                'accessibility_tree': self.a11y is not None,
                'semantic_geometry': self.geometry is not None,
                'occlusion_resolver': self.occlusion is not None,
                'element_embeddings': True,
                'page_topology': True,
                'action_optimizer': self.optimizer is not None,
                'spatial_reasoner': True,
                'win32_scanner': True,
                'perception_engine': True,
            },
            'action_history_count': len(self._action_history),
            'monitors': len(self.perception.win32.get_monitors()),
        }

    def history(self) -> List[Dict]:
        """Return action history."""
        return self._action_history[-50:]

    # ═══════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════

    def _get_active_tab(self) -> Optional[str]:
        """Get the active tab ID."""
        if self._active_tab:
            return self._active_tab
        try:
            tabs = self.cdp.tabs()
            if tabs:
                self._active_tab = tabs[0]['id']
                return self._active_tab
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════════════

def _cmd_see(god, args):
    result = god.see(depth=args.depth)
    if args.json:
        for key in ['elements', 'actionable_elements']:
            if key in result:
                result[key] = f'[{len(result[key])} items]'
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result.get('accessibility_tree', 'No data'))


def _cmd_find(god, args):
    if not args.args:
        print('Usage: god_mode find <concept>')
        return
    for r in god.find(' '.join(args.args))[:10]:
        sim = r.get('similarity', 0)
        print(f"  [{sim:.2f}] {r.get('role','')} \"{r.get('name','')}\" "
              f"@({r.get('x',0)},{r.get('y',0)})")


def _cmd_click(god, args):
    if not args.args:
        print('Usage: god_mode click <concept>')
        return
    print('clicked' if god.click(' '.join(args.args)) else 'not found')


def _cmd_overlays(god, _args):
    god._ensure_modules()
    overlays = god.occlusion.detect_overlays(god._get_active_tab())
    if overlays:
        for ov in overlays:
            print(f"  {ov['tag']} z={ov['z']} ({ov['reason']}) "
                  f"\"{ov.get('text','')[:50]}\"")
    else:
        print('No overlays detected')


def _cmd_graph(god, _args):
    god._ensure_modules()
    tab = god._get_active_tab()
    geo = god.geometry.extract(tab)
    god.graph.build(geo.get('elements', []))
    print(json.dumps(god.graph.to_compact(), indent=2))


def _cmd_tabs(god, _args):
    for t in god.tabs():
        print(f"  [{t['id'][:8]}] {t.get('title','?')[:50]}")
        print(f"             {t.get('url','?')[:80]}")


def _cmd_windows(god, _args):
    god.scan_world()
    for w in god.windows():
        print(f"  z={w['z']:2d} {w['name'][:50]}")


def _cmd_monitors(god, _args):
    for i, m in enumerate(god.monitors()):
        print(f"  Monitor {i}: {m['x']},{m['y']} {m['w']}x{m['h']}")


def _cmd_page_type(god, _args):
    god._ensure_modules()
    tab = god._get_active_tab()
    geo = god.geometry.extract(tab)
    print(f"Page type: {god.embeddings.classify_page_type(geo.get('elements', []))}")


def _cmd_a11y(god, _args):
    god._ensure_modules()
    print(god.a11y.parse_compact(god._get_active_tab()))


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='GOD MODE -- Structural Perception Engine')
    parser.add_argument('command', nargs='?', default='status',
                        choices=['status', 'see', 'scene', 'find', 'click',
                                 'describe', 'overlays', 'graph', 'tabs',
                                 'windows', 'monitors', 'action-space',
                                 'page-type', 'a11y'],
                        help='Command')
    parser.add_argument('args', nargs='*')
    parser.add_argument('--port', type=int, default=9222)
    parser.add_argument('--depth', default='standard',
                        choices=['minimal', 'standard', 'deep', 'god'])
    parser.add_argument('--json', action='store_true')

    args = parser.parse_args()
    god = GodMode(cdp_port=args.port)

    dispatch = {
        'status': lambda: print(json.dumps(god.status(), indent=2)),
        'see': lambda: _cmd_see(god, args),
        'scene': lambda: print(god.scene()),
        'find': lambda: _cmd_find(god, args),
        'click': lambda: _cmd_click(god, args),
        'describe': lambda: print(god.describe()),
        'overlays': lambda: _cmd_overlays(god, args),
        'graph': lambda: _cmd_graph(god, args),
        'tabs': lambda: _cmd_tabs(god, args),
        'windows': lambda: _cmd_windows(god, args),
        'monitors': lambda: _cmd_monitors(god, args),
        'action-space': lambda: print(god.action_space()),
        'page-type': lambda: _cmd_page_type(god, args),
        'a11y': lambda: _cmd_a11y(god, args),
    }

    handler = dispatch.get(args.command)
    if handler:
        handler()


if __name__ == '__main__':
    main()
