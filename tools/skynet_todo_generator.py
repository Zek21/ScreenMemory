#!/usr/bin/env python3
"""
skynet_todo_generator.py -- Auto-generate TODO items from system analysis.

Sources:
  - Testing gaps (files without tests)
  - Stale code (old files with TODO comments)
  - Bus alerts needing action
  - Upgrade scan results (if available)

Usage:
    python tools/skynet_todo_generator.py              # Generate and write TODOs
    python tools/skynet_todo_generator.py --dry-run     # Preview without writing
    python tools/skynet_todo_generator.py --max 10      # Limit to N items
    python tools/skynet_todo_generator.py --json        # Output as JSON

# signed: delta
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
TODOS_FILE = DATA / "todos.json"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]
WORKER_SPECIALTIES = {
    "alpha": ["frontend", "dashboard", "UI", "architecture"],
    "beta": ["backend", "infrastructure", "daemons", "Python"],
    "gamma": ["security", "research", "analysis", "performance"],
    "delta": ["testing", "validation", "auditing", "docs"],
}

# Directories to scan for testing gaps
SOURCE_DIRS = ["tools", "core"]
EXCLUDE_PATTERNS = {"__pycache__", ".git", "node_modules", "env", ".venv", "data", "screenshots", "logs"}


def _load_existing_todos():
    """Load existing TODO items."""
    try:
        raw = json.loads(TODOS_FILE.read_text())
        items = raw.get("todos", []) if isinstance(raw, dict) else raw
        return items
    except Exception:
        return []


def _existing_titles(todos):
    """Get set of existing TODO titles for dedup."""
    return {t.get("title", "").lower().strip() for t in todos}


def _next_id(todos):
    """Generate next auto-generated TODO ID."""
    max_num = 0
    for t in todos:
        tid = t.get("id", "")
        if tid.startswith("auto_"):
            try:
                num = int(tid.split("_")[1])
                max_num = max(max_num, num)
            except (ValueError, IndexError):
                pass
    return max_num + 1


def _suggest_assignee(title, category):
    """Suggest best worker based on category and title keywords."""
    title_lower = title.lower()
    scores = {w: 0 for w in WORKER_NAMES}

    for worker, specialties in WORKER_SPECIALTIES.items():
        for spec in specialties:
            if spec in title_lower or spec in category:
                scores[worker] += 1

    # Category-based defaults
    if category == "testing":
        scores["delta"] += 2
    elif category == "security":
        scores["gamma"] += 2
    elif category == "infrastructure":
        scores["beta"] += 2
    elif category in ("frontend", "dashboard"):
        scores["alpha"] += 2

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "shared"


def find_testing_gaps(max_items=5):
    """Find Python source files without corresponding test files."""
    items = []
    test_dir = ROOT / "tests"
    existing_tests = set()

    # Collect existing test file targets
    if test_dir.exists():
        for tf in test_dir.rglob("test_*.py"):
            # test_foo.py -> foo
            name = tf.stem.replace("test_", "")
            existing_tests.add(name)
    # Also check root-level test files
    for tf in ROOT.glob("test_*.py"):
        name = tf.stem.replace("test_", "")
        existing_tests.add(name)

    # Scan source directories
    for src_dir_name in SOURCE_DIRS:
        src_dir = ROOT / src_dir_name
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            if any(part in EXCLUDE_PATTERNS for part in py_file.parts):
                continue
            if py_file.name.startswith("_") or py_file.name == "__init__.py":
                continue
            stem = py_file.stem
            if stem not in existing_tests:
                rel = py_file.relative_to(ROOT)
                items.append({
                    "title": f"Add tests for {rel}",
                    "priority": "medium",
                    "category": "testing",
                    "source": "testing_gap",
                })
                if len(items) >= max_items:
                    return items
    return items


def find_stale_todos(max_items=5, stale_days=30):
    """Find files with TODO comments that haven't been modified recently."""
    items = []
    cutoff = time.time() - (stale_days * 86400)

    for src_dir_name in SOURCE_DIRS + ["tools"]:
        src_dir = ROOT / src_dir_name
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            if any(part in EXCLUDE_PATTERNS for part in py_file.parts):
                continue
            try:
                mtime = py_file.stat().st_mtime
                if mtime > cutoff:
                    continue  # Recently modified, skip
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                todo_lines = []
                for i, line in enumerate(content.split("\n"), 1):
                    if "TODO" in line or "FIXME" in line or "HACK" in line:
                        todo_lines.append((i, line.strip()[:80]))
                if todo_lines:
                    rel = py_file.relative_to(ROOT)
                    items.append({
                        "title": f"Address {len(todo_lines)} TODO/FIXME in {rel} (stale {stale_days}+ days)",
                        "priority": "low",
                        "category": "code_quality",
                        "source": "stale_todo",
                        "details": [f"L{ln}: {txt}" for ln, txt in todo_lines[:3]],
                    })
                    if len(items) >= max_items:
                        return items
            except Exception:
                continue
    return items


def find_bus_alerts(max_items=5):
    """Find actionable bus alerts."""
    items = []
    try:
        from urllib.request import urlopen
        resp = urlopen("http://localhost:8420/bus/messages?limit=50", timeout=2)
        messages = json.loads(resp.read())
        if isinstance(messages, dict):
            messages = messages.get("messages", [])

        for m in messages:
            mtype = m.get("type", "")
            content = str(m.get("content", ""))
            if mtype in ("alert", "error", "incident", "STUCK_RECOVERED", "MODEL_DRIFT"):
                items.append({
                    "title": f"Investigate bus alert: {content[:80]}",
                    "priority": "high",
                    "category": "ops",
                    "source": "bus_alert",
                })
                if len(items) >= max_items:
                    return items
    except Exception:
        pass
    return items


def find_upgrade_scan_items(max_items=5):
    """Import items from upgrade scanner results."""
    items = []
    scan_file = DATA / "upgrade_scan_results.json"
    if not scan_file.exists():
        return items
    try:
        results = json.loads(scan_file.read_text())
        findings = results.get("findings", results.get("items", []))
        for f in findings:
            title = f.get("title", f.get("description", f.get("finding", "")))
            if not title:
                continue
            items.append({
                "title": f"Upgrade: {str(title)[:80]}",
                "priority": f.get("priority", "medium"),
                "category": "upgrade",
                "source": "upgrade_scanner",
            })
            if len(items) >= max_items:
                return items
    except Exception:
        pass
    return items


def generate_todos(max_total=20, dry_run=False):
    """Generate TODO items from all sources."""
    existing = _load_existing_todos()
    existing_set = _existing_titles(existing)
    id_counter = _next_id(existing)

    per_source = max(max_total // 4, 2)

    # Collect from all sources
    candidates = []
    candidates.extend(find_testing_gaps(per_source))
    candidates.extend(find_stale_todos(per_source))
    candidates.extend(find_bus_alerts(per_source))
    candidates.extend(find_upgrade_scan_items(per_source))

    # Deduplicate against existing
    new_items = []
    for c in candidates:
        if c["title"].lower().strip() in existing_set:
            continue
        assignee = _suggest_assignee(c["title"], c.get("category", ""))
        item = {
            "id": f"auto_{id_counter}",
            "title": c["title"],
            "status": "pending",
            "assignee": assignee,
            "priority": c.get("priority", "medium"),
            "wave": "auto_generated",
            "source": c.get("source", "unknown"),
            "generated_at": datetime.now().isoformat(),
        }
        new_items.append(item)
        existing_set.add(c["title"].lower().strip())
        id_counter += 1
        if len(new_items) >= max_total:
            break

    if not dry_run and new_items:
        # Write to todos.json
        try:
            from tools.skynet_atomic import safe_read_json, atomic_write_json
        except ImportError:
            safe_read_json = None
            atomic_write_json = None

        if safe_read_json and atomic_write_json:
            current = safe_read_json(TODOS_FILE, default={"todos": []})
        else:
            try:
                current = json.loads(TODOS_FILE.read_text())
            except Exception:
                current = {"todos": []}

        if isinstance(current, dict):
            current_list = current.get("todos", [])
        else:
            current_list = current

        current_list.extend(new_items)

        output = {"todos": current_list}
        if atomic_write_json:
            atomic_write_json(TODOS_FILE, output)
        else:
            TODOS_FILE.write_text(json.dumps(output, indent=2))

    return {
        "generated": len(new_items),
        "items": new_items,
        "existing_count": len(existing),
        "dry_run": dry_run,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    as_json = "--json" in sys.argv
    max_items = 20

    for i, arg in enumerate(sys.argv):
        if arg == "--max" and i + 1 < len(sys.argv):
            try:
                max_items = int(sys.argv[i + 1])
            except ValueError:
                pass

    result = generate_todos(max_total=max_items, dry_run=dry_run)

    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        mode = "DRY RUN" if dry_run else "GENERATED"
        print(f"\n=== Skynet TODO Generator ({mode}) ===")
        print(f"Existing TODOs: {result['existing_count']}")
        print(f"New items generated: {result['generated']}")

        if result["items"]:
            print(f"\n{'ID':<12} {'Priority':<10} {'Assignee':<10} {'Source':<16} Title")
            print(f"{'--':<12} {'--------':<10} {'--------':<10} {'------':<16} -----")
            for item in result["items"]:
                print(f"{item['id']:<12} {item['priority']:<10} {item['assignee']:<10} "
                      f"{item['source']:<16} {item['title'][:60]}")
        else:
            print("No new items to generate.")

        if not dry_run and result["generated"] > 0:
            print(f"\nWritten to {TODOS_FILE}")


if __name__ == "__main__":
    main()
