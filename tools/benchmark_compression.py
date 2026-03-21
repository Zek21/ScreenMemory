#!/usr/bin/env python3
"""Benchmark GodMode accessibility-tree compression ratio.

Tests the paper's claim: 100,000+ DOM tokens compressed to ~1,400 semantic tokens
(71× compression ratio). Uses sample HTML to measure actual ratios.

Usage:
    python tools/benchmark_compression.py
    python tools/benchmark_compression.py --json
"""
# signed: gamma

import json
import time
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Representative sample pages as raw HTML/DOM token estimates
SAMPLE_PAGES = [
    {
        "name": "Simple form (10 inputs)",
        "elements": 10,
        "raw_dom_tokens_est": 5000,
    },
    {
        "name": "Medium page (50 elements)",
        "elements": 50,
        "raw_dom_tokens_est": 25000,
    },
    {
        "name": "Complex page (200 elements)",
        "elements": 200,
        "raw_dom_tokens_est": 100000,
    },
    {
        "name": "Heavy SPA (500 elements)",
        "elements": 500,
        "raw_dom_tokens_est": 250000,
    },
]


def _generate_mock_elements(count):
    """Generate mock actionable elements as the optimizer would see them."""
    roles = ["button", "link", "textbox", "checkbox", "combobox", "menuitem",
             "tab", "heading", "navigation", "listitem"]
    elements = []
    for i in range(count):
        role = roles[i % len(roles)]
        elements.append({
            "ref": i,
            "role": role,
            "name": f"Element {i} - {role} label text here" if i % 3 != 0 else "",
            "box": [
                (i * 50) % 1000,
                (i * 30) % 800,
                ((i * 50) % 1000) + 100,
                ((i * 30) % 800) + 30,
            ],
            "states": {"focused": True} if i == 0 else {},
            "actionable": role in ("button", "link", "textbox", "checkbox", "combobox"),
        })
    return elements


def _estimate_tokens(text):
    """Rough token estimate: ~4 chars per token (GPT-family approximation)."""
    return max(1, len(text) // 4)


def _generate_raw_dom(element_count):
    """Generate a representative raw DOM string for token counting."""
    tags = ["div", "span", "a", "button", "input", "section", "header",
            "footer", "nav", "ul", "li", "p", "h1", "h2", "form", "label"]
    attrs = ['class="container mx-auto px-4"', 'id="main-content"',
             'style="display:flex;align-items:center;justify-content:space-between"',
             'data-testid="component-wrapper"', 'aria-label="navigation menu"',
             'role="button" tabindex="0"']

    lines = ['<!DOCTYPE html><html><head><title>Page</title></head><body>']
    # Each "real" element generates ~50 DOM nodes of wrapper/styling noise
    noise_ratio = 50
    total_nodes = element_count * noise_ratio
    for i in range(total_nodes):
        tag = tags[i % len(tags)]
        attr = attrs[i % len(attrs)]
        if i % 5 == 0:
            lines.append(f'<{tag} {attr}>Content text for node {i}</{tag}>')
        elif i % 3 == 0:
            lines.append(f'<{tag} {attr}>')
        else:
            lines.append(f'</{tag}>')
    lines.append('</body></html>')
    return '\n'.join(lines)


def _compress_to_compact(elements):
    """Simulate the ActionSpaceOptimizer compact output format."""
    scene = []
    for el in elements:
        if not el.get("actionable") and not el.get("name"):
            continue
        entry = {
            "ref": el["ref"],
            "role": el["role"],
            "name": (el.get("name", "") or "")[:60],
            "box": el.get("box", [0, 0, 0, 0]),
        }
        if el.get("states"):
            entry["s"] = el["states"]
        scene.append(entry)
    return json.dumps(scene, separators=(",", ":"))


def benchmark_compression():
    """Run compression ratio benchmark on sample data."""
    results = []

    for page in SAMPLE_PAGES:
        n = page["elements"]

        # Generate raw DOM and compressed representation
        raw_dom = _generate_raw_dom(n)
        raw_tokens = _estimate_tokens(raw_dom)

        elements = _generate_mock_elements(n)
        compressed = _compress_to_compact(elements)
        compressed_tokens = _estimate_tokens(compressed)

        ratio = raw_tokens / compressed_tokens if compressed_tokens > 0 else 0

        results.append({
            "page": page["name"],
            "actionable_elements": n,
            "raw_dom_chars": len(raw_dom),
            "raw_dom_tokens": raw_tokens,
            "compressed_chars": len(compressed),
            "compressed_tokens": compressed_tokens,
            "compression_ratio": round(ratio, 1),
            "paper_estimate_ratio": 71,
        })

    return results


def benchmark_live_compression():
    """Attempt live compression via CDP if Chrome is available."""
    try:
        from tools.chrome_bridge.cdp import CDP
        cdp = CDP(port=9222)
        tabs = cdp.tabs()
        if not tabs:
            return {"error": "No Chrome tabs found"}

        tab = tabs[0]
        tab_id = tab["id"]

        # Get raw DOM size
        t0 = time.perf_counter()
        raw_dom = cdp.eval(tab_id, "document.documentElement.outerHTML")
        dom_time = (time.perf_counter() - t0) * 1000

        if not raw_dom or not isinstance(raw_dom, str):
            return {"error": "Could not retrieve DOM"}

        raw_tokens = _estimate_tokens(raw_dom)

        # Get accessibility tree
        t1 = time.perf_counter()
        a11y_nodes = cdp.accessibility_tree(tab_id)
        a11y_time = (time.perf_counter() - t1) * 1000

        # Filter to meaningful nodes
        meaningful = [n for n in a11y_nodes
                      if n.get("role", {}).get("value") not in ("none", "generic", "")]
        compact = json.dumps([{
            "role": n.get("role", {}).get("value", ""),
            "name": (n.get("name", {}).get("value", "") or "")[:60],
        } for n in meaningful], separators=(",", ":"))
        compressed_tokens = _estimate_tokens(compact)

        ratio = raw_tokens / compressed_tokens if compressed_tokens > 0 else 0

        return {
            "url": tab.get("url", "unknown"),
            "raw_dom_chars": len(raw_dom),
            "raw_dom_tokens": raw_tokens,
            "a11y_nodes": len(a11y_nodes),
            "meaningful_nodes": len(meaningful),
            "compressed_tokens": compressed_tokens,
            "compression_ratio": round(ratio, 1),
            "dom_fetch_ms": round(dom_time, 1),
            "a11y_fetch_ms": round(a11y_time, 1),
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Benchmark accessibility tree compression")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    parser.add_argument("--live", action="store_true", help="Also test live Chrome tab")
    args = parser.parse_args()

    synthetic_results = benchmark_compression()
    live_result = benchmark_live_compression() if args.live else None

    output = {"synthetic": synthetic_results}
    if live_result:
        output["live"] = live_result

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print("=" * 75)
        print("Accessibility Tree Compression Ratio Benchmark")
        print("=" * 75)
        print(f"{'Page':<30} {'Raw Tok':>8} {'Comp Tok':>9} {'Ratio':>6} {'Paper':>6}")
        print("-" * 75)
        for r in synthetic_results:
            print(f"{r['page']:<30} {r['raw_dom_tokens']:>8,} {r['compressed_tokens']:>9,} "
                  f"{r['compression_ratio']:>6.1f}× {r['paper_estimate_ratio']:>5}×")
        print("-" * 75)
        print("  Paper claims: ~100,000 raw DOM tokens -> ~1,400 semantic tokens (71x ratio)")
        print("  Note: Actual ratio depends on page complexity and content density")

        if live_result:
            print(f"\n  Live Chrome tab:")
            if "error" in live_result:
                print(f"    Error: {live_result['error']}")
            else:
                print(f"    URL: {live_result['url'][:60]}")
                print(f"    Raw DOM: {live_result['raw_dom_tokens']:,} tokens")
                print(f"    A11y nodes: {live_result['a11y_nodes']} total, {live_result['meaningful_nodes']} meaningful")
                print(f"    Compressed: {live_result['compressed_tokens']:,} tokens")
                print(f"    Ratio: {live_result['compression_ratio']:.1f}×")

        print("=" * 75)

    return output


if __name__ == "__main__":
    main()
